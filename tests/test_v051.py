"""Honest per-session state enum (v0.5.1, extended v0.8.2).

``active`` must mean ACTUALLY WORKING: a session with a finished turn
(status=idle, displayStatus="Awaiting PR") stays on its AO board column but
must never count as active. The enum:

  working     live turn
  idle        alive, no turn
  awaiting_pr work finished, PR not opened yet
  exited      agent process gone, session resumable (v0.8.2: its own state —
              labeling it "Archived" made live workers read as dead)
  archived    not alive (terminated — the archive strip's population)

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

    def test_exited_is_exited_never_archived(self):
        # v0.8.2: an exited session is resumable and keeps its AO board
        # column — labeling it "Archived" made live workers read as dead
        # (Wesley's v0.8.0 board report). Termination stays the ONLY
        # archived state.
        self.assertEqual(state_for(session(status="exited")), "exited")
        self.assertNotEqual(state_for(session(status="exited")), "archived")

    def test_terminated_beats_exited(self):
        # Termination is definitive even when the row also looks exited.
        self.assertEqual(
            state_for(session(status="exited", terminated=True)), "archived")

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
        states = ("working", "idle", "awaiting_pr", "exited", "archived")
        self.assertEqual(set(plugin_api.SESSION_STATES), set(states))


class FrontendStateContractTest(unittest.TestCase):
    """The frontend ships its own state fallback (deriveState) and labels —
    they must agree with the backend enum or the chip lies again."""

    def test_frontend_labels_and_fallback_cover_exited(self):
        import re
        from pathlib import Path
        js = (Path(__file__).resolve().parent.parent / "dashboard" / "dist" / "index.js").read_text()
        self.assertIn('exited: "Exited"', js)
        # deriveState must map exited → exited (never "archived")
        self.assertRegex(js, r'if \(st === "exited"\) return "exited";')
        self.assertRegex(
            js, r'if \(st === "terminated"\) return "archived";')
        # and terminated must be checked BEFORE exited in deriveState
        term = js.index('if (st === "terminated") return "archived";')
        exit_ = js.index('if (st === "exited") return "exited";')
        self.assertLess(term, exit_)

    def test_active_only_toggle_is_actually_consulted(self):
        # v0.8.2 regression: the v0.5.1 rewrite of filterSession dropped the
        # read of the `activeOnly` state (opts alone decided), so "Show all"
        # was a no-op — every non-working card was permanently hidden and the
        # live board read as "everything is archived". The gate must consult
        # BOTH the toggle state and the per-caller opt-out.
        from pathlib import Path
        js = (Path(__file__).resolve().parent.parent / "dashboard" / "dist" / "index.js").read_text()
        self.assertIn("if (activeOnly && applyActiveOnly && stateOf(c) !== \"working\") return false;",
                      js)

    def test_orchestrator_strip_and_archive_are_not_active_only_gated(self):
        # v0.8.3: the orchestrator strip must not hide under Active-only (the
        # coordinators vanish the moment none is mid-turn), and the archive
        # strip stays collapsed by default so 300 rows of history don't read
        # as the board. Both persist/express their contract in index.js.
        from pathlib import Path
        js = (Path(__file__).resolve().parent.parent / "dashboard" / "dist" / "index.js").read_text()
        self.assertIn("filterSession(c, { activeOnly: false, recency: false });",
                      js)  # shared by matchesArchiveFilters AND matchesOrchFilters
        self.assertIn('window.localStorage.getItem("ao-bridge.archExpanded")', js)
        self.assertIn('"ao-bridge.archExpanded"', js)


if __name__ == "__main__":
    unittest.main()
