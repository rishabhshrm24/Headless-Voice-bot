from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
import pytest

from app import auth
from app.main import app


def anon():
    return TestClient(app, follow_redirects=False)


def test_anonymous_is_locked_out_everywhere():
    c = anon()
    assert c.get("/call").status_code in (302, 307)
    assert c.get("/dashboard").status_code in (302, 307)
    assert c.get("/static/call.css").status_code == 401
    for path in ["/api/sessions", "/api/stats", "/api/knowledge-base"]:
        assert c.get(path).status_code == 401
    assert c.post("/chat", json={"question": "hi"}).status_code == 401
    assert c.post("/chat/batch", json={"questions": ["hi"]}).status_code == 401
    assert c.post("/ingest").status_code == 401
    assert c.post("/api/sessions/clear").status_code == 401
    assert c.post("/feedback", json={"session_id": "x", "rating": 3, "resolved": True}).status_code == 401
    assert c.get("/openapi.json").status_code == 404
    assert c.get("/docs").status_code == 404


def test_public_surface_is_minimal():
    c = anon()
    assert c.get("/health").json() == {"status": "ok"}
    assert "Disallow: /" in c.get("/robots.txt").text
    assert "noindex" in c.get("/health").headers["x-robots-tag"]


def test_wrong_code_rejected_and_throttled():
    c = anon()
    for _ in range(10):
        assert c.post("/login", json={"code": "nope"}).status_code == 401
    assert c.post("/login", json={"code": "test-code"}).status_code == 429


def test_caller_can_reach_call_but_not_admin(caller_client):
    assert caller_client.get("/call").status_code == 200
    assert caller_client.get("/static/call.css").status_code == 200
    assert caller_client.get("/api/stats").status_code == 401
    assert caller_client.post("/chat", json={"question": "hi"}).status_code == 401


def test_admin_reaches_dashboard_and_api(admin_client):
    assert admin_client.get("/dashboard").status_code == 200
    assert admin_client.get("/api/stats").status_code == 200


def test_admin_header_works_for_scripts():
    r = anon().get("/api/stats", headers={"X-Admin-Key": "test-admin"})
    assert r.status_code == 200


def test_static_path_traversal_blocked(caller_client):
    assert caller_client.get("/static/../main.py").status_code == 404
    assert caller_client.get("/static/%2e%2e/main.py").status_code == 404


def test_unset_secrets_fail_closed(monkeypatch):
    from app import config
    monkeypatch.setattr(config, "ACCESS_CODES", [])
    monkeypatch.setattr(config, "ADMIN_KEY", "")
    c = anon()
    assert c.post("/login", json={"code": ""}).status_code == 401
    assert c.get("/api/stats", headers={"X-Admin-Key": ""}).status_code == 401


def test_voice_ws_requires_login():
    with pytest.raises(WebSocketDisconnect):
        with TestClient(app).websocket_connect("/ws/voice"):
            pass


def test_monitor_ws_requires_admin(caller_client):
    with pytest.raises(WebSocketDisconnect):
        with caller_client.websocket_connect("/ws/monitor"):
            pass


def test_one_call_at_a_time_and_daily_cap(monkeypatch):
    from app import config
    assert auth.try_begin_call("c") is None
    assert auth.try_begin_call("c") is not None
    auth.end_call("c", auth.time.monotonic() - 1000)
    assert auth.try_begin_call("c") is not None  # daily budget (900s) used up
