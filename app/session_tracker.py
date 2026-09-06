import asyncio
import logging
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Set
from fastapi import WebSocket

logger = logging.getLogger("session_tracker")


class SessionEvent:
    def __init__(
        self,
        event_type: str,
        title: str,
        details: Optional[Dict[str, Any]] = None,
        timestamp: Optional[float] = None,
    ):
        self.id = str(uuid.uuid4())[:8]
        self.timestamp = timestamp or time.time()
        self.time_str = datetime.fromtimestamp(self.timestamp).strftime("%H:%M:%S")
        self.iso_time = datetime.fromtimestamp(self.timestamp).isoformat()
        self.event_type = event_type
        self.title = title
        self.details = details or {}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "timestamp": self.timestamp,
            "time_str": self.time_str,
            "iso_time": self.iso_time,
            "event_type": self.event_type,
            "title": self.title,
            "details": self.details,
        }


class Session:
    def __init__(
        self,
        session_id: str,
        session_type: str,
        client_host: Optional[str] = None,
        summary: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        self.id = session_id
        self.session_type = session_type  # websocket_voice, http_voice, text_chat, batch_chat, openai_compat
        self.status = "active"  # active, completed, error
        self.started_at = time.time()
        self.started_iso = datetime.fromtimestamp(self.started_at).isoformat()
        self.ended_at: Optional[float] = None
        self.ended_iso: Optional[str] = None
        self.duration_seconds: float = 0.0
        self.client_host = client_host or "127.0.0.1"
        self.summary = summary or f"{session_type} session"
        self.metadata = metadata or {}
        self.events: List[SessionEvent] = []
        self.stats = {
            "user_messages": 0,
            "assistant_messages": 0,
            "rag_lookups": 0,
            "tool_calls": 0,
            "errors": 0,
        }

    def _update_summary(self, details: Dict[str, Any]) -> None:
        if not self.summary or self.summary.startswith(self.session_type):
            snippet = details.get("question") or details.get("transcript") or ""
            if snippet:
                self.summary = snippet[:80] + ("..." if len(snippet) > 80 else "")

    def _update_stats_for_event(self, event_type: str, details: Dict[str, Any]) -> None:
        stat_keys = {
            "user_message": "user_messages",
            "speech_transcribed": "user_messages",
            "assistant_reply": "assistant_messages",
            "speech_synthesized": "assistant_messages",
            "rag_query": "rag_lookups",
            "tool_call": "tool_calls",
            "function_call": "tool_calls",
            "error": "errors",
        }
        key = stat_keys.get(event_type)
        if key:
            self.stats[key] += 1
        if event_type in ("user_message", "speech_transcribed"):
            self._update_summary(details)

    def add_event(self, event_type: str, title: str, details: Optional[Dict[str, Any]] = None) -> SessionEvent:
        event = SessionEvent(event_type=event_type, title=title, details=details)
        self.events.append(event)
        self._update_stats_for_event(event_type, event.details)
        self.duration_seconds = round(time.time() - self.started_at, 2)
        return event

    def end(self, status: str = "completed", error: Optional[str] = None):
        self.status = status
        self.ended_at = time.time()
        self.ended_iso = datetime.fromtimestamp(self.ended_at).isoformat()
        self.duration_seconds = round(self.ended_at - self.started_at, 2)
        if error:
            self.status = "error"
            self.add_event("error", "Session encountered an error", {"error": error})
        self.add_event("session_end", f"Session ended ({self.status})", {"duration_seconds": self.duration_seconds})

    def to_dict(self, include_events: bool = True) -> Dict[str, Any]:
        duration = self.duration_seconds if self.ended_at else round(time.time() - self.started_at, 2)
        data = {
            "id": self.id,
            "session_type": self.session_type,
            "status": self.status,
            "started_at": self.started_at,
            "started_iso": self.started_iso,
            "ended_at": self.ended_at,
            "ended_iso": self.ended_iso,
            "duration_seconds": duration,
            "client_host": self.client_host,
            "summary": self.summary,
            "metadata": self.metadata,
            "stats": self.stats,
            "events_count": len(self.events),
        }
        if include_events:
            data["events"] = [e.to_dict() for e in self.events]
        return data


class SessionTracker:
    def __init__(self, max_sessions: int = 500):
        self._sessions: Dict[str, Session] = {}
        self._session_order: List[str] = []
        self._max_sessions = max_sessions
        self._monitors: Set[WebSocket] = set()
        self._lock = asyncio.Lock()

    def create_session(
        self,
        session_type: str,
        client_host: Optional[str] = None,
        summary: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        custom_id: Optional[str] = None,
    ) -> Session:
        prefix = {
            "websocket_voice": "ws",
            "http_voice": "voice",
            "text_chat": "chat",
            "batch_chat": "batch",
            "openai_compat": "oa",
        }.get(session_type, "sess")
        
        session_id = custom_id or f"{prefix}_{uuid.uuid4().hex[:8]}"
        session = Session(
            session_id=session_id,
            session_type=session_type,
            client_host=client_host,
            summary=summary,
            metadata=metadata,
        )
        session.add_event("session_start", f"Session started ({session_type})", {"client": client_host})
        
        self._sessions[session_id] = session
        self._session_order.append(session_id)

        # Trim old sessions if exceeding max
        while len(self._session_order) > self._max_sessions:
            oldest_id = self._session_order.pop(0)
            self._sessions.pop(oldest_id, None)

        self._broadcast_nowait({
            "action": "session_created",
            "session": session.to_dict(include_events=False),
        })
        return session

    def record_event(
        self,
        session_id: str,
        event_type: str,
        title: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> Optional[SessionEvent]:
        session = self._sessions.get(session_id)
        if not session:
            return None
        event = session.add_event(event_type, title, details)
        self._broadcast_nowait({
            "action": "session_event",
            "session_id": session_id,
            "session_status": session.status,
            "session_summary": session.summary,
            "stats": session.stats,
            "duration_seconds": session.duration_seconds,
            "event": event.to_dict(),
        })
        return event

    def end_session(
        self,
        session_id: str,
        status: str = "completed",
        error: Optional[str] = None,
    ) -> Optional[Session]:
        session = self._sessions.get(session_id)
        if not session:
            return None
        session.end(status=status, error=error)
        self._broadcast_nowait({
            "action": "session_ended",
            "session": session.to_dict(include_events=True),
        })
        return session

    def get_session(self, session_id: str) -> Optional[Session]:
        return self._sessions.get(session_id)

    def _matches_filters(
        self,
        sess: Session,
        session_type: Optional[str],
        status: Optional[str],
        search: Optional[str],
    ) -> bool:
        if session_type and session_type != "all" and sess.session_type != session_type:
            return False
        if status and status != "all" and sess.status != status:
            return False
        if search:
            q = search.lower()
            if not (q in sess.id.lower() or (sess.summary and q in sess.summary.lower()) or q in sess.client_host.lower()):
                return False
        return True

    def list_sessions(
        self,
        limit: int = 50,
        session_type: Optional[str] = None,
        status: Optional[str] = None,
        search: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        results = []
        for sid in reversed(self._session_order):
            sess = self._sessions.get(sid)
            if sess and self._matches_filters(sess, session_type, status, search):
                results.append(sess.to_dict(include_events=False))
                if len(results) >= limit:
                    break
        return results

    def get_stats(self) -> Dict[str, Any]:
        total_sessions = len(self._sessions)
        active_sessions = sum(1 for s in self._sessions.values() if s.status == "active")
        completed_sessions = sum(1 for s in self._sessions.values() if s.status == "completed")
        error_sessions = sum(1 for s in self._sessions.values() if s.status == "error")

        total_rag_searches = sum(s.stats.get("rag_lookups", 0) for s in self._sessions.values())
        total_user_messages = sum(s.stats.get("user_messages", 0) for s in self._sessions.values())
        total_tool_calls = sum(s.stats.get("tool_calls", 0) for s in self._sessions.values())

        type_counts = {
            "websocket_voice": 0,
            "http_voice": 0,
            "text_chat": 0,
            "batch_chat": 0,
            "openai_compat": 0,
        }
        for s in self._sessions.values():
            if s.session_type in type_counts:
                type_counts[s.session_type] += 1

        durations = [s.duration_seconds for s in self._sessions.values() if s.duration_seconds > 0]
        avg_duration = round(sum(durations) / len(durations), 2) if durations else 0.0

        return {
            "total_sessions": total_sessions,
            "active_sessions": active_sessions,
            "completed_sessions": completed_sessions,
            "error_sessions": error_sessions,
            "total_rag_searches": total_rag_searches,
            "total_user_messages": total_user_messages,
            "total_tool_calls": total_tool_calls,
            "type_counts": type_counts,
            "avg_duration_seconds": avg_duration,
        }

    def clear(self):
        self._sessions.clear()
        self._session_order.clear()
        self._broadcast_nowait({"action": "sessions_cleared"})

    async def register_monitor(self, websocket: WebSocket):
        await websocket.accept()
        self._monitors.add(websocket)
        logger.info("Monitor connected. Total monitors: %d", len(self._monitors))
        try:
            # Send initial stats and recent sessions list
            await websocket.send_json({
                "action": "init",
                "stats": self.get_stats(),
                "recent_sessions": self.list_sessions(limit=30),
            })
            while True:
                # Keep monitor connection open & handle ping/pong or client commands
                await websocket.receive_text()
        except Exception:
            pass
        finally:
            self._monitors.discard(websocket)
            logger.info("Monitor disconnected. Total monitors: %d", len(self._monitors))

    def _broadcast_nowait(self, payload: Dict[str, Any]):
        if not self._monitors:
            return
        
        async def _do_broadcast():
            dead_monitors = set()
            for ws in set(self._monitors):
                try:
                    await ws.send_json(payload)
                except Exception:
                    dead_monitors.add(ws)
            if dead_monitors:
                self._monitors.difference_update(dead_monitors)

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_do_broadcast())
        except RuntimeError:
            pass


# Global singleton instance
tracker = SessionTracker()
