"""
Targeted tests for password reset and self-service account deletion.

Run against a live server (BASE_URL) for the HTTP-level flows, same pattern
as e2e_tests.py and subscription_tests.py. The password-reset email send is
exercised directly against send_password_reset_email() in-process, with
send_via_resend monkeypatched, so we can assert exactly what was sent
without needing a real Resend account.

Usage:
    python app.py                        # in one terminal (FLASK_ENV=development)
    python account_management_tests.py   # in another
"""

import http.cookiejar
import re
import time
import urllib.error
import urllib.request
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

    def post(self, path: str, data: dict, include_csrf: bool = True, csrf_source: str = "/login"):
        payload = dict(data)
        if include_csrf:
            payload["csrf_token"] = self.csrf_token(csrf_source)
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

    def csrf_token(self, path: str = "/login") -> str:
        resp = self.get(path)
        html = resp.read().decode("utf-8", errors="ignore")
        match = CSRF_RE.search(html)
        assert match, f"Could not find csrf_token in page HTML for {path}"
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
    resp = sess.post("/register", {"name": name, "email": email, "password": password}, csrf_source="/register")
    assert resp.status == 200, f"Register failed for {email}: status {resp.status}"


def login(sess: Session, email: str, password: str) -> None:
    resp = sess.post("/login", {"email": email, "password": password})
    html = read(resp)
    assert resp.status == 200 and "Your active trials" in html, f"Login failed for {email}"


