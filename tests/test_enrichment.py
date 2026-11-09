"""v0.5.0 contracts: Hermes session-store enrichment.

For sessions whose harness is ``hermes`` AND whose Hermes session row is found
in the local ``state.db``, the detail payload gains a ``hermesEnriched``
section: the session's stats (model, tokens, cost, duration) plus full-fidelity
tool calls (name + args + output) and reasoning that ACP truncates or drops.

The AO↔Hermes join is the worktree cwd AO launches ``hermes acp`` with —
``…/worktrees/<projectId>/<sessionId>`` (standalone: ``…/worktrees/standalone/
sessions/<sessionId>``), filtered to rows started at/after the AO session's
creation, most recent wins. All reads are sqlite read-only; a missing DB, a
missing row, or any error degrades to ``{"found": false}`` with the rest of
the payload untouched.
"""

import json
import os
import sqlite3
import tempfile
import unittest

import conftest
from fake_ao_server import FakeAO, FakeAOServer, make_session

from fastapi import FastAPI
from fastapi.testclient import TestClient

plugin_api = conftest.load_plugin_api()


def make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(plugin_api.router, prefix="/api/plugins/ao-bridge")
    return app


# AO createdAt for every fixture session (epoch 1767225600 = 2026-01-01T00:00:00Z)
AO_CREATED_AT = "2026-01-01T00:00:00Z"
AO_CREATED_EPOCH = 1767225600.0


def make_fixture_db(path: str, sessions: list[dict], messages: list[dict]) -> str:
    """A minimal state.db holding just the columns enrichment reads."""
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE sessions (
        id TEXT PRIMARY KEY, cwd TEXT, started_at REAL, ended_at REAL,
        message_count INTEGER, tool_call_count INTEGER, model TEXT,
        input_tokens INTEGER, output_tokens INTEGER, reasoning_tokens INTEGER,
        estimated_cost_usd REAL, title TEXT)""")
    con.execute("""CREATE TABLE messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT,
        content TEXT, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT,
        timestamp REAL, reasoning TEXT, reasoning_content TEXT, active INTEGER)""")
    for s in sessions:
        con.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
            s["id"], s["cwd"], s["started_at"], s.get("ended_at"),
            s.get("message_count", 0), s.get("tool_call_count", 0),
            s.get("model"), s.get("input_tokens", 0), s.get("output_tokens", 0),
            s.get("reasoning_tokens", 0), s.get("estimated_cost_usd", 0.0),
            s.get("title")))
    for m in messages:
        con.execute("INSERT INTO messages (session_id, role, content, tool_call_id,"
                    " tool_calls, tool_name, timestamp, reasoning, reasoning_content,"
                    " active) VALUES (?,?,?,?,?,?,?,?,?,?)", (
                        m["session_id"], m["role"], m.get("content"),
                        m.get("tool_call_id"), m.get("tool_calls"),
                        m.get("tool_name"), m["timestamp"], m.get("reasoning"),
                        m.get("reasoning_content"), m.get("active", 1)))
    con.commit()
    con.close()
    return path


class EnrichmentTestBase(unittest.TestCase):
    """Fixture DB + env pointing the plugin at it; one hermes worker session."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, "state.db")
        self.worktree = os.path.join(self.tmp.name, "worktrees", "proj-1", "ao-worker-1")
        make_fixture_db(
            self.db_path,
            sessions=[{"id": "local-1", "cwd": self.worktree,
                       "started_at": AO_CREATED_EPOCH + 4, "ended_at": AO_CREATED_EPOCH + 90,
                       "message_count": 6, "tool_call_count": 2, "model": "glm-5.3-flash",
                       "input_tokens": 1000, "output_tokens": 200, "reasoning_tokens": 50,
                       "estimated_cost_usd": 0.0123, "title": "fix the thing"}],
            messages=[
                {"session_id": "local-1", "role": "assistant",
                 "timestamp": AO_CREATED_EPOCH + 5, "reasoning": "think step one"},
                {"session_id": "local-1", "role": "assistant",
                 "timestamp": AO_CREATED_EPOCH + 6,
                 "tool_calls": json.dumps([{"id": "call_1", "name": "terminal",
                                            "arguments": {"command": "git status"}}])},
                {"session_id": "local-1", "role": "tool", "tool_call_id": "call_1",
                 "tool_name": "terminal", "content": "On branch main\nnothing to commit",
                 "timestamp": AO_CREATED_EPOCH + 7},
                {"session_id": "local-1", "role": "assistant",
                 "timestamp": AO_CREATED_EPOCH + 8,
                 "tool_calls": json.dumps([{"id": "call_2", "name": "read_file",
                                            "arguments": {"path": "a.py"}}])},
                {"session_id": "local-1", "role": "tool", "tool_call_id": "call_2",
                 "tool_name": "read_file", "content": "x = 1\n",
                 "timestamp": AO_CREATED_EPOCH + 9},
                {"session_id": "local-1", "role": "user", "content": "next user turn",
                 "timestamp": AO_CREATED_EPOCH + 10},
            ])
        os.environ["AO_BRIDGE_STATE_DB"] = self.db_path
        self.addCleanup(os.environ.pop, "AO_BRIDGE_STATE_DB", None)


