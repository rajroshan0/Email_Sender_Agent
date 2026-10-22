# Team Email Sender (Render, free setup)

A password-protected web page where your team uploads a CSV, previews the branded email,
chooses which connected Gmail account to send from, and presses Send. Sending runs in the
background; the page shows live progress. People already emailed are skipped.

## Deploy (about 25 minutes, no cost)

1. **Database (free Postgres, kept outside Render).** Render's own free database is deleted after
   30 days, so use another one. Create a NEW, separate free project (do not reuse your creator
   database):
   - Neon (neon.tech): create a project, copy the **connection string** (starts with `postgresql://`).
   - or Supabase: new project > Connect > **Session pooler** connection string. Replace
     `[YOUR-PASSWORD]` with the database password (URL-encode special characters).
   Keep the string secret; it is a password. Free Supabase projects can pause when unused;
   if the app cannot connect after a long break, restore the project in Supabase.
2. **GitHub:** create a *private* repository with the contents of this `team-app` folder
   (app.py, db.py, mailer.py, templates/, requirements.txt, render.yaml). Do NOT upload
   credentials.json, token.json or any .env file.
3. **Render:** New > Blueprint > pick the repo. Fill in when asked:
   - `DATABASE_URL`: the connection string from step 1.
   - `APP_PASSWORD`: the shared team password (long and unique).
   - `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`: type `todo` for now, fix in step 5.
4. **Find your address**, e.g. `https://email-sender-abcd.onrender.com` (shown on the service page).
5. **Google Cloud** (same project as before) > APIs & Services > Credentials > Create credentials >
   OAuth client ID > type **Web application**. Under *Authorized redirect URIs* add exactly:
   `https://YOUR-ADDRESS.onrender.com/oauth/callback`
   Copy the client ID and secret into Render (Environment) and let it redeploy.
6. Open your address, log in with the team password, go to **Gmail accounts** and press
   **Connect a Gmail account** for each account (log in, Advanced > Go to app > Allow).
7. **Design & settings:** brand, title, logo, contact button link, footer, colors, limits.
8. **New send:** choose account, upload CSV (+ attachment files), check the preview, press Send.

## Using the free web plan
- The app sleeps after 15 minutes without visits. Opening the page wakes it (about a minute).
- **Keep the send page open until the send finishes.** Its live updates keep the app awake.
  If the app sleeps or restarts mid-send, open the send and press **Resume**.
- An email that was in flight at that moment is marked *uncertain*; check the Gmail Sent folder
  before retrying it.
- Free plans have 750 hours per month, which is plenty for occasional use.

## CSV format
`receiver_email`, `message` required. Optional: `name`, `greeting`, `subject`, `attachment`
(file names, `;` between several; upload the files in the same form). Save as CSV UTF-8.
`{name}` inside message or subject is replaced by the name.

## Good to know
- Keep `--workers 1` in the start command (the background sender lives inside the app).
- Each account stops at the daily limit set in settings (default 400 per rolling 24 hours).
- Never change `TOKEN_ENCRYPTION_KEY` after connecting accounts: the stored logins would become
  unreadable and you would have to connect the accounts again.
- Anyone with the team password can send from every connected account. Change `APP_PASSWORD`
  in Render when someone leaves the team.

## Run on your own computer (optional test)
    python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
    export SECRET_KEY=$(python3 -c "import secrets;print(secrets.token_urlsafe(40))")
    export TOKEN_ENCRYPTION_KEY=$(python3 -c "import secrets;print(secrets.token_urlsafe(40))")
    export APP_PASSWORD=choose-one BASE_URL=http://localhost:5000
    python -m flask --app app run
(Without DATABASE_URL it uses a local SQLite file. Connecting Gmail locally also needs
GOOGLE_CLIENT_ID/SECRET, `http://localhost:5000/oauth/callback` as a redirect URI, and
`export OAUTHLIB_INSECURE_TRANSPORT=1`.)
