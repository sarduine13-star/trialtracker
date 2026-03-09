import os
import csv
import sqlite3
from datetime import date, datetime
from pathlib import Path

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    send_file,
)
from werkzeug.middleware.proxy_fix import ProxyFix


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "trialtracker.db"


def create_app():
    app = Flask(__name__)
    app.wsgi_app = ProxyFix(app.wsgi_app)  # type: ignore[assignment]

    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
    app.config["DATABASE"] = str(DB_PATH)

    # Email configuration (SMTP or SendGrid SMTP)
    app.config["MAIL_HOST"] = os.environ.get("MAIL_HOST", "")
    app.config["MAIL_PORT"] = int(os.environ.get("MAIL_PORT", "587"))
    app.config["MAIL_USE_TLS"] = os.environ.get("MAIL_USE_TLS", "true").lower() == "true"
    app.config["MAIL_USERNAME"] = os.environ.get("MAIL_USERNAME", "")
    app.config["MAIL_PASSWORD"] = os.environ.get("MAIL_PASSWORD", "")
    app.config["MAIL_FROM"] = os.environ.get("MAIL_FROM", "")

    app.config["REMINDER_TASK_TOKEN"] = os.environ.get("REMINDER_TASK_TOKEN", "")

    with app.app_context():
        init_db()

    register_routes(app)
    return app


