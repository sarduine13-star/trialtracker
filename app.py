import calendar
import csv
import hashlib
import io
import json
import os
import re
import secrets
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv
from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from flask_login import (
    LoginManager,
    UserMixin,
    current_user,
    login_required,
    login_user,
    logout_user,
)
from flask_sqlalchemy import SQLAlchemy
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "trialtracker.db"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PASSWORD_RESET_SALT = "password-reset"
PASSWORD_RESET_MAX_AGE_SECONDS = 3600

db = SQLAlchemy()
login_manager = LoginManager()
login_manager.login_view = "login"
login_manager.login_message = "Please log in to continue."
login_manager.login_message_category = "error"


class User(db.Model, UserMixin):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    trials = db.relationship(
        "Trial", backref="user", lazy=True, cascade="all, delete-orphan"
    )

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)


class Trial(db.Model):
    __tablename__ = "trials"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False, index=True
    )
    product_name = db.Column(db.String(200), nullable=False)
    vendor = db.Column(db.String(200))
    monthly_cost = db.Column(db.Float, nullable=False)
    trial_end_date = db.Column(db.String(10), nullable=False)  # YYYY-MM-DD
    notes = db.Column(db.Text)
    # "trial" (one-time, non-renewing) or "subscription" (recurring).
    # Existing rows default to "trial" so prior behavior is preserved.
    kind = db.Column(db.String(20), nullable=False, default="trial", server_default="trial")
    # "monthly" or "yearly"; only meaningful when kind == "subscription".
    billing_cycle = db.Column(db.String(10))
    reminder_7_sent = db.Column(db.Boolean, nullable=False, default=False)
    reminder_1_sent = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def create_app():
    app = Flask(__name__)
    app.wsgi_app = ProxyFix(app.wsgi_app)  # type: ignore[assignment]

    flask_env = os.environ.get("FLASK_ENV", "production")

    secret_key = os.environ.get("SECRET_KEY")
    if not secret_key:
        if flask_env == "development":
            secret_key = "dev-secret-change-me"
        else:
            raise RuntimeError(
                "SECRET_KEY environment variable must be set (FLASK_ENV is not "
                "'development')."
            )
    app.config["SECRET_KEY"] = secret_key

    database_url = os.environ.get("DATABASE_URL", "").strip()
    if database_url:
        if database_url.startswith("postgres://"):
            database_url = database_url.replace("postgres://", "postgresql://", 1)
        app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True}
    else:
        database_url = f"sqlite:///{DB_PATH}"
    app.config["SQLALCHEMY_DATABASE_URI"] = database_url
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    # Email configuration (Resend HTTPS API) - set by the operator only.
    app.config["RESEND_API_KEY"] = os.environ.get("RESEND_API_KEY", "")
    app.config["MAIL_FROM"] = os.environ.get("MAIL_FROM", "")

    app.config["REMINDER_TASK_TOKEN"] = os.environ.get("REMINDER_TASK_TOKEN", "")
    app.config["CONTACT_EMAIL"] = os.environ.get("CONTACT_EMAIL", "")

    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = flask_env != "development"

    db.init_app(app)
    login_manager.init_app(app)

    with app.app_context():
        db.create_all()
        run_additive_migrations()

    register_routes(app)
    return app


def run_additive_migrations() -> None:
    """Add newly introduced columns to an existing 'trials' table.

    db.create_all() only creates missing tables, not missing columns on a
    table that already exists in production. This adds any columns the
    current model expects but the database doesn't have yet, without
    touching existing rows/tables otherwise.
    """
    inspector = db.inspect(db.engine)
    if "trials" not in inspector.get_table_names():
        return

    existing_columns = {col["name"] for col in inspector.get_columns("trials")}
    with db.engine.begin() as conn:
        if "kind" not in existing_columns:
            conn.execute(
                db.text(
                    "ALTER TABLE trials ADD COLUMN kind VARCHAR(20) NOT NULL DEFAULT 'trial'"
                )
            )
        if "billing_cycle" not in existing_columns:
            conn.execute(db.text("ALTER TABLE trials ADD COLUMN billing_cycle VARCHAR(10)"))


