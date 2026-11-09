"""v0.6.0 contracts: data/rendering parity with the AO desktop app.

Everything here is a port of AO's own rendering semantics, verified against
the AO frontend source (frontend/src/renderer, packages/product-ui) and the
live daemon on 2026-11-09:

- usage line  — cost (formatCostNanos, nano-USD) · tokens (formatTokenCount),
  either part drops out when unknown (SessionMemoryPanel sessionReport).
- resource chip — "1.4 GB · 82%" while the agent is active, just "240 MB"
  while idle (SessionsBoardAdapters toResourcePresentation + product-ui
  formatResourceBytes).
- lane order — pinned first (the board doubles as the session list; AO pins
  to the top of its sidebar), then attention (displayStatus Blocked / CI
  failing / Changes requested), then most-recent activity (SessionsBoardView
  BoardColumnView attentionPriority || updatedAt desc).
- card fields — isPinned/model/mode/branchState plus the full PR facts the
  session row carries (state/ci/review/mergeability/reviewComments).
- routes — POST/DELETE /sessions/{id}/pin, PATCH /sessions/{id} with the
  daemon's 1..100 displayName contract; spawn passes model/effort/approvalMode
  through exactly as the SpawnSessionRequest enum lists them.
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


class AOFormatTest(unittest.TestCase):
    """The AO desktop app's own presentation formats, ported verbatim."""

    def test_token_count_uses_ao_units(self):
        f = plugin_api.format_token_count
        self.assertEqual(f(999), "999 tok")
        self.assertEqual(f(1000), "1K tok")
        self.assertEqual(f(150159), "150.2K tok")
        self.assertEqual(f(1414601), "1.4M tok")
        self.assertEqual(f(17700000), "17.7M tok")
        self.assertEqual(f(1500000000), "1.5B tok")
        self.assertEqual(f(None), "0 tok")
        self.assertEqual(f(-5), "0 tok")

    def test_cost_nanos_follows_ao_rules(self):
        f = plugin_api.format_cost_nanos
        self.assertIsNone(f(None))
        self.assertIsNone(f(-1))
        self.assertEqual(f(0), "$0.00")
        self.assertEqual(f(445_600_000), "$0.45")          # sub-dollar, >= 1 cent
        self.assertEqual(f(17_700_000_000), "$17.70")      # grouping + cents
        self.assertEqual(f(5_000_000), "$0.005")           # sub-cent never rounds to $0.00
        self.assertEqual(f(1), "$0.000000001")

    def test_memory_bytes_follows_ao_rules(self):
        f = plugin_api.format_memory_bytes
        self.assertEqual(f(2 * 1024 ** 3), "2.0 GB")
        self.assertEqual(f(1500 * 1024 ** 2), "1.5 GB")    # >= 1000 MB reads GB
        self.assertEqual(f(150_000_000), "143 MB")
        self.assertEqual(f(4_500_000), "4.3 MB")           # below 10 MB keeps a decimal
        self.assertEqual(f(1500), "1 KB")                  # a swapped-out page is KB, not "1 MB"
        self.assertEqual(f(None), "")
        self.assertEqual(f(-1), "")

    def test_usage_label_is_cost_dot_tokens(self):
        f = plugin_api.usage_label
        self.assertEqual(
            f({"processedTokens": 17_700_000, "estimatedCost": {"totalNanos": 445_600_000}}),
            "$0.45 · 17.7M tok")
        self.assertEqual(f({"processedTokens": 150_159, "estimatedCost": None}),
                         "150.2K tok")
        self.assertEqual(f({"processedTokens": None,
                            "estimatedCost": {"totalNanos": 445_600_000}}),
                         "$0.45")
        self.assertEqual(f({"processedTokens": None, "estimatedCost": None}), "")
        self.assertEqual(f(None), "")

    def test_resource_label_shows_cpu_only_while_working(self):
        memory = {"rssBytes": 150_000_000, "cpuPercent": 3.2}
        self.assertEqual(plugin_api.resource_label(memory, True), "143 MB · 3%")
        self.assertEqual(plugin_api.resource_label(memory, False), "143 MB")
        self.assertEqual(plugin_api.resource_label(None, True), "")


