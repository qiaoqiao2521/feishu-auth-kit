import json
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from feishu_auth_kit.transport import (
    BridgeError,
    EncryptedStore,
    TransportBinding,
    TransportSender,
    bind_verified_profile,
    deliver_pending,
    inbox_status,
    recover_delivery,
)
from feishu_auth_kit.transport.realtime_io import EventQueue, read_queued


@pytest.fixture
def configured(tmp_path):
    store = EncryptedStore(tmp_path)
    store.initialize()
    bind_verified_profile(
        store, TransportBinding("cli_SYNTHETICAPP", "ou_SYNTHETICOWNER", "oc_SYNTHETICPRIVATE")
    )
    provider = NS(
        app_id="cli_SYNTHETICAPP",
        get_tenant_access_token=Mock(return_value=NS(token="SYNTHETIC_TOKEN_ONLY", expire=7200)),
    )
    session = NS(
        request=Mock(
            return_value=NS(
                status_code=200,
                json=lambda: {
                    "code": 0,
                    "data": {"message_id": "om_SYNTHETICSENT", "chat_id": "oc_SYNTHETICPRIVATE"},
                },
            )
        )
    )
    sender = TransportSender(store, auth_provider=provider, session=session)
    return store, provider, session, sender


def test_profile_reused_without_credentials(configured):
    store, provider, session, sender = configured
    assert "app_secret" not in store.read("bot")
    assert sender.send("op_SYNTHETIC", "SYNTHETIC_TEXT")["status"] == "sent"
    assert provider.get_tenant_access_token.call_count == 1
    assert not store.exists("tenant_tokens")  # Provider owns its auth storage.
    assert session.request.call_args.kwargs["timeout"] == 20


def test_uuid_and_operation_durable_before_auth_or_send(configured):
    store, provider, session, sender = configured

    def auth(**kwargs):
        record = store.read("binding")["chat_deliveries"]["op_SYNTHETIC"]
        assert record["status"] == "sending" and record["uuid"]
        return NS(token="SYNTHETIC_TOKEN_ONLY", expire=7200)

    provider.get_tenant_access_token.side_effect = auth
    sender.send("op_SYNTHETIC", "SYNTHETIC_TEXT")
    assert session.request.call_args.kwargs["json"]["uuid"] == sender.status("op_SYNTHETIC")["uuid"]


@pytest.mark.parametrize("failure", [TimeoutError, ConnectionError])
def test_uncertain_post_not_replayed(configured, failure):
    store, _, session, sender = configured
    session.request.side_effect = failure("SYNTHETIC_ERROR")
    assert sender.send("op_SYNTHETIC", "SYNTHETIC_TEXT")["status"] == "unknown"
    assert sender.status("op_SYNTHETIC")["status"] == "unknown"
    assert sender.send("op_SYNTHETIC", "SYNTHETIC_TEXT")["repeat_blocked"]
    assert session.request.call_count == 1


def test_auth_failure_before_dispatch_is_failed(configured):
    _, provider, session, sender = configured
    provider.get_tenant_access_token.side_effect = TimeoutError()
    assert sender.send("op_SYNTHETIC", "SYNTHETIC_TEXT")["status"] == "failed"
    session.request.assert_not_called()
    assert sender.send("op_SYNTHETIC", "SYNTHETIC_TEXT")["repeat_blocked"]


def test_crash_record_unknown_and_receipt_reconciliation(configured):
    store, _, session, sender = configured
    session.request.side_effect = KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        sender.send("op_SYNTHETIC", "SYNTHETIC_TEXT")
    assert sender.status("op_SYNTHETIC")["status"] == "unknown"
    assert sender.send("op_SYNTHETIC", "SYNTHETIC_TEXT")["status"] == "unknown"
    with pytest.raises(BridgeError):
        sender.reconcile("op_SYNTHETIC", message_id="om_SYNTHETIC", chat_id="oc_WRONG")
    assert (
        sender.reconcile("op_SYNTHETIC", message_id="om_SYNTHETIC", chat_id="oc_SYNTHETICPRIVATE")[
            "status"
        ]
        == "sent"
    )
    assert session.request.call_count == 1


def test_changed_operation_payload_blocked(configured):
    _, _, session, sender = configured
    sender.send("op_SYNTHETIC", "SYNTHETIC_TEXT")
    with pytest.raises(BridgeError):
        sender.send("op_SYNTHETIC", "SYNTHETIC_CHANGED")
    assert session.request.call_count == 1


@pytest.mark.parametrize("payload", [{}, {"code": 0, "data": {}}, {"code": 123}])
def test_malformed_or_uncertain_response(configured, payload):
    _, _, session, sender = configured
    session.request.return_value = NS(status_code=200, json=lambda: payload)
    assert sender.send("op_SYNTHETIC", "SYNTHETIC_TEXT")["status"] == "unknown"
    assert sender.send("op_SYNTHETIC", "SYNTHETIC_TEXT")["repeat_blocked"]


