"""Owner-only official Feishu WebSocket intake and blocking local wait.

listen maintains ONE connection and an encrypted durable queue. wait-new is a
local inotify blocking read, not a remote history poll. Default output is
metadata only. --read explicitly claims message IDs using chat_io's existing
watermark; it is reserved for a single host consumer.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import datetime
import fcntl
import json
import os
import select
import signal
import sqlite3
import struct
import threading
import time
from types import SimpleNamespace

from .binding import baseline_ids, binding_verified, owner_verified, private_chat_id
from .cloud_bridge import (
    DOMAINS,
    BridgeError,
    EncryptedStore,
    _atomic_private,
    _private_dir,
    create_owner_transport,
)
from .group_policy import configure_authorized_group, group_policy, group_receipt


def verified_identity(store):
    bot, binding = store.read("bot"), store.read("binding")
    if not (
        binding_verified(binding)
        and owner_verified(bot)
        and binding.get("app_id") == bot.get("app_id")
        and ("version" not in binding or binding.get("owner_open_id") == bot.get("owner_open_id"))
        and private_chat_id(binding)
    ):
        raise BridgeError("Verified owner and app required")
    return bot, private_chat_id(binding)


def normalize_owner_event(payload, bot, chat_id, received_ms=None, policy=None):
    """Called only by the authenticated SDK dispatcher, never a public endpoint."""
    if not isinstance(payload, dict):
        return None
    header, event = payload.get("header") or {}, payload.get("event") or {}
    if not isinstance(header, dict) or not isinstance(event, dict):
        return None
    sender, message = event.get("sender") or {}, event.get("message") or {}
    if not isinstance(sender, dict) or not isinstance(message, dict):
        return None
    sender_id = sender.get("sender_id") or {}
    if not isinstance(sender_id, dict):
        return None
    if (
        header.get("app_id") != bot["app_id"]
        or sender.get("sender_type") != "user"
        or sender_id.get("open_id") != bot["owner_open_id"]
        or not isinstance(message.get("message_id"), str)
        or not message["message_id"]
        or not isinstance(header.get("event_id"), str)
        or not header["event_id"]
        or header.get("event_type", "im.message.receive_v1") != "im.message.receive_v1"
    ):
        return None
    source = "private"
    own_mentions = []
    if message.get("chat_type") == "p2p" and message.get("chat_id") == chat_id:
        pass
    elif (
        message.get("chat_type") == "group"
        and policy
        and message.get("chat_id") == policy["chat_id"]
    ):
        mentions = message.get("mentions")
        if not isinstance(mentions, list):
            return None
        # Trust the authenticated platform mention ID, never name or text scans.
        own_mentions = [
            {"key": mention.get("key"), "id": {"open_id": policy["bot_open_id"]}}
            for mention in mentions
            if isinstance(mention, dict)
            and isinstance(mention.get("id"), dict)
            and mention["id"].get("open_id") == policy["bot_open_id"]
        ]
        if not own_mentions:
            return None
        source = "group"
    else:
        return None
    try:
        created_ms = int(message["create_time"])
        if created_ms < 0:
            return None
    except (TypeError, ValueError, KeyError):
        return None
    received_ms = received_ms if received_ms is not None else time.time_ns() // 1_000_000
    metadata = {
        "event_id": header["event_id"],
        "message_id": message["message_id"],
        "source": source,
        "chat_id": message["chat_id"],
        "msg_type": message.get("message_type"),
        "created_ms": created_ms,
        "received_ms": received_ms,
        "network_delay_ms": received_ms - created_ms,
    }
    # Explicit allowlist excludes SDK verification tokens and arbitrary extra data.
    clean = {
        "header": {"app_id": bot["app_id"], "event_id": header["event_id"]},
        "event": {
            "sender": {"sender_type": "user", "sender_id": {"open_id": bot["owner_open_id"]}},
            "message": {
                key: message.get(key)
                for key in (
                    "message_id",
                    "create_time",
                    "chat_id",
                    "chat_type",
                    "message_type",
                    "content",
                    "thread_id",
                    "root_id",
                    "parent_id",
                )
            },
        },
    }
    if source == "group":
        clean["event"]["message"]["mentions"] = own_mentions
    return metadata, clean


class EventQueue:
    def __init__(self, store, bot, chat_id):
        self.store, self.bot, self.chat_id = store, bot, chat_id
        self.folder = store.private / "realtime"
        _private_dir(self.folder)
        path = self.folder / "events.sqlite3"
        if path.exists() and (path.is_symlink() or not path.is_file()):
            raise BridgeError("Unsafe queue path")
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        self.condition = threading.Condition()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, "
            "message_id TEXT UNIQUE NOT NULL, metadata TEXT NOT NULL, payload BLOB NOT NULL)"
        )
        self.db.commit()
        self.cipher = store._cipher()
        self.state = "starting"
        self.protocol_version = 2
        self.state_since_ms = time.time_ns() // 1_000_000

    def set_state(self, state):
        with self.condition:
            self.state = state
            self.state_since_ms = time.time_ns() // 1_000_000
            _atomic_private(
                self.folder / "status.json",
                json.dumps(
                    {"status": state, "state_since_ms": self.state_since_ms, "protocol_version": 2}
                ).encode(),
            )
            self.condition.notify_all()

    def status(self):
        with self.condition:
            status_path = self.folder / "status.json"
            if status_path.exists():
                current = json.loads(status_path.read_text())
                self.state = current["status"]
                self.state_since_ms = current["state_since_ms"]
                self.protocol_version = current.get("protocol_version", 1)
            seq = self.db.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]
            return {
                "status": self.state,
                "last_seq": seq,
                "state_since_ms": self.state_since_ms,
                "transport": "official_websocket",
                "message_watermark_modified": False,
                "protocol_version": self.protocol_version,
                "group_policy_enabled": self.protocol_version == 2
                and bool(group_policy(self.bot, self.store.read("binding"))),
            }

    def receive(self, payload, received_ms=None):
        policy = group_policy(self.bot, self.store.read("binding"))
        normalized = normalize_owner_event(payload, self.bot, self.chat_id, received_ms, policy)
        if normalized is None:
            return None
        metadata, clean = normalized
        encrypted = self.cipher.encrypt(json.dumps(clean, ensure_ascii=False).encode())
        with self.condition:
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO events(message_id, metadata, payload) VALUES(?,?,?)",
                (metadata["message_id"], json.dumps(metadata), encrypted),
            )
            self.db.commit()
            if cursor.rowcount != 1:
                return None
            metadata["seq"] = cursor.lastrowid
            _atomic_private(
                self.folder / "wake.json", json.dumps({"seq": metadata["seq"]}).encode()
            )
            self.condition.notify_all()
        return metadata

    def wait(self, after, timeout):
        deadline = time.monotonic() + timeout
        with self.condition:
            while True:
                rows = self.db.execute(
                    "SELECT seq,metadata FROM events WHERE seq>? ORDER BY seq LIMIT 100", (after,)
                ).fetchall()
                if rows:
                    events = [dict(json.loads(metadata), seq=seq) for seq, metadata in rows]
                    return {
                        "status": "events",
                        "events": events,
                        "last_seq": rows[-1][0],
                        "connection": self.state,
                        "returned_ms": time.time_ns() // 1_000_000,
                    }
                left = deadline - time.monotonic()
                if left <= 0 or self.state in {"stopped", "error"}:
                    return {
                        "status": "timeout" if left <= 0 else self.state,
                        "events": [],
                        "last_seq": after,
                        "connection": self.state,
                    }
                self.condition.wait(left)


class FileWake:
    """Linux kernel file-change wait. No network listener or timed polling."""

    def __init__(self, folder):
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.fd = self.libc.inotify_init1(os.O_CLOEXEC)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "inotify initialization failed")
        self.wd = self.libc.inotify_add_watch(self.fd, os.fsencode(folder), 0x8 | 0x80)
        if self.wd < 0:
            self.close()
            raise OSError(ctypes.get_errno(), "inotify watch failed")

    def wait(self, timeout):
        deadline = time.monotonic() + timeout
        while True:
            ready, _, _ = select.select([self.fd], [], [], max(0, deadline - time.monotonic()))
            if not ready:
                return False
            data = os.read(self.fd, 65536)
            offset = 0
            while offset + 16 <= len(data):
                _, mask, _, length = struct.unpack_from("iIII", data, offset)
                name = data[offset + 16 : offset + 16 + length].rstrip(b"\0")
                offset += 16 + length
                if (mask & 0x80 and name in {b"wake.json", b"status.json"}) or mask & 0x4000:
                    return True

    def close(self):
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


def listener_alive(folder):
    fd = os.open(folder / "listener.lock", os.O_RDWR | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def local_request(store, action, after=0, timeout=600):
    bot, chat_id = verified_identity(store)
    folder = store.private / "realtime"
    if not folder.exists() or not listener_alive(folder):
        raise BridgeError("WebSocket listener is not running")
    queue = EventQueue(store, bot, chat_id)
    watcher = FileWake(folder)
    deadline = time.monotonic() + min(3600, max(0, timeout))
    notified_ms = None
    try:
        while True:
            status = queue.status()
            if action == "status":
                return status
            result = queue.wait(after, 0)
            if result["events"]:
                result["notification_observed_ms"] = notified_ms
                result["already_queued"] = notified_ms is None
                return result
            left = deadline - time.monotonic()
            if left <= 0 or status["status"] in {"stopped", "error"}:
                return result
            # Watch is installed before querying, so enqueue/query/wait has no
            # lost wakeup window. Only actual filesystem events trigger re-read.
            if watcher.wait(left):
                notified_ms = time.time_ns() // 1_000_000
    finally:
        watcher.close()
        queue.db.close()


def read_queued(store, events, *, auth_provider=None):
    """Host-only explicit claim, sharing chat_io's lock and dedup watermark."""
    from .chat_io import binding_lock

    bot, chat_id = verified_identity(store)
    transport = (
        create_owner_transport(store, auth_provider=auth_provider)
        if auth_provider
        else create_owner_transport(store)
    )
    path = store.private / "realtime" / "events.sqlite3"
    db = sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True)
    cipher = store._cipher()
    messages = []
    try:
        with binding_lock(store):
            binding = store.read("binding")
            seen = set(binding.get("chat_seen_ids", [])) | set(baseline_ids(binding))
            policy = group_policy(bot, binding)
            group_seen = (
                set(binding.get("group_seen_ids", {}).get(policy["chat_id"], []))
                if policy
                else set()
            )
            receipts = binding.setdefault("group_read_receipts", {})
            for event in events:
                if event["message_id"] in seen or event["message_id"] in group_seen:
                    continue
                row = db.execute(
                    "SELECT payload FROM events WHERE seq=? AND message_id=?",
                    (event["seq"], event["message_id"]),
                ).fetchone()
                if row is None:
                    raise BridgeError("Queued event unavailable")
                payload = json.loads(cipher.decrypt(row[0]))
                normalized = normalize_owner_event(
                    payload, bot, chat_id, event["received_ms"], policy
                )
                if normalized is None:
                    raise BridgeError("Queued event owner mismatch")
                source = normalized[0]["source"]
                item = payload["event"]["message"]
                content = item["content"]
                if isinstance(content, str):
                    content = json.loads(content)
                result = {
                    "message_id": item["message_id"],
                    "msg_type": item["message_type"],
                    "source": source,
                    "chat_id": item["chat_id"],
                    "created_at_utc": datetime.datetime.fromtimestamp(
                        int(item["create_time"]) / 1000, datetime.timezone.utc
                    ).isoformat(),
                    "received_ms": event["received_ms"],
                    "network_delay_ms": event["network_delay_ms"],
                }
                result.update({key: item.get(key) for key in ("thread_id", "root_id", "parent_id")})
                if item["message_type"] == "text":
                    result["text"] = content.get("text", "")
                elif source == "private" and item["message_type"] in {"image", "file"}:
                    result["local_attachment"] = transport.receive_event(payload)["path"]
                else:
                    result["content"] = content
                if source == "group":
                    result["reply_context"] = {
                        "target_chat_id": item["chat_id"],
                        "reply_to": item["message_id"],
                        "scope": "group_only",
                        "allow_dm_context": False,
                    }
                    receipts[item["message_id"]] = group_receipt(bot, policy, item["message_id"])
                    group_seen.add(item["message_id"])
                else:
                    seen.add(item["message_id"])
                result["seq"] = event["seq"]
                binding.setdefault("host_inbox", {})[item["message_id"]] = {
                    "status": "pending",
                    "message": result,
                    "attempts": 0,
                }
                messages.append(result)
            binding["chat_seen_ids"] = list(seen)
            if policy:
                binding.setdefault("group_seen_ids", {})[policy["chat_id"]] = list(group_seen)
            store.write("binding", binding)
    finally:
        db.close()
        if hasattr(transport, "session"):
            transport.session.close()
    return messages