class LaneOrderTest(unittest.TestCase):
    def test_attention_sorts_before_plain_cards(self):
        cards = [
            {"displayStatus": "Working", "updatedAt": "2026-01-03T00:00:00Z"},
            {"displayStatus": "CI failing", "updatedAt": "2026-01-01T00:00:00Z"},
        ]
        plugin_api.sort_column_cards(cards)
        self.assertEqual([c["displayStatus"] for c in cards], ["CI failing", "Working"])

    def test_attention_set_matches_ao(self):
        self.assertEqual(plugin_api.ATTENTION_DISPLAY_STATUSES,
                         {"Blocked", "CI failing", "Changes requested"})

    def test_most_recent_activity_breaks_ties(self):
        cards = [
            {"displayStatus": "Working", "updatedAt": "2026-01-01T00:00:00Z"},
            {"displayStatus": "Working", "updatedAt": "2026-01-05T00:00:00Z"},
            {"displayStatus": "Working", "updatedAt": "2026-01-03T00:00:00Z"},
        ]
        plugin_api.sort_column_cards(cards)
        self.assertEqual([c["updatedAt"] for c in cards],
                         ["2026-01-05T00:00:00Z", "2026-01-03T00:00:00Z",
                          "2026-01-01T00:00:00Z"])

    def test_pinned_comes_first(self):
        cards = [
            {"isPinned": False, "displayStatus": "Blocked", "updatedAt": "2026-01-05T00:00:00Z"},
            {"isPinned": True, "displayStatus": "Working", "updatedAt": "2026-01-01T00:00:00Z"},
        ]
        plugin_api.sort_column_cards(cards)
        self.assertTrue(cards[0]["isPinned"])


class CardFieldsTest(unittest.TestCase):
    SESSION = {
        "id": "p1-1", "kind": "worker", "harness": "hermes",
        "displayName": "worker", "mode": "chat",
        "projectId": "p1", "branch": "feat/x",
        "status": "working", "displayStatus": "Working",
        "activity": {"state": "active", "lastActivityAt": "2026-01-02T00:00:00Z"},
        "createdAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-02T00:00:00Z",
        "isPinned": True, "model": "claude-opus-5-5",
        "branchState": {"commits": 3, "unpushed": 1},
        "prs": [{"url": "https://github.com/a/b/pull/7", "number": 7, "state": "open",
                 "ci": "failing", "review": "review_required",
                 "mergeability": "blocked", "reviewComments": True,
                 "updatedAt": "2026-01-02T00:00:00Z"}],
    }

    def test_card_carries_the_full_session_facts(self):
        card = plugin_api.card_for(self.SESSION, {"p1": "proj"})
        self.assertTrue(card["isPinned"])
        self.assertEqual(card["model"], "claude-opus-5-5")
        self.assertEqual(card["mode"], "chat")
        self.assertEqual(card["branchState"], {"commits": 3, "unpushed": 1})
        self.assertEqual(card["prs"][0], {
            "url": "https://github.com/a/b/pull/7", "number": 7, "state": "open",
            "ci": "failing", "review": "review_required",
            "mergeability": "blocked", "reviewComments": True,
            "updatedAt": "2026-01-02T00:00:00Z"})

    def test_card_attaches_usage_and_resource_labels(self):
        usage_by_id = {"p1-1": {"processedTokens": 150_159,
                                "estimatedCost": {"totalNanos": 445_600_000},
                                "incomplete": True}}
        memory_by_id = {"p1-1": {"rssBytes": 150_000_000, "cpuPercent": 3.2}}
        card = plugin_api.card_for(self.SESSION, {}, usage_by_id, memory_by_id)
        self.assertEqual(card["usage"], {"label": "$0.45 · 150.2K tok", "incomplete": True})
        # activity.state defaults to "active" in fixtures -> CPU is shown
        self.assertEqual(card["resource"]["label"], "143 MB · 3%")
        self.assertEqual(card["resource"]["rssBytes"], 150_000_000)

    def test_card_without_usage_or_memory_omits_the_chips(self):
        card = plugin_api.card_for(self.SESSION, {})
        self.assertIsNone(card["usage"])
        self.assertIsNone(card["resource"])


