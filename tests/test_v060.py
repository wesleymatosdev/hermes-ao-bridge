"""v0.6.0/0.6.1 contracts: data/rendering parity with the AO desktop app
(v0.6.0, another branch of work) plus the v0.6.1 board additions below.

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

import base64
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
                         + lane["blocked"] + lane["done"], 2)
        # v0.6.1: terminated workers leave the counts entirely (Archive strip).
        self.assertNotIn("archive", lane)


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


# ---------------------------------------------------------------------------
# v0.6.1: thinking-before-speech ordering, composer image paste, archive
# strip with restore (wire-contract bullets live in the git history of the
# original feature branch; the contracts themselves are asserted below).
# ---------------------------------------------------------------------------

def msg(seq, role, turn_id, created_at, text="body"):
    return {"sequence": seq, "role": role, "turnId": turn_id,
            "createdAt": created_at, "text": text, "origin": "human"}


def reasoning(seq, turn_id, created_at, text="a thought"):
    return {"activityKind": "reasoning", "status": "completed", "summary": "Reasoning",
            "sequence": seq, "revision": 1, "turnId": turn_id, "createdAt": created_at,
            "detail": {"text": text}}


def command(seq, turn_id, created_at):
    return {"activityKind": "command", "status": "completed", "summary": "ls",
            "sequence": seq, "revision": 1, "turnId": turn_id, "createdAt": created_at,
            "detail": {"command": "ls", "exitCode": 0}}


def png_b64(size: int = 16) -> str:
    return base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * size).decode("ascii")


class BoardRouteTestBase(unittest.TestCase):
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


class ThinkingOrderHoistTest(BoardRouteTestBase):
    """``hoist_inverted_reasoning``: the transcript render order must put a
    thought before the assistant message it introduces — but only by touching
    INVERTED reasoning sequences; every other row's sort key is wire truth."""

    def detail(self):
        return self.http.get("/api/plugins/ao-bridge/sessions/s1/detail").json()

    def merged_order(self, body):
        rows = ([("msg", m["sequence"], m["role"]) for m in body["transcript"]]
                + [(a["activityKind"], a["sequence"], "") for a in body["activities"]])
        rows.sort(key=lambda r: r[1])
        return rows

    def setUp_session(self, messages, activities):
        self.ao.add_session(make_session("s1", project_id="hermes-agent"))
        self.ao.set_conversation("s1", messages)
        self.ao.set_activities("s1", activities)

    def test_inverted_reasoning_renders_before_its_message(self):
        # The bug shape (screenshot, card zcode-tui-ci-fix): the thought
        # streamed first (createdAt 00:00:30) but its wire sequence landed
        # after the message (3 > 2). The payload must re-key it between the
        # user message and the assistant message.
        self.setUp_session(
            [msg(1, "user", "t1", "2026-01-01T00:00:00Z"),
             msg(2, "assistant", "t1", "2026-01-01T00:01:00Z", "the answer")],
            [reasoning(3, "t1", "2026-01-01T00:00:30Z", "thinking first")])
        body = self.detail()
        row = [a for a in body["activities"] if a["activityKind"] == "reasoning"][0]
        self.assertLess(row["sequence"], 2)
        self.assertGreater(row["sequence"], 1)
        order = self.merged_order(body)
        kinds = [(k, role) for k, _s, role in order]
        self.assertLess(kinds.index(("reasoning", "")), kinds.index(("msg", "assistant")))

    def test_already_ordered_reasoning_keeps_its_wire_sequence(self):
        # Every session probed live carries thought-before-message already;
        # the pass must be a no-op there (identity on good wire shapes).
        self.setUp_session(
            [msg(4, "assistant", "t1", "2026-01-01T00:01:00Z")],
            [reasoning(3, "t1", "2026-01-01T00:00:30Z")])
        body = self.detail()
        row = [a for a in body["activities"] if a["activityKind"] == "reasoning"][0]
        self.assertEqual(row["sequence"], 3)

    def test_reasoning_of_another_turn_is_never_hoisted(self):
        # A thought of turn t2 sequenced after t1's message belongs to t2's
        # (later) message — the same-turn pairing must not drag it back.
        self.setUp_session(
            [msg(2, "assistant", "t1", "2026-01-01T00:01:00Z"),
             msg(5, "assistant", "t2", "2026-01-01T00:05:00Z")],
            [reasoning(3, "t2", "2026-01-01T00:04:00Z")])
        body = self.detail()
        row = [a for a in body["activities"] if a["activityKind"] == "reasoning"][0]
        self.assertEqual(row["sequence"], 3)

    def test_trailing_streaming_thought_is_left_alone(self):
        # Mid-turn, the latest thought has no assistant message after it yet.
        self.setUp_session(
            [msg(2, "assistant", "t1", "2026-01-01T00:01:00Z")],
            [reasoning(9, "t1", "2026-01-01T00:09:00Z")])
        body = self.detail()
        row = [a for a in body["activities"] if a["activityKind"] == "reasoning"][0]
        self.assertEqual(row["sequence"], 9)

    def test_command_and_tool_sequences_are_never_touched(self):
        # The interleave of terminal/mcp_tool rows with everything else is the
        # invariant the fix must preserve. The thought here was created after
        # the last assistant message (a trailing/mid-turn thought), so nothing
        # about it is inverted and nothing is re-keyed.
        self.setUp_session(
            [msg(1, "user", "t1", "2026-01-01T00:00:00Z"),
             msg(4, "assistant", "t1", "2026-01-01T00:04:00Z")],
            [command(2, "t1", "2026-01-01T00:00:30Z"),
             reasoning(6, "t1", "2026-01-01T00:05:00Z")])
        body = self.detail()
        rows = {a["activityKind"]: a["sequence"] for a in body["activities"]}
        self.assertEqual(rows["command"], 2)
        self.assertEqual(rows["reasoning"], 6)

    def test_thought_created_before_its_message_but_sequenced_after_hoists(self):
        # Inversion by the wire, not the sequence gap: the thought streamed at
        # 00:03 (before the 00:04 message) yet carries sequence 6 > 4 — same
        # repair as the screenshot case.
        self.setUp_session(
            [msg(1, "user", "t1", "2026-01-01T00:00:00Z"),
             msg(4, "assistant", "t1", "2026-01-01T00:04:00Z")],
            [command(2, "t1", "2026-01-01T00:00:30Z"),
             reasoning(6, "t1", "2026-01-01T00:03:00Z")])
        body = self.detail()
        rows = {a["activityKind"]: a["sequence"] for a in body["activities"]}
        self.assertEqual(rows["command"], 2)
        self.assertLess(rows["reasoning"], 4)
        self.assertGreater(rows["reasoning"], 2)

    def test_hoisted_thoughts_stay_in_stream_order(self):
        # Two inverted thoughts of one turn hoist below the same message
        # without swapping their relative order.
        self.setUp_session(
            [msg(1, "user", "t1", "2026-01-01T00:00:00Z"),
             msg(8, "assistant", "t1", "2026-01-01T00:08:00Z")],
            [reasoning(9, "t1", "2026-01-01T00:06:00Z", "first thought"),
             reasoning(10, "t1", "2026-01-01T00:07:00Z", "second thought")])
        body = self.detail()
        rows = sorted([a for a in body["activities"] if a["activityKind"] == "reasoning"],
                      key=lambda a: a["sequence"])
        self.assertEqual([r["text"] for r in rows], ["first thought", "second thought"])
        self.assertTrue(all(r["sequence"] < 8 for r in rows))


