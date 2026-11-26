"""Cross-language drift check: the Python side must only use values the Rust UI knows."""
import re
import unittest
from pathlib import Path

from streaming import VALID_EXPRESSIONS
from ui_bridge import VALID_EFFORTS

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


def rust_struct_variant_fields(enum: str, variant: str) -> set[str]:
    src = PROTOCOL_RS.read_text()
    block = re.search(rf"pub enum {enum} \{{(.*?)\n\}}", src, re.DOTALL).group(1)
    fields = re.search(rf"{variant} \{{(.*?)\}}", block, re.DOTALL).group(1)
    return set(re.findall(r"(\w+):", fields))


class ProtocolDriftTests(unittest.TestCase):
    def test_weather_card_matches_rust_widget(self):
        import json

        import tools

        card = tools.weather_card(json.dumps({
            "city": "X", "current": {"temp_c": 1, "condition": "c", "code": 0, "is_day": True},
        }))
        self.assertEqual(card.pop("widget"), "weather")
        self.assertEqual(set(card), rust_struct_variant_fields("Widget", "Weather"))

    def test_expressions_match(self):
        self.assertEqual(set(rust_enum("Expression").values()), set(VALID_EXPRESSIONS))

    def test_efforts_match(self):
        self.assertEqual(set(rust_enum("Effort").values()), set(VALID_EFFORTS))

    def test_status_states_are_known_to_ui(self):
        self.assertLessEqual(PYTHON_STATES, set(rust_enum("AppyState").values()))


if __name__ == "__main__":
    unittest.main()