def normalize_email(raw_email: str) -> str:
    return raw_email.strip().lower()


def _password_reset_serializer(app: Flask) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(app.config["SECRET_KEY"], salt=PASSWORD_RESET_SALT)


def _password_fingerprint(password_hash: str) -> str:
    return hashlib.sha256(password_hash.encode("utf-8")).hexdigest()[:16]


def generate_password_reset_token(app: Flask, user: "User") -> str:
    """Sign a time-limited token binding this reset link to the user's
    current password hash, so resetting the password (or requesting a
    fresh link) invalidates any older outstanding link automatically,
    without needing a separate token table.
    """
    payload = {"uid": user.id, "pwfp": _password_fingerprint(user.password_hash)}
    return _password_reset_serializer(app).dumps(payload)


def verify_password_reset_token(app: Flask, token: str):
    """Return the User the token was issued for, or None if the token is
    missing, tampered, expired, or has already been used (the bound
    password fingerprint no longer matches the user's current hash).
    """
    try:
        payload = _password_reset_serializer(app).loads(
            token, max_age=PASSWORD_RESET_MAX_AGE_SECONDS
        )
    except (BadSignature, SignatureExpired):
        return None

    user = db.session.get(User, payload.get("uid"))
    if user is None:
        return None
    if not secrets.compare_digest(_password_fingerprint(user.password_hash), payload.get("pwfp", "")):
        return None
    return user