class JoinResolutionTest(EnrichmentTestBase):
    """Phase-1 gate contracts: cwd is the join key; the createdAt window and
    most-recent-row rule make it deterministic; failures return None."""

    def join(self, project_id="proj-1", session_id="ao-worker-1",
             created_epoch=AO_CREATED_EPOCH):
        con = plugin_api.open_hermes_db()
        try:
            return plugin_api.find_hermes_session(con, project_id, session_id, created_epoch)
        finally:
            con.close()

    def test_worker_cwd_joins_exact(self):
        row = self.join()
        self.assertIsNotNone(row)
        self.assertEqual(row["id"], "local-1")
        self.assertEqual(row["model"], "glm-5.3-flash")

    def test_standalone_cwd_joins(self):
        stand = os.path.join(self.tmp.name, "worktrees", "standalone",
                             "sessions", "ao-solo")
        os.unlink(self.db_path)
        make_fixture_db(self.db_path, sessions=[
            {"id": "local-1", "cwd": self.worktree, "started_at": AO_CREATED_EPOCH + 4},
            {"id": "local-2", "cwd": stand, "started_at": AO_CREATED_EPOCH + 3}],
            messages=[])
        row = self.join(project_id="", session_id="ao-solo")
        self.assertIsNotNone(row)
        self.assertEqual(row["id"], "local-2")

    def test_created_at_window_excludes_stale_rows(self):
        # A row from BEFORE the AO session existed is a different conversation.
        self.assertIsNone(self.join(created_epoch=AO_CREATED_EPOCH + 600))

    def test_most_recent_row_wins_on_resume(self):
        os.unlink(self.db_path)
        make_fixture_db(self.db_path, sessions=[
            {"id": "local-old", "cwd": self.worktree, "started_at": AO_CREATED_EPOCH + 4},
            {"id": "local-new", "cwd": self.worktree, "started_at": AO_CREATED_EPOCH + 400}],
            messages=[])
        row = self.join()
        self.assertEqual(row["id"], "local-new")

    def test_no_row_returns_none(self):
        self.assertIsNone(self.join(session_id="never-launched"))

    def test_like_metachars_in_ids_are_literal(self):
        # A session id containing % or _ must not turn the suffix match into a
        # wildcard scan joining an unrelated row.
        self.assertIsNone(self.join(session_id="ao-worker%"))
        self.assertIsNone(self.join(session_id="ao_worker_1"))


