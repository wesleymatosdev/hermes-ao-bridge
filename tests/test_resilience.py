"""Resilience contracts (v0.2.8): version-skew detection and daemon-outage
degradation + recovery.

Two failure modes, both seen live on 2026-11-09:

1. Version skew. The dashboard imports ``plugin_api.py`` ONCE at web-server
   startup (``web_server_dashboard._mount_one_plugin_api`` -> ``import_user_code``)
   and never re-imports after a plugin update, while ``dist/index.js`` and
   ``manifest.json`` are served from disk per request. The backend must expose
   the version it was imported with so the frontend can compare it against the
   served manifest and surface "plugin updated — reload" instead of misbehaving
   silently.

2. Daemon outage. The AO daemon bouncing must not leave a stuck board: every
   route answers with a clean typed error payload (not a bare string detail),
   the WS stream tells connected clients the daemon is down (and that it is
   back), and nothing caches dead state — after the upstream returns, every
   route and the stream recover without a dashboard restart.
"""

import json
import os
import queue
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import conftest
from fake_ao_server import FakeAO, FakeAOServer, make_session

from fastapi import FastAPI
from fastapi.testclient import TestClient

plugin_api = conftest.load_plugin_api()

RECV_TIMEOUT_S = 6.0


def recv_json(ws, timeout_s=RECV_TIMEOUT_S):
    """receive_json without a native timeout: run it on a daemon thread and
    wait with a real deadline so a silent stream fails the test, not the run."""
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


def read_manifest_version() -> str:
    path = conftest._PLUGIN_DASHBOARD / "manifest.json"
    return str(json.loads(path.read_text()).get("version") or "")


class SkewBase(unittest.TestCase):
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


class PluginVersionExposureTest(SkewBase):
    """The loaded backend must identify itself: health and board payloads carry
    the manifest version as of the module's import — that is the only version
    the running process can truthfully claim."""

    def test_health_exposes_plugin_version(self):
        resp = self.http.get("/api/plugins/ao-bridge/health")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["pluginVersion"], read_manifest_version())

    def test_board_exposes_plugin_version(self):
        resp = self.http.get("/api/plugins/ao-bridge/board")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["pluginVersion"], read_manifest_version())

    def test_version_is_bound_at_import_not_read_per_request(self):
        # The skew mechanism in miniature: a module imported from a dashboard
        # dir reports that dir's manifest version as of ITS import and keeps
        # reporting it after the file on disk changes — so "the running backend
        # is older than the served frontend" is detectable, not invisible.
        staging = Path(tempfile.mkdtemp(prefix="ao-bridge-skew-"))
        try:
            for name in ("plugin_api.py", "manifest.json"):
                shutil.copy2(conftest._PLUGIN_DASHBOARD / name, staging)
            manifest_path = staging / "manifest.json"
            disk = json.loads(manifest_path.read_text())
            disk["version"] = "9.9.9-skew"
            manifest_path.write_text(json.dumps(disk))
            fresh = conftest.load_plugin_api_from(staging)
            self.assertEqual(fresh.PLUGIN_VERSION, "9.9.9-skew")
            self.assertNotEqual(fresh.PLUGIN_VERSION, plugin_api.PLUGIN_VERSION)
            # A plugin update lands on disk AFTER the import (the fast-forward
            # under a live dashboard): the loaded module must not follow it.
            disk["version"] = "8.8.8-later"
            manifest_path.write_text(json.dumps(disk))
            self.assertEqual(fresh.PLUGIN_VERSION, "9.9.9-skew")
        finally:
            shutil.rmtree(staging, ignore_errors=True)


class UpstreamDownErrorShapeTest(SkewBase):
    """With the daemon unreachable, every route answers a clean TYPED error
    payload — kind + retryable + message — so the UI can degrade in theme
    instead of parsing prose. Never a blank board or a bare string detail."""

    def _point_at_dead_upstream(self):
        self.server.shutdown()
        self.server.server_close()
        os.environ["AO_BRIDGE_BASE_URL"] = "http://127.0.0.1:1"

    def assert_typed_unreachable(self, resp):
        self.assertEqual(resp.status_code, 502, resp.text)
        detail = resp.json()["detail"]
        self.assertIsInstance(detail, dict, "error detail must be a typed payload")
        self.assertEqual(detail["kind"], "daemon_unreachable")
        self.assertTrue(detail["retryable"])
        self.assertTrue(str(detail["message"]).strip())
        self.assertTrue(str(detail.get("pluginVersion") or "").strip())
        # The probed address, so a first-run UI can say WHAT it tried to reach
        # (the daemon base URL is configurable via the ao_base_url setting).
        self.assertEqual(detail.get("baseUrl"), "http://127.0.0.1:1")

    def test_health_typed_error_when_upstream_down(self):
        self._point_at_dead_upstream()
        self.assert_typed_unreachable(
            self.http.get("/api/plugins/ao-bridge/health"))

    def test_board_typed_error_when_upstream_down(self):
        self._point_at_dead_upstream()
        self.assert_typed_unreachable(
            self.http.get("/api/plugins/ao-bridge/board"))

    def test_spawn_typed_error_when_upstream_down(self):
        self._point_at_dead_upstream()
        self.assert_typed_unreachable(
            self.http.post("/api/plugins/ao-bridge/spawn",
                           json={"projectId": "p", "harness": "hermes"}))

    def test_spawn_validation_precedes_upstream_error(self):
        # A request the plugin can reject locally must 422 even while the
        # daemon is down — the outage must not mask request validation.
        self._point_at_dead_upstream()
        resp = self.http.post("/api/plugins/ao-bridge/spawn", json={"prompt": "x"})
        self.assertEqual(resp.status_code, 422)

    def test_send_typed_error_when_upstream_down(self):
        self._point_at_dead_upstream()
        self.assert_typed_unreachable(
            self.http.post("/api/plugins/ao-bridge/sessions/s1/send",
                           json={"message": "hi"}))

    def test_send_empty_message_rejected_while_upstream_down(self):
        self._point_at_dead_upstream()
        resp = self.http.post("/api/plugins/ao-bridge/sessions/s1/send",
                              json={"message": "   "})
        self.assertEqual(resp.status_code, 422)

    def test_kill_typed_error_when_upstream_down(self):
        self._point_at_dead_upstream()
        self.assert_typed_unreachable(
            self.http.post("/api/plugins/ao-bridge/sessions/s1/kill"))

    def test_detail_typed_error_when_upstream_down(self):
        self._point_at_dead_upstream()
        self.assert_typed_unreachable(
            self.http.get("/api/plugins/ao-bridge/sessions/s1/detail"))


