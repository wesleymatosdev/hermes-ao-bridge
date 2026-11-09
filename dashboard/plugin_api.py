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
- POST /api/v1/sessions/{id}/conversation/steer-or-send
                                               {"text", "clientMessageId"} -> 202
                                               {"outcome": "steered"|"sent", ...};
                                               body field is "text" (not "message"),
                                               the idempotency handle is REQUIRED
                                               (missing -> 409 CHAT_STEER_UNCERTAIN),
                                               messages land with origin=human
- POST /api/v1/sessions/{id}/kill
- GET  /api/v1/reports?projectId=<id>          -> {"reports": [...]} (relay, opt-in)
"""

from __future__ import annotations

import asyncio
import calendar
import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

router = APIRouter()
log = logging.getLogger("ao-bridge")

PLUGIN_ID = "ao-bridge"
DEFAULT_BASE_URL = "http://127.0.0.1:3001"

# URL scheme of the "open this session in the AO desktop app" deep links.
# Verified against the installed AO app's Info.plist (CFBundleURLTypes,
# 0.14.11-nightly): it registers ONLY "ao-app" (declared as the orchestrator
# authentication callback), so plain "ao://" links never reach the app. If a
# future build registers "ao://" or handles session paths differently, this
# constant is the one place to change.
AO_DEEP_LINK_SCHEME = "ao-app"


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

def _load_hermes_config() -> dict:
    """Best-effort config.yaml read. Runs inside the dashboard server, so
    ``hermes_cli`` is importable there; in bare test harnesses it may not be,
    and any failure just means "empty config"."""
    try:
        from hermes_cli.config import load_config_readonly  # type: ignore
    except Exception:  # pragma: no cover - exercised only outside the dashboard server
        return {}
    try:
        return load_config_readonly() or {}
    except Exception:  # pragma: no cover - an unreadable config grants no setting
        return {}


def _config_setting(key: str) -> Optional[str]:
    """Best-effort read of ``plugins.entries.ao-bridge.settings.<key>`` from config.yaml."""
    try:
        config = _load_hermes_config()
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


_TRUTHY = ("1", "true", "yes", "on")


def open_in_ao_links() -> bool:
    """Show the "Open in AO" deep-link buttons?

    OFF by default, deliberately: AO's open-url handler does not route session
    deep links yet (verified at AO source, 2026-11-09 — it only knows the
    orchestrator auth callback and billing hosts; session URLs are dropped), so
    the buttons would focus the app without navigating to the session. The
    ``ao-app://`` builder stays in the code; re-enabling is this config flip,
    not a code dive, once upstream AO registers ``ao://`` or handles session
    paths on ``ao-app://``.
    """
    from_config = _config_setting("open_in_ao_links")
    if from_config:
        return from_config.lower() in _TRUTHY
    # Env override mirrors AO_BRIDGE_BASE_URL — dev/test parity with the
    # dashboard's config-backed setting.
    env = (os.environ.get("AO_BRIDGE_OPEN_LINKS") or "").strip().lower()
    if env:
        return env in _TRUTHY
    return False


# ---------------------------------------------------------------------------
# AO REST client (stdlib urllib; one short-lived client per request)
# ---------------------------------------------------------------------------

class AOApiError(Exception):
    """AO daemon answered with a non-2xx status."""

    def __init__(self, status: int, message: str, code: str = ""):
        super().__init__(message)
        self.status = status
        self.message = message
        # The daemon's machine-readable error code ("CHAT_STEER_TEXT_REQUIRED",
        # ...) from {"error", "code", "message"} bodies; "" when absent.
        self.code = code

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
                code = str(detail.get("code") or "") if isinstance(detail, dict) else ""
            except (ValueError, AttributeError):
                message = err.reason
                code = ""
            raise AOApiError(err.code, str(message), code=code) from err
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

    def steer_or_send(self, session_id: str, text: str, client_message_id: str) -> dict:
        """AO-native steer-or-send: steers a session whose turn is in flight,
        queues a new turn on an idle one (verified live, 2026-11-09). Wire
        quirks that are easy to get wrong: the body field is ``text`` (not
        ``message``), and the daemon REQUIRES the ``clientMessageId``
        idempotency handle — without it every request answers 409
        CHAT_STEER_UNCERTAIN. Messages land with origin=human, so they render
        as "End user" in the transcript."""
        sid = urllib.parse.quote(session_id, safe="")
        return self.request("POST", f"/api/v1/sessions/{sid}/conversation/steer-or-send",
                            {"text": text, "clientMessageId": client_message_id})

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
    """One round-trip pair (projects + sessions) reduced to everything the UI draws.

    Orchestrator sessions (``kind == "orchestrator"`` — they coordinate workers,
    they don't carry worker state) are split into ``orchestrators`` so the
    worker columns and their counters stay meaningful."""
    ao = ao or client()
    projects = ao.projects()
    project_names = {p.get("id"): (p.get("name") or p.get("id")) for p in projects if p.get("id")}
    all_cards = [card_for(s, project_names) for s in ao.sessions()]
    _attach_plans(all_cards, ao)
    cards = [c for c in all_cards if (c.get("kind") or "worker") != "orchestrator"]
    orchestrators = [c for c in all_cards if c.get("kind") == "orchestrator"]
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
        "orchestrators": orchestrators,
        "openInAoLinks": open_in_ao_links(),
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
                  base_url: str = "", code: str = "") -> dict:
    """The typed error payload every failure path returns. ``pluginVersion``
    rides along so even an error response lets the frontend run the skew check
    (an erroring stale backend must still be identifiable)."""
    detail: dict = {"kind": kind, "retryable": retryable, "message": message,
                    "pluginVersion": PLUGIN_VERSION}
    if upstream_status is not None:
        detail["upstreamStatus"] = upstream_status
    if base_url:
        detail["baseUrl"] = base_url
    if code:
        detail["code"] = code
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


# Steer refusal codes from the daemon's writeSteerError mapping (AO backend
# source, 2026-11-09) that deserve board-facing wording or a retryability
# verdict. ``None`` message = carry the daemon's own prose through (it names
# the concrete cause, e.g. which kind of turn is in the way). Codes outside
# this table fall through to the generic upstream mapping with the code attached.
STEER_ERROR_HINTS = {
    "CHAT_STEER_TEXT_REQUIRED": ("validation", "Message must not be empty", False),
    "CHAT_STEER_UNSUPPORTED": (
        "conflict",
        "This session's harness can't take guidance mid-turn — send it after the "
        "turn ends instead",
        False),
    "CHAT_TURN_NOT_STEERABLE": ("conflict", None, True),
    "CHAT_STEER_UNCERTAIN": (
        "conflict",
        "The daemon could not confirm delivery — check the transcript before resending",
        False),
    "CHAT_STEER_IDEMPOTENCY_CONFLICT": ("conflict", None, False),
    "CHAT_UNSUPPORTED_STEER_CONTENT": ("validation", None, False),
}


def _raise_steer_error(err: AOApiError) -> None:
    """Steer-route error mapping: the daemon's CHAT_* code rides in the typed
    payload (so the UI and tests can key on it), connection/gone cases keep
    the shared mapping."""
    if isinstance(err, AOConnectionError) or err.is_gone:
        _raise_mapped(err)
    kind, message, retryable = STEER_ERROR_HINTS.get(
        err.code or "", ("upstream_error", None, err.is_transient))
    raise HTTPException(status_code=err.status, detail=_error_detail(
        kind, message or err.message, retryable=retryable,
        upstream_status=err.status, code=err.code))


# ---------------------------------------------------------------------------
# Routes (mounted at /api/plugins/ao-bridge/)
# ---------------------------------------------------------------------------

@router.get("/health")
def health():
    ensure_relay_started()  # the relay loop arms on first backend traffic
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
    ensure_relay_started()  # the dashboard polls this constantly; arm the relay here
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


@router.post("/sessions/{session_id}/steer")
def steer(session_id: str, body: SendBody):
    """Steer-or-send — the AO-native message delivery (NOT the queue-only
    /send): a session whose turn is in flight gets steered mid-turn, an idle
    one gets a new turn. The idempotency handle is minted here per request;
    the daemon rejects a duplicate handle, so a client retry with the same
    text mints a new handle and is a deliberate second delivery."""
    ao = client()
    if not body.message.strip():
        raise HTTPException(status_code=422, detail="Message must not be empty")
    try:
        resp = ao.steer_or_send(session_id, body.message,
                                client_message_id=uuid.uuid4().hex)
    except AOApiError as err:
        _raise_steer_error(err)
    return {"ok": True, "sessionId": session_id,
            "outcome": (resp or {}).get("outcome") or "sent"}


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

# Steered guidance rides the conversation as a ``system`` activity, not a
# message row (wire truth, live steer probe 2026-11-09: detail.event ==
# "steer", detail.text is the steered text, detail.origin its provenance).
# Without surfacing it, a steered message never shows in the transcript —
# the sender wouldn't see their own text after the daemon confirmed delivery.
STEER_ACTIVITY_EVENT = "steer"


def _bounded(text: str, limit: int) -> tuple[str, bool]:
    return (text, False) if len(text) <= limit else (text[:limit], True)


THINKING_PREVIEW_CHARS = 80


def _thinking_preview(text: str) -> str:
    """One-line collapsed-row preview of a reasoning entry: whitespace
    (incl. newlines) collapsed, bounded to THINKING_PREVIEW_CHARS."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= THINKING_PREVIEW_CHARS:
        return collapsed
    return collapsed[:THINKING_PREVIEW_CHARS]


def activity_for(act: dict) -> Optional[dict]:
    """Reduce one conversation activity to what the transcript row renders."""
    kind = act.get("activityKind")
    if kind == "system":
        detail = act.get("detail")
        if not isinstance(detail, dict) or detail.get("event") != STEER_ACTIVITY_EVENT:
            return None  # non-steer system events are not transcript surface
        text, truncated = _bounded(
            str(detail.get("text") or act.get("summary") or ""), TOOL_OUTPUT_MAX_CHARS)
        return {"id": act.get("id") or "",
                "activityKind": "steer",
                "status": act.get("status") or "",
                "summary": act.get("summary") or "",
                "sequence": act.get("sequence"),
                "text": text,
                "textTruncated": truncated,
                "origin": detail.get("origin") or ""}
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
        row["preview"] = _thinking_preview(text)
    return row


def tool_activities(conv: dict) -> dict:
    """Tool-execution activities of a conversation snapshot, sequence-ordered,
    bounded to the last ACTIVITY_TAIL_COUNT entries.

    Reasoning liveness (wire truth probed on the live daemon, 2026-11-09):
    while a turn is streaming, EVERY reasoning entry of that turn reports
    status "running" and only the highest-sequence one is still being written
    (its ``revision`` bumps) — earlier ones go stale and never flip back
    until the turn ends. So only the last reasoning entry may carry
    ``live: true``; the UI pulses just that one.
    """
    acts = [activity_for(a) for a in (conv.get("activities") or []) if isinstance(a, dict)]
    acts = [a for a in acts if a and a.get("sequence") is not None]
    acts.sort(key=lambda a: a["sequence"])
    for act in reversed(acts):
        if act.get("activityKind") == "reasoning":
            if act.get("status") == "running":
                act["live"] = True
            break
    total = len(acts)
    return {"activities": acts[-ACTIVITY_TAIL_COUNT:], "activitiesTotal": total}


def ao_deep_link(project_id: str, session_id: str) -> str:
    """Canonical AO desktop-app deep link (scheme: ``AO_DEEP_LINK_SCHEME``);
    each path segment percent-encoded so ids containing '/', '?' or '#' cannot
    reshape the link."""
    sid = urllib.parse.quote(str(session_id), safe="")
    if project_id:
        return (f"{AO_DEEP_LINK_SCHEME}://sessions/"
                f"{urllib.parse.quote(str(project_id), safe='')}/{sid}")
    return f"{AO_DEEP_LINK_SCHEME}://sessions/{sid}"


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
    payload = {
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
    attach_hermes_enrichment(payload, sess)
    return payload


@router.get("/sessions/{session_id}/detail")
def detail(session_id: str):
    """Read-only card detail: session metadata + bounded transcript tail."""
    ao = client()
    try:
        return detail_payload(ao, session_id)
    except AOApiError as err:
        _raise_mapped(err)


# ---------------------------------------------------------------------------
# Hermes session-store enrichment (harness == "hermes" only)
#
# Parity audit (2026-11-09, sessions agent-orchestrator-172 + hermes-agent-31):
# ACP delivers AO a lossy view of a hermes worker — command outputs capped at
# a few hundred chars, oversized tool results degraded to the persisted-output
# notice (hermes-agent-31: 116k chars of tool activity in AO vs 442k in the
# Hermes store), and no session stats. Reasoning is already at parity.
#
# The backend runs inside the gateway process, so for harness=="hermes"
# sessions it can re-read what Hermes actually stored — read-only, straight
# sqlite against state.db (no Hermes imports beyond a path helper; the plugin
# and the gateway share the machine in a normal install — that assumption is
# the point, and a missing DB degrades to {"found": false}).
#
# The AO↔Hermes join (measured 52/60 recent sessions; the misses are sessions
# that never produced a Hermes row): AO launches ``hermes acp`` with cwd
# ``<daemon data root>/worktrees/<projectId>/<sessionId>`` (standalone:
# ``…/worktrees/standalone/sessions/<sessionId>``), so the cwd suffix — not a
# guessed timestamp correlation — is the key, narrowed by
# started_at >= AO createdAt and most-recent-wins for resumed sessions.
# ---------------------------------------------------------------------------

HERMES_ACTIVITY_TAIL = 400
HERMES_SLACK_SECONDS = 5.0


def hermes_db_path() -> Path:
    """The profile's state.db. ``AO_BRIDGE_STATE_DB`` overrides (tests,
    non-standard installs); otherwise the Hermes home resolver, falling back
    to the stock home when Hermes isn't importable (bare test harnesses)."""
    env = (os.environ.get("AO_BRIDGE_STATE_DB") or "").strip()
    if env:
        return Path(env)
    try:
        from hermes_constants import get_hermes_home  # type: ignore
        return Path(get_hermes_home()) / "state.db"
    except Exception:
        return Path.home() / ".hermes" / "state.db"


def open_hermes_db():
    """Read-only sqlite handle on state.db, or None when it's missing or
    unopenable — enrichment is additive, never load-bearing."""
    path = hermes_db_path()
    if not path.is_file():
        return None
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        return con
    except sqlite3.Error:
        return None


def _epoch_from_iso(value: str) -> float:
    if not value:
        return 0.0
    try:
        return calendar.timegm(time.strptime(value[:19], "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, OverflowError):
        return 0.0


def _iso_from_epoch(ts) -> str:
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(ts)))
    except (TypeError, ValueError, OverflowError):
        return ""


def hermes_cwd_tail(project_id: str, session_id: str) -> str:
    """The worktree-cwd suffix AO provisions for this session (audit: the
    layout above). Empty project = standalone spawn."""
    session_id = str(session_id or "")
    if not session_id:
        return ""
    if project_id:
        return f"/worktrees/{project_id}/{session_id}"
    return f"/worktrees/standalone/sessions/{session_id}"


def find_hermes_session(con, project_id: str, session_id: str,
                        created_epoch: float) -> Optional[dict]:
    """The Hermes session row behind an AO session: exact cwd-suffix match,
    started at/after the AO session's creation (``%``/``_`` in ids are
    escaped — LIKE metacharacters must not widen the match), most recent
    wins (AO resumes mint a fresh Hermes row per launch). None = no join."""
    tail = hermes_cwd_tail(project_id, session_id)
    if not tail or con is None:
        return None
    pattern = "%" + tail.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    row = con.execute(
        "SELECT id, cwd, started_at, ended_at, message_count, tool_call_count,"
        " model, input_tokens, output_tokens, reasoning_tokens,"
        " estimated_cost_usd, title"
        " FROM sessions WHERE cwd LIKE ? ESCAPE '\\' AND started_at >= ?"
        " ORDER BY started_at DESC LIMIT 1",
        (pattern, created_epoch - HERMES_SLACK_SECONDS)).fetchone()
    return dict(row) if row else None


def _parse_tool_calls(raw) -> list:
    if not raw:
        return []
    try:
        calls = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return []
    if isinstance(calls, list):
        return [c for c in calls if isinstance(c, dict)]
    return []


def hermes_enrichment(con, session_row: dict,
                      tail: int = HERMES_ACTIVITY_TAIL) -> dict:
    """Full-fidelity activities + session stats for one joined Hermes row.

    Tool rows carry the exact ``tool_name`` and full output ACP truncates;
    an assistant row's ``tool_calls`` args are joined to its result row by
    ``tool_call_id``. Reasoning rides the assistant rows. User and plain
    assistant text are left out — the AO transcript already carries them.
    Bounded to the last ``tail`` items."""
    rows = con.execute(
        "SELECT role, content, tool_calls, tool_call_id, tool_name, timestamp,"
        " reasoning, reasoning_content FROM messages"
        " WHERE session_id = ? AND active = 1 ORDER BY timestamp, id",
        (session_row["id"],)).fetchall()
    results = {r["tool_call_id"]: dict(r) for r in rows
               if r["role"] == "tool" and r["tool_call_id"]}
    items: list = []
    for r in rows:
        at = _iso_from_epoch(r["timestamp"])
        if r["role"] == "assistant":
            text = r["reasoning"] or r["reasoning_content"] or ""
            if text.strip():
                shown, truncated = _bounded(str(text), TOOL_OUTPUT_MAX_CHARS)
                items.append({"kind": "reasoning", "text": shown,
                              "textTruncated": truncated,
                              "preview": _thinking_preview(shown), "at": at})
            for call in _parse_tool_calls(r["tool_calls"]):
                result = results.get(str(call.get("id") or "")) or {}
                output = str(result.get("content") or "")
                output, out_trunc = _bounded(output, TOOL_OUTPUT_MAX_CHARS)
                args = call.get("arguments")
                if isinstance(args, dict):
                    try:
                        args = json.dumps(args, ensure_ascii=False, sort_keys=True)
                    except (TypeError, ValueError):
                        args = str(args)
                args = str(args or "")
                args, in_trunc = _bounded(args, TOOL_INPUT_MAX_CHARS)
                items.append({"kind": "tool",
                              "toolName": result.get("tool_name")
                                          or str(call.get("name") or "tool"),
                              "input": args, "inputTruncated": in_trunc,
                              "output": output, "outputTruncated": out_trunc,
                              "at": at})
    total = len(items)
    started = session_row.get("started_at")
    ended = session_row.get("ended_at")
    return {
        "session": {
            "model": session_row.get("model") or "",
            "title": session_row.get("title") or "",
            "startedAt": _iso_from_epoch(started),
            "endedAt": _iso_from_epoch(ended),
            "durationSeconds": (round(ended - started) if started and ended else None),
            "messageCount": session_row.get("message_count"),
            "toolCallCount": session_row.get("tool_call_count"),
            "inputTokens": session_row.get("input_tokens"),
            "outputTokens": session_row.get("output_tokens"),
            "reasoningTokens": session_row.get("reasoning_tokens"),
            "estimatedCostUsd": session_row.get("estimated_cost_usd"),
        },
        "activities": items[-tail:],
        "activitiesTotal": total,
        "activitiesTruncated": total > tail,
    }


def attach_hermes_enrichment(payload: dict, ao_session: dict) -> None:
    """Add ``hermesEnriched`` to a detail payload. Any failure (no DB, no
    join, unreadable row) degrades to ``{"found": false}`` — the AO-side
    payload is already complete at that point, so enrichment can never break
    the sheet."""
    enriched: dict = {"found": False}
    try:
        if (ao_session.get("harness") or "") == "hermes":
            con = open_hermes_db()
            if con is not None:
                try:
                    row = find_hermes_session(
                        con, str(ao_session.get("projectId") or ""),
                        str(ao_session.get("id") or ""),
                        _epoch_from_iso(str(ao_session.get("createdAt") or "")))
                    if row is not None:
                        merged = hermes_enrichment(con, row)
                        enriched = {"found": True, "sessionId": row["id"], **merged}
                finally:
                    con.close()
    except Exception as exc:  # a broken enrichment must not break the sheet
        log.warning("ao-bridge hermes enrichment failed: %s", exc)
    payload["hermesEnriched"] = enriched


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
# loop and delivers compact one-line milestones as LIVE turns into the
# destination session's conversation — resolved PER EVENT from AO's own
# orchestrator mapping (the project's live kind=orchestrator session, whose
# Hermes conversation is identified by its cwd marker
# ``/worktrees/<project>/orchestrator/``), so different workers can belong to
# different orchestrators. Delivery rides the gateway's own api_server
# session-chat route (``POST /api/sessions/{id}/chat`` — the sanctioned
# immediate-submission path ``hermes peer dm`` drives), with a two-tier route:
# api_server (generic — the user pastes their gateway's API_SERVER_KEY into
# ``relay_api_key``; the URL auto-detects from config.yaml) is preferred, and
# the hermes-peer credential path (``relay_peer`` — a registered bot peer's
# scoped key, machine-specific) is the fallback/enhancement. ``relay.delivery``
# picks: auto (default) | api_server | peer. The event WAKES the destination
# session immediately (a live turn — costs the session a model turn;
# batching/debounce keeps it one turn per burst), and the conversation's
# bound surfaces render the exchange. ``relay_session`` is an explicit
# override for unmappable cases only.
#
# Why poll + diff rather than the daemon's event stream: the loopback REST API
# is the surface this plugin is already built on, while the daemon's stream
# endpoints are either off-surface (WS on the paired port, connection-password
# gated) or firehose-shaped (SSE /api/v1/events carries only {"id"} per event
# and emits thousands of updates/minute on a busy machine). A 5s diff-poll of
# sessions + per-project reports yields exactly the transitions the relay
# cares about, dedupable by stable ids.
# ---------------------------------------------------------------------------

RELAY_POLL_SECONDS = 5.0
RELAY_DEBOUNCE_SECONDS = 5.0
RELAY_BATCH_MAX_LINES = 8
RELAY_NOTE_LIMIT = 140
# The gateway's api_server listener — the surface ``hermes peer dm`` drives
# with POST /api/sessions/{id}/chat. The relay submits its batches there so an
# event becomes a live turn in the destination session (wakes it immediately)
# instead of a store row that only lands at the session's next turn. The URL
# auto-detects from the local Hermes config.yaml's api_server address; this
# stock default covers a gateway on its stock bind.
RELAY_API_URL_DEFAULT = "http://127.0.0.1:8642"
# One synchronous agent turn can legitimately take minutes — the same ceiling
# ``hermes peer dm`` uses.
RELAY_CHAT_TIMEOUT_S = 600

# Delivery routes. ``api_server`` is the generic one any Hermes user can set
# up (gateway api_server + its API_SERVER_KEY, pasted into the plugin config);
# ``peer`` is the hermes-peer credential path (a registered bot peer's scoped
# key — machine-specific setup). ``auto`` prefers api_server, falls back to
# peer, and reports "not configured" honestly when neither is available.
RELAY_DELIVERY_MODES = ("auto", "api_server", "peer")

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
    __slots__ = ("kind", "worker", "detail", "ao_session_id", "project_id")

    def __init__(self, kind: str, worker: str, detail: str = "",
                 ao_session_id: str = "", project_id: str = ""):
        self.kind, self.worker, self.detail = kind, worker, detail
        self.ao_session_id, self.project_id = ao_session_id, project_id


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
        events.append(RelayEvent(kind, worker, str(rpt.get("note") or ""),
                                 ao_session_id=sid, project_id=str(rpt.get("projectId") or "")))
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
        events.append(RelayEvent(kind, str(sess.get("displayName") or sid), "",
                                 ao_session_id=sid, project_id=str(sess.get("projectId") or "")))
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
    __slots__ = ("enabled", "override_session", "events", "delivery", "api_url",
                 "api_key", "peer")

    def __init__(self, enabled: bool, override_session: str, events: set,
                 delivery: str, api_url: str, api_key: str, peer: str):
        self.enabled, self.override_session, self.events = enabled, override_session, events
        self.delivery, self.api_url, self.api_key, self.peer = delivery, api_url, api_key, peer


def _normalized_api_host(host: str) -> str:
    """A host the relay can dial: wildcard binds mean loopback for a plugin
    that (by contract) only ever talks to the gateway on the same machine."""
    host = (host or "").strip()
    return "127.0.0.1" if host in ("", "0.0.0.0", "::", "*") else host


def detect_api_url() -> str:
    """The gateway api_server base URL from Hermes config.yaml — the ADDRESS
    only, never any credential. Reads the same places the gateway itself does
    (``platforms.api_server`` / ``gateway.api_server`` / ``gateway.platforms.api_server``;
    see gateway/config_loader.py merge_platform_sections). "" when config.yaml
    declares no api_server address — the caller then falls back to the stock
    bind. Auto-detection is best-effort: a wrong address just fails delivery
    and shows on /relay, it never breaks the board."""
    config = _load_hermes_config()
    if not isinstance(config, dict):
        return ""
    gateway = config.get("gateway") if isinstance(config.get("gateway"), dict) else {}
    for block in (config.get("platforms", {}).get("api_server") if isinstance(config.get("platforms"), dict) else None,
                  gateway.get("api_server"),
                  gateway.get("platforms", {}).get("api_server") if isinstance(gateway.get("platforms"), dict) else None):
        if not isinstance(block, dict):
            continue
        port = str(block.get("port") or "").strip()
        if not port and not str(block.get("host") or "").strip():
            continue
        host = _normalized_api_host(str(block.get("host") or ""))
        return f"http://{host}:{port or '8642'}"
    return ""


def read_relay_settings() -> RelaySettings:
    """relay.* plugin settings; defaults are OFF, and the destination is
    resolved per event from AO's own orchestrator mapping. ``relay_session``
    is only an explicit override for unmappable cases. ``relay_api_url``
    auto-detects from config.yaml when unset."""
    enabled = _config_raw("relay_enabled")
    events_raw = _config_raw("relay_events")
    if isinstance(events_raw, (list, tuple)):
        parsed = {str(item).strip() for item in events_raw if str(item).strip()}
    else:
        parsed = {part.strip() for part in str(events_raw or "").split(",")} - {""}
    events = parsed & set(DEFAULT_RELAY_EVENTS) if parsed else set(DEFAULT_RELAY_EVENTS)
    delivery = str(_config_raw("relay_delivery") or "auto").strip().lower()
    if delivery not in RELAY_DELIVERY_MODES:
        delivery = "auto"  # a typoed mode must not silently change the meaning
    api_url = (str(_config_raw("relay_api_url") or "").strip().rstrip("/")
               or detect_api_url()
               or RELAY_API_URL_DEFAULT)
    return RelaySettings(
        enabled=str(enabled).strip().lower() in ("1", "true", "yes", "on"),
        override_session=str(_config_raw("relay_session") or "").strip(),
        events=events,
        delivery=delivery,
        api_url=api_url,
        api_key=str(_config_raw("relay_api_key") or "").strip(),
        peer=str(_config_raw("relay_peer") or "").strip(),
    )


def _config_raw(key: str):
    """Raw ``plugins.entries.ao-bridge.settings.<key>`` (bools/lists survive);
    None when unset. Best-effort like ``_config_setting``."""
    try:
        return _load_hermes_config().get("plugins", {}).get("entries", {}).get(
            PLUGIN_ID, {}).get("settings", {}).get(key)
    except Exception:  # pragma: no cover - an unreadable config grants no setting
        return None


def _open_session_db():
    """SessionDB (read use) for resolving the destination session; None
    outside a Hermes install (bare test harnesses) — the relay then fails
    closed on unmappable events, it never guesses a target."""
    try:
        from hermes_state import SessionDB  # type: ignore
    except Exception:
        return None
    try:
        return SessionDB()
    except Exception:
        return None


def _relay_secret(name: str) -> str:
    """One Hermes secret by name, through the same scoped read ``hermes peer``
    uses (secret store first, raw env fallback). Empty when unavailable —
    never a plugin-config value, because there are none."""
    try:
        from agent.secret_scope import get_secret  # type: ignore
        return (get_secret(name, "") or "").strip()
    except Exception:
        return (os.environ.get(name) or "").strip()


def peer_key_for(peer: str) -> str:
    """Credential for the peer route: the named peer's scoped key
    (``HERMES_PEER_<NAME>_KEY``, exactly what ``hermes peer`` resolves),
    falling back to the gateway's own API_SERVER_KEY. "" when nothing resolves."""
    if not peer:
        return ""
    env_name = f"HERMES_PEER_{peer.upper().replace('-', '_')}_KEY"
    return _relay_secret(env_name) or _relay_secret("API_SERVER_KEY")


def select_delivery_route(settings: RelaySettings) -> tuple:
    """Which route delivers, and with which credential: ``(route, key, reason)``.

    ``route`` is ``"api_server"``, ``"peer"`` or ``""`` (not configured). The
    api_server route is the GENERIC one — works for anyone who enabled the
    gateway's api_server platform — so ``auto`` prefers it; ``peer`` needs a
    registered bot peer (machine-specific setup). An explicitly picked route
    that is unavailable stays unavailable (honest), it never silently falls
    back to the other one."""
    if settings.delivery == "api_server" or (settings.delivery == "auto" and settings.api_key):
        if settings.api_key:
            return "api_server", settings.api_key, ""
        return "", "", ("relay.delivery=api_server but relay_api_key is empty — "
                        "paste your gateway's API_SERVER_KEY into the plugin config "
                        "(see the README's Enable delivery section)")
    peer_key = peer_key_for(settings.peer)
    if settings.delivery == "peer" or (settings.delivery == "auto" and settings.peer):
        if peer_key:
            return "peer", peer_key, ""
        return "", "", ("relay.delivery=peer but no key resolves for "
                        f"{settings.peer!r} — register the peer with `hermes peer add` "
                        "and set HERMES_PEER_<NAME>_KEY")
    return "", "", ("Delivery not configured — relay_enabled is on but no route is set up. "
                    "Enable the api_server platform and paste its key into relay_api_key "
                    "(README: Enable delivery), or register a bot peer (relay_peer).")


def _post_session_chat(api_url: str, key: str, session_id: str, text: str) -> dict:
    """POST one batch as a live turn into ``session_id`` via the gateway's
    api_server session-chat route — the sanctioned immediate-submission path
    (``hermes peer dm`` drives the same route). Returns
    ``{"ok": True}`` or ``{"ok": False, "retryable": bool, "error": str}``."""
    url = f"{api_url}/api/sessions/{urllib.parse.quote(session_id, safe='')}/chat"
    data = json.dumps({"message": text}).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json",
        "User-Agent": "ao-bridge-relay"})
    try:
        with urllib.request.urlopen(req, timeout=RELAY_CHAT_TIMEOUT_S) as resp:
            body = resp.read()
    except urllib.error.HTTPError as err:
        try:
            detail = json.loads(err.read() or b"{}")
            message = detail.get("error", {}).get("message") or detail.get("message") or err.reason
        except (ValueError, AttributeError):
            message = err.reason
        # 429 (global concurrency cap) and 5xx clear on their own; auth and
        # a missing session do not.
        return {"ok": False, "retryable": err.code == 429 or err.code >= 500,
                "error": f"chat route {err.code}: {message}"}
    except (urllib.error.URLError, OSError) as err:
        # Gateway api_server down: retryable, the loop tries the next tick.
        return {"ok": False, "retryable": True, "error": f"gateway api unreachable: {err}"}
    if not body:
        return {"ok": True}
    try:
        payload = json.loads(body)
    except ValueError:
        return {"ok": True}
    status = str(payload.get("status") or "")
    if status and status not in ("completed", "success", "ok"):
        return {"ok": False, "retryable": False,
                "error": f"turn ended with status {status!r}"}
    return {"ok": True}


