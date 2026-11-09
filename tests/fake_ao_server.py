"""In-process stdlib fake of the AO daemon REST API (no live daemon, no network).

Adapted from the hermes-ao-connector test suite, extended with the surface the
ao-bridge board uses: the projects list, the sessions list, and kill.
All state lives in a ``FakeAO`` object the test holds.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse


def make_session(session_id: str, *, project_id: str = "", display_name: str = "",
                 status: str = "working", harness: str = "hermes",
                 terminated: bool = False, created_at: str = "2026-01-01T00:00:00Z",
                 kanban_column: str = "", provision_state: str = "ready",
                 display_status: str = "", prs: list | None = None) -> dict:
    return {
        "id": session_id, "kind": "worker", "harness": harness,
        "displayName": display_name or session_id, "mode": "chat",
        "activity": {"state": "active", "lastActivityAt": created_at},
        "isTerminated": terminated, "status": status,
        "displayStatus": display_status, "kanbanColumn": kanban_column,
        "provisionState": provision_state, "projectId": project_id,
        "branch": "feat/test" if project_id else "",
        "prs": prs or [], "createdAt": created_at, "updatedAt": created_at,
    }


class FakeAO:
    """Programmable AO daemon state."""

    def __init__(self):
        self.project_rows: list[dict] = []
        self.sessions_by_id: dict[str, dict] = {}
        self.conversations: dict[str, list[dict]] = {}
        self.activities: dict[str, list[dict]] = {}
        self.turns: dict[str, list[dict]] = {}
        self.conversation_requests: list[str] = []   # conversation fetches seen
        self.created: list[dict] = []            # spawn payloads seen
        self.sends: list[dict] = []              # send payloads seen
        self.steers: list[dict] = []             # steer-or-send payloads seen
        self.steer_error: Optional[tuple] = None  # (code, status, message) to answer instead
        self.killed: list[str] = []
        self.healthz_ok = True
        self.spawn_status = 201
        self.send_status = 200
        self.kill_status = 200
        self.next_session_id = 1

    def add_session(self, session: dict) -> None:
        self.sessions_by_id[session["id"]] = session

    def set_conversation(self, session_id: str, messages: list[dict]) -> None:
        self.conversations[session_id] = messages

    def set_activities(self, session_id: str, activities: list[dict]) -> None:
        self.activities[session_id] = activities

    def set_turns(self, session_id: str, turns: list[dict]) -> None:
        self.turns[session_id] = turns

    def session_ids(self) -> list[str]:
        return list(self.sessions_by_id.keys())


class _Handler(BaseHTTPRequestHandler):

    @property
    def ao(self) -> FakeAO:
        return self.server.ao  # type: ignore[attr-defined]

    def log_message(self, format, *args):  # noqa: A002  (base signature)
        pass

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except ValueError:
            return {}

    def _json(self, code: int, obj) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _parts(self) -> list[str]:
        return urlparse(self.path).path.split("/")

    def _session_id(self) -> str:
        # /api/v1/sessions/{id} or /api/v1/sessions/{id}/...
        return self._parts()[4] if len(self._parts()) > 4 else ""

    def do_GET(self):
        ao = self.ao
        path = urlparse(self.path).path
        if path == "/healthz":
            return self._json(200 if ao.healthz_ok else 503,
                              {"ok": ao.healthz_ok, "status": "ok" if ao.healthz_ok else "down"})
        if path == "/api/v1/projects":
            return self._json(200, {"projects": ao.project_rows})
        if path == "/api/v1/sessions":
            return self._json(200, {"sessions": list(ao.sessions_by_id.values())})
        if path.endswith("/conversation"):
            session_id = self._session_id()
            if session_id not in ao.sessions_by_id:
                return self._json(404, {"error": "not found"})
            ao.conversation_requests.append(session_id)
            return self._json(200, {"sessionId": session_id,
                                    "latestSequence": len(ao.conversations.get(session_id) or []),
                                    "messages": ao.conversations.get(session_id) or [],
                                    "activities": ao.activities.get(session_id) or [],
                                    "turns": ao.turns.get(session_id) or []})
        session = ao.sessions_by_id.get(self._session_id())
        if session is None:
            return self._json(404, {"error": "not found"})
        return self._json(200, session)

    def do_POST(self):
        ao = self.ao
        path = urlparse(self.path).path
        body = self._read_body()
        if path == "/api/v1/sessions":
            session_id = f"s-fake-{ao.next_session_id}"
            ao.next_session_id += 1
            ao.add_session(make_session(session_id))
            ao.created.append(body)
            if ao.spawn_status != 201:
                return self._json(ao.spawn_status, {"error": "boom"})
            return self._json(201, {"session": {"id": session_id,
                                                "displayName": body.get("displayName") or session_id}})
        if path.endswith("/conversation/steer-or-send"):
            session = ao.sessions_by_id.get(self._session_id())
            if session is None:
                return self._json(404, {"error": "not found"})
            # Wire truth (live daemon + AO backend source, 2026-11-09): the
            # steer body is {"text", "clientMessageId"} — "text", not "message",
            # and the idempotency handle is REQUIRED (no handle → the daemon
            # answers 409 CHAT_STEER_UNCERTAIN, not a validation error).
            if not str(body.get("text") or "").strip():
                return self._json(400, {"error": "validation", "code": "CHAT_STEER_TEXT_REQUIRED",
                                        "message": "steer text is required", "requestId": "fake"})
            if not str(body.get("clientMessageId") or ""):
                return self._json(409, {"error": "conflict", "code": "CHAT_STEER_UNCERTAIN",
                                        "message": "the provider may have received this guidance",
                                        "requestId": "fake"})
            ao.steers.append({"session_id": self._session_id(), "body": body})
            if ao.steer_error is not None:
                code, status, message = ao.steer_error
                return self._json(status, {"error": "conflict", "code": code,
                                           "message": message, "requestId": "fake"})
            # The daemon steers a working session and queues a turn on an idle one.
            outcome = "steered" if session.get("status") == "working" else "sent"
            return self._json(202, {"outcome": outcome, "turnId": "t-fake",
                                    "providerTurnId": "pt-fake",
                                    "state": "running" if outcome == "steered" else "queued",
                                    "duplicate": False})
        if path.endswith("/send"):
            session = ao.sessions_by_id.get(self._session_id())
            if session is None:
                return self._json(404, {"error": "not found"})
            if session["isTerminated"]:
                return self._json(409, {"error": "terminated"})
            ao.sends.append({"session_id": self._session_id(), "body": body})
            if ao.send_status != 200:
                return self._json(ao.send_status, {"error": "boom"})
            return self._json(200, {"ok": True, "sessionId": self._session_id(),
                                    "message": body.get("message")})
        if path.endswith("/kill"):
            session = ao.sessions_by_id.get(self._session_id())
            if session is None:
                return self._json(404, {"error": "not found"})
            ao.killed.append(self._session_id())
            session["isTerminated"] = True
            session["status"] = "terminated"
            if ao.kill_status != 200:
                return self._json(ao.kill_status, {"error": "boom"})
            return self._json(200, {"ok": True})
        return self._json(404, {"error": "no such route"})


class FakeAOServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, ao: FakeAO):
        self.ao = ao
        super().__init__(("127.0.0.1", 0), _Handler)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def start(self) -> None:
        threading.Thread(target=self.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.shutdown()
