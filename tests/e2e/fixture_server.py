"""Browser-E2E fixture: the REAL plugin stack under a fake AO daemon.

Renders the plugin exactly the way the dashboard does — plugin_api.py imported
via spec_from_file_location and mounted at /api/plugins/ao-bridge/, the real
dist/index.js + dist/style.css served from the plugin tree, React behind a
minimal window.__HERMES_PLUGIN_SDK__ shim — and points the backend at an
in-process FakeAO daemon (tests/fake_ao_server.py). Loopback only; the real
AO daemon and the real Hermes home are never touched (HERMES_HOME is pointed
at a throwaway dir for the server's lifetime).

The plugin tree under test comes from AO_BRIDGE_E2E_PLUGIN_ROOT (default: this
checkout's root) — that is the knob the RED/GREEN proof runs turn.

Served surfaces:
  GET /                                  the host page (shims + mounts the plugin)
  GET /dashboard-plugins/ao-bridge/dist/{index.js,style.css}   the real frontend
  GET   …/dist/style.css?break=1         engine-proof variant: oklch()/color-mix()
                                         token functions made unparseable, the
                                         same reproduction the v0.8.1 fix used
  GET /dashboard-plugins/ao-bridge/assets/…    per-harness icons + map
  /api/plugins/ao-bridge/…               the REAL backend router
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse, Response

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# The dashboard server imports plugin_api.py once via spec_from_file_location;
# the E2E fixture does the same against the tree under test.
import importlib.util


def load_plugin_api_from(root: Path):
    path = Path(root) / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location(
        f"ao_bridge_e2e_plugin_api_{abs(hash(str(root)))}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _recent_iso(minutes_ago: int = 5) -> str:
    dt = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def build_board_fixture(fake_ao_cls):
    """Populate a FakeAO with the minimal reference daemon's canonical rows
    (tests/e2e/reference_board.py — the source of truth this gate asserts
    parity against): one worker per board edge (working / idle / exited /
    awaiting PR / review / CI-failed / queued / ready / daemon-archive-column
    / terminated) plus live-working, live-idle and terminated orchestrators.
    All recently active so the default 24h recency window keeps them."""
    from tests.e2e.reference_board import canonical_rows

    ao = fake_ao_cls()
    ao.project_rows = [{"id": "proj-demo", "name": "demo-project",
                        "path": "/tmp/ao-bridge-e2e/demo"}]
    for row in canonical_rows(_recent_iso()):
        ao.add_session(row)
    return ao


# Minimal host: the shims the real dashboard provides (plugin SDK + page
# registry), then the real plugin registers and mounts itself. React 18.3.1
# UMD builds are vendored in tests/e2e/vendor/ (SHA-256s pinned in
# tests/e2e/vendor/README.md) — no network, no npm.
HOST_PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>ao-bridge E2E host</title>
<link rel="stylesheet" href="/dashboard-plugins/ao-bridge/dist/style.css{css_query}">
<style>body { margin: 0; }</style>
</head>
<body>
<div id="root"></div>
<script src="/__vendor__/react.production.min.js"></script>
<script src="/__vendor__/react-dom.production.min.js"></script>
<script>
window.__HERMES_PLUGINS__ = {
  _pages: {},
  register: function (name, comp) { this._pages[name] = comp; },
};
window.__HERMES_PLUGIN_SDK__ = {
  React: window.React,
  hooks: window.React,
  authedFetch: function (url, opts) { return fetch(url, opts); },
  buildWsUrl: function (path) {
    var proto = location.protocol === "https:" ? "wss://" : "ws://";
    return Promise.resolve(proto + location.host + path);
  },
};
</script>
<script src="/dashboard-plugins/ao-bridge/dist/index.js"></script>
<script>
(function () {
  var page = window.__HERMES_PLUGINS__._pages["ao-bridge"];
  if (!page) {
    document.title = "E2E-ERROR: plugin did not register";
    return;
  }
  ReactDOM.createRoot(document.getElementById("root"))
    .render(React.createElement(page));
})();
</script>
</body>
</html>
"""


