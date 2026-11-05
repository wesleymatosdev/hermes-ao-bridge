"""Router contracts for dashboard/plugin_api.py via the FastAPI TestClient.

Mounts the router exactly as the dashboard server does (``/api/plugins/ao-bridge/``)
and drives it against the in-process fake AO daemon.
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


class RouterTestBase(unittest.TestCase):
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


class HealthRouteTest(RouterTestBase):
    def test_health_reports_base_url_and_daemon(self):
        resp = self.http.get("/api/plugins/ao-bridge/health")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["baseUrl"], self.server.url)
        self.assertEqual(body["daemon"]["status"], "ok")

    def test_unreachable_daemon_maps_to_502(self):
        os.environ["AO_BRIDGE_BASE_URL"] = "http://127.0.0.1:1"
        fresh = TestClient(make_app())
        resp = fresh.get("/api/plugins/ao-bridge/health")
        self.assertEqual(resp.status_code, 502)


class BoardRouteTest(RouterTestBase):
    def test_board_assembles_projects_and_cards(self):
        self.ao.project_rows = [{"id": "hermes-agent", "name": "hermes-agent", "path": "/x"},
                                {"id": "stale", "name": "stale", "path": "/gone",
                                 "folderMissing": True}]
        self.ao.add_session(make_session("s1", project_id="hermes-agent", status="merged"))
        self.ao.add_session(make_session("s2", status="working"))
        resp = self.http.get("/api/plugins/ao-bridge/board")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(len(body["projects"]), 2)
        self.assertTrue(body["projects"][1]["folderMissing"])
        self.assertEqual(body["counts"]["done"], 1)
        self.assertEqual(body["counts"]["running"], 1)
        cards = {c["id"]: c for c in body["cards"]}
        self.assertEqual(cards["s1"]["project"], "hermes-agent")
        self.assertIn("harnesses", body)
        self.assertIn("hermes", body["harnesses"])

    def test_board_down_daemon_maps_to_502(self):
        self.ao.healthz_ok = False  # daemon gone mid-poll
        os.environ["AO_BRIDGE_BASE_URL"] = "http://127.0.0.1:1"
        fresh = TestClient(make_app())
        resp = fresh.get("/api/plugins/ao-bridge/board")
        self.assertEqual(resp.status_code, 502)


class SpawnRouteTest(RouterTestBase):
    def test_spawn_forwards_to_ao(self):
        resp = self.http.post("/api/plugins/ao-bridge/spawn", json={
            "projectId": "hermes-agent", "harness": "hermes",
            "displayName": "demo", "prompt": "say hi"})
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(resp.json()["session"]["id"].startswith("s-fake-"))
        body = self.ao.created[0]
        self.assertEqual(body["projectId"], "hermes-agent")
        self.assertEqual(body["kind"], "worker")

    def test_spawn_requires_project_or_name(self):
        resp = self.http.post("/api/plugins/ao-bridge/spawn", json={"prompt": "x"})
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(self.ao.created, [])


class SendAndKillRouteTest(RouterTestBase):
    def test_send_forwards_message(self):
        self.ao.add_session(make_session("s1"))
        resp = self.http.post("/api/plugins/ao-bridge/sessions/s1/send",
                              json={"message": "hi", "userAuthored": True})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.ao.sends[0]["body"]["message"], "hi")

    def test_send_empty_message_rejected(self):
        self.ao.add_session(make_session("s1"))
        resp = self.http.post("/api/plugins/ao-bridge/sessions/s1/send", json={"message": "  "})
        self.assertEqual(resp.status_code, 422)

    def test_send_gone_session_maps_to_404(self):
        resp = self.http.post("/api/plugins/ao-bridge/sessions/none/send",
                              json={"message": "hi"})
        self.assertEqual(resp.status_code, 404)

    def test_send_terminated_session_maps_to_409(self):
        self.ao.add_session(make_session("s1", terminated=True))
        resp = self.http.post("/api/plugins/ao-bridge/sessions/s1/send",
                              json={"message": "hi"})
        self.assertEqual(resp.status_code, 409)

    def test_kill_forwards(self):
        self.ao.add_session(make_session("s1"))
        resp = self.http.post("/api/plugins/ao-bridge/sessions/s1/kill")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.ao.killed, ["s1"])


if __name__ == "__main__":
    unittest.main()
