"""AO event relay contracts (v0.3.3): worker milestones -> the orchestrator's
Hermes session, delivered as LIVE turns.

The relay watches the AO daemon (sessions + worker reports) on a backend-owned
poll loop and delivers compact one-line milestones into the destination
session's conversation through the gateway's own api_server session-chat
route (``POST /api/sessions/{id}/chat`` — the same sanctioned immediate-
submission path ``hermes peer dm`` drives). A delivered event WAKES the
session immediately (a real model turn — that is the point; the v0.3.2
store-row design landed only at the session's next turn, the known-bug class
this replaces). The destination is resolved PER EVENT from AO's own
orchestrator mapping; ``relay_session`` is an explicit override fallback.

Wire truth (verified live, 2026-11-09):
- AO REST session rows carry ``kind`` (``orchestrator`` | ``worker``),
  ``isTerminated``, ``updatedAt``, ``projectId`` — the orchestrator pick.
- AO orchestrator conversations run with cwd under
  ``<worktrees>/<project>/orchestrator/`` — the Hermes-side session marker.
- The gateway api_server route takes ``{"message": text}`` with
  ``Authorization: Bearer``, 401 on bad key (verified against the gateway
  source and live on a scratch api_server instance).

Contracts locked here:
- route selection: auto = api_server (relay_api_key) first, then the peer
  credential; an explicitly picked unavailable route stays unavailable;
  neither available -> honest permanent failure, never a silent no-op. The
  api_server route never touches the Hermes secret store — the key comes
  from the pasted plugin config only.
- report transitions relay once per (report id, state); repeatCount churn on
  the same state never re-relays; a state change does.
- session transitions relay working -> idle / exited / terminated only; the
  FIRST poll is a baseline and emits nothing.
- destination resolution: exactly one live project orchestrator whose Hermes
  conversation (cwd marker, not ended) exists -> that session; no/ambiguous
  orchestrator or no matching conversation -> unmapped (dropped, never
  guessed) unless ``relay_session`` override is set.
- delivery: no credential -> permanent failure; 429/5xx/connection ->
  retryable (batch kept); other HTTP errors -> permanent (batch dropped).
- formatting: one compact English line per event; batches join into ONE
  message per destination.
"""

import time
import unittest
import urllib.error
from unittest.mock import patch

import conftest

plugin_api = conftest.load_plugin_api()


def make_report(report_id, state="done", note="did the thing", session_id="sess-1",
                project_id="p1", repeat=1):
    return {"id": report_id, "sessionId": session_id, "projectId": project_id,
            "state": state, "note": note, "createdAt": "2026-11-09T00:00:00Z",
            "repeatCount": repeat}


def make_session(session_id, status="working", name=None, project_id="p1",
                 kind="worker"):
    return {
        "id": session_id, "displayName": name or session_id,
        "status": status, "isTerminated": status == "terminated",
        "projectId": project_id, "kind": kind,
        "updatedAt": "2026-11-09T00:00:00Z",
    }


def make_hermes_row(session_id, cwd, end_reason=None, last_active=100.0):
    return {"id": session_id, "cwd": cwd, "end_reason": end_reason,
            "source": "local", "last_active": last_active}


class FakeSettings:
    """RelaySettings stand-in for tests that don't exercise config parsing."""

    def __init__(self, override_session="", api_url="http://api.test",
                 peer="", api_key="", delivery="auto", enabled=True):
        self.enabled = enabled
        self.override_session = override_session
        self.api_url = api_url
        self.delivery = delivery
        self.api_key = api_key
        self.peer = peer
        self.events = set(plugin_api.DEFAULT_RELAY_EVENTS)


