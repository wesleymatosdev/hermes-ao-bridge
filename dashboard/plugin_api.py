"""ao-bridge backend API — the local AO daemon surfaced to the Hermes dashboard.

A third-party dashboard plugin: ``dashboard/plugin_api.py`` is imported by the
dashboard web server (spec_from_file_location) and its ``router`` is mounted at
``/api/plugins/ao-bridge/``. This module therefore must be importable standalone:
stdlib + fastapi only, with the ``hermes_cli`` config read wrapped best-effort so
bare-FastAPI test harnesses and the plugin validator can load it too.

Network contract: talks ONLY to the local AO daemon REST API (default
``http://127.0.0.1:3001``). No secrets, no external calls. The base URL is
configurable via the plugin's ``config_schema`` (``plugins.entries.ao-bridge.settings.ao_base_url``
in config.yaml), overridable with ``AO_BRIDGE_BASE_URL`` for development.

AO wire contract (verified against the live daemon and the AO backend source):
- GET  /healthz
- GET  /api/v1/projects                        -> {"projects": [...]}
- GET  /api/v1/sessions                        -> {"sessions": [...]}
- POST /api/v1/sessions                        spawn (201, {"session": {...}})
- POST /api/v1/sessions/{id}/send              {"message", "userAuthored"}
- POST /api/v1/sessions/{id}/kill
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

router = APIRouter()
log = logging.getLogger("ao-bridge")

PLUGIN_ID = "ao-bridge"
DEFAULT_BASE_URL = "http://127.0.0.1:3001"

# Board columns, in display order. AO session state maps onto these via
# ``column_for`` below; Archive holds terminated sessions.
COLUMN_ORDER = ["queued", "running", "review", "blocked", "done", "archive"]
COLUMN_LABELS = {
    "queued": "Queued",
    "running": "Running",
    "review": "Review",
    "blocked": "Blocked",
    "done": "Done",
    "archive": "Archive",
}

# Harnesses offered in the spawn form. The daemon accepts far more (see the
# SpawnSessionRequest enum in the AO backend); these are the ones seen working
# on this machine plus the documented defaults.
COMMON_HARNESSES = [
    "hermes", "claude-code", "codex", "opencode", "agy", "gemini", "goose", "cursor",
]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _config_setting(key: str) -> Optional[str]:
    """Best-effort read of ``plugins.entries.ao-bridge.settings.<key>`` from config.yaml.

    Runs inside the dashboard server, so ``hermes_cli`` is importable there; in
    bare test harnesses it may not be, and any failure just means "unset".
    """
    try:
        from hermes_cli.config import load_config_readonly  # type: ignore
    except Exception:  # pragma: no cover - exercised only outside the dashboard server
        return None
    try:
        config = load_config_readonly() or {}
        entries = config.get("plugins", {}).get("entries", {})
        settings = entries.get(PLUGIN_ID, {}).get("settings", {})
        value = settings.get(key)
        return str(value).strip() if value else None
    except Exception:  # pragma: no cover - an unreadable config grants no setting
        return None


def ao_base_url() -> str:
    """Resolve the AO daemon base URL: config.yaml setting, then env, then default."""
    from_config = _config_setting("ao_base_url")
    if from_config:
        return from_config.rstrip("/")
    from_env = (os.environ.get("AO_BRIDGE_BASE_URL") or "").strip()
    if from_env:
        return from_env.rstrip("/")
    return DEFAULT_BASE_URL


# ---------------------------------------------------------------------------
# AO REST client (stdlib urllib; one short-lived client per request)
# ---------------------------------------------------------------------------

class AOApiError(Exception):
    """AO daemon answered with a non-2xx status."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message

    @property
    def is_gone(self) -> bool:
        return self.status == 404

    @property
    def is_transient(self) -> bool:
        return self.status >= 500


class AOConnectionError(AOApiError):
    """The AO daemon is unreachable."""

    def __init__(self, message: str = "AO daemon is unreachable"):
        super().__init__(502, message)


