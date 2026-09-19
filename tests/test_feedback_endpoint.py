import time

from fastapi.testclient import TestClient

from app.main import app
from app.session_tracker import tracker


def test_feedback_records_event_on_existing_session(caller_client):
    session = tracker.create_session(session_type="websocket_voice", summary="test call")
    client = caller_client

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


def test_feedback_comment_is_optional(caller_client):
    session = tracker.create_session(session_type="websocket_voice", summary="test call 2")
    client = caller_client

    resp = client.post("/feedback", json={
        "session_id": session.id,
        "rating": 2,
        "resolved": False,
    })

    assert resp.status_code == 200
    stored = tracker.get_session(session.id).to_dict(include_events=True)
    feedback_events = [e for e in stored["events"] if e["event_type"] == "feedback"]
    assert feedback_events[0]["details"]["comment"] is None


def test_feedback_404s_for_unknown_session(caller_client):
    client = caller_client

    resp = client.post("/feedback", json={
        "session_id": "does-not-exist",
        "rating": 3,
        "resolved": True,
    })

    assert resp.status_code == 404


def test_feedback_rejects_rating_out_of_range(caller_client):
    session = tracker.create_session(session_type="websocket_voice", summary="test call 3")
    client = caller_client

    too_low = client.post("/feedback", json={
        "session_id": session.id,
        "rating": 0,
        "resolved": True,
    })
    too_high = client.post("/feedback", json={
        "session_id": session.id,
        "rating": 6,
        "resolved": True,
    })

    assert too_low.status_code == 422
    assert too_high.status_code == 422


def test_feedback_after_session_end_does_not_change_duration(caller_client):
    session = tracker.create_session(session_type="websocket_voice", summary="test call 4")
    time.sleep(0.05)
    tracker.end_session(session.id, status="completed")
    ended_duration = tracker.get_session(session.id).duration_seconds

    time.sleep(0.05)
    client = caller_client
    resp = client.post("/feedback", json={
        "session_id": session.id,
        "rating": 5,
        "resolved": True,
        "comment": "posted after the call ended",
    })

    assert resp.status_code == 200
    assert tracker.get_session(session.id).duration_seconds == ended_duration
