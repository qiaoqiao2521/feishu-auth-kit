import fcntl
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

from feishu_auth_kit.transport.chat_io import send_once
from feishu_auth_kit.transport.cloud_bridge import BridgeError, EncryptedStore, _atomic_private
from feishu_auth_kit.transport.outbound_io import folders, operation_key, submit
from feishu_auth_kit.transport.realtime_io import FileWake


class OutboundTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = EncryptedStore(Path(self.temp.name))
        self.store.initialize()
        self.store.write("bot", {"app_id": "cli_test", "owner_source": "registration"})
        self.store.write(
            "binding",
            {
                "app_id": "cli_test",
                "name_matches": True,
                "owner_matches": True,
                "overview_delivery": {"chat_id": "oc_owner"},
            },
        )
        self.root, self.requests, self.results = folders(self.store)
        self.fd = os.open(self.root / "listener.lock", os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)

    def tearDown(self):
        os.close(self.fd)
        self.temp.cleanup()

    def test_explicit_submit_is_encrypted_and_wakes(self):
        watcher = FileWake(self.requests)
        seen = []

        def worker():
            if watcher.wait(2):
                path = next(self.requests.glob("*.enc"))
                raw = path.read_bytes()
                seen.append(raw)
                payload = json.loads(self.store._cipher().decrypt(raw))
                self.assertEqual(payload["text"], "PRIVATE_REPLY")
                _atomic_private(
                    self.results / (path.stem + ".json"),
                    b'{"status":"sent","message_id":"om_test"}',
                )
                _atomic_private(self.results / "wake.json", b"{}")

        thread = threading.Thread(target=worker)
        thread.start()
        try:
            result = submit(self.store, "op_test", "PRIVATE_REPLY", timeout=2)
            thread.join(1)
            self.assertEqual(result["status"], "sent")
            self.assertNotIn(b"PRIVATE_REPLY", seen[0])
            duplicate = submit(self.store, "op_test", "PRIVATE_REPLY", timeout=0)
            self.assertTrue(duplicate["repeat_blocked"])
            self.assertEqual(len(list(self.requests.glob("*.enc"))), 1)
        finally:
            watcher.close()

    def test_timeout_stays_pending_without_resending(self):
        result = submit(self.store, "op_pending", "private", timeout=0)
        self.assertEqual(result["status"], "pending")
        self.assertFalse(result["automatic_resend"])
        path = self.requests / (operation_key("op_pending") + ".enc")
        before = path.read_bytes()
        submit(self.store, "op_pending", "private", timeout=0)
        self.assertEqual(path.read_bytes(), before)

    def test_operation_cannot_change_content(self):
        submit(self.store, "same_operation", "first", timeout=0)
        with self.assertRaises(BridgeError):
            submit(self.store, "same_operation", "different", timeout=0)

    def test_stopped_sender_does_not_queue(self):
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        with self.assertRaises(BridgeError):
            submit(self.store, "not_sent", "text", timeout=0)
        self.assertEqual(list(self.requests.glob("*.enc")), [])

    def test_crashed_sending_ledger_is_not_replayed(self):
        binding = self.store.read("binding")
        binding["chat_deliveries"] = {"op_crashed": {"status": "sending", "uuid": "existing-id"}}
        self.store.write("binding", binding)
        transport = NS()  # No network methods: reaching one would fail this test.
        result = send_once(self.store, transport, "op_crashed", "do not replay")
        self.assertEqual(result["status"], "sending")
        self.assertTrue(result["repeat_blocked"])


if __name__ == "__main__":
    unittest.main()
