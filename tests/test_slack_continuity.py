"""Offline crash/restart and native-context regression tests for Slack transport."""

import json
import sqlite3
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path

from feishu_auth_kit.slack_continuity import (
    ContinuityStore,
    LedgerSchemaError,
    SlackContinuityBridge,
)
from feishu_auth_kit.slack_transport import SlackBinding


class FakeNative:
    def __init__(self):
        self.calls = []
        self.issue = {"id": "coordinating-issue", "conversationSessionGeneration": 0}
        self.generation = 0
        self.sources = {}
        self.results = {}
        self.ensure_error = False
        self.submit_error = False
        self.hold_reset = False

    def ensure_conversation(self):
        self.calls.append(("ensure",))
        if self.ensure_error:
            raise TimeoutError("creation response lost")
        return self.issue

    def lookup_conversation(self):
        self.calls.append(("lookup",))
        return self.issue

    def submit(self, issue_id, text, request_id):
        self.calls.append(("submit", issue_id, text, request_id))
        if text.strip() == "/new":
            self.generation += 1
        source = {"id": "source-" + request_id, "conversationSessionGeneration": self.generation}
        self.sources[request_id] = source
        self.results[source["id"]] = (
            {"state": "pending" if self.hold_reset else "reset"}
            if text.strip() == "/new"
            else {"state": "pending"}
        )
        if self.submit_error:
            raise TimeoutError("submission committed, response lost")
        return source

    def reconcile(self, issue_id, text, request_id, source_id):
        self.calls.append(("reconcile", issue_id, text, request_id, source_id))
        return self.sources.get(request_id)

    def status(self, issue_id, source_id, generation):
        self.calls.append(("status", issue_id, source_id, generation))
        return self.results[source_id]

    def finish(self, request_id, reply_id=None):
        self.results[self.sources[request_id]["id"]] = {
            "state": "final",
            "runId": "run-1",
            "replyCommentId": reply_id or "reply-" + request_id,
            "text": "Native final answer",
        }

    def calls_of(self, kind):
        return [call for call in self.calls if call[0] == kind]


class FakeSender:
    def __init__(self):
        self.calls = []
        self.error = False
        self.receipt_override = None
        self.reconcile_calls = []
        self.reconcile_receipt = None
        self.reconcile_error = None

    def send(self, channel_id, text, request_id):
        self.calls.append((channel_id, text, request_id))
        if self.error:
            raise TimeoutError("Slack delivery outcome unknown")
        if self.receipt_override is not None:
            return self.receipt_override
        return {"ok": True, "channel": channel_id, "ts": "1234.000001"}

    def reconcile(self, channel_id, text, request_id, oldest):
        self.reconcile_calls.append((channel_id, text, request_id, oldest))
        if self.reconcile_error is not None:
            raise self.reconcile_error
        return self.reconcile_receipt


class SlackContinuityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "continuity.sqlite3")
        self.binding = SlackBinding(
            workspace_id="T_OWNER",
            app_id="A_APP",
            owner_user_id="U_OWNER",
            dm_channel_id="D_OWNER",
            server_url="https://native.example.invalid",
            company_id="company-1",
            agent_id="agent-1",
            board_user_id="board-owner",
        )
        self.message_timestamps = {}
        self.native = FakeNative()
        self.sender = FakeSender()
        self.store = ContinuityStore(self.path, self.binding)
        self.bridge = SlackContinuityBridge(self.store, self.native, self.sender)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def restart(self):
        self.store.close()
        self.store = ContinuityStore(self.path, self.binding)
        self.bridge = SlackContinuityBridge(self.store, self.native, self.sender)

    def receive(self, request_id, text="Hello", **overrides):
        timestamp = self.message_timestamps.setdefault(
            request_id, f"{len(self.message_timestamps) + 1}.000001"
        )
        event = {
            "client_request_id": request_id,
            "event_id": "event-" + request_id,
            "text": text,
            "workspace_id": self.binding.workspace_id,
            "app_id": self.binding.app_id,
            "user_id": self.binding.owner_user_id,
            "channel_id": self.binding.dm_channel_id,
            "message_ts": timestamp,
        }
        event.update(overrides)
        return self.store.receive(event)

    def row(self, request_id):
        return next(row for row in self.store.events() if row["request_id"] == request_id)

    def test_same_issue_and_native_generation_survive_messages_and_restart(self):
        self.receive("1", "Remember that my project is Atlas")
        self.bridge.tick()
        self.native.finish("1")
        self.bridge.tick()
        self.restart()
        self.receive("2", "What is my project's name?")
        self.bridge.tick()
        submits = self.native.calls_of("submit")
        self.assertEqual([call[1] for call in submits], ["coordinating-issue"] * 2)
        self.assertEqual(len(self.native.calls_of("ensure")), 1)
        self.assertEqual(self.row("1")["native_generation"], self.row("2")["native_generation"])
        self.assertEqual(self.store.meta()["issue_id"], "coordinating-issue")

    def test_receipt_retry_is_deduplicated_and_new_increments_only_once(self):
        self.receive("1", "/new")
        self.receive("1", "/new", event_id="retry-envelope")
        self.restart()
        self.receive("1", "/new", event_id="another-envelope")
        self.assertEqual(self.store.meta()["generation"], 1)
        self.assertEqual(len(self.store.events()), 1)
        self.bridge.tick()
        self.bridge.tick()
        self.assertEqual(len(self.native.calls_of("submit")), 1)
        self.assertEqual(self.row("1")["execution"], "reset")

    def test_receipt_identity_cannot_be_reused_with_changed_body(self):
        self.receive("1", "first body")
        with self.assertRaises(ValueError):
            self.receive("1", "changed body")
        self.assertEqual(json.loads(self.row("1")["payload"])["text"], "first body")

    def test_new_suppresses_late_old_results_without_changing_issue(self):
        self.receive("old")
        self.bridge.tick()
        self.native.finish("old")
        self.receive("reset", "/new")
        self.receive("new", "Start fresh")
        self.bridge.tick()
        self.assertEqual(self.row("old")["delivery"], "suppressed")
        self.assertEqual(self.row("reset")["execution"], "reset")
        self.assertEqual(self.row("new")["native_generation"], 1)
        self.assertEqual(self.sender.calls, [])
        self.native.finish("new")
        self.bridge.tick()
        self.assertEqual([call[2] for call in self.sender.calls], ["new"])
        self.assertEqual(
            {call[1] for call in self.native.calls_of("submit")}, {"coordinating-issue"}
        )

    def test_delayed_message_before_latest_reset_is_never_submitted(self):
        self.receive("reset", "/new", message_ts="20.000001")
        self.bridge.tick()
        self.restart()
        self.receive("late-old", "Earlier message", message_ts="10.000001")
        self.bridge.tick()
        self.assertEqual(self.row("late-old")["execution"], "superseded")
        self.assertEqual(self.row("late-old")["delivery"], "suppressed")
        self.assertEqual([call[3] for call in self.native.calls_of("submit")], ["reset"])
        self.assertEqual(self.store.meta()["generation"], 1)

    def test_delayed_old_reset_does_not_reset_newer_context(self):
        self.receive("reset", "/new", message_ts="20.000001")
        self.bridge.tick()
        self.receive("fresh", "New context", message_ts="21.000001")
        self.bridge.tick()
        self.restart()
        self.receive("old-reset", "/new", message_ts="5.000001")
        self.bridge.tick()
        self.assertEqual(self.store.meta()["generation"], 1)
        self.assertEqual(self.row("old-reset")["execution"], "superseded")
        self.assertEqual(self.row("old-reset")["delivery"], "suppressed")
        self.assertEqual([call[3] for call in self.native.calls_of("submit")], ["reset", "fresh"])
        self.native.finish("fresh")
        self.bridge.tick()
        self.assertEqual([call[2] for call in self.sender.calls], ["fresh"])

    def test_stale_reset_does_not_block_later_valid_message_after_restart(self):
        self.receive("reset", "/new", message_ts="20.000001")
        self.bridge.tick()
        self.receive("stale-reset", "/new", message_ts="5.000001")
        self.bridge.tick()
        self.restart()
        self.receive("valid", "Continue fresh context", message_ts="21.000001")
        self.bridge.tick()
        self.assertEqual(self.row("stale-reset")["execution"], "superseded")
        self.assertEqual(self.row("valid")["execution"], "pending")
        self.assertEqual([call[3] for call in self.native.calls_of("submit")], ["reset", "valid"])
        self.assertEqual(self.store.meta()["generation"], 1)

    def test_reset_timestamp_order_preserves_microsecond_precision(self):
        self.receive("reset", "/new", message_ts="1791612345.123456")
        self.bridge.tick()
        self.restart()
        self.receive("older", "One microsecond older", message_ts="1791612345.123455")
        self.receive("stale-reset", "/new", message_ts="1791612345.123454")
        self.receive("newer", "One microsecond newer", message_ts="1791612345.123457")
        self.bridge.tick()
        self.assertEqual(self.row("older")["execution"], "superseded")
        self.assertEqual(self.row("stale-reset")["execution"], "superseded")
        self.assertEqual(self.row("newer")["execution"], "pending")
        self.assertEqual([call[3] for call in self.native.calls_of("submit")], ["reset", "newer"])
        self.assertEqual(self.store.meta()["generation"], 1)

    def test_consecutive_new_commands_supersede_unsubmitted_earlier_reset(self):
        self.receive("reset-1", "/new")
        self.receive("reset-2", "/new")
        self.bridge.tick()
        self.assertEqual(self.store.meta()["generation"], 2)
        self.assertEqual(self.row("reset-1")["execution"], "superseded")
        self.assertEqual(self.row("reset-2")["execution"], "reset")
        self.assertEqual([call[3] for call in self.native.calls_of("submit")], ["reset-2"])

    def test_new_supersedes_unsubmitted_prior_messages(self):
        self.receive("old")
        self.receive("reset", "/new")
        self.bridge.tick()
        self.assertEqual(self.row("old")["execution"], "superseded")
        self.assertEqual([call[3] for call in self.native.calls_of("submit")], ["reset"])

    def test_explicit_task_reference_is_source_text_not_conversation_switch(self):
        body = "Please review task issue ENG-42 (native issue task-uuid), then report here."
        self.receive("1", body)
        self.bridge.tick()
        self.assertEqual(
            self.native.calls_of("submit"), [("submit", "coordinating-issue", body, "1")]
        )
        self.assertEqual(self.store.meta()["issue_id"], "coordinating-issue")
        self.assertEqual(json.loads(self.row("1")["payload"])["text"], body)

    def test_unknown_submit_after_restart_reconciles_never_posts_again(self):
        self.native.submit_error = True
        self.receive("1")
        self.bridge.tick()
        self.assertEqual(self.row("1")["execution"], "unknown")
        self.restart()
        self.native.finish("1")
        self.bridge.tick()
        self.bridge.tick()
        self.assertEqual(len(self.native.calls_of("submit")), 1)
        self.assertEqual(len(self.native.calls_of("reconcile")), 1)
        self.assertEqual(self.row("1")["delivery"], "delivered")

    def test_unresolved_submit_remains_unknown_without_repost(self):
        self.native.submit_error = True
        self.receive("1")
        self.bridge.tick()
        self.native.sources.clear()
        self.restart()
        self.bridge.tick()
        self.bridge.tick()
        self.assertEqual(len(self.native.calls_of("submit")), 1)
        self.assertEqual(len(self.native.calls_of("reconcile")), 2)
        self.assertEqual(self.row("1")["execution"], "unknown")

    def test_unknown_conversation_creation_only_uses_lookup_after_restart(self):
        self.native.ensure_error = True
        self.receive("1")
        self.bridge.tick()
        self.assertEqual(self.store.meta()["creation"], "unknown")
        self.restart()
        issue = self.native.issue
        self.native.issue = None
        self.bridge.tick()
        self.bridge.tick()
        self.assertEqual(len(self.native.calls_of("ensure")), 1)
        self.assertEqual(len(self.native.calls_of("lookup")), 2)
        self.assertEqual(self.native.calls_of("submit"), [])
        self.native.issue = issue
        self.bridge.tick()
        self.assertEqual(len(self.native.calls_of("ensure")), 1)
        self.assertEqual(self.store.meta()["creation"], "confirmed")

    def test_unknown_delivery_reconciles_exact_receipt_without_second_send(self):
        self.receive("1")
        self.bridge.tick()
        self.native.finish("1")
        self.sender.error = True
        self.bridge.tick()
        self.restart()
        self.sender.reconcile_receipt = {
            "ok": True,
            "channel": self.binding.dm_channel_id,
            "ts": "2000.000001",
        }
        self.bridge.tick()
        self.assertEqual(self.row("1")["delivery"], "delivered")
        self.assertEqual(self.row("1")["slack_ts"], "2000.000001")
        self.assertEqual(len(self.sender.calls), 1)
        self.assertEqual(
            self.sender.reconcile_calls[-1],
            (self.binding.dm_channel_id, "Native final answer", "1", "1.000001"),
        )

    def test_delivery_readback_without_receipt_or_scope_stays_unknown(self):
        self.receive("1")
        self.bridge.tick()
        self.native.finish("1")
        self.sender.error = True
        self.bridge.tick()
        self.restart()
        self.bridge.tick()
        self.assertEqual(self.row("1")["delivery"], "unknown")
        self.sender.reconcile_error = PermissionError("missing_scope")
        self.bridge.tick()
        self.assertEqual(self.row("1")["delivery"], "unknown")
        self.assertEqual(len(self.sender.calls), 1)
        self.assertEqual(len(self.sender.reconcile_calls), 2)

    def test_coalesced_readback_uses_original_delivery_request_identity(self):
        self.receive("1")
        self.receive("2")
        self.bridge.tick()
        self.native.finish("1", reply_id="shared-reply")
        self.native.finish("2", reply_id="shared-reply")
        self.sender.error = True
        self.bridge.tick()
        self.restart()
        self.bridge.tick()
        self.assertTrue(self.sender.reconcile_calls)
        self.assertEqual({call[2] for call in self.sender.reconcile_calls}, {"1"})
        self.assertEqual({call[3] for call in self.sender.reconcile_calls}, {"1.000001"})
        self.assertEqual(len(self.sender.calls), 1)
        self.sender.reconcile_receipt = {
            "ok": True,
            "channel": self.binding.dm_channel_id,
            "ts": "2000.000001",
        }
        self.bridge.tick()
        self.assertEqual([row["delivery"] for row in self.store.events()], ["delivered"] * 2)
        self.assertEqual(len(self.sender.calls), 1)

    def test_old_generation_unknown_delivery_can_be_confirmed_by_read_only_lookup(self):
        self.receive("old")
        self.bridge.tick()
        self.native.finish("old")
        self.sender.error = True
        self.bridge.tick()
        self.receive("reset", "/new")
        self.sender.reconcile_receipt = {
            "ok": True,
            "channel": self.binding.dm_channel_id,
            "ts": "2000.000001",
        }
        self.restart()
        self.bridge.tick()
        self.assertEqual(self.row("old")["delivery"], "delivered")
        self.assertEqual(self.row("reset")["execution"], "reset")
        self.assertEqual(len(self.sender.calls), 1)

    def test_unknown_delivery_never_resends_after_restart(self):
        self.receive("1")
        self.bridge.tick()
        self.native.finish("1")
        self.sender.error = True
        self.bridge.tick()
        self.assertEqual(self.row("1")["delivery"], "unknown")
        self.restart()
        self.sender.error = False
        self.bridge.tick()
        self.bridge.tick()
        self.assertEqual(len(self.sender.calls), 1)
        self.assertEqual(self.row("1")["delivery"], "unknown")

    def test_unverified_delivery_receipt_is_unknown_and_not_retried(self):
        self.receive("1")
        self.bridge.tick()
        self.native.finish("1")
        self.sender.receipt_override = {"ok": True, "channel": "D_WRONG", "ts": "1.0"}
        self.bridge.tick()
        self.restart()
        self.bridge.tick()
        self.assertEqual(self.row("1")["delivery"], "unknown")
        self.assertEqual(len(self.sender.calls), 1)

    def test_coalesced_sources_deliver_shared_reply_once(self):
        self.receive("1")
        self.receive("2")
        self.bridge.tick()
        self.native.finish("1", reply_id="shared-reply")
        self.native.finish("2", reply_id="shared-reply")
        self.bridge.tick()
        self.restart()
        self.bridge.tick()
        self.assertEqual(len(self.sender.calls), 1)
        self.assertEqual([row["delivery"] for row in self.store.events()], ["delivered"] * 2)
        self.assertEqual(self.row("1")["slack_ts"], self.row("2")["slack_ts"])

    def test_unknown_coalesced_delivery_is_not_sent_by_other_source(self):
        self.receive("1")
        self.receive("2")
        self.bridge.tick()
        self.native.finish("1", reply_id="shared-reply")
        self.native.finish("2", reply_id="shared-reply")
        self.sender.error = True
        self.bridge.tick()
        self.assertEqual(len(self.sender.calls), 1)
        self.assertEqual([row["delivery"] for row in self.store.events()], ["unknown"] * 2)

    def test_two_store_instances_deduplicate_simultaneous_reset_receipts(self):
        barrier = threading.Barrier(2)
        errors = []
        event = {
            "client_request_id": "reset",
            "event_id": "reset-event",
            "text": "/new",
            "workspace_id": self.binding.workspace_id,
            "app_id": self.binding.app_id,
            "user_id": self.binding.owner_user_id,
            "channel_id": self.binding.dm_channel_id,
            "message_ts": "1234.0",
        }

        def receive_from_second_connection():
            store = None
            try:
                store = ContinuityStore(self.path, self.binding)
                barrier.wait(timeout=5)
                store.receive(event)
            except BaseException as exc:
                errors.append(exc)
            finally:
                if store is not None:
                    store.close()

        threads = [threading.Thread(target=receive_from_second_connection) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.store.meta()["generation"], 1)
        self.assertEqual(len(self.store.events()), 1)

    def test_overlapping_workers_claim_native_submission_only_once(self):
        self.bridge.tick()  # Establish the conversation before overlapping source claims.
        self.receive("1")
        entered_submit = threading.Event()
        release_submit = threading.Event()
        errors = []
        original_submit = self.native.submit

        def blocked_submit(*args):
            entered_submit.set()
            if not release_submit.wait(timeout=5):
                raise TimeoutError("test did not release submit")
            return original_submit(*args)

        self.native.submit = blocked_submit

        def first_worker():
            store = None
            try:
                store = ContinuityStore(self.path, self.binding)
                SlackContinuityBridge(store, self.native, self.sender).tick()
            except BaseException as exc:
                errors.append(exc)
            finally:
                if store is not None:
                    store.close()

        thread = threading.Thread(target=first_worker)
        thread.start()
        try:
            self.assertTrue(entered_submit.wait(timeout=5))
            self.bridge.tick()  # Another process may reconcile, but must not submit again.
            self.assertEqual(self.row("1")["execution"], "unknown")
            self.assertEqual(len(self.native.calls_of("reconcile")), 1)
        finally:
            release_submit.set()
            thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(self.native.calls_of("submit")), 1)
        self.assertEqual(self.row("1")["execution"], "pending")

    def test_process_exit_after_delivery_intent_does_not_replay_send(self):
        self.receive("1")
        self.bridge.tick()
        self.native.finish("1")
        original_send = self.sender.send

        def crash_after_send(*args):
            original_send(*args)
            raise SystemExit("simulated crash before receipt persisted")

        self.sender.send = crash_after_send
        with self.assertRaises(SystemExit):
            self.bridge.tick()
        self.restart()
        self.sender.send = original_send
        self.bridge.tick()
        self.assertEqual(len(self.sender.calls), 1)
        self.assertEqual(self.row("1")["delivery"], "unknown")

    def test_legacy_schema_missing_reset_timestamp_preserves_existing_rows(self):
        self.receive("1", "Durable evidence must survive migration rejection")
        self.bridge.tick()
        self.store.db.execute("ALTER TABLE binding DROP COLUMN reset_ts")
        self.store.db.execute("PRAGMA user_version=0")
        before = list(self.store.db.iterdump())
        with self.assertRaisesRegex(LedgerSchemaError, "Preserve the database"):
            ContinuityStore(self.path, self.binding)
        with sqlite3.connect(self.path) as verification:
            self.assertEqual(list(verification.iterdump()), before)
            self.assertEqual(verification.execute("PRAGMA user_version").fetchone()[0], 0)
            columns = {row[1] for row in verification.execute("PRAGMA table_info(binding)")}
            self.assertNotIn("reset_ts", columns)
            self.assertEqual(verification.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)

    def test_future_schema_version_is_rejected_without_changing_database(self):
        self.receive("1", "Future version evidence")
        self.store.db.execute("PRAGMA user_version=99")
        before = list(self.store.db.iterdump())
        with self.assertRaises(LedgerSchemaError):
            ContinuityStore(self.path, self.binding)
        with sqlite3.connect(self.path) as verification:
            self.assertEqual(verification.execute("PRAGMA user_version").fetchone()[0], 99)
            self.assertEqual(list(verification.iterdump()), before)

    def test_current_schema_reloads_without_altering_receipts(self):
        self.receive("1", "Current schema durable receipt")
        before = self.store.events()
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], 1)
        self.restart()
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], 1)
        self.assertEqual(self.store.events(), before)

    def test_compatible_unversioned_schema_is_marked_current_without_data_loss(self):
        self.receive("1", "Compatible unversioned receipt")
        before = self.store.events()
        self.store.db.execute("PRAGMA user_version=0")
        self.restart()
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], 1)
        self.assertEqual(self.store.events(), before)

    def test_binding_mismatch_rejected(self):
        for field in (
            "workspace_id",
            "app_id",
            "owner_user_id",
            "dm_channel_id",
            "server_url",
            "company_id",
            "agent_id",
            "board_user_id",
        ):
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, "different Slack/native identity"):
                    ContinuityStore(self.path, replace(self.binding, **{field: "other"}))

    def test_pending_reset_blocks_subsequent_source(self):
        self.native.hold_reset = True
        self.receive("reset", "/new")
        self.receive("later")
        self.bridge.tick()
        self.assertEqual([call[3] for call in self.native.calls_of("submit")], ["reset"])
        self.native.results["source-reset"] = {"state": "reset"}
        self.bridge.tick()
        self.assertEqual([call[3] for call in self.native.calls_of("submit")], ["reset", "later"])

    def test_whitespace_reset_has_same_barrier_as_plain_reset(self):
        self.native.hold_reset = True
        self.receive("reset", " \n/new\t ")
        self.receive("later")
        self.bridge.tick()
        self.assertEqual(self.store.meta()["generation"], 1)
        self.assertEqual([call[3] for call in self.native.calls_of("submit")], ["reset"])


if __name__ == "__main__":
    unittest.main()