class UpstreamRecoveryTest(SkewBase):
    """No cached dead state: after the upstream returns, every route serves
    live data again on the SAME app instance (the dashboard never restarts)."""

    def _point_at_dead_upstream(self):
        self.server.shutdown()
        self.server.server_close()
        os.environ["AO_BRIDGE_BASE_URL"] = "http://127.0.0.1:1"

    def test_all_routes_recover_after_upstream_returns(self):
        self._point_at_dead_upstream()
        for method, url, kwargs in (
            ("get", "/api/plugins/ao-bridge/health", {}),
            ("get", "/api/plugins/ao-bridge/board", {}),
            ("post", "/api/plugins/ao-bridge/spawn", {"json": {"projectId": "p"}}),
            ("post", "/api/plugins/ao-bridge/sessions/s1/send", {"json": {"message": "hi"}}),
            ("post", "/api/plugins/ao-bridge/sessions/s1/kill", {}),
            ("get", "/api/plugins/ao-bridge/sessions/s1/detail", {}),
        ):
            resp = getattr(self.http, method)(url, **kwargs)
            self.assertEqual(resp.status_code, 502, f"{method} {url} while down")

        # The daemon "comes back": a fresh server, re-pointed by config.
        self.ao.add_session(make_session("s1", project_id="p"))
        self.server = FakeAOServer(self.ao)
        self.server.start()
        os.environ["AO_BRIDGE_BASE_URL"] = self.server.url

        health = self.http.get("/api/plugins/ao-bridge/health")
        self.assertEqual(health.status_code, 200)
        self.assertTrue(health.json()["ok"])
        board = self.http.get("/api/plugins/ao-bridge/board")
        self.assertEqual(board.status_code, 200)
        self.assertEqual([c["id"] for c in board.json()["cards"]], ["s1"])
        spawn = self.http.post("/api/plugins/ao-bridge/spawn",
                               json={"projectId": "p", "harness": "hermes"})
        self.assertEqual(spawn.status_code, 201)
        send = self.http.post("/api/plugins/ao-bridge/sessions/s1/send",
                              json={"message": "hi"})
        self.assertEqual(send.status_code, 200)
        detail = self.http.get("/api/plugins/ao-bridge/sessions/s1/detail")
        self.assertEqual(detail.status_code, 200)
        kill = self.http.post("/api/plugins/ao-bridge/sessions/s1/kill")
        self.assertEqual(kill.status_code, 200)


class EventsStreamOutageTest(SkewBase):
    """The WS stream must not go silent through an outage: connected clients
    hear a typed daemon-down frame (so a live-mode board can show the banner),
    the connection stays open, and the board frame flows again once the daemon
    is back — even when the card set did not change across the outage."""

    def test_error_frame_during_outage_then_board_frame_on_recovery(self):
        self.ao.add_session(make_session("s1"))
        self.ao.add_session(make_session("s2", status="merged"))
        with patch.object(plugin_api, "_ws_upgrade_authorized", return_value=True):
            with self.http.websocket_connect("/api/plugins/ao-bridge/events") as ws:
                self._run_outage(ws)

    def _run_outage(self, ws):
        first = recv_json(ws)
        self.assertEqual(first["type"], "board")
        self.assertIn("pluginVersion", first["board"])

        # Daemon goes down mid-stream.
        self.server.shutdown()
        self.server.server_close()
        t0 = time.monotonic()
        err = recv_json(ws)
        self.assertLess(time.monotonic() - t0, RECV_TIMEOUT_S)
        self.assertEqual(err["type"], "daemon_error")
        self.assertEqual(err["error"]["kind"], "daemon_unreachable")
        self.assertTrue(err["error"]["retryable"])
        self.assertTrue(str(err["error"].get("baseUrl") or "").strip())

        # Daemon comes back on a fresh server (the bouncer scenario), same
        # card set — the client must still hear a board frame so it can
        # clear the degraded state and restore live mode.
        self.server = FakeAOServer(self.ao)
        self.server.start()
        os.environ["AO_BRIDGE_BASE_URL"] = self.server.url
        back = recv_json(ws)
        self.assertEqual(back["type"], "board")
        self.assertEqual([c["id"] for c in back["board"]["cards"]], ["s1", "s2"])


if __name__ == "__main__":
    unittest.main()