class ReportTransitionTests(unittest.TestCase):
    def setUp(self):
        self.seen = {}

    def test_first_sighting_relays_once(self):
        events = plugin_api.report_events([make_report("r1")], self.seen,
                                          plugin_api.DEFAULT_RELAY_EVENTS)
        self.assertEqual([e.kind for e in events], ["report_done"])
        again = plugin_api.report_events([make_report("r1")], self.seen,
                                         plugin_api.DEFAULT_RELAY_EVENTS)
        self.assertEqual(again, [])

    def test_repeat_count_churn_does_not_re_relay(self):
        plugin_api.report_events([make_report("r1", repeat=1)], self.seen,
                                 plugin_api.DEFAULT_RELAY_EVENTS)
        events = plugin_api.report_events([make_report("r1", repeat=4)], self.seen,
                                          plugin_api.DEFAULT_RELAY_EVENTS)
        self.assertEqual(events, [])

    def test_state_change_relays_again(self):
        plugin_api.report_events([make_report("r1", state="checkpoint")], self.seen,
                                 plugin_api.DEFAULT_RELAY_EVENTS)
        events = plugin_api.report_events([make_report("r1", state="done")], self.seen,
                                          plugin_api.DEFAULT_RELAY_EVENTS)
        self.assertEqual([e.kind for e in events], ["report_done"])

    def test_untracked_state_never_relays(self):
        events = plugin_api.report_events([make_report("r1", state=None)], self.seen,
                                          plugin_api.DEFAULT_RELAY_EVENTS)
        self.assertEqual(events, [])

    def test_filter_list_drops_unselected_kinds(self):
        events = plugin_api.report_events(
            [make_report("r1", state="checkpoint"), make_report("r2", state="done")],
            self.seen, {"report_done"})
        self.assertEqual([e.kind for e in events], ["report_done"])

    def test_worker_name_and_origin_resolve(self):
        events = plugin_api.report_events(
            [make_report("r1", session_id="sess-9", project_id="proj-2")], self.seen,
            plugin_api.DEFAULT_RELAY_EVENTS, names={"sess-9": "my worker"})
        self.assertEqual(events[0].worker, "my worker")
        self.assertEqual(events[0].ao_session_id, "sess-9")
        self.assertEqual(events[0].project_id, "proj-2")


class SessionTransitionTests(unittest.TestCase):
    def test_first_poll_is_baseline_silent(self):
        prev = {}
        events = plugin_api.session_events(
            [make_session("s1", status="idle"), make_session("s2", status="exited")],
            prev, plugin_api.DEFAULT_RELAY_EVENTS)
        self.assertEqual(events, [])
        self.assertEqual(prev, {"s1": "idle", "s2": "exited"})

    def test_working_to_idle_and_exited_relay(self):
        prev = {"s1": "working", "s2": "working"}
        events = plugin_api.session_events(
            [make_session("s1", status="idle"), make_session("s2", status="exited")],
            prev, plugin_api.DEFAULT_RELAY_EVENTS)
        self.assertEqual(sorted(e.kind for e in events), ["session_exited", "session_idle"])

    def test_steady_state_and_irrelevant_transitions_silent(self):
        prev = {"s1": "idle", "s2": "queued"}
        events = plugin_api.session_events(
            [make_session("s1", status="idle"), make_session("s2", status="working")],
            prev, plugin_api.DEFAULT_RELAY_EVENTS)
        self.assertEqual(events, [])

    def test_gone_sessions_do_not_relay(self):
        prev = {"s1": "working"}
        events = plugin_api.session_events([], prev, plugin_api.DEFAULT_RELAY_EVENTS)
        self.assertEqual(events, [])


class FormatTests(unittest.TestCase):
    def test_report_line_is_compact_english(self):
        line = plugin_api.format_relay_event(plugin_api.RelayEvent(
            "report_done", "my worker", "shipped the fix\n\nsecond line"))
        self.assertIn("✅", line)
        self.assertIn("my worker", line)
        self.assertIn("shipped the fix", line)
        self.assertNotIn("\n", line)

    def test_long_note_truncated(self):
        line = plugin_api.format_relay_event(plugin_api.RelayEvent(
            "report_done", "w", "x" * 400))
        self.assertLess(len(line), 200)
        self.assertTrue(line.endswith("…"))

    def test_session_line_names_the_transition(self):
        line = plugin_api.format_relay_event(plugin_api.RelayEvent(
            "session_idle", "my worker", ""))
        self.assertIn("💤", line)
        self.assertIn("my worker", line)
        self.assertIn("idle", line)


