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
- GET  /api/v1/reports?projectId=<id>          -> {"reports": [...]} (relay, opt-in)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

router = APIRouter()
log = logging.getLogger("ao-bridge")

PLUGIN_ID = "ao-bridge"
DEFAULT_BASE_URL = "http://127.0.0.1:3001"


def _manifest_version() -> str:
    """Version from the manifest NEXT TO THIS FILE, read once at import.

    The dashboard imports this module once at web-server startup and never
    re-imports after a plugin update, while the frontend assets (and the
    manifest itself) are served from disk per request. Reporting the
    import-time version is what lets the frontend detect that skew — a
    freshly served UI driving a stale in-process backend — and tell the user
    to reload instead of misbehaving silently.
    """
    try:
        raw = (Path(__file__).resolve().parent / "manifest.json").read_text(encoding="utf-8")
        return str(json.loads(raw).get("version") or "")
    except (OSError, ValueError):  # a broken manifest must not take the board down
        return ""


PLUGIN_VERSION = _manifest_version()

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

    def __init__(self, message: str = "AO daemon is unreachable", base_url: str = ""):
        super().__init__(502, message)
        self.base_url = base_url


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
            raise AOConnectionError(f"AO daemon unreachable at {self.base_url}: {err}",
                                    base_url=self.base_url) from err
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

    def reports(self, project_id: str) -> list:
        resp = self.request("GET", "/api/v1/reports?" + urllib.parse.urlencode({"projectId": project_id}))
        return resp.get("reports") or []


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


# ---------------------------------------------------------------------------
# Plan steps
#
# Wire truth (verified against the live daemon, 2026-11-09): the conversation
# snapshot carries a top-level ``activities`` array; entries with
# ``activityKind == "plan"`` hold ``detail.steps`` — [{status, text}] with
# status in in_progress|pending|completed|cancelled — plus ``detail.event``
# and ``detail.writtenAt``. The LAST plan activity is the live plan and
# matches the AO desktop app's Plan panel. The sessions list and single-session
# GETs carry no activities and there is no bulk endpoint, so plans ride the
# board snapshot from a TTL cache: at most one conversation round-trip per
# working session per TTL window, never per card per poll.
# ---------------------------------------------------------------------------

PLAN_TTL_SECONDS = 15.0
_plan_cache: dict[str, tuple[float, Optional[dict]]] = {}
_plan_cache_lock = threading.Lock()

PLAN_STEP_STATUSES = ("in_progress", "pending", "completed", "cancelled")


def latest_plan(conv: dict) -> Optional[dict]:
    """Parse the live plan out of a conversation snapshot (or None)."""
    activities = conv.get("activities")
    if not isinstance(activities, list):
        return None
    for act in reversed(activities):
        if not isinstance(act, dict) or act.get("activityKind") != "plan":
            continue
        detail = act.get("detail") or {}
        steps = [
            {"status": st.get("status") if st.get("status") in PLAN_STEP_STATUSES else "pending",
             "text": st.get("text") or ""}
            for st in (detail.get("steps") or [])
            if isinstance(st, dict) and st.get("text")
        ]
        return {"steps": steps, "writtenAt": detail.get("writtenAt") or act.get("createdAt") or ""}
    return None


def _attach_plans(cards: list, ao: AOClient) -> None:
    """Attach ``plan`` to the cards of working sessions, TTL-cached."""
    working = [c for c in cards if c.get("status") == "working"]
    now = time.monotonic()
    for card in working:
        sid = card["id"]
        with _plan_cache_lock:
            hit = _plan_cache.get(sid)
        if hit and hit[0] > now:
            if hit[1]:
                card["plan"] = hit[1]
            continue
        try:
            plan = latest_plan(ao.conversation(sid))
        except AOApiError:
            # daemon blip or the session ended mid-fetch: keep the last known plan
            plan = hit[1] if hit else None
        with _plan_cache_lock:
            _plan_cache[sid] = (now + PLAN_TTL_SECONDS, plan)
        if plan:
            card["plan"] = plan
    with _plan_cache_lock:
        live = {c["id"] for c in working}
        for sid in [k for k in _plan_cache if k not in live]:
            del _plan_cache[sid]


