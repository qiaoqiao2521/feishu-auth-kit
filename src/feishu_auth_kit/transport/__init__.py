"""Optional Linux transport. Existing authentication imports remain unchanged."""

from .binding import TransportBinding, configure_binding
from .cloud_bridge import BridgeError, EncryptedStore, OwnerOnlyTransport
from .host_callback import consume_once, deliver_pending, inbox_status, recover_delivery
from .sender import OperationStore, RequestExecutor, TransportSender, bind_verified_profile

__all__ = [
    "BridgeError",
    "EncryptedStore",
    "OwnerOnlyTransport",
    "TransportBinding",
    "configure_binding",
    "OperationStore",
    "RequestExecutor",
    "TransportSender",
    "bind_verified_profile",
    "consume_once",
    "deliver_pending",
    "inbox_status",
    "recover_delivery",
]