class BatchTests(unittest.TestCase):
    def test_batch_joins_into_one_message(self):
        batcher = plugin_api.RelayBatcher(debounce_s=0.0, max_lines=8)
        self.assertIsNone(batcher.add("line one"))
        self.assertIsNone(batcher.add("line two"))
        text = batcher.flush()
        self.assertEqual(text, "line one\nline two")
        self.assertIsNone(batcher.flush())  # empty again

    def test_cap_flushes_early(self):
        batcher = plugin_api.RelayBatcher(debounce_s=60.0, max_lines=2)
        self.assertIsNone(batcher.add("a"))
        self.assertEqual(batcher.add("b"), "a\nb")  # cap reached -> immediate flush

    def test_due_respects_debounce(self):
        batcher = plugin_api.RelayBatcher(debounce_s=0.05, max_lines=8)
        batcher.add("a")
        self.assertFalse(batcher.due())
        time.sleep(0.06)
        self.assertTrue(batcher.due())

    def test_restore_keeps_deferred_batch(self):
        batcher = plugin_api.RelayBatcher(debounce_s=0.0, max_lines=8)
        batcher.add("a")
        batcher.add("b")
        text = batcher.flush()
        batcher.restore(text)
        self.assertEqual(batcher.flush(), "a\nb")


class DestinationTests(unittest.TestCase):
    """Per-event worker -> orchestrator mapping (no static session target)."""

    def setUp(self):
        self.ao_sessions = [
            make_session("orch-1", status="idle", kind="orchestrator"),
            make_session("w1", status="working"),
        ]

    class FakeAO:
        def __init__(self, sessions):
            self._sessions = sessions

        def sessions(self):
            return self._sessions

    class FakeDB:
        def __init__(self, rows):
            self._rows = rows

        def list_sessions_rich(self, **kwargs):
            return self._rows

    def test_single_live_orchestrator_resolves_via_cwd_marker(self):
        ao = self.FakeAO(self.ao_sessions)
        db = self.FakeDB([
            make_hermes_row("local-worker", "/x/worktrees/p1/w1", end_reason="acp_disconnect"),
            make_hermes_row("local-orch", "/x/worktrees/p1/orchestrator/orch-1-orchestrator"),
        ])
        dest, reason = plugin_api.resolve_relay_destination("p1", ao, FakeSettings(), db)
        self.assertEqual(dest, "local-orch")
        self.assertEqual(reason, "")

    def test_ended_orchestrator_conversation_does_not_resolve(self):
        ao = self.FakeAO(self.ao_sessions)
        db = self.FakeDB([
            make_hermes_row("local-orch-dead", "/x/worktrees/p1/orchestrator/o",
                            end_reason="acp_disconnect"),
        ])
        dest, reason = plugin_api.resolve_relay_destination("p1", ao, FakeSettings(), db)
        self.assertIsNone(dest)
        self.assertIn("no live Hermes conversation", reason)

    def test_no_live_orchestrator_is_unmapped(self):
        ao = self.FakeAO([make_session("w1", status="working")])
        dest, reason = plugin_api.resolve_relay_destination("p1", ao, FakeSettings(),
                                                            self.FakeDB([]))
        self.assertIsNone(dest)
        self.assertIn("no live orchestrator", reason)

    def test_ambiguous_orchestrators_are_unmapped(self):
        ao = self.FakeAO([
            make_session("orch-1", status="idle", kind="orchestrator"),
            make_session("orch-2", status="idle", kind="orchestrator"),
        ])
        dest, reason = plugin_api.resolve_relay_destination("p1", ao, FakeSettings(),
                                                            self.FakeDB([]))
        self.assertIsNone(dest)
        self.assertIn("ambiguous", reason)

    def test_terminated_orchestrator_ignored(self):
        ao = self.FakeAO([make_session("orch-1", status="terminated", kind="orchestrator")])
        dest, _ = plugin_api.resolve_relay_destination("p1", ao, FakeSettings(), self.FakeDB([]))
        self.assertIsNone(dest)

    def test_override_is_fallback_for_unmapped_cases(self):
        ao = self.FakeAO([])  # nothing mappable
        settings = FakeSettings(override_session="local-manual")
        dest, reason = plugin_api.resolve_relay_destination("p1", ao, settings,
                                                            self.FakeDB([]))
        self.assertEqual(dest, "local-manual")
        self.assertIn("override", reason)

    def test_override_does_not_shadow_a_resolvable_mapping(self):
        ao = self.FakeAO(self.ao_sessions)
        db = self.FakeDB([make_hermes_row("local-orch", "/x/worktrees/p1/orchestrator/o")])
        dest, reason = plugin_api.resolve_relay_destination(
            "p1", ao, FakeSettings(override_session="local-manual"), db)
        self.assertEqual(dest, "local-orch")  # the real mapping wins
        self.assertEqual(reason, "")