def test_no_rebind_or_unsafe_timeout(configured):
    store, provider, session, _ = configured
    with pytest.raises(BridgeError):
        bind_verified_profile(store, TransportBinding("cli_OTHER", "ou_OTHER", "oc_OTHER"))
    with pytest.raises(ValueError):
        TransportSender(store, auth_provider=provider, session=session, timeout=30)


def test_durable_intake_and_explicit_callback_recovery(configured, monkeypatch):
    store, _, _, _ = configured
    bot = store.read("bot")
    queue = EventQueue(store, bot, "oc_SYNTHETICPRIVATE")
    event = {
        "header": {"app_id": bot["app_id"], "event_id": "ev_SYNTHETIC"},
        "event": {
            "sender": {"sender_type": "user", "sender_id": {"open_id": bot["owner_open_id"]}},
            "message": {
                "chat_type": "p2p",
                "chat_id": "oc_SYNTHETICPRIVATE",
                "message_id": "om_SYNTHETICINBOUND",
                "create_time": "1000",
                "message_type": "text",
                "content": json.dumps({"text": "SYNTHETIC_PRIVATE_ONLY"}),
            },
        },
    }
    queue.receive(event, 1500)
    monkeypatch.setattr(
        "feishu_auth_kit.transport.realtime_io.create_owner_transport", lambda _: object()
    )
    events = queue.wait(0, 0)["events"]
    read_queued(store, events)
    assert read_queued(store, events) == []
    assert inbox_status(store)["om_SYNTHETICINBOUND"]["status"] == "pending"
    callback = Mock(side_effect=RuntimeError("SYNTHETIC_FAILURE"))
    with pytest.raises(RuntimeError):
        deliver_pending(store, callback)
    assert inbox_status(store)["om_SYNTHETICINBOUND"]["status"] == "failed"
    assert deliver_pending(store, callback) == 0  # No automatic replay.
    recover_delivery(store, "om_SYNTHETICINBOUND")
    accepted = Mock()
    assert deliver_pending(store, accepted) == 1
    assert accepted.call_args.args[0]["message_id"] == "om_SYNTHETICINBOUND"
    assert inbox_status(store)["om_SYNTHETICINBOUND"] == {"status": "completed", "attempts": 2}
    queue.db.close()


def test_interrupted_callback_recovery(configured):
    store, _, _, _ = configured
    binding = store.read("binding")
    binding["host_inbox"] = {
        "om_SYNTHETIC": {
            "status": "processing",
            "attempts": 1,
            "message": {"message_id": "om_SYNTHETIC"},
        }
    }
    store.write("binding", binding)
    callback = Mock()
    assert deliver_pending(store, callback) == 0
    recover_delivery(store, "om_SYNTHETIC")
    assert deliver_pending(store, callback) == 1


def test_thread_reply_and_fingerprint(configured):
    store, _, session, sender = configured
    binding = store.read("binding")
    binding["chat_seen_ids"] = ["om_SYNTHETICREAD"]
    store.write("binding", binding)
    result = sender.send(
        "op_THREAD", "SYNTHETIC_REPLY", reply_to="om_SYNTHETICREAD", reply_in_thread=True
    )
    assert result["status"] == "sent"
    assert session.request.call_args.kwargs["json"]["reply_in_thread"] is True
    with pytest.raises(BridgeError):
        sender.send(
            "op_THREAD", "SYNTHETIC_REPLY", reply_to="om_SYNTHETICREAD", reply_in_thread=False
        )
    assert session.request.call_count == 1


def test_raw_thread_routing_preserved(configured):
    from feishu_auth_kit.transport.realtime_io import normalize_owner_event

    store, _, _, _ = configured
    bot = store.read("bot")
    event = {
        "header": {"app_id": bot["app_id"], "event_id": "ev_SYNTHETIC"},
        "event": {
            "sender": {"sender_type": "user", "sender_id": {"open_id": bot["owner_open_id"]}},
            "message": {
                "chat_type": "p2p",
                "chat_id": "oc_SYNTHETICPRIVATE",
                "message_id": "om_SYNTHETIC",
                "create_time": "1000",
                "message_type": "text",
                "content": '{"text":"SYNTHETIC"}',
                "thread_id": "omt_SYNTHETIC",
                "root_id": "om_SYNTHETICROOT",
                "parent_id": "om_SYNTHETICPARENT",
            },
        },
    }
    _, clean = normalize_owner_event(event, bot, "oc_SYNTHETICPRIVATE")
    for key in ("thread_id", "root_id", "parent_id"):
        assert clean["event"]["message"][key] == event["event"]["message"][key]


