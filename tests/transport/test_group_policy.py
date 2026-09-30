import copy
import fcntl
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from feishu_auth_kit.transport.chat_io import send_once
from feishu_auth_kit.transport.cloud_bridge import (
    BridgeError,
    EncryptedStore,
    OwnerOnlyTransport,
    _atomic_private,
)
from feishu_auth_kit.transport.group_policy import (
    configure_authorized_group,
    group_policy,
    group_receipt,
)
from feishu_auth_kit.transport.outbound_io import folders, submit
from feishu_auth_kit.transport.realtime_io import EventQueue, normalize_owner_event, read_queued

AUTHORIZED_APP_ID = "cli_SYNTHETICINVALIDAPP"
AUTHORIZED_GROUP_ID = "oc_SYNTHETICINVALIDGROUP"


class GroupPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = EncryptedStore(Path(self.temp.name))
        self.store.initialize()
        self.bot = {
            "app_id": AUTHORIZED_APP_ID,
            "app_secret": "TEST_ONLY",
            "brand": "feishu",
            "owner_source": "registration",
            "owner_open_id": "ou_owner",
        }
        self.policy = {
            "version": 1,
            "enabled": True,
            "app_id": AUTHORIZED_APP_ID,
            "owner_open_id": "ou_owner",
            "chat_id": AUTHORIZED_GROUP_ID,
            "bot_open_id": "ou_verifiedbot",
            "bot_open_id_source": "authenticated_bot_v3_info",
        }
        self.binding = {
            "app_id": AUTHORIZED_APP_ID,
            "name_matches": True,
            "owner_matches": True,
            "overview_delivery": {"chat_id": "oc_private"},
            "receive_test": {"baseline_ids": []},
            "chat_seen_ids": ["om_private_read"],
            "owner_mention_group": self.policy,
        }
        self.store.write("bot", self.bot)
        self.store.write("binding", self.binding)
        self.queue = EventQueue(self.store, self.bot, "oc_private")
        self.event = {
            "header": {"app_id": AUTHORIZED_APP_ID, "event_id": "ev_group"},
            "event": {
                "sender": {"sender_type": "user", "sender_id": {"open_id": "ou_owner"}},
                "message": {
                    "chat_type": "group",
                    "chat_id": AUTHORIZED_GROUP_ID,
                    "message_id": "om_group",
                    "message_type": "text",
                    "create_time": "1000",
                    "content": '{"text":"@_user_1 hello"}',
                    "mentions": [
                        {
                            "key": "@_user_1",
                            "name": "example-bot",
                            "id": {"open_id": "ou_verifiedbot"},
                        }
                    ],
                },
            },
        }
        self.network = []

        def request(method, url, **kwargs):
            self.network.append((method, url, kwargs))
            return NS(
                status_code=200,
                json=lambda: {
                    "code": 0,
                    "data": {"message_id": "om_reply", "chat_id": self.response_chat},
                },
            )

        self.response_chat = AUTHORIZED_GROUP_ID
        self.transport = NS(
            base="https://official.invalid",
            owner="ou_owner",
            app_id=AUTHORIZED_APP_ID,
            _headers=lambda: {"Authorization": "Bearer TEST_ONLY"},
            _json=OwnerOnlyTransport._json,
            last_auth_cache="test",
            session=NS(request=request),
        )

    def tearDown(self):
        self.queue.db.close()
        self.temp.cleanup()

    def read_group(self):
        self.queue.receive(self.event, 1500)
        with patch(
            "feishu_auth_kit.transport.realtime_io.create_owner_transport", return_value=object()
        ):
            return read_queued(self.store, self.queue.wait(0, 0)["events"])

    def test_accept_exact_owner_bot_mention_and_record_source(self):
        result = self.queue.receive(self.event, 1500)
        self.assertEqual(result["source"], "group")
        self.assertEqual(result["chat_id"], AUTHORIZED_GROUP_ID)
        clean = json.loads(
            self.queue.cipher.decrypt(
                self.queue.db.execute("SELECT payload FROM events").fetchone()[0]
            )
        )
        self.assertNotIn("name", clean["event"]["message"]["mentions"][0])
        self.assertEqual(
            clean["event"]["message"]["mentions"][0]["id"]["open_id"], "ou_verifiedbot"
        )

    def test_wrong_group_owner_app_and_bot_sender_rejected(self):
        mutations = [
            (("header", "app_id"), "cli_other"),
            (("event", "message", "chat_id"), "oc_other"),
            (("event", "sender", "sender_id", "open_id"), "ou_other"),
            (("event", "sender", "sender_type"), "app"),
            (("event", "sender", "sender_type"), "bot"),
        ]
        for path, value in mutations:
            with self.subTest(path=path, value=value):
                event = copy.deepcopy(self.event)
                node = event
                for key in path[:-1]:
                    node = node[key]
                node[path[-1]] = value
                self.assertIsNone(self.queue.receive(event, 1500))
        self.assertEqual(self.queue.wait(0, 0)["last_seq"], 0)

    def test_missing_fake_other_bot_and_everyone_mentions_rejected(self):
        for mentions in (
            None,
            [],
            [{"name": "example-bot", "id": {"open_id": "ou_otherbot"}}],
            [{"id": {"open_id": "all"}}],
            [{"id": {"user_id": "ou_verifiedbot"}}],
            [{"id": "ou_verifiedbot"}],
        ):
            event = copy.deepcopy(self.event)
            event["event"]["message"]["mentions"] = mentions
            event["event"]["message"]["content"] = '{"text":"@example-bot @ou_verifiedbot"}'
            self.assertIsNone(self.queue.receive(event, 1500))

    def test_policy_absent_revoked_or_wrong_identity_fails_closed(self):
        for field, value in [
            ("enabled", False),
            ("app_id", "other"),
            ("chat_id", "oc_other"),
            ("owner_open_id", "ou_other"),
            ("bot_open_id_source", "guessed"),
            ("bot_open_id", "ou_owner"),
        ]:
            binding = copy.deepcopy(self.binding)
            binding["owner_mention_group"][field] = value
            self.store.write("binding", binding)
            self.assertIsNone(self.queue.receive(self.event, 1500))
        binding = copy.deepcopy(self.binding)
        binding.pop("owner_mention_group")
        self.store.write("binding", binding)
        self.assertIsNone(self.queue.receive(self.event, 1500))
        # Policy is read per event, with no listener restart needed after initial upgrade.
        self.store.write("binding", self.binding)
        self.assertIsNotNone(self.queue.receive(self.event, 1500))

    def test_duplicate_group_event_queued_and_claimed_once(self):
        self.assertIsNotNone(self.queue.receive(self.event, 1500))
        self.assertIsNone(self.queue.receive(self.event, 1600))
        self.assertEqual(self.queue.wait(0, 0)["last_seq"], 1)
        with patch(
            "feishu_auth_kit.transport.realtime_io.create_owner_transport", return_value=object()
        ):
            events = self.queue.wait(0, 0)["events"]
            self.assertEqual(len(read_queued(self.store, events)), 1)
            self.assertEqual(read_queued(self.store, events), [])

    def test_group_read_receipt_separate_from_private_watermark(self):
        messages = self.read_group()
        self.assertEqual(messages[0]["source"], "group")
        self.assertEqual(
            messages[0]["reply_context"],
            {
                "target_chat_id": AUTHORIZED_GROUP_ID,
                "reply_to": "om_group",
                "scope": "group_only",
                "allow_dm_context": False,
            },
        )
        binding = self.store.read("binding")
        self.assertEqual(binding["chat_seen_ids"], ["om_private_read"])
        self.assertEqual(binding["group_seen_ids"][AUTHORIZED_GROUP_ID], ["om_group"])
        self.assertEqual(
            binding["group_read_receipts"]["om_group"],
            group_receipt(self.bot, self.policy, "om_group"),
        )

    def test_private_messages_do_not_need_mention_or_group_policy(self):
        binding = copy.deepcopy(self.binding)
        binding.pop("owner_mention_group")
        self.store.write("binding", binding)
        event = copy.deepcopy(self.event)
        event["event"]["message"].update(
            chat_type="p2p", chat_id="oc_private", message_id="om_private_new"
        )
        event["event"]["message"].pop("mentions")
        self.queue.receive(event, 1500)
        with patch(
            "feishu_auth_kit.transport.realtime_io.create_owner_transport", return_value=object()
        ):
            messages = read_queued(self.store, self.queue.wait(0, 0)["events"])
        self.assertEqual(messages[0]["source"], "private")
        self.assertNotIn("reply_context", messages[0])
        self.assertIn("om_private_new", self.store.read("binding")["chat_seen_ids"])

    def test_group_send_requires_read_claim_and_explicit_group_target(self):
        self.queue.receive(self.event, 1500)
        for reply_to, target in [
            ("om_group", AUTHORIZED_GROUP_ID),
            ("om_group", None),
            (None, AUTHORIZED_GROUP_ID),
        ]:
            with self.assertRaises(BridgeError):
                send_once(self.store, self.transport, "denied", "hello", reply_to, target)
        self.assertEqual(self.network, [])
        self.assertNotIn("denied", self.store.read("binding").get("chat_deliveries", {}))
        self.read_group()
        result = send_once(
            self.store,
            self.transport,
            "approved",
            "GROUP_REPLY_ONLY",
            "om_group",
            AUTHORIZED_GROUP_ID,
        )
        self.assertEqual(result["source"], "group")
        self.assertEqual(result["chat_id"], AUTHORIZED_GROUP_ID)
        self.assertEqual(len(self.network), 1)
        method, url, kwargs = self.network[0]
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/om_group/reply"))
        self.assertIsNone(kwargs["params"])
        self.assertNotIn("receive_id", kwargs["json"])
        self.assertEqual(json.loads(kwargs["json"]["content"])["text"], "GROUP_REPLY_ONLY")
        self.assertNotIn("om_private_read", json.dumps(kwargs))
        repeat = send_once(
            self.store,
            self.transport,
            "approved",
            "GROUP_REPLY_ONLY",
            "om_group",
            AUTHORIZED_GROUP_ID,
        )
        self.assertTrue(repeat["repeat_blocked"])
        self.assertEqual(len(self.network), 1)

    def test_group_and_private_targets_cannot_cross(self):
        self.read_group()
        for reply_to, target in [
            ("om_group", None),
            ("om_private_read", AUTHORIZED_GROUP_ID),
            ("om_group", "oc_other"),
            ("om_group", "oc_private"),
        ]:
            with self.subTest(reply_to=reply_to, target=target):
                with self.assertRaises(BridgeError):
                    send_once(self.store, self.transport, "denied-cross", "hello", reply_to, target)
        self.assertEqual(self.network, [])
        self.response_chat = "oc_private"
        self.assertEqual(
            send_once(self.store, self.transport, "private-ok", "DM", "om_private_read")["source"],
            "private",
        )
        self.assertTrue(self.network[0][1].endswith("/om_private_read/reply"))
        self.assertEqual(
            send_once(self.store, self.transport, "private-new", "DM")["source"], "private"
        )
        self.assertEqual(self.network[1][2]["json"]["receive_id"], "ou_owner")

    def test_revocation_blocks_send_of_previous_read_group(self):
        self.read_group()
        binding = self.store.read("binding")
        binding["owner_mention_group"]["enabled"] = False
        self.store.write("binding", binding)
        with self.assertRaises(BridgeError):
            send_once(
                self.store, self.transport, "revoked", "hello", "om_group", AUTHORIZED_GROUP_ID
            )
        self.assertEqual(self.network, [])

    def test_wrong_returned_chat_is_recorded_and_never_retried(self):
        self.read_group()
        self.response_chat = "oc_private"
        with self.assertRaises(BridgeError):
            send_once(
                self.store,
                self.transport,
                "unexpected-target",
                "hello",
                "om_group",
                AUTHORIZED_GROUP_ID,
            )
        self.assertEqual(
            self.store.read("binding")["chat_deliveries"]["unexpected-target"]["status"],
            "target_mismatch",
        )
        result = send_once(
            self.store,
            self.transport,
            "unexpected-target",
            "hello",
            "om_group",
            AUTHORIZED_GROUP_ID,
        )
        self.assertTrue(result["repeat_blocked"])
        self.assertEqual(len(self.network), 1)

    def test_operation_fingerprint_includes_exact_reply_and_destination(self):
        self.read_group()
        send_once(self.store, self.transport, "same-op", "hello", "om_group", AUTHORIZED_GROUP_ID)
        for text, reply, target in [
            ("changed", "om_group", AUTHORIZED_GROUP_ID),
            ("hello", "om_private_read", None),
            ("hello", "om_group", None),
        ]:
            with self.assertRaises(BridgeError):
                send_once(self.store, self.transport, "same-op", text, reply, target)
        self.assertEqual(len(self.network), 1)

    def test_old_sender_cannot_queue_new_group_protocol(self):
        self.read_group()
        root, requests, results = folders(self.store)
        fd = os.open(root / "listener.lock", os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            _atomic_private(root / "status.json", b'{"status":"ready"}')
            with self.assertRaises(BridgeError):
                submit(self.store, "group-op", "hello", "om_group", 0, AUTHORIZED_GROUP_ID)
            self.assertEqual(list(requests.glob("*.enc")), [])
            _atomic_private(
                root / "status.json",
                b'{"status":"ready","protocol_version":2,"group_replies":true}',
            )
            result = submit(self.store, "group-op", "hello", "om_group", 0, AUTHORIZED_GROUP_ID)
            self.assertEqual(result["status"], "pending")
            request = json.loads(
                self.store._cipher().decrypt(next(requests.glob("*.enc")).read_bytes())
            )
            self.assertEqual(request["target_chat"], AUTHORIZED_GROUP_ID)
            self.assertEqual(request["reply_to"], "om_group")
        finally:
            os.close(fd)

    def test_old_listener_status_does_not_claim_group_support(self):
        _atomic_private(
            self.queue.folder / "status.json", b'{"status":"connected","state_since_ms":1}'
        )
        status = self.queue.status()
        self.assertEqual(status["protocol_version"], 1)
        self.assertFalse(status["group_policy_enabled"])
        self.queue.set_state("connected")
        self.assertTrue(self.queue.status()["group_policy_enabled"])

    def test_configure_reads_real_bot_identity_without_sending_or_scope_changes(self):
        calls = []

        def request(method, url, **kwargs):
            calls.append((method, url))
            return NS(
                status_code=200,
                json=lambda: {
                    "code": 0,
                    "bot": {"app_name": "example-bot", "open_id": "ou_SYNTHETICINVALIDBOT"},
                },
            )

        self.transport.session.request = request
        result = configure_authorized_group(
            self.store, self.transport, chat_id=AUTHORIZED_GROUP_ID, expected_app_name="example-bot"
        )
        self.assertEqual(calls, [("GET", "https://official.invalid/open-apis/bot/v3/info/")])
        self.assertEqual(result["bot_open_id"], "ou_SYNTHETICINVALIDBOT")
        self.assertFalse(result["automatic_reply"])
        self.assertEqual(
            group_policy(self.bot, self.store.read("binding"))["bot_open_id"], result["bot_open_id"]
        )

    def test_malformed_events_do_not_crash_listener(self):
        for payload in (
            None,
            [],
            {"header": "bad"},
            {"event": "bad"},
            {"event": {"sender": "bad"}},
            {"event": {"message": []}},
            {"event": {"sender": {"sender_id": "bad"}}},
        ):
            self.assertIsNone(
                normalize_owner_event(payload, self.bot, "oc_private", policy=self.policy)
            )


if __name__ == "__main__":
    unittest.main()