class RouteSelectionTests(unittest.TestCase):
    """The two-tier route: api_server (generic — pasted API_SERVER_KEY) is
    primary, the hermes-peer credential is the fallback enhancement, and
    neither-available is an honest 'not configured', never a silent no-op."""

    def test_auto_prefers_api_server_with_pasted_key(self):
        settings = FakeSettings(api_key="pasted-k", peer="relaybot", delivery="auto")
        with patch.object(plugin_api, "_relay_secret",
                          side_effect=AssertionError("must not touch the secret store")):
            route, key, reason = plugin_api.select_delivery_route(settings)
        self.assertEqual(route, "api_server")
        self.assertEqual(key, "pasted-k")
        self.assertEqual(reason, "")

    def test_auto_falls_back_to_peer_when_no_api_key(self):
        settings = FakeSettings(peer="relaybot", api_key="", delivery="auto")

        def fake_secret(name, default=""):
            return {"HERMES_PEER_RELAYBOT_KEY": "peer-k"}.get(name, default)

        with patch.object(plugin_api, "_relay_secret", side_effect=fake_secret):
            route, key, reason = plugin_api.select_delivery_route(settings)
        self.assertEqual(route, "peer")
        self.assertEqual(key, "peer-k")

    def test_auto_with_neither_is_honestly_unconfigured(self):
        settings = FakeSettings(peer="", api_key="", delivery="auto")
        with patch.object(plugin_api, "_relay_secret", return_value=""):
            route, key, reason = plugin_api.select_delivery_route(settings)
        self.assertEqual(route, "")
        self.assertFalse(bool(route))
        self.assertIn("not configured", reason)
        self.assertIn("Enable delivery", reason)

    def test_peer_key_hitting_the_secret_store_falls_back_to_api_server_key(self):
        # The peer route keeps its original resolution: the named peer's
        # scoped key first, then the gateway's own API_SERVER_KEY.
        def fake_secret(name, default=""):
            return {"API_SERVER_KEY": "admin-k"}.get(name, default)

        with patch.object(plugin_api, "_relay_secret", side_effect=fake_secret):
            self.assertEqual(plugin_api.peer_key_for("relaybot"), "admin-k")

    def test_explicit_api_server_with_no_key_stays_unavailable(self):
        settings = FakeSettings(api_key="", delivery="api_server")
        with patch.object(plugin_api, "_relay_secret",
                          side_effect=AssertionError("must not fall back silently")):
            route, _key, reason = plugin_api.select_delivery_route(settings)
        self.assertEqual(route, "")
        self.assertIn("relay_api_key", reason)

    def test_explicit_peer_without_a_key_stays_unavailable(self):
        settings = FakeSettings(peer="ghost", delivery="peer")
        with patch.object(plugin_api, "_relay_secret", return_value=""):
            route, _key, reason = plugin_api.select_delivery_route(settings)
        self.assertEqual(route, "")
        self.assertIn("ghost", reason)

    def test_unconfigured_delivery_is_a_permanent_failure(self):
        settings = FakeSettings(peer="", api_key="", delivery="auto")
        with patch.object(plugin_api, "_relay_secret", return_value=""), \
             patch.object(plugin_api, "_post_session_chat") as post:
            result = plugin_api.deliver_relay_batch("s", "text", settings)
        self.assertFalse(result["ok"])
        self.assertFalse(result["retryable"])
        self.assertIn("not configured", result["error"])
        post.assert_not_called()