class BoardDataParityTest(unittest.TestCase):
    """Board snapshot integration over the fake daemon."""

    def setUp(self):
        self.ao = FakeAO()
        self.server = FakeAOServer(self.ao)
        self.server.start()
        self._old_env = os.environ.get("AO_BRIDGE_BASE_URL")
        os.environ["AO_BRIDGE_BASE_URL"] = self.server.url
        plugin_api._board_extra_cache.clear()
        self.ao.sessions_by_id.clear()
        self.ao.add_session(make_session("p1-1", project_id="p1",
                                         created_at="2026-01-01T00:00:00Z",
                                         pinned=True, model="claude-opus-5-5",
                                         branch_state={"commits": 3, "unpushed": 1}))
        self.ao.add_session(make_session("p1-2", project_id="p1",
                                         created_at="2026-01-03T00:00:00Z",
                                         display_status="CI failing"))
        self.ao.usage_rows = [
            {"sessionId": "p1-1", "processedTokens": 150_159, "totalTokens": 150_159,
             "incomplete": False, "estimatedCost": {"totalNanos": 445_600_000}},
        ]
        self.ao.memory_rows = [
            {"sessionId": "p1-1", "rssBytes": 150_000_000, "cpuPercent": 3.2},
        ]

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("AO_BRIDGE_BASE_URL", None)
        else:
            os.environ["AO_BRIDGE_BASE_URL"] = self._old_env
        self.server.shutdown()
        self.server.server_close()
        plugin_api._board_extra_cache.clear()

    def test_board_cards_carry_usage_and_ao_lane_order(self):
        snap = plugin_api.board_snapshot(plugin_api.client())
        by_id = {c["id"]: c for c in snap["cards"]}
        self.assertEqual(by_id["p1-1"]["usage"]["label"], "$0.45 · 150.2K tok")
        self.assertEqual(by_id["p1-1"]["resource"]["label"], "143 MB · 3%")
        self.assertTrue(by_id["p1-1"]["isPinned"])
        # p1-2 is newer but carries the attention displayStatus: attention
        # beats recency in the AO lane order.
        running = snap["counts"]["blocked"] + snap["counts"]["running"]
        self.assertGreaterEqual(running, 2)
        lane = snap["counts"]
        self.assertEqual(lane["queued"] + lane["running"] + lane["review"]
                         + lane["blocked"] + lane["done"] + lane["archive"], 2)