def board_snapshot(ao: Optional[AOClient] = None) -> dict:
    """One round-trip pair (projects + sessions) reduced to everything the UI draws."""
    ao = ao or client()
    projects = ao.projects()
    project_names = {p.get("id"): (p.get("name") or p.get("id")) for p in projects if p.get("id")}
    cards = [card_for(s, project_names) for s in ao.sessions()]
    _attach_plans(cards, ao)
    by_column = {c: [] for c in COLUMN_ORDER}
    for card in cards:
        by_column.setdefault(card["column"], []).append(card)
    for bucket in by_column.values():
        bucket.sort(key=lambda c: c.get("createdAt") or "", reverse=True)
    return {
        "ok": True,
        "pluginVersion": PLUGIN_VERSION,
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

def _error_detail(kind: str, message: str, retryable: bool,
                  upstream_status: Optional[int] = None,
                  base_url: str = "") -> dict:
    """The typed error payload every failure path returns. ``pluginVersion``
    rides along so even an error response lets the frontend run the skew check
    (an erroring stale backend must still be identifiable)."""
    detail: dict = {"kind": kind, "retryable": retryable, "message": message,
                    "pluginVersion": PLUGIN_VERSION}
    if upstream_status is not None:
        detail["upstreamStatus"] = upstream_status
    if base_url:
        detail["baseUrl"] = base_url
    return detail


def _raise_mapped(err: AOApiError) -> None:
    """Translate an AO error into a dashboard-facing HTTP error with a typed
    JSON detail (not prose) so the UI can degrade in theme and retry."""
    if isinstance(err, AOConnectionError):
        raise HTTPException(status_code=502, detail=_error_detail(
            "daemon_unreachable", str(err), retryable=True,
            base_url=getattr(err, "base_url", "")))
    if err.is_gone:
        raise HTTPException(status_code=404, detail=_error_detail(
            "gone", "Session not found (gone from the AO daemon)",
            retryable=False, upstream_status=err.status))
    if err.status == 409:
        raise HTTPException(status_code=409, detail=_error_detail(
            "conflict", str(err.message), retryable=False, upstream_status=err.status))
    raise HTTPException(status_code=err.status, detail=_error_detail(
        "upstream_error", str(err.message) or "AO daemon error",
        retryable=err.is_transient, upstream_status=err.status))


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
    return {"ok": True, "pluginVersion": PLUGIN_VERSION, "baseUrl": ao.base_url,
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
ACTIVITY_TAIL = 12

# Tool-execution activities rendered in the transcript (wire truth probed on
# the live daemon, 2026-11-09: entries with activityKind command | mcp_tool |
# reasoning | plan, sharing the messages' ``sequence`` numbering space, so the
# UI interleaves them by sequence). ``plan`` is rendered by latest_plan() above
# and ``system``/``file_change`` are not part of the transcript contract, so
# both are skipped here.
TOOL_ACTIVITY_KINDS = ("command", "mcp_tool", "reasoning")
ACTIVITY_TAIL_COUNT = 400
TOOL_OUTPUT_MAX_CHARS = 4000
TOOL_INPUT_MAX_CHARS = 1000


def _bounded(text: str, limit: int) -> tuple[str, bool]:
    return (text, False) if len(text) <= limit else (text[:limit], True)


def activity_for(act: dict) -> Optional[dict]:
    """Reduce one conversation activity to what the transcript row renders."""
    kind = act.get("activityKind")
    if kind not in TOOL_ACTIVITY_KINDS:
        return None
    detail = act.get("detail") or {}
    if not isinstance(detail, dict):
        detail = {}
    row: dict = {
        "id": act.get("id") or "",
        "activityKind": kind,
        "status": act.get("status") or "",
        "summary": act.get("summary") or "",
        "sequence": act.get("sequence"),
    }
    if kind == "command":
        output, truncated = _bounded(str(detail.get("output") or ""), TOOL_OUTPUT_MAX_CHARS)
        row["command"] = detail.get("command") or detail.get("rawCommand") or act.get("summary") or ""
        row["exitCode"] = detail.get("exitCode")
        row["output"] = output
        row["outputTruncated"] = truncated
    elif kind == "mcp_tool":
        output, truncated = _bounded(str(detail.get("output") or ""), TOOL_OUTPUT_MAX_CHARS)
        raw_input = detail.get("input")
        if isinstance(raw_input, dict):
            try:
                shown_input = json.dumps(raw_input, ensure_ascii=False, sort_keys=True)
            except (TypeError, ValueError):
                shown_input = str(raw_input)
        else:
            shown_input = str(raw_input or "")
        shown_input, input_truncated = _bounded(shown_input, TOOL_INPUT_MAX_CHARS)
        row["toolName"] = detail.get("providerToolName") or act.get("summary") or "tool"
        row["input"] = shown_input
        row["inputTruncated"] = input_truncated
        row["output"] = output
        row["outputTruncated"] = truncated
    else:  # reasoning
        text, truncated = _bounded(str(detail.get("text") or ""), TOOL_OUTPUT_MAX_CHARS)
        row["text"] = text
        row["textTruncated"] = truncated
    return row


def tool_activities(conv: dict) -> dict:
    """Tool-execution activities of a conversation snapshot, sequence-ordered,
    bounded to the last ACTIVITY_TAIL_COUNT entries."""
    acts = [activity_for(a) for a in (conv.get("activities") or []) if isinstance(a, dict)]
    acts = [a for a in acts if a and a.get("sequence") is not None]
    acts.sort(key=lambda a: a["sequence"])
    total = len(acts)
    return {"activities": acts[-ACTIVITY_TAIL_COUNT:], "activitiesTotal": total}


def ao_deep_link(project_id: str, session_id: str) -> str:
    """Canonical ao:// deep link; each path segment percent-encoded so ids
    containing '/', '?' or '#' cannot reshape the link."""
    sid = urllib.parse.quote(str(session_id), safe="")
    if project_id:
        return f"ao://sessions/{urllib.parse.quote(str(project_id), safe='')}/{sid}"
    return f"ao://sessions/{sid}"


def detail_payload(ao: AOClient, session_id: str) -> dict:
    """Read-only card detail: session metadata + bounded transcript tail +
    tool-execution activities.

    Shared by the REST detail route and the WS watch push so the sheet sees
    exactly one payload shape on both paths. Raises ``AOApiError``; the REST
    route maps it, the WS loop handles it.
    """
    sess = ao.get_session(session_id)
    conv = ao.conversation(session_id)
    messages = conv.get("messages") or []
    if not isinstance(messages, list):
        messages = []
    turns = conv.get("turns") if isinstance(conv.get("turns"), list) else []
    tail = messages[-TRANSCRIPT_TAIL_LINES:]
    project_id = sess.get("projectId") or ""
    activity = sess.get("activity") or {}
    tools = tool_activities(conv)
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
            "aoLink": ao_deep_link(project_id, session_id),
        },
        "transcript": [
            m for m in (
                {"role": m.get("role") or "", "text": m.get("text") or "",
                 "createdAt": m.get("createdAt") or "",
                 # Shared sequence numbering space with activities — the UI
                 # interleaves both by it.
                 "sequence": m.get("sequence"),
                 # Wire truth (probed on the live daemon, 2026-11-09): origin is
                 # the reliable provenance signal — "human" only for sends typed
                 # in the AO app, "automation" for every REST /send call.
                 # authoredByUser is self-declared by the sender (whoever passes
                 # userAuthored: true) and proves nothing about human authorship.
                 "origin": m.get("origin") or "",
                 "authoredByUser": bool(m.get("authoredByUser"))}
                for m in tail if isinstance(m, dict)
            ) if m["text"]
        ],
        "transcriptTotal": len(messages),
        "transcriptTruncated": len(messages) > len(tail),
        "activities": tools["activities"],
        "activitiesTotal": tools["activitiesTotal"],
        "plan": latest_plan(conv),
        # The running turn's state — intra-turn progress the session list
        # never carries (the list row is static for a whole turn).
        "turnState": (turns[0].get("state") or "") if turns else "",
    }


