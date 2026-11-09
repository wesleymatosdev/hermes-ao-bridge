"""Honest per-session state enum (v0.5.1).

``active`` must mean ACTUALLY WORKING: a session with a finished turn
(status=idle, displayStatus="Awaiting PR") stays on its AO board column but
must never count as active. The enum:

  working     live turn
  idle        alive, no turn
  awaiting_pr work finished, PR not opened yet
  archived    not alive (terminated / exited)

Column placement keeps mirroring AO (column_for is untouched); this enum rides
ON TOP as the honest activity state.
"""

import conftest  # noqa: F401
import unittest

from fake_ao_server import make_session

plugin_api = conftest.load_plugin_api()

state_for = plugin_api.state_for


def session(**kw):
    kw.setdefault("status", "idle")
    return make_session("s1", **kw)


class StateEnumTest(unittest.TestCase):
    def test_idle_awaiting_pr_is_not_active(self):
        # The reported bug: finished agents looked active. status=idle +
        # displayStatus="Awaiting PR" is the exact live payload (verified
        # against the daemon) — it must land in awaiting_pr, never working.
        s = session(display_status="Awaiting PR")
        self.assertEqual(state_for(s), "awaiting_pr")
        self.assertNotEqual(state_for(s), "working")

    def test_working_status_is_working(self):
        self.assertEqual(state_for(session(status="working")), "working")

    def test_plain_idle_is_idle(self):
        self.assertEqual(state_for(session(status="idle")), "idle")

    def test_review_pending_is_idle(self):
        # Alive, no turn — the turn is over, review is a human wait.
        self.assertEqual(state_for(session(status="review_pending")), "idle")

    def test_provisioning_is_idle(self):
        # Alive, no turn yet.
        self.assertEqual(state_for(session(status="idle",
                                           provision_state="provisioning")), "idle")

    def test_terminated_is_archived(self):
        self.assertEqual(state_for(session(status="idle", terminated=True)), "archived")

    def test_status_terminated_beats_awaiting_pr(self):
        # displayStatus can lag the real state; termination is definitive.
        s = session(status="terminated", display_status="Awaiting PR")
        self.assertEqual(state_for(s), "archived")

    def test_exited_is_archived(self):
        # Agent process gone: not alive, so never active.
        self.assertEqual(state_for(session(status="exited")), "archived")

    def test_merged_is_not_working(self):
        self.assertNotEqual(state_for(session(status="merged")), "working")


class StateFieldContractTest(unittest.TestCase):
    def test_card_carries_state(self):
        card = plugin_api.card_for(session(display_status="Awaiting PR"), {})
        self.assertEqual(card["state"], "awaiting_pr")

    def test_card_state_working(self):
        card = plugin_api.card_for(session(status="working"), {})
        self.assertEqual(card["state"], "working")

    def test_state_values_are_the_documented_enum(self):
        states = ("working", "idle", "awaiting_pr", "archived")
        self.assertEqual(set(plugin_api.SESSION_STATES), set(states))


if __name__ == "__main__":
    unittest.main()
