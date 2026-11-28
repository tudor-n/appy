"""Cross-language drift check: the Python side must only use values the Rust UI knows."""
import re
import unittest
from pathlib import Path

from streaming import VALID_EXPRESSIONS
from ui_bridge import MEMORY_DECISIONS, MEMORY_KINDS, VALID_EFFORTS

PROTOCOL_RS = Path(__file__).resolve().parent.parent / "appy-ui" / "src" / "protocol.rs"

# Every state the Python backend can put in a "status" message
PYTHON_STATES = {"idle", "thinking", "speaking", "booting"}


def snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def rust_enum(name: str) -> dict[str, str]:
    """Map variant -> wire name for a fieldless `pub enum` in protocol.rs."""
    src = PROTOCOL_RS.read_text()
    m = re.search(rf"pub enum {name} \{{(.*?)\n\}}", src, re.DOTALL)
    assert m, f"enum {name} not found in {PROTOCOL_RS}"
    variants, rename = {}, None
    for line in m.group(1).splitlines():
        line = line.strip()
        r = re.match(r'#\[serde\(rename = "(\w+)"\)\]', line)
        if r:
            rename = r.group(1)
            continue
        v = re.match(r"(\w+),", line)
        if v:
            variants[v.group(1)] = rename or snake(v.group(1))
            rename = None
    return variants


def rust_struct_fields(name: str) -> set[str]:
    src = PROTOCOL_RS.read_text()
    block = re.search(rf"pub struct {name} \{{(.*?)\n\}}", src, re.DOTALL).group(1)
    return set(re.findall(r"pub (\w+):", block))


class ProtocolDriftTests(unittest.TestCase):
    def test_weather_card_matches_rust_widget(self):
        import json

        import tools

        fields = rust_struct_fields("WeatherReport")
        now = tools.weather_card(json.dumps({
            "city": "X", "current": {"temp_c": 1, "condition": "c", "code": 0, "is_day": True},
        }))
        forecast = tools.weather_card(json.dumps({"city": "X", "forecast": {
            "weekday": "Saturday", "high_c": 5, "low_c": 0, "condition": "c", "code": 61}}))
        for card in (now, forecast):
            self.assertEqual(card.pop("widget"), "weather")
            self.assertEqual(set(card), fields)

    def test_music_card_matches_rust_widget(self):
        import json

        from dj import DJ

        class Riff:
            def is_running(self):
                return True

            def status(self):
                return "Playing"

            def now_playing(self):
                return {"title": "Karma Police", "artist": "Radiohead", "album": "OK Computer"}

        card = DJ(Riff(), None).music_card("music_control", json.dumps(
            {"title": "Karma Police", "artist": "Radiohead", "album": "OK Computer", "state": "playing"}))
        self.assertEqual(card.pop("widget"), "music")
        self.assertEqual(set(card), rust_struct_fields("MusicReport"))

    def test_playback_states_known_to_ui(self):
        import dj

        self.assertLessEqual(set(dj.CARD_STATES), set(rust_enum("PlaybackState").values()))

    def test_weather_icons_are_known_to_ui(self):
        import tools

        python_icons = {icon for pair in tools.WMO_ICON.values() for icon in pair}
        self.assertLessEqual(python_icons, set(rust_enum("WeatherIcon").values()))

    def test_expressions_match(self):
        self.assertEqual(set(rust_enum("Expression").values()), set(VALID_EXPRESSIONS))

    def test_efforts_match(self):
        self.assertEqual(set(rust_enum("Effort").values()), set(VALID_EFFORTS))

    def test_modes_match(self):
        import tools

        self.assertEqual(set(rust_enum("Mode").values()), set(tools.VALID_MODES))

    def test_status_states_are_known_to_ui(self):
        self.assertLessEqual(PYTHON_STATES, set(rust_enum("AppyState").values()))

    def test_memory_kinds_and_decisions_match(self):
        kinds = set(rust_enum("MemoryKind").values()) - {"other"}
        self.assertEqual(kinds, set(MEMORY_KINDS))
        self.assertEqual(set(rust_enum("Decision").values()), set(MEMORY_DECISIONS))

    def test_memory_proposal_fields_match(self):
        import memory_client

        self.assertEqual(rust_struct_fields("Proposal"), set(memory_client.PROPOSAL_FIELDS) | {"error"})


if __name__ == "__main__":
    unittest.main()
