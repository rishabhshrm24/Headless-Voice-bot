from fastapi.testclient import TestClient

from app.main import app


def test_call_route_serves_html(caller_client):
    client = caller_client

    resp = client.get("/call")

    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "<title>" in resp.text
