"""v0.2 contracts: detail route (metadata + transcript tail) and the WS /events
live stream (server polls the daemon, pushes on change)."""

import json
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


def recv_json(ws, timeout_s=6.0):
    """receive_json without a native timeout: run it on a daemon thread and
    wait with a real deadline so a broken stream fails the test, not the run."""
    q: "queue.Queue" = queue.Queue()

    def run():
        try:
            q.put(ws.receive_json())
        except Exception as exc:  # noqa: BLE001 - surfaced to the assertion
            q.put(exc)

    threading.Thread(target=run, daemon=True).start()
    try:
        result = q.get(timeout=timeout_s)
    except queue.Empty:
        raise AssertionError(f"no WS message within {timeout_s}s")
    if isinstance(result, Exception):
        raise result
    return result


def make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(plugin_api.router, prefix="/api/plugins/ao-bridge")
    return app


def ws_messages(ws, count, timeout_s=5.0):
    out = []
    for _ in range(count):
        data = ws.receive_json(timeout=timeout_s)
        out.append(data)
    return out


class DetailRouteTest(unittest.TestCase):
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
            {"kind": "noise"},  # no text: dropped
        ])

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("AO_BRIDGE_BASE_URL", None)
        else:
            os.environ["AO_BRIDGE_BASE_URL"] = self._old_env
        self.server.shutdown()
        self.server.server_close()

    def test_detail_returns_metadata_and_transcript(self):
        resp = self.http.get("/api/plugins/ao-bridge/sessions/s1/detail")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])
        s = body["session"]
        self.assertEqual(s["id"], "s1")
        self.assertEqual(s["projectId"], "hermes-agent")
        self.assertEqual(s["aoLink"], "ao-app://sessions/hermes-agent/s1")
        self.assertEqual([m["text"] for m in body["transcript"]], ["do the thing", "OK"])
        self.assertFalse(body["transcriptTruncated"])

    def test_detail_standalone_ao_link_has_no_project(self):
        self.ao.add_session(make_session("s2"))
        resp = self.http.get("/api/plugins/ao-bridge/sessions/s2/detail")
        self.assertEqual(resp.json()["session"]["aoLink"], "ao-app://sessions/s2")

    def test_detail_truncates_transcript_to_tail(self):
        plugin_api.TRANSCRIPT_TAIL_LINES = 2
        self.ao.set_conversation("s1", [
            {"role": "user", "text": f"msg {i}", "createdAt": "2026-01-01T00:00:00Z"}
            for i in range(5)
        ])
        try:
            resp = self.http.get("/api/plugins/ao-bridge/sessions/s1/detail")
            body = resp.json()
            self.assertTrue(body["transcriptTruncated"])
            self.assertEqual(body["transcriptTotal"], 5)
            self.assertEqual([m["text"] for m in body["transcript"]], ["msg 3", "msg 4"])
        finally:
            plugin_api.TRANSCRIPT_TAIL_LINES = 200

    def test_detail_forwards_origin_and_authorship_flag(self):
        # Wire truth (probed on the live daemon, 2026-11-09): origin is the
        # reliable provenance signal — "human" only for app-typed sends,
        # "automation" for every REST /send call even when the sender passes
        # userAuthored: true (authoredByUser is self-declared). The detail
        # route must forward origin so the renderer never labels an
        # orchestrator send "End user".
        self.ao.set_conversation("s1", [
            {"role": "user", "text": "typed in the AO app",
             "createdAt": "2026-01-01T00:00:00Z", "origin": "human"},
            {"role": "assistant", "text": "worker output",
             "createdAt": "2026-01-01T00:00:05Z", "origin": "provider"},
            {"role": "user", "text": "orchestrator-injected addendum",
             "createdAt": "2026-01-01T00:00:10Z", "origin": "automation",
             "authoredByUser": True},
        ])
        resp = self.http.get("/api/plugins/ao-bridge/sessions/s1/detail")
        transcript = resp.json()["transcript"]
        self.assertEqual(
            [(m["text"], m["origin"], m["authoredByUser"]) for m in transcript],
            [("typed in the AO app", "human", False),
             ("worker output", "provider", False),
             ("orchestrator-injected addendum", "automation", True)])

    def test_detail_gone_session_maps_to_404(self):
        resp = self.http.get("/api/plugins/ao-bridge/sessions/none/detail")
        self.assertEqual(resp.status_code, 404)


class EventsWebSocketTest(unittest.TestCase):
    def setUp(self):
        self.ao = FakeAO()
        self.server = FakeAOServer(self.ao)
        self.server.start()
        self._old_env = os.environ.get("AO_BRIDGE_BASE_URL")
        os.environ["AO_BRIDGE_BASE_URL"] = self.server.url

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("AO_BRIDGE_BASE_URL", None)
        else:
            os.environ["AO_BRIDGE_BASE_URL"] = self._old_env
        self.server.shutdown()
        self.server.server_close()

    def test_events_stream_pushes_board_on_change(self):
        self.ao.project_rows = [{"id": "hermes-agent", "name": "hermes-agent", "path": "/x"}]
        self.ao.add_session(make_session("s1", project_id="hermes-agent", status="working"))
        with patch.object(plugin_api, "_ws_upgrade_authorized", return_value=True):
            with TestClient(make_app()).websocket_connect(
                    "/api/plugins/ao-bridge/events") as ws:
                # Initial snapshot arrives on the first poll cycle (<= ~2s).
                first = recv_json(ws)
                self.assertEqual(first["type"], "board")
                self.assertEqual(first["board"]["counts"]["running"], 1)
                # No change -> no push; then a state change -> pushed within 2s.
                self.ao.add_session(make_session("s2", project_id="hermes-agent",
                                                 status="merged"))
                second = recv_json(ws)
                self.assertEqual(second["type"], "board")
                self.assertEqual(second["board"]["counts"]["done"], 1)
                self.assertEqual(len(second["board"]["cards"]), 2)

    def test_events_stream_ignores_daemon_blips(self):
        self.ao.add_session(make_session("s1"))
        self.ao.healthz_ok = False  # board route fails; stream must survive
        with patch.object(plugin_api, "_ws_upgrade_authorized", return_value=True):
            with TestClient(make_app()).websocket_connect(
                    "/api/plugins/ao-bridge/events") as ws:
                self.ao.healthz_ok = True
                self.ao.add_session(make_session("s2", status="working"))
                msg = recv_json(ws)
                self.assertEqual(msg["type"], "board")

    def test_events_ws_rejected_without_upgrade_auth(self):
        with patch.object(plugin_api, "_ws_upgrade_authorized", return_value=False):
            # The server closes before accept, so the connect itself raises.
            with self.assertRaises(Exception):
                with TestClient(make_app()).websocket_connect(
                        "/api/plugins/ao-bridge/events") as ws:
                    recv_json(ws, timeout_s=3)


if __name__ == "__main__":
    unittest.main()
