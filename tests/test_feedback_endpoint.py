from fastapi.testclient import TestClient

from app.main import app
from app.session_tracker import tracker


def test_feedback_records_event_on_existing_session():
    session = tracker.create_session(session_type="websocket_voice", summary="test call")
    client = TestClient(app)

    resp = client.post("/feedback", json={
        "session_id": session.id,
        "rating": 4,
        "resolved": True,
        "comment": "worked great",
    })

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    stored = tracker.get_session(session.id).to_dict(include_events=True)
    feedback_events = [e for e in stored["events"] if e["event_type"] == "feedback"]
    assert len(feedback_events) == 1
    assert feedback_events[0]["details"] == {
        "rating": 4,
        "resolved": True,
        "comment": "worked great",
    }


def test_feedback_comment_is_optional():
    session = tracker.create_session(session_type="websocket_voice", summary="test call 2")
    client = TestClient(app)

    resp = client.post("/feedback", json={
        "session_id": session.id,
        "rating": 2,
        "resolved": False,
    })

    assert resp.status_code == 200
    stored = tracker.get_session(session.id).to_dict(include_events=True)
    feedback_events = [e for e in stored["events"] if e["event_type"] == "feedback"]
    assert feedback_events[0]["details"]["comment"] is None


def test_feedback_404s_for_unknown_session():
    client = TestClient(app)

    resp = client.post("/feedback", json={
        "session_id": "does-not-exist",
        "rating": 3,
        "resolved": True,
    })

    assert resp.status_code == 404
