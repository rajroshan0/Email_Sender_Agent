"""Team email sender: password-protected web app that sends branded emails through Gmail."""
import base64
import csv
import hmac
import io
import json
import logging
import mimetypes
import os
import random
import re
import secrets
import threading
import time
from datetime import timedelta

import requests
from flask import (Flask, Response, abort, flash, g, jsonify, redirect, render_template,
                   request, session, url_for)
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from sqlalchemy import delete, func, insert, select, update
from werkzeug.middleware.proxy_fix import ProxyFix

from db import (accounts_t, assets_t, decrypt, encrypt, engine, init_db, job_files_t, jobs_t,
                recipients_t, settings_t, utcnow, _fernet)
from mailer import (DEFAULTS, EMAIL_RE, build_html, build_message, clean_line, subject_of)

os.environ.setdefault("OAUTHLIB_RELAXED_TOKEN_SCOPE", "1")
log = logging.getLogger("team-app")
logging.basicConfig(level=logging.INFO)

GMAIL_SEND = "https://www.googleapis.com/auth/gmail.send"
SCOPES = [GMAIL_SEND, "https://www.googleapis.com/auth/userinfo.email", "openid"]
MAX_ROWS = 1000
MAX_FILE = 10 * 1024 * 1024
MAX_FILES_TOTAL = 20 * 1024 * 1024
STATUSES = ["pending", "sending", "sent", "failed", "skipped", "uncertain"]

