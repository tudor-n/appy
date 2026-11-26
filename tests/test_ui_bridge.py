import threading
import unittest

from fastapi.testclient import TestClient

from ui_bridge import UiBridge


def receive(ws, timeout: float = 2.0) -> dict:
    """ws.receive_json() that fails instead of hanging when nothing arrives."""
    box = []
    t = threading.Thread(target=lambda: box.append(ws.receive_json()), daemon=True)
    t.start()
    t.join(timeout)
    if not box:
        raise AssertionError("no message from the bridge")
    return box[0]


class UiBridgeTests(unittest.TestCase):
    def test_late_client_gets_the_current_status(self):
        # "booting" is sent while the overlay is usually still reconnecting; a client
        # that connects afterwards must still see it
        bridge = UiBridge(get_effort=lambda: "none")
        bridge.status("booting")
        with TestClient(bridge.app).websocket_connect("/ws") as ws:
            first, second = receive(ws), receive(ws)
        self.assertEqual(first, {"type": "effort", "value": "none"})
        self.assertEqual(second, {"type": "status", "state": "booting", "label": "booting"})

    def test_replays_only_the_latest_status(self):
        bridge = UiBridge()
        bridge.status("booting")
        bridge.status("idle")
        with TestClient(bridge.app).websocket_connect("/ws") as ws:
            self.assertEqual(receive(ws)["state"], "idle")

    def test_no_status_yet_sends_nothing_extra(self):
        bridge = UiBridge(get_effort=lambda: "low")
        with TestClient(bridge.app).websocket_connect("/ws") as ws:
            self.assertEqual(receive(ws), {"type": "effort", "value": "low"})
            ws.send_json({"type": "user_text", "text": "  "})  # nothing else queued

    def test_compact_message_calls_on_compact(self):
        calls = []
        bridge = UiBridge(on_compact=lambda: calls.append("compact"))
        with TestClient(bridge.app).websocket_connect("/ws") as ws:
            ws.send_json({"type": "compact"})
            ws.send_json({"type": "user_text", "text": "  "})  # ignored; flushes the first
        self.assertEqual(calls, ["compact"])

    def test_late_client_gets_proposals(self):
        bridge = UiBridge()
        bridge.memory_proposals([{"slug": "bench"}])
        with TestClient(bridge.app).websocket_connect("/ws") as ws:
            self.assertEqual(receive(ws), {"type": "memory_proposals", "items": [{"slug": "bench"}]})

    def test_memory_committed_is_sent(self):
        bridge = UiBridge()
        sent = []
        bridge.emit = sent.append
        bridge.memory_committed("approved", "added the HUD")
        self.assertEqual(sent, [{"type": "memory_committed", "kind": "approved", "summary": "added the HUD"}])

    def test_memory_decision_and_undo_reach_callbacks(self):
        calls = []
        bridge = UiBridge(on_memory_decision=lambda *a: calls.append(a),
                          on_memory_undo=lambda: calls.append("undo"))
        with TestClient(bridge.app).websocket_connect("/ws") as ws:
            ws.send_json({"type": "memory_decision", "slug": "bench", "decision": "accept"})
            ws.send_json({"type": "memory_decision", "slug": "bench", "decision": "edit", "text": "# B\n"})
            ws.send_json({"type": "memory_undo"})
            ws.send_json({"type": "user_text", "text": "  "})  # flushes
        self.assertEqual(calls, [("bench", "accept", None), ("bench", "edit", "# B\n"), "undo"])

    def test_bad_decisions_ignored(self):
        calls = []
        bridge = UiBridge(on_memory_decision=lambda *a: calls.append(a))
        with TestClient(bridge.app).websocket_connect("/ws") as ws:
            for bad in ({"slug": "bench", "decision": "maybe"}, {"decision": "accept"},
                        {"slug": "", "decision": "accept"}, {"slug": "bench", "decision": "edit"},
                        {"slug": "bench", "decision": "edit", "text": "   "}):
                ws.send_json({"type": "memory_decision", **bad})
            ws.send_json({"type": "user_text", "text": "  "})
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