@router.get("/sessions/{session_id}/detail")
def detail(session_id: str):
    """Read-only card detail: session metadata + bounded transcript tail."""
    ao = client()
    try:
        return detail_payload(ao, session_id)
    except AOApiError as err:
        _raise_mapped(err)


# ---------------------------------------------------------------------------
# Live updates: WS stream (server polls the AO daemon, pushes diffs)
# ---------------------------------------------------------------------------

EVENT_POLL_SECONDS = 0.5


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


def _ws_watch_target(msg: dict) -> Optional[str]:
    """Extract a watch target from a client frame, else None.

    ``{"type": "watch", "sessionId": "<id>"}`` asks the stream to also push
    that session's detail payload (transcript, plan, turn state) while it
    changes — the detail sheet's live-follow channel.
    """
    if not isinstance(msg, dict) or msg.get("type") != "watch":
        return None
    sid = str(msg.get("sessionId") or "").strip()
    return sid or None


@router.websocket("/events")
async def events(ws: WebSocket):
    """Push board snapshots when AO state changes (server polls at ~1s).

    Sends ``{"type": "board", "board": <board snapshot>}`` only when the card
    set changed, so an idle daemon costs the client nothing. A client may add
    ``{"type": "watch", "sessionId": id}`` to also receive
    ``{"type": "detail", "sessionId": id, "detail": <detail payload>}`` pushes
    whenever that session's transcript, plan or turn state changes — the card
    detail sheet uses this to follow new messages live.
    """
    if not _ws_upgrade_authorized(ws):
        await ws.close(code=1008)
        return
    await ws.accept()
    last_digest: Optional[str] = None
    upstream_down = False
    watch_sid: Optional[str] = None
    watch_digest: Optional[str] = None
    try:
        while True:
            got_frame = False
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=EVENT_POLL_SECONDS)
                got_frame = True
                if msg.get("type") == "websocket.disconnect":
                    return
            except TimeoutError:
                pass  # no client message — poll the daemon
            if got_frame:
                text = msg.get("text")
                if isinstance(msg.get("bytes"), (bytes, bytearray)):
                    try:
                        text = bytes(msg["bytes"]).decode("utf-8", "replace")
                    except Exception:
                        text = None
                if isinstance(text, str) and text:
                    try:
                        target = _ws_watch_target(json.loads(text))
                    except ValueError:
                        target = None
                    if target and target != watch_sid:
                        watch_sid = target
                        watch_digest = None  # force one push for the new target
            try:
                snap = await asyncio.to_thread(board_snapshot, client())
            except AOApiError as err:
                # Daemon down/blip: keep the connection, keep last board — but
                # TELL the client, once per outage: a live-mode board has no
                # HTTP poll running, so a silent `continue` would leave it
                # showing stale data with a live badge and no banner.
                if not upstream_down:
                    upstream_down = True
                    try:
                        await ws.send_json({"type": "daemon_error", "error": {
                            "kind": ("daemon_unreachable" if isinstance(err, AOConnectionError)
                                     else "upstream_error"),
                            "message": str(err), "retryable": True,
                            "baseUrl": getattr(err, "base_url", ""),
                            "pluginVersion": PLUGIN_VERSION}})
                    except Exception:
                        pass  # client already gone; the outer handlers clean up
                continue
            if upstream_down:
                # Daemon is back: force a board push even when the card set is
                # unchanged across the outage, so clients clear the degraded
                # state and restore live mode.
                upstream_down = False
                last_digest = None
            digest = _cards_digest(snap.get("cards") or [])
            if digest != last_digest:
                last_digest = digest
                await ws.send_json({"type": "board", "board": snap})
            if watch_sid:
                try:
                    payload = await asyncio.to_thread(detail_payload, client(), watch_sid)
                except AOApiError as err:
                    if err.is_gone:
                        watch_sid = None  # session vanished: drop the watch
                    continue
                d_digest = _cards_digest(payload)
                if d_digest != watch_digest:
                    watch_digest = d_digest
                    await ws.send_json({"type": "detail", "sessionId": watch_sid,
                                        "detail": payload})
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