class PinRenameSpawnRoutesTest(unittest.TestCase):
    def setUp(self):
        self.ao = FakeAO()
        self.server = FakeAOServer(self.ao)
        self.server.start()
        self._old_env = os.environ.get("AO_BRIDGE_BASE_URL")
        os.environ["AO_BRIDGE_BASE_URL"] = self.server.url
        self.ao.add_session(make_session("s1", status="idle"))
        self.http = TestClient(make_app())

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("AO_BRIDGE_BASE_URL", None)
        else:
            os.environ["AO_BRIDGE_BASE_URL"] = self._old_env
        self.server.shutdown()
        self.server.server_close()

    def test_pin_posts_and_unpin_deletes(self):
        r = self.http.post("/api/plugins/ao-bridge/sessions/s1/pin")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.ao.pins, [("s1", True)])
        self.assertTrue(self.ao.sessions_by_id["s1"]["isPinned"])
        r = self.http.request("DELETE", "/api/plugins/ao-bridge/sessions/s1/pin")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.ao.pins[-1], ("s1", False))
        self.assertFalse(self.ao.sessions_by_id["s1"]["isPinned"])

    def test_pin_unknown_session_maps_to_404(self):
        r = self.http.post("/api/plugins/ao-bridge/sessions/ghost/pin")
        self.assertEqual(r.status_code, 404)

    def test_rename_patches_display_name(self):
        r = self.http.patch("/api/plugins/ao-bridge/sessions/s1",
                            json={"displayName": "  renamed  "})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.ao.renames, [("s1", "renamed")])
        self.assertEqual(self.ao.sessions_by_id["s1"]["displayName"], "renamed")

    def test_rename_enforces_the_daemon_contract(self):
        self.assertEqual(
            self.http.patch("/api/plugins/ao-bridge/sessions/s1",
                            json={"displayName": "   "}).status_code, 422)
        self.assertEqual(
            self.http.patch("/api/plugins/ao-bridge/sessions/s1",
                            json={"displayName": "x" * 101}).status_code, 422)
        self.assertEqual(self.ao.renames, [])

    def test_spawn_forwards_model_effort_and_approval_mode(self):
        r = self.http.post("/api/plugins/ao-bridge/spawn", json={
            "projectId": "p1", "harness": "claude-code", "displayName": "w",
            "prompt": "go", "model": "claude-opus-5-5", "effort": "high",
            "approvalMode": "accept-edits"})
        self.assertEqual(r.status_code, 201)
        body = self.ao.created[-1]
        self.assertEqual(body["model"], "claude-opus-5-5")
        self.assertEqual(body["effort"], "high")
        self.assertEqual(body["approvalMode"], "accept-edits")

    def test_spawn_omits_unset_spawn_options(self):
        r = self.http.post("/api/plugins/ao-bridge/spawn",
                           json={"projectId": "p1", "displayName": "w"})
        self.assertEqual(r.status_code, 201)
        body = self.ao.created[-1]
        self.assertNotIn("model", body)
        self.assertNotIn("effort", body)
        self.assertNotIn("approvalMode", body)

    def test_spawn_rejects_an_unknown_approval_mode(self):
        r = self.http.post("/api/plugins/ao-bridge/spawn",
                           json={"projectId": "p1", "displayName": "w",
                                 "approvalMode": "yolo"})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(self.ao.created, [])


class DetailDataParityTest(unittest.TestCase):
    def setUp(self):
        self.ao = FakeAO()
        self.server = FakeAOServer(self.ao)
        self.server.start()
        self._old_env = os.environ.get("AO_BRIDGE_BASE_URL")
        os.environ["AO_BRIDGE_BASE_URL"] = self.server.url
        self.ao.add_session(make_session(
            "s1", status="idle", pinned=True, model="claude-opus-5-5",
            prs=[{"url": "https://github.com/a/b/pull/7", "number": 7,
                  "state": "draft", "ci": "unknown", "review": "",
                  "mergeability": "unknown", "reviewComments": False,
                  "updatedAt": "2026-01-01T00:00:00Z"}],
            branch_state={"commits": 2, "unpushed": 0}))
        self.ao.session_usage["s1"] = {
            "sessionId": "s1", "harnesses": [], "incomplete": False,
            "totals": {"processedTokens": 17_700_000,
                       "estimatedCost": {"totalNanos": 445_600_000}}}
        self.http = TestClient(make_app())

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("AO_BRIDGE_BASE_URL", None)
        else:
            os.environ["AO_BRIDGE_BASE_URL"] = self._old_env
        self.server.shutdown()
        self.server.server_close()

    def test_detail_carries_metadata_usage_and_pr_facts(self):
        payload = self.http.get(
            "/api/plugins/ao-bridge/sessions/s1/detail").json()
        s = payload["session"]
        self.assertTrue(s["isPinned"])
        self.assertEqual(s["model"], "claude-opus-5-5")
        self.assertEqual(s["mode"], "chat")
        self.assertEqual(s["branchState"], {"commits": 2, "unpushed": 0})
        self.assertEqual(payload["usage"]["label"], "$0.45 · 17.7M tok")
        self.assertEqual(payload["prs"][0]["state"], "draft")
        self.assertIn("number", payload["prs"][0])

    def test_detail_survives_a_missing_usage_endpoint(self):
        del self.ao.session_usage["s1"]
        payload = self.http.get(
            "/api/plugins/ao-bridge/sessions/s1/detail").json()
        self.assertIsNone(payload["usage"])