def deliver_relay_batch(session_id: str, text: str, settings: RelaySettings) -> dict:
    """Wake the destination session NOW: submit the batch as a live turn
    through the api_server chat route. A delivered event costs the session a
    model turn — that is the point (the steering rejected next-turn-only
    store rows); batching/debounce keeps the cost per burst, not per event."""
    if not session_id:
        return {"ok": False, "retryable": False, "error": "no destination session"}
    route, key, reason = select_delivery_route(settings)
    if not route:
        return {"ok": False, "retryable": False, "error": reason}
    return _post_session_chat(settings.api_url, key, session_id, text)


def _live_orchestrator_for_project(ao, project_id: str) -> tuple:
    """The project's live AO orchestrator session: ``(session_id, "")`` when
    exactly one, ``(None, reason)`` when none or ambiguous. AO exposes the
    orchestrator/worker kinds on its REST rows; the desktop app's own report
    routing keys off the same kind."""
    try:
        sessions = [s for s in ao.sessions() if isinstance(s, dict)
                    and s.get("projectId") == project_id and s.get("kind") == "orchestrator"
                    and not s.get("isTerminated")]
    except AOApiError:
        return None, "ao daemon unreachable"
    live = [s for s in sessions if (s.get("status") or "") != "terminated"]
    if not live:
        return None, f"no live orchestrator session for project {project_id!r}"
    if len(live) > 1:
        # Several orchestrators share the project: relaying into one of them
        # would guess. The override exists for exactly this.
        return None, f"{len(live)} live orchestrators for project {project_id!r} — ambiguous"
    live.sort(key=lambda s: s.get("updatedAt") or "", reverse=True)
    return str(live[0].get("id")), ""


