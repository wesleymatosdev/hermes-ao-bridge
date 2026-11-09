"""v0.3 contracts: tool-execution activities in the detail route (rendered
inline in the transcript, interleaved by sequence) and the percent-encoded
ao:// deep link."""

import os
import unittest

import conftest
from fake_ao_server import FakeAO, FakeAOServer, make_session

from fastapi import FastAPI
from fastapi.testclient import TestClient

plugin_api = conftest.load_plugin_api()


def make_activity(seq, kind, *, status="completed", summary="did a thing",
                  detail=None, activity_id="a1"):
    """One conversation activity in the live daemon's wire shape (probed
    2026-11-09)."""
    base = {
        "kind": "activity", "id": activity_id, "turnId": "t1",
        "sequence": seq, "revision": 1, "activityKind": kind,
        "status": status, "summary": summary,
        "createdAt": "2026-01-01T00:00:00Z",
    }
    if detail is not None:
        base["detail"] = detail
    return base


def make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(plugin_api.router, prefix="/api/plugins/ao-bridge")
    return app


class ToolActivitiesTest(unittest.TestCase):
    def setUp(self):
        self.ao = FakeAO()
        self.server = FakeAOServer(self.ao)
        self.server.start()
        self._old_env = os.environ.get("AO_BRIDGE_BASE_URL")
        os.environ["AO_BRIDGE_BASE_URL"] = self.server.url
        self.http = TestClient(make_app())
        self.ao.add_session(make_session("s1", project_id="hermes-agent"))
        self.ao.set_conversation("s1", [
            {"role": "user", "text": "go", "createdAt": "2026-01-01T00:00:00Z",
             "sequence": 1},
            {"role": "assistant", "text": "done", "createdAt": "2026-01-01T00:00:30Z",
             "sequence": 20},
        ])

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("AO_BRIDGE_BASE_URL", None)
        else:
            os.environ["AO_BRIDGE_BASE_URL"] = self._old_env
        self.server.shutdown()
        self.server.server_close()

    def test_detail_returns_command_mcp_and_reasoning_activities(self):
        self.ao.set_activities("s1", [
            make_activity(1, "system", detail={}),  # not rendered
            make_activity(2, "plan", detail={"steps": []}),  # plan rides detail.plan
            make_activity(3, "reasoning", summary="Reasoning",
                          detail={"text": "let me check"}),
            make_activity(5, "command", summary="git status",
                          detail={"command": "git status", "exitCode": 0,
                                  "output": "clean"}),
            make_activity(7, "mcp_tool", summary="ToolSearch",
                          detail={"providerToolName": "ToolSearch",
                                  "input": {"query": "select:Read"},
                                  "output": "[read]"}),
            make_activity(11, "file_change", detail={}),  # not rendered
        ])
        body = self.http.get("/api/plugins/ao-bridge/sessions/s1/detail").json()
        acts = body["activities"]
        self.assertEqual([a["activityKind"] for a in acts],
                         ["reasoning", "command", "mcp_tool"])
        self.assertEqual([a["sequence"] for a in acts], [3, 5, 7])
        self.assertEqual(body["activitiesTotal"], 3)  # system/plan/file_change excluded
        cmd = acts[1]
        self.assertEqual(cmd["command"], "git status")
        self.assertEqual(cmd["exitCode"], 0)
        self.assertEqual(cmd["output"], "clean")
        tool = acts[2]
        self.assertEqual(tool["toolName"], "ToolSearch")
        self.assertEqual(tool["input"], '{"query": "select:Read"}')
        self.assertEqual(tool["output"], "[read]")
        reason = acts[0]
        self.assertEqual(reason["text"], "let me check")

    def test_transcript_rows_carry_sequence_for_interleaving(self):
        body = self.http.get("/api/plugins/ao-bridge/sessions/s1/detail").json()
        self.assertEqual([m["sequence"] for m in body["transcript"]], [1, 20])

    def test_command_failure_keeps_exit_code_and_status(self):
        self.ao.set_activities("s1", [
            make_activity(4, "command", status="failed", summary="pytest",
                          detail={"command": "pytest", "exitCode": 2,
                                  "output": "2 failed"}),
        ])
        body = self.http.get("/api/plugins/ao-bridge/sessions/s1/detail").json()
        cmd = body["activities"][0]
        self.assertEqual((cmd["status"], cmd["exitCode"]), ("failed", 2))

    def test_activity_output_is_bounded(self):
        self.ao.set_activities("s1", [
            make_activity(4, "command", summary="cat big",
                          detail={"command": "cat big.txt", "exitCode": 0,
                                  "output": "x" * (plugin_api.TOOL_OUTPUT_MAX_CHARS + 500)}),
        ])
        body = self.http.get("/api/plugins/ao-bridge/sessions/s1/detail").json()
        cmd = body["activities"][0]
        self.assertEqual(len(cmd["output"]), plugin_api.TOOL_OUTPUT_MAX_CHARS)
        self.assertTrue(cmd["outputTruncated"])

    def test_activities_tail_is_bounded_and_ordered(self):
        self.ao.set_activities("s1", [
            make_activity(i, "command", summary=f"cmd {i}", activity_id=f"a{i}",
                          detail={"command": f"cmd {i}", "exitCode": 0, "output": ""})
            for i in range(plugin_api.ACTIVITY_TAIL_COUNT + 10)
        ])
        body = self.http.get("/api/plugins/ao-bridge/sessions/s1/detail").json()
        acts = body["activities"]
        self.assertEqual(len(acts), plugin_api.ACTIVITY_TAIL_COUNT)
        self.assertEqual(body["activitiesTotal"], plugin_api.ACTIVITY_TAIL_COUNT + 10)
        seqs = [a["sequence"] for a in acts]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(seqs[0], 10)  # oldest dropped, not newest

    def test_session_without_activities_omits_nothing(self):
        body = self.http.get("/api/plugins/ao-bridge/sessions/s1/detail").json()
        self.assertEqual(body["activities"], [])
        self.assertEqual(body["activitiesTotal"], 0)


class DeepLinkTest(unittest.TestCase):
    def setUp(self):
        self.ao = FakeAO()
        self.server = FakeAOServer(self.ao)
        self.server.start()
        self._old_env = os.environ.get("AO_BRIDGE_BASE_URL")
        os.environ["AO_BRIDGE_BASE_URL"] = self.server.url
        self.http = TestClient(make_app())

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("AO_BRIDGE_BASE_URL", None)
        else:
            os.environ["AO_BRIDGE_BASE_URL"] = self._old_env
        self.server.shutdown()
        self.server.server_close()

    def test_ao_link_helper_encodes_every_segment(self):
        # Route-level round-trip of exotic ids is Starlette's business; what
        # this plugin owns is that the built link never contains a raw '/', '?'
        # or '#' inside a segment.
        self.assertEqual(plugin_api.ao_deep_link("", "s2"), "ao://sessions/s2")
        self.assertEqual(plugin_api.ao_deep_link("hermes-agent", "s2"),
                         "ao://sessions/hermes-agent/s2")
        self.assertEqual(plugin_api.ao_deep_link("a/b", "c/d"),
                         "ao://sessions/a%2Fb/c%2Fd")
        self.assertEqual(plugin_api.ao_deep_link("p#1", "s?2 x"),
                         "ao://sessions/p%231/s%3F2%20x")


if __name__ == "__main__":
    unittest.main()
