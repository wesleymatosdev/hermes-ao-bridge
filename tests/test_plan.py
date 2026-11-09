"""Live-plan contracts: parsing the plan activity, board attach rules, detail payload.

Wire truth (live daemon, 2026-11-09): GET /sessions/{sid}/conversation returns a
top-level ``activities`` array; entries with activityKind=='plan' carry
detail.steps [{status, text}] + detail.event/writtenAt, and the LAST plan
activity is the live plan (matches the AO desktop app's Plan panel).
"""

import conftest  # noqa: F401
import unittest

from fake_ao_server import FakeAO, FakeAOServer, make_session

plugin_api = conftest.load_plugin_api()

latest_plan = plugin_api.latest_plan


def plan_activity(steps, written_at="2026-11-09T02:24:54Z"):
    return {
        "kind": "activity", "id": "a1", "activityKind": "plan", "status": "running",
        "detail": {"event": "plan", "spansTurns": True,
                   "steps": [{"status": s, "text": t} for s, t in steps],
                   "writtenAt": written_at},
        "createdAt": "2026-11-09T02:24:47Z",
    }


class LatestPlanParsingTest(unittest.TestCase):
    def test_extracts_steps_from_plan_activity(self):
        conv = {"activities": [
            {"activityKind": "tool_call", "detail": {}},
            plan_activity([("in_progress", "Repro bug"), ("pending", "Fix bug")]),
        ]}
        plan = latest_plan(conv)
        self.assertEqual(plan["writtenAt"], "2026-11-09T02:24:54Z")
        self.assertEqual(plan["steps"], [
            {"status": "in_progress", "text": "Repro bug"},
            {"status": "pending", "text": "Fix bug"},
        ])

    def test_last_plan_activity_wins(self):
        conv = {"activities": [
            plan_activity([("completed", "old step")], written_at="t1"),
            plan_activity([("in_progress", "new step")], written_at="t2"),
        ]}
        plan = latest_plan(conv)
        self.assertEqual(plan["writtenAt"], "t2")
        self.assertEqual(len(plan["steps"]), 1)
        self.assertEqual(plan["steps"][0]["text"], "new step")

    def test_no_plan_or_junk_payloads_return_none(self):
        self.assertIsNone(latest_plan({}))
        self.assertIsNone(latest_plan({"activities": None}))
        self.assertIsNone(latest_plan({"activities": [{"activityKind": "tool_call"}]}))
        self.assertIsNone(latest_plan({"activities": ["junk", 42]}))
        # steps without text are dropped, unknown statuses fall back to pending
        conv = {"activities": [{"activityKind": "plan", "detail": {"steps": [
            {"status": "in_progress"}, {"bogus": True},
            {"status": "weird", "text": "step"},
        ]}}]}
        self.assertEqual(latest_plan(conv)["steps"],
                         [{"status": "pending", "text": "step"}])


class PlanBoardAttachTest(unittest.TestCase):
    """Board route: plans attach to working sessions only, TTL-cached."""

    def setUp(self):
        self.ao = FakeAO()
        self.server = FakeAOServer(self.ao)
        self.server.start()
        self._old_env = None

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        plugin_api._plan_cache.clear()

    def _client(self):
        import os
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        self._old_env = os.environ.get("AO_BRIDGE_BASE_URL")
        os.environ["AO_BRIDGE_BASE_URL"] = self.server.url
        app = FastAPI()
        app.include_router(plugin_api.router, prefix="/api/plugins/ao-bridge")
        return TestClient(app)

    def test_plan_attaches_to_working_session_only(self):
        self.ao.add_session(make_session("working-1", status="working"))
        self.ao.add_session(make_session("idle-1", status="idle"))
        self.ao.set_activities("working-1", [plan_activity(
            [("in_progress", "step a"), ("pending", "step b")])])
        http = self._client()
        resp = http.get("/api/plugins/ao-bridge/board")
        self.assertEqual(resp.status_code, 200)
        cards = {c["id"]: c for c in resp.json()["cards"]}
        self.assertEqual(cards["working-1"]["plan"]["steps"][0]["text"], "step a")
        self.assertNotIn("plan", cards["idle-1"])

    def test_no_plan_key_when_session_has_no_plan(self):
        self.ao.add_session(make_session("working-2", status="working"))
        http = self._client()
        resp = http.get("/api/plugins/ao-bridge/board")
        cards = {c["id"]: c for c in resp.json()["cards"]}
        self.assertNotIn("plan", cards["working-2"])

    def test_plan_rides_ttl_cache_not_per_poll(self):
        self.ao.add_session(make_session("working-3", status="working"))
        self.ao.set_activities("working-3", [plan_activity([("in_progress", "s")])])
        http = self._client()
        http.get("/api/plugins/ao-bridge/board")
        http.get("/api/plugins/ao-bridge/board")
        http.get("/api/plugins/ao-bridge/board")
        self.assertEqual(len(self.ao.conversation_requests), 1,
                         "TTL cache must absorb repeated board polls")

    def test_stale_plan_drops_off_when_session_stops_working(self):
        self.ao.add_session(make_session("working-4", status="working"))
        self.ao.set_activities("working-4", [plan_activity([("in_progress", "s")])])
        http = self._client()
        http.get("/api/plugins/ao-bridge/board")
        sess = self.ao.sessions_by_id["working-4"]
        sess["status"] = "idle"
        sess["displayStatus"] = "Idle"
        resp = http.get("/api/plugins/ao-bridge/board")
        cards = {c["id"]: c for c in resp.json()["cards"]}
        self.assertNotIn("plan", cards["working-4"])
        self.assertNotIn("working-4", plugin_api._plan_cache)


class PlanDetailRouteTest(unittest.TestCase):
    def setUp(self):
        self.ao = FakeAO()
        self.server = FakeAOServer(self.ao)
        self.server.start()
        self._old_env = None

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def _client(self):
        import os
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        self._old_env = os.environ.get("AO_BRIDGE_BASE_URL")
        os.environ["AO_BRIDGE_BASE_URL"] = self.server.url
        app = FastAPI()
        app.include_router(plugin_api.router, prefix="/api/plugins/ao-bridge")
        return TestClient(app)

    def test_detail_includes_plan_steps(self):
        self.ao.add_session(make_session("s1"))
        self.ao.set_activities("s1", [
            plan_activity([("completed", "done step"),
                           ("in_progress", "current step"),
                           ("pending", "later step"),
                           ("cancelled", "dropped step")])])
        http = self._client()
        resp = http.get("/api/plugins/ao-bridge/sessions/s1/detail")
        self.assertEqual(resp.status_code, 200)
        plan = resp.json()["plan"]
        self.assertEqual([s["status"] for s in plan["steps"]],
                         ["completed", "in_progress", "pending", "cancelled"])

    def test_detail_plan_null_without_plan(self):
        self.ao.add_session(make_session("s2"))
        http = self._client()
        self.assertIsNone(http.get("/api/plugins/ao-bridge/sessions/s2/detail").json()["plan"])