class SteerAttachmentsTest(BoardRouteTestBase):
    """Composer image paste: the steer route forwards the daemon's exact
    attachment shape and refuses everything else it cannot mean."""

    def setUp(self):
        super().setUp()
        self.ao.add_session(make_session("idle1", status="idle"))

    def steer(self, attachments, message="look at this"):
        return self.http.post(
            "/api/plugins/ao-bridge/sessions/idle1/steer",
            json={"message": message, "attachments": attachments})

    def test_image_attachments_forward_in_the_documented_shape(self):
        data = png_b64()
        resp = self.steer([{"mimeType": "image/png", "data": data}])
        self.assertEqual(resp.status_code, 200)
        body = self.ao.steers[-1]["body"]
        self.assertEqual(body["text"], "look at this")
        self.assertEqual(body["attachments"],
                         [{"mimeType": "image/png", "data": data}])

    def test_no_attachments_keeps_the_body_free_of_the_field(self):
        resp = self.steer([])
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("attachments", self.ao.steers[-1]["body"])

    def test_non_image_mime_is_refused_locally(self):
        resp = self.steer([{"mimeType": "application/pdf", "data": png_b64()}])
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(self.ao.steers, [])

    def test_svg_is_refused(self):
        resp = self.steer([{"mimeType": "image/svg+xml", "data": png_b64()}])
        self.assertEqual(resp.status_code, 422)

    def test_data_url_prefix_is_refused(self):
        resp = self.steer([{"mimeType": "image/png",
                            "data": "data:image/png;base64," + png_b64()}])
        self.assertEqual(resp.status_code, 422)

    def test_invalid_base64_is_refused(self):
        resp = self.steer([{"mimeType": "image/png", "data": "not!base64!"}])
        self.assertEqual(resp.status_code, 422)

    def test_over_the_daemon_per_image_cap_is_refused(self):
        resp = self.steer([{"mimeType": "image/png", "data": png_b64(plugin_api.STEER_IMAGE_MAX_BYTES + 1)}])
        self.assertEqual(resp.status_code, 422)

    def test_over_the_daemon_image_count_cap_is_refused(self):
        too_many = [{"mimeType": "image/png", "data": png_b64()}
                    for _ in range(plugin_api.STEER_IMAGES_MAX_COUNT + 1)]
        resp = self.steer(too_many)
        self.assertEqual(resp.status_code, 422)

    def test_limits_match_the_daemon_contract(self):
        # The caps are mirrored from the daemon (conversationContent gate) —
        # drift here would mean a locally-accepted message the daemon refuses.
        self.assertEqual(plugin_api.STEER_IMAGE_MAX_BYTES, 10 << 20)
        self.assertEqual(plugin_api.STEER_IMAGES_MAX_TOTAL_BYTES, 25 << 20)
        self.assertEqual(plugin_api.STEER_IMAGES_MAX_COUNT, 8)


