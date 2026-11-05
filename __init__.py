"""ao-bridge — Hermes dashboard plugin bridging the local AO daemon.

No tool/command registrations: everything this plugin provides lives in the
dashboard layer (``dashboard/manifest.json`` tab + ``dashboard/plugin_api.py``
backend, mounted by the dashboard web server at /api/plugins/ao-bridge/).
"""

PLUGIN_ID = "ao-bridge"


def register(ctx) -> None:
    """Native-package entry point. Nothing to register at agent runtime."""
    return None
