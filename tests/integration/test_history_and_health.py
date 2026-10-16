from tests.helpers import cancel, confirm, hold


def test_booking_history_lists_every_status_newest_first(client):
    a = hold(client, 80, "22BCE7777").json()["reservation"]["id"]
    b = hold(client, 81, "22BCE7777").json()["reservation"]["id"]
    c = hold(client, 82, "22BCE7777").json()["reservation"]["id"]
    hold(client, 83, "SOMEONE_ELSE")
    confirm(client, a, "SUCCESS")
    cancel(client, b)

    h = client.get("/api/v1/customers/22BCE7777/bookings").json()
    assert h["count"] == 3
    assert [r["id"] for r in h["reservations"]] == [c, b, a]
    assert {r["status"] for r in h["reservations"]} == {"HELD", "CANCELLED", "CONFIRMED"}
    assert client.get("/api/v1/customers/NOBODY/bookings").json()["count"] == 0


def test_reservation_status_endpoint(client):
    rid = hold(client, 84).json()["reservation"]["id"]
    r = client.get(f"/api/v1/reservations/{rid}").json()
    assert r["status"] == "HELD" and r["payments"] == [] and r["seconds_remaining"] > 0


def test_health_endpoints_and_openapi(client):
    assert client.get("/health/live").json() == {"status": "ok"}
    ready = client.get("/health/ready").json()
    assert ready == {"status": "ok", "database": "last_seat_test_db"}
    spec = client.get("/openapi.json").json()
    for path in (
        "/api/v1/reservations",
        "/api/v1/reservations/{reservation_id}/confirm",
        "/api/v1/reservations/{reservation_id}/cancel",
        "/api/v1/payments/{payment_id}/simulate-callback",
        "/api/v1/customers/{customer_ref}/bookings",
        "/api/v1/events/{event_id}/seats/{seat_number}",
    ):
        assert path in spec["paths"]
    assert client.get("/docs").status_code == 200
