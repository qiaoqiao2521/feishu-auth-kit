import copy
import json
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from feishu_auth_kit.transport.cloud_bridge import BridgeError, EncryptedStore
from feishu_auth_kit.transport.realtime_io import (
    EventQueue,
    FileWake,
    normalize_owner_event,
    read_queued,
    verified_identity,
)


class RealtimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = EncryptedStore(Path(self.temp.name))
        self.store.initialize()
        self.bot = {
            "app_id": "cli_test",
            "app_secret": "TEST_ONLY_SECRET",
            "brand": "feishu",
            "owner_open_id": "ou_owner",
            "owner_source": "registration",
        }
        self.store.write("bot", self.bot)
        self.store.write(
            "binding",
            {
                "app_id": "cli_test",
                "name_matches": True,
                "owner_matches": True,
                "overview_delivery": {"chat_id": "oc_owner"},
                "receive_test": {"baseline_ids": []},
            },
        )
        self.queue = EventQueue(self.store, self.bot, "oc_owner")
        self.event = {
            "header": {
                "app_id": "cli_test",
                "event_id": "ev_test",
                "token": "DO_NOT_PERSIST_TOKEN",
            },
            "event": {
                "sender": {"sender_type": "user", "sender_id": {"open_id": "ou_owner"}},
                "message": {
                    "chat_type": "p2p",
                    "chat_id": "oc_owner",
                    "message_id": "om_test",
                    "message_type": "text",
                    "create_time": "1000",
                    "content": json.dumps({"text": "ENCRYPTED_PRIVATE_MESSAGE"}),
                },
            },
        }

    def tearDown(self):
        self.queue.db.close()
        self.temp.cleanup()

    def test_owner_app_chat_and_sender_guard(self):
        paths = [
            ("header", "app_id"),
            ("event", "sender", "sender_type"),
            ("event", "sender", "sender_id", "open_id"),
            ("event", "message", "chat_type"),
            ("event", "message", "chat_id"),
        ]
        for path in paths:
            event = copy.deepcopy(self.event)
            node = event
            for key in path[:-1]:
                node = node[key]
            node[path[-1]] = "OTHER"
            self.assertIsNone(normalize_owner_event(event, self.bot, "oc_owner"))

    def test_metadata_only_and_encrypted_durable_queue(self):
        self.queue.receive(self.event, 1500)
        result = self.queue.wait(0, 0)
        self.assertEqual(result["events"][0]["network_delay_ms"], 500)
        self.assertNotIn("ENCRYPTED_PRIVATE_MESSAGE", json.dumps(result))
        raw = (self.queue.folder / "events.sqlite3").read_bytes()
        self.assertNotIn(b"ENCRYPTED_PRIVATE_MESSAGE", raw)
        self.assertNotIn(b"DO_NOT_PERSIST_TOKEN", raw)
        encrypted = self.queue.db.execute("SELECT payload FROM events").fetchone()[0]
        clean = self.queue.cipher.decrypt(encrypted)
        self.assertIn(b"ENCRYPTED_PRIVATE_MESSAGE", clean)
        self.assertNotIn(b"DO_NOT_PERSIST_TOKEN", clean)
        self.assertEqual(stat.S_IMODE((self.queue.folder / "events.sqlite3").stat().st_mode), 0o600)
        reopened = EventQueue(self.store, self.bot, "oc_owner")
        self.assertEqual(reopened.wait(0, 0)["last_seq"], 1)
        reopened.db.close()

    def test_dedup_and_no_watermark_consumption(self):
        before = self.store.read("binding")
        self.assertIsNotNone(self.queue.receive(self.event, 1500))
        self.assertIsNone(self.queue.receive(self.event, 1600))
        self.assertEqual(self.queue.wait(0, 0)["last_seq"], 1)
        self.assertEqual(self.store.read("binding"), before)

    def test_condition_wait_wakes_on_event(self):
        result = []
        thread = threading.Thread(target=lambda: result.append(self.queue.wait(0, 2)))
        thread.start()
        time.sleep(0.03)
        sent = time.monotonic()
        self.queue.receive(self.event, 1500)
        thread.join(0.5)
        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - sent, 0.5)
        self.assertEqual(result[0]["status"], "events")

    def test_inotify_wakes_without_polling(self):
        watcher = FileWake(self.queue.folder)
        result = []
        try:
            thread = threading.Thread(target=lambda: result.append(watcher.wait(2)))
            thread.start()
            time.sleep(0.03)
            sent = time.monotonic()
            self.queue.receive(self.event, 1500)
            thread.join(0.5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result, [True])
            self.assertLess(time.monotonic() - sent, 0.5)
            self.assertEqual(self.queue.wait(0, 0)["last_seq"], 1)
        finally:
            watcher.close()

    def test_parent_read_shares_chat_watermark(self):
        self.queue.receive(self.event, 1500)
        events = self.queue.wait(0, 0)["events"]
        with patch(
            "feishu_auth_kit.transport.realtime_io.create_owner_transport", return_value=object()
        ):
            messages = read_queued(self.store, events)
            self.assertEqual(messages[0]["text"], "ENCRYPTED_PRIVATE_MESSAGE")
            self.assertEqual(read_queued(self.store, events), [])
        self.assertIn("om_test", self.store.read("binding")["chat_seen_ids"])

    def test_history_read_is_respected(self):
        binding = self.store.read("binding")
        binding["chat_seen_ids"] = ["om_test"]
        self.store.write("binding", binding)
        self.queue.receive(self.event, 1500)
        with patch(
            "feishu_auth_kit.transport.realtime_io.create_owner_transport", return_value=object()
        ):
            self.assertEqual(read_queued(self.store, self.queue.wait(0, 0)["events"]), [])

    def test_timeout_and_stop(self):
        self.assertEqual(self.queue.wait(0, 0)["status"], "timeout")
        self.queue.set_state("stopped")
        self.assertEqual(self.queue.wait(0, 100)["status"], "stopped")

    def test_unverified_binding_blocked(self):
        binding = self.store.read("binding")
        binding["owner_matches"] = False
        self.store.write("binding", binding)
        with self.assertRaises(BridgeError):
            verified_identity(self.store)


if __name__ == "__main__":
    unittest.main()