# ---------------------------------------------------------------------------
# AO event relay (opt-in): worker milestones -> the orchestrator's Hermes session
#
# Wesley's use case: worker completions/stalls from AO only arrive on the
# orchestrator's AO-embedded conversation; when he chats with Hermes elsewhere
# they never reach him. The relay watches the daemon on a backend-owned poll
# loop and injects compact one-line milestones into ONE Hermes session chosen
# by explicit config (``relay_session``), through the same sanctioned store
# path async delegation completions use (``SessionDB.append_delegation_delivery``
# — a durable user-role delivery row, folded into the session's next turn as
# context and visible to transcript viewers immediately). No platform sends,
# no secrets, no Telegram/Discord configuration: the target is a session id
# and the transport is entirely Hermes core. The injected row becomes part of
# that conversation — the agent sees it on the next turn and may act on it;
# it does not itself wake an idle session.
#
# Why poll + diff rather than the daemon's event stream: the loopback REST API
# is the surface this plugin is already built on (no credentials), while the
# daemon's stream endpoints are either off-surface (WS on the paired port,
# connection-password gated) or firehose-shaped (SSE /api/v1/events carries
# only {"id"} per event and emits thousands of updates/minute on a busy
# machine). A 5s diff-poll of sessions + per-project reports yields exactly
# the transitions the relay cares about, dedupable by stable ids.
# ---------------------------------------------------------------------------