def listen(store, *, use_environment_proxy=False, credentials_provider=None):
    os.umask(0o077)
    bot, chat_id = verified_identity(store)
    folder = store.private / "realtime"
    _private_dir(folder)
    fd = os.open(folder / "listener.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise BridgeError("A listener is already running") from None
    queue = EventQueue(store, bot, chat_id)
    queue.set_state("starting")
    import lark_oapi as lark
    import lark_oapi.ws.client as ws_module
    import requests
    from lark_oapi.core.log import logger

    logger.disabled = True

    handshake_session = requests.Session()
    handshake_session.trust_env = use_environment_proxy
    original_requests = ws_module.requests
    original_connect_kwargs = ws_module._ws_connect_kwargs

    def bounded_post(url, **kwargs):
        kwargs.update(timeout=(10, 20), allow_redirects=False)
        return handshake_session.post(url, **kwargs)

    ws_module.requests = SimpleNamespace(post=bounded_post)
    # Opt in only when this host already has a trusted environment proxy route.
    # No environment, network, security or proxy configuration is changed.
    ws_module._ws_connect_kwargs = lambda: {"proxy": True if use_environment_proxy else None}

    def callback(data):
        received = time.time_ns() // 1_000_000
        metadata = queue.receive(json.loads(lark.JSON.marshal(data)), received)
        if metadata:
            print(json.dumps({"status": "owner_event", **metadata}), flush=True)

    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(callback)
        .build()
    )
    started = time.monotonic()

    class CloudClient(lark.ws.Client):
        async def _connect(self):
            await super()._connect()
            queue.set_state("connected")
            print(
                json.dumps(
                    {
                        "status": "connected",
                        "startup_ms": round((time.monotonic() - started) * 1000),
                        "last_seq": queue.status()["last_seq"],
                    }
                ),
                flush=True,
            )

    credentials = credentials_provider() if credentials_provider else bot
    if credentials.get("app_id") != bot["app_id"] or not credentials.get("app_secret"):
        raise BridgeError("WebSocket credentials must match the verified host app")
    client = CloudClient(
        credentials["app_id"],
        credentials["app_secret"],
        event_handler=handler,
        domain=DOMAINS[bot["brand"]],
        log_level=lark.LogLevel.ERROR,
        auto_reconnect=True,
    )
    client.on_reconnecting = lambda: queue.set_state("reconnecting")
    loop = ws_module.loop
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    def loop_error(loop, context):
        queue.set_state("error")
        print(
            json.dumps(
                {"status": "async_error", "error_type": type(context.get("exception")).__name__}
            ),
            flush=True,
        )
        stop.set()

    loop.set_exception_handler(loop_error)

    async def run():
        await asyncio.wait_for(client._connect(), 35)
        ping = asyncio.create_task(client._ping_loop())
        try:
            await stop.wait()
        finally:
            client._auto_reconnect = False
            ping.cancel()
            await client._disconnect()
            await asyncio.gather(ping, return_exceptions=True)

    try:
        loop.run_until_complete(run())
    finally:
        queue.set_state("stopped")
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        os.close(fd)
        queue.db.close()
        handshake_session.close()
        ws_module.requests = original_requests
        ws_module._ws_connect_kwargs = original_connect_kwargs


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action", choices=["listen", "status", "wait-new", "configure-authorized-group"]
    )
    parser.add_argument("--after", type=int, default=0)
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--read", action="store_true")
    parser.add_argument("--chat-id", help="One group explicitly authorized by the operator")
    parser.add_argument("--expected-app-name", help="Optional additional bot-name check")
    parser.add_argument("--use-environment-proxy", action="store_true")
    args = parser.parse_args()
    store = EncryptedStore()
    if args.action == "configure-authorized-group":
        if not args.chat_id:
            parser.error("--chat-id is required for group configuration")
        transport = create_owner_transport(store)
        try:
            result = configure_authorized_group(
                store, transport, chat_id=args.chat_id, expected_app_name=args.expected_app_name
            )
        finally:
            transport.session.close()
        print(json.dumps(result), flush=True)
        return 0
    if args.action == "listen":
        listen(store, use_environment_proxy=args.use_environment_proxy)
        return 0
    result = local_request(store, args.action, args.after, args.timeout)
    if args.read and args.action == "wait-new" and result.get("events"):
        result["messages"] = read_queued(store, result["events"])
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        result = {"status": "error", "error_type": type(exc).__name__}
        if isinstance(getattr(exc, "code", None), int):
            result["error_code"] = exc.code
        print(json.dumps(result), flush=True)
        raise SystemExit(1) from None
