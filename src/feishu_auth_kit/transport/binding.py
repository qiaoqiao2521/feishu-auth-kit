"""Explicit host-verified binding; legacy records remain readable in place."""

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class TransportBinding:
    app_id: str
    owner_open_id: str
    private_chat_id: str
    baseline_message_ids: tuple[str, ...] = ()
    version: int = 1


def private_chat_id(binding):
    return binding.get("private_chat_id") or binding.get("overview_delivery", {}).get("chat_id")


def baseline_ids(binding):
    return binding.get(
        "baseline_message_ids", binding.get("receive_test", {}).get("baseline_ids", [])
    )


def binding_verified(binding):
    if "version" in binding:
        return binding.get("version") == 1 and binding.get("verified") is True
    return bool(binding.get("name_matches") and binding.get("owner_matches"))


def owner_verified(bot):
    return bot.get("owner_source") in {"registration", "authenticated_app_info", "host_profile"}


def configure_binding(store, binding: TransportBinding, *, auth_client, session=None):
    """Verify owner and private conversation using read-only platform lookups.

    Credentials must already be stored in the local encrypted bot record. No
    diagnostic send or model is required. Rebinding existing state is refused.
    """
    from .chat_io import binding_lock
    from .cloud_bridge import BridgeError, OwnerOnlyTransport

    bot = store.read("bot")
    if (
        binding.version != 1
        or not binding.private_chat_id
        or not binding.owner_open_id
        or bot.get("app_id") != binding.app_id
        or auth_client.app_id != binding.app_id
    ):
        raise BridgeError("Invalid binding or authentication app")
    info = auth_client.get_app_info(force_refresh=True)
    if info.app_id != binding.app_id or info.effective_owner_open_id != binding.owner_open_id:
        raise BridgeError("Authenticated app owner mismatch")
    transport = OwnerOnlyTransport(
        auth_client=auth_client,
        session=session or auth_client.session,
        app_id=binding.app_id,
        owner_open_id=binding.owner_open_id,
        brand=bot["brand"],
        parse_context=None,
        root=store.private.parent,
        token_store=store,
    )
    response = transport.session.request(
        "GET",
        transport.base + "/open-apis/im/v1/messages",
        headers=transport._headers(),
        params={
            "container_id_type": "chat",
            "container_id": binding.private_chat_id,
            "page_size": 50,
        },
        timeout=30,
        allow_redirects=False,
    )
    items = transport._json(response).get("items") or []
    # A known owner message proves the app-visible private conversation. No text
    # or attachments are copied from the response into the binding.
    from urllib.parse import quote

    chat_response = transport.session.request(
        "GET",
        transport.base + "/open-apis/im/v1/chats/" + quote(binding.private_chat_id, safe=""),
        headers=transport._headers(),
        timeout=30,
        allow_redirects=False,
    )
    if transport._json(chat_response).get("chat_mode") != "p2p":
        raise BridgeError("Configured conversation is not private")
    found = any(
        item.get("chat_id") == binding.private_chat_id
        and item.get("sender", {}).get("sender_type") == "user"
        and item.get("sender", {}).get("id_type") == "open_id"
        and item.get("sender", {}).get("id") == binding.owner_open_id
        for item in items
    )
    if not found:
        raise BridgeError("Private conversation needs an app-visible owner message")
    with binding_lock(store):
        if store.exists("binding"):
            raise BridgeError("Binding already exists; explicit migration is required")
        bot.update(owner_open_id=binding.owner_open_id, owner_source="authenticated_app_info")
        store.write("bot", bot)
        store.write("binding", dict(asdict(binding), verified=True))
    return {"status": "bound", "version": 1}
