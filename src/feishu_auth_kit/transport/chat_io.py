"""Temporary owner-only chat I/O. No model, daemon or automatic replies.

read-new reads once; wait-new waits only until one new batch; send transmits the
exact supplied text once under a caller-provided durable operation key.
"""

import argparse
import datetime
import fcntl
import hashlib
import json
import os
import time
import uuid
from contextlib import contextmanager
from urllib.parse import quote

from .binding import baseline_ids, binding_verified, private_chat_id
from .cloud_bridge import BridgeError, EncryptedStore, create_owner_transport
from .group_policy import resolve_reply_target


@contextmanager
def binding_lock(store):
    fd = os.open(store.private / "chat.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def read_new(store, transport):
    binding = store.read("binding")
    if not binding_verified(binding):
        raise BridgeError("App and owner have not been verified")
    chat = private_chat_id(binding)
    response = transport.session.request(
        "GET",
        transport.base + "/open-apis/im/v1/messages",
        headers=transport._headers(),
        params={
            "container_id_type": "chat",
            "container_id": chat,
            "page_size": 50,
            "sort_type": "ByCreateTimeDesc",
        },
        timeout=30,
        allow_redirects=False,
    )
    items = list(reversed(transport._json(response).get("items") or []))
    results = []
    with binding_lock(store):
        binding = store.read("binding")
        seen = set(binding.get("chat_seen_ids", [])) | set(baseline_ids(binding))
        for item in items:
            sender = item.get("sender") or {}
            if item.get("message_id") in seen:
                continue
            if (
                item.get("chat_id") != chat
                or sender.get("sender_type") != "user"
                or sender.get("id_type") != "open_id"
                or sender.get("id") != transport.owner
            ):
                continue
            content = json.loads(item.get("body", {}).get("content", "{}"))
            timestamp = item.get("create_time")
            when = (
                datetime.datetime.fromtimestamp(
                    int(timestamp) / 1000, datetime.timezone.utc
                ).isoformat()
                if timestamp
                else None
            )
            row = {
                "message_id": item["message_id"],
                "msg_type": item.get("msg_type"),
                "created_at_utc": when,
                "source": "private",
                "chat_id": chat,
            }
            if item.get("msg_type") == "text":
                row["text"] = content.get("text", "")
            elif item.get("msg_type") in {"image", "file"}:
                event = {
                    "header": {"app_id": transport.app_id},
                    "event": {
                        "sender": {"sender_id": {"open_id": transport.owner}},
                        "message": {
                            "chat_type": "p2p",
                            "chat_id": chat,
                            "message_id": item["message_id"],
                            "message_type": item["msg_type"],
                            "content": content,
                        },
                    },
                }
                received = transport.receive_event(event)
                row["local_attachment"] = received["path"]
            else:
                row["content"] = content
            results.append(row)
            seen.add(item["message_id"])
        binding["chat_seen_ids"] = list(seen)
        store.write("binding", binding)
    return results


def send_once(
    store, transport, operation, text, reply_to=None, target_chat=None, reply_in_thread=False
):
    started = time.monotonic()
    if (
        not isinstance(operation, str)
        or not operation
        or len(operation) > 512
        or not isinstance(text, str)
        or not text
        or len(text.encode()) > 100000
        or not isinstance(reply_in_thread, bool)
        or (reply_in_thread and not reply_to)
    ):
        raise BridgeError("An operation key and exact reply text are required")
    with binding_lock(store):
        binding = store.read("binding")
        if not binding_verified(binding):
            raise BridgeError("App and owner have not been verified")
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "text": text,
                    "reply_to": reply_to,
                    "target_chat": target_chat,
                    "reply_in_thread": reply_in_thread,
                },
                sort_keys=True,
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        deliveries = binding.setdefault("chat_deliveries", {})
        prior = deliveries.get(operation)
        if prior:
            if prior.get("request_fingerprint") not in {None, fingerprint}:
                raise BridgeError(
                    "Operation key was already used for a different reply or destination"
                )
            return {
                "status": prior["status"],
                "message_id": prior.get("message_id"),
                "repeat_blocked": True,
            }
        target = resolve_reply_target(store, binding, reply_to, target_chat)
        op = {
            "status": "sending",
            "uuid": str(uuid.uuid4()),
            "request_fingerprint": fingerprint,
            "source": target["source"],
            "expected_chat_id": target["chat_id"],
            "reply_to": reply_to,
        }
        deliveries[operation] = op
        store.write("binding", binding)
    payload = {
        "msg_type": "text",
        "content": json.dumps({"text": text}, ensure_ascii=False),
        "uuid": op["uuid"],
    }
    if reply_to:
        url = transport.base + "/open-apis/im/v1/messages/" + quote(reply_to, safe="") + "/reply"
        payload["reply_in_thread"] = reply_in_thread
        params = None
    else:
        url = transport.base + "/open-apis/im/v1/messages"
        params = {"receive_id_type": "open_id"}
        payload["receive_id"] = transport.owner
    prepared = time.monotonic()
    try:
        headers = transport._headers()
    except Exception as exc:
        with binding_lock(store):
            binding = store.read("binding")
            binding["chat_deliveries"][operation].update(
                status="failed", error_type=type(exc).__name__
            )
            store.write("binding", binding)
        return {"status": "failed", "automatic_resend": False, "reason": "auth_before_dispatch"}
    authenticated = time.monotonic()
    try:
        response = transport.session.request(
            "POST",
            url,
            headers=headers,
            params=params,
            json=payload,
            timeout=getattr(transport, "send_timeout", 20),
            allow_redirects=False,
        )
    except Exception as exc:
        with binding_lock(store):
            binding = store.read("binding")
            binding["chat_deliveries"][operation].update(
                status="unknown", error_type=type(exc).__name__
            )
            store.write("binding", binding)
        return {"status": "unknown", "automatic_resend": False}
    requested = time.monotonic()
    # Explicit authentication rejection is distinguishable from an uncertain
    # network outcome. Refresh the rejected cached token once for future work,
    # but never automatically replay this message POST.
    try:
        rejected_code = response.json().get("code")
    except (ValueError, AttributeError):
        rejected_code = None
    if rejected_code in {99991663, 99991664, 99991665, 99991666, 99991668}:
        refreshed = False
        try:
            transport._headers(
                rejected_token=headers.get("Authorization", "Bearer host-executor")[7:]
            )
            refreshed = True
        except Exception:
            pass
        with binding_lock(store):
            binding = store.read("binding")
            binding["chat_deliveries"][operation].update(
                status="auth_rejected", error_code=rejected_code, token_refreshed=refreshed
            )
            store.write("binding", binding)
        return {
            "status": "auth_rejected",
            "error_code": rejected_code,
            "token_refreshed": refreshed,
            "automatic_resend": False,
        }
    try:
        data = transport._json(response)
    except Exception:
        with binding_lock(store):
            binding = store.read("binding")
            binding["chat_deliveries"][operation].update(status="unknown")
            store.write("binding", binding)
        return {"status": "unknown", "automatic_resend": False}
    if data.get("chat_id") and data["chat_id"] != target["chat_id"]:
        with binding_lock(store):
            binding = store.read("binding")
            binding["chat_deliveries"][operation].update(
                status="target_mismatch", message_id=data.get("message_id"), chat_id=data["chat_id"]
            )
            store.write("binding", binding)
        raise BridgeError("Send returned an unexpected chat; automatic retry is blocked")
    if not data.get("message_id"):
        with binding_lock(store):
            binding = store.read("binding")
            binding["chat_deliveries"][operation].update(status="unknown")
            store.write("binding", binding)
        return {"status": "unknown", "automatic_resend": False}
    with binding_lock(store):
        binding = store.read("binding")
        binding["chat_deliveries"][operation].update(
            status="sent", message_id=data["message_id"], chat_id=data.get("chat_id")
        )
        store.write("binding", binding)
    finished = time.monotonic()
    return {
        "status": "sent",
        "message_id": data["message_id"],
        "chat_id": data.get("chat_id"),
        "source": target["source"],
        "expected_chat_id": target["chat_id"],
        "auth_cache": transport.last_auth_cache,
        "timings_ms": {
            "prepare": round((prepared - started) * 1000),
            "tenant_auth": round((authenticated - prepared) * 1000),
            "send_request": round((requested - authenticated) * 1000),
            "record_result": round((finished - requested) * 1000),
            "total": round((finished - started) * 1000),
        },
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["read-new", "wait-new", "send"])
    parser.add_argument("--operation")
    parser.add_argument("--text")
    parser.add_argument("--reply-to")
    parser.add_argument("--reply-in-thread", action="store_true")
    parser.add_argument(
        "--target-chat", help="Explicit allowlisted group; requires a read owner @message"
    )
    args = parser.parse_args()
    started = time.monotonic()
    try:
        store = EncryptedStore()
        transport = create_owner_transport(store)
        initialized = time.monotonic()
        if args.action == "send":
            result = send_once(
                store, transport, args.operation, args.text, args.reply_to, args.target_chat
            )
            result.setdefault("timings_ms", {})["transport_setup"] = round(
                (initialized - started) * 1000
            )
            result["timings_ms"]["command_total"] = round((time.monotonic() - started) * 1000)
        else:
            while True:
                messages = read_new(store, transport)
                if messages or args.action == "read-new":
                    result = {"status": "read", "messages": messages}
                    break
                time.sleep(5)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return 0
    except Exception as exc:
        print(json.dumps({"status": "error", "error_type": type(exc).__name__}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
