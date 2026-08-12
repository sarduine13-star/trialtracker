"""
End-to-end + isolation tests for TrialTracker's multi-user conversion.

Run against a live server (BASE_URL) for the HTTP-level flows (register,
login, CRUD, cross-user isolation, CSV export). The reminder-selection /
duplicate-prevention / failure-isolation behavior is exercised directly
against send_due_reminders() in-process, with send_via_sendgrid monkeypatched,
because that lets us assert exactly which recipients got which message
without needing a real SendGrid account.

Usage:
    python app.py                 # in one terminal (FLASK_ENV=development)
    python e2e_tests.py           # in another
"""

import http.cookiejar
import io
import re
import time
import urllib.error
import urllib.request
from datetime import date, timedelta
from urllib.parse import urlencode

BASE_URL = "http://127.0.0.1:5000"

CSRF_RE = re.compile(r'name="csrf_token"\s+value="([^"]+)"')


class Session:
    """A cookie-aware HTTP client standing in for one browser/user."""

    def __init__(self):
        self.cookiejar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookiejar)
        )
        self._csrf_token = None

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
        # Any GET on a page with a form refreshes/returns the session's token.
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
    final_url = resp.geturl()
    assert resp.status == 200, f"Register failed with status {resp.status}: {html[:300]}"
    assert final_url.endswith("/") or final_url.rstrip("/").endswith(
        "127.0.0.1:5000"
    ), f"Expected dashboard after register, got {final_url}"
    assert "Your active trials" in html, "Dashboard heading missing after register"


def login(sess: Session, email: str, password: str) -> None:
    resp = sess.post("/login", {"email": email, "password": password})
    html = read(resp)
    assert resp.status == 200, f"Login failed with status {resp.status}: {html[:300]}"
    assert "Your active trials" in html, "Dashboard heading missing after login"


def logout(sess: Session) -> None:
    resp = sess.post("/logout", {})
    assert resp.status == 200, f"Logout failed with status {resp.status}"


def add_trial(sess: Session, name: str, days: int, cost: float) -> None:
    end_date = date.today() + timedelta(days=days)
    resp = sess.post(
        "/trials/add",
        {
            "product_name": name,
            "vendor": f"{name} Inc",
            "monthly_cost": str(cost),
            "trial_end_date": end_date.strftime("%Y-%m-%d"),
            "notes": "",
        },
    )
    assert resp.status == 200, f"Add trial for {name} failed with status {resp.status}"
    _ = read(resp)


def get_trial_id(email: str, product_name: str):
    from app import Trial, User, app as flask_app, db

    with flask_app.app_context():
        user = User.query.filter_by(email=email).first()
        trial = Trial.query.filter_by(user_id=user.id, product_name=product_name).first()
        return trial.id if trial else None