class AOClient:
    """Minimal AO daemon REST client. Stdlib only; no auth by default (loopback)."""

    def __init__(self, base_url: str, timeout: float = 10.0, token: Optional[str] = None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.token = token

    def request(self, method: str, path: str, payload: Optional[dict] = None) -> dict:
        url = f"{self.base_url}{path}"
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read()
        except urllib.error.HTTPError as err:
            try:
                detail = json.loads(err.read() or b"{}")
                message = detail.get("message") or detail.get("error") or err.reason
            except (ValueError, AttributeError):
                message = err.reason
            raise AOApiError(err.code, str(message)) from err
        except (urllib.error.URLError, OSError) as err:
            raise AOConnectionError(f"AO daemon unreachable at {self.base_url}: {err}") from err
        if not body:
            return {}
        try:
            parsed = json.loads(body)
        except ValueError:
            return {"value": body.decode("utf-8", "replace")}
        return parsed if isinstance(parsed, dict) else {"value": parsed}

    # -- AO API surface (wire contract verified against the live daemon) --

    def healthz(self) -> dict:
        return self.request("GET", "/healthz")

    def projects(self) -> list:
        resp = self.request("GET", "/api/v1/projects")
        return resp.get("projects") or []

    def sessions(self) -> list:
        resp = self.request("GET", "/api/v1/sessions")
        return resp.get("sessions") or []

    def spawn(self, *, display_name: str = "", prompt: str = "", project_id: str = "",
              harness: str = "hermes", mode: str = "chat", branch: str = "") -> dict:
        body: dict = {"kind": "worker", "mode": mode, "harness": harness}
        if display_name:
            body["displayName"] = display_name
        if project_id:
            body["projectId"] = project_id
        if branch:
            body["branch"] = branch
        if prompt:
            body["prompt"] = prompt
        return self.request("POST", "/api/v1/sessions", body)

    def get_session(self, session_id: str) -> dict:
        # The daemon wraps single-session GETs in {"session": {...}} (the list
        # endpoint does not) — unwrap so callers always get the row.
        # Session ids are interpolated into daemon URLs — always percent-encode
        # them so ?, # or .. cannot reshape the request path.
        sid = urllib.parse.quote(session_id, safe="")
        resp = self.request("GET", f"/api/v1/sessions/{sid}")
        return resp.get("session") or resp

    def conversation(self, session_id: str) -> dict:
        """Conversation snapshot: ``latestSequence`` + ``messages`` rows."""
        sid = urllib.parse.quote(session_id, safe="")
        return self.request("GET", f"/api/v1/sessions/{sid}/conversation")

    def send(self, session_id: str, message: str, user_authored: bool = True) -> dict:
        sid = urllib.parse.quote(session_id, safe="")
        return self.request("POST", f"/api/v1/sessions/{sid}/send",
                            {"message": message, "userAuthored": user_authored})

    def kill(self, session_id: str) -> dict:
        sid = urllib.parse.quote(session_id, safe="")
        return self.request("POST", f"/api/v1/sessions/{sid}/kill", {})


def client(timeout_s: float = 15.0, token: Optional[str] = None) -> AOClient:
    return AOClient(ao_base_url(), timeout=timeout_s, token=token)

# A real spawn provisions a git worktree and launches the agent harness; the
# daemon routinely takes tens of seconds. Reads stay short so a dead daemon
# fails the board fast.
SPAWN_TIMEOUT_S = 120.0


# ---------------------------------------------------------------------------
# Board mapping: AO session state -> kanban column
# ---------------------------------------------------------------------------

def column_for(session: dict) -> str:
    """Map one AO session to a board column. First matching rule wins; the
    rules are ordered from most to most specific terminal state.

    Verified value domain on a live daemon (303 sessions):
      status: terminated, exited, idle, merged, working, ci_failed, review_pending
      kanbanColumn: archive, building, validating, ready, needs_review
      provisionState: ready, provisioning, failed
    """
    if session.get("isTerminated") or session.get("status") == "terminated":
        return "archive"
    if session.get("status") == "merged":
        return "done"
    if session.get("status") == "review_pending" or session.get("kanbanColumn") == "needs_review":
        return "review"
    if session.get("status") == "ci_failed" or session.get("provisionState") == "failed":
        return "blocked"
    display = (session.get("displayStatus") or "").lower()
    if "review" in display:
        return "review"
    if display == "awaiting pr":
        # AO's "Awaiting PR" state: work finished, PR not opened yet.
        return "done"
    if session.get("provisionState") == "provisioning":
        return "queued"
    if session.get("kanbanColumn") == "ready":
        return "done"
    # working / idle / exited (agent process exited but session resumable) all
    # keep the session alive on the board; displayStatus shows the nuance.
    return "running"


def card_for(session: dict, project_names: dict) -> dict:
    """Reduce one AO session to the fields the board card renders."""
    project_id = session.get("projectId") or ""
    prs = []
    for pr in session.get("prs") or []:
        if isinstance(pr, dict) and pr.get("url"):
            prs.append({
                "url": pr.get("url"),
                "number": pr.get("number"),
                "state": pr.get("state"),
                "ci": pr.get("ci"),
            })
    activity = session.get("activity") or {}
    return {
        "id": session.get("id"),
        "displayName": session.get("displayName") or session.get("id"),
        "projectId": project_id,
        "project": project_names.get(project_id, project_id or "standalone"),
        "harness": session.get("harness") or "",
        "kind": session.get("kind") or "",
        "column": column_for(session),
        "status": session.get("status") or "",
        "displayStatus": session.get("displayStatus") or "",
        "branch": session.get("branch") or "",
        "activityState": activity.get("state") or "",
        "createdAt": session.get("createdAt") or "",
        "updatedAt": session.get("updatedAt") or "",
        "prs": prs,
    }


def board_snapshot(ao: Optional[AOClient] = None) -> dict:
    """One round-trip pair (projects + sessions) reduced to everything the UI draws."""
    ao = ao or client()
    projects = ao.projects()
    project_names = {p.get("id"): (p.get("name") or p.get("id")) for p in projects if p.get("id")}
    cards = [card_for(s, project_names) for s in ao.sessions()]
    by_column = {c: [] for c in COLUMN_ORDER}
    for card in cards:
        by_column.setdefault(card["column"], []).append(card)
    for bucket in by_column.values():
        bucket.sort(key=lambda c: c.get("createdAt") or "", reverse=True)
    return {
        "ok": True,
        "baseUrl": ao.base_url,
        "columns": [{"id": c, "label": COLUMN_LABELS[c]} for c in COLUMN_ORDER],
        "harnesses": COMMON_HARNESSES,
        "projects": [
            {"id": p.get("id"), "name": p.get("name") or p.get("id"),
             "path": p.get("path") or "", "folderMissing": bool(p.get("folderMissing"))}
            for p in projects if p.get("id")
        ],
        "cards": cards,
        "counts": {c: len(by_column[c]) for c in by_column},
    }


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------

class SpawnBody(BaseModel):
    project_id: str = Field("", alias="projectId")
    harness: str = Field("hermes")
    display_name: str = Field("", alias="displayName")
    prompt: str = ""
    mode: str = Field("chat")
    branch: str = ""

    model_config = {"populate_by_name": True}


class SendBody(BaseModel):
    message: str
    user_authored: bool = Field(True, alias="userAuthored")

    model_config = {"populate_by_name": True}


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------

def _raise_mapped(err: AOApiError) -> None:
    """Translate an AO error into a dashboard-facing HTTP error."""
    if isinstance(err, AOConnectionError):
        raise HTTPException(status_code=502, detail=str(err))
    if err.is_gone:
        raise HTTPException(status_code=404, detail="Session not found (gone from the AO daemon)")
    if err.status == 409:
        raise HTTPException(status_code=409, detail=str(err.message))
    raise HTTPException(status_code=err.status, detail=str(err.message) or "AO daemon error")


# ---------------------------------------------------------------------------
# Routes (mounted at /api/plugins/ao-bridge/)
# ---------------------------------------------------------------------------

@router.get("/health")
def health():
    ao = client()
    daemon: dict = {}
    try:
        daemon = ao.healthz()
    except AOApiError as err:
        _raise_mapped(err)
    return {"ok": True, "baseUrl": ao.base_url,
            "daemon": {"status": daemon.get("status"), "pid": daemon.get("pid")}}


@router.get("/board")
def board():
    ao = client()
    try:
        return board_snapshot(ao)
    except AOApiError as err:
        _raise_mapped(err)


@router.post("/spawn", status_code=201)
def spawn(body: SpawnBody):
    ao = client(timeout_s=SPAWN_TIMEOUT_S)
    if not body.project_id and not body.display_name:
        raise HTTPException(status_code=422, detail="A project or a display name is required")
    resp: dict = {}
    try:
        resp = ao.spawn(project_id=body.project_id, harness=body.harness,
                        display_name=body.display_name, prompt=body.prompt,
                        mode=body.mode, branch=body.branch)
    except AOApiError as err:
        _raise_mapped(err)
    session = (resp or {}).get("session") or {}
    return {"ok": True, "session": {"id": session.get("id"),
                                    "displayName": session.get("displayName") or body.display_name}}


@router.post("/sessions/{session_id}/send")
def send(session_id: str, body: SendBody):
    ao = client()
    if not body.message.strip():
        raise HTTPException(status_code=422, detail="Message must not be empty")
    try:
        resp = ao.send(session_id, body.message, user_authored=body.user_authored)
    except AOApiError as err:
        _raise_mapped(err)
    return {"ok": True, "sessionId": session_id}


@router.post("/sessions/{session_id}/kill")
def kill(session_id: str):
    ao = client()
    try:
        ao.kill(session_id)
    except AOApiError as err:
        _raise_mapped(err)
    return {"ok": True, "sessionId": session_id}


TRANSCRIPT_TAIL_LINES = 200


@router.get("/sessions/{session_id}/detail")
def detail(session_id: str):
    """Read-only card detail: session metadata + bounded transcript tail."""
    ao = client()
    sess: dict = {}
    conv: dict = {}
    try:
        sess = ao.get_session(session_id)
        conv = ao.conversation(session_id)
    except AOApiError as err:
        _raise_mapped(err)
    messages = conv.get("messages") or []
    if not isinstance(messages, list):
        messages = []
    tail = messages[-TRANSCRIPT_TAIL_LINES:]
    project_id = sess.get("projectId") or ""
    activity = sess.get("activity") or {}
    return {
        "ok": True,
        "session": {
            "id": sess.get("id"),
            "displayName": sess.get("displayName") or sess.get("id"),
            "projectId": project_id,
            "harness": sess.get("harness") or "",
            "status": sess.get("status") or "",
            "displayStatus": sess.get("displayStatus") or "",
            "branch": sess.get("branch") or "",
            "createdAt": sess.get("createdAt") or "",
            "updatedAt": sess.get("updatedAt") or "",
            "activityState": activity.get("state") or "",
            "aoLink": f"ao://sessions/{project_id}/{session_id}" if project_id
                      else f"ao://sessions/{session_id}",
        },
        "transcript": [
            m for m in (
                {"role": m.get("role") or "", "text": m.get("text") or "",
                 "createdAt": m.get("createdAt") or ""}
                for m in tail if isinstance(m, dict)
            ) if m["text"]
        ],
        "transcriptTotal": len(messages),
        "transcriptTruncated": len(messages) > len(tail),
    }


# ---------------------------------------------------------------------------
# Live updates: WS stream (server polls the AO daemon, pushes diffs)
# ---------------------------------------------------------------------------

EVENT_POLL_SECONDS = 1.0


def _ws_upgrade_authorized(ws: WebSocket) -> bool:
    """Authorize the WS upgrade via the dashboard's canonical gate
    (``web_server_chat._ws_auth_ok``: ``?token=`` / ``?ticket=``) so this
    endpoint can never drift from core auth; accepts when the dashboard isn't
    importable (bare-FastAPI test harness)."""
    try:
        from hermes_cli import web_server_chat as _ws
    except Exception:
        return True
    return bool(_ws._ws_auth_ok(ws))


def _cards_digest(cards: list) -> str:
    return hashlib.sha256(
        json.dumps(cards, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


@router.websocket("/events")
async def events(ws: WebSocket):
    """Push board snapshots when AO state changes (server polls at ~1s).

    Sends ``{"type": "board", "board": <board snapshot>}`` only when the card
    set changed, so an idle daemon costs the client nothing.
    """
    if not _ws_upgrade_authorized(ws):
        await ws.close(code=1008)
        return
    await ws.accept()
    last_digest: Optional[str] = None
    try:
        while True:
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=EVENT_POLL_SECONDS)
                if msg.get("type") == "websocket.disconnect":
                    return
            except TimeoutError:
                pass  # no client message — poll the daemon
            try:
                snap = await asyncio.to_thread(board_snapshot, client())
            except AOApiError:
                continue  # daemon down/blip: keep the connection, keep last board
            digest = _cards_digest(snap.get("cards") or [])
            if digest != last_digest:
                last_digest = digest
                await ws.send_json({"type": "board", "board": snap})
    except WebSocketDisconnect:
        return
    except asyncio.CancelledError:
        return
    except Exception as exc:  # never crash the dashboard worker
        log.warning("ao-bridge event stream error: %s", exc)
        try:
            await ws.close()
        except Exception:
            pass
