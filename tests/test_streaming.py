import unittest

from streaming import ExpressionTagParser, SpeechChunker, pop_ready_chunk


def feed_all(parser: ExpressionTagParser, pieces: list[str]) -> str:
    out = "".join(parser.feed(p) for p in pieces)
    return out + parser.flush()


class PopReadyChunkTests(unittest.TestCase):
    def test_no_boundary_returns_buffer(self):
        self.assertEqual(pop_ready_chunk("Hello there", True), (None, "Hello there"))

    def test_first_chunk_waits_for_min_length(self):
        # "Hi," is a clause but too short to be spoken on its own
        self.assertEqual(pop_ready_chunk("Hi, there", True), (None, "Hi, there"))

    def test_first_chunk_merges_short_clauses(self):
        chunk, rest = pop_ready_chunk("Well, that is a great question, honestly. More", True)
        self.assertEqual(chunk, "Well, that is a great question,")
        self.assertEqual(rest, "honestly. More")

    def test_later_chunks_split_on_sentences_only(self):
        chunk, rest = pop_ready_chunk("Yes, sure. And then", False)
        self.assertEqual(chunk, "Yes, sure.")
        self.assertEqual(rest, "And then")


class SpeechChunkerTests(unittest.TestCase):
    def test_streams_chunks_and_flushes_tail(self):
        c = SpeechChunker()
        self.assertEqual(c.feed("The weather today is "), [])
        self.assertEqual(c.feed("lovely, warm and sunny"), ["The weather today is lovely,"])
        # After the first chunk only full sentences are released
        self.assertEqual(c.feed(". Grab a hat, ok! Bye"), ["warm and sunny.", "Grab a hat, ok!"])
        self.assertEqual(c.flush(), "Bye")
        self.assertEqual(c.flush(), "")

    def test_flush_strips_whitespace(self):
        c = SpeechChunker()
        c.feed("   ")
        self.assertEqual(c.flush(), "")


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
