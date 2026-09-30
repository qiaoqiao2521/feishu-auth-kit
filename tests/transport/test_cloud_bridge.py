import json
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

from feishu_auth_kit.transport.cloud_bridge import (
    BridgeError,
    EncryptedStore,
    OwnerOnlyTransport,
    RegistrationBridge,
    safe_filename,
)


class FakeResponse:
    def __init__(self, data=None, payload=b"test-file", status=200):
        self.status_code = status
        self.data = data or {}
        self.payload = payload
        self.headers = {"Content-Type": "application/octet-stream"}
        self.closed = False

    def json(self):
        return {"code": 0, "data": self.data}

    def iter_content(self, chunk_size):
        yield self.payload

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


class FakeRegistration:
    def __init__(self):
        self.poll_args = None
        self.outcome = NS(
            status="success",
            result=NS(
                app_id="cli_test_only",
                app_secret="SYNTHETIC_SECRET_ONLY",
                domain="feishu",
                open_id="ou_new_owner",
            ),
        )
        self.url = "https://accounts.feishu.cn/verify?user_code=TEST"

    def init(self):
        return NS(supported_auth_methods=["client_secret"])

    def begin(self):
        return NS(
            device_code="TEST_DEVICE_CODE",
            qr_url=self.url,
            user_code="TEST",
            interval=5,
            expires_in=600,
        )

    def poll(self, code, **kwargs):
        self.poll_args = (code, kwargs)
        return self.outcome


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = EncryptedStore(self.root)
        self.store.initialize()

    def tearDown(self):
        self.temp.cleanup()

    def transport(self, session=None, **overrides):
        auth = NS(
            app_id="cli_test_only", get_tenant_access_token=lambda **kw: NS(token="TEST_TOKEN")
        )
        values = dict(
            app_id="cli_test_only",
            sender_open_id="ou_new_owner",
            chat_type="p2p",
            message_id="om_test",
            message_type="file",
        )
        values.update(overrides)
        value = OwnerOnlyTransport(
            auth_client=auth,
            session=session or FakeSession(),
            app_id="cli_test_only",
            owner_open_id="ou_new_owner",
            brand="feishu",
            parse_context=lambda payload: NS(**values),
            root=self.root,
        )
        value.prepare_file_dirs()
        return value

    def event(self, name="example.txt"):
        return {
            "event": {
                "message": {"content": json.dumps({"file_key": "file_test", "file_name": name})}
            }
        }

    def test_encrypted_store_has_no_plaintext(self):
        self.store.write("bot", {"app_secret": "SYNTHETIC_SECRET_ONLY"})
        self.assertEqual(self.store.read("bot")["app_secret"], "SYNTHETIC_SECRET_ONLY")
        for path in self.store.private.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"SYNTHETIC_SECRET_ONLY", path.read_bytes())
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            else:
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)

    def test_missing_key_not_regenerated(self):
        self.store.write("bot", {"dummy": True})
        self.store.key_path.unlink()
        with self.assertRaises(BridgeError):
            self.store.initialize()

    def test_record_name_traversal_rejected(self):
        with self.assertRaises(BridgeError):
            self.store.write("../../bad", {})

    def test_begin_never_returns_device_code(self):
        bridge = RegistrationBridge(FakeRegistration(), self.store, now=lambda: 1000)
        result = bridge.begin()
        self.assertNotIn("TEST_DEVICE_CODE", json.dumps(result))
        self.assertEqual(self.store.read("registration")["device_code"], "TEST_DEVICE_CODE")
        with self.assertRaises(BridgeError):
            bridge.begin()

    def test_untrusted_verification_url_blocked(self):
        client = FakeRegistration()
        client.url = "https://evil.invalid/verify"
        with self.assertRaises(BridgeError):
            RegistrationBridge(client, self.store).begin()

    def test_poll_pins_new_owner_and_redacts_secret(self):
        client = FakeRegistration()
        bridge = RegistrationBridge(client, self.store, now=lambda: 1000)
        bridge.begin()
        result = bridge.poll(30)
        self.assertNotIn("SYNTHETIC_SECRET_ONLY", json.dumps(result))
        self.assertEqual(result["status"], "configured")
        self.assertEqual(self.store.read("bot")["owner_open_id"], "ou_new_owner")
        self.assertEqual(self.store.read("registration"), {"status": "completed"})
        with self.assertRaises(BridgeError):
            bridge.poll()

    def test_expiry_not_extended_by_poll(self):
        client = FakeRegistration()
        RegistrationBridge(client, self.store, now=lambda: 1000).begin()
        result = RegistrationBridge(client, self.store, now=lambda: 1601).poll()
        self.assertEqual(result, {"status": "expired"})
        self.assertIsNone(client.poll_args)

    def test_remote_error_text_omitted(self):
        client = FakeRegistration()
        client.outcome = NS(status="error", result=None, message="SYNTHETIC_SECRET_ONLY")
        bridge = RegistrationBridge(client, self.store)
        bridge.begin()
        self.assertEqual(bridge.poll(), {"status": "error"})

    def test_missing_owner_blocks_ready_status(self):
        client = FakeRegistration()
        client.outcome.result.open_id = None
        bridge = RegistrationBridge(client, self.store)
        bridge.begin()
        self.assertEqual(bridge.poll()["status"], "owner_verification_required")
        with self.assertRaises(BridgeError):
            OwnerOnlyTransport(
                auth_client=NS(app_id="cli_test_only"),
                session=FakeSession(),
                app_id="cli_test_only",
                owner_open_id=None,
                brand="feishu",
                parse_context=lambda x: x,
                root=self.root,
            )

    def test_outside_file_never_uploaded(self):
        transport = self.transport()
        outside = self.root / "private" / "do-not-send.txt"
        outside.write_text("SYNTHETIC_SECRET_ONLY")
        with self.assertRaises(BridgeError):
            transport.send_file(outside)
        self.assertEqual(transport.session.calls, [])

    def test_symlink_to_secret_blocked(self):
        transport = self.transport()
        link = transport.outgoing / "sneaky.txt"
        link.symlink_to(self.store.key_path)
        with self.assertRaises(BridgeError):
            transport.send_file(link)
        self.assertEqual(transport.session.calls, [])

    def test_send_is_owner_only(self):
        session = FakeSession(
            [
                FakeResponse({"file_key": "file_test"}),
                FakeResponse({"message_id": "om_sent", "chat_id": "oc_owner"}),
            ]
        )
        transport = self.transport(session)
        path = transport.outgoing / "hello.txt"
        path.write_text("hello")
        result = transport.send_file(path)
        self.assertEqual(result["status"], "sent")
        self.assertEqual(session.calls[1][2]["json"]["receive_id"], "ou_new_owner")
        self.assertEqual(session.calls[1][2]["params"]["receive_id_type"], "open_id")
        self.assertFalse(session.calls[0][2]["allow_redirects"])

    def test_group_other_owner_or_app_blocked(self):
        for overrides in [
            {"chat_type": "group"},
            {"sender_open_id": "ou_other"},
            {"app_id": "cli_old_bot"},
        ]:
            transport = self.transport(**overrides)
            with self.assertRaises(BridgeError):
                transport.receive_event(self.event())
            self.assertEqual(transport.session.calls, [])

    def test_filename_traversal_sanitized_download_idempotent(self):
        response = FakeResponse(payload=b"hello")
        session = FakeSession([response])
        transport = self.transport(session)
        result = transport.receive_event(self.event("../../master.key"))
        path = Path(result["path"])
        self.assertEqual(path.parent, transport.incoming)
        self.assertEqual(path.read_bytes(), b"hello")
        self.assertTrue(response.closed)
        self.assertEqual(
            transport.receive_event(self.event("../../master.key"))["status"], "already_present"
        )
        self.assertEqual(len(session.calls), 1)

    def test_failed_download_removes_partial_file(self):
        session = FakeSession([FakeResponse(status=403)])
        transport = self.transport(session)
        with self.assertRaises(BridgeError):
            transport.receive_event(self.event())
        self.assertEqual(list(transport.incoming.iterdir()), [])

    def test_safe_filename(self):
        self.assertEqual(safe_filename("..\\foo\n.txt"), "foo_.txt")

    def test_image_uses_image_resource_type(self):
        session = FakeSession([FakeResponse(payload=b"image-test")])
        transport = self.transport(session, message_type="image")
        event = {"event": {"message": {"content": json.dumps({"image_key": "img_test"})}}}
        result = transport.receive_event(event)
        self.assertEqual(result["status"], "downloaded")
        self.assertEqual(session.calls[0][2]["params"]["type"], "image")


if __name__ == "__main__":
    unittest.main()