def _hermes_orchestrator_session(db, project_id: str) -> str:
    """The Hermes session backing the project's orchestrator conversation:
    AO runs it with cwd under ``<worktrees>/<project>/orchestrator/`` (seen on
    this machine's live sessions), so the cwd marker — not the volatile display
    name — identifies it. Most-recently-active live session wins."""
    if db is None:
        return ""
    marker = f"/worktrees/{project_id}/orchestrator/"
    try:
        rows = db.list_sessions_rich(source="local", limit=200,
                                     order_by_last_active=True, compact_rows=True)
    except Exception:
        return ""
    for row in rows:
        cwd = str(row.get("cwd") or "")
        if marker not in cwd:
            continue
        if row.get("end_reason"):
            continue
        return str(row.get("id") or "")
    return ""


def resolve_relay_destination(project_id: str, ao, settings: RelaySettings, db) -> tuple:
    """Where one event goes: the project's live orchestrator conversation,
    resolved per event (different workers may belong to different
    orchestrators). ``(session_id, "")`` or ``(None, reason)``; the explicit
    ``relay_session`` override catches every unmappable case — no live
    orchestrator, an ambiguous one, or one without a live conversation."""
    mapped, _ao_id = _live_orchestrator_for_project(ao, project_id)
    if mapped:
        session = _hermes_orchestrator_session(db, project_id)
        if session:
            return session, ""
        reason = (f"orchestrator {mapped} has no live Hermes conversation "
                  f"(no session cwd under /worktrees/{project_id}/orchestrator/)")
    else:
        reason = _ao_id
    if settings.override_session:
        return settings.override_session, f"override used: {reason}"
    return None, reason


