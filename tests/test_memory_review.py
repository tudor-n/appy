import json
import threading
import time
import unittest

from memory_review import MemoryReview

ITEM = {"slug": "bench", "title": "Bench", "summary": "Replaced the PSU.", "before": "# Bench\n",
        "after": "# Bench\n- Korad\n", "stale": False}


class FakeClient:
    def __init__(self):
        self.items = [dict(ITEM)]
        self.decisions = []
        self.befores = []
        self.down = False
        self.next_decision = None
        self.undone = "memory: Replaced the PSU. [approved]"
        self.auto = False
        self.set_result = None
        self.head = "aaa"
        self.auto_items = []
        self.undo_calls = []
        self.refuse = set()
        self.undo_extra = {}

    def settings(self):
        return None if self.down else {"auto_approve": self.auto}

    def set_settings(self, auto_approve):
        if self.set_result is not None:
            return self.set_result
        self.auto = auto_approve
        return {"auto_approve": auto_approve, "accepted": [], "stale": []}

    def changes(self, after):
        if self.down:
            return None
        items = self.auto_items if after == "aaa" else []
        return {"head": "bbb" if items else after or self.head, "items": items}

    def proposals(self):
        return None if self.down else [dict(i) for i in self.items]

    def decide(self, slug, decision, text=None, before=None):
        self.decisions.append((slug, decision, text))
        self.befores.append(before)
        if self.down:
            return {"error": "long-term memory is unreachable right now"}
        if self.next_decision is not None:
            return self.next_decision
        self.items = [i for i in self.items if i["slug"] != slug]
        if decision == "accept":
            return {"status": "accepted", "commit": "abc", "summary": ITEM["summary"]}
        if decision == "edit":
            return {"status": "edited", "commit": "def", "summary": ITEM["summary"]}
        return {"status": "denied"}

    def undo(self, expect=None):
        self.undo_calls.append(expect)
        if expect is not None and expect in self.refuse:
            return {"error": "memory changed since: undo the newer change first"}
        if self.down:
            return {"error": "long-term memory is unreachable right now"}
        if not self.undone:
            return {"undone": None, "commit": None}
        return {"undone": self.undone, "commit": "f00", **self.undo_extra}


class MemoryReviewTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.lists = []
        self.chips = []
        self.changed = threading.Condition()

        def emit_list(items):
            with self.changed:
                self.lists.append(items)
                self.changed.notify_all()

        self.facts = []
        self.commits = []
        self.errors = []

        def emit_chip(kind, summary, facts=(), dropped=(), commit=""):
            with self.changed:
                self.chips.append((kind, summary))
                self.facts.append((list(facts), list(dropped)))
                self.commits.append(commit)
                self.changed.notify_all()

        def emit_error(text):
            with self.changed:
                self.errors.append(text)
                self.changed.notify_all()

        self.settings = []
        self.notes = []

        def emit_settings(on):
            with self.changed:
                self.settings.append(on)
                self.changed.notify_all()

        def on_change(line):
            with self.changed:
                self.notes.append(line)
                self.changed.notify_all()

        self.review = MemoryReview(self.client, emit_list, emit_chip, emit_error=emit_error,
                                   emit_settings=emit_settings,
                                   on_change=on_change, poll_seconds=0.05)

    def tearDown(self):
        self.review.stop()

    def wait(self, what, timeout=2.0):
        deadline = time.time() + timeout
        with self.changed:
            while not what():
                left = deadline - time.time()
                if left <= 0:
                    self.fail("timed out")
                self.changed.wait(left)

    def test_start_emits_proposals(self):
        self.review.start()
        self.wait(lambda: self.lists)
        self.assertEqual(self.lists[0], [ITEM])

    def test_poll_only_emits_changes(self):
        self.review.start()
        self.wait(lambda: self.lists)
        time.sleep(0.3)  # several polls, nothing changed
        self.assertEqual(len(self.lists), 1)
        self.client.items = []
        self.wait(lambda: len(self.lists) == 2)
        self.assertEqual(self.lists[1], [])

    def test_accept_emits_chip_and_refreshes(self):
        self.review.start()
        self.wait(lambda: self.lists)
        self.review.decide("bench", "accept", None)
        self.wait(lambda: self.chips and len(self.lists) >= 2)
        self.assertEqual(self.chips, [("approved", "Replaced the PSU.")])
        self.assertEqual(self.lists[-1], [])
        self.assertEqual(self.client.decisions, [("bench", "accept", None)])

    def test_edit_emits_edited_chip(self):
        self.review.start()
        self.review.decide("bench", "edit", "# Bench\n- Korad KA3005P\n")
        self.wait(lambda: self.chips)
        self.assertEqual(self.chips, [("edited", "Replaced the PSU.")])
        self.assertEqual(self.client.decisions[0][2], "# Bench\n- Korad KA3005P\n")

    def test_edit_describes_the_note_the_mac_saved(self):
        # The Mac carries an edit over to the current note, keeping a fact remembered
        # since the proposal was written: that fact wasn't removed
        self.client.next_decision = {"status": "edited", "commit": "def", "summary": "s",
                                     "text": "# Bench\n- Korad\n- Pinecil\n"}
        self.review.start()
        self.review.decide("bench", "edit", "# Bench\n- Korad\n", "# Bench\n- Pinecil\n")
        self.wait(lambda: self.notes)
        self.assertEqual(self.facts[0], (["Korad"], []))
        self.assertEqual(self.notes, ["note 'bench' now also says: Korad"])

    def test_no_chip_when_nothing_was_committed(self):
        self.review.start()
        self.wait(lambda: self.lists)
        self.client.next_decision = {"status": "edited", "commit": None, "summary": "same text"}
        self.review.decide("bench", "edit", "# Bench\n")
        self.wait(lambda: len(self.lists) >= 2)
        self.assertEqual(self.chips, [])

    def test_edit_forwards_the_before_it_was_based_on(self):
        self.review.start()
        self.review.decide("bench", "edit", "# Bench\n- x\n", "# Bench\n")
        self.wait(lambda: self.client.decisions)
        self.assertEqual(self.client.befores, ["# Bench\n"])

    def test_stale_accept_shows_updating(self):
        self.review.start()
        self.wait(lambda: self.lists)
        self.client.next_decision = {"status": "stale"}
        self.client.items[0]["stale"] = True
        self.review.decide("bench", "accept", None)
        self.wait(lambda: len(self.lists) >= 2)
        self.assertTrue(self.lists[-1][0]["stale"])
        self.assertEqual(self.chips, [])

    def test_failed_decision_refreshes(self):
        # the proposal is gone (denied elsewhere, or a forget discarded it)
        self.review.start()
        self.wait(lambda: self.lists)
        self.client.next_decision = {"error": "there's no pending update for 'bench'"}
        self.client.items = []
        self.review.decide("bench", "accept", None)
        self.wait(lambda: len(self.lists) >= 2)
        self.assertEqual(self.lists[-1], [])

    def test_edit_error_reaches_ui(self):
        self.review.start()
        self.wait(lambda: self.lists)
        self.client.next_decision = {"error": "edit not saved: lost the title line"}
        self.review.decide("bench", "edit", "no title")
        self.wait(lambda: len(self.lists) >= 2)
        self.assertEqual(self.lists[-1][0]["error"], "edit not saved: lost the title line")

    def test_service_down_decision_keeps_card(self):
        self.review.start()
        self.wait(lambda: self.lists)
        self.client.down = True
        self.review.decide("bench", "accept", None)
        self.wait(lambda: len(self.lists) >= 2)
        [item] = self.lists[-1]  # the card stays, with the reason, buttons usable again
        self.assertEqual(item["slug"], "bench")
        self.assertIn("unreachable", item["error"])

    def test_undo_emits_undid_chip(self):
        self.review.start()
        self.review.undo()
        self.wait(lambda: self.chips)
        self.assertEqual(self.chips, [("undo", "memory: Replaced the PSU. [approved]")])
        self.client.undone = None
        self.review.undo()
        time.sleep(0.2)
        self.assertEqual(len(self.chips), 1)  # nothing left to undo: no chip

    def test_tool_commit_chip_only_when_committed(self):
        line = json.dumps({"slug": "core", "line": "- Tudor lives in Sibiu.", "commit": "abc"})
        self.review.tool_committed("memory_remember", line)
        self.review.tool_committed("memory_forget", json.dumps({"line": "- x", "commit": "d"}))
        self.review.tool_committed("memory_remember", json.dumps({"line": "- y", "commit": None}))
        self.review.tool_committed("memory_remember", json.dumps({"error": "full"}))
        self.review.tool_committed("get_weather", line)
        self.review.tool_committed("memory_remember", "not json")
        self.assertEqual(self.chips, [("remember", "- Tudor lives in Sibiu."), ("forget", "- x")])
        self.assertEqual(self.facts, [(["Tudor lives in Sibiu."], []), ([], ["x"])])

    def test_undo_reverts_exactly_the_pills_commits(self):
        self.review.start()
        self.review.undo(["c3", "c2", "c1"])
        self.wait(lambda: len(self.chips) >= 3)
        self.assertEqual(self.client.undo_calls, ["c3", "c2", "c1"])
        self.assertEqual(self.errors, [])

    def test_undo_stops_when_something_newer_changed_memory(self):
        self.client.refuse = {"c2"}
        self.review.start()
        self.review.undo(["c3", "c2", "c1"])
        self.wait(lambda: self.errors)
        self.assertEqual(self.client.undo_calls, ["c3", "c2"])  # c1 not attempted
        self.assertEqual(len(self.chips), 1)
        self.assertIn("newer", self.errors[0])

    def test_failed_plain_undo_is_shown(self):
        self.client.down = True
        self.review.start()
        self.review.undo()
        self.wait(lambda: self.errors)
        self.assertIn("unreachable", self.errors[0])

    def test_chips_carry_the_commit(self):
        self.review.start()
        self.review.decide("bench", "accept", None)
        self.wait(lambda: self.chips)
        self.assertEqual(self.commits[0], "abc")
        self.review.tool_committed("memory_remember",
                                   json.dumps({"line": "- y", "commit": "d1"}))
        self.assertEqual(self.commits[-1], "d1")

    def test_chips_carry_the_facts(self):
        self.client.items = [dict(ITEM, before="# Bench\n- Riden\n", after="# Bench\n- Korad\n")]
        self.review.start()
        self.review.decide("bench", "accept", None)
        self.wait(lambda: self.chips)
        self.assertEqual(self.facts[0], (["Korad"], ["Riden"]))
        self.client.auto_items = [{"commit": "bbb", "slug": "bench", "summary": "s",
                                   "added": ["A."], "removed": []}]
        self.wait(lambda: len(self.chips) >= 2)
        self.assertEqual((self.chips[1][0], self.facts[1]), ("auto", (["A."], [])))


    def test_start_sends_the_setting(self):
        self.client.auto = True
        self.review.start()
        self.wait(lambda: self.settings)
        self.assertEqual(self.settings, [True])

    def test_auto_commits_become_chips_and_notes(self):
        self.review.start()
        self.wait(lambda: self.lists)
        self.client.auto_items = [{"commit": "bbb", "slug": "bench", "summary": "PSU is a Korad",
                                   "added": ["PSU is a Korad."], "removed": ["PSU is a Riden."]}]
        self.wait(lambda: self.chips)
        self.assertEqual(self.chips, [("auto", "PSU is a Korad")])
        self.assertEqual(self.notes, ["note 'bench' now also says: PSU is a Korad.; "
                                      "no longer says: PSU is a Riden."])
        time.sleep(0.2)  # later polls ask after "bbb": no repeats
        self.assertEqual(len(self.chips), 1)

    def test_head_is_taken_at_start_so_old_auto_commits_are_not_replayed(self):
        self.client.head = "zzz"  # commits before "zzz" are history
        self.client.auto_items = [{"commit": "bbb", "slug": "bench", "summary": "old"}]
        self.review.start()
        self.wait(lambda: self.lists)
        time.sleep(0.2)
        self.assertEqual(self.chips, [])

    def test_accept_edit_undo_reach_on_change(self):
        self.review.start()
        self.review.decide("bench", "accept", None)
        self.wait(lambda: self.notes)
        self.client.items = [dict(ITEM)]
        self.review.decide("bench", "edit", "# Bench\n- x\n")
        self.wait(lambda: len(self.notes) >= 2)
        self.review.undo()
        self.wait(lambda: len(self.notes) >= 3)
        self.assertEqual(self.notes, ["note 'bench' now also says: Korad",
                                      "note 'bench' now also says: x",
                                      "undone, no longer true: Replaced the PSU."])

    def test_undo_note_says_what_the_undo_changed(self):
        self.client.undone = "memory: forget bench: The PSU is a Korad."
        self.client.undo_extra = {"slug": "bench", "added": ["The PSU is a Korad."],
                                  "removed": []}
        self.review.start()
        self.review.undo()
        self.wait(lambda: self.notes)
        self.assertEqual(self.notes, ["undone: note 'bench' now also says: The PSU is a Korad."])

    def test_note_describes_removed_facts_and_falls_back_to_the_summary(self):
        self.client.items = [dict(ITEM, before="# Bench\n- Riden\n- Rigol\n",
                                  after="# Bench\n- Korad\n- Rigol\n")]
        self.review.start()
        self.review.decide("bench", "accept", None)
        self.wait(lambda: self.notes)
        self.assertEqual(self.notes[0], "note 'bench' now also says: Korad; no longer says: Riden")
        self.client.items = [dict(ITEM, before="# Bench\n> old\n", after="# Bench\n> new\n")]
        self.review.refresh()
        self.wait(lambda: len(self.lists) >= 3)
        self.review.decide("bench", "accept", None)
        self.wait(lambda: len(self.notes) >= 2)
        self.assertEqual(self.notes[1], "note 'bench' changed: Replaced the PSU.")

    def test_setting_is_fetched_again_until_the_mac_answers(self):
        self.client.down = True  # e.g. booted before the VPN was up
        self.review.start()
        time.sleep(0.15)
        self.assertEqual(self.settings, [])
        self.client.down = False
        self.wait(lambda: self.settings)
        self.assertEqual(self.settings, [False])
        time.sleep(0.2)
        self.assertEqual(self.settings, [False])  # once known, not resent every poll

    def test_undo_announces_pending_auto_commits_first(self):
        self.review.stop()
        self.review = MemoryReview(self.client, lambda items: None,
                                   lambda k, s, *facts: self.chips.append((k, s)),
                                   on_change=self.notes.append, poll_seconds=60)
        self.review.start()
        self.review.refresh()  # takes the head
        time.sleep(0.1)
        self.client.auto_items = [{"commit": "bbb", "slug": "bench", "summary": "PSU is a Korad"}]
        self.review.undo()
        deadline = time.time() + 2
        while len(self.chips) < 2 and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual([k for k, _ in self.chips], ["auto", "undo"])

    def test_set_auto_approve_emits_the_macs_value(self):
        self.review.start()
        self.wait(lambda: self.settings)
        self.review.set_auto_approve(True)
        self.wait(lambda: len(self.settings) >= 2)
        self.assertEqual(self.settings[-1], True)
        self.client.set_result = {"error": "long-term memory is unreachable right now"}
        self.review.set_auto_approve(False)
        self.wait(lambda: len(self.settings) >= 3)
        self.assertEqual(self.settings[-1], True)  # the Mac still says on

if __name__ == "__main__":
    unittest.main()
