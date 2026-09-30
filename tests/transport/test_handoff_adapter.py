"""Additional offline checks for parameterization and the host callback seam."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from feishu_auth_kit.transport.cloud_bridge import BridgeError, EncryptedStore
from feishu_auth_kit.transport.group_policy import configure_authorized_group, group_policy
from feishu_auth_kit.transport.host_callback import consume_once


class HandoffAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = EncryptedStore(Path(self.temp.name))
        self.store.initialize()
        self.bot = {
            "app_id": "cli_SYNTHETICINVALIDAPP2",
            "brand": "feishu",
            "owner_open_id": "ou_SYNTHETICINVALIDOWNER",
            "owner_source": "registration",
        }
        self.store.write("bot", self.bot)
        self.store.write(
            "binding",
            {
                "app_id": self.bot["app_id"],
                "name_matches": True,
                "owner_matches": True,
                "overview_delivery": {"chat_id": "oc_SYNTHETICINVALIDPRIVATE"},
            },
        )
        self.request = Mock(
            return_value=NS(
                status_code=200,
                json=lambda: {
                    "code": 0,
                    "bot": {"app_name": "example-bot", "open_id": "ou_SYNTHETICINVALIDBOT"},
                },
            )
        )
        self.transport = NS(
            app_id=self.bot["app_id"],
            base="https://official.invalid",
            _headers=lambda: {},
            session=NS(request=self.request),
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_configured_group_and_app_are_parameters(self):
        result = configure_authorized_group(
            self.store,
            self.transport,
            chat_id="oc_SYNTHETICINVALIDGROUP2",
            expected_app_name="example-bot",
        )
        self.assertEqual(result["chat_id"], "oc_SYNTHETICINVALIDGROUP2")
        self.assertIsNotNone(group_policy(self.bot, self.store.read("binding")))
        self.assertEqual(self.request.call_args.args[0], "GET")

    def test_invalid_group_rejected_before_network(self):
        for value in (None, "", "unscoped", "oc_bad/path"):
            with self.assertRaises(BridgeError):
                configure_authorized_group(self.store, self.transport, chat_id=value)
        self.request.assert_not_called()

    def test_name_mismatch_does_not_enable_group(self):
        with self.assertRaises(BridgeError):
            configure_authorized_group(
                self.store,
                self.transport,
                chat_id="oc_SYNTHETICINVALIDGROUP",
                expected_app_name="another-name",
            )
        self.assertNotIn("owner_mention_group", self.store.read("binding"))

    def claim_message(self, message):
        binding = self.store.read("binding")
        binding["host_inbox"] = {
            "om_SYNTHETIC": {"status": "pending", "attempts": 0, "message": message}
        }
        self.store.write("binding", binding)
        return [message]

    def test_callback_receives_claimed_message_and_checkpoint(self):
        callback = Mock()
        message = {"text": "SYNTHETIC_MESSAGE_ONLY", "source": "private"}
        with (
            patch(
                "feishu_auth_kit.transport.host_callback.local_request",
                return_value={"status": "events", "events": [{"seq": 1}], "last_seq": 1},
            ),
            patch(
                "feishu_auth_kit.transport.host_callback.read_queued",
                side_effect=lambda *_: self.claim_message(message),
            ) as claim,
        ):
            result = consume_once(self.store, after=0, on_message=callback, timeout=0)
        claim.assert_called_once()
        callback.assert_called_once_with(message)
        self.assertEqual(result["last_seq"], 1)

    def test_empty_batch_does_not_claim_or_call(self):
        callback = Mock()
        with (
            patch(
                "feishu_auth_kit.transport.host_callback.local_request",
                return_value={"status": "timeout", "events": [], "last_seq": 7},
            ),
            patch("feishu_auth_kit.transport.host_callback.read_queued") as claim,
        ):
            result = consume_once(self.store, after=7, on_message=callback, timeout=0)
        claim.assert_not_called()
        callback.assert_not_called()
        self.assertEqual(result["last_seq"], 7)

    def test_callback_failure_propagates_without_auto_retry(self):
        callback = Mock(side_effect=RuntimeError("SYNTHETIC_FAILURE"))
        with (
            patch(
                "feishu_auth_kit.transport.host_callback.local_request",
                return_value={"status": "events", "events": [{"seq": 1}], "last_seq": 1},
            ),
            patch(
                "feishu_auth_kit.transport.host_callback.read_queued",
                side_effect=lambda *_: self.claim_message({"source": "private"}),
            ),
        ):
            with self.assertRaises(RuntimeError):
                consume_once(self.store, after=0, on_message=callback, timeout=0)
        callback.assert_called_once()


if __name__ == "__main__":
    unittest.main()
