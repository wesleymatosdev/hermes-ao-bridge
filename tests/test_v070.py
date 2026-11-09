"""v0.6.0 contracts: the terminal-view feature.

Backend surface under test:
- detail payload carries terminalHandleId + the terminalView gate
- WS /sessions/{id}/terminal proxies frames verbatim to the daemon's /mux,
  scoped to the session's PTY handle (open/data/resize passthrough, scoped-id
  refusal, daemon pushes, exited)
- gate + lookup failures close the socket with the typed frames/codes

The daemon mux is faked in-process (websockets.serve on a thread) speaking
the real wire protocol (ch-tagged JSON, base64 PTY bytes) — same shape the AO
backend's own terminal_mux_test.go exercises.
"""

import base64
import json
import os
import threading
import unittest
from unittest.mock import patch

import websockets
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import conftest
from fake_ao_server import FakeAO, FakeAOServer, make_session

plugin_api = conftest.load_plugin_api()


def make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(plugin_api.router, prefix="/api/plugins/ao-bridge")
    return app


class FakeMux:
    """In-process fake of the AO daemon's /mux WebSocket (real framing).

    Behaves like the daemon's Manager: open → opened + a repaint data frame;
    data frames echo back; every client frame is recorded for assertions.
    """

    def __init__(self):
        self.received = []          # parsed client frames, in order
        self.clients = []           # server-side connections (for pushes)
        self.server = None
        self.loop = None
        self.port = None
        self._ready = threading.Event()

    async def _handler(self, ws):
        self.clients.append(ws)
        try:
            async for raw in ws:
                frame = json.loads(raw)
                self.received.append(frame)
                if frame.get("type") == "open":
                    tid = frame["id"]
                    await ws.send(json.dumps({"ch": "terminal", "type": "opened", "id": tid}))
                    await ws.send(json.dumps({
                        "ch": "terminal", "type": "data", "id": tid,
                        "data": base64.b64encode(b"ready\r\n").decode()}))
                elif frame.get("type") == "data":
                    # Echo: proves the raw bytes survived the round trip.
                    await ws.send(json.dumps({
                        "ch": "terminal", "type": "data", "id": frame["id"],
                        "data": frame["data"]}))
        except websockets.ConnectionClosed:
            pass

    def start(self):
        import asyncio

        self.loop = asyncio.new_event_loop()

        async def main():
            import websockets.asyncio.server

            self.server = await websockets.asyncio.server.serve(
                self._handler, "127.0.0.1", 0)
            self.port = self.server.sockets[0].getsockname()[1]

        def run():
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(main())
            self._ready.set()
            self.loop.run_forever()

        threading.Thread(target=run, daemon=True).start()
        assert self._ready.wait(5), "fake mux did not start"

    def push(self, frame: dict, wait_for_client_s: float = 5.0) -> None:
        """Server→client push on the first live connection.

        Waits for the proxy's upstream connection first: the route dials /mux
        asynchronously after the browser handshake, and a push racing that
        dial would otherwise silently no-op (the race this wait kills).
        """
        import asyncio
        import time

        deadline = time.monotonic() + wait_for_client_s
        while not self.clients and time.monotonic() < deadline:
            time.sleep(0.02)
        if not self.clients:
            raise AssertionError("no proxy connection reached the fake mux")

        async def do_push():
            await self.clients[0].send(json.dumps(frame))

        asyncio.run_coroutine_threadsafe(do_push(), self.loop).result(5)

    def stop(self):
        import asyncio

        async def aclose():
            self.server.close()
            await self.server.wait_closed()

        asyncio.run_coroutine_threadsafe(aclose(), self.loop).result(5)
        self.loop.call_soon_threadsafe(self.loop.stop)