class ArchiveStripTest(BoardRouteTestBase):
    """Archived sessions: split out of the columns server-side; Restore rides
    the daemon's restore route and a restored session re-enters the board."""

    def test_terminated_workers_are_split_into_archived(self):
        self.ao.add_session(make_session("live1", status="working", project_id="p1"))
        self.ao.add_session(make_session("dead1", status="terminated", terminated=True))
        self.ao.add_session(make_session("dead2", status="idle", terminated=True))
        board = self.http.get("/api/plugins/ao-bridge/board").json()
        ids = [c["id"] for c in board["cards"]]
        self.assertNotIn("dead1", ids)
        self.assertNotIn("dead2", ids)
        self.assertIn("live1", ids)
        self.assertEqual(sorted(c["id"] for c in board["archived"]), ["dead1", "dead2"])

    def test_archive_is_no_longer_a_column(self):
        board = self.http.get("/api/plugins/ao-bridge/board").json()
        self.assertNotIn("archive", [c["id"] for c in board["columns"]])

    def test_restore_forwards_and_reports_the_mode(self):
        self.ao.add_session(make_session("dead1", status="terminated", terminated=True))
        resp = self.http.post("/api/plugins/ao-bridge/sessions/dead1/restore")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["restoreMode"], "native")
        self.assertEqual(self.ao.restored, ["dead1"])
        # Restored: the session is live again, so the next board snapshot
        # moves it out of the archive strip.
        board = self.http.get("/api/plugins/ao-bridge/board").json()
        self.assertEqual([c["id"] for c in board["archived"]], [])
        self.assertIn("dead1", [c["id"] for c in board["cards"]])

    def test_restore_of_a_live_session_is_a_conflict(self):
        self.ao.add_session(make_session("live1", status="working"))
        resp = self.http.post("/api/plugins/ao-bridge/sessions/live1/restore")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(self.ao.restored, [])

    def test_restore_of_a_gone_session_is_404(self):
        resp = self.http.post("/api/plugins/ao-bridge/sessions/who/restore")
        self.assertEqual(resp.status_code, 404)


if __name__ == "__main__":
    unittest.main()
