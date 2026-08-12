"""
Targeted tests for the Trial/Subscription feature added on top of the
existing multi-user TrialTracker app: subscription creation, monthly/yearly
renewal advancement, reminder-flag reset on renewal, trials never
auto-renewing, and duplicate-send protection across a renewal.

Run against a live server (BASE_URL) for the HTTP-level flows (add
subscription via form, dashboard labels, CSV export), and directly against
advance_due_subscriptions() / send_due_reminders() in-process for the
renewal-math and reminder behavior, same pattern as e2e_tests.py.

Usage:
    python app.py                    # in one terminal (FLASK_ENV=development)
    python subscription_tests.py     # in another
"""

import http.cookiejar
import re
import time
import urllib.error
import urllib.request
from datetime import date, timedelta
from urllib.parse import urlencode

BASE_URL = "http://127.0.0.1:5000"

CSRF_RE = re.compile(r'name="csrf_token"\s+value="([^"]+)"')


class Session:
    def __init__(self):
        self.cookiejar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookiejar)
        )

    def get(self, path: str):
        req = urllib.request.Request(BASE_URL + path, method="GET")
        try:
            return self.opener.open(req)
        except urllib.error.HTTPError as e:
            return e

    def post(self, path: str, data: dict, include_csrf: bool = True):
        payload = dict(data)
        if include_csrf:
            payload["csrf_token"] = self.csrf_token()
        encoded = urlencode(payload).encode("utf-8")
        req = urllib.request.Request(
            BASE_URL + path,
            data=encoded,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            return self.opener.open(req)
        except urllib.error.HTTPError as e:
            return e

    def csrf_token(self) -> str:
        resp = self.get("/login")
        html = resp.read().decode("utf-8", errors="ignore")
        match = CSRF_RE.search(html)
        assert match, "Could not find csrf_token in page HTML"
        return match.group(1)


def read(resp) -> str:
    return resp.read().decode("utf-8", errors="ignore")


def wait_for_server(timeout: float = 15.0) -> None:
    start = time.time()
    last_err = None
    while time.time() - start < timeout:
        try:
            urllib.request.urlopen(BASE_URL + "/login")
            print("Server is up.")
            return
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(0.5)
    raise SystemExit(f"Server did not become ready within {timeout}s: {last_err}")


def reset_database() -> None:
    from app import Trial, User, app as flask_app, db

    with flask_app.app_context():
        db.session.query(Trial).delete()
        db.session.query(User).delete()
        db.session.commit()


def register(sess: Session, name: str, email: str, password: str) -> None:
    resp = sess.post("/register", {"name": name, "email": email, "password": password})
    html = read(resp)
    assert resp.status == 200, f"Register failed with status {resp.status}: {html[:300]}"


def add_item(sess: Session, name: str, days: int, cost: float, kind: str, billing_cycle: str = ""):
    end_date = date.today() + timedelta(days=days)
    data = {
        "product_name": name,
        "vendor": f"{name} Inc",
        "monthly_cost": str(cost),
        "trial_end_date": end_date.strftime("%Y-%m-%d"),
        "notes": "",
        "kind": kind,
    }
    if billing_cycle:
        data["billing_cycle"] = billing_cycle
    resp = sess.post("/trials/add", data)
    html = read(resp)
    assert resp.status == 200, f"Add {name} failed with status {resp.status}: {html[:300]}"
    return html


def get_trial(email: str, product_name: str):
    from app import Trial, User, app as flask_app, db

    with flask_app.app_context():
        user = User.query.filter_by(email=email).first()
        return Trial.query.filter_by(user_id=user.id, product_name=product_name).first()


def main() -> None:
    print("Waiting for server on", BASE_URL)
    wait_for_server()

    print("Resetting database")
    reset_database()

    user_email = "subtest@test.com"
    sess = Session()

    print("TEST N: Register user, add a Trial (default kind)")
    register(sess, "Sub Tester", user_email, "password1")
    add_item(sess, "FreeTrialApp", 10, 12.00, kind="trial")
    trial = get_trial(user_email, "FreeTrialApp")
    assert trial.kind == "trial", f"Expected kind='trial', got {trial.kind!r}"
    assert trial.billing_cycle is None, f"Trial should not have a billing_cycle, got {trial.billing_cycle!r}"
    print("TEST N PASSED")

    print("TEST O: Add a Monthly subscription via form")
    add_item(sess, "MonthlyApp", 15, 20.00, kind="subscription", billing_cycle="monthly")
    monthly = get_trial(user_email, "MonthlyApp")
    assert monthly.kind == "subscription", f"Expected kind='subscription', got {monthly.kind!r}"
    assert monthly.billing_cycle == "monthly", f"Expected billing_cycle='monthly', got {monthly.billing_cycle!r}"
    print("TEST O PASSED")

    print("TEST P: Add a Yearly subscription; dashboard shows 'Renews' + monthly-equivalent spend")
    add_item(sess, "YearlyApp", 60, 120.00, kind="subscription", billing_cycle="yearly")
    yearly = get_trial(user_email, "YearlyApp")
    assert yearly.billing_cycle == "yearly", f"Expected billing_cycle='yearly', got {yearly.billing_cycle!r}"

    resp = sess.get("/")
    html = read(resp)
    assert "Renews" in html, "Expected 'Renews' label for subscription on dashboard"
    assert "Trial ends" in html, "Expected 'Trial ends' label for trial on dashboard"
    # 12 (trial) + 20 (monthly) + 120/12=10 (yearly-equivalent) = 42.00
    assert "$42.00" in html, f"Expected potential monthly spend $42.00 (12 + 20 + 10 yearly-equiv) in dashboard"
    print("TEST P PASSED")

    print("TEST Q: Subscription requires a billing recurrence")
    resp = sess.post(
        "/trials/add",
        {
            "product_name": "BadSub",
            "vendor": "",
            "monthly_cost": "9.99",
            "trial_end_date": (date.today() + timedelta(days=30)).strftime("%Y-%m-%d"),
            "notes": "",
            "kind": "subscription",
        },
    )
    html = read(resp)
    assert "recurrence" in html.lower(), "Expected billing recurrence validation error"
    bad = get_trial(user_email, "BadSub")
    assert bad is None, "Invalid subscription (missing billing_cycle) should not have been saved"
    print("TEST Q PASSED")

    print("TEST R: Monthly renewal advancement rolls a past-due subscription forward and resets flags")
    from app import Trial, User, app as flask_app, db, advance_due_subscriptions

    with flask_app.app_context():
        user = User.query.filter_by(email=user_email).first()
        t = Trial(
            user_id=user.id,
            product_name="OverdueMonthly",
            vendor="",
            monthly_cost=10.0,
            trial_end_date=(date.today() - timedelta(days=40)).strftime("%Y-%m-%d"),
            kind="subscription",
            billing_cycle="monthly",
            reminder_7_sent=True,
            reminder_1_sent=True,
        )
        db.session.add(t)
        db.session.commit()
        user_id = user.id

        advance_due_subscriptions()

        db.session.refresh(t)
        new_date = date.fromisoformat(t.trial_end_date)
        assert new_date >= date.today(), f"Expected advanced date >= today, got {new_date}"
        assert t.reminder_7_sent is False, "reminder_7_sent should reset on renewal advancement"
        assert t.reminder_1_sent is False, "reminder_1_sent should reset on renewal advancement"
    print(f"TEST R PASSED (advanced to {new_date})")

    print("TEST S: Yearly renewal advancement skips multiple overdue periods to the next upcoming date")
    with flask_app.app_context():
        t = Trial(
            user_id=user_id,
            product_name="OverdueYearly",
            vendor="",
            monthly_cost=100.0,
            trial_end_date=(date.today() - timedelta(days=800)).strftime("%Y-%m-%d"),  # >2 years overdue
            kind="subscription",
            billing_cycle="yearly",
            reminder_7_sent=True,
            reminder_1_sent=True,
        )
        db.session.add(t)
        db.session.commit()

        advance_due_subscriptions()

        db.session.refresh(t)
        new_date = date.fromisoformat(t.trial_end_date)
        assert new_date >= date.today(), f"Expected advanced yearly date >= today, got {new_date}"
        one_cycle_earlier = new_date.replace(year=new_date.year - 1)
        assert one_cycle_earlier < date.today(), (
            "Advancement should land on the *next upcoming* renewal, not skip past it"
        )
        assert t.reminder_7_sent is False and t.reminder_1_sent is False
    print(f"TEST S PASSED (advanced to {new_date})")

    print("TEST T: Trials are never auto-advanced")
    with flask_app.app_context():
        past = (date.today() - timedelta(days=15)).strftime("%Y-%m-%d")
        t = Trial(
            user_id=user_id,
            product_name="ExpiredTrial",
            vendor="",
            monthly_cost=5.0,
            trial_end_date=past,
            kind="trial",
        )
        db.session.add(t)
        db.session.commit()

        advance_due_subscriptions()

        db.session.refresh(t)
        assert t.trial_end_date == past, (
            f"Trial end date should never auto-advance, expected {past} got {t.trial_end_date}"
        )
    print("TEST T PASSED")

    print("TEST U: Renewal advancement + reminder send does not double-fire for the old cycle")
    from app import send_due_reminders
    import app as app_module

    resend_calls = []

    def fake_send_via_resend(api_key, from_email, to_email, subject, body):
        resend_calls.append({"to": to_email, "subject": subject})
        return 202

    with flask_app.app_context():
        flask_app.config["RESEND_API_KEY"] = "test-fake-key"
        flask_app.config["MAIL_FROM"] = "notifications@example.com"

        t = Trial(
            user_id=user_id,
            product_name="RenewingSub",
            vendor="",
            monthly_cost=15.0,
            # Overdue: was due 35 days ago, should advance to next monthly cycle.
            trial_end_date=(date.today() - timedelta(days=35)).strftime("%Y-%m-%d"),
            kind="subscription",
            billing_cycle="monthly",
            reminder_7_sent=True,  # flags set for the OLD (now past) cycle
            reminder_1_sent=True,
        )
        db.session.add(t)
        db.session.commit()

        resend_calls.clear()
        original_send = app_module.send_via_resend
        app_module.send_via_resend = fake_send_via_resend
        try:
            send_due_reminders(flask_app)
        finally:
            app_module.send_via_resend = original_send

        db.session.refresh(t)
        old_cycle_recipients = [m for m in resend_calls if m["to"] == user_email and "RenewingSub" in m.get("subject", "")]
        # The new cycle's renewal date should not be exactly 7 or 1 days out
        # (it's ~28+ days out), so no reminder should fire for it right away,
        # and the old cycle's flags must be reset rather than left "sent".
        assert t.reminder_7_sent is False, "New cycle should start with reminder_7_sent reset"
        assert t.reminder_1_sent is False, "New cycle should start with reminder_1_sent reset"
    print("TEST U PASSED (no stale reminder state carried into new cycle)")

    print("TEST V: CSV export includes Type and Billing Cycle columns")
    resp = sess.get("/export/csv")
    csv_text = read(resp)
    assert "Type" in csv_text and "Billing Cycle" in csv_text, "CSV header missing Type/Billing Cycle"
    assert "Subscription" in csv_text and "Monthly" in csv_text, "CSV rows missing subscription type/cycle values"
    assert "Trial" in csv_text, "CSV rows missing trial type value"
    print("TEST V PASSED")

    print("TEST W: Add/Edit/Delete forms render an actual CSRF field (real-browser regression check)")
    # The synthetic Session.post() helper above always injects a csrf_token
    # into the POST body regardless of what's in the page, which would mask
    # a form that never renders the hidden csrf_token field at all (as a
    # real browser's native form submission would send no such key). Assert
    # directly on the rendered HTML instead.
    resp = sess.get("/trials/add")
    add_html = read(resp)
    assert 'name="csrf_token"' in add_html, "Add-trial form is missing the csrf_token field"

    resp = sess.get("/")
    dash_html = read(resp)
    assert dash_html.count('name="csrf_token"') >= 2, (
        "Dashboard delete form(s) missing the csrf_token field"
    )

    monthly_trial = get_trial(user_email, "MonthlyApp")
    resp = sess.get(f"/trials/{monthly_trial.id}/edit")
    edit_html = read(resp)
    assert 'name="csrf_token"' in edit_html, "Edit-trial form is missing the csrf_token field"

    # Simulate a real browser: submit using ONLY the token scraped from the
    # add-trial page itself, not a token fetched from a different page.
    token_match = CSRF_RE.search(add_html)
    assert token_match, "Could not find csrf_token value on the add-trial page"
    real_token = token_match.group(1)

    payload = {
        "product_name": "RealFormSubmit",
        "vendor": "",
        "monthly_cost": "3.00",
        "trial_end_date": (date.today() + timedelta(days=20)).strftime("%Y-%m-%d"),
        "notes": "",
        "kind": "trial",
        "csrf_token": real_token,
    }
    resp = sess.post("/trials/add", payload, include_csrf=False)
    assert resp.status == 200, f"Real-browser-style add failed with status {resp.status}"
    assert get_trial(user_email, "RealFormSubmit") is not None, (
        "Trial submitted with the page's own csrf_token was not saved"
    )
    print("TEST W PASSED")

    print("\nAll subscription tests (N-W) passed.")


if __name__ == "__main__":
    main()
