import json
import unittest

from note_attach import NoteAttacher

INDEX = """dummy-robot | Dummy robot Project | Information regarding Tudor's robot project. | 2026-11-27
appy-ui | Appy ui | Appy's UI is made in Rust. | 2026-11-27
mark-i-suit | Mark i suit | The Mark I suit is an arm exoskeleton | 2026-11-27
watering-bot | Watering bot | Details regarding Tudor's automated plant watering system. | 2026-11-27
lumen-project | Lumen project | Lumen is a BI application | 2026-11-27"""

NOTES = {
    "dummy-robot": "# Dummy robot Project\n> Tudor's robot\n- Dummy uses a PCA9685 motor driver.\n",
    "lumen-project": "# Lumen project\n> BI app\n- Lumen's first KPI is MRR.\n",
}


class FakeClient:
    def __init__(self):
        self.reads = []
        self.index = INDEX

    def read(self, slug):
        self.reads.append(slug)
        if slug in NOTES:
            return json.dumps({"slug": slug, "text": NOTES[slug]})
        return json.dumps({"error": "long-term memory is unreachable right now"})

    def context(self):
        return {"core": "", "index": self.index}


class NoteAttacherTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.attacher = NoteAttacher(self.client, INDEX)

    def test_a_mentioned_note_is_attached(self):
        block, slugs = self.attacher.attach("hi appy, remember dummy's components?")
        self.assertEqual(slugs, ["dummy-robot"])
        self.assertIn("note 'dummy-robot'", block)
        self.assertIn("- Dummy uses a PCA9685 motor driver.", block)

    def test_matching_ignores_appy_and_generic_words(self):
        for text in ("hey appy, how are you", "this project is cool", "what about the ui",
                     "my robot is broken", "tudor here"):
            self.assertEqual(self.attacher.matches(text), [], text)
        self.assertEqual(self.attacher.matches("Lumen and the Mark I suit"),
                         ["mark-i-suit", "lumen-project"])
        self.assertEqual(self.attacher.matches("the watering schedule"), ["watering-bot"])
        self.assertEqual(self.attacher.matches("dummyish"), [])  # whole words only

    def test_attached_once_per_conversation(self):
        self.attacher.attach("tell me about dummy")
        block, slugs = self.attacher.attach("and dummy's power supply?")
        self.assertEqual((block, slugs), ("", []))
        self.assertEqual(self.client.reads, ["dummy-robot"])

    def test_release_lets_a_note_attach_again(self):
        _, slugs = self.attacher.attach("dummy?")
        self.attacher.release(slugs)  # the turn failed: Appy never saw it
        self.assertEqual(self.attacher.attach("dummy?")[1], ["dummy-robot"])

    def test_refresh_picks_up_new_notes_and_reattaches_changed_ones(self):
        self.attacher.attach("dummy?")
        self.client.index = INDEX + "\nfifi | Fifi | Tudor's rover | 2026-11-27"
        self.attacher.refresh()
        self.assertEqual(self.attacher.matches("how is fifi"), ["fifi"])
        self.assertEqual(self.attacher.attach("dummy again")[1], ["dummy-robot"])

    def test_unreadable_note_is_skipped_and_retried_later(self):
        block, slugs = self.attacher.attach("mark i suit status?")
        self.assertEqual((block, slugs), ("", []))
        NOTES["mark-i-suit"] = "# Mark i suit\n> arm exoskeleton\n- BLDC motors.\n"
        try:
            self.assertEqual(self.attacher.attach("the suit?")[1], ["mark-i-suit"])
        finally:
            del NOTES["mark-i-suit"]

    def test_several_notes_in_one_block(self):
        block, slugs = self.attacher.attach("compare dummy and lumen")
        self.assertEqual(slugs, ["dummy-robot", "lumen-project"])
        self.assertEqual(block.count("[From your memory"), 1)


if __name__ == "__main__":
    unittest.main()
