from tests.helpers import cancel, confirm, count, hold


def test_replay_returns_identical_response_without_new_rows(client, db):
    first = hold(client, 30, key="key-30")
    second = hold(client, 30, key="key-30")
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert second.headers.get("Idempotent-Replayed") == "true"
    assert "Idempotent-Replayed" not in first.headers
    assert count(db, "SELECT COUNT(*) FROM reservations") == 1
    assert count(db, "SELECT COUNT(*) FROM idempotency_records") == 1


def test_same_key_different_payload_is_rejected(client, db):
    assert hold(client, 31, key="k").status_code == 201
    r = hold(client, 32, key="k")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "IDEMPOTENCY_KEY_PAYLOAD_MISMATCH"
    assert count(db, "SELECT COUNT(*) FROM reservations") == 1


def test_without_key_a_retry_is_a_new_request(client):
    assert hold(client, 33).status_code == 201
    assert hold(client, 33).status_code == 409  # the client's own hold now blocks it


def test_payment_replay_never_creates_second_attempt(client, db):
    rid = hold(client, 34).json()["reservation"]["id"]
    a = confirm(client, rid, "FAILURE", key="pay-1")
    b = confirm(client, rid, "FAILURE", key="pay-1")
    assert a.status_code == b.status_code == 402
    assert a.json() == b.json()
    assert count(db, "SELECT COUNT(*) FROM payment_attempts") == 1

    c = confirm(client, rid, "SUCCESS", key="pay-2")
    d = confirm(client, rid, "SUCCESS", key="pay-2")
    assert c.status_code == d.status_code == 200 and c.json() == d.json()
    assert count(db, "SELECT COUNT(*) FROM payment_attempts") == 2


def test_same_key_is_scoped_per_endpoint(client, db):
    rid = hold(client, 35, key="shared").json()["reservation"]["id"]
    assert confirm(client, rid, "SUCCESS", key="shared").status_code == 200
    assert cancel(client, rid, key="shared").status_code == 200
    assert count(db, "SELECT COUNT(*) FROM idempotency_records") == 3


def test_key_reused_for_different_reservation_is_mismatch(client):
    a = hold(client, 36).json()["reservation"]["id"]
    b = hold(client, 37).json()["reservation"]["id"]
    assert cancel(client, a, key="c1").status_code == 200
    r = cancel(client, b, key="c1")
    assert r.status_code == 409 and r.json()["error"]["code"] == "IDEMPOTENCY_KEY_PAYLOAD_MISMATCH"


def test_invalid_idempotency_key_rejected(client):
    r = hold(client, 38, key="x" * 129)
    assert r.status_code == 422 and r.json()["error"]["code"] == "INVALID_IDEMPOTENCY_KEY"