def add_months(d: date, months: int) -> date:
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def add_years(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:
        # Feb 29 landing on a non-leap year.
        return d.replace(year=d.year + years, day=28)


def advance_renewal_date(current: date, billing_cycle: str) -> date:
    if billing_cycle == "yearly":
        return add_years(current, 1)
    return add_months(current, 1)


def advance_due_subscriptions() -> None:
    """Roll any subscription whose renewal date has passed forward to its
    next upcoming renewal date, resetting reminder flags for the new cycle.

    Trials are never advanced. Subscriptions more than one billing period
    overdue (e.g. the app was down for a couple of months) are advanced
    repeatedly until the renewal date is no longer in the past.
    """
    today = date.today()
    overdue = Trial.query.filter(
        Trial.kind == "subscription", Trial.trial_end_date < today.isoformat()
    ).all()

    changed = False
    for trial in overdue:
        end_date = datetime.strptime(trial.trial_end_date, "%Y-%m-%d").date()
        billing_cycle = trial.billing_cycle or "monthly"
        while end_date < today:
            end_date = advance_renewal_date(end_date, billing_cycle)
        trial.trial_end_date = end_date.strftime("%Y-%m-%d")
        trial.reminder_7_sent = False
        trial.reminder_1_sent = False
        changed = True

    if changed:
        db.session.commit()


def register_routes(app: Flask) -> None:
    @app.before_request
    def csrf_protect():
        if request.method == "POST":
            token = session.get("_csrf_token")
            submitted = request.form.get("csrf_token")
            if not token or not submitted or not secrets.compare_digest(token, submitted):
                abort(400, description="Invalid or missing CSRF token.")

    def get_csrf_token() -> str:
        if "_csrf_token" not in session:
            session["_csrf_token"] = secrets.token_hex(32)
        return session["_csrf_token"]

    app.jinja_env.globals["csrf_token"] = get_csrf_token

    @app.route("/privacy")
    def privacy():
        return render_template("privacy.html", title="Privacy Policy")

    @app.route("/terms")
    def terms():
        return render_template("terms.html", title="Terms of Service")

    @app.route("/faq")
    def faq():
        return render_template("faq.html", title="FAQ")

    @app.route("/contact")
    def contact():
        contact_email = app.config.get("CONTACT_EMAIL") or ""
        return render_template("contact.html", title="Contact", contact_email=contact_email)

    @app.route("/register", methods=["GET", "POST"])
    def register():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))

        if request.method == "POST":
            name = request.form.get("name", "").strip()
            email = normalize_email(request.form.get("email", ""))
            password = request.form.get("password", "")

            error = None
            if not name:
                error = "Name is required."
            elif not email or not EMAIL_RE.match(email):
                error = "A valid email is required."
            elif len(password) < 8:
                error = "Password must be at least 8 characters."
            elif User.query.filter_by(email=email).first() is not None:
                error = "An account with that email already exists."

            if error:
                flash(error, "error")
                return render_template(
                    "register.html", title="Create your account", name=name, email=email
                )

            user = User(name=name, email=email)
            user.set_password(password)
            db.session.add(user)
            db.session.commit()

            login_user(user)
            flash("Welcome! Add your first trial to get started.", "success")
            return redirect(url_for("dashboard"))

        return render_template("register.html", title="Create your account")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))

        if request.method == "POST":
            email = normalize_email(request.form.get("email", ""))
            password = request.form.get("password", "")

            user = User.query.filter_by(email=email).first()
            if user is None or not user.check_password(password):
                flash("Invalid email or password.", "error")
                return render_template("login.html", title="Log in", email=email)

            login_user(user)
            flash(f"Welcome back, {user.name}.", "success")
            return redirect(url_for("dashboard"))

        return render_template("login.html", title="Log in")

    @app.route("/forgot-password", methods=["GET", "POST"])
    def forgot_password():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))

        if request.method == "POST":
            email = normalize_email(request.form.get("email", ""))
            user = User.query.filter_by(email=email).first()
            if user is not None:
                token = generate_password_reset_token(app, user)
                reset_url = url_for("reset_password", token=token, _external=True)
                send_password_reset_email(app, user, reset_url)

            flash(
                "If an account exists for that email address, a password reset link has been sent.",
                "success",
            )
            return redirect(url_for("login"))

        return render_template("forgot_password.html", title="Forgot password")

    @app.route("/reset-password/<token>", methods=["GET", "POST"])
    def reset_password(token):
        user = verify_password_reset_token(app, token)
        if user is None:
            flash("This password reset link is invalid or has expired.", "error")
            return redirect(url_for("forgot_password"))

        if request.method == "POST":
            password = request.form.get("password", "")
            confirm_password = request.form.get("confirm_password", "")

            error = None
            if len(password) < 8:
                error = "Password must be at least 8 characters."
            elif password != confirm_password:
                error = "Passwords do not match."

            if error:
                flash(error, "error")
                return render_template("reset_password.html", title="Reset password", token=token)

            user.set_password(password)
            db.session.commit()
            flash("Your password has been reset. Please log in.", "success")
            return redirect(url_for("login"))

        return render_template("reset_password.html", title="Reset password", token=token)

    @app.route("/logout", methods=["POST"])
    @login_required
    def logout():
        logout_user()
        flash("Logged out.", "success")
        return redirect(url_for("login"))

    @app.route("/")
    def dashboard():
        if not current_user.is_authenticated:
            return render_template(
                "landing.html",
                title="TrialTracker — Free Trial & Subscription Reminder",
                landing_page=True,
            )

        advance_due_subscriptions()

        rows = Trial.query.filter_by(user_id=current_user.id).all()

        trials = []
        total_monthly_cost = 0.0
        today = date.today()

        for row in rows:
            end_date = datetime.strptime(row.trial_end_date, "%Y-%m-%d").date()
            days_remaining = (end_date - today).days

            cost = float(row.monthly_cost)
            if row.kind == "subscription" and row.billing_cycle == "yearly":
                monthly_equivalent = cost / 12
            else:
                monthly_equivalent = cost
            total_monthly_cost += monthly_equivalent

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
                    "id": row.id,
                    "product_name": row.product_name,
                    "vendor": row.vendor,
                    "monthly_cost": cost,
                    "kind": row.kind,
                    "billing_cycle": row.billing_cycle,
                    "trial_end_date": end_date,
                    "days_remaining": days_remaining,
                    "notes": row.notes,
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

    def _validate_trial_form(existing_cost=None):
        product_name = request.form.get("product_name", "").strip()
        vendor = request.form.get("vendor", "").strip()
        monthly_cost = request.form.get("monthly_cost", "").strip()
        trial_end_date = request.form.get("trial_end_date", "").strip()
        notes = request.form.get("notes", "").strip()
        kind = request.form.get("kind", "trial").strip()
        billing_cycle = request.form.get("billing_cycle", "").strip()

        if kind not in ("trial", "subscription"):
            kind = "trial"
        if kind != "subscription":
            billing_cycle = ""

        error = None
        if not product_name:
            error = "Product name is required."
        elif not monthly_cost:
            error = "Monthly cost is required."
        elif not trial_end_date:
            error = "Date is required."
        elif kind == "subscription" and billing_cycle not in ("monthly", "yearly"):
            error = "Billing recurrence (Monthly or Yearly) is required for subscriptions."

        try:
            monthly_cost_value = float(monthly_cost)
        except ValueError:
            error = error or "Cost must be a number."
            monthly_cost_value = existing_cost if existing_cost is not None else 0.0

        try:
            datetime.strptime(trial_end_date, "%Y-%m-%d").date()
        except ValueError:
            error = error or "Date must be a valid date (YYYY-MM-DD)."

        return {
            "product_name": product_name,
            "vendor": vendor,
            "monthly_cost": monthly_cost,
            "monthly_cost_value": monthly_cost_value,
            "trial_end_date": trial_end_date,
            "notes": notes,
            "kind": kind,
            "billing_cycle": billing_cycle or None,
            "error": error,
        }

    @app.route("/trials/add", methods=["GET", "POST"])
    @login_required
    def add_trial():
        if request.method == "POST":
            form = _validate_trial_form()

            if form["error"]:
                flash(form["error"], "error")
                return render_template(
                    "add_edit_trial.html",
                    title="Add Trial",
                    trial={
                        "id": None,
                        "product_name": form["product_name"],
                        "vendor": form["vendor"],
                        "monthly_cost": form["monthly_cost"],
                        "trial_end_date": form["trial_end_date"],
                        "notes": form["notes"],
                        "kind": form["kind"],
                        "billing_cycle": form["billing_cycle"],
                    },
                )

            trial = Trial(
                user_id=current_user.id,
                product_name=form["product_name"],
                vendor=form["vendor"],
                monthly_cost=form["monthly_cost_value"],
                trial_end_date=form["trial_end_date"],
                notes=form["notes"],
                kind=form["kind"],
                billing_cycle=form["billing_cycle"],
            )
            db.session.add(trial)
            db.session.commit()

            flash(
                "Subscription added." if form["kind"] == "subscription" else "Trial added.",
                "success",
            )
            return redirect(url_for("dashboard"))

        return render_template("add_edit_trial.html", title="Add Trial")

    @app.route("/trials/<int:trial_id>/edit", methods=["GET", "POST"])
    @login_required
    def edit_trial(trial_id: int):
        trial = Trial.query.filter_by(id=trial_id, user_id=current_user.id).first()
        if trial is None:
            abort(404)

        if request.method == "POST":
            form = _validate_trial_form(existing_cost=trial.monthly_cost)

            if form["error"]:
                flash(form["error"], "error")
                return render_template(
                    "add_edit_trial.html",
                    title="Edit Trial",
                    trial={
                        "id": trial.id,
                        "product_name": form["product_name"] or trial.product_name,
                        "vendor": form["vendor"] or trial.vendor,
                        "monthly_cost": form["monthly_cost"] or trial.monthly_cost,
                        "trial_end_date": form["trial_end_date"] or trial.trial_end_date,
                        "notes": form["notes"] or trial.notes,
                        "kind": form["kind"],
                        "billing_cycle": form["billing_cycle"],
                    },
                )

            trial.product_name = form["product_name"]
            trial.vendor = form["vendor"]
            trial.monthly_cost = form["monthly_cost_value"]
            if form["trial_end_date"] != trial.trial_end_date:
                trial.trial_end_date = form["trial_end_date"]
                trial.reminder_7_sent = False
                trial.reminder_1_sent = False
            trial.notes = form["notes"]
            trial.kind = form["kind"]
            trial.billing_cycle = form["billing_cycle"]
            db.session.commit()

            flash(
                "Subscription updated." if form["kind"] == "subscription" else "Trial updated.",
                "success",
            )
            return redirect(url_for("dashboard"))

        return render_template(
            "add_edit_trial.html",
            title="Edit Trial",
            trial={
                "id": trial.id,
                "product_name": trial.product_name,
                "vendor": trial.vendor,
                "monthly_cost": trial.monthly_cost,
                "trial_end_date": trial.trial_end_date,
                "notes": trial.notes,
                "kind": trial.kind,
                "billing_cycle": trial.billing_cycle,
            },
        )

    @app.route("/trials/<int:trial_id>/delete", methods=["POST"])
    @login_required
    def delete_trial(trial_id: int):
        trial = Trial.query.filter_by(id=trial_id, user_id=current_user.id).first()
        if trial is None:
            abort(404)

        db.session.delete(trial)
        db.session.commit()
        flash("Trial deleted.", "success")
        return redirect(url_for("dashboard"))

    @app.route("/export/csv")
    @login_required
    def export_csv():
        rows = (
            Trial.query.filter_by(user_id=current_user.id)
            .order_by(Trial.trial_end_date.asc())
            .all()
        )

        text_buffer = io.StringIO()
        writer = csv.writer(text_buffer)
        writer.writerow(
            [
                "Product",
                "Vendor",
                "Monthly Cost",
                "Trial End Date",
                "Notes",
                "Created At",
                "Type",
                "Billing Cycle",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row.product_name,
                    row.vendor,
                    row.monthly_cost,
                    row.trial_end_date,
                    row.notes,
                    row.created_at.isoformat() if row.created_at else "",
                    "Subscription" if row.kind == "subscription" else "Trial",
                    (row.billing_cycle or "").capitalize(),
                ]
            )

        mem_buffer = io.BytesIO(text_buffer.getvalue().encode("utf-8"))
        mem_buffer.seek(0)

        return send_file(
            mem_buffer,
            mimetype="text/csv",
            as_attachment=True,
            download_name="trials.csv",
        )

    @app.route("/account")
    @login_required
    def account():
        return render_template("account.html", title="Account")

    @app.route("/account/delete", methods=["POST"])
    @login_required
    def delete_account():
        password = request.form.get("password", "")
        confirm_deletion = request.form.get("confirm_deletion")

        if not confirm_deletion:
            flash("You must confirm that you understand this action is permanent.", "error")
            return redirect(url_for("account"))

        if not current_user.check_password(password):
            flash("Incorrect password. Your account was not deleted.", "error")
            return redirect(url_for("account"))

        user = db.session.get(User, current_user.id)
        db.session.delete(user)
        db.session.commit()
        logout_user()
        flash("Your TrialTracker account and all associated data have been deleted.", "success")
        return redirect(url_for("dashboard"))

    @app.route("/tasks/run-reminders")
    def run_reminders():
        expected_token = app.config.get("REMINDER_TASK_TOKEN") or ""
        auth_header = request.headers.get("Authorization", "")
        provided = auth_header[7:] if auth_header.startswith("Bearer ") else ""
        if not expected_token or not provided or not secrets.compare_digest(provided, expected_token):
            return "Unauthorized", 401

        sent = send_due_reminders(app)
        return {"sent": sent}


