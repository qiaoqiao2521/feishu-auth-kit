"""Persistent owner-only HTTPS sender, invoked only with explicit parent replies.

No automatic replies, external listener, models, or arbitrary request endpoints.
The command queue is encrypted. Existing send_once owns durable operation dedup
and refuses uncertain-send retries. A crash can leave an operation unresolved;
the worker never blindly repeats it.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import signal
import time

from .chat_io import binding_lock, send_once
from .cloud_bridge import (
    BridgeError,
    EncryptedStore,
    _atomic_private,
    _private_dir,
    _read_private,
    create_owner_transport,
)
from .group_policy import resolve_reply_target
from .realtime_io import FileWake, listener_alive, verified_identity


def folders(store):
    root = store.private / "outbound"
    for path in (root, root / "requests", root / "results"):
        _private_dir(path)
    return root, root / "requests", root / "results"


def operation_key(operation):
    if not isinstance(operation, str) or not operation or len(operation) > 512:
        raise BridgeError("A bounded operation key is required")
    return hashlib.sha256(operation.encode()).hexdigest()


def safe_error(exc):
    return {
        "status": "unknown",
        "error_type": type(exc).__name__,
        "automatic_resend": False,
        "inspect_operation_before_retry": True,
    }


def submit(
    store, operation, text, reply_to=None, timeout=20, target_chat=None, reply_in_thread=False
):
    if not isinstance(text, str) or not text or len(text.encode()) > 100000:
        raise BridgeError("Exact bounded reply text is required")
    verified_identity(store)
    root, requests, results = folders(store)
    if not listener_alive(root):
        raise BridgeError("Persistent sender is not running")
    key = operation_key(operation)
    with binding_lock(store):
        resolve_reply_target(store, store.read("binding"), reply_to, target_chat)
    payload = {"operation": operation, "text": text, "reply_to": reply_to}
    if reply_in_thread:
        if not reply_to:
            raise BridgeError("Thread replies require a read reply target")
        payload["reply_in_thread"] = True
    if target_chat is not None:
        # Fail before queueing if an old sender is still serving the DM protocol.
        status = (
            json.loads(_read_private(root / "status.json"))
            if (root / "status.json").exists()
            else {}
        )
        if status.get("protocol_version") != 2 or not status.get("group_replies"):
            raise BridgeError("Persistent sender must be upgraded before group replies")
        payload["target_chat"] = target_chat
    request_path = requests / (key + ".enc")
    result_path = results / (key + ".json")
    fd = os.open(root / "submit.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    watcher = FileWake(results)
    started = time.monotonic()
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        existed = request_path.exists()
        if existed:
            previous = json.loads(store._cipher().decrypt(_read_private(request_path)))
            if previous != payload:
                raise BridgeError("Operation key was already used with different content")
        else:
            _atomic_private(
                request_path,
                store._cipher().encrypt(json.dumps(payload, ensure_ascii=False).encode()),
            )
            _atomic_private(requests / "wake.json", json.dumps({"operation_key": key}).encode())
        fcntl.flock(fd, fcntl.LOCK_UN)
        deadline = time.monotonic() + timeout
        while True:
            if result_path.exists():
                result = json.loads(_read_private(result_path))
                result["persistent_sender"] = True
                result["submit_total_ms"] = round((time.monotonic() - started) * 1000)
                if existed:
                    result["repeat_blocked"] = True
                return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {
                    "status": "pending",
                    "persistent_sender": True,
                    "automatic_resend": False,
                    "operation": operation,
                }
            watcher.wait(remaining)
    finally:
        watcher.close()
        os.close(fd)


def serve(store, *, auth_provider=None):
    os.umask(0o077)
    _, chat = verified_identity(store)
    root, requests, results = folders(store)
    fd = os.open(root / "listener.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise BridgeError("A persistent sender is already running") from None
    watcher = FileWake(requests)
    transport = create_owner_transport(store, auth_provider=auth_provider)
    stopped = False

    def state(value, **fields):
        report = {
            "status": value,
            "updated_ms": time.time_ns() // 1_000_000,
            "protocol_version": 2,
            "group_replies": True,
            **fields,
        }
        _atomic_private(root / "status.json", json.dumps(report).encode())
        print(json.dumps(report), flush=True)

    def stop(signum, frame):
        nonlocal stopped
        stopped = True
        _atomic_private(requests / "wake.json", b'{"stop":true}')

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    state("warming")
    try:
        started = time.monotonic()
        response = transport.session.request(
            "GET",
            transport.base + "/open-apis/im/v1/messages",
            headers=transport._headers(),
            params={
                "container_id_type": "chat",
                "container_id": chat,
                "page_size": 1,
                "sort_type": "ByCreateTimeDesc",
            },
            timeout=30,
            allow_redirects=False,
        )
        transport._json(response)  # Read only, no history watermark and no content output.
        state("ready", warmup_ms=round((time.monotonic() - started) * 1000))
        while not stopped:
            for path in sorted(requests.glob("*.enc"), key=lambda p: p.stat().st_mtime_ns):
                result_path = results / (path.stem + ".json")
                if result_path.exists():
                    continue
                try:
                    payload = json.loads(store._cipher().decrypt(_read_private(path)))
                    if operation_key(payload.get("operation")) != path.stem:
                        raise BridgeError("Operation filename mismatch")
                    allowed = {"operation", "text", "reply_to", "target_chat", "reply_in_thread"}
                    if not {"operation", "text", "reply_to"} <= set(payload) <= allowed:
                        raise BridgeError("Unsupported outbound request shape")
                    if (
                        not isinstance(payload["text"], str)
                        or not payload["text"]
                        or len(payload["text"].encode()) > 100000
                    ):
                        raise BridgeError("Invalid outbound text")
                    result = send_once(
                        store,
                        transport,
                        payload["operation"],
                        payload["text"],
                        payload["reply_to"],
                        payload.get("target_chat"),
                        payload.get("reply_in_thread", False),
                    )
                except Exception as exc:
                    result = safe_error(exc)
                from .sender import TransportSender

                result = TransportSender._canonical(result)
                result["result_recorded_ms"] = time.time_ns() // 1_000_000
                _atomic_private(result_path, json.dumps(result).encode())
                _atomic_private(
                    results / "wake.json", json.dumps({"operation_key": path.stem}).encode()
                )
                print(
                    json.dumps(
                        {"status": "operation_result", "operation_key": path.stem, **result}
                    ),
                    flush=True,
                )
                if stopped:
                    break
            if not stopped:
                watcher.wait(3600)
    finally:
        state("stopped")
        watcher.close()
        transport.session.close()
        os.close(fd)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["serve", "status", "send"])
    parser.add_argument("--operation")
    parser.add_argument("--text")
    parser.add_argument("--reply-to")
    parser.add_argument("--reply-in-thread", action="store_true")
    parser.add_argument(
        "--target-chat", help="Explicit allowlisted group; requires a read owner @message"
    )
    parser.add_argument("--timeout", type=float, default=20)
    args = parser.parse_args()
    store = EncryptedStore()
    if args.action == "serve":
        serve(store)
        return 0
    if args.action == "status":
        root, _, _ = folders(store)
        result = (
            json.loads(_read_private(root / "status.json"))
            if (root / "status.json").exists()
            else {"status": "not_started"}
        )
        result["running"] = listener_alive(root) if (root / "listener.lock").exists() else False
    else:
        result = submit(
            store,
            args.operation,
            args.text,
            args.reply_to,
            args.timeout,
            args.target_chat,
            args.reply_in_thread,
        )
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps(safe_error(exc)), flush=True)
        raise SystemExit(1) from None