class EnrichmentMergeTest(EnrichmentTestBase):
    """The merge: reasoning rows and tool-call+result pairs become bounded
    activities; user/assistant text rows are left to the AO transcript."""

    def enrichment(self, tail=plugin_api.HERMES_ACTIVITY_TAIL):
        con = plugin_api.open_hermes_db()
        try:
            row = plugin_api.find_hermes_session(con, "proj-1", "ao-worker-1",
                                                 AO_CREATED_EPOCH)
            return plugin_api.hermes_enrichment(con, row, tail=tail)
        finally:
            con.close()

    def test_reasoning_items_carry_text(self):
        out = self.enrichment()
        reasons = [a for a in out["activities"] if a["kind"] == "reasoning"]
        self.assertEqual(len(reasons), 1)
        self.assertEqual(reasons[0]["text"], "think step one")

    def test_tool_items_merge_call_with_result(self):
        out = self.enrichment()
        tools = [a for a in out["activities"] if a["kind"] == "tool"]
        self.assertEqual(len(tools), 2)
        first = tools[0]
        self.assertEqual(first["toolName"], "terminal")
        self.assertIn("git status", first["input"])
        self.assertIn("nothing to commit", first["output"])
        self.assertFalse(first["outputTruncated"])

    def test_user_and_assistant_text_rows_are_skipped(self):
        out = self.enrichment()
        kinds = {a["kind"] for a in out["activities"]}
        self.assertEqual(kinds, {"reasoning", "tool"})

    def test_session_stats(self):
        out = self.enrichment()
        stats = out["session"]
        self.assertEqual(stats["model"], "glm-5.3-flash")
        self.assertEqual(stats["messageCount"], 6)
        self.assertEqual(stats["toolCallCount"], 2)
        self.assertEqual(stats["inputTokens"], 1000)
        self.assertEqual(stats["reasoningTokens"], 50)
        self.assertEqual(stats["estimatedCostUsd"], 0.0123)
        self.assertEqual(stats["title"], "fix the thing")
        self.assertEqual(stats["durationSeconds"], 86)

    def test_tail_bounding_keeps_totals(self):
        os.unlink(self.db_path)
        msgs = []
        for i in range(5):
            msgs.append({"session_id": "local-1", "role": "assistant",
                         "timestamp": AO_CREATED_EPOCH + i, "reasoning": f"step {i}"})
        make_fixture_db(self.db_path, sessions=[
            {"id": "local-1", "cwd": self.worktree, "started_at": AO_CREATED_EPOCH}],
            messages=msgs)
        # under the tail cap: nothing flagged truncated
        out = self.enrichment()
        self.assertEqual(out["activitiesTotal"], 5)
        self.assertFalse(out["activitiesTruncated"])
        self.assertEqual(len(out["activities"]), 5)
        # tail=3: only the LAST three survive, totals keep the full count
        out = self.enrichment(tail=3)
        self.assertEqual(out["activitiesTotal"], 5)
        self.assertTrue(out["activitiesTruncated"])
        self.assertEqual([a["text"] for a in out["activities"]],
                         ["step 2", "step 3", "step 4"])

    def test_long_outputs_are_bounded_with_flag(self):
        os.unlink(self.db_path)
        make_fixture_db(self.db_path, sessions=[
            {"id": "local-1", "cwd": self.worktree, "started_at": AO_CREATED_EPOCH}],
            messages=[
                {"session_id": "local-1", "role": "assistant",
                 "timestamp": AO_CREATED_EPOCH + 1,
                 "tool_calls": json.dumps([{"id": "c", "name": "read_file",
                                            "arguments": {"path": "big.txt"}}])},
                {"session_id": "local-1", "role": "tool", "tool_call_id": "c",
                 "tool_name": "read_file", "content": "y" * 100_000,
                 "timestamp": AO_CREATED_EPOCH + 2}])
        out = self.enrichment()
        tool = out["activities"][0]
        self.assertTrue(tool["outputTruncated"])
        self.assertEqual(len(tool["output"]), plugin_api.TOOL_OUTPUT_MAX_CHARS)


class DetailPayloadContractTest(EnrichmentTestBase):
    """The detail route: hermesEnriched rides a hermes session's payload,
    degrades to found:false elsewhere, and never breaks the payload."""

    def detail(self, harness="hermes"):
        ao = FakeAO()
        ao.add_session(make_session("ao-worker-1", project_id="proj-1",
                                    harness=harness, created_at=AO_CREATED_AT))
        ao.set_conversation("ao-worker-1", [
            {"role": "assistant", "text": "hello", "sequence": 1}])
        with FakeAOServer(ao) as server:
            server.start()
            os.environ["AO_BRIDGE_BASE_URL"] = server.url
            self.addCleanup(os.environ.pop, "AO_BRIDGE_BASE_URL", None)
            with TestClient(make_app()) as client:
                resp = client.get("/api/plugins/ao-bridge/sessions/ao-worker-1/detail")
        self.assertEqual(resp.status_code, 200)
        return resp.json()

    def test_hermes_session_gains_enrichment(self):
        payload = self.detail(harness="hermes")
        enriched = payload.get("hermesEnriched") or {}
        self.assertTrue(enriched.get("found"))
        self.assertEqual(enriched["session"]["model"], "glm-5.3-flash")
        kinds = {a["kind"] for a in enriched["activities"]}
        self.assertEqual(kinds, {"reasoning", "tool"})
        # the AO side of the payload is untouched
        self.assertEqual(payload["transcript"][0]["text"], "hello")

    def test_non_hermes_harness_skips_enrichment(self):
        payload = self.detail(harness="claude-code")
        self.assertFalse((payload.get("hermesEnriched") or {}).get("found", False))

    def test_missing_db_degrades_without_breakage(self):
        os.environ["AO_BRIDGE_STATE_DB"] = os.path.join(self.tmp.name, "nope.db")
        payload = self.detail(harness="hermes")
        self.assertTrue(payload.get("ok"))
        self.assertEqual(payload["transcript"][0]["text"], "hello")
        self.assertFalse((payload.get("hermesEnriched") or {}).get("found", False))