RELAY_POLL_SECONDS = 5.0
RELAY_DEBOUNCE_SECONDS = 5.0
RELAY_BATCH_MAX_LINES = 8
RELAY_NOTE_LIMIT = 140

RELAY_REPORT_STATES = ("done", "checkpoint", "stuck", "needs_input")
RELAY_SESSION_FROM = "working"
RELAY_SESSION_STATUSES = {"idle": "session_idle", "exited": "session_exited",
                          "terminated": "session_terminated"}
DEFAULT_RELAY_EVENTS = tuple(
    [f"report_{s}" for s in RELAY_REPORT_STATES] + list(RELAY_SESSION_STATUSES.values()))
REPORT_EMOJI = {"done": "✅", "checkpoint": "📍", "stuck": "🚨", "needs_input": "❓"}
SESSION_EMOJI = {"session_idle": "💤", "session_exited": "🚪", "session_terminated": "🗄️"}

# The seen/dedup state must not grow without bound across daemon restarts and
# report churn; entries older than this are re-relayable (a day-old report
# reappearing is news again).
RELAY_SEEN_TTL_SECONDS = 24 * 3600.0


class RelayEvent:
    __slots__ = ("kind", "worker", "detail")

    def __init__(self, kind: str, worker: str, detail: str = ""):
        self.kind, self.worker, self.detail = kind, worker, detail


def _one_line(text: str, limit: int) -> str:
    line = " ".join((text or "").split())
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


def report_events(reports: list, seen: dict, kinds, names: Optional[dict] = None) -> list:
    """Diff AO worker reports against ``seen`` ({(id, state): ts}); new
    (id, state) pairs relay, repeatCount churn on the same state never does.
    ``seen`` is mutated even for dropped kinds so a later filter change cannot
    resurrect old events."""
    now = time.monotonic()
    events = []
    for rpt in reports:
        if not isinstance(rpt, dict):
            continue
        state = rpt.get("state")
        rid = rpt.get("id")
        if state not in RELAY_REPORT_STATES or not rid:
            continue
        key = (rid, state)
        is_new = key not in seen
        seen[key] = now
        if not is_new:
            continue
        kind = f"report_{state}"
        if kind not in kinds:
            continue
        sid = rpt.get("sessionId") or ""
        worker = (names or {}).get(sid) or sid or "?"
        events.append(RelayEvent(kind, worker, str(rpt.get("note") or "")))
    for key in [k for k, ts in seen.items() if now - ts > RELAY_SEEN_TTL_SECONDS]:
        del seen[key]
    return events


