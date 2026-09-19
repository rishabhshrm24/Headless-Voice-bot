import pytest

from app import auth, config


@pytest.fixture(autouse=True)
def _auth_config(monkeypatch):
    monkeypatch.setattr(config, "ACCESS_CODES", ["test-code"])
    monkeypatch.setattr(config, "ADMIN_KEY", "test-admin")
    monkeypatch.setattr(config, "MAX_CALL_SECONDS", 300)
    monkeypatch.setattr(config, "DAILY_CALL_SECONDS", 900)
    auth._active.clear()
    auth._used.clear()
    from app import main
    main._failed_logins.clear()


@pytest.fixture
def caller_client():
    from fastapi.testclient import TestClient
    from app.main import app
    c = TestClient(app)
    assert c.post("/login", json={"code": "test-code"}).status_code == 200
    return c


@pytest.fixture
def admin_client():
    from fastapi.testclient import TestClient
    from app.main import app
    c = TestClient(app)
    assert c.post("/login", json={"code": "test-admin"}).status_code == 200
    return c
