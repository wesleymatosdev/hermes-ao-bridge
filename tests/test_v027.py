"""v0.2.7 contracts: the detail payload's intra-turn fields (turnState +
activities tail) and the WS watch channel that powers the detail sheet's
live transcript follow ("watch" client frame -> "detail" pushes)."""

import os
import queue
import threading
import unittest
from unittest.mock import patch

import conftest
from fake_ao_server import FakeAO, FakeAOServer, make_session

from fastapi import FastAPI
from fastapi.testclient import TestClient

plugin_api = conftest.load_plugin_api()


class WsReader:
    """One pump thread per socket: frames land in a queue the test drains with
    deadlines. A per-read thread would strand a blocked reader on the socket
    every time a test proves "no message arrives", starving later reads."""

    def __init__(self, ws):
        self.q: "queue.Queue" = queue.Queue()
        threading.Thread(target=self._pump, args=(ws,), daemon=True).start()

    def _pump(self, ws):
        while True:
            try:
                item = ws.receive_json()
            except Exception as exc:  # noqa: BLE001 - surfaced to the reader
                self.q.put(exc)
                return
            self.q.put(item)

    def next(self, timeout_s=6.0):
        try:
            result = self.q.get(timeout=timeout_s)
        except queue.Empty:
            raise AssertionError(f"no WS message within {timeout_s}s")
        if isinstance(result, Exception):
            raise result
        return result

    def next_is_nothing(self, timeout_s=2.0):
        """Assert nothing arrives within the window (digest-gated silence)."""
        try:
            item = self.q.get(timeout=timeout_s)
        except queue.Empty:
            return
        raise AssertionError(f"unexpected WS message within {timeout_s}s: {item!r}")


def make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(plugin_api.router, prefix="/api/plugins/ao-bridge")
    return app


class DetailPayloadExtrasTest(unittest.TestCase):
    def setUp(self):
        self.ao = FakeAO()
        self.server = FakeAOServer(self.ao)
        self.server.start()
        self._old_env = os.environ.get("AO_BRIDGE_BASE_URL")
        os.environ["AO_BRIDGE_BASE_URL"] = self.server.url
        self.http = TestClient(make_app())
        self.ao.add_session(make_session("s1", project_id="hermes-agent",
                                         display_name="my task"))
        self.ao.set_conversation("s1", [
            {"role": "user", "text": "do the thing", "createdAt": "2026-01-01T00:00:00Z"},
            {"role": "assistant", "text": "OK", "createdAt": "2026-01-01T00:00:05Z"},
        ])
        self.ao.set_turns("s1", [{"id": "t1", "state": "running"}])
        self.ao.set_activities("s1", [
            {"activityKind": "plan", "status": "completed", "summary": "Plan",
             "sequence": 1, "revision": 1,
             "detail": {"steps": [{"status": "completed", "text": "step one"}]}},
            {"activityKind": "reasoning", "status": "running", "summary": "Reasoning",
             "sequence": 2, "revision": 3,
             "detail": {"text": "huge detail blob that must not ship"}},
        ])

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("AO_BRIDGE_BASE_URL", None)
        else:
            os.environ["AO_BRIDGE_BASE_URL"] = self._old_env
        self.server.shutdown()
        self.server.server_close()

    def test_detail_carries_turn_state_and_activity_tail(self):
        resp = self.http.get("/api/plugins/ao-bridge/sessions/s1/detail")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["turnState"], "running")
        # activities are the tool-execution rows (v0.3.0 shape): the plan
        # activity renders in the Plan panel, so only reasoning shows here.
        self.assertEqual(len(body["activities"]), 1)
        self.assertEqual(body["activities"][-1],
                         {"id": "", "activityKind": "reasoning", "status": "running",
                          "summary": "Reasoning", "sequence": 2,
                          "text": "huge detail blob that must not ship",
                          "textTruncated": False})

    def test_detail_activities_bounded_and_summary_only(self):
        self.ao.set_activities("s1", [
            {"activityKind": "command", "status": "completed", "summary": f"cmd {i}",
             "sequence": i, "revision": 1,
             "detail": {"text": "big"}} for i in range(plugin_api.ACTIVITY_TAIL_COUNT + 4)
        ])
        body = self.http.get("/api/plugins/ao-bridge/sessions/s1/detail").json()
        self.assertEqual(body["activitiesTotal"], plugin_api.ACTIVITY_TAIL_COUNT + 4)
        self.assertEqual(len(body["activities"]), plugin_api.ACTIVITY_TAIL_COUNT)

    def test_detail_without_turns_or_activities_is_empty_not_broken(self):
        self.ao.add_session(make_session("s2", project_id="hermes-agent"))
        self.ao.set_conversation("s2", [
            {"role": "user", "text": "hi", "createdAt": "2026-01-01T00:00:00Z"},
        ])
        body = self.http.get("/api/plugins/ao-bridge/sessions/s2/detail").json()
        self.assertEqual(body["turnState"], "")
        self.assertEqual(body["activities"], [])


