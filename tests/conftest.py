"""Test bootstrap: isolate HERMES_HOME and make the plugin under test importable.

The plugin backend (dashboard/plugin_api.py) is stdlib + fastapi only, so the
tests do not need the Hermes source tree on sys.path; HERMES_HOME is still
pointed at a throwaway dir so nothing touches the real Hermes home.
"""

import os
import sys
import tempfile
from pathlib import Path

# A throwaway Hermes home for anything resolving config paths at call time.
_TEST_HOME = tempfile.mkdtemp(prefix="ao-bridge-tests-home-")
os.environ["HERMES_HOME"] = _TEST_HOME

# dashboard/plugin_api.py is loaded by the dashboard server via
# spec_from_file_location; tests do the same so the import path matches prod.
_PLUGIN_DASHBOARD = Path(__file__).resolve().parent.parent / "dashboard"
if str(_PLUGIN_DASHBOARD.parent) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DASHBOARD.parent))


def load_plugin_api():
    """Import dashboard/plugin_api.py exactly the way the dashboard server does."""
    import importlib.util

    path = _PLUGIN_DASHBOARD / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("ao_bridge_plugin_api", path)
    assert spec is not None and spec.loader is not None, f"spec load failed for {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