def session_events(sessions: list, prev_status: dict, kinds) -> list:
    """Diff AO session status against ``prev_status`` ({id: status}); only
    working -> idle/exited/terminated transitions relay, and the first poll
    records the baseline silently (the caller decides that by pre-seeding
    ``prev_status`` — an empty dict means baseline-on-next-poll here)."""
    events = []
    live_ids = set()
    for sess in sessions:
        if not isinstance(sess, dict):
            continue
        sid = sess.get("id")
        if not sid:
            continue
        live_ids.add(sid)
        status = "terminated" if sess.get("isTerminated") else (sess.get("status") or "")
        previous = prev_status.get(sid)
        prev_status[sid] = status
        kind = RELAY_SESSION_STATUSES.get(status)
        if kind is None or kind not in kinds or previous != RELAY_SESSION_FROM:
            continue
        events.append(RelayEvent(kind, str(sess.get("displayName") or sid), ""))
    for gone in [sid for sid in prev_status if sid not in live_ids]:
        del prev_status[gone]
    return events


def format_relay_event(ev: RelayEvent) -> str:
    if ev.kind.startswith("report_"):
        state = ev.kind[len("report_"):]
        emoji = REPORT_EMOJI.get(state, "•")
        detail = _one_line(ev.detail, RELAY_NOTE_LIMIT)
        label = "needs input" if state == "needs_input" else state
        return f"{emoji} AO {ev.worker} — report {label}: {detail}" if detail \
            else f"{emoji} AO {ev.worker} — report {label}"
    emoji = SESSION_EMOJI.get(ev.kind, "•")
    what = {"session_idle": "is idle", "session_exited": "exited",
            "session_terminated": "was terminated"}[ev.kind]
    return f"{emoji} AO {ev.worker} {what} (was working)"


class RelayBatcher:
    """Collect one-line events; flush as ONE message after a debounce window
    or when the line cap is hit, so a burst of milestones costs one turn."""

    def __init__(self, debounce_s: float = RELAY_DEBOUNCE_SECONDS,
                 max_lines: int = RELAY_BATCH_MAX_LINES):
        self.debounce_s, self.max_lines = debounce_s, max_lines
        self._lines: list = []
        self._first_add: Optional[float] = None

    def add(self, line: str) -> Optional[str]:
        if self._first_add is None:
            self._first_add = time.monotonic()
        self._lines.append(line)
        if len(self._lines) >= self.max_lines:
            return self.flush()
        return None

    def due(self, now: Optional[float] = None) -> bool:
        if not self._lines:
            return False
        elapsed = (time.monotonic() if now is None else now) - (self._first_add or 0.0)
        return elapsed >= self.debounce_s

    def flush(self) -> Optional[str]:
        if not self._lines:
            return None
        text = "\n".join(self._lines)
        self._lines, self._first_add = [], None
        return text

    def restore(self, text: str) -> None:
        """Put a flushed batch back (delivery deferred: a live turn refused the
        write — keep it for the next tick instead of losing the events)."""
        for line in reversed(text.split("\n")):
            self._lines.insert(0, line)
        if self._lines and self._first_add is None:
            self._first_add = time.monotonic()


class RelaySettings:
    __slots__ = ("enabled", "session_id", "events")

    def __init__(self, enabled: bool, session_id: str, events: set):
        self.enabled, self.session_id, self.events = enabled, session_id, events


def read_relay_settings() -> RelaySettings:
    """relay.* plugin settings; defaults are OFF and explicit — the relay
    never runs, and never picks a session, without the user naming one."""
    enabled = _config_raw("relay_enabled")
    events_raw = _config_raw("relay_events")
    if isinstance(events_raw, (list, tuple)):
        parsed = {str(item).strip() for item in events_raw if str(item).strip()}
    else:
        parsed = {part.strip() for part in str(events_raw or "").split(",")} - {""}
    events = parsed & set(DEFAULT_RELAY_EVENTS) if parsed else set(DEFAULT_RELAY_EVENTS)
    return RelaySettings(
        enabled=str(enabled).strip().lower() in ("1", "true", "yes", "on"),
        session_id=str(_config_raw("relay_session") or "").strip(),
        events=events,
    )