# ------------------------------------------------------------------ app setup
SECRET_KEY = os.environ.get("SECRET_KEY", "")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
BASE_URL = (os.environ.get("BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/")
if not SECRET_KEY or not APP_PASSWORD:
    raise RuntimeError("Set SECRET_KEY and APP_PASSWORD environment variables")
_fernet()  # fails early if TOKEN_ENCRYPTION_KEY is missing/invalid

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config.update(
    SECRET_KEY=SECRET_KEY,
    MAX_CONTENT_LENGTH=32 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=BASE_URL.startswith("https"),
)
app.permanent_session_lifetime = timedelta(hours=12)


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


@app.template_filter("fmt")
def fmt_dt(d):
    return d.strftime("%Y-%m-%d %H:%M UTC") if d else ""


@app.context_processor
def inject():
    return {"csrf_token": csrf_token, "nonce": getattr(g, "nonce", "")}


FAILS = {}  # ip -> [count, locked_until]


def client_ip():
    return request.remote_addr or "?"


@app.before_request
def guard():
    g.nonce = secrets.token_urlsafe(16)
    if request.endpoint in (None, "health"):
        return None
    if request.method == "POST":
        sent = request.form.get("csrf", "")
        if not sent or not hmac.compare_digest(sent, session.get("csrf", "")):
            abort(400, "Invalid or missing form token. Reload the page and try again.")
    if request.endpoint != "login" and not session.get("auth"):
        return redirect(url_for("login"))
    return None


@app.after_request
def headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers.setdefault("Cache-Control", "no-store")
    resp.headers.setdefault(
        "Content-Security-Policy",
        f"default-src 'self'; img-src 'self' https: data:; style-src 'self' 'unsafe-inline'; "
        f"script-src 'nonce-{getattr(g, 'nonce', '')}'; frame-ancestors 'self'")
    return resp


# ------------------------------------------------------------------ settings helpers
def get_settings():
    S = dict(DEFAULTS)
    with engine.connect() as c:
        for k, v in c.execute(select(settings_t.c.key, settings_t.c.value)):
            if k in S:
                S[k] = v
    return S


def get_logo():
    with engine.connect() as c:
        r = c.execute(select(assets_t.c.mime, assets_t.c.data).where(assets_t.c.name == "logo")).first()
    return (r[0], bytes(r[1])) if r else None


COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
LINK_RE = re.compile(r"^(https?://|mailto:)\S+$", re.I)
TEXT_FIELDS = ["BRAND_NAME", "FROM_NAME", "TITLE", "SUBTITLE", "DEFAULT_SUBJECT", "GREETING",
               "GREETING_NO_NAME", "SIGN_OFF", "SIGNER", "BUTTON_TEXT", "UNSUBSCRIBE_TEXT"]


def validate_settings(form):
    out, errs = {}, []
    for k in TEXT_FIELDS:
        v = clean_line(form.get(k, ""))
        if not v:
            errs.append(f"{k.replace('_', ' ').title()} cannot be empty")
        out[k] = v[:300]
    rt = clean_line(form.get("REPLY_TO", ""))
    if rt and not EMAIL_RE.match(rt):
        errs.append("Reply-To must be an email address or empty")
    out["REPLY_TO"] = rt
    link = clean_line(form.get("BUTTON_LINK", ""))
    if not LINK_RE.match(link):
        errs.append("Button link must start with https://, http:// or mailto:")
    out["BUTTON_LINK"] = link
    logo_url = clean_line(form.get("LOGO_URL", ""))
    if logo_url and not re.match(r"^https://\S+$", logo_url):
        errs.append("Logo URL must start with https://")
    out["LOGO_URL"] = logo_url
    for k in ("COLOR_MAIN", "COLOR_ACCENT"):
        v = clean_line(form.get(k, ""))
        if not COLOR_RE.match(v):
            errs.append(f"{k.replace('_', ' ').title()} must look like #12263F")
        out[k] = v
    lines = [clean_line(l) for l in form.get("FOOTER_LINES", "").splitlines() if clean_line(l)]
    if len(lines) > 8:
        errs.append("Footer can have at most 8 lines")
    out["FOOTER_LINES"] = "\n".join(l[:200] for l in lines[:8])
    try:
        d = float(form.get("DELAY_SECONDS", ""))
        assert 1 <= d <= 60
        out["DELAY_SECONDS"] = str(d).rstrip("0").rstrip(".")
    except (ValueError, AssertionError):
        errs.append("Delay must be a number from 1 to 60 seconds")
    try:
        n = int(form.get("DAILY_LIMIT", ""))
        assert 1 <= n <= 2000
        out["DAILY_LIMIT"] = str(n)
    except (ValueError, AssertionError):
        errs.append("Daily limit must be a whole number from 1 to 2000")
    tpl = form.get("TEMPLATE", "classic")
    if tpl not in ("classic", "minimal"):
        errs.append("Choose a design")
        tpl = "classic"
    out["TEMPLATE"] = tpl
    out["SHOW_BRAND_NAME_WITH_LOGO"] = "1" if form.get("SHOW_BRAND_NAME_WITH_LOGO") else "0"
    out["FIRST_NAME_ONLY"] = "1" if form.get("FIRST_NAME_ONLY") else "0"
    return out, errs


def save_settings(values):
    with engine.begin() as c:
        for k, v in values.items():
            if c.execute(update(settings_t).where(settings_t.c.key == k).values(value=v)).rowcount == 0:
                c.execute(insert(settings_t).values(key=k, value=v))


def sniff_image(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    return None


# ------------------------------------------------------------------ auth routes
@app.route("/health")
def health():
    with engine.connect() as c:
        c.execute(select(1))
    return "ok"


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        ip = client_ip()
        rec = FAILS.get(ip, [0, 0])
        if rec[1] > time.time():
            return render_template("login.html", error="Too many attempts. Wait a few minutes."), 429
        if hmac.compare_digest(request.form.get("password", "").encode(), APP_PASSWORD.encode()):
            FAILS.pop(ip, None)
            session.clear()
            session.permanent = True
            session["auth"] = True
            return redirect(url_for("index"))
        rec[0] += 1
        if rec[0] >= 5:
            rec = [0, time.time() + 300]
        FAILS[ip] = rec
        time.sleep(1)
        return render_template("login.html", error="Wrong password."), 401
    return render_template("login.html", error="")


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ------------------------------------------------------------------ dashboard
def job_counts(conn, job_ids):
    out = {j: {s: 0 for s in STATUSES} for j in job_ids}
    if job_ids:
        q = (select(recipients_t.c.job_id, recipients_t.c.status, func.count())
             .where(recipients_t.c.job_id.in_(job_ids))
             .group_by(recipients_t.c.job_id, recipients_t.c.status))
        for jid, st, n in conn.execute(q):
            out[jid][st] = n
    return out


@app.route("/")
def index():
    with engine.connect() as c:
        jobs = c.execute(
            select(jobs_t, accounts_t.c.email.label("account_email"))
            .select_from(jobs_t.outerjoin(accounts_t, jobs_t.c.account_id == accounts_t.c.id))
            .order_by(jobs_t.c.id.desc()).limit(50)).mappings().all()
        counts = job_counts(c, [j["id"] for j in jobs])
        n_accounts = c.execute(select(func.count()).select_from(accounts_t)).scalar()
    return render_template("index.html", jobs=jobs, counts=counts, n_accounts=n_accounts)


# ------------------------------------------------------------------ settings
@app.route("/settings", methods=["GET", "POST"])
def settings():
    S = get_settings()
    if request.method == "POST":
        values, errs = validate_settings(request.form)
        f = request.files.get("logo")
        new_logo = None
        if f and f.filename:
            data = f.read(1024 * 1024 + 1)
            mime = sniff_image(data)
            if len(data) > 1024 * 1024:
                errs.append("Logo must be smaller than 1 MB")
            elif not mime:
                errs.append("Logo must be a PNG or JPG file")
            else:
                new_logo = (mime, data)
        if errs:
            for e in errs:
                flash(e, "err")
            S.update(values)
            return render_template("settings.html", S=S, has_logo=bool(get_logo())), 400
        save_settings(values)
        with engine.begin() as c:
            if request.form.get("remove_logo"):
                c.execute(delete(assets_t).where(assets_t.c.name == "logo"))
            if new_logo:
                c.execute(delete(assets_t).where(assets_t.c.name == "logo"))
                c.execute(insert(assets_t).values(name="logo", mime=new_logo[0], data=new_logo[1]))
        flash("Settings saved.", "ok")
        return redirect(url_for("settings"))
    return render_template("settings.html", S=S, has_logo=bool(get_logo()))


@app.route("/logo")
def logo():
    lg = get_logo()
    if not lg:
        abort(404)
    return Response(lg[1], mimetype=lg[0], headers={"Cache-Control": "private, max-age=60"})


def logo_src_for(S):
    """For previews: the logo as a data: link (so the preview needs no login)."""
    if S["LOGO_URL"]:
        return S["LOGO_URL"]
    lg = get_logo()
    return f"data:{lg[0]};base64,{base64.b64encode(lg[1]).decode()}" if lg else ""


def preview_response(row, S):
    resp = Response(build_html(row, S, logo_src_for(S)), mimetype="text/html")
    resp.headers["Content-Security-Policy"] = (
        "default-src 'none'; img-src https: data:; style-src 'unsafe-inline'; frame-ancestors 'self'")
    return resp


SAMPLE_ROW = {"email": "sample@example.com", "name": "Sample Name", "greeting": "", "subject": "",
              "message": "This is how your message will look.\n\nA second paragraph, for example "
                         "with details and next steps."}


@app.route("/preview/sample")
def preview_sample():
    return preview_response(SAMPLE_ROW, get_settings())


# ------------------------------------------------------------------ gmail accounts (web OAuth)
def redirect_uri():
    return BASE_URL + "/oauth/callback"


def client_config():
    return {"web": {
        "client_id": os.environ["GOOGLE_CLIENT_ID"],
        "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": [redirect_uri()]}}


def oauth_ready():
    return bool(BASE_URL and os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET"))


@app.route("/accounts")
def accounts():
    with engine.connect() as c:
        rows = c.execute(select(accounts_t).order_by(accounts_t.c.id)).mappings().all()
    return render_template("accounts.html", accounts=rows, oauth_ready=oauth_ready(),
                           redirect_uri=redirect_uri() if BASE_URL else "(set BASE_URL)")


@app.post("/oauth/start")
def oauth_start():
    if not oauth_ready():
        flash("Google login is not configured (GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / BASE_URL).", "err")
        return redirect(url_for("accounts"))
    flow = Flow.from_client_config(client_config(), scopes=SCOPES, redirect_uri=redirect_uri())
    url, state = flow.authorization_url(access_type="offline", prompt="consent select_account",
                                        include_granted_scopes="false")
    session["oauth_state"] = state
    session["oauth_verifier"] = flow.code_verifier
    return redirect(url)


@app.route("/oauth/callback")
def oauth_callback():
    state = session.pop("oauth_state", None)
    verifier = session.pop("oauth_verifier", None)
    if request.args.get("error"):
        flash("Google login was cancelled or refused.", "err")
        return redirect(url_for("accounts"))
    if not state or request.args.get("state") != state:
        abort(400, "Invalid login state. Start again from the Accounts page.")
    try:
        flow = Flow.from_client_config(client_config(), scopes=SCOPES, state=state, redirect_uri=redirect_uri())
        flow.code_verifier = verifier
        flow.fetch_token(authorization_response=request.url)
        creds = flow.credentials
        if not creds.refresh_token:
            flash("Google did not return a long-term login. Remove this app at "
                  "myaccount.google.com/permissions and connect again.", "err")
            return redirect(url_for("accounts"))
        info = requests.get("https://www.googleapis.com/oauth2/v3/userinfo",
                            headers={"Authorization": f"Bearer {creds.token}"}, timeout=15).json()
        email = (info.get("email") or "").lower()
        if not email:
            raise ValueError("no email returned")
    except Exception:
        log.exception("OAuth callback failed")
        flash("Could not connect that Gmail account. Please try again.", "err")
        return redirect(url_for("accounts"))
    token_enc = encrypt(creds.refresh_token)
    with engine.begin() as c:
        if c.execute(update(accounts_t).where(accounts_t.c.email == email)
                     .values(token_enc=token_enc)).rowcount == 0:
            c.execute(insert(accounts_t).values(email=email, token_enc=token_enc, created_at=utcnow()))
    flash(f"Connected {email}.", "ok")
    return redirect(url_for("accounts"))


@app.post("/accounts/<int:aid>/delete")
def account_delete(aid):
    with engine.begin() as c:
        busy = c.execute(select(func.count()).select_from(jobs_t)
                         .where(jobs_t.c.account_id == aid, jobs_t.c.status == "running")).scalar()
        if busy:
            flash("A send is running with this account. Stop it first.", "err")
            return redirect(url_for("accounts"))
        c.execute(update(jobs_t).where(jobs_t.c.account_id == aid).values(account_id=None))
        c.execute(delete(accounts_t).where(accounts_t.c.id == aid))
    flash("Account removed.", "ok")
    return redirect(url_for("accounts"))


# ------------------------------------------------------------------ new job
EMAIL_KEYS = ("receiver_email", "receiver", "email", "to")
OVERRIDE_KEYS = ["TEMPLATE", "TITLE", "SUBTITLE", "BUTTON_TEXT", "BUTTON_LINK",
                 "DEFAULT_SUBJECT", "COLOR_MAIN", "COLOR_ACCENT"]
MAX_PASTE = 2 * 1024 * 1024


def parse_table(text):
    """Pasted table (from Excel / Google Sheets = tab separated) or CSV, or just a list of emails."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff").strip("\n")
    if not text.strip():
        return [], ""
    first = text.split("\n", 1)[0]
    if "\t" in first:
        delim = "\t"
    elif first.count(";") > first.count(","):
        delim = ";"
    else:
        delim = ","
    heads = {h.strip().lower() for h in next(csv.reader([first], delimiter=delim), [])}
    rows = []
    if not heads & set(EMAIL_KEYS):
        # no header row: accept a plain list of email addresses
        lines = [l for l in text.split("\n") if l.strip()]
        if not all("@" in l for l in lines):
            return None, ("The first row must be column names, for example: name, receiver_email, message "
                          "(or paste only email addresses, one per line).")
        for i, line in enumerate(lines, start=1):
            for tok in re.split(r"[,;\s]+", line):
                if tok:
                    rows.append({"row_no": i, "email": clean_line(tok), "name": "", "greeting": "",
                                 "subject": "", "message": "", "attachments": ""})
        if len(rows) > MAX_ROWS:
            return None, f"Too many rows (max {MAX_ROWS} per send). Split the list."
        return rows, ""
    reader = csv.DictReader(io.StringIO(text, newline=""), delimiter=delim)
    for i, raw in enumerate(reader, start=2):
        r = {(k or "").strip().lower(): (v or "") for k, v in raw.items() if k is not None}
        if not any(v.strip() for v in r.values()):
            continue

        def pick(*keys, multiline=False):
            for k in keys:
                if r.get(k, "").strip():
                    return r[k].strip() if multiline else clean_line(r[k])
            return ""
        rows.append({"row_no": i, "email": pick(*EMAIL_KEYS), "name": pick("name"),
                     "greeting": pick("greeting"), "subject": pick("subject"),
                     "message": pick("message", "text", multiline=True),
                     "attachments": ";".join(x.strip() for x in pick("attachment", "attachments").split(";") if x.strip())})
        if len(rows) > MAX_ROWS:
            return None, f"Too many rows (max {MAX_ROWS} per send). Split the list."
    return rows, ""


def analyze_recipients(text, common, attach_names, done):
    """Returns (rows, error). Each row gets status pending/skipped + detail."""
    rows, err = parse_table(text)
    if err:
        return None, err
    seen, out = set(), []
    for r in rows:
        status, detail = "pending", ""
        if not r["message"] and common:
            r = {**r, "message": common}
        if not EMAIL_RE.match(r["email"]):
            status, detail = "skipped", "Not a valid email address"
        elif r["email"].lower() in seen:
            status, detail = "skipped", "Listed twice"
        elif not r["message"]:
            status, detail = "skipped", "No message (write one in the message box or add a message column)"
        elif r["email"].lower() in done:
            status, detail = "skipped", "Already emailed before"
        else:
            missing = [a for a in r["attachments"].split(";") if a and a.lower() not in attach_names]
            if missing:
                status, detail = "skipped", "Attachment not uploaded: " + ", ".join(missing)
        if status == "pending":
            seen.add(r["email"].lower())
        out.append({**r, "status": status, "detail": detail})
    return out, ""


def read_overrides(form, S):
    """Per-send design choices. Invalid values fall back to the saved setting and are reported."""
    ov, errs = {}, []
    for k in OVERRIDE_KEYS:
        ov[k] = clean_line(form.get(k, S[k]))[:300]
    label = lambda k: k.replace("_", " ").title()
    if ov["TEMPLATE"] not in ("classic", "minimal"):
        errs.append("Choose a design")
        ov["TEMPLATE"] = S["TEMPLATE"]
    for k in ("TITLE", "SUBTITLE", "BUTTON_TEXT", "DEFAULT_SUBJECT"):
        if not ov[k]:
            errs.append(f"{label(k)} cannot be empty")
            ov[k] = S[k]
    if not LINK_RE.match(ov["BUTTON_LINK"]):
        errs.append("Button link must start with https://, http:// or mailto:")
        ov["BUTTON_LINK"] = S["BUTTON_LINK"]
    for k in ("COLOR_MAIN", "COLOR_ACCENT"):
        if not COLOR_RE.match(ov[k]):
            errs.append(f"{label(k)} must look like #12263F")
            ov[k] = S[k]
    S2 = dict(S)
    S2.update(ov)
    return S2, ov, errs


def sent_set(conn):
    return {e for (e,) in conn.execute(select(func.lower(recipients_t.c.email))
                                       .where(recipients_t.c.status == "sent"))}


def safe_filename(name):
    name = os.path.basename((name or "").replace("\\", "/"))
    name = re.sub(r"[\x00-\x1f]", "", name).strip()
    return name[:200]


def form_values(form, S):
    v = {k: form.get(k, S[k]) for k in OVERRIDE_KEYS}
    v.update(COMMON_MESSAGE=form.get("COMMON_MESSAGE", ""), recipients=form.get("recipients", ""),
             name=form.get("name", ""), skip_sent=(form.get("skip_sent") == "1") if form else True,
             account_id=form.get("account_id", ""))
    return v


@app.route("/new", methods=["GET", "POST"])
def new_job():
    S = get_settings()
    with engine.connect() as c:
        accts = c.execute(select(accounts_t.c.id, accounts_t.c.email).order_by(accounts_t.c.id)).all()

    def page(status=200):
        return render_template("new.html", accounts=accts, v=form_values(request.form, S)), status

    if request.method == "GET":
        return page()

    try:
        account_id = int(request.form.get("account_id", ""))
    except ValueError:
        account_id = 0
    if account_id not in [a[0] for a in accts]:
        flash("Choose a sender Gmail account (connect one on the Accounts page first).", "err")
        return page(400)
    S2, ov, errs = read_overrides(request.form, S)
    if errs:
        for e in errs:
            flash(e, "err")
        return page(400)

    files, total, seen_names = [], 0, set()
    for up in request.files.getlist("files"):
        name = safe_filename(up.filename)
        if not name:
            continue
        data = up.read(MAX_FILE + 1)
        if len(data) > MAX_FILE:
            flash(f"{name} is larger than 10 MB.", "err")
            return page(400)
        total += len(data)
        if name.lower() in seen_names:
            flash(f"Two attachments are named {name}.", "err")
            return page(400)
        seen_names.add(name.lower())
        files.append({"filename": name, "data": data,
                      "mime": mimetypes.guess_type(name)[0] or "application/octet-stream"})
    if total > MAX_FILES_TOTAL:
        flash("Attachments are larger than 20 MB in total.", "err")
        return page(400)

    skip_sent = request.form.get("skip_sent") == "1"
    job_name = clean_line(request.form.get("name", ""))[:200] or f"Send {utcnow():%Y-%m-%d %H:%M}"
    with engine.begin() as c:
        done = sent_set(c) if skip_sent else set()
        rows, err = analyze_recipients(request.form.get("recipients", "")[:MAX_PASTE],
                                       request.form.get("COMMON_MESSAGE", "").strip(), seen_names, done)
        if err:
            flash(err, "err")
            return page(400)
        if not rows:
            flash("Paste at least one recipient.", "err")
            return page(400)
        job_id = c.execute(insert(jobs_t).values(
            name=job_name, account_id=account_id, status="draft", skip_sent=skip_sent, note="",
            options=json.dumps(ov), created_at=utcnow())).inserted_primary_key[0]
        c.execute(insert(recipients_t), [{**r, "job_id": job_id, "sent_at": None} for r in rows])
        if files:
            c.execute(insert(job_files_t), [{**x, "job_id": job_id} for x in files])
    flash("Review the preview below, then press Send.", "ok")
    return redirect(url_for("job", jid=job_id))


@app.post("/preview/live")
def preview_live():
    S = get_settings()
    S2, ov, errs = read_overrides(request.form, S)
    common = request.form.get("COMMON_MESSAGE", "").strip()
    names = {n.strip().lower() for n in request.form.get("file_names", "").split("\n") if n.strip()}
    with engine.connect() as c:
        done = sent_set(c) if request.form.get("skip_sent") == "1" else set()
    rows, err = analyze_recipients(request.form.get("recipients", "")[:MAX_PASTE], common, names, done)
    rows = rows or []
    ready = [r for r in rows if r["status"] == "pending"]
    skipped = [r for r in rows if r["status"] == "skipped"]
    if ready:
        row = dict(ready[0])
    else:
        row = dict(SAMPLE_ROW)
        if common:
            row["message"] = common
    parts = []
    if err:
        parts.append(err)
    if rows:
        parts.append(f"{len(ready)} ready to send" + (f", {len(skipped)} skipped" if skipped else ""))
        for r in skipped[:4]:
            parts.append(f"Row {r['row_no']} ({r['email'] or 'empty'}): {r['detail']}")
    else:
        parts.append("Showing a sample. Paste recipients to see the first real email.")
    parts += errs
    return jsonify(html=build_html(row, S2, logo_src_for(S2)), summary="\n".join(parts))


def load_job(c, jid):
    j = c.execute(select(jobs_t, accounts_t.c.email.label("account_email"))
                  .select_from(jobs_t.outerjoin(accounts_t, jobs_t.c.account_id == accounts_t.c.id))
                  .where(jobs_t.c.id == jid)).mappings().first()
    if not j:
        abort(404)
    return j


@app.route("/job/<int:jid>")
def job(jid):
    with engine.connect() as c:
        j = load_job(c, jid)
        counts = job_counts(c, [jid])[jid]
        rows = c.execute(select(recipients_t).where(recipients_t.c.job_id == jid)
                         .order_by(recipients_t.c.id)).mappings().all()
        nfiles = c.execute(select(func.count()).select_from(job_files_t)
                           .where(job_files_t.c.job_id == jid)).scalar()
    return render_template("job.html", j=j, counts=counts, rows=rows, nfiles=nfiles)


@app.route("/job/<int:jid>/rows")
def job_rows(jid):
    with engine.connect() as c:
        load_job(c, jid)
        rows = c.execute(select(recipients_t).where(recipients_t.c.job_id == jid)
                         .order_by(recipients_t.c.id)).mappings().all()
    return render_template("_rows.html", rows=rows)


@app.route("/job/<int:jid>/status")
def job_status(jid):
    with engine.connect() as c:
        j = load_job(c, jid)
        counts = job_counts(c, [jid])[jid]
    return jsonify(status=j["status"], note=j["note"], counts=counts)


def job_options(jid):
    with engine.connect() as c:
        raw = c.execute(select(jobs_t.c.options).where(jobs_t.c.id == jid)).scalar() or "{}"
    try:
        data = json.loads(raw)
    except ValueError:
        data = {}
    return {k: v for k, v in data.items() if k in OVERRIDE_KEYS and isinstance(v, str)}


@app.route("/job/<int:jid>/preview")
def job_preview(jid):
    with engine.connect() as c:
        load_job(c, jid)
        row = c.execute(select(recipients_t).where(recipients_t.c.job_id == jid,
                                                   recipients_t.c.status != "skipped")
                        .order_by(recipients_t.c.id).limit(1)).mappings().first()
    if not row:
        return Response("<p style='font-family:sans-serif;padding:20px'>No valid emails in this send.</p>",
                        mimetype="text/html")
    S = get_settings()
    S.update(job_options(jid))
    return preview_response(dict(row), S)


@app.post("/job/<int:jid>/send")
def job_send(jid):
    with engine.begin() as c:
        j = load_job(c, jid)
        if j["account_id"] is None:
            flash("The sender account was removed. Create a new send with another account.", "err")
            return redirect(url_for("job", jid=jid))
        if j["status"] == "running":
            return redirect(url_for("job", jid=jid))
        c.execute(update(jobs_t).where(jobs_t.c.id == jid).values(status="running", note=""))
    start_job(jid)
    return redirect(url_for("job", jid=jid))


@app.post("/job/<int:jid>/stop")
def job_stop(jid):
    with engine.begin() as c:
        load_job(c, jid)
        c.execute(update(jobs_t).where(jobs_t.c.id == jid, jobs_t.c.status == "running")
                  .values(status="paused", note="Paused by you. Press Resume to continue."))
    flash("Pausing after the current email.", "ok")
    return redirect(url_for("job", jid=jid))


@app.post("/job/<int:jid>/retry")
def job_retry(jid):
    with engine.begin() as c:
        load_job(c, jid)
        n = c.execute(update(recipients_t).where(recipients_t.c.job_id == jid,
                                                 recipients_t.c.status == "failed")
                      .values(status="pending", detail="")).rowcount
    flash(f"{n} failed email(s) set to pending. Press Resume/Send to try again." if n
          else "No failed emails.", "ok")
    return redirect(url_for("job", jid=jid))


@app.post("/job/<int:jid>/delete")
def job_delete(jid):
    with engine.begin() as c:
        j = load_job(c, jid)
        if j["status"] == "running":
            flash("Stop the send before deleting it.", "err")
            return redirect(url_for("job", jid=jid))
        c.execute(delete(recipients_t).where(recipients_t.c.job_id == jid))
        c.execute(delete(job_files_t).where(job_files_t.c.job_id == jid))
        c.execute(delete(jobs_t).where(jobs_t.c.id == jid))
    flash("Deleted. (Emails already sent stay sent.)", "ok")
    return redirect(url_for("index"))


# ------------------------------------------------------------------ background sender
RUNNING = {}
RUN_LOCK = threading.Lock()


def get_gmail_service(refresh_token):
    creds = Credentials(token=None, refresh_token=refresh_token,
                        token_uri="https://oauth2.googleapis.com/token",
                        client_id=os.environ["GOOGLE_CLIENT_ID"],
                        client_secret=os.environ["GOOGLE_CLIENT_SECRET"], scopes=[GMAIL_SEND])
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def start_job(jid):
    with RUN_LOCK:
        t = RUNNING.get(jid)
        if t and t.is_alive():
            return
        t = threading.Thread(target=run_job, args=(jid,), daemon=True, name=f"job-{jid}")
        RUNNING[jid] = t
        t.start()


def set_job(jid, **vals):
    with engine.begin() as c:
        c.execute(update(jobs_t).where(jobs_t.c.id == jid).values(**vals))


def run_job(jid):
    try:
        _run_job(jid)
    except Exception as ex:
        log.exception("job %s crashed", jid)
        set_job(jid, status="stopped", note=f"Stopped by an unexpected error ({type(ex).__name__}). "
                                           "Press Resume to continue.")


def _run_job(jid):
    S = get_settings()
    S.update(job_options(jid))
    with engine.connect() as c:
        j = c.execute(select(jobs_t).where(jobs_t.c.id == jid)).mappings().first()
        acct = c.execute(select(accounts_t).where(accounts_t.c.id == j["account_id"])).mappings().first() if j else None
        files = {r["filename"].lower(): (r["filename"], r["mime"], bytes(r["data"]))
                 for r in c.execute(select(job_files_t).where(job_files_t.c.job_id == jid)).mappings()}
    if not j or not acct:
        set_job(jid, status="stopped", note="Sender account not found.")
        return
    logo = get_logo()
    try:
        service = get_gmail_service(decrypt(acct["token_enc"]))
    except Exception:
        set_job(jid, status="stopped", note="Could not use the sender account. Reconnect it on the Accounts page.")
        return
    delay, cap = float(S["DELAY_SECONDS"]), int(S["DAILY_LIMIT"])

    while True:
        with engine.begin() as c:
            if c.execute(select(jobs_t.c.status).where(jobs_t.c.id == jid)).scalar() != "running":
                return
            row = c.execute(select(recipients_t).where(recipients_t.c.job_id == jid,
                                                       recipients_t.c.status == "pending")
                            .order_by(recipients_t.c.id).limit(1)).mappings().first()
            if not row:
                c.execute(update(jobs_t).where(jobs_t.c.id == jid).values(status="done", note=""))
                return
            since = utcnow() - timedelta(hours=24)
            sent24 = c.execute(
                select(func.count()).select_from(recipients_t.join(jobs_t, recipients_t.c.job_id == jobs_t.c.id))
                .where(jobs_t.c.account_id == acct["id"], recipients_t.c.status == "sent",
                       recipients_t.c.sent_at >= since)).scalar()
            if sent24 >= cap:
                c.execute(update(jobs_t).where(jobs_t.c.id == jid).values(
                    status="paused", note=f"Daily limit of {cap} emails for {acct['email']} reached. "
                                          "Press Resume tomorrow (limit counts the last 24 hours)."))
                return
            c.execute(update(recipients_t).where(recipients_t.c.id == row["id"]).values(status="sending"))

        status, detail, stop_note = "sent", "", None
        try:
            atts = [files[a.lower()] for a in row["attachments"].split(";") if a]
            raw = build_message(dict(row), S, acct["email"], logo, atts)
            service.users().messages().send(userId="me", body={"raw": raw}).execute()
        except RefreshError:
            status, stop_note = "pending", "Google no longer accepts the login for this account. Reconnect it on the Accounts page."
        except HttpError as e:
            code = getattr(e.resp, "status", 0)
            text_ = str(e).lower()
            if code == 401:
                status, stop_note = "pending", "Google rejected the login for this account. Reconnect it on the Accounts page."
            elif code == 429 or (code == 403 and ("limit" in text_ or "quota" in text_)):
                status, stop_note = "pending", "Gmail sending limit reached for this account. Try again later."
            else:
                status, detail = "failed", f"Gmail error {code}: {str(getattr(e, 'reason', ''))[:150]}"
        except Exception as ex:
            status, detail = "failed", f"{type(ex).__name__}: {str(ex)[:150]}"

        with engine.begin() as c:
            c.execute(update(recipients_t).where(recipients_t.c.id == row["id"]).values(
                status=status, detail=detail, sent_at=utcnow() if status == "sent" else None))
            if stop_note:
                c.execute(update(jobs_t).where(jobs_t.c.id == jid).values(status="paused", note=stop_note))
        if stop_note:
            return
        time.sleep(delay + random.uniform(0, 1))


def recover_after_restart():
    with engine.begin() as c:
        c.execute(update(jobs_t).where(jobs_t.c.status == "running").values(
            status="paused", note="Interrupted by a restart. Press Resume to continue."))
        c.execute(update(recipients_t).where(recipients_t.c.status == "sending").values(
            status="uncertain", detail="Interrupted while sending. Check the Sent folder before retrying."))


init_db()
recover_after_restart()
