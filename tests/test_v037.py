"""v0.3.7 contracts: the ao-app:// deep-link scheme (builder + single
constant), the open_in_ao_links flag that gates the interim "Open in AO"
workaround buttons, and the orchestrator split — kind=orchestrator sessions
leave the worker columns/counters and ride the board payload's
``orchestrators`` list instead.

The ao-app scheme is verified against the installed AO desktop app's
Info.plist (0.14.11-nightly registers ONLY "ao-app"); the orchestrator split
against the live daemon's session payload (kind ∈ {worker, orchestrator}).
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


class DeepLinkSchemeTest(unittest.TestCase):
    def test_scheme_is_the_single_constant(self):
        # The whole point of the constant: every deep link — backend helper and
        # payload fields alike — flows through AO_DEEP_LINK_SCHEME.
        self.assertEqual(plugin_api.AO_DEEP_LINK_SCHEME, "ao-app")
        self.assertTrue(plugin_api.ao_deep_link("p", "s").startswith(
            plugin_api.AO_DEEP_LINK_SCHEME + "://sessions/"))

    def test_detail_ao_link_uses_the_scheme(self):
        old = os.environ.get("AO_BRIDGE_BASE_URL")
        ao = FakeAO()
        server = FakeAOServer(ao)
        server.start()
        os.environ["AO_BRIDGE_BASE_URL"] = server.url
        try:
            http = TestClient(make_app())
            ao.add_session(make_session("s1", project_id="hermes-agent"))
            body = http.get("/api/plugins/ao-bridge/sessions/s1/detail").json()
            self.assertEqual(body["session"]["aoLink"],
                             "ao-app://sessions/hermes-agent/s1")
        finally:
            if old is None:
                os.environ.pop("AO_BRIDGE_BASE_URL", None)
            else:
                os.environ["AO_BRIDGE_BASE_URL"] = old
            server.shutdown()
            server.server_close()


class OpenInAoLinksFlagTest(unittest.TestCase):
    """The Open in AO buttons are the interim workaround (AO opens; the session
    id goes to the clipboard for Search ⌘K) — gated off by default until AO
    ships session deep links."""

    def setUp(self):
        self.ao = FakeAO()
        self.server = FakeAOServer(self.ao)
        self.server.start()
        self._old_env = os.environ.pop("AO_BRIDGE_OPEN_LINKS", None)
        # Deterministic against the developer's real config.yaml: the config
        # read must answer "unset" here.
        self._old_cfg = plugin_api._config_setting
        plugin_api._config_setting = lambda key: None
        os.environ["AO_BRIDGE_BASE_URL"] = self.server.url
        self.http = TestClient(make_app())
        self.ao.add_session(make_session("w1", project_id="hermes-agent"))

    def tearDown(self):
        plugin_api._config_setting = self._old_cfg
        if self._old_env is None:
            os.environ.pop("AO_BRIDGE_OPEN_LINKS", None)
        else:
            os.environ["AO_BRIDGE_OPEN_LINKS"] = self._old_env
        os.environ.pop("AO_BRIDGE_BASE_URL", None)
        self.server.shutdown()
        self.server.server_close()

    def test_default_is_off(self):
        self.assertFalse(plugin_api.open_in_ao_links())

    def test_env_override_flips_it(self):
        os.environ["AO_BRIDGE_OPEN_LINKS"] = "1"
        self.assertTrue(plugin_api.open_in_ao_links())
        os.environ["AO_BRIDGE_OPEN_LINKS"] = "not-a-bool"
        self.assertFalse(plugin_api.open_in_ao_links())

    def test_board_payload_carries_the_flag(self):
        self.assertFalse(
            self.http.get("/api/plugins/ao-bridge/board").json()["openInAoLinks"])
        os.environ["AO_BRIDGE_OPEN_LINKS"] = "true"
        self.assertTrue(
            self.http.get("/api/plugins/ao-bridge/board").json()["openInAoLinks"])


class OrchestratorSplitTest(unittest.TestCase):
    def setUp(self):
        self.ao = FakeAO()
        self.server = FakeAOServer(self.ao)
        self.server.start()
        self._old_env = os.environ.get("AO_BRIDGE_BASE_URL")
        os.environ["AO_BRIDGE_BASE_URL"] = self.server.url
        self.http = TestClient(make_app())
        self.ao.add_session(make_session("w1", project_id="hermes-agent",
                                         status="working"))
        orch = make_session("o1", project_id="hermes-agent", status="working")
        orch["kind"] = "orchestrator"
        self.ao.add_session(orch)

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("AO_BRIDGE_BASE_URL", None)
        else:
            os.environ["AO_BRIDGE_BASE_URL"] = self._old_env
        self.server.shutdown()
        self.server.server_close()

    def test_orchestrators_leave_worker_columns_and_counts(self):
        body = self.http.get("/api/plugins/ao-bridge/board").json()
        card_ids = [c["id"] for c in body["cards"]]
        self.assertNotIn("o1", card_ids)
        self.assertIn("w1", card_ids)
        # The counter must count workers only: one running session, not two.
        self.assertEqual(body["counts"]["running"], 1)

    def test_orchestrators_ride_their_own_payload_list(self):
        body = self.http.get("/api/plugins/ao-bridge/board").json()
        orch = [c for c in body["orchestrators"] if c["id"] == "o1"]
        self.assertEqual(len(orch), 1)
        self.assertEqual(orch[0]["kind"], "orchestrator")
        # The card shape is shared — the strip renders the same fields.
        self.assertEqual(orch[0]["project"], "hermes-agent")
        self.assertEqual(orch[0]["column"], "running")

    def test_ws_board_frames_carry_the_split(self):
        # The strip updates live only if the stream's board frames carry it.
        from unittest.mock import patch
        with patch.object(plugin_api, "_ws_upgrade_authorized", return_value=True):
            with self.http.websocket_connect(
                    "/api/plugins/ao-bridge/events") as ws:
                frame = ws.receive_json()
                self.assertEqual(frame["type"], "board")
                board = frame["board"]
                self.assertEqual([c["id"] for c in board["orchestrators"]], ["o1"])
                self.assertNotIn("o1", [c["id"] for c in board["cards"]])


if __name__ == "__main__":
    unittest.main()
