"""Behaviour contracts for the AO REST client in plugin_api (stdlib urllib)."""

import os
import unittest

import conftest
from fake_ao_server import FakeAO, FakeAOServer, make_session

plugin_api = conftest.load_plugin_api()

AOClient = plugin_api.AOClient
AOApiError = plugin_api.AOApiError
AOConnectionError = plugin_api.AOConnectionError


class ClientTestBase(unittest.TestCase):
    def setUp(self):
        self.ao = FakeAO()
        self.server = FakeAOServer(self.ao)
        self.server.start()
        self._old_env = os.environ.get("AO_BRIDGE_BASE_URL")
        os.environ["AO_BRIDGE_BASE_URL"] = self.server.url
        self.client = AOClient(self.server.url)

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("AO_BRIDGE_BASE_URL", None)
        else:
            os.environ["AO_BRIDGE_BASE_URL"] = self._old_env
        self.server.shutdown()
        self.server.server_close()


class HealthzTest(ClientTestBase):
    def test_healthz_reachable(self):
        resp = self.client.healthz()
        self.assertEqual(resp["ok"], True)
        self.assertEqual(resp["status"], "ok")

    def test_healthz_down_maps_to_connection_error(self):
        self.ao.healthz_ok = False
        with self.assertRaises(AOApiError):
            self.client.healthz()

    def test_unreachable_raises_connection_error(self):
        dead = AOClient("http://127.0.0.1:1")  # nothing listens on port 1
        with self.assertRaises(AOConnectionError):
            dead.healthz()


class ProjectsAndSessionsTest(ClientTestBase):
    def test_projects_list_parses(self):
        self.ao.project_rows = [{"id": "hermes-agent", "name": "hermes-agent", "path": "/x"}]
        projects = self.client.projects()
        self.assertEqual(projects[0]["id"], "hermes-agent")

    def test_sessions_list_parses(self):
        self.ao.add_session(make_session("s1", project_id="personal"))
        sessions = self.client.sessions()
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["projectId"], "personal")

    def test_get_session_and_gone_detection(self):
        self.ao.add_session(make_session("s1"))
        self.assertEqual(self.client.get_session("s1")["isTerminated"], False)
        with self.assertRaises(AOApiError) as ctx:
            self.client.get_session("s-missing")
        self.assertTrue(ctx.exception.is_gone)

    def test_get_session_unwraps_daemon_envelope(self):
        self.ao.add_session(make_session("s1", project_id="hermes-agent"))
        self.ao.sessions_by_id["s1"] = {"session": self.ao.sessions_by_id["s1"]}
        row = self.client.get_session("s1")
        self.assertEqual(row["projectId"], "hermes-agent")

    def test_conversation_parses_messages(self):
        self.ao.add_session(make_session("s1"))
        self.ao.set_conversation("s1", [{"role": "user", "text": "hi"}])
        conv = self.client.conversation("s1")
        self.assertEqual(conv["messages"][0]["text"], "hi")
        with self.assertRaises(AOApiError) as ctx:
            self.client.conversation("s-missing")
        self.assertTrue(ctx.exception.is_gone)


class SpawnTest(ClientTestBase):
    def test_spawn_sends_worker_payload(self):
        resp = self.client.spawn(display_name="bridge demo", prompt="do a thing",
                                 project_id="hermes-agent", harness="hermes")
        self.assertTrue(resp["session"]["id"].startswith("s-fake-"))
        body = self.ao.created[0]
        self.assertEqual(body["kind"], "worker")
        self.assertEqual(body["mode"], "chat")
        self.assertEqual(body["harness"], "hermes")
        self.assertEqual(body["projectId"], "hermes-agent")
        self.assertEqual(body["displayName"], "bridge demo")
        self.assertEqual(body["prompt"], "do a thing")

    def test_spawn_without_project_omits_project_id(self):
        self.client.spawn(display_name="standalone")
        self.assertNotIn("projectId", self.ao.created[0])
        self.assertNotIn("prompt", self.ao.created[0])


class SendAndKillTest(ClientTestBase):
    def test_send_posts_user_authored_message(self):
        self.ao.add_session(make_session("s1"))
        resp = self.client.send("s1", "hello board")
        self.assertTrue(resp["ok"])
        seen = self.ao.sends[0]
        self.assertEqual(seen["body"], {"message": "hello board", "userAuthored": True})

    def test_send_terminated_maps_to_conflict(self):
        self.ao.add_session(make_session("s1", terminated=True))
        with self.assertRaises(AOApiError) as ctx:
            self.client.send("s1", "x")
        self.assertEqual(ctx.exception.status, 409)

    def test_send_gone_maps_to_is_gone(self):
        with self.assertRaises(AOApiError) as ctx:
            self.client.send("s-missing", "x")
        self.assertTrue(ctx.exception.is_gone)

    def test_send_server_error_maps_to_transient(self):
        self.ao.add_session(make_session("s1"))
        self.ao.send_status = 503
        with self.assertRaises(AOApiError) as ctx:
            self.client.send("s1", "x")
        self.assertTrue(ctx.exception.is_transient)

    def test_kill_marks_session_terminated(self):
        self.ao.add_session(make_session("s1"))
        self.client.kill("s1")
        self.assertEqual(self.ao.killed, ["s1"])
        self.assertTrue(self.ao.sessions_by_id["s1"]["isTerminated"])


if __name__ == "__main__":
    unittest.main()
