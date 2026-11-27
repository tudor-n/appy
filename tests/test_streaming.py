import unittest

from streaming import ExpressionTagParser, SpeechChunker, pop_ready_chunk, speakable


def feed_all(parser: ExpressionTagParser, pieces: list[str]) -> str:
    out = "".join(parser.feed(p) for p in pieces)
    return out + parser.flush()


class PopReadyChunkTests(unittest.TestCase):
    """Kokoro voices each chunk as its own utterance, so a chunk that ends at a comma
    gets a sentence's falling tone. Chunks are therefore always whole sentences."""

    def test_no_boundary_returns_buffer(self):
        self.assertEqual(pop_ready_chunk("Hello there"), (None, "Hello there"))

    def test_never_splits_at_a_comma(self):
        # Regression: "That sounds like a plan, boss." came out as "...a plan. Boss."
        self.assertEqual(
            pop_ready_chunk("That sounds like a plan, boss"), (None, "That sounds like a plan, boss")
        )
        self.assertEqual(
            pop_ready_chunk("That sounds like a plan, boss. Want"),
            ("That sounds like a plan, boss.", "Want"),
        )

    def test_a_short_opening_sentence_goes_on_its_own(self):
        # Used to be glued to the next sentence to reach a minimum length
        self.assertEqual(pop_ready_chunk("Finally. I was"), ("Finally.", "I was"))

    def test_sentence_end_needs_following_whitespace(self):
        # "3." could still become "3.5": wait until the stream moves on
        self.assertEqual(pop_ready_chunk("It's 3."), (None, "It's 3."))


class SpeechChunkerTests(unittest.TestCase):
    def test_streams_sentences_and_flushes_tail(self):
        c = SpeechChunker()
        self.assertEqual(c.feed("The weather today is "), [])
        self.assertEqual(c.feed("lovely, warm and sunny"), [])
        self.assertEqual(
            c.feed(". Grab a hat, ok! Bye"),
            ["The weather today is lovely, warm and sunny.", "Grab a hat, ok!"],
        )
        self.assertEqual(c.flush(), "Bye")
        self.assertEqual(c.flush(), "")

    def test_flush_strips_whitespace(self):
        c = SpeechChunker()
        c.feed("   ")
        self.assertEqual(c.flush(), "")


class SpeakableNumbersTests(unittest.TestCase):
    """Kokoro reads "5.5" as "five five" and "1,500" as "one, five hundred"."""

    def test_decimals_say_point_and_digits(self):
        self.assertEqual(speakable("It's 5.5 degrees."), "It's 5 point 5 degrees.")
        self.assertEqual(speakable("Pi is 3.14."), "Pi is 3 point 1 4.")
        self.assertEqual(speakable("It's 10.05 km"), "It's 10 point 0 5 km")
        self.assertEqual(speakable("It's -2.5 or 5.5%"), "It's -2 point 5 or 5 point 5%")

    def test_thousands_separators_are_dropped(self):
        self.assertEqual(speakable("That's 1,500 people."), "That's 1500 people.")
        self.assertEqual(speakable("1,234,567.89"), "1234567 point 8 9")

    def test_leaves_other_dots_and_commas_alone(self):
        for text in ["Version 2.0.1 is out", "Ping 192.168.1.1", "It's 5. Then 6.",
                     "Days 1, 2, and 3", "Between 1,2 and 3", "No numbers here."]:
            self.assertEqual(speakable(text), text)


class ExpressionTagParserTests(unittest.TestCase):
    def test_tag_in_one_piece(self):
        p = ExpressionTagParser()
        self.assertEqual(feed_all(p, ["[happy] Hello!"]), "Hello!")
        self.assertEqual(p.expression, "happy")

    def test_tag_split_across_pieces(self):
        p = ExpressionTagParser()
        self.assertEqual(p.feed("  [Cur"), "")
        self.assertEqual(p.feed("ious]"), "")
        self.assertEqual(p.feed(" What?"), " What?")
        self.assertEqual(p.expression, "curious")

    def test_no_tag_passes_through(self):
        p = ExpressionTagParser()
        self.assertEqual(feed_all(p, ["Hi", " there"]), "Hi there")
        self.assertEqual(p.expression, "neutral")

    def test_unknown_tag_is_kept_as_text(self):
        p = ExpressionTagParser()
        self.assertEqual(feed_all(p, ["[angry] no"]), "[angry] no")
        self.assertEqual(p.expression, "neutral")

    def test_long_bracket_gives_up(self):
        p = ExpressionTagParser()
        text = "[this is not a tag at all"
        self.assertEqual(feed_all(p, [text]), text)
        self.assertEqual(p.expression, "neutral")

    def test_flush_releases_unclosed_short_reply(self):
        p = ExpressionTagParser()
        self.assertEqual(p.feed("[ok"), "")
        self.assertEqual(p.flush(), "[ok")

    def test_whitespace_only_is_held(self):
        p = ExpressionTagParser()
        self.assertEqual(p.feed("  "), "")
        self.assertEqual(p.feed("[focused]Done."), "Done.")
        self.assertEqual(p.expression, "focused")


if __name__ == "__main__":
    unittest.main()