class DeliveryTests(unittest.TestCase):
    def test_delivers_via_session_chat_route(self):
        recorded = {}

        def fake_post(api_url, key, session_id, text):
            recorded.update(api_url=api_url, key=key, session_id=session_id, text=text)
            return {"ok": True}

        settings = FakeSettings(api_url="http://api.test", api_key="k",
                                delivery="api_server")
        with patch.object(plugin_api, "_post_session_chat", side_effect=fake_post):
            result = plugin_api.deliver_relay_batch("local-orch", "a\nb", settings)
        self.assertTrue(result["ok"])
        self.assertEqual(recorded["api_url"], "http://api.test")
        self.assertEqual(recorded["session_id"], "local-orch")
        self.assertEqual(recorded["text"], "a\nb")

    def test_http_classification_is_manual_not_mocked(self):
        """The retryable/permanent split is a contract; test it against real
        urllib machinery, not a mock."""
        err = urllib.error.HTTPError("http://x", 503, "busy", {}, None)
        with patch.object(plugin_api.urllib.request, "urlopen", side_effect=err):
            result = plugin_api._post_session_chat("http://x", "k", "s", "t")
        self.assertFalse(result["ok"])
        self.assertTrue(result["retryable"])

        err = urllib.error.HTTPError("http://x", 401, "denied", {}, None)
        with patch.object(plugin_api.urllib.request, "urlopen", side_effect=err):
            result = plugin_api._post_session_chat("http://x", "k", "s", "t")
        self.assertFalse(result["ok"])
        self.assertFalse(result["retryable"])

        err = urllib.error.URLError("connection refused")
        with patch.object(plugin_api.urllib.request, "urlopen", side_effect=err):
            result = plugin_api._post_session_chat("http://x", "k", "s", "t")
        self.assertFalse(result["ok"])
        self.assertTrue(result["retryable"])


class SettingsTests(unittest.TestCase):
    def test_defaults_are_off_autodetect_url_and_no_override(self):
        with patch.object(plugin_api, "_config_raw", return_value=None), \
             patch.object(plugin_api, "detect_api_url", return_value=""):
            settings = plugin_api.read_relay_settings()
        self.assertFalse(settings.enabled)
        self.assertEqual(settings.override_session, "")
        self.assertEqual(settings.delivery, "auto")
        self.assertEqual(settings.api_key, "")
        self.assertEqual(settings.api_url, plugin_api.RELAY_API_URL_DEFAULT)
        self.assertEqual(settings.peer, "")
        self.assertEqual(settings.events, set(plugin_api.DEFAULT_RELAY_EVENTS))

    def test_events_parse_and_validate(self):
        with patch.object(plugin_api, "_config_raw", side_effect=lambda k: {
            "relay_enabled": True, "relay_session": "local-abc",
            "relay_events": "report_done, session_idle, bogus_kind"}.get(k)):
            settings = plugin_api.read_relay_settings()
        self.assertTrue(settings.enabled)
        self.assertEqual(settings.override_session, "local-abc")
        self.assertEqual(settings.events, {"report_done", "session_idle"})

    def test_delivery_mode_parses_and_typo_coerces_to_auto(self):
        with patch.object(plugin_api, "_config_raw", side_effect=lambda k: {
            "relay_delivery": "api_server", "relay_api_key": "k"}.get(k)):
            settings = plugin_api.read_relay_settings()
        self.assertEqual(settings.delivery, "api_server")
        self.assertEqual(settings.api_key, "k")
        with patch.object(plugin_api, "_config_raw", side_effect=lambda k: {
            "relay_delivery": "bogus"}.get(k)):
            settings = plugin_api.read_relay_settings()
        self.assertEqual(settings.delivery, "auto")

    def test_api_url_explicit_wins_over_detection(self):
        with patch.object(plugin_api, "_config_raw", side_effect=lambda k: {
            "relay_api_url": "http://127.0.0.1:9999"}.get(k)), \
             patch.object(plugin_api, "detect_api_url", return_value="http://127.0.0.1:7000"):
            settings = plugin_api.read_relay_settings()
        self.assertEqual(settings.api_url, "http://127.0.0.1:9999")

    def test_api_url_detected_when_unset_falls_back_to_stock_default(self):
        with patch.object(plugin_api, "_config_raw", return_value=None), \
             patch.object(plugin_api, "detect_api_url", return_value="http://127.0.0.1:8700"):
            settings = plugin_api.read_relay_settings()
        self.assertEqual(settings.api_url, "http://127.0.0.1:8700")


