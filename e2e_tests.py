from datetime import date, timedelta
from urllib.parse import urlencode
import json
import time
import urllib.error
import urllib.request

from app import get_db_connection


BASE_URL = "http://127.0.0.1:5000"


def http_get(path: str) -> urllib.request.addinfourl:
    return urllib.request.urlopen(BASE_URL + path)


def http_post(path: str, data: dict) -> urllib.request.addinfourl:
    encoded = urlencode(data).encode("utf-8")
    req = urllib.request.Request(
        BASE_URL + path,
        data=encoded,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    return urllib.request.urlopen(req)


def wait_for_server(timeout: float = 15.0) -> None:
    start = time.time()
    last_err: Exception | None = None
    while time.time() - start < timeout:
        try:
            resp = http_get("/")
            print("Initial GET / status:", resp.status, "url:", resp.geturl())
            return
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(0.5)
    raise SystemExit(f"Server did not become ready within {timeout}s: {last_err}")


def main() -> None:
    # Ensure server is up (covers Test 1 basic availability)
    print("Waiting for server on", BASE_URL)
    wait_for_server()

    # TEST 2: visit /, confirm redirect to /setup on clean DB
    print("TEST 2: GET / and expect redirect to /setup")
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM trials")
    cur.execute("DELETE FROM settings")
    conn.commit()
    conn.close()

    resp = http_get("/")
    html = resp.read().decode("utf-8", errors="ignore")
    final_url = resp.geturl()
    print("Status:", resp.status, "Final URL:", final_url)
    assert resp.status == 200, f"Expected HTTP 200 after redirect, got {resp.status}"
    assert final_url.endswith("/setup"), f"Expected redirect to /setup, got {final_url}"
    assert "Welcome to TrialTracker" in html, "Setup page copy not found"
    print("TEST 2 PASSED")

    # TEST 3: Complete setup
    print("TEST 3: POST /setup with Test User")
    resp = http_post(
        "/setup",
        {"owner_name": "Test User", "owner_email": "test@test.com"},
    )
    html = resp.read().decode("utf-8", errors="ignore")
    final_url = resp.geturl()
    print("Status:", resp.status, "Final URL:", final_url)
    assert resp.status == 200, f"Expected 200 after setup redirect, got {resp.status}"
    assert "/setup" not in final_url, f"Expected dashboard after setup, got {final_url}"
    assert "Your active trials" in html, "Dashboard heading not found after setup"
    print("TEST 3 PASSED")

    # TEST 4: Add 3 trials
    print("TEST 4: Add 3 trials")

    def add_trial(name: str, days: int, cost: float) -> None:
        end_date = date.today() + timedelta(days=days)
        resp_local = http_post(
            "/trials/add",
            {
                "product_name": name,
                "vendor": f"{name} Inc",
                "monthly_cost": str(cost),
                "trial_end_date": end_date.strftime("%Y-%m-%d"),
                "notes": "",
            },
        )
        assert (
            resp_local.status == 200
        ), f"Add trial for {name} failed with status {resp_local.status}"
        _ = resp_local.read()

    add_trial("Netflix", 2, 15.99)
    add_trial("Spotify", 8, 9.99)
    add_trial("Adobe", 30, 54.99)
    print("TEST 4 PASSED")

    # TEST 5, 6, 7: Dashboard ordering, urgency, total monthly cost
    print("TEST 5/6/7: Verify dashboard ordering, urgency, and total cost")
    resp = http_get("/")
    html = resp.read().decode("utf-8", errors="ignore")

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

    assert (
        "$80.97" in html
    ), "Expected total monthly cost $80.97 in dashboard HTML (15.99 + 9.99 + 54.99)"
    print("TEST 5/6/7 PASSED")

    # DB IDs
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT id FROM trials WHERE product_name='Netflix'")
    netflix_id = cur.fetchone()[0]
    cur.execute("SELECT id FROM trials WHERE product_name='Adobe'")
    adobe_id = cur.fetchone()[0]
    conn.close()

    # TEST 8: Edit Netflix cost
    print("TEST 8: Edit Netflix cost to 22.99")
    resp = http_post(
        f"/trials/{netflix_id}/edit",
        {
            "product_name": "Netflix",
            "vendor": "Netflix Inc",
            "monthly_cost": "22.99",
            "trial_end_date": (date.today() + timedelta(days=2)).strftime("%Y-%m-%d"),
            "notes": "",
        },
    )
    _ = resp.read()
    assert resp.status == 200, f"Edit Netflix failed with status {resp.status}"

    resp = http_get("/")
    html = resp.read().decode("utf-8", errors="ignore")
    assert "$22.99" in html, "Expected updated Netflix cost $22.99 in dashboard HTML"
    print("TEST 8 PASSED")

    # TEST 9: Delete Adobe
    print("TEST 9: Delete Adobe")
    # raw POST with empty body
    req = urllib.request.Request(
        BASE_URL + f"/trials/{adobe_id}/delete",
        data=b"",
        method="POST",
    )
    resp = urllib.request.urlopen(req)
    _ = resp.read()
    assert resp.status == 200, f"Delete Adobe failed with status {resp.status}"

    resp = http_get("/")
    html = resp.read().decode("utf-8", errors="ignore")
    assert "Adobe" not in html, "Adobe still appears on dashboard after delete"
    print("TEST 9 PASSED")

    # TEST 10: CSV export
    print("TEST 10: Export CSV")
    resp = http_get("/export/csv")
    csv_text = resp.read().decode("utf-8", errors="ignore")
    assert (
        "Product,Vendor,Monthly Cost,Trial End Date,Notes,Created At" in csv_text
    ), "CSV header row missing or incorrect"
    assert "Netflix" in csv_text and "Spotify" in csv_text and "Adobe" not in csv_text, (
        "CSV contents do not match expected trials after delete"
    )
    print("TEST 10 PASSED")

    # TEST 11: Reminder endpoint
    print("TEST 11: Reminder endpoint")
    resp = http_get("/tasks/run-reminders?token=testtoken")
    body = resp.read().decode("utf-8", errors="ignore")
    print("Status:", resp.status, "Body:", body)
    assert resp.status == 200, f"Reminder endpoint failed with status {resp.status}"
    payload = json.loads(body)
    assert "sent" in payload, 'Reminder endpoint JSON missing "sent" key'
    print("TEST 11 PASSED")

    print("All HTTP-level tests 2–11 completed successfully.")


if __name__ == "__main__":
    main()