class TerminalRouteTestBase(unittest.TestCase):
    def setUp(self):
        self.ao = FakeAO()
        self.server = FakeAOServer(self.ao)
        self.server.start()
        self._old_env = os.environ.get("AO_BRIDGE_BASE_URL")
        os.environ["AO_BRIDGE_BASE_URL"] = self.server.url
        # The dashboard's canonical WS gate demands a real auth ticket when
        # hermes_cli is importable (as in the Hermes venv); every WS test in
        # this suite patches it the same way.
        self._auth_patcher = patch.object(plugin_api, "_ws_upgrade_authorized",
                                          return_value=True)
        self._auth_patcher.start()
        self.mux = FakeMux()
        self.mux.start()
        # The proxy derives the mux URL from the REST base; the fakes live on
        # separate ports, so point the URL builder at the fake mux (call-time
        # module-global lookup — the same seam prod resolves through).
        self._old_mux_url = plugin_api._mux_ws_url
        plugin_api._mux_ws_url = lambda base: f"ws://127.0.0.1:{self.mux.port}/mux"
        self.http = TestClient(make_app())

    def tearDown(self):
        self._auth_patcher.stop()
        plugin_api._mux_ws_url = self._old_mux_url
        self.mux.stop()
        if self._old_env is None:
            os.environ.pop("AO_BRIDGE_BASE_URL", None)
        else:
            os.environ["AO_BRIDGE_BASE_URL"] = self._old_env
        self.server.shutdown()
        self.server.server_close()

    def tearDown(self):
        plugin_api._mux_ws_url = self._old_mux_url
        self.mux.stop()
        if self._old_env is None:
            os.environ.pop("AO_BRIDGE_BASE_URL", None)
        else:
            os.environ["AO_BRIDGE_BASE_URL"] = self._old_env
        self.server.shutdown()
        self.server.server_close()


class DetailPayloadTerminalFieldsTest(TerminalRouteTestBase):
    def test_detail_carries_handle_and_gate(self):
        s = make_session("t1", status="working")
        s["terminalHandleId"] = "ptyhost-v1:t1"
        self.ao.add_session(s)
        self.ao.add_session(make_session("headless", status="working"))
        body = self.http.get("/api/plugins/ao-bridge/sessions/t1/detail").json()
        self.assertEqual(body["session"]["terminalHandleId"], "ptyhost-v1:t1")
        self.assertTrue(body["session"]["terminalView"])

    def test_headless_session_has_empty_handle(self):
        self.ao.add_session(make_session("headless", status="working"))
        body = self.http.get("/api/plugins/ao-bridge/sessions/headless/detail").json()
        self.assertEqual(body["session"]["terminalHandleId"], "")
        self.assertTrue(body["session"]["terminalView"])

    def test_tui_session_conversation_409_degrades_to_meta_and_terminal(self):
        # Ground truth (live daemon): tui-mode sessions — the ones WITH a
        # terminal — answer 409 on the conversation surface. The sheet must
        # still load (metadata + Terminal tab) instead of erroring wholesale.
        s = make_session("tui1", status="working")
        s["terminalHandleId"] = "ptyhost-v1:tui1"
        self.ao.add_session(s)
        self.ao.conversation_status = 409
        body = self.http.get("/api/plugins/ao-bridge/sessions/tui1/detail").json()
        self.assertEqual(body["session"]["terminalHandleId"], "ptyhost-v1:tui1")
        self.assertEqual(body["transcript"], [])
        self.assertIn("Terminal UI mode", body["conversationNote"])

    def test_conversation_outage_still_fails_the_detail(self):
        # The degrade is only for the daemon's typed refusal — a daemon outage
        # must not silently render an empty sheet as if nothing were wrong.
        s = make_session("tui2", status="working")
        self.ao.add_session(s)
        self.ao.conversation_status = 503
        resp = self.http.get("/api/plugins/ao-bridge/sessions/tui2/detail")
        # The error mapper preserves the upstream status; anything but 2xx
        # proves the outage still fails the sheet.
        self.assertEqual(resp.status_code, 503)