class DetectApiUrlTests(unittest.TestCase):
    """URL auto-detection from Hermes config.yaml — the ADDRESS only, never
    any credential."""

    def _detect(self, config):
        with patch.object(plugin_api, "_load_hermes_config", return_value=config):
            return plugin_api.detect_api_url()

    def test_platforms_block_port(self):
        url = self._detect({"platforms": {"api_server": {"port": 9000}}})
        self.assertEqual(url, "http://127.0.0.1:9000")

    def test_wildcard_bind_reads_as_loopback(self):
        url = self._detect({"platforms": {"api_server": {"host": "0.0.0.0", "port": 9000}}})
        self.assertEqual(url, "http://127.0.0.1:9000")

    def test_gateway_shorthand_block(self):
        url = self._detect({"gateway": {"api_server": {"host": "192.168.1.5", "port": 8643}}})
        self.assertEqual(url, "http://192.168.1.5:8643")

    def test_no_api_server_block_is_empty(self):
        self.assertEqual(self._detect({"platforms": {"telegram": {"enabled": True}}}), "")
        self.assertEqual(self._detect({}), "")

    def test_url_never_carries_a_credential(self):
        url = self._detect({"platforms": {"api_server": {
            "port": 8642, "key": "super-secret-key"}}})
        self.assertEqual(url, "http://127.0.0.1:8642")
        self.assertNotIn("super-secret-key", url)