class WatchChannelTest(unittest.TestCase):
    def setUp(self):
        self.ao = FakeAO()
        self.server = FakeAOServer(self.ao)
        self.server.start()
        self._old_env = os.environ.get("AO_BRIDGE_BASE_URL")
        os.environ["AO_BRIDGE_BASE_URL"] = self.server.url
        self.ao.project_rows = [{"id": "hermes-agent", "name": "hermes-agent", "path": "/x"}]
        self.ao.add_session(make_session("s1", project_id="hermes-agent", status="working"))
        self.ao.set_conversation("s1", [
            {"role": "user", "text": "do the thing", "createdAt": "2026-01-01T00:00:00Z"},
            {"role": "assistant", "text": "OK", "createdAt": "2026-01-01T00:00:05Z"},
        ])

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("AO_BRIDGE_BASE_URL", None)
        else:
            os.environ["AO_BRIDGE_BASE_URL"] = self._old_env
        self.server.shutdown()
        self.server.server_close()

    def _connect(self):
        # Patch the upgrade gate the same way test_v02 does.
        return TestClient(make_app()).websocket_connect(
            "/api/plugins/ao-bridge/events")

    def test_watch_pushes_detail_and_follows_new_messages(self):
        with patch.object(plugin_api, "_ws_upgrade_authorized", return_value=True):
            with self._connect() as ws:
                reader = WsReader(ws)
                first = reader.next()
                self.assertEqual(first["type"], "board")
                ws.send_json({"type": "watch", "sessionId": "s1"})
                detail = reader.next()
                self.assertEqual(detail["type"], "detail")
                self.assertEqual(detail["sessionId"], "s1")
                self.assertEqual([m["text"] for m in detail["detail"]["transcript"]],
                                 ["do the thing", "OK"])
                # Identical state -> no push; a new message -> pushed with the
                # transcript grown (digest-gated, within a poll cycle).
                self.ao.set_conversation("s1", self.ao.conversations["s1"] + [
                    {"role": "assistant", "text": "still working",
                     "createdAt": "2026-01-01T00:00:30Z"},
                ])
                follow = reader.next()
                self.assertEqual(follow["type"], "detail")
                self.assertEqual(follow["detail"]["transcript"][-1]["text"],
                                 "still working")

    def test_switching_watch_to_gone_session_drops_watch_without_push(self):
        with patch.object(plugin_api, "_ws_upgrade_authorized", return_value=True):
            with self._connect() as ws:
                reader = WsReader(ws)
                first = reader.next()
                self.assertEqual(first["type"], "board")
                ws.send_json({"type": "watch", "sessionId": "s1"})
                detail = reader.next()
                self.assertEqual(detail["type"], "detail")
                self.assertEqual(detail["sessionId"], "s1")
                # Re-target a session that doesn't exist: 404 -> watch dropped,
                # no detail push, stream keeps running.
                ws.send_json({"type": "watch", "sessionId": "other"})
                reader.next_is_nothing(timeout_s=2.5)

    def test_watch_of_gone_session_is_dropped_and_stream_survives(self):
        with patch.object(plugin_api, "_ws_upgrade_authorized", return_value=True):
            with self._connect() as ws:
                reader = WsReader(ws)
                reader.next()  # initial board
                ws.send_json({"type": "watch", "sessionId": "missing"})
                reader.next_is_nothing(timeout_s=2.5)  # 404 -> watch dropped
                # The stream still works: a board change must arrive.
                self.ao.add_session(make_session("s2", status="merged"))
                msg = reader.next()
                self.assertEqual(msg["type"], "board")


if __name__ == "__main__":
    unittest.main()
