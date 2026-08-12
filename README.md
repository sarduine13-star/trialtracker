## TrialTracker

TrialTracker is a small Flask dashboard that helps individuals and small teams stay ahead of free trial expirations.

It's a free multi-user hosted app: anyone can create an account, add their own SaaS trials, and get color-coded urgency, potential monthly spend, and automatic email reminders 7 days and 1 day before renewal. Each account only ever sees its own trials.

### Features

- **Accounts**: Register / log in / log out. Passwords are hashed; each account's data is isolated from every other account.
- **Dashboard**: See all of your active trials with days remaining, urgency colors, and monthly cost.
- **Total potential spend**: One number at the top showing monthly cost if all your trials convert.
- **Email reminders**: Automatic emails 7 days and 1 day before each trial ends, sent to the account's own email.
- **CSV export**: One‑click export of your trials to CSV.
- **Edit & delete**: Update or remove your trials with confirmation.

### 1. Prerequisites

- **Python 3.10+**
- A Resend account with a verified sending domain (reminders send via Resend's HTTPS API, not SMTP — outbound SMTP is blocked on many hosting plans, including Railway's free/hobby tier)

### 2. Setup (local)

1. **Clone / download** this folder onto your machine.

2. **Create and activate a virtual environment** (recommended):

   ```bash
   cd "trial traker_files"
   python -m venv .venv
   .venv\Scripts\activate  # On Windows
   # source .venv/bin/activate  # On macOS / Linux
   ```

3. **Install dependencies**:

   ```bash
   pip install -r requirements.txt
   ```

4. **Create your `.env` file** based on `.env.example`:

   ```bash
   copy .env.example .env  # Windows
   # cp .env.example .env  # macOS / Linux
   ```

   Then open `.env` and set:

   - **SECRET_KEY**: any long random string.
   - **DATABASE_URL**: optional locally — leave unset to use a local SQLite file (`trialtracker.db`), or point at Postgres to match production.
   - **RESEND_API_KEY / MAIL_FROM** for your Resend account (operator-configured; individual users never see or set these). `MAIL_FROM` must be on a domain verified in Resend.
   - **REMINDER_TASK_TOKEN**: generate a long random token (used to secure the reminder endpoint).

5. **Run the app**:

   ```bash
   set FLASK_ENV=development  # Windows (optional, relaxes the session cookie for local http)
   python app.py
   ```

   Visit `http://localhost:5000` in your browser.

### 3. Accounts

TrialTracker is multi-user. On first visit you'll be redirected to **Log in**, with a link to **Create an account** (name, email, password). Each account only ever sees, edits, deletes, or exports its own trials — trying to access another account's trial by guessing its URL returns a 404.

Your account email is also where reminder emails are sent — there's nothing else to configure.

### 4. Using the dashboard

- **Add a trial** with:
  - Product name
  - Vendor / account
  - Monthly cost
  - Trial end date
  - Optional notes
- The dashboard:
  - Shows **days remaining** with urgency colors.
  - Shows **monthly cost** per trial.
  - Shows **total potential monthly cost** at the top.
  - Lets you **edit** or **delete** any trial (delete asks for confirmation).
  - Provides **“Export CSV”** to download all trials.

### 5. Email reminders

TrialTracker can send **two reminder emails per trial**:

- **7 days before** the trial end date.
- **1 day before** the trial end date.

Reminders go to the **notification email** you entered during setup.

#### How reminders are triggered

The app exposes a small internal task endpoint, authenticated with a bearer token header (not a query string, so it never appears in URLs or logs):

- `GET /tasks/run-reminders` with header `Authorization: Bearer YOUR_REMINDER_TASK_TOKEN`

When this endpoint is called:

- It checks the database for trials that are exactly 7 or 1 days from expiry.
- It sends emails (via Resend's HTTPS API) for any that have not yet received that specific reminder.
- It marks the reminder as sent so you don’t get duplicates.

To run reminders automatically:

- Use a **cron job**, **Railway scheduled task**, **Replit cron**, or an external uptime monitor to hit this URL once per day with the `Authorization: Bearer` header set.

### 6. Deploying to Railway

1. **Push this folder to a Git repo** (GitHub, GitLab, etc.).
2. In Railway, **Create New Project → Deploy from Repo** and select the repo.
3. **Add a Postgres database** to the project (Railway → New → Database → PostgreSQL). Railway automatically injects `DATABASE_URL` into your service — you don't need to set it by hand.
4. Set the remaining environment variables in the **Variables** tab:
   - `SECRET_KEY` — long random string. Required; the app refuses to start without it unless `FLASK_ENV=development`.
   - `RESEND_API_KEY`, `MAIL_FROM`
   - `REMINDER_TASK_TOKEN` — long random token
   - `FLASK_ENV=production` (or leave unset — production is the default)
5. Railway installs from `requirements.txt` (including `psycopg2-binary` for Postgres) and starts the app via `Procfile` / `railway.json`:

   ```bash
   gunicorn app:app
   ```

6. Configure a **Railway Cron** (or external scheduler) to call once per day:

   - `GET https://your-railway-domain/tasks/run-reminders` with header `Authorization: Bearer REMINDER_TASK_TOKEN`

### 7. Deploying to Replit

1. Create a new **Python / Flask** Replit.
2. Upload all files from this folder into the Replit project.
3. In Replit’s **Secrets** panel, add:
   - `SECRET_KEY`
   - `RESEND_API_KEY`, `MAIL_FROM`
   - `REMINDER_TASK_TOKEN`
4. Make sure the run command is something like:

   ```bash
   python app.py
   ```

5. Use an external scheduler (like cron or a ping service) to call:

   - `GET https://your-replit-url/tasks/run-reminders` with header `Authorization: Bearer REMINDER_TASK_TOKEN`

### 8. Data & storage

- Production uses **PostgreSQL** via `DATABASE_URL` (Flask-SQLAlchemy).
- If `DATABASE_URL` is unset, the app falls back to a local **SQLite file** (`trialtracker.db`) — development only, not tracked in git.
- Tables (`users`, `trials`) are created automatically on startup.

### 9. Security notes

- There are **no hardcoded secrets** in the codebase.
- Passwords are hashed with Werkzeug's `generate_password_hash`; plaintext passwords are never stored.
- Every trial belongs to a `user_id`; all trial routes filter by the logged-in user and return 404 (not another user's data) if an id doesn't belong to them.
- `SECRET_KEY` has no insecure default outside of `FLASK_ENV=development` — production refuses to start without it.
- Session cookies are `HttpOnly`, `SameSite=Lax`, and `Secure` outside of development.
- POST requests require a per-session CSRF token embedded in each form.
- The `/tasks/run-reminders` endpoint requires the `REMINDER_TASK_TOKEN` as an `Authorization: Bearer` header (never a query string), and the token is never rendered into any page HTML.
- Keep your `.env` file and Resend/database credentials private — never commit them.

