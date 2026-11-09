"""Minimal Chrome DevTools Protocol client — python websockets, no npm deps.

Just enough CDP to render the real dashboard frontend in a real headless
Chrome and assert user-visible contracts: launch Chrome with a debug port,
open a tab, evaluate JS in the page, click, wait for conditions, take
screenshots. Written for tests/e2e/test_e2e_board.py; not a general client.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
import uuid
from pathlib import Path

from websockets.sync.client import connect

CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
    "/usr/bin/chromium",
    "/usr/bin/google-chrome",
)


def find_chrome() -> str | None:
    for path in CHROME_CANDIDATES:
        if Path(path).exists():
            return path
    which = shutil.which("chromium") or shutil.which("google-chrome") or shutil.which("chrome")
    return which


class CDPError(RuntimeError):
    pass


class _BrowserWS:
    """One websocket to the browser endpoint; caller owns the reader loop."""

    def __init__(self, url: str):
        self._ws = connect(url, max_size=64 * 1024 * 1024, close_timeout=5)
        self._lock = threading.Lock()
        self._next_id = 0

    def send(self, payload: dict) -> int:
        with self._lock:
            self._next_id += 1
            msg_id = self._next_id
            payload["id"] = msg_id
            self._ws.send(json.dumps(payload))
            return msg_id

    def recv(self, timeout: float) -> dict:
        return json.loads(self._ws.recv(timeout=timeout))

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:
            pass


class Browser:
    """Headless Chrome + one tab, addressed over CDP.

    Lifecycle: launch() discovers the DevTools port from Chrome's own stderr
    banner (the --remote-debugging-port=0 contract), connects to the browser
    websocket, creates and attaches to a tab.
    """

    def __init__(self, window_size: str = "1600,1000"):
        self.chrome_bin: str | None = (
            os.environ.get("CHROME_BIN") or find_chrome())
        if not self.chrome_bin:
            raise CDPError(
                "no Chrome/Chromium found — the browser E2E lane needs a real "
                "Chrome (set CHROME_BIN to its absolute path)")
        self._window_size = window_size
        self._proc: subprocess.Popen | None = None
        self._profile_dir: tempfile.TemporaryDirectory | None = None
        self._ws: _BrowserWS | None = None
        self._session_id: str | None = None
        self._events: dict[str, list[dict]] = {}
        self._pending: dict[int, dict] = {}
        self._reader: threading.Thread | None = None
        self._closed = False

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        self._profile_dir = tempfile.TemporaryDirectory(prefix="ao-bridge-e2e-chrome-")
        self._proc = subprocess.Popen(
            [self.chrome_bin,
             "--headless=new",
             "--remote-debugging-port=0",
             "--user-data-dir=" + self._profile_dir.name,
             "--no-first-run", "--no-default-browser-check",
             "--disable-gpu", "--hide-scrollbars",
             "--window-size=" + self._window_size,
             "--disable-features=PaintHolding",
             "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        port = self._wait_devtools_port(timeout=20)
        browser_ws = self._fetch_browser_ws(port)
        self._ws = _BrowserWS(browser_ws)
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        target = self.cmd("Target.createTarget", {"url": "about:blank"})
        attached = self.cmd("Target.attachToTarget",
                            {"targetId": target["targetId"], "flatten": True})
        self._session_id = attached["sessionId"]

    def _wait_devtools_port(self, timeout: float) -> int:
        deadline = time.monotonic() + timeout
        assert self._proc is not None and self._proc.stderr is not None
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                raise CDPError("Chrome exited immediately during launch")
            line = self._proc.stderr.readline().decode("utf-8", "replace")
            if "DevTools listening on ws://" in line:
                return int(line.rsplit(":", 1)[1].split("/")[0])
        raise CDPError("Chrome never printed its DevTools listening banner")

    @staticmethod
    def _fetch_browser_ws(port: int) -> str:
        deadline = time.monotonic() + 10
        last_err: Exception | None = None
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/json/version", timeout=2) as resp:
                    return json.load(resp)["webSocketDebuggerUrl"]
            except Exception as err:  # endpoint comes up slightly after the banner
                last_err = err
                time.sleep(0.2)
        raise CDPError(f"/json/version never answered: {last_err}")

    def stop(self) -> None:
        self._closed = True
        if self._ws:
            self._ws.close()
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        if self._profile_dir:
            self._profile_dir.cleanup()

    def __enter__(self) -> "Browser":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    # -- wire ---------------------------------------------------------------

    def _read_loop(self) -> None:
        while not self._closed:
            try:
                msg = self._ws.recv(timeout=1)
            except TimeoutError:
                continue
            except Exception:
                if not self._closed:
                    # Connection died: fail every pending call promptly.
                    for fut in self._pending.values():
                        fut["error"] = CDPError("CDP connection closed")
                        fut["done"].set()
                    self._pending.clear()
                return
            if "id" in msg:
                fut = self._pending.pop(msg["id"], None)
                if fut is not None:
                    fut["msg"] = msg
                    fut["done"].set()
            else:
                self._events.setdefault(msg.get("method", ""), []).append(msg)

    def cmd(self, method: str, params: dict | None = None) -> dict:
        assert self._ws is not None
        fut = {"done": threading.Event(), "msg": None, "error": None}
        payload = {"method": method, "params": params or {}}
        if self._session_id is not None:
            payload["sessionId"] = self._session_id
        msg_id = self._ws.send(payload)
        self._pending[msg_id] = fut
        if not fut["done"].wait(timeout=30):
            self._pending.pop(msg_id, None)
            raise CDPError(f"CDP timed out: {method}")
        if fut["error"]:
            raise fut["error"]
        msg = fut["msg"]
        if "error" in msg:
            raise CDPError(f"{method} failed: {msg['error']}")
        return msg.get("result", {})

    def drain_events(self, method: str) -> list[dict]:
        out = self._events.pop(method, [])
        return out

    # -- page helpers -------------------------------------------------------

    def goto(self, url: str, timeout: float = 20) -> None:
        self.cmd("Page.enable")
        self.cmd("Page.navigate", {"url": url})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.eval("document.readyState")
            if state == "complete":
                return
            time.sleep(0.1)
        raise CDPError(f"page never reached complete: {url}")

    def eval(self, expression: str):
        result = self.cmd("Runtime.evaluate", {
            "expression": expression, "returnByValue": True,
            "awaitPromise": True,
        })
        if result.get("exceptionDetails"):
            raise CDPError(f"page JS error: {result['exceptionDetails']}")
        return result.get("result", {}).get("value")

    def eval_fn(self, body: str):
        """Run an async IIFE body that must return a JSON value.

        `body` is JS statements; wrap: (async () => { body })()."""
        return self.eval(f"(async () => {{ {body} }})()")

    def wait_for(self, expression: str, timeout: float = 10,
                 desc: str = "") -> None:
        deadline = time.monotonic() + timeout
        last = None
        wrapped = f"(async () => {{ return ({expression}); }})()"
        while time.monotonic() < deadline:
            last = self.eval(wrapped)
            if last:
                return
            time.sleep(0.15)
        raise CDPError(f"condition never became true ({desc or expression}): {last!r}")

    def click(self, selector: str) -> bool:
        return bool(self.eval_fn(f"""
            const el = document.querySelector({json.dumps(selector)});
            if (!el) return false;
            el.click();
            return true;
        """))

    def screenshot(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = self.cmd("Page.captureScreenshot", {"format": "png"})
        path.write_bytes(base64.b64decode(data["data"]))
        return path

    def set_viewport(self, width: int, height: int) -> None:
        self.cmd("Emulation.setDeviceMetricsOverride",
                 {"width": width, "height": height,
                  "deviceScaleFactor": 1, "mobile": False})
