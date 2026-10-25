def test_root_redirects_to_frontend(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/app/"


def test_frontend_files_are_served(client):
    page = client.get("/app/")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    assert "<title>The Last Seat</title>" in page.text
    for asset, kind in (("/app/app.js", "javascript"), ("/app/styles.css", "text/css")):
        r = client.get(asset)
        assert r.status_code == 200 and kind in r.headers["content-type"]


def test_frontend_only_calls_existing_api_paths(client):
    js = client.get("/app/app.js").text
    paths = client.get("/openapi.json").json()["paths"]
    for prefix in (
        "/api/v1/events",
        "/api/v1/reservations",
        "/api/v1/payments",
        "/api/v1/customers",
    ):
        assert prefix in js
        assert any(p.startswith(prefix) for p in paths)


def test_unknown_static_file_uses_error_envelope(client):
    r = client.get("/app/missing.js")
    assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"
