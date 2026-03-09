## TrialTracker

TrialTracker is a tiny Flask + SQLite dashboard that helps individuals and small teams stay ahead of free trial expirations.

It shows all of your SaaS trials in one place, color‑codes urgency, calculates potential monthly spend if everything converts, and emails you 7 days and 1 day before renewal.

### Features

- **Dashboard**: See all active trials with days remaining, urgency colors, and monthly cost.
- **Total potential spend**: One number at the top showing monthly cost if all trials convert.
- **Email reminders**: Automatic emails 7 days and 1 day before each trial ends.
- **CSV export**: One‑click export of all trials to CSV.
- **Onboarding**: Simple first‑run setup for your name and notification email.
- **Edit & delete**: Update or remove trials with confirmation.

### 1. Prerequisites

- **Python 3.10+**
- An SMTP provider (SendGrid, Gmail with app password, or any SMTP account)

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
   - **MAIL_HOST / MAIL_PORT / MAIL_USE_TLS / MAIL_USERNAME / MAIL_PASSWORD / MAIL_FROM** for your SMTP or SendGrid account.
   - **REMINDER_TASK_TOKEN**: generate a long random token (used to secure the reminder endpoint).

5. **Run the app**:

   ```bash
   set FLASK_ENV=development  # Windows (optional)
   python app.py
   ```

   Visit `http://localhost:5000` in your browser.

### 3. First‑time onboarding

On your first visit, TrialTracker shows a **setup screen**:

- Enter **your name** (used in reminder emails).
- Enter the **notification email address** where you want reminders delivered.

These values are stored in the local SQLite database (`trialtracker.db`). There is no login and no multi‑user system in this MVP.

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

The app exposes a small internal task endpoint:

- `GET /tasks/run-reminders?token=YOUR_REMINDER_TASK_TOKEN`

When this endpoint is called:

- It checks the database for trials that are exactly 7 or 1 days from expiry.
- It sends emails for any that have not yet received that specific reminder.
- It marks the reminder as sent so you don’t get duplicates.

To run reminders automatically:

- Use a **cron job**, **Railway scheduled task**, **Replit cron**, or an external uptime monitor to hit this URL once per day.

### 6. Deploying to Railway

1. **Push this folder to a Git repo** (GitHub, GitLab, etc.).
2. In Railway, **Create New Project → Deploy from Repo** and select the repo.
3. Set environment variables in the **Variables** tab:
   - `SECRET_KEY`
   - `MAIL_HOST`, `MAIL_PORT`, `MAIL_USE_TLS`, `MAIL_USERNAME`, `MAIL_PASSWORD`, `MAIL_FROM`
   - `REMINDER_TASK_TOKEN`
4. Railway will install from `requirements.txt` and start the app with `gunicorn`:

   ```bash
   gunicorn app:app
   ```

   (You can also set the start command explicitly in the Railway service settings.)

5. Optionally configure a **Railway Cron** or external scheduler to call:

   - `https://your-railway-domain/tasks/run-reminders?token=REMINDER_TASK_TOKEN`

### 7. Deploying to Replit

1. Create a new **Python / Flask** Replit.
2. Upload all files from this folder into the Replit project.
3. In Replit’s **Secrets** panel, add:
   - `SECRET_KEY`
   - `MAIL_HOST`, `MAIL_PORT`, `MAIL_USE_TLS`, `MAIL_USERNAME`, `MAIL_PASSWORD`, `MAIL_FROM`
   - `REMINDER_TASK_TOKEN`
4. Make sure the run command is something like:

   ```bash
   python app.py
   ```

5. Use an external scheduler (like cron or a ping service) to call:

   - `https://your-replit-url/tasks/run-reminders?token=REMINDER_TASK_TOKEN`

### 8. Data & storage

- All data lives in a single **SQLite file**: `trialtracker.db` in the project root.
- This is ideal for **single‑user** and **small team** deployments on inexpensive hosts.
- No external database is required for the MVP.

### 9. Security notes

- There are **no hardcoded secrets** in the codebase.
- Keep your `.env` file and SMTP credentials private.
- Use a long random value for `SECRET_KEY` and `REMINDER_TASK_TOKEN`.
- If you expose the app publicly, restrict who can access it (VPN, basic auth, or IP allow‑listing, depending on your host).