def main() -> None:
    print("Waiting for server on", BASE_URL)
    wait_for_server()

    print("Resetting database")
    reset_database()

    user_a_email = "usera@test.com"
    user_b_email = "userb@test.com"

    session_a = Session()
    session_b = Session()

    # TEST A: Register User A
    print("TEST A: Register User A")
    register(session_a, "User A", user_a_email, "passwordA1")
    print("TEST A PASSED")

    # TEST B: Login User A (logout, then log back in)
    print("TEST B: Login User A")
    logout(session_a)
    resp = session_a.get("/")
    assert "/login" in resp.geturl(), f"Expected redirect to /login when logged out, got {resp.geturl()}"
    login(session_a, user_a_email, "passwordA1")
    print("TEST B PASSED")

    # TEST C: User A adds multiple trials
    print("TEST C: User A adds multiple trials")
    add_trial(session_a, "Netflix", 2, 15.99)
    add_trial(session_a, "Spotify", 8, 9.99)
    add_trial(session_a, "Adobe", 30, 54.99)
    print("TEST C PASSED")

    # TEST D: Dashboard urgency/order/total
    print("TEST D: Verify dashboard ordering, urgency, and total cost")
    resp = session_a.get("/")
    html = read(resp)

    n_idx = html.find("Netflix")
    s_idx = html.find("Spotify")
    a_idx = html.find("Adobe")
    assert -1 not in (n_idx, s_idx, a_idx), "One of the trials is missing from dashboard HTML"
    assert n_idx < s_idx < a_idx, (
        f"Expected Netflix, then Spotify, then Adobe. Indexes: N={n_idx}, S={s_idx}, A={a_idx}"
    )

    n_segment = html[max(0, n_idx - 400) : n_idx + 400]
    assert (
        "trial-critical" in n_segment or "badge-critical" in n_segment
    ), f"Expected Netflix to be marked critical, segment: {n_segment[:200]}"

    assert "$80.97" in html, "Expected total monthly cost $80.97 (15.99 + 9.99 + 54.99)"
    print("TEST D PASSED")

    netflix_id = get_trial_id(user_a_email, "Netflix")
    adobe_id = get_trial_id(user_a_email, "Adobe")
    assert netflix_id and adobe_id, "Could not resolve trial ids for User A"

    # TEST E: Edit works
    print("TEST E: Edit Netflix cost to 22.99")
    resp = session_a.post(
        f"/trials/{netflix_id}/edit",
        {
            "product_name": "Netflix",
            "vendor": "Netflix Inc",
            "monthly_cost": "22.99",
            "trial_end_date": (date.today() + timedelta(days=2)).strftime("%Y-%m-%d"),
            "notes": "",
        },
    )
    _ = read(resp)
    assert resp.status == 200, f"Edit Netflix failed with status {resp.status}"

    resp = session_a.get("/")
    html = read(resp)
    assert "$22.99" in html, "Expected updated Netflix cost $22.99 in dashboard HTML"
    print("TEST E PASSED")

    # TEST F: Delete works
    print("TEST F: Delete Adobe")
    resp = session_a.post(f"/trials/{adobe_id}/delete", {})
    _ = read(resp)
    assert resp.status == 200, f"Delete Adobe failed with status {resp.status}"

    resp = session_a.get("/")
    html = read(resp)
    assert "Adobe" not in html, "Adobe still appears on dashboard after delete"
    print("TEST F PASSED")

    # TEST G: CSV export contains only User A's current trials
    print("TEST G: Export CSV contains only User A's trials")
    resp = session_a.get("/export/csv")
    csv_text = read(resp)
    assert (
        "Product,Vendor,Monthly Cost,Trial End Date,Notes,Created At" in csv_text
    ), "CSV header row missing or incorrect"
    assert "Netflix" in csv_text and "Spotify" in csv_text and "Adobe" not in csv_text, (
        "CSV contents do not match expected trials after delete"
    )
    print("TEST G PASSED")

    # TEST H: Register User B
    print("TEST H: Register User B")
    register(session_b, "User B", user_b_email, "passwordB1")
    print("TEST H PASSED")

    # TEST I: User B cannot see User A's trials
    print("TEST I: User B cannot see User A's trials")
    resp = session_b.get("/")
    html = read(resp)
    assert "Netflix" not in html and "Spotify" not in html, (
        "User B's dashboard leaked User A's trial data"
    )
    assert "No trials yet" in html, "Expected empty state on User B's fresh dashboard"
    print("TEST I PASSED")

    # TEST J: User B cannot edit/delete/export User A's data by guessing IDs
    print("TEST J: User B cannot access User A's trials by id")

    resp = session_b.get(f"/trials/{netflix_id}/edit")
    assert resp.status == 404, f"Expected 404 for cross-user edit GET, got {resp.status}"

    resp = session_b.post(
        f"/trials/{netflix_id}/edit",
        {
            "product_name": "Hijacked",
            "vendor": "Hijacked Inc",
            "monthly_cost": "1.00",
            "trial_end_date": (date.today() + timedelta(days=2)).strftime("%Y-%m-%d"),
            "notes": "",
        },
    )
    assert resp.status == 404, f"Expected 404 for cross-user edit POST, got {resp.status}"

    resp = session_b.post(f"/trials/{netflix_id}/delete", {})
    assert resp.status == 404, f"Expected 404 for cross-user delete, got {resp.status}"

    # Confirm User A's Netflix trial is untouched
    resp = session_a.get("/")
    html = read(resp)
    assert "Hijacked" not in html, "User B was able to modify User A's trial"
    assert "$22.99" in html, "User A's Netflix trial cost changed unexpectedly"

    add_trial(session_b, "Zoom", 5, 14.99)
    resp = session_b.get("/export/csv")
    csv_text = read(resp)
    assert "Zoom" in csv_text, "User B's own trial missing from their CSV export"
    assert "Netflix" not in csv_text and "Spotify" not in csv_text, (
        "User B's CSV export leaked User A's trials"
    )
    print("TEST J PASSED")

    # TEST K / L: Reminder selection, correct recipient, and duplicate prevention
    print("TEST K/L: Reminder selection, recipient targeting, and dedupe")
    from app import Trial, User, app as flask_app, db, send_due_reminders
    import app as app_module

    sendgrid_calls = []
    sendgrid_fail_for = set()

    def fake_send_via_sendgrid(api_key, from_email, to_email, subject, body):
        if to_email in sendgrid_fail_for:
            raise RuntimeError(f"Simulated SendGrid failure for {to_email}")
        sendgrid_calls.append({"to": to_email, "subject": subject, "from": from_email})
        return 202

    with flask_app.app_context():
        flask_app.config["SENDGRID_API_KEY"] = "test-fake-key"
        flask_app.config["MAIL_FROM"] = "notifications@example.com"

        user_a = User.query.filter_by(email=user_a_email).first()
        user_b = User.query.filter_by(email=user_b_email).first()

        spotify = Trial.query.filter_by(user_id=user_a.id, product_name="Spotify").first()
        spotify.trial_end_date = (date.today() + timedelta(days=7)).strftime("%Y-%m-%d")
        spotify.reminder_7_sent = False

        zoom = Trial.query.filter_by(user_id=user_b.id, product_name="Zoom").first()
        zoom.trial_end_date = (date.today() + timedelta(days=1)).strftime("%Y-%m-%d")
        zoom.reminder_1_sent = False
        db.session.commit()

        sendgrid_calls.clear()
        sendgrid_fail_for.clear()
        original_send = app_module.send_via_sendgrid
        app_module.send_via_sendgrid = fake_send_via_sendgrid
        try:
            sent = send_due_reminders(flask_app)
        finally:
            app_module.send_via_sendgrid = original_send

        assert sent == 2, f"Expected 2 reminders sent, got {sent}"
        recipients = {m["to"] for m in sendgrid_calls}
        assert recipients == {user_a_email, user_b_email}, (
            f"Reminder recipients mismatch: {recipients}"
        )
        spotify_msg = next(m for m in sendgrid_calls if m["to"] == user_a_email)
        zoom_msg = next(m for m in sendgrid_calls if m["to"] == user_b_email)
        assert "Spotify" in spotify_msg["subject"] and "7 day" in spotify_msg["subject"]
        assert "Zoom" in zoom_msg["subject"] and "1 day" in zoom_msg["subject"]
        print("TEST K PASSED (correct recipient/content per user via SendGrid HTTPS API)")

        # TEST L: running again same-day must not re-send (flags already set)
        sendgrid_calls.clear()
        app_module.send_via_sendgrid = fake_send_via_sendgrid
        try:
            sent_again = send_due_reminders(flask_app)
        finally:
            app_module.send_via_sendgrid = original_send
        assert sent_again == 0, f"Expected 0 duplicate reminders, got {sent_again}"
        print("TEST L PASSED (no duplicate reminders)")

        # Bonus: one user's send failure must not affect the other user's data
        spotify.reminder_7_sent = False
        zoom.reminder_1_sent = False
        db.session.commit()

        sendgrid_calls.clear()
        sendgrid_fail_for.clear()
        sendgrid_fail_for.add(user_a_email)  # simulate User A's send failing
        app_module.send_via_sendgrid = fake_send_via_sendgrid
        try:
            sent_mixed = send_due_reminders(flask_app)
        finally:
            app_module.send_via_sendgrid = original_send

        assert sent_mixed == 1, f"Expected only User B's reminder to succeed, got {sent_mixed}"
        db.session.refresh(spotify)
        db.session.refresh(zoom)
        assert spotify.reminder_7_sent is False, (
            "User A's failed send should not be marked as sent (would skip retry)"
        )
        assert zoom.reminder_1_sent is True, (
            "User B's successful send should be marked sent despite User A's failure"
        )
        print("TEST K/L PASSED (one user's send failure does not corrupt another user's data)")

    # TEST: reminder task token protection at the HTTP layer (Authorization: Bearer header)
    print("TEST: /tasks/run-reminders Bearer token protection")

    def reminder_request(token=None, use_query_string=False):
        url = BASE_URL + "/tasks/run-reminders"
        headers = {}
        if use_query_string and token:
            url += f"?token={token}"
        elif token:
            headers["Authorization"] = f"Bearer {token}"
        return urllib.request.Request(url, headers=headers)

    try:
        r = urllib.request.urlopen(reminder_request())
        raise AssertionError(f"Expected 401 with no token, got {r.status}")
    except urllib.error.HTTPError as e:
        assert e.code == 401, f"Expected 401 with no token, got {e.code}"
    print("PASSED: missing token rejected (401)")

    try:
        r = urllib.request.urlopen(reminder_request(token="wrong"))
        raise AssertionError(f"Expected 401 with wrong token, got {r.status}")
    except urllib.error.HTTPError as e:
        assert e.code == 401, f"Expected 401 with wrong token, got {e.code}"
    print("PASSED: wrong Bearer token rejected (401)")

    try:
        r = urllib.request.urlopen(reminder_request(token="testtoken", use_query_string=True))
        raise AssertionError(f"Expected 401 for query-string token, got {r.status}")
    except urllib.error.HTTPError as e:
        assert e.code == 401, f"Expected 401 for query-string token (no longer authorizes), got {e.code}"
    print("PASSED: query-string token no longer authorizes (401)")

    r = urllib.request.urlopen(reminder_request(token="testtoken"))
    assert r.status == 200, f"Expected 200 with correct Bearer token, got {r.status}"
    body = r.read().decode("utf-8", errors="ignore")
    assert '"sent"' in body, 'Reminder endpoint JSON missing "sent" key'
    print("PASSED: valid Bearer token succeeds (200)")
    print("TEST PASSED (reminder token protection)")

    # Confirm the reminder token never appears in rendered HTML
    resp = session_a.get("/")
    html = read(resp)
    assert "testtoken" not in html, "Reminder task token leaked into dashboard HTML"
    print("TEST PASSED (reminder token not present in frontend HTML)")

    # TEST M: Logout protects private routes
    print("TEST M: Logout protects private routes")
    logout(session_a)
    resp = session_a.get("/")
    assert "/login" in resp.geturl(), f"Expected redirect to /login, got {resp.geturl()}"
    html = read(resp)
    assert "Netflix" not in html, "Private trial data visible after logout"

    resp = session_a.get("/trials/add")
    assert "/login" in resp.geturl(), "add_trial not protected after logout"

    resp = session_a.get("/export/csv")
    assert "/login" in resp.geturl(), "export_csv not protected after logout"
    print("TEST M PASSED")

    print("\nAll tests (A-M) passed.")


if __name__ == "__main__":
    main()
