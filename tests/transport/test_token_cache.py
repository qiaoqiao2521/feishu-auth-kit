import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from feishu_auth_kit.transport.chat_io import send_once
from feishu_auth_kit.transport.cloud_bridge import EncryptedStore, OwnerOnlyTransport


class TokenCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = EncryptedStore(Path(self.temp.name))
        self.store.initialize()
        self.calls = 0
        self.store.write(
            "binding",
            {
                "name_matches": True,
                "owner_matches": True,
                "overview_delivery": {"chat_id": "oc_owner"},
            },
        )

    def tearDown(self):
        self.temp.cleanup()

    def transport(self, app_id="cli_test", session=None):
        def token(**kw):
            self.calls += 1
            return NS(token="TEST_TOKEN_" + str(self.calls), expire=7200)

        return OwnerOnlyTransport(
            auth_client=NS(app_id=app_id, get_tenant_access_token=token),
            session=session,
            app_id=app_id,
            owner_open_id="ou_owner",
            brand="feishu",
            parse_context=lambda x: x,
            root=Path(self.temp.name),
            token_store=self.store,
        )

    def test_new_transport_reuses_encrypted_cache(self):
        a = self.transport()
        h1 = a._headers()
        b = self.transport()
        h2 = b._headers()
        self.assertEqual(h1, h2)
        self.assertEqual(self.calls, 1)
        self.assertEqual(b.last_auth_cache, "encrypted_store")
        self.assertNotIn(b"TEST_TOKEN", (self.store.records / "tenant_tokens.enc").read_bytes())

    def test_app_isolation(self):
        a = self.transport("cli_first")
        b = self.transport("cli_second")
        self.assertNotEqual(a._headers(), b._headers())
        self.assertEqual(self.calls, 2)
        self.assertEqual(len(self.store.read("tenant_tokens")), 2)

    def test_expiry_refreshes_with_skew(self):
        self.transport()._headers()
        cache = self.store.read("tenant_tokens")
        for entry in cache.values():
            entry["expires_at"] = 0
        self.store.write("tenant_tokens", cache)
        self.transport()._headers()
        self.assertEqual(self.calls, 2)

    def test_clock_rollback_invalidates_cache(self):
        self.transport()._headers()
        with patch("feishu_auth_kit.transport.cloud_bridge.time.time", return_value=1):
            self.transport()._headers()
        self.assertEqual(self.calls, 2)

    def test_rejected_token_refreshes_once_and_uses_other_rotation(self):
        a = self.transport()
        old = a._headers()["Authorization"][7:]
        new = self.transport()._headers(rejected_token=old)
        same = self.transport()._headers(rejected_token=old)
        self.assertEqual(new, same)
        self.assertEqual(self.calls, 2)

    def test_auth_rejection_refreshes_without_resending(self):
        class Session:
            calls = 0

            def request(self, *args, **kwargs):
                self.calls += 1
                return NS(status_code=401, json=lambda: {"code": 99991663})

        session = Session()
        transport = self.transport(session=session)
        result = send_once(self.store, transport, "test-auth-rejection", "test")
        self.assertEqual(result["status"], "auth_rejected")
        self.assertFalse(result["automatic_resend"])
        self.assertEqual(session.calls, 1)
        self.assertEqual(self.calls, 2)
        duplicate = send_once(self.store, transport, "test-auth-rejection", "test")
        self.assertTrue(duplicate["repeat_blocked"])
        self.assertEqual(session.calls, 1)

    def test_success_includes_timings_and_cache_source(self):
        session = NS(
            request=lambda *a, **k: NS(
                status_code=200,
                json=lambda: {"code": 0, "data": {"message_id": "om_test", "chat_id": "oc_owner"}},
            )
        )
        result = send_once(self.store, self.transport(session=session), "test-ok", "test")
        self.assertEqual(result["status"], "sent")
        self.assertEqual(result["auth_cache"], "refreshed")
        self.assertIn("tenant_auth", result["timings_ms"])


if __name__ == "__main__":
    unittest.main()
