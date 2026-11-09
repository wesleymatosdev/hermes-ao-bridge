"""AO event relay contracts (v0.3.x): worker milestones -> the orchestrator's
Hermes conversation.

The relay watches the AO daemon (sessions + worker reports) on a backend-owned
poll loop and injects compact one-line milestones into a Hermes session chosen
by explicit config (``relay_session``), through the same sanctioned store path
async delegation completions use (``SessionDB.append_delegation_delivery``).
No platform sends, no secrets: the delivery target is a session id, the
transport is entirely Hermes core.

Wire truth (probed against the live AO daemon, 2026-11-09):
- GET /api/v1/reports?projectId=<id> -> {"reports": [{id, sessionId, projectId,
  state, note, outputs, createdAt, repeatCount}]}; states seen on a live
  daemon: done, checkpoint, needs_input, None (stuck documented by AO's CLI).
- Session rows carry ``status`` (working / idle / exited / terminated / ...).

Contracts locked here:
- report transitions relay once per (report id, state); repeatCount churn on
  the same state never re-relays; a state change does.
- session transitions relay working -> idle / exited / terminated only; the
  FIRST poll is a baseline and emits nothing (a relay start must not flood the
  conversation with the daemon's current state).
- the ``relay_events`` filter list drops kinds the user did not pick.
- formatting: one compact English line per event, newlines collapsed, note
  truncated; batches join into ONE message.
- delivery rides ``append_delegation_delivery`` with a content-stable
  delegation id (idempotent across retries) on the resume-resolved session;
  a live-turn refusal (store raises) keeps the batch pending for the next tick.
"""

import time
import unittest
from unittest.mock import patch

import conftest

plugin_api = conftest.load_plugin_api()


def make_report(report_id, state="done", note="did the thing", session_id="sess-1",
                repeat=1):
    return {"id": report_id, "sessionId": session_id, "projectId": "p1",
            "state": state, "note": note, "createdAt": "2026-11-09T00:00:00Z",
            "repeatCount": repeat}


def make_session(session_id, status="working", name=None):
    return {
        "id": session_id, "displayName": name or session_id,
        "status": status, "isTerminated": status == "terminated",
        "projectId": "p1",
    }


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

    def test_worker_name_resolves_from_sessions(self):
        events = plugin_api.report_events(
            [make_report("r1", session_id="sess-9")], self.seen,
            plugin_api.DEFAULT_RELAY_EVENTS, names={"sess-9": "my worker"})
        self.assertEqual(events[0].worker, "my worker")


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


class DeliveryTests(unittest.TestCase):
    def test_delivery_uses_delegation_path_with_stable_id(self):
        recorded = []

        class FakeDB:
            def resolve_resume_session_id(self, session_id):
                return "resolved-tip"

            def append_delegation_delivery(self, session_id, content, metadata):
                recorded.append((session_id, content, metadata))
                return 42

        with patch.object(plugin_api, "_open_session_db", return_value=FakeDB()):
            result = plugin_api.deliver_relay_batch("sess-1", "a\nb")
        self.assertTrue(result["ok"])
        sid, content, metadata = recorded[0]
        self.assertEqual(sid, "resolved-tip")
        self.assertEqual(content, "a\nb")
        self.assertTrue(metadata["delegation_id"].startswith("ao-bridge:"))
        # Same content -> same delegation id (idempotent across retries).
        with patch.object(plugin_api, "_open_session_db", return_value=FakeDB()):
            again = plugin_api.deliver_relay_batch("sess-1", "a\nb")
        self.assertEqual(again["delegationId"], metadata["delegation_id"])

    def test_live_turn_refusal_is_retryable(self):
        class BusyDB:
            def resolve_resume_session_id(self, session_id):
                return session_id

            def append_delegation_delivery(self, session_id, content, metadata):
                raise RuntimeError("a live turn refuses the write")

        with patch.object(plugin_api, "_open_session_db", return_value=BusyDB()):
            result = plugin_api.deliver_relay_batch("sess-1", "a")
        self.assertFalse(result["ok"])
        self.assertTrue(result["retryable"])

    def test_missing_store_is_fatal_not_retryable(self):
        with patch.object(plugin_api, "_open_session_db", return_value=None):
            result = plugin_api.deliver_relay_batch("sess-1", "a")
        self.assertFalse(result["ok"])
        self.assertFalse(result["retryable"])


class SettingsTests(unittest.TestCase):
    def test_defaults_are_off_and_explicit(self):
        with patch.object(plugin_api, "_config_raw", return_value=None):
            settings = plugin_api.read_relay_settings()
        self.assertFalse(settings.enabled)
        self.assertEqual(settings.session_id, "")
        self.assertEqual(settings.events, set(plugin_api.DEFAULT_RELAY_EVENTS))

    def test_events_parse_and_validate(self):
        with patch.object(plugin_api, "_config_raw", side_effect=lambda k: {
            "relay_enabled": True, "relay_session": "local-abc",
            "relay_events": "report_done, session_idle, bogus_kind"}.get(k)):
            settings = plugin_api.read_relay_settings()
        self.assertTrue(settings.enabled)
        self.assertEqual(settings.session_id, "local-abc")
        self.assertEqual(settings.events, {"report_done", "session_idle"})


class PollOnceTests(unittest.TestCase):
    """The tick pipeline: poll -> diff -> batch -> deliver, with a fake AO client."""

    def test_tick_relays_new_report_and_transitions(self):
        worker = plugin_api.RelayWorker(poll_seconds=0.0, debounce_s=0.0)
        sessions = [make_session("s1", status="working", name="w1")]
        reports = [make_report("r1", state="done", session_id="s1")]

        class FakeAO:
            def projects(self):
                return [{"id": "p1"}]

            def sessions(self):
                return sessions

            def reports(self, project_id):
                return reports if project_id == "p1" else []

        class FakeDB:
            def resolve_resume_session_id(self, session_id):
                return session_id

            def append_delegation_delivery(self, session_id, content, metadata):
                return 1

        delivered = []
        with patch.object(plugin_api, "_open_session_db", return_value=FakeDB()):
            worker.poll_once(FakeAO(), sink=delivered.append, session_id="sess-x")
            # Baseline poll: silent — enabling the relay must not flood the
            # conversation with the daemon's current state.
            self.assertEqual(delivered, [])

            # A NEW report relays; the old one re-appearing does not.
            reports.append(make_report("r2", state="checkpoint", session_id="s1"))
            worker.poll_once(FakeAO(), sink=delivered.append, session_id="sess-x")
            self.assertEqual(len(delivered), 1)
            self.assertIn("📍", delivered[0])

            # Second tick, same state: nothing.
            worker.poll_once(FakeAO(), sink=delivered.append, session_id="sess-x")
            self.assertEqual(len(delivered), 1)

            # Worker goes idle: session transition relays.
            sessions[0]["status"] = "idle"
            worker.poll_once(FakeAO(), sink=delivered.append, session_id="sess-x")
            self.assertEqual(len(delivered), 2)
            self.assertIn("idle", delivered[1])

    def test_disabled_relay_polls_nothing(self):
        worker = plugin_api.RelayWorker(poll_seconds=0.0, debounce_s=0.0)

        class Boom:
            def projects(self):
                raise AssertionError("poll must not run while relay is disabled")

        self.assertFalse(worker.tick(Boom(), sink=lambda text: None))


if __name__ == "__main__":
    unittest.main()
