"""Column-mapping contracts: AO session state -> board column.

Rules verified against the live daemon's observed value domain (see
column_for docstring in plugin_api.py).
"""

import conftest  # noqa: F401
import unittest

from fake_ao_server import FakeAO, make_session

plugin_api = conftest.load_plugin_api()

column_for = plugin_api.column_for
card_for = plugin_api.COLUMN_ORDER


class ColumnMappingTest(unittest.TestCase):
    def test_terminated_goes_to_archive_first(self):
        # A terminated session wins over every other signal.
        s = make_session("s1", status="terminated", terminated=True)
        self.assertEqual(column_for(s), "archive")

    def test_merged_is_done(self):
        self.assertEqual(column_for(make_session("s1", status="merged")), "done")

    def test_review_pending_is_review(self):
        self.assertEqual(column_for(make_session("s1", status="review_pending")), "review")

    def test_needs_review_column_is_review(self):
        self.assertEqual(column_for(make_session("s1", status="idle",
                                                 kanban_column="needs_review")), "review")

    def test_ci_failed_is_blocked(self):
        self.assertEqual(column_for(make_session("s1", status="ci_failed")), "blocked")

    def test_provision_failed_is_blocked(self):
        self.assertEqual(column_for(make_session("s1", status="idle",
                                                 provision_state="failed")), "blocked")

    def test_provisioning_is_queued(self):
        self.assertEqual(column_for(make_session("s1", status="idle",
                                                 provision_state="provisioning")), "queued")

    def test_awaiting_pr_ready_is_done(self):
        self.assertEqual(column_for(make_session("s1", status="idle",
                                                 kanban_column="ready")), "done")

    def test_display_status_review_is_review(self):
        self.assertEqual(column_for(make_session("s1", status="idle",
                                                 display_status="Needs human review")), "review")

    def test_display_status_awaiting_pr_is_done(self):
        self.assertEqual(column_for(make_session("s1", status="idle",
                                                 display_status="Awaiting PR")), "done")

    def test_working_idle_and_exited_are_running(self):
        for status in ("working", "idle", "exited"):
            self.assertEqual(column_for(make_session("s1", status=status)), "running", status)

    def test_column_order_covers_all_mapped_columns(self):
        # "archive" is intentionally absent since v0.6.0: terminated workers
        # join the `archived` strip population, not a board column.
        for column in ("done", "review", "blocked", "queued", "running"):
            self.assertIn(column, card_for)


class CardReductionTest(unittest.TestCase):
    def test_card_fields_for_project_session(self):
        pr = {"url": "https://github.com/x/y/pull/1", "number": 1, "state": "open", "ci": "unknown"}
        s = make_session("s1", project_id="personal", display_name="my task", prs=[pr])
        card = plugin_api.card_for(s, {"personal": "personal"})
        self.assertEqual(card["id"], "s1")
        self.assertEqual(card["displayName"], "my task")
        self.assertEqual(card["project"], "personal")
        self.assertEqual(card["harness"], "hermes")
        self.assertEqual(card["branch"], "feat/test")
        self.assertEqual(card["prs"][0]["url"], pr["url"])
        self.assertEqual(card["prs"][0]["number"], 1)

    def test_card_without_project_is_standalone(self):
        card = plugin_api.card_for(make_session("s2"), {})
        self.assertEqual(card["project"], "standalone")
        self.assertEqual(card["projectId"], "")

    def test_malformed_pr_is_dropped(self):
        s = make_session("s3", prs=[{"number": 2}, "junk", {"url": "https://x/pr/3", "number": 3}])
        card = plugin_api.card_for(s, {})
        self.assertEqual([p["number"] for p in card["prs"]], [3])
        self.assertEqual([p["url"] for p in card["prs"]], ["https://x/pr/3"])


class BoardSnapshotTest(unittest.TestCase):
    def test_snapshot_groups_cards_by_column(self):
        ao = FakeAOFromFixtures()
        snapshot = plugin_api.board_snapshot(ao)
        self.assertEqual(snapshot["counts"]["done"], 1)
        self.assertEqual(snapshot["counts"]["running"], 1)
        self.assertNotIn("archive", snapshot["counts"])
        done_cards = [c for c in snapshot["cards"] if c["column"] == "done"]
        self.assertEqual(done_cards[0]["project"], "hermes-agent")
        self.assertTrue(snapshot["columns"][0]["id"] in ("queued", "running", "review", "blocked", "done"))
        # Terminated workers leave the columns entirely (the strip population).
        self.assertEqual([c["id"] for c in snapshot["archived"]], ["s-arch"])


class FakeAOFromFixtures(FakeAO):
    def __init__(self):
        super().__init__()
        self._projects = [{"id": "hermes-agent", "name": "hermes-agent", "path": "/x"}]
        self.base_url = "http://fake.local"
        self.add_session(make_session("s-done", project_id="hermes-agent", status="merged"))
        self.add_session(make_session("s-arch", status="terminated", terminated=True))
        self.add_session(make_session("s-run", project_id="personal", status="working"))

    def projects(self):
        return self._projects

    def sessions(self):
        return list(self.sessions_by_id.values())

    def conversation(self, session_id):
        # board_snapshot fetches working sessions' plans; this fixture has none
        return {}

    def sessions_usage(self):
        return []  # no usage fixtures in this test

    def sessions_memory(self):
        return []


if __name__ == "__main__":
    unittest.main()