def send_via_resend(api_key: str, from_email: str, to_email: str, subject: str, body: str) -> int:
    payload = json.dumps(
        {
            "from": from_email,
            "to": [to_email],
            "subject": subject,
            "text": body,
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "TrialTracker/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status


def send_password_reset_email(app: Flask, user: "User", reset_url: str) -> None:
    api_key = app.config.get("RESEND_API_KEY")
    from_email = app.config.get("MAIL_FROM")
    if not api_key or not from_email:
        return

    subject = "Reset your TrialTracker password"
    lines = [
        f"Hi {user.name},",
        "",
        "We received a request to reset your TrialTracker password.",
        "",
        f"Reset your password: {reset_url}",
        "",
        f"This link expires in {PASSWORD_RESET_MAX_AGE_SECONDS // 60} minutes.",
        "",
        "If you didn't request this, you can safely ignore this email —",
        "your password will not be changed.",
        "",
        "— TrialTracker",
    ]
    body = "\n".join(lines)

    try:
        send_via_resend(api_key, from_email, user.email, subject, body)
    except Exception:
        # Best-effort: the forgot-password response is always generic
        # regardless of transport success, so there is nothing to surface
        # to the requester here.
        pass


def send_due_reminders(app: Flask) -> int:
    advance_due_subscriptions()

    today = date.today()

    candidates = Trial.query.filter(
        db.or_(Trial.reminder_7_sent.is_(False), Trial.reminder_1_sent.is_(False))
    ).all()

    to_send = []
    for trial in candidates:
        end_date = datetime.strptime(trial.trial_end_date, "%Y-%m-%d").date()
        days_remaining = (end_date - today).days
        if days_remaining == 7 and not trial.reminder_7_sent:
            to_send.append((trial, 7))
        elif days_remaining == 1 and not trial.reminder_1_sent:
            to_send.append((trial, 1))

    if not to_send:
        return 0

    api_key = app.config.get("RESEND_API_KEY")
    from_email = app.config.get("MAIL_FROM")

    if not api_key or not from_email:
        return 0

    sent_count = 0
    for trial, days in to_send:
        owner = trial.user
        if owner is None:
            continue

        day_word = f"{days} day{'s' if days != 1 else ''}"
        is_subscription = trial.kind == "subscription"

        if is_subscription:
            cost_label = "Yearly cost" if trial.billing_cycle == "yearly" else "Monthly cost"
            subject = f"Renews in {day_word}: {trial.product_name}"
            lines = [
                f"Hi {owner.name},",
                "",
                f"Your subscription for {trial.product_name} renews in {day_word}.",
                f"Vendor: {trial.vendor or 'N/A'}",
                f"{cost_label}: ${trial.monthly_cost:.2f}",
                f"Renewal date: {trial.trial_end_date}",
                "",
                "Decide whether to keep, cancel, or change your plan before it renews.",
                "",
                "— TrialTracker",
            ]
        else:
            subject = f"Trial ending in {day_word}: {trial.product_name}"
            lines = [
                f"Hi {owner.name},",
                "",
                f"Your trial for {trial.product_name} is ending in {day_word}.",
                f"Vendor: {trial.vendor or 'N/A'}",
                f"Monthly cost if converted: ${trial.monthly_cost:.2f}",
                f"Trial end date: {trial.trial_end_date}",
                "",
                "Decide whether to keep, cancel, or downgrade so you're not surprised by charges.",
                "",
                "— TrialTracker",
            ]
        body = "\n".join(lines)

        try:
            status = send_via_resend(api_key, from_email, owner.email, subject, body)
        except Exception:
            # This user's send failed; leave their reminder flag unset so it
            # retries next run, and continue on to the remaining users.
            continue

        if status not in (200, 201, 202):
            continue

        if days == 7:
            trial.reminder_7_sent = True
        else:
            trial.reminder_1_sent = True
        db.session.commit()
        sent_count += 1

    return sent_count


app = create_app()


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