class RelayWorker:
    """Backend-owned poll loop: independent of dashboard browser clients,
    silent through daemon downtime (the next successful tick resumes), and a
    no-op unless the relay is explicitly enabled. Destinations are resolved
    per event; each destination session gets its own debounced batch."""

    def __init__(self, poll_seconds: float = RELAY_POLL_SECONDS,
                 debounce_s: float = RELAY_DEBOUNCE_SECONDS):
        self.poll_seconds, self.debounce_s = poll_seconds, debounce_s
        self.seen_reports: dict = {}
        self.prev_status: dict = {}
        self.baselined = False
        self.pending: dict = {}  # destination session id -> RelayBatcher
        self.last_error = ""
        self.last_delivery = 0.0
        self.delivered_count = 0

    def tick(self, ao, sink) -> bool:
        """One relay tick; False when disabled (the loop then just sleeps)."""
        settings = read_relay_settings()
        if not settings.enabled:
            return False
        self.poll_once(ao, sink, settings=settings)
        return True

    def poll_once(self, ao, sink, *, settings=None, kinds=None, db="unset") -> None:
        settings = settings or read_relay_settings()
        kinds = kinds or settings.events
        if db == "unset":
            db = _open_session_db()
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
            # enabling the relay must not flood any conversation with history.
            self.baselined = True
            events = []
        for ev in events:
            dest, reason = resolve_relay_destination(ev.project_id, ao, settings, db)
            if not dest:
                # Unmappable (no live orchestrator / ambiguous / no matching
                # conversation): dropped rather than guessed; the override or
                # /relay diagnostics is the remedy.
                self.last_error = f"{ev.kind} for {ev.project_id or '?'} unmapped: {reason}"
                continue
            batcher = self.pending.get(dest)
            if batcher is None:
                batcher = self.pending[dest] = RelayBatcher(debounce_s=self.debounce_s)
            batcher.add(format_relay_event(ev))
        self._flush(sink, settings)

    def _flush(self, sink, settings: RelaySettings) -> None:
        """Deliver each destination's debounced batch as one live turn."""
        for dest in list(self.pending):
            batcher = self.pending[dest]
            if not batcher.due():
                continue
            text = batcher.flush()
            if not text:
                self.pending.pop(dest, None)
                continue
            result = deliver_relay_batch(dest, text, settings)
            if result["ok"]:
                self.last_delivery = time.monotonic()
                self.delivered_count += 1
                self.last_error = ""
                sink(text)
                self.pending.pop(dest, None)
            elif not result.get("retryable"):
                # Permanent (no credential, session gone): drop the batch —
                # retrying forever would only spin. /relay reports the cause.
                self.last_error = f"{dest}: {result.get('error') or 'delivery failed'}"
                self.pending.pop(dest, None)
            else:
                # Gateway busy/unreachable (429, 5xx, connection): keep the
                # batch for the next tick.
                batcher.restore(text)
                self.last_error = f"{dest}: {result.get('error') or 'delivery deferred'}"


