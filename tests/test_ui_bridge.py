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


if __name__ == "__main__":
    unittest.main()