class TerminalGateTest(TerminalRouteTestBase):
    def test_disabled_feature_closes_4403(self):
        os.environ["AO_BRIDGE_TERMINAL_VIEW"] = "0"
        try:
            s = make_session("t1", status="working")
            s["terminalHandleId"] = "ptyhost-v1:t1"
            self.ao.add_session(s)
            with self.assertRaises(WebSocketDisconnect) as ctx:
                with self.http.websocket_connect(
                        "/api/plugins/ao-bridge/sessions/t1/terminal"):
                    pass
            self.assertEqual(ctx.exception.code, 4403)
        finally:
            os.environ.pop("AO_BRIDGE_TERMINAL_VIEW", None)
        self.assertTrue(plugin_api.terminal_view_enabled())  # default restored

    def test_unknown_session_closes_4404_with_error_frame(self):
        with self.http.websocket_connect(
                "/api/plugins/ao-bridge/sessions/ghost/terminal") as ws:
            frame = json.loads(ws.receive_text())
            self.assertEqual(frame["ch"], "terminal")
            self.assertEqual(frame["type"], "error")
            with self.assertRaises(WebSocketDisconnect) as ctx:
                ws.receive_text()
        self.assertEqual(ctx.exception.code, 4404)

    def test_headless_session_answers_error_then_closes_cleanly(self):
        self.ao.add_session(make_session("headless", status="working"))
        with self.http.websocket_connect(
                "/api/plugins/ao-bridge/sessions/headless/terminal") as ws:
            frame = json.loads(ws.receive_text())
            self.assertEqual(frame["type"], "error")
            self.assertIn("no live terminal", frame["error"])
            with self.assertRaises(WebSocketDisconnect) as ctx:
                ws.receive_text()
        self.assertEqual(ctx.exception.code, 1000)


class TerminalProxyPassthroughTest(TerminalRouteTestBase):
    def setUp(self):
        super().setUp()
        s = make_session("t1", status="working")
        s["terminalHandleId"] = "ptyhost-v1:t1"
        self.ao.add_session(s)
        self.handle = "ptyhost-v1:t1"
        self.ws = self.http.websocket_connect(
            "/api/plugins/ao-bridge/sessions/t1/terminal")
        self.term = self.ws.__enter__()

    def tearDown(self):
        try:
            self.term.close()
        except Exception:
            pass
        self.ws.__exit__(None, None, None)
        super().tearDown()

    def _recv(self):
        return json.loads(self.term.receive_text())

    def _attach(self):
        """Full attach handshake, drained: open → opened → repaint."""
        self.term.send_text(json.dumps(
            {"ch": "terminal", "type": "open", "id": self.handle, "cols": 80, "rows": 24}))
        self.assertEqual(self._recv()["type"], "opened")
        self.assertEqual(self._recv()["type"], "data")

    def test_open_attaches_and_receives_repaint(self):
        self.term.send_text(json.dumps(
            {"ch": "terminal", "type": "open", "id": self.handle, "cols": 80, "rows": 24}))
        opened = self._recv()
        self.assertEqual((opened["ch"], opened["type"], opened["id"]),
                         ("terminal", "opened", self.handle))
        repaint = self._recv()
        self.assertEqual(repaint["type"], "data")
        self.assertEqual(base64.b64decode(repaint["data"]), b"ready\r\n")
        open_frame = self.mux.received[0]
        self.assertEqual(open_frame["type"], "open")
        self.assertEqual((open_frame["id"], open_frame["cols"], open_frame["rows"]),
                         (self.handle, 80, 24))

    def test_keystrokes_reach_the_daemon_and_echo_returns(self):
        payload = base64.b64encode("sleep 5 && echo hello\r".encode()).decode()
        self.term.send_text(json.dumps(
            {"ch": "terminal", "type": "data", "id": self.handle, "data": payload}))
        echo = self._recv()
        self.assertEqual(base64.b64decode(echo["data"]).decode(), "sleep 5 && echo hello\r")
        sent = self.mux.received[0]
        self.assertEqual(base64.b64decode(sent["data"]).decode(), "sleep 5 && echo hello\r")

    def test_foreign_terminal_id_is_refused_and_never_forwarded(self):
        before = len(self.mux.received)
        self.term.send_text(json.dumps(
            {"ch": "terminal", "type": "data", "id": "ptyhost-v1:OTHER", "data": "aGk="}))
        frame = self._recv()
        self.assertEqual(frame["type"], "error")
        self.assertEqual(frame["id"], "ptyhost-v1:OTHER")
        self.assertIn("scoped", frame["error"])
        self.assertEqual(len(self.mux.received), before)  # nothing left the proxy

    def test_resize_is_forwarded_verbatim(self):
        self.term.send_text(json.dumps(
            {"ch": "terminal", "type": "resize", "id": self.handle,
             "cols": 120, "rows": 40}))
        deadline = __import__("time").time() + 5
        while __import__("time").time() < deadline and not self.mux.received:
            __import__("time").sleep(0.02)
        self.assertEqual(self.mux.received[0]["type"], "resize")
        self.assertEqual((self.mux.received[0]["cols"], self.mux.received[0]["rows"]),
                         (120, 40))

    def test_daemon_pushes_reach_the_browser(self):
        self._attach()
        self.mux.push({"ch": "terminal", "type": "exited", "id": self.handle})
        frame = self._recv()
        self.assertEqual((frame["ch"], frame["type"], frame["id"]),
                         ("terminal", "exited", self.handle))

    def test_authoritative_resize_push_reaches_the_browser(self):
        self._attach()
        self.mux.push({"ch": "terminal", "type": "resize", "id": self.handle,
                       "cols": 100, "rows": 30})
        frame = self._recv()
        self.assertEqual(frame["type"], "resize")
        self.assertEqual((frame["cols"], frame["rows"]), (100, 30))

    def test_non_terminal_channel_frames_are_dropped(self):
        before = len(self.mux.received)
        self.term.send_text(json.dumps({"ch": "subscribe", "type": "subscribe"}))
        self.term.send_text("not json at all")
        self.term.send_text(json.dumps(
            {"ch": "terminal", "type": "data", "id": self.handle,
             "data": base64.b64encode(b"still alive\r").decode()}))
        echo = self._recv()
        self.assertEqual(base64.b64decode(echo["data"]).decode(), "still alive\r")
        self.assertEqual(len(self.mux.received), before + 1)  # only the data frame


