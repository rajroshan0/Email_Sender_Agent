"""Builds the branded HTML + plain-text email. Settings come in as a dict (see DEFAULTS)."""
import base64
import html
import mimetypes
import re
from email.message import EmailMessage
from email.utils import formataddr

DEFAULTS = {
    "BRAND_NAME": "Your Brand",
    "FROM_NAME": "Your Brand",
    "REPLY_TO": "",
    "TITLE": "Your Brand Title",
    "SUBTITLE": "A short line about your brand",
    "LOGO_URL": "",
    "SHOW_BRAND_NAME_WITH_LOGO": "1",
    "DEFAULT_SUBJECT": "A message from Your Brand",
    "GREETING": "Hi {name},",
    "GREETING_NO_NAME": "Hello,",
    "FIRST_NAME_ONLY": "0",
    "SIGN_OFF": "Best regards,",
    "SIGNER": "Your Name",
    "BUTTON_TEXT": "Contact me",
    "BUTTON_LINK": "mailto:you@yourbrand.com",
    "FOOTER_LINES": "you@yourbrand.com\n+000 000 000\nwww.yourbrand.com",
    "UNSUBSCRIBE_TEXT": 'Do not want these emails? Just reply with "unsubscribe" and I will remove you.',
    "COLOR_MAIN": "#12263F",
    "COLOR_ACCENT": "#C8963E",
    "DELAY_SECONDS": "3",
    "DAILY_LIMIT": "400",
}

EMAIL_RE = re.compile(r"^[^@\s,;<>\"']+@[^@\s,;<>\"']+\.[^@\s,;<>\"']+$")


def clean_line(s):
    """Single line, no header-injection characters."""
    return re.sub(r"[\r\n\t]+", " ", s or "").strip()


def footer_lines(S):
    return [l.strip() for l in S["FOOTER_LINES"].splitlines() if l.strip()]


def display_name(row, S):
    name = clean_line(row.get("name", ""))
    if name and S["FIRST_NAME_ONLY"] == "1":
        return name.split()[0]
    return name


def fill(text, name):
    return text.replace("{name}", name or "")


def greeting(row, S):
    if row.get("greeting"):
        return clean_line(row["greeting"])
    name = display_name(row, S)
    if name:
        return S["GREETING"].replace("{name}", name)
    return S["GREETING_NO_NAME"]


def subject_of(row, S):
    return clean_line(fill(row.get("subject") or S["DEFAULT_SUBJECT"], display_name(row, S)))


def plain_text(row, S):
    msg = fill(row["message"], display_name(row, S))
    return "\n".join([greeting(row, S), "", msg, "", S["SIGN_OFF"], S["SIGNER"], "",
                      f"{S['BUTTON_TEXT']}: {S['BUTTON_LINK']}", "", " | ".join(footer_lines(S)),
                      "", S["UNSUBSCRIBE_TEXT"]])


def build_html(row, S, logo_src):
    e = html.escape
    main, accent = S["COLOR_MAIN"], S["COLOR_ACCENT"]
    font = "Segoe UI,Helvetica,Arial,sans-serif"

    text = fill(row["message"], display_name(row, S)).replace("\r\n", "\n")
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    body = "".join(
        f'<p style="margin:0 0 16px 0;font-size:16px;line-height:1.6;color:#333333;">'
        f'{e(p).replace(chr(10), "<br>")}</p>' for p in paras)

    logo = ""
    if logo_src:
        logo = (f'<img src="{e(logo_src)}" alt="{e(S["BRAND_NAME"])}" width="140" '
                f'style="display:block;margin:0 auto;border:0;max-width:140px;height:auto;">')
    brand = ""
    if not logo_src or S["SHOW_BRAND_NAME_WITH_LOGO"] == "1":
        brand = (f'<div style="font-family:{font};font-size:15px;font-weight:700;letter-spacing:3px;'
                 f'text-transform:uppercase;color:{main};margin-top:{"10px" if logo else "0"};">'
                 f'{e(S["BRAND_NAME"])}</div>')

    footer = " &nbsp;&#8226;&nbsp; ".join(e(x) for x in footer_lines(S))

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f2f4f7;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f2f4f7;">
<tr><td align="center" style="padding:24px 12px;">
  <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="width:100%;max-width:600px;background:#ffffff;border-radius:6px;overflow:hidden;">
    <tr><td align="center" style="padding:28px 24px 22px 24px;background:#ffffff;font-family:{font};">{logo}{brand}</td></tr>
    <tr><td align="center" style="background:{main};padding:28px 32px;font-family:{font};border-bottom:4px solid {accent};">
      <div style="font-size:26px;line-height:1.3;font-weight:700;color:#ffffff;">{e(S["TITLE"])}</div>
      <div style="font-size:15px;line-height:1.5;color:{accent};margin-top:8px;">{e(S["SUBTITLE"])}</div>
    </td></tr>
    <tr><td style="padding:32px 36px 8px 36px;font-family:{font};">
      <p style="margin:0 0 18px 0;font-size:17px;font-weight:600;color:{main};">{e(greeting(row, S))}</p>
      {body}
    </td></tr>
    <tr><td align="center" style="padding:12px 36px 28px 36px;font-family:{font};">
      <table role="presentation" cellpadding="0" cellspacing="0"><tr>
        <td align="center" bgcolor="{accent}" style="border-radius:4px;">
          <a href="{e(S["BUTTON_LINK"])}" style="display:inline-block;padding:13px 30px;font-family:{font};font-size:15px;font-weight:700;color:{main};text-decoration:none;">{e(S["BUTTON_TEXT"])}</a>
        </td></tr></table>
    </td></tr>
    <tr><td style="padding:0 36px 26px 36px;font-family:{font};font-size:16px;line-height:1.5;color:#333333;">
      {e(S["SIGN_OFF"])}<br><strong style="color:{main};">{e(S["SIGNER"])}</strong>
    </td></tr>
    <tr><td align="center" style="background:#f7f8fa;padding:20px 24px;border-top:1px solid #e3e6eb;font-family:{font};">
      <div style="font-size:13px;color:{main};font-weight:600;">{e(S["BRAND_NAME"])}</div>
      <div style="font-size:12px;color:#666666;margin-top:6px;line-height:1.6;">{footer}</div>
      <div style="font-size:11px;color:#8a8f98;margin-top:10px;line-height:1.5;">{e(S["UNSUBSCRIBE_TEXT"])}</div>
    </td></tr>
  </table>
</td></tr></table>
</body></html>"""


def build_message(row, S, sender_email, logo=None, attachments=()):
    """row: dict. logo: (mime, bytes) or None. attachments: [(filename, mime, bytes)]. Returns Gmail 'raw' string."""
    use_cid = bool(logo) and not S["LOGO_URL"]
    logo_src = S["LOGO_URL"] or ("cid:logo" if use_cid else "")

    msg = EmailMessage()
    msg["To"] = clean_line(row["email"])
    msg["From"] = formataddr((clean_line(S["FROM_NAME"]), sender_email))
    msg["Subject"] = subject_of(row, S)
    if S["REPLY_TO"]:
        msg["Reply-To"] = clean_line(S["REPLY_TO"])
    msg.set_content(plain_text(row, S))
    msg.add_alternative(build_html(row, S, logo_src), subtype="html")

    if use_cid:
        maintype, subtype = logo[0].split("/")
        msg.get_payload()[1].add_related(logo[1], maintype, subtype, cid="<logo>")

    for filename, mime, data in attachments:
        mime = mime or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        maintype, subtype = mime.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()
