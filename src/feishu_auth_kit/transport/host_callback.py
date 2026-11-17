"""Durable host intake with explicit recovery and at-least-once callback semantics."""

from .chat_io import binding_lock
from .cloud_bridge import BridgeError
from .realtime_io import local_request, read_queued


def inbox_status(store):
    """Metadata only; processing may mean a live callback or an interrupted one."""
    with binding_lock(store):
        inbox = store.read("binding").get("host_inbox", {})
        return {
            key: {"status": value["status"], "attempts": value["attempts"]}
            for key, value in inbox.items()
        }


def recover_delivery(store, message_id):
    """Replay failed/interrupted delivery after fencing the old worker.

    Effects may already have happened. Deduplicate by message_id and stable
    outbound operation keys. This cannot promise exactly-once callback effects.
    """
    with binding_lock(store):
        binding = store.read("binding")
        entry = binding.get("host_inbox", {}).get(message_id)
        if not entry or entry["status"] not in {"processing", "failed"}:
            raise BridgeError("Delivery is not recoverable")
        entry["status"] = "pending"
        store.write("binding", binding)


def deliver_pending(store, on_message):
    if not callable(on_message):
        raise TypeError("A host message callback is required")
    with binding_lock(store):
        keys = list(store.read("binding").get("host_inbox", {}))
    delivered = 0
    for key in keys:
        with binding_lock(store):
            binding = store.read("binding")
            entry = binding["host_inbox"][key]
            if entry["status"] != "pending":
                continue
            entry["status"] = "processing"
            entry["attempts"] += 1
            message = entry["message"]
            store.write("binding", binding)
        try:
            on_message(message)
        except BaseException:
            with binding_lock(store):
                binding = store.read("binding")
                binding["host_inbox"][key]["status"] = "failed"
                store.write("binding", binding)
            raise
        with binding_lock(store):
            binding = store.read("binding")
            binding["host_inbox"][key]["status"] = "completed"
            store.write("binding", binding)
        delivered += 1
    return delivered


def consume_once(store, *, after, on_message, timeout=600, auth_provider=None):
    """Deliver pending intake, then persist and deliver one SDK queue batch.

    Successful callback means durable custody accepted by the host. Never
    auto-replay failed/processing callbacks. Sequence checkpointing is host-owned.
    """
    delivered = deliver_pending(store, on_message)
    if delivered:
        return {
            "status": "recovered_pending",
            "last_seq": after,
            "delivered_to_callback": delivered,
        }
    batch = local_request(store, "wait-new", after=after, timeout=timeout)
    events = batch.get("events") or []
    if events:
        if auth_provider:
            read_queued(store, events, auth_provider=auth_provider)
        else:
            read_queued(store, events)
    delivered = deliver_pending(store, on_message)
    return {
        "status": batch["status"],
        "last_seq": batch["last_seq"],
        "delivered_to_callback": delivered,
    }
