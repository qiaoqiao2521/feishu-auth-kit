"""Host-profile authentication and a durable, four-state text-send contract."""

from pathlib import Path
from typing import Protocol

from .chat_io import binding_lock, send_once
from .cloud_bridge import BridgeError, OwnerOnlyTransport


class OperationStore(Protocol):
    """Host-supplied atomic durable records and a private directory for locks.

    write must fsync file AND parent directory before returning. This protocol
    stores routing/operation metadata, never the provider's secret or token.
    EncryptedStore is an optional implementation, not a required auth format.
    """

    private: Path

    def read(self, name: str) -> dict: ...
    def write(self, name: str, value: dict) -> None: ...
    def exists(self, name: str) -> bool: ...


def bind_verified_profile(store: OperationStore, binding, *, brand="feishu"):
    """Record the host's independently verified existing profile identity.

    The host must verify app/owner/private chat before calling. This is a trust
    boundary, not identity discovery; no registration or credential generation
    occurs. Do not use message text to supply these fields.
    """
    from dataclasses import asdict

    from .cloud_bridge import DOMAINS

    if (
        binding.version != 1
        or not all((binding.app_id, binding.owner_open_id, binding.private_chat_id))
        or brand not in DOMAINS
    ):
        raise BridgeError("A verified host profile binding is required")
    with binding_lock(store):
        if store.exists("binding") or store.exists("bot"):
            raise BridgeError("Profile binding already exists; refusing replacement")
        store.write(
            "bot",
            {
                "app_id": binding.app_id,
                "owner_open_id": binding.owner_open_id,
                "brand": brand,
                "owner_source": "host_profile",
            },
        )
        store.write("binding", dict(asdict(binding), verified=True))


class TransportSender:
    """Reuse an existing tenant-token provider and persistent HTTP session.

    provider.app_id and provider.get_tenant_access_token(force_refresh=...) use
    the kit auth shape; tokens have .token and .expire. The provider can call a
    host CLI profile, with bounded execution and no token logging. It owns auth
    cache/storage. The sender never changes profiles or copies credentials.
    """

    def __init__(self, store: OperationStore, *, auth_provider, session, timeout=20):
        from .realtime_io import verified_identity

        bot, _ = verified_identity(store)
        if not 0 < timeout < 30:
            raise ValueError("Send timeout must be positive and below host 30-second limit")
        self.store = store
        self.transport = OwnerOnlyTransport(
            auth_client=auth_provider,
            session=session,
            app_id=bot["app_id"],
            owner_open_id=bot["owner_open_id"],
            brand=bot["brand"],
            parse_context=None,
            root=store.private.parent,
        )
        self.transport.send_timeout = timeout

    def send(self, operation, text, *, reply_to=None, target_chat=None, reply_in_thread=False):
        try:
            result = send_once(
                self.store, self.transport, operation, text, reply_to, target_chat, reply_in_thread
            )
        except BridgeError:
            # Only a newly observed target mismatch is a delivery result.
            # Fingerprint/ACL validation errors must remain explicit failures.
            with binding_lock(self.store):
                record = self.store.read("binding").get("chat_deliveries", {}).get(operation)
            if record and record.get("status") == "target_mismatch":
                return self._canonical(dict(record))
            raise
        return self._canonical(result)

    @staticmethod
    def _canonical(result):
        status = result["status"]
        if status in {"sending", "target_mismatch"}:
            status = "unknown"
        elif status == "auth_rejected":
            status = "failed"
        return dict(result, status=status, automatic_resend=False)

    def status(self, operation):
        with binding_lock(self.store):
            record = self.store.read("binding").get("chat_deliveries", {}).get(operation)
        if record is None:
            return {"status": "failed", "reason": "not_registered", "automatic_resend": False}
        return self._canonical(dict(record))

    def reconcile(self, operation, *, message_id, chat_id):
        """Record host-verified platform receipt; never performs a network send.

        The host must independently establish this receipt belongs to this UUID
        and destination. Lack of a receipt is not evidence that sending failed.
        """
        if not message_id:
            raise BridgeError("A verified platform receipt is required")
        with binding_lock(self.store):
            binding = self.store.read("binding")
            record = binding.get("chat_deliveries", {}).get(operation)
            if (
                not record
                or record["status"] not in {"sending", "unknown"}
                or record["expected_chat_id"] != chat_id
            ):
                raise BridgeError("Operation cannot be reconciled with this receipt")
            record.update(status="sent", message_id=message_id, chat_id=chat_id)
            self.store.write("binding", binding)
        return self.status(operation)
