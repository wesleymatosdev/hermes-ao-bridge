"""v0.4.0 contracts: steering/messaging workers from the board.

The plugin's steer route (POST /sessions/{id}/steer) is a passthrough to the
daemon's POST /api/v1/sessions/{id}/conversation/steer-or-send — the combined
AO-native operation: it steers a session whose turn is in flight and queues a
new turn on an idle one. Wire truth verified against the live daemon and the
AO backend source (2026-11-09):
- the steer body is {"text", "clientMessageId"} — "text", NOT "message";
- the daemon REQUIRES the clientMessageId idempotency handle (missing → 409
  CHAT_STEER_UNCERTAIN, not a validation error);
- answers 202 {"outcome": "steered"|"sent", ...};
- messages land with origin=human, so they render as "End user";
- errors are {"error": kind, "code": "CHAT_*", "message", "requestId"}.

The legacy /send route and the queue-only semantics stay untouched (compat).
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


class SteerRouteTestBase(unittest.TestCase):
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


class SteerOrSendPassthroughTest(SteerRouteTestBase):
    def setUp(self):
        super().setUp()
        self.ao.add_session(make_session("idle1", status="idle"))
        self.ao.add_session(make_session("busy1", status="working"))

    def test_steer_forwards_text_and_a_client_message_id(self):
        resp = self.http.post("/api/plugins/ao-bridge/sessions/idle1/steer",
                              json={"message": "check the failing test"})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["sessionId"], "idle1")
        # The daemon queued a turn (idle target) — the outcome rides through.
        self.assertEqual(body["outcome"], "sent")
        steers = self.ao.steers
        self.assertEqual(len(steers), 1)
        self.assertEqual(steers[0]["session_id"], "idle1")
        self.assertEqual(steers[0]["body"]["text"], "check the failing test")
        # The idempotency handle is a plugin-side requirement: without it the
        # daemon answers 409 CHAT_STEER_UNCERTAIN for every request.
        self.assertTrue(steers[0]["body"].get("clientMessageId"))

    def test_working_session_steers_and_reports_the_outcome(self):
        resp = self.http.post("/api/plugins/ao-bridge/sessions/busy1/steer",
                              json={"message": "also update the changelog"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["outcome"], "steered")
        self.assertEqual(self.ao.steers[0]["session_id"], "busy1")

    def test_each_send_gets_its_own_idempotency_handle(self):
        for text in ("first", "second"):
            self.http.post("/api/plugins/ao-bridge/sessions/idle1/steer",
                           json={"message": text})
        ids = [s["body"]["clientMessageId"] for s in self.ao.steers]
        self.assertEqual(len(ids), 2)
        self.assertNotEqual(ids[0], ids[1])

    def test_empty_message_is_rejected_before_the_daemon(self):
        resp = self.http.post("/api/plugins/ao-bridge/sessions/idle1/steer",
                              json={"message": "   "})
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(self.ao.steers, [])

    def test_legacy_send_route_still_works(self):
        resp = self.http.post("/api/plugins/ao-bridge/sessions/idle1/send",
                              json={"message": "old path", "userAuthored": True})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([s["body"]["message"] for s in self.ao.sends], ["old path"])
        self.assertEqual(self.ao.steers, [])


class SteerErrorMappingTest(SteerRouteTestBase):
    def setUp(self):
        super().setUp()
        self.ao.add_session(make_session("w1", status="idle"))

    def test_daemon_error_code_reaches_the_client(self):
        self.ao.steer_error = ("CHAT_TURN_NOT_STEERABLE", 409, "a compaction turn is running")
        resp = self.http.post("/api/plugins/ao-bridge/sessions/w1/steer",
                              json={"message": "hello"})
        self.assertEqual(resp.status_code, 409)
        detail = resp.json()["detail"]
        self.assertEqual(detail["code"], "CHAT_TURN_NOT_STEERABLE")
        self.assertEqual(detail["upstreamStatus"], 409)
        self.assertEqual(detail["message"], "a compaction turn is running")
        # AO marks this one retryable — the blocking turn ends on its own.
        self.assertTrue(detail["retryable"])
        self.assertEqual(detail["pluginVersion"], plugin_api.PLUGIN_VERSION)

    def test_uncertain_delivery_is_a_conflict_not_a_server_error(self):
        self.ao.steer_error = ("CHAT_STEER_UNCERTAIN", 409, "delivery is uncertain")
        resp = self.http.post("/api/plugins/ao-bridge/sessions/w1/steer",
                              json={"message": "hello"})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["detail"]["code"], "CHAT_STEER_UNCERTAIN")

    def test_validation_code_maps_to_a_client_error(self):
        self.ao.steer_error = ("CHAT_UNSUPPORTED_STEER_CONTENT", 400,
                               "this agent cannot steer every attachment")
        resp = self.http.post("/api/plugins/ao-bridge/sessions/w1/steer",
                              json={"message": "hello"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["detail"]["code"], "CHAT_UNSUPPORTED_STEER_CONTENT")

    def test_gone_session_maps_to_404(self):
        resp = self.http.post("/api/plugins/ao-bridge/sessions/ghost/steer",
                              json={"message": "hello"})
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["detail"]["kind"], "gone")


class SteerActivityTranscriptTest(SteerRouteTestBase):
    """Steered guidance rides the conversation as a ``system`` activity, not a
    message row (live steer probe, 2026-11-09). The detail payload must
    surface steer events — the sender has to see their own steered text —
    and must keep every other system event off the transcript."""

    def test_steer_system_activity_rides_the_detail_payload(self):
        self.ao.add_session(make_session("w1"))
        self.ao.set_activities("w1", [
            {"id": "a1", "activityKind": "system", "status": "completed",
             "sequence": 5, "summary": "internal event",
             "detail": {"event": "gc"}},
            {"id": "a2", "activityKind": "system", "status": "completed",
             "sequence": 6, "summary": "steer text",
             "detail": {"event": "steer", "origin": "human", "text": "steer text"}},
        ])
        body = self.http.get("/api/plugins/ao-bridge/sessions/w1/detail").json()
        steers = [a for a in body["activities"] if a["activityKind"] == "steer"]
        self.assertEqual(len(steers), 1)
        self.assertEqual(steers[0]["text"], "steer text")
        self.assertEqual(steers[0]["origin"], "human")
        self.assertEqual(steers[0]["sequence"], 6)
        # Non-steer system events stay off the payload.
        kinds = {a["activityKind"] for a in body["activities"]}
        self.assertNotIn("system", kinds)


class SteerClientTest(unittest.TestCase):
    """AOClient surface: the passthrough posts the daemon's wire shape."""

    def setUp(self):
        self.ao = FakeAO()
        self.server = FakeAOServer(self.ao)
        self.server.start()
        self.ao.add_session(make_session("w1", status="idle"))
        self.client = plugin_api.AOClient(self.server.url)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_steer_or_send_posts_text_and_handle(self):
        result = self.client.steer_or_send("w1", "hello", client_message_id="cm-1")
        self.assertEqual(result["outcome"], "sent")
        self.assertEqual(self.ao.steers[0]["session_id"], "w1")
        self.assertEqual(self.ao.steers[0]["body"],
                         {"text": "hello", "clientMessageId": "cm-1"})

    def test_daemon_error_carries_its_code(self):
        self.ao.steer_error = ("CHAT_STEER_UNSUPPORTED", 409, "cannot take guidance")
        with self.assertRaises(plugin_api.AOApiError) as ctx:
            self.client.steer_or_send("w1", "hello", client_message_id="cm-2")
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "CHAT_STEER_UNSUPPORTED")


if __name__ == "__main__":
    unittest.main()
