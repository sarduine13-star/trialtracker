import csv
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
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "trialtracker.db"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

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

    # Email configuration (SendGrid HTTPS Mail Send API) - set by the operator only.
    app.config["SENDGRID_API_KEY"] = os.environ.get("SENDGRID_API_KEY", "")
    app.config["MAIL_FROM"] = os.environ.get("MAIL_FROM", "")

    app.config["REMINDER_TASK_TOKEN"] = os.environ.get("REMINDER_TASK_TOKEN", "")

    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = flask_env != "development"

    db.init_app(app)
    login_manager.init_app(app)

    with app.app_context():
        db.create_all()

    register_routes(app)
    return app


def normalize_email(raw_email: str) -> str:
    return raw_email.strip().lower()


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

    @app.route("/logout", methods=["POST"])
    @login_required
    def logout():
        logout_user()
        flash("Logged out.", "success")
        return redirect(url_for("login"))

    @app.route("/")
    @login_required
    def dashboard():
        rows = Trial.query.filter_by(user_id=current_user.id).all()

        trials = []
        total_monthly_cost = 0.0
        today = date.today()

        for row in rows:
            end_date = datetime.strptime(row.trial_end_date, "%Y-%m-%d").date()
            days_remaining = (end_date - today).days

            total_monthly_cost += float(row.monthly_cost)

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
                    "monthly_cost": float(row.monthly_cost),
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
            monthly_cost_value = existing_cost if existing_cost is not None else 0.0

        try:
            datetime.strptime(trial_end_date, "%Y-%m-%d").date()
        except ValueError:
            error = "Trial end date must be a valid date (YYYY-MM-DD)."

        return {
            "product_name": product_name,
            "vendor": vendor,
            "monthly_cost": monthly_cost,
            "monthly_cost_value": monthly_cost_value,
            "trial_end_date": trial_end_date,
            "notes": notes,
            "error": error,
        }

    @app.route("/trials/add", methods=["GET", "POST"])
    @login_required
    def add_trial():
        if request.method == "POST":
            form = _validate_trial_form()

            if form["error"]:
                flash(form["error"], "error")
                return render_template("add_edit_trial.html", title="Add Trial")

            trial = Trial(
                user_id=current_user.id,
                product_name=form["product_name"],
                vendor=form["vendor"],
                monthly_cost=form["monthly_cost_value"],
                trial_end_date=form["trial_end_date"],
                notes=form["notes"],
            )
            db.session.add(trial)
            db.session.commit()

            flash("Trial added.", "success")
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
                    },
                )

            trial.product_name = form["product_name"]
            trial.vendor = form["vendor"]
            trial.monthly_cost = form["monthly_cost_value"]
            trial.trial_end_date = form["trial_end_date"]
            trial.notes = form["notes"]
            db.session.commit()

            flash("Trial updated.", "success")
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
            ["Product", "Vendor", "Monthly Cost", "Trial End Date", "Notes", "Created At"]
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

    @app.route("/tasks/run-reminders")
    def run_reminders():
        expected_token = app.config.get("REMINDER_TASK_TOKEN") or ""
        auth_header = request.headers.get("Authorization", "")
        provided = auth_header[7:] if auth_header.startswith("Bearer ") else ""
        if not expected_token or not provided or not secrets.compare_digest(provided, expected_token):
            return "Unauthorized", 401

        sent = send_due_reminders(app)
        return {"sent": sent}


def send_via_sendgrid(api_key: str, from_email: str, to_email: str, subject: str, body: str) -> int:
    payload = json.dumps(
        {
            "personalizations": [{"to": [{"email": to_email}]}],
            "from": {"email": from_email},
            "subject": subject,
            "content": [{"type": "text/plain", "value": body}],
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        "https://api.sendgrid.com/v3/mail/send",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status


def send_due_reminders(app: Flask) -> int:
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

    api_key = app.config.get("SENDGRID_API_KEY")
    from_email = app.config.get("MAIL_FROM")

    if not api_key or not from_email:
        return 0

    sent_count = 0
    for trial, days in to_send:
        owner = trial.user
        if owner is None:
            continue

        subject = f"Trial ending in {days} day{'s' if days != 1 else ''}: {trial.product_name}"
        lines = [
            f"Hi {owner.name},",
            "",
            f"Your trial for {trial.product_name} is ending in {days} day{'s' if days != 1 else ''}.",
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
            status = send_via_sendgrid(api_key, from_email, owner.email, subject, body)
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