def test_concurrent_operation_single_dispatch(configured):
    import concurrent.futures
    import time

    store, provider, session, sender = configured
    second = TransportSender(store, auth_provider=provider, session=session)
    response = session.request.return_value

    def delayed(*args, **kwargs):
        time.sleep(0.05)
        return response

    session.request.side_effect = delayed
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda s: s.send("op_CONCURRENT", "SYNTHETIC"), [sender, second]))
    assert session.request.call_count == 1
    assert any(result["status"] == "sent" for result in results)
    assert sender.status("op_CONCURRENT")["status"] == "sent"


@pytest.mark.parametrize("mode", ["provider", "executor"])
def test_cli_thread_profile_and_secret_redaction(configured, tmp_path, monkeypatch, capsys, mode):
    import sys

    from feishu_auth_kit.transport.cli import main

    store, provider, _, _ = configured
    binding = store.read("binding")
    binding["chat_seen_ids"] = ["om_SYNTHETICREAD"]
    store.write("binding", binding)
    monkeypatch.setitem(
        sys.modules,
        "synthetic_host",
        NS(
            provider=lambda: provider,
            store=lambda: store,
            executor=lambda: NS(app_id=provider.app_id, request=request),
        ),
    )
    request = Mock(
        return_value=NS(
            status_code=200,
            json=lambda: {
                "code": 0,
                "data": {"message_id": "om_SYNTHETICSENT", "chat_id": "oc_SYNTHETICPRIVATE"},
            },
        )
    )
    session = NS(request=request, close=Mock())
    monkeypatch.setattr("requests.Session", lambda: session)
    text = tmp_path / "reply.txt"
    text.write_text("SYNTHETIC_PRIVATE_TEXT")
    assert (
        main(
            [
                "send",
                "--store-factory",
                "synthetic_host:store",
                "--provider",
                "synthetic_host:provider",
                "--operation",
                "op_CLI",
                "--text-file",
                str(text),
                "--reply-to",
                "om_SYNTHETICREAD",
                "--reply-in-thread",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert json.loads(output)["status"] == "sent"
    assert "SYNTHETIC_PRIVATE_TEXT" not in output and "SYNTHETIC_TOKEN_ONLY" not in output
    assert request.call_args.kwargs["json"]["reply_in_thread"] is True
    session.close.assert_called_once()


def test_normal_cli_executor_requires_no_token_or_secret(configured):
    store, _, _, _ = configured
    requests = []

    class Backend:
        app_id = "cli_SYNTHETICAPP"

        def request(self, method, url, **kwargs):
            # The host implementation invokes its normal official CLI, rather
            # than reading/decrypting profile config or extracting credentials.
            assert kwargs["headers"] == {}
            assert "op_EXECUTOR" in store.read("binding")["chat_deliveries"]
            assert (
                store.read("binding")["chat_deliveries"]["op_EXECUTOR"]["uuid"]
                == kwargs["json"]["uuid"]
            )
            requests.append((method, kwargs))
            return NS(
                status_code=200,
                json=lambda: {
                    "code": 0,
                    "data": {
                        "message_id": "om_SYNTHETICEXECUTOR",
                        "chat_id": "oc_SYNTHETICPRIVATE",
                    },
                },
            )

    sender = TransportSender(store, request_executor=Backend())
    assert sender.send("op_EXECUTOR", "SYNTHETIC")["status"] == "sent"
    assert sender.send("op_EXECUTOR", "SYNTHETIC")["repeat_blocked"]
    assert len(requests) == 1
    assert not store.exists("tenant_tokens") and "app_secret" not in store.read("bot")


def test_executor_timeout_unknown_without_cli_replay(configured):
    store, _, _, _ = configured
    backend = NS(app_id="cli_SYNTHETICAPP", request=Mock(side_effect=TimeoutError()))
    sender = TransportSender(store, request_executor=backend)
    assert sender.send("op_EXECUTOR", "SYNTHETIC")["status"] == "unknown"
    assert sender.send("op_EXECUTOR", "SYNTHETIC")["status"] == "unknown"
    assert backend.request.call_count == 1


def test_executor_thread_reply_is_preserved(configured):
    store, _, session, _ = configured
    binding = store.read("binding")
    binding["chat_seen_ids"] = ["om_SYNTHETICREAD"]
    store.write("binding", binding)
    backend = NS(app_id="cli_SYNTHETICAPP", request=session.request)
    sender = TransportSender(store, request_executor=backend)
    sender.send("op_EXECUTOR", "SYNTHETIC", reply_to="om_SYNTHETICREAD", reply_in_thread=True)
    assert backend.request.call_args.kwargs["json"]["reply_in_thread"] is True
    assert backend.request.call_args.kwargs["headers"] == {}