_relay_worker: Optional[RelayWorker] = None
_relay_lock = threading.Lock()
_relay_thread: Optional[threading.Thread] = None


def _relay_sink(_text: str) -> None:
    """Delivery is complete at the chat-route admission; nothing at INFO."""


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
    """Relay diagnostics for the dashboard / operators: what it watches, which
    delivery route is active, and whether the last tick was healthy. When the
    relay is enabled but no route resolves, ``delivery`` carries the honest
    "not configured" state (with the reason) instead of a silent no-op."""
    worker = ensure_relay_started()
    settings = read_relay_settings()
    route, _key, reason = select_delivery_route(settings)
    return {
        "ok": True,
        "enabled": settings.enabled,
        "overrideSession": settings.override_session,
        "apiUrl": settings.api_url,
        "peer": settings.peer,
        "delivery": {
            "mode": settings.delivery,
            "route": route,
            "configured": bool(route),
            "reason": reason,
        },
        "events": sorted(settings.events),
        "watching": worker.baselined,
        "delivered": worker.delivered_count,
        "pendingDestinations": sorted(worker.pending),
        "lastError": worker.last_error,
    }


@router.get("/relay/sessions")
def relay_session_suggestions():
    """Recent Hermes sessions of this profile, for picking the
    ``relay_session`` override. The normal destination is resolved from AO's
    own orchestrator mapping; the override is only for unmappable cases."""
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