def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db_connection()
    cur = conn.cursor()

    # Settings table for single-user onboarding
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            owner_name TEXT NOT NULL,
            owner_email TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )

    # Trials table
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS trials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_name TEXT NOT NULL,
            vendor TEXT,
            monthly_cost REAL NOT NULL,
            trial_end_date TEXT NOT NULL,
            notes TEXT,
            reminder_7_sent INTEGER NOT NULL DEFAULT 0,
            reminder_1_sent INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """
    )

    conn.commit()
    conn.close()


def load_settings():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, owner_name, owner_email FROM settings WHERE id = 1")
    row = cur.fetchone()
    conn.close()
    return row


def register_routes(app: Flask) -> None:
    @app.before_request
    def ensure_onboarding():
        # Allow setup and static assets before onboarding is complete
        if request.endpoint in {"setup", "static"}:
            return None

        settings = load_settings()
        if settings is None:
            return redirect(url_for("setup"))
        return None

    @app.route("/setup", methods=["GET", "POST"])
    def setup():
        if request.method == "POST":
            owner_name = request.form.get("owner_name", "").strip()
            owner_email = request.form.get("owner_email", "").strip()

            if not owner_name or not owner_email:
                flash("Name and email are required.", "error")
                return render_template("setup.html", title="Welcome to TrialTracker")

            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("DELETE FROM settings")
            cur.execute(
                """
                INSERT INTO settings (id, owner_name, owner_email, created_at)
                VALUES (1, ?, ?, ?)
                """,
                (owner_name, owner_email, datetime.utcnow().isoformat()),
            )
            conn.commit()
            conn.close()

            flash("You're all set! Add your first trial.", "success")
            return redirect(url_for("dashboard"))

        return render_template("setup.html", title="Welcome to TrialTracker")

    @app.route("/")
    def dashboard():
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                id,
                product_name,
                vendor,
                monthly_cost,
                trial_end_date,
                notes,
                created_at
            FROM trials
            """
        )
        rows = cur.fetchall()
        conn.close()

        trials = []
        total_monthly_cost = 0.0
        today = date.today()

        for row in rows:
            end_date = datetime.strptime(row["trial_end_date"], "%Y-%m-%d").date()
            days_remaining = (end_date - today).days

            total_monthly_cost += float(row["monthly_cost"])

            if days_remaining < 0:
                urgency = "expired"
            elif days_remaining <= 2:
                urgency = "critical"
            elif days_remaining <= 7:
                urgency = "warning"
            else:
                urgency = "ok"

            trials.append(
                {
                    "id": row["id"],
                    "product_name": row["product_name"],
                    "vendor": row["vendor"],
                    "monthly_cost": float(row["monthly_cost"]),
                    "trial_end_date": end_date,
                    "days_remaining": days_remaining,
                    "notes": row["notes"],
                    "urgency": urgency,
                }
            )

        # Sort by days remaining, soonest first
        trials.sort(key=lambda t: t["days_remaining"])

        return render_template(
            "dashboard.html",
            title="TrialTracker",
            trials=trials,
            total_monthly_cost=total_monthly_cost,
        )

    @app.route("/trials/add", methods=["GET", "POST"])
    def add_trial():
        if request.method == "POST":
            product_name = request.form.get("product_name", "").strip()
            vendor = request.form.get("vendor", "").strip()
            monthly_cost = request.form.get("monthly_cost", "").strip()
            trial_end_date = request.form.get("trial_end_date", "").strip()
            notes = request.form.get("notes", "").strip()

            error = None
            if not product_name:
                error = "Product name is required."
            elif not monthly_cost:
                error = "Monthly cost is required."
            elif not trial_end_date:
                error = "Trial end date is required."

            try:
                monthly_cost_value = float(monthly_cost)
            except ValueError:
                error = "Monthly cost must be a number."
                monthly_cost_value = 0.0

            try:
                datetime.strptime(trial_end_date, "%Y-%m-%d").date()
            except ValueError:
                error = "Trial end date must be a valid date (YYYY-MM-DD)."

            if error:
                flash(error, "error")
                return render_template("add_edit_trial.html", title="Add Trial")

            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO trials (
                    product_name, vendor, monthly_cost, trial_end_date, notes, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    product_name,
                    vendor,
                    monthly_cost_value,
                    trial_end_date,
                    notes,
                    datetime.utcnow().isoformat(),
                ),
            )
            conn.commit()
            conn.close()

            flash("Trial added.", "success")
            return redirect(url_for("dashboard"))

        return render_template("add_edit_trial.html", title="Add Trial")

    @app.route("/trials/<int:trial_id>/edit", methods=["GET", "POST"])
    def edit_trial(trial_id: int):
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT id, product_name, vendor, monthly_cost, trial_end_date, notes FROM trials WHERE id = ?",
            (trial_id,),
        )
        row = cur.fetchone()

        if row is None:
            conn.close()
            flash("Trial not found.", "error")
            return redirect(url_for("dashboard"))

        if request.method == "POST":
            product_name = request.form.get("product_name", "").strip()
            vendor = request.form.get("vendor", "").strip()
            monthly_cost = request.form.get("monthly_cost", "").strip()
            trial_end_date = request.form.get("trial_end_date", "").strip()
            notes = request.form.get("notes", "").strip()

            error = None
            if not product_name:
                error = "Product name is required."
            elif not monthly_cost:
                error = "Monthly cost is required."
            elif not trial_end_date:
                error = "Trial end date is required."

            try:
                monthly_cost_value = float(monthly_cost)
            except ValueError:
                error = "Monthly cost must be a number."
                monthly_cost_value = float(row["monthly_cost"])

            try:
                datetime.strptime(trial_end_date, "%Y-%m-%d").date()
            except ValueError:
                error = "Trial end date must be a valid date (YYYY-MM-DD)."

            if error:
                flash(error, "error")
                conn.close()
                return render_template(
                    "add_edit_trial.html",
                    title="Edit Trial",
                    trial={
                        "id": row["id"],
                        "product_name": product_name or row["product_name"],
                        "vendor": vendor or row["vendor"],
                        "monthly_cost": monthly_cost or row["monthly_cost"],
                        "trial_end_date": trial_end_date or row["trial_end_date"],
                        "notes": notes or row["notes"],
                    },
                )

            cur.execute(
                """
                UPDATE trials
                SET product_name = ?, vendor = ?, monthly_cost = ?, trial_end_date = ?, notes = ?
                WHERE id = ?
                """,
                (
                    product_name,
                    vendor,
                    monthly_cost_value,
                    trial_end_date,
                    notes,
                    trial_id,
                ),
            )
            conn.commit()
            conn.close()

            flash("Trial updated.", "success")
            return redirect(url_for("dashboard"))

        trial = {
            "id": row["id"],
            "product_name": row["product_name"],
            "vendor": row["vendor"],
            "monthly_cost": row["monthly_cost"],
            "trial_end_date": row["trial_end_date"],
            "notes": row["notes"],
        }
        conn.close()

        return render_template("add_edit_trial.html", title="Edit Trial", trial=trial)

    @app.route("/trials/<int:trial_id>/delete", methods=["POST"])
    def delete_trial(trial_id: int):
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM trials WHERE id = ?", (trial_id,))
        conn.commit()
        conn.close()
        flash("Trial deleted.", "success")
        return redirect(url_for("dashboard"))

    @app.route("/export/csv")
    def export_csv():
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT product_name, vendor, monthly_cost, trial_end_date, notes, created_at
            FROM trials
            ORDER BY trial_end_date ASC
            """
        )
        rows = cur.fetchall()
        conn.close()

        output_path = BASE_DIR / "trials_export.csv"
        with output_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    "Product",
                    "Vendor",
                    "Monthly Cost",
                    "Trial End Date",
                    "Notes",
                    "Created At",
                ]
            )
            for row in rows:
                writer.writerow(
                    [
                        row["product_name"],
                        row["vendor"],
                        row["monthly_cost"],
                        row["trial_end_date"],
                        row["notes"],
                        row["created_at"],
                    ]
                )

        return send_file(
            output_path,
            mimetype="text/csv",
            as_attachment=True,
            download_name="trials.csv",
        )

    @app.route("/tasks/run-reminders")
    def run_reminders():
        expected_token = app.config.get("REMINDER_TASK_TOKEN") or ""
        provided = request.args.get("token", "")
        if not expected_token or provided != expected_token:
            return "Unauthorized", 401

        sent = send_due_reminders(app)
        return {"sent": sent}


def send_due_reminders(app: Flask) -> int:
    import smtplib
    from email.message import EmailMessage

    conn = get_db_connection()
    cur = conn.cursor()

    today = date.today()
    today_str = today.strftime("%Y-%m-%d")

    # Trials exactly 7 or 1 days away that haven't had that reminder yet
    cur.execute(
        """
        SELECT id, product_name, vendor, monthly_cost, trial_end_date,
               reminder_7_sent, reminder_1_sent
        FROM trials
        """
    )
    rows = cur.fetchall()

    settings = load_settings()
    if settings is None:
        conn.close()
        return 0

    owner_name = settings["owner_name"]
    owner_email = settings["owner_email"]

    to_send = []
    for row in rows:
        end_date = datetime.strptime(row["trial_end_date"], "%Y-%m-%d").date()
        days_remaining = (end_date - today).days
        if days_remaining == 7 and not row["reminder_7_sent"]:
            to_send.append((row, 7))
        elif days_remaining == 1 and not row["reminder_1_sent"]:
            to_send.append((row, 1))

    if not to_send:
        conn.close()
        return 0

    host = app.config.get("MAIL_HOST")
    port = app.config.get("MAIL_PORT")
    use_tls = app.config.get("MAIL_USE_TLS", True)
    username = app.config.get("MAIL_USERNAME")
    password = app.config.get("MAIL_PASSWORD")
    from_email = app.config.get("MAIL_FROM") or username

    if not host or not from_email:
        conn.close()
        return 0

    sent_count = 0
    try:
        with smtplib.SMTP(host, port) as smtp:
            if use_tls:
                smtp.starttls()
            if username and password:
                smtp.login(username, password)

            for row, days in to_send:
                msg = EmailMessage()
                subject = f"Trial ending in {days} day{'s' if days != 1 else ''}: {row['product_name']}"
                msg["Subject"] = subject
                msg["From"] = from_email
                msg["To"] = owner_email

                lines = [
                    f"Hi {owner_name},",
                    "",
                    f"Your trial for {row['product_name']} is ending in {days} day{'s' if days != 1 else ''}.",
                    f"Vendor: {row['vendor'] or 'N/A'}",
                    f"Monthly cost if converted: ${row['monthly_cost']:.2f}",
                    f"Trial end date: {row['trial_end_date']}",
                    "",
                    "Decide whether to keep, cancel, or downgrade so you’re not surprised by charges.",
                    "",
                    "— TrialTracker",
                ]
                msg.set_content("\n".join(lines))
                smtp.send_message(msg)
                sent_count += 1

                if days == 7:
                    cur.execute(
                        "UPDATE trials SET reminder_7_sent = 1 WHERE id = ?",
                        (row["id"],),
                    )
                elif days == 1:
                    cur.execute(
                        "UPDATE trials SET reminder_1_sent = 1 WHERE id = ?",
                        (row["id"],),
                    )

        conn.commit()
    finally:
        conn.close()

    return sent_count


app = create_app()


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))

