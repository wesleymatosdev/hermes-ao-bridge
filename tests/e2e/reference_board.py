"""The minimal reference daemon — source of truth for the E2E parity gate.

A small canonical set of AO daemon session rows covering every board edge the
gate reasons about, plus a PURE model that computes the board those rows
IMPLY. The E2E contract is parity: render the rows through the real plugin in
Chrome and assert the DOM equals the model 1:1 — same card names per column,
same state chips, same archive membership, same orchestrator strip.

Archive ground truth — AO desktop, extracted app.asar (v0.8.3 investigation,
SessionsBoard-THqlEmxF.js function He + useCloudSessionEvents function b):
the desktop groups a session under Archive when

    kanbanColumn === "archive" || isTerminated === true || status === "terminated"

The sessions REST payload carries NO explicit archive marker (archivedAt /
is_archived exist only in the daemon's retention store, never in list rows),
so this predicate over the list rows IS the desktop truth. It applies to ALL
sessions — orchestrators included.

Column placement mirrors the AO board mapping documented in the README (the
same rules the plugin mirrors); the activity-state enum mirrors the plugin's
documented honest-state derivation. Both are re-derived HERE, independently,
so the gate asserts the rendered board against the data, not against the
plugin's own code.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Canonical rows
# ---------------------------------------------------------------------------

WORKER_EDGES = (
    # (id, display name, row overrides) — one worker per board edge.
    ("w-working", "working-worker", {"status": "working"}),
    ("w-idle", "idle-worker", {"status": "idle"}),
    ("w-exited", "exited-worker", {"status": "exited"}),
    ("w-awaiting", "awaiting-worker",
     {"status": "idle", "displayStatus": "Awaiting PR"}),
    ("w-review", "review-worker", {"status": "review_pending"}),
    ("w-cifailed", "ci-failed-worker", {"status": "ci_failed"}),
    ("w-queued", "queued-worker",
     {"status": "idle", "provisionState": "provisioning"}),
    ("w-ready", "ready-worker", {"status": "idle", "kanbanColumn": "ready"}),
    # The predicate edge: daemon moved the row into its archive column but the
    # session is NOT terminated — desktop still archives it (no explicit
    # marker exists; the column IS the marker).
    ("w-arch-col", "arch-col-worker",
     {"status": "idle", "kanbanColumn": "archive"}),
    ("w-terminated", "terminated-worker",
     {"status": "terminated", "isTerminated": True}),
)

ORCHESTRATOR_EDGES = (
    ("o-working", "orch-working", {"status": "working"}),
    ("o-live", "orch-live", {"status": "idle"}),
    ("o-dead", "orch-dead",
     {"status": "terminated", "isTerminated": True}),
)


def canonical_rows(created_at: str) -> list[dict]:
    """The canonical session rows (worker + orchestrator edges), recent."""
    rows = []
    for session_id, name, overrides in WORKER_EDGES + ORCHESTRATOR_EDGES:
        row = {
            "id": session_id,
            "kind": "orchestrator" if session_id.startswith("o-") else "worker",
            "harness": "hermes",
            "displayName": name,
            "mode": "chat",
            "activity": {"state": "idle", "lastActivityAt": created_at},
            "isTerminated": False,
            "status": "idle",
            "displayStatus": "",
            "kanbanColumn": "",
            "provisionState": "ready",
            "projectId": "proj-demo",
            "branch": "feat/test",
            "prs": [],
            "createdAt": created_at,
            "updatedAt": created_at,
            "isPinned": False,
            "model": "",
        }
        if overrides.get("status") == "working":
            row["activity"]["state"] = "active"
        row.update(overrides)
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# The pure board model
# ---------------------------------------------------------------------------

def is_archived(row: dict) -> bool:
    """AO desktop's archive predicate over the daemon's list rows (the asar
    function He / useCloudSessionEvents truth — see module docstring)."""
    return (row.get("kanbanColumn") == "archive"
            or row.get("isTerminated") is True
            or row.get("status") == "terminated")


def column_for(row: dict) -> str:
    """The AO board column the row implies (README mapping, first match wins)."""
    if is_archived(row):
        return "archive"
    if row.get("status") == "merged":
        return "done"
    if (row.get("status") == "review_pending"
            or row.get("kanbanColumn") == "needs_review"):
        return "review"
    if (row.get("status") == "ci_failed"
            or row.get("provisionState") == "failed"):
        return "blocked"
    display = (row.get("displayStatus") or "").lower()
    if "review" in display:
        return "review"
    if display == "awaiting pr":
        return "done"
    if row.get("provisionState") == "provisioning":
        return "queued"
    if row.get("kanbanColumn") == "ready":
        return "done"
    return "running"


def state_for(row: dict) -> str:
    """The honest activity state the row implies (what a card's chip shows)."""
    if is_archived(row):
        return "archived"
    if row.get("status") == "working":
        return "working"
    if row.get("status") == "exited":
        return "exited"
    if "awaiting pr" in (row.get("displayStatus") or "").lower():
        return "awaiting_pr"
    return "idle"


def expected_board(rows: list[dict], active_only: bool) -> dict:
    """The board the daemon data implies, as the DOM must show it.

    Returns {"columns": {column: set(names)}, "archive": set(names),
    "orchestrators": set(names), "chips": {name: state}}.

    - Archived rows (desktop predicate, ALL kinds) form the Archive strip; the
      strip ignores the activity toggle and the recency window.
    - kind == "orchestrator" (not archived) forms the orchestrator strip; it
      opts out of Active-only (v0.8.3).
    - Worker cards fill their columns; Active-only keeps only live turns
      (state working) — Show all returns everyone not archived.
    """
    columns: dict[str, set] = {c: set() for c in
                               ("queued", "running", "review", "blocked", "done")}
    archive: set = set()
    orchestrators: set = set()
    chips: dict[str, str] = {}
    for row in rows:
        name = str(row.get("displayName") or row.get("id") or "")
        if is_archived(row):
            archive.add(name)
            chips[name] = "archived"
            continue
        chips[name] = state_for(row)
        if row.get("kind") == "orchestrator":
            orchestrators.add(name)
            continue
        if active_only and chips[name] != "working":
            continue
        columns.setdefault(column_for(row), set()).add(name)
    return {"columns": columns, "archive": archive,
            "orchestrators": orchestrators, "chips": chips}