def _config_raw(key: str):
    """Raw ``plugins.entries.ao-bridge.settings.<key>`` (bools/lists survive);
    None when unset or unreadable. Best-effort like ``_config_setting``."""
    try:
        from hermes_cli.config import load_config_readonly  # type: ignore
    except Exception:  # pragma: no cover - exercised only outside the dashboard server
        return None
    try:
        config = load_config_readonly() or {}
        return config.get("plugins", {}).get("entries", {}).get(PLUGIN_ID, {}).get("settings", {}).get(key)
    except Exception:  # pragma: no cover - an unreadable config grants no setting
        return None


def _open_session_db():
    """SessionDB for the relay's delivery rows; None outside a Hermes install
    (bare test harnesses) — the relay then fails closed, it never improvises a
    transport. The DB is Hermes's own store in the process's profile home."""
    try:
        from hermes_state import SessionDB  # type: ignore
    except Exception:
        return None
    try:
        return SessionDB()
    except Exception:
        return None


def _delegation_id_for(text: str) -> str:
    """Content-stable dedup key: the same batch delivered twice (a retry after
    a crash between write and state update) lands on the existing row instead
    of duplicating the turn."""
    return "ao-bridge:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def deliver_relay_batch(session_id: str, text: str) -> dict:
    """Inject one batch into the Hermes session as a durable delivery row —
    the same store path an async delegation completion takes. Retryable
    failures (a live turn refuses the inter-turn write) return
    ``{"ok": False, "retryable": True}`` so the worker keeps the batch."""
    if not session_id:
        return {"ok": False, "retryable": False, "error": "no relay_session configured"}
    db = _open_session_db()
    if db is None:
        return {"ok": False, "retryable": False, "error": "Hermes session store unavailable"}
    try:
        resolver = getattr(db, "resolve_resume_session_id", None)
        resolved = callable(resolver) and resolver(session_id)
        row_id = db.append_delegation_delivery(
            resolved or session_id, text,
            {"delegation_id": _delegation_id_for(text),
             "delivery_notice": "ao-bridge relay", "origin": "ao-bridge"},
        )
        return {"ok": True, "rowId": row_id, "sessionId": resolved or session_id,
                "delegationId": _delegation_id_for(text)}
    except Exception as exc:
        return {"ok": False, "retryable": True, "error": str(exc)}