class PollOnceTests(unittest.TestCase):
    """The tick pipeline: poll -> diff -> resolve destination -> batch -> deliver."""

    def _worker_and_fakes(self):
        worker = plugin_api.RelayWorker(poll_seconds=0.0, debounce_s=0.0)
        sessions = [
            make_session("orch-1", status="idle", kind="orchestrator"),
            make_session("s1", status="working", name="w1"),
        ]
        reports = []

        class FakeAO:
            def projects(self):
                return [{"id": "p1"}]

            def sessions(self):
                return sessions

            def reports(self, project_id):
                return reports if project_id == "p1" else []

        db = DestinationTests.FakeDB([
            make_hermes_row("local-orch", "/x/worktrees/p1/orchestrator/orch-1"),
        ])
        return worker, sessions, reports, FakeAO(), db

    def test_baseline_then_delivery_to_resolved_destination(self):
        worker, sessions, reports, ao, db = self._worker_and_fakes()
        reports.append(make_report("r1", state="done", session_id="s1"))
        delivered = []
        with patch.object(plugin_api, "_post_session_chat",
                          side_effect=lambda url, key, sid, text: delivered.append((sid, text)) or {"ok": True}):
            worker.poll_once(ao, lambda t: None, settings=FakeSettings(api_key="k"), db=db)
            # Baseline poll: silent — enabling the relay must not flood any
            # conversation with the daemon's current state.
            self.assertEqual(delivered, [])

            reports.append(make_report("r2", state="checkpoint", session_id="s1"))
            worker.poll_once(ao, lambda t: None, settings=FakeSettings(api_key="k"), db=db)
            self.assertEqual(len(delivered), 1)
            dest, text = delivered[0]
            self.assertEqual(dest, "local-orch")  # per-event resolution, not config
            self.assertIn("📍", text)

            # Same state again: nothing.
            worker.poll_once(ao, lambda t: None, settings=FakeSettings(api_key="k"), db=db)
            self.assertEqual(len(delivered), 1)

            # Worker goes idle: session transition relays to the same place.
            sessions[1]["status"] = "idle"
            worker.poll_once(ao, lambda t: None, settings=FakeSettings(api_key="k"), db=db)
            self.assertEqual(len(delivered), 2)
            self.assertIn("idle", delivered[1][1])

    def test_unmapped_event_is_dropped_not_guessed(self):
        worker, _s, reports, ao, db = self._worker_and_fakes()
        db._rows = []  # no orchestrator conversation -> unmappable
        reports.append(make_report("r1", state="done", session_id="s1"))
        delivered = []
        with patch.object(plugin_api, "_post_session_chat",
                          side_effect=lambda *a: delivered.append(a) or {"ok": True}):
            worker.poll_once(ao, lambda t: None, settings=FakeSettings(), db=db)
            self.assertEqual(delivered, [])  # baseline: mapped nowhere, silently
            reports.append(make_report("r2", state="done", session_id="s2"))
            worker.poll_once(ao, lambda t: None, settings=FakeSettings(), db=db)
        self.assertEqual(delivered, [])
        self.assertIn("unmapped", worker.last_error)

    def test_override_catches_unmapped_when_set(self):
        worker, _s, reports, ao, db = self._worker_and_fakes()
        db._rows = []
        reports.append(make_report("r1", state="done", session_id="s1"))
        delivered = []
        settings = FakeSettings(override_session="local-manual", api_key="k")
        with patch.object(plugin_api, "_post_session_chat",
                          side_effect=lambda url, key, sid, text: delivered.append(sid) or {"ok": True}):
            worker.poll_once(ao, lambda t: None, settings=settings, db=db)  # baseline
            reports.append(make_report("r2", state="done", session_id="s2"))
            worker.poll_once(ao, lambda t: None, settings=settings, db=db)
        self.assertEqual(delivered, ["local-manual"])

    def test_retryable_failure_keeps_batch_for_next_tick(self):
        worker, _s, reports, ao, db = self._worker_and_fakes()
        reports.append(make_report("r1", state="done", session_id="s1"))
        calls = []

        def flaky(url, key, sid, text):
            calls.append(sid)
            return {"ok": False, "retryable": True, "error": "gateway api unreachable"}

        settings = FakeSettings(api_key="k")
        with patch.object(plugin_api, "_post_session_chat", side_effect=flaky):
            worker.poll_once(ao, lambda t: None, settings=settings, db=db)  # baseline
            reports.append(make_report("r2", state="done", session_id="s2"))
            worker.poll_once(ao, lambda t: None, settings=settings, db=db)
            self.assertEqual(len(calls), 1)  # tried once, deferred
        # Next tick, gateway healthy again: the deferred batch delivers.
        with patch.object(plugin_api, "_post_session_chat",
                          side_effect=lambda url, key, sid, text: calls.append(sid) or {"ok": True}):
            worker.poll_once(ao, lambda t: None, settings=settings, db=db)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1], "local-orch")

    def test_disabled_relay_polls_nothing(self):
        worker = plugin_api.RelayWorker(poll_seconds=0.0, debounce_s=0.0)

        class Boom:
            def projects(self):
                raise AssertionError("poll must not run while relay is disabled")

        self.assertFalse(worker.tick(Boom(), lambda t: None))


if __name__ == "__main__":
    unittest.main()