def break_css(css_text: str) -> str:
    """Engine-proof reproduction: make every fragile color function one the
    browser does NOT know, keeping the parentheses balanced. A custom property
    holding an unknown function is a valid token stream — the failure lands
    exactly where the v0.8.0 bug lived: var() substitution into a real
    property is invalid at computed-value time, so every consuming surface
    (card background, chip colors) goes unset. Mangling to an unbalanced
    token instead would invalidate the DECLARATION at parse time, silently
    falling back to the base (nebula) tokens — masking the mechanism."""
    for fn in ("color-mix(", "oklch(", "oklab("):
        css_text = css_text.replace(fn, "ao-e2e-broken-" + fn)
    return css_text


class FixtureServer:
    """Start/stop the whole rig; `url` is what Chrome navigates to."""

    def __init__(self, plugin_root: Path):
        from tests.fake_ao_server import FakeAO, FakeAOServer

        self.plugin_root = Path(plugin_root)
        self._home = tempfile.mkdtemp(prefix="ao-bridge-e2e-home-")
        self._old_env = {k: os.environ.get(k) for k in
                         ("HERMES_HOME", "AO_BRIDGE_BASE_URL")}

        self.ao = build_board_fixture(FakeAO)
        self.daemon = FakeAOServer(self.ao)
        self.daemon.start()
        os.environ["HERMES_HOME"] = self._home
        os.environ["AO_BRIDGE_BASE_URL"] = self.daemon.url

        self.plugin_api = load_plugin_api_from(self.plugin_root)
        self.app = FastAPI()
        self.app.include_router(self.plugin_api.router,
                                prefix="/api/plugins/ao-bridge")
        self._add_host_routes()
        self._server = uvicorn.Config(self.app, host="127.0.0.1", port=0,
                                      log_level="error")
        self._uvicorn = uvicorn.Server(self._server)
        self._thread = threading.Thread(target=self._uvicorn.run, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 20.0
        while not self._uvicorn.started and time.monotonic() < deadline:
            time.sleep(0.1)
        if not self._uvicorn.started:
            raise RuntimeError("fixture server never started")
        port = self._uvicorn.servers[0].sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"

    def _add_host_routes(self) -> None:
        dist = self.plugin_root / "dashboard" / "dist"
        assets = self.plugin_root / "dashboard" / "assets"
        vendor = Path(__file__).resolve().parent / "vendor"

        @self.app.get("/", include_in_schema=False)
        def host_page(themebreak: str = Query("", alias="themebreak")):
            css_query = "?break=1" if themebreak == "1" else ""
            return Response(HOST_PAGE.replace("{css_query}", css_query),
                            media_type="text/html")

        @self.app.get("/dashboard-plugins/ao-bridge/dist/index.js",
                      include_in_schema=False)
        def index_js():
            return Response((dist / "index.js").read_text(),
                            media_type="application/javascript")

        @self.app.get("/dashboard-plugins/ao-bridge/dist/style.css",
                      include_in_schema=False)
        def style_css(break_: str = Query("", alias="break")):
            text = (dist / "style.css").read_text()
            if break_ == "1":
                text = break_css(text)
            return Response(text, media_type="text/css")

        @self.app.get("/dashboard-plugins/ao-bridge/assets/{asset_path:path}",
                      include_in_schema=False)
        def asset(asset_path: str):
            path = assets / asset_path
            if not path.is_file():
                return JSONResponse({"error": "not found"}, status_code=404)
            media = ("application/json" if path.suffix == ".json"
                     else "image/svg+xml" if path.suffix == ".svg"
                     else "application/octet-stream")
            return Response(path.read_bytes(), media_type=media)

        @self.app.get("/__vendor__/{name}", include_in_schema=False)
        def vendor_js(name: str):
            path = vendor / name
            if not path.is_file():
                return JSONResponse({"error": "not found"}, status_code=404)
            return Response(path.read_bytes(),
                            media_type="application/javascript")

    def close(self) -> None:
        self._uvicorn.should_exit = True
        self._thread.join(timeout=10)
        self.daemon.stop()
        self.daemon.server_close()
        for key, old in self._old_env.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
