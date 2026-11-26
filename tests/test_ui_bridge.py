import unittest

from fastapi.testclient import TestClient

from ui_bridge import UiBridge


class UiBridgeTests(unittest.TestCase):
    def test_compact_message_calls_on_compact(self):
        calls = []
        bridge = UiBridge(on_compact=lambda: calls.append("compact"))
        with TestClient(bridge.app).websocket_connect("/ws") as ws:
            ws.send_json({"type": "compact"})
            ws.send_json({"type": "user_text", "text": "  "})  # ignored; flushes the first
        self.assertEqual(calls, ["compact"])


if __name__ == "__main__":
    unittest.main()