class TerminalProxyLifecycleTest(TerminalRouteTestBase):
    def test_daemon_down_answers_error_frame_and_closes(self):
        s = make_session("t1", status="working")
        s["terminalHandleId"] = "ptyhost-v1:t1"
        self.ao.add_session(s)
        # Point the mux URL at a dead port: session lookup (REST fake) works,
        # the daemon socket does not.
        plugin_api._mux_ws_url = lambda base: "ws://127.0.0.1:1/mux"
        with self.http.websocket_connect(
                "/api/plugins/ao-bridge/sessions/t1/terminal") as ws:
            frame = json.loads(ws.receive_text())
            self.assertEqual(frame["type"], "error")
            self.assertIn("failed", frame["error"])
            with self.assertRaises(WebSocketDisconnect) as ctx:
                ws.receive_text()
        self.assertEqual(ctx.exception.code, 1011)


class TerminalViewFlagTest(unittest.TestCase):
    def test_defaults_on_and_env_can_disable(self):
        old = os.environ.pop("AO_BRIDGE_TERMINAL_VIEW", None)
        try:
            self.assertTrue(plugin_api.terminal_view_enabled())
            os.environ["AO_BRIDGE_TERMINAL_VIEW"] = "0"
            self.assertFalse(plugin_api.terminal_view_enabled())
            os.environ["AO_BRIDGE_TERMINAL_VIEW"] = "yes"
            self.assertTrue(plugin_api.terminal_view_enabled())
        finally:
            if old is None:
                os.environ.pop("AO_BRIDGE_TERMINAL_VIEW", None)
            else:
                os.environ["AO_BRIDGE_TERMINAL_VIEW"] = old


if __name__ == "__main__":
    unittest.main()