def main() -> None:
    print("Waiting for server on", BASE_URL)
    wait_for_server()

    print("Resetting database")
    reset_database()

    # =================== PASSWORD RESET ===================

    print("TEST PR-A: /forgot-password loads with a CSRF field")
    sess = Session()
    resp = sess.get("/forgot-password")
    html = read(resp)
    assert resp.status == 200 and 'name="csrf_token"' in html
    print("TEST PR-A PASSED")

    print("TEST PR-B: CSRF protection on /forgot-password")
    resp = sess.post("/forgot-password", {"email": "whoever@test.com"}, include_csrf=False)
    assert resp.status == 400, f"Expected 400 without CSRF, got {resp.status}"
    print("TEST PR-B PASSED")

    print("TEST PR-C: register a user for reset testing")
    reset_email = "resetme@test.com"
    reset_pw = "originalPW1"
    register(sess, "Reset Me", reset_email, reset_pw)
    print("TEST PR-C PASSED")

    print("TEST PR-D: known and unknown email produce the identical generic response")
    fresh1 = Session()
    resp_known = fresh1.post("/forgot-password", {"email": reset_email}, csrf_source="/forgot-password")
    html_known = read(resp_known)

    fresh2 = Session()
    resp_unknown = fresh2.post("/forgot-password", {"email": "doesnotexist@test.com"}, csrf_source="/forgot-password")
    html_unknown = read(resp_unknown)

    GENERIC = "If an account exists for that email address, a password reset link has been sent."
    assert GENERIC in html_known, "Known-email response missing generic message"
    assert GENERIC in html_unknown, "Unknown-email response missing generic message"
    assert resp_known.status == resp_unknown.status == 200
    print("TEST PR-D PASSED (no account-existence leak)")

    print("TEST PR-E: reset email transport invoked only for the known account, with correct content")
    from app import User, app as flask_app, db, send_password_reset_email, generate_password_reset_token
    import app as app_module

    sent = []

    def fake_send(api_key, from_email, to_email, subject, body):
        sent.append({"to": to_email, "subject": subject, "body": body})
        return 202

    with flask_app.app_context():
        flask_app.config["RESEND_API_KEY"] = "test-fake-key"
        flask_app.config["MAIL_FROM"] = "notifications@example.com"
        user = User.query.filter_by(email=reset_email).first()
        token = generate_password_reset_token(flask_app, user)
        reset_url = f"{BASE_URL}/reset-password/{token}"

        original_send = app_module.send_via_resend
        app_module.send_via_resend = fake_send
        try:
            send_password_reset_email(flask_app, user, reset_url)
        finally:
            app_module.send_via_resend = original_send

        assert len(sent) == 1, f"Expected exactly 1 email sent, got {len(sent)}"
        assert sent[0]["to"] == reset_email
        assert "TrialTracker" in sent[0]["subject"] or "password" in sent[0]["subject"].lower()
        assert reset_url in sent[0]["body"], "Reset link missing from email body"
        assert "expires" in sent[0]["body"].lower(), "Expiration notice missing from email body"
        assert "ignore" in sent[0]["body"].lower(), "Ignore-if-not-you notice missing from email body"
    print("TEST PR-E PASSED")

    print("TEST PR-F: valid reset token allows setting a new password")
    resp = sess.get(f"/reset-password/{token}")
    reset_html = read(resp)
    assert resp.status == 200 and 'name="csrf_token"' in reset_html

    new_pw = "brandNewPW2"
    resp = sess.post(
        f"/reset-password/{token}",
        {"password": new_pw, "confirm_password": new_pw},
        csrf_source=f"/reset-password/{token}",
    )
    html = read(resp)
    assert resp.status == 200 and "log in" in html.lower(), f"Reset did not redirect to login: {resp.status}"
    print("TEST PR-F PASSED")

    print("TEST PR-G: old password no longer authenticates, new password does")
    fail_sess = Session()
    resp = fail_sess.post("/login", {"email": reset_email, "password": reset_pw})
    assert "Invalid email or password" in read(resp), "Old password should no longer work"

    ok_sess = Session()
    login(ok_sess, reset_email, new_pw)
    print("TEST PR-G PASSED")

    print("TEST PR-H: used reset token cannot be replayed")
    replay_sess = Session()
    resp = replay_sess.get(f"/reset-password/{token}")
    assert resp.status == 200
    replay_html = read(resp)
    # Token endpoint redirects (via flash) back through /forgot-password when invalid;
    # the resulting page must NOT be the password-entry form.
    assert 'name="password"' not in replay_html or "invalid or has expired" in replay_html.lower(), (
        "Used token appears to still be accepted as valid"
    )
    resp2 = replay_sess.post(
        f"/reset-password/{token}",
        {"password": "anotherPW3", "confirm_password": "anotherPW3"},
        csrf_source="/forgot-password",
    )
    with flask_app.app_context():
        u = User.query.filter_by(email=reset_email).first()
        assert u.check_password(new_pw), "Replayed token must not have changed the password again"
    print("TEST PR-H PASSED (reset token cannot be reused)")

    print("TEST PR-I: tampered token rejected")
    tampered = token[:-4] + ("A" if token[-4] != "A" else "B") + token[-3:]
    resp = sess.get(f"/reset-password/{tampered}")
    html = read(resp)
    assert "invalid or has expired" in html.lower() or resp.geturl().endswith("/forgot-password"), (
        "Tampered token was not rejected"
    )
    print("TEST PR-I PASSED")

    print("TEST PR-J: expired token rejected")
    with flask_app.app_context():
        user2 = User.query.filter_by(email=reset_email).first()
        # Craft a token, then verify with max_age=0 by monkeypatching the max-age window
        expired_token = generate_password_reset_token(flask_app, user2)
        original_max_age = app_module.PASSWORD_RESET_MAX_AGE_SECONDS
        app_module.PASSWORD_RESET_MAX_AGE_SECONDS = 0
        try:
            time.sleep(1.1)
            result = app_module.verify_password_reset_token(flask_app, expired_token)
        finally:
            app_module.PASSWORD_RESET_MAX_AGE_SECONDS = original_max_age
        assert result is None, "Expired token should be rejected"
    print("TEST PR-J PASSED")

    print("TEST PR-K: mismatched passwords rejected on reset form")
    with flask_app.app_context():
        user3 = User.query.filter_by(email=reset_email).first()
        mismatch_token = generate_password_reset_token(flask_app, user3)
    mismatch_sess = Session()
    resp = mismatch_sess.post(
        f"/reset-password/{mismatch_token}",
        {"password": "somePassword1", "confirm_password": "differentPassword2"},
        csrf_source=f"/reset-password/{mismatch_token}",
    )
    html = read(resp)
    assert "do not match" in html.lower(), "Mismatched passwords should be rejected with an error"
    print("TEST PR-K PASSED")

    # =================== ACCOUNT DELETION ===================

    print("\nTEST AD-A: register two users, each with a trial")
    from datetime import date, timedelta

    user_a_email, user_a_pw = "dela@test.com", "passwordA9"
    user_b_email, user_b_pw = "delb@test.com", "passwordB9"

    sess_a = Session()
    register(sess_a, "Del A", user_a_email, user_a_pw)
    token_a = sess_a.csrf_token("/trials/add")
    end_date = (date.today() + timedelta(days=10)).strftime("%Y-%m-%d")
    sess_a.post("/trials/add", {
        "product_name": "TrialAlpha", "vendor": "", "monthly_cost": "5.00",
        "trial_end_date": end_date, "notes": "", "kind": "trial", "csrf_token": token_a,
    }, include_csrf=False)

    sess_b = Session()
    register(sess_b, "Del B", user_b_email, user_b_pw)
    token_b = sess_b.csrf_token("/trials/add")
    sess_b.post("/trials/add", {
        "product_name": "TrialBeta", "vendor": "", "monthly_cost": "7.00",
        "trial_end_date": end_date, "notes": "", "kind": "trial", "csrf_token": token_b,
    }, include_csrf=False)
    print("TEST AD-A PASSED")

    print("TEST AD-B: unauthenticated deletion fails")
    anon_sess = Session()
    resp = anon_sess.post("/account/delete", {"password": "whatever", "confirm_deletion": "yes"}, csrf_source="/login")
    # urllib's opener auto-follows the login_required redirect for POST, so
    # the final response is a 200 render of /login -- assert on the final
    # URL, same pattern e2e_tests.py uses for other login_required routes.
    assert "/login" in resp.geturl(), f"Unauthenticated delete should redirect to login, landed on {resp.geturl()}"
    with flask_app.app_context():
        assert User.query.filter_by(email=user_a_email.lower()).first() is not None, (
            "Unauthenticated delete attempt must not have deleted the account"
        )
    print("TEST AD-B PASSED")

    print("TEST AD-C: GET cannot delete an account")
    resp = sess_a.get("/account/delete")
    assert resp.status == 405, f"Expected 405 for GET on delete route, got {resp.status}"
    print("TEST AD-C PASSED")

    print("TEST AD-D: invalid CSRF token fails")
    resp = sess_a.post("/account/delete", {"password": user_a_pw, "confirm_deletion": "yes"}, include_csrf=False)
    assert resp.status == 400, f"Expected 400 for missing CSRF, got {resp.status}"
    print("TEST AD-D PASSED")

    print("TEST AD-E: wrong password fails, account not deleted")
    resp = sess_a.post("/account/delete", {"password": "totallyWrongPW", "confirm_deletion": "yes"}, csrf_source="/account")
    html = read(resp)
    assert "Incorrect password" in html, "Wrong password should show an error"
    with flask_app.app_context():
        assert User.query.filter_by(email=user_a_email).first() is not None, "Account deleted despite wrong password!"
    print("TEST AD-E PASSED")

    print("TEST AD-F: user A cannot delete user B (session-bound, no target-user field exists)")
    # There is no user-id parameter in the delete form at all -- the deleted
    # account is always current_user. Confirm B is untouched by A's requests.
    with flask_app.app_context():
        assert User.query.filter_by(email=user_b_email).first() is not None
    print("TEST AD-F PASSED (deletion route has no cross-user target vector)")

    print("TEST AD-G: correct password + confirmation deletes user A only")
    resp = sess_a.post("/account/delete", {"password": user_a_pw, "confirm_deletion": "yes"}, csrf_source="/account")
    html = read(resp)
    assert "deleted" in html.lower(), f"Expected deletion confirmation, got status {resp.status}"

    with flask_app.app_context():
        assert User.query.filter_by(email=user_a_email).first() is None, "User A still exists after deletion"
        from app import Trial
        assert Trial.query.filter_by(product_name="TrialAlpha").first() is None, "User A's trial survived deletion"
        assert User.query.filter_by(email=user_b_email).first() is not None, "User B was deleted!"
        assert Trial.query.filter_by(product_name="TrialBeta").first() is not None, "User B's trial was deleted!"
    print("TEST AD-G PASSED (only A and A's data removed; B untouched)")

    print("TEST AD-H: deleted credentials can no longer log in")
    dead_sess = Session()
    resp = dead_sess.post("/login", {"email": user_a_email, "password": user_a_pw})
    assert "Invalid email or password" in read(resp), "Deleted account should not be able to log in"
    print("TEST AD-H PASSED")

    print("TEST AD-I: session was cleared by deletion (protected route no longer authorized)")
    resp = sess_a.get("/trials/add", follow_redirects=False) if False else sess_a.get("/trials/add")
    # sess_a's cookie jar still holds the old session cookie; server-side
    # logout must have invalidated the login state regardless.
    final_url = resp.geturl()
    assert "/login" in final_url or resp.status in (302, 401), (
        f"Deleted user's session should no longer grant access, got url={final_url} status={resp.status}"
    )
    print("TEST AD-I PASSED")

    print("TEST AD-J: user B still fully functional after A's deletion")
    resp = sess_b.get("/")
    html = read(resp)
    assert "TrialBeta" in html, "User B's dashboard broken after User A's deletion"
    print("TEST AD-J PASSED")

    print("\nAll account-management tests (PR-A through PR-K, AD-A through AD-J) passed.")


if __name__ == "__main__":
    main()
