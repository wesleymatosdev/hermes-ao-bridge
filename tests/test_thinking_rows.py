"""Thinking-row contracts (v0.3.8): the card-detail transcript renders the
worker's reasoning as a clearly SECONDARY "Thinking" row — muted, italic,
collapsed to a one-line preview, pulsing only while that thought is actually
being streamed.

The render DECISIONS live in the backend payload so they are unit-testable:
``preview`` (whitespace-collapsed first ~80 chars) and ``live`` (true on the
last reasoning entry only, and only when its status is "running" — wire
truth probed on the live daemon, 2026-11-09: mid-turn, EVERY reasoning entry
of the running turn reports status "running"; only the highest-sequence one
is still being written). The frontend just renders those fields; the
Worker-chip coupling (thinking hides when Worker hides) is a frontend
timeline filter, documented in dist/index.js and the README.
"""

import os
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


def reasoning(seq, status="running", text="a thought", revision=1):
    return {"activityKind": "reasoning", "status": status, "summary": "Reasoning",
            "sequence": seq, "revision": revision, "detail": {"text": text}}


class ThinkingPreviewTest(unittest.TestCase):
    def test_preview_collapses_whitespace_and_newlines(self):
        self.assertEqual(
            plugin_api._thinking_preview("line one\n\n  line two\ttabbed  "),
            "line one line two tabbed")

    def test_preview_bounded_to_the_constant(self):
        long = "x" * 500
        out = plugin_api._thinking_preview(long)
        self.assertEqual(out, "x" * plugin_api.THINKING_PREVIEW_CHARS)
        self.assertEqual(plugin_api.THINKING_PREVIEW_CHARS, 80)

    def test_preview_of_short_text_is_unchanged(self):
        self.assertEqual(plugin_api._thinking_preview("short thought"), "short thought")
        self.assertEqual(plugin_api._thinking_preview(""), "")


class ThinkingLiveFlagTest(unittest.TestCase):
    def setUp(self):
        self.ao = FakeAO()
        self.server = FakeAOServer(self.ao)
        self.server.start()
        self._old_env = os.environ.get("AO_BRIDGE_BASE_URL")
        os.environ["AO_BRIDGE_BASE_URL"] = self.server.url
        self.http = TestClient(make_app())
        self.ao.add_session(make_session("s1", project_id="hermes-agent"))
        self.ao.set_conversation("s1", [])

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("AO_BRIDGE_BASE_URL", None)
        else:
            os.environ["AO_BRIDGE_BASE_URL"] = self._old_env
        self.server.shutdown()
        self.server.server_close()

    def detail_activities(self):
        body = self.http.get("/api/plugins/ao-bridge/sessions/s1/detail").json()
        return {a["sequence"]: a for a in body["activities"]}

    def test_mid_turn_only_the_last_running_entry_is_live(self):
        # Wire truth on a working session: every reasoning entry of the live
        # turn reads "running" — pulsing all of them would be a lie.
        self.ao.set_activities("s1", [
            reasoning(60, text="earlier thought"),
            reasoning(64, text="middle thought"),
            reasoning(75, text="the thought being streamed"),
        ])
        acts = self.detail_activities()
        self.assertIsNone(acts[60].get("live"))
        self.assertIsNone(acts[64].get("live"))
        self.assertTrue(acts[75]["live"])

    def test_completed_latest_is_not_live(self):
        self.ao.set_activities("s1", [reasoning(536, status="completed")])
        acts = self.detail_activities()
        self.assertIsNone(acts[536].get("live"))

    def test_recovered_latest_is_not_live(self):
        # "recovered" observed on finished turns (hermes-agent-41): terminal,
        # never a live stream.
        self.ao.set_activities("s1", [reasoning(10, status="recovered")])
        acts = self.detail_activities()
        self.assertIsNone(acts[10].get("live"))

    def test_no_reasoning_activities_is_clean(self):
        self.ao.set_activities("s1", [
            {"activityKind": "command", "status": "completed", "summary": "ls",
             "sequence": 1, "revision": 1, "detail": {"command": "ls"}},
        ])
        self.assertEqual(self.detail_activities()[1]["activityKind"], "command")

    def test_latest_reasoning_after_a_later_command_is_still_the_live_one(self):
        # Liveness is "last reasoning entry", not "last activity": tool calls
        # interleave after the thought without stopping its stream.
        self.ao.set_activities("s1", [
            reasoning(60, text="earlier thought"),
            reasoning(75, text="streaming thought"),
            {"activityKind": "command", "status": "pending", "summary": "pytest",
             "sequence": 76, "revision": 1, "detail": {"command": "pytest"}},
        ])
        acts = self.detail_activities()
        self.assertIsNone(acts[60].get("live"))
        self.assertTrue(acts[75]["live"])


class ThinkingPayloadTest(unittest.TestCase):
    def setUp(self):
        self.ao = FakeAO()
        self.server = FakeAOServer(self.ao)
        self.server.start()
        self._old_env = os.environ.get("AO_BRIDGE_BASE_URL")
        os.environ["AO_BRIDGE_BASE_URL"] = self.server.url
        self.http = TestClient(make_app())
        self.ao.add_session(make_session("s1", project_id="hermes-agent"))
        self.ao.set_conversation("s1", [])

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("AO_BRIDGE_BASE_URL", None)
        else:
            os.environ["AO_BRIDGE_BASE_URL"] = self._old_env
        self.server.shutdown()
        self.server.server_close()

    def test_reasoning_row_carries_a_collapsed_preview(self):
        self.ao.set_activities("s1", [
            reasoning(2, text="Key findings:\n- the field is `text`\n- bounded"),
        ])
        row = self.http.get("/api/plugins/ao-bridge/sessions/s1/detail").json()["activities"][0]
        self.assertEqual(row["preview"], "Key findings: - the field is `text` - bounded")
        # The full text stays available for the expanded body.
        self.assertIn("Key findings:", row["text"])

    def test_reasoning_row_full_text_still_bounded(self):
        self.ao.set_activities("s1", [reasoning(2, text="y" * (plugin_api.TOOL_OUTPUT_MAX_CHARS + 99))])
        row = self.http.get("/api/plugins/ao-bridge/sessions/s1/detail").json()["activities"][0]
        self.assertEqual(len(row["text"]), plugin_api.TOOL_OUTPUT_MAX_CHARS)
        self.assertTrue(row["textTruncated"])
        self.assertEqual(len(row["preview"]), plugin_api.THINKING_PREVIEW_CHARS)


if __name__ == "__main__":
    unittest.main()