class RelayWorker:
    """Backend-owned poll loop: independent of dashboard browser clients,
    silent through daemon downtime (the next successful tick resumes), and a
    no-op unless the relay is explicitly enabled AND a session is named."""

    def __init__(self, poll_seconds: float = RELAY_POLL_SECONDS,
                 debounce_s: float = RELAY_DEBOUNCE_SECONDS):
        self.poll_seconds, self.debounce_s = poll_seconds, debounce_s
        self.seen_reports: dict = {}
        self.prev_status: dict = {}
        self.baselined = False
        self.batcher = RelayBatcher(debounce_s=debounce_s)
        self.last_error = ""
        self.last_delivery = 0.0
        self.delivered_count = 0
        self.pending_text: Optional[str] = None

    def tick(self, ao, sink) -> bool:
        """One relay tick; False when disabled (the loop then just sleeps)."""
        settings = read_relay_settings()
        if not settings.enabled or not settings.session_id:
            return False
        self.poll_once(ao, sink, kinds=settings.events,
                       session_id=settings.session_id)
        return True

    def poll_once(self, ao, sink, *, kinds=None, session_id="") -> None:
        kinds = kinds or set(DEFAULT_RELAY_EVENTS)
        try:
            projects = ao.projects()
            names = {}
            sessions = ao.sessions()
            for sess in sessions:
                if isinstance(sess, dict) and sess.get("id"):
                    names[sess["id"]] = str(sess.get("displayName") or sess["id"])
            reports = []
            for project in projects:
                pid = project.get("id") if isinstance(project, dict) else None
                if pid:
                    reports.extend(ao.reports(pid))
            self.last_error = ""
        except Exception as exc:  # daemon down: silent, next tick retries
            self.last_error = str(exc)
            return
        events = report_events(reports, self.seen_reports, kinds, names)
        events += session_events(sessions, self.prev_status, kinds)
        if not self.baselined:
            # First successful poll records the daemon's current state only —
            # enabling the relay must not flood the conversation with history.
            self.baselined = True
            events = []
        for ev in events:
            self.batcher.add(format_relay_event(ev))
        self._flush(sink, session_id)

    def _flush(self, sink, session_id: str) -> None:
        text = self.batcher.flush() if self.batcher.due() else None
        if not text or not session_id:
            if text:  # nothing to deliver into yet; keep the batch
                self.batcher.restore(text)
            return
        result = deliver_relay_batch(session_id, text)
        if result["ok"]:
            self.last_delivery = time.monotonic()
            self.delivered_count += 1
            self.last_error = ""
            sink(text)
        elif not result.get("retryable"):
            # Permanent (store unavailable): drop the batch — retrying forever
            # would only spin. The /relay status endpoint reports the cause.
            self.last_error = result.get("error") or "delivery failed"
        else:
            # A live turn refused the write: keep the batch for the next tick.
            self.batcher.restore(text)
            self.last_error = result.get("error") or "delivery deferred"


_relay_worker: Optional[RelayWorker] = None
_relay_lock = threading.Lock()
_relay_thread: Optional[threading.Thread] = None


def _relay_sink(_text: str) -> None:
    """Delivery is complete at the store row; nothing further to log at INFO."""


def _relay_loop() -> None:
    worker = _relay_worker
    while True:
        disabled = False
        try:
            disabled = not worker.tick(client(), _relay_sink)
        except Exception as exc:  # never let the loop die on one bad tick
            log.warning("ao-bridge relay tick failed: %s", exc)
        # Disabled relays idle at half cadence; the daemon being down is just
        # another failed tick — the loop resumes silently when it is back.
        time.sleep(worker.poll_seconds * (2 if disabled else 1))


def ensure_relay_started() -> RelayWorker:
    """Start the relay loop once per process; cheap to call from any route."""
    global _relay_worker, _relay_thread
    with _relay_lock:
        if _relay_worker is None:
            _relay_worker = RelayWorker()
        if _relay_thread is None or not _relay_thread.is_alive():
            _relay_thread = threading.Thread(target=_relay_loop, name="ao-bridge-relay",
                                             daemon=True)
            _relay_thread.start()
        return _relay_worker


@router.get("/relay")
def relay_status():
    """Relay diagnostics for the dashboard / operators: what it watches, where
    it delivers, and whether the last tick was healthy."""
    worker = ensure_relay_started()
    settings = read_relay_settings()
    return {
        "ok": True,
        "enabled": settings.enabled,
        "session": settings.session_id,
        "events": sorted(settings.events),
        "watching": worker.baselined,
        "delivered": worker.delivered_count,
        "lastError": worker.last_error,
    }


@router.get("/relay/sessions")
def relay_session_suggestions():
    """Recent Hermes sessions of this profile, for picking ``relay_session``.
    The choice itself stays explicit config — the relay never auto-targets."""
    db = _open_session_db()
    if db is None:
        return {"ok": False, "sessions": [], "error": "Hermes session store unavailable"}
    try:
        rows = db.list_sessions_rich(limit=8, order_by_last_active=True, compact_rows=True)
    except Exception as exc:
        return {"ok": False, "sessions": [], "error": str(exc)}
    return {"ok": True, "sessions": [
        {"id": row.get("id"), "title": row.get("title") or "",
         "lastActive": str(row.get("last_active") or row.get("lastActivity") or "")}
        for row in rows if isinstance(row, dict) and row.get("id")
    ]}

