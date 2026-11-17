"""Narrow owner-mention policy for the one explicitly configured group.

No automatic reply, OAuth grant, membership change, or arbitrary allowlist.
Identity and read receipts live only in the existing encrypted binding record.
"""

from __future__ import annotations

import re
import time

from .binding import binding_verified, owner_verified, private_chat_id
from .cloud_bridge import BridgeError

POLICY_VERSION = 1


def group_policy(bot, binding):
    """Fail closed for groups; a missing/invalid policy does not disable DMs."""
    policy = binding.get("owner_mention_group")
    if not isinstance(policy, dict):
        return None
    if (
        not binding_verified(binding)
        or binding.get("app_id") != bot.get("app_id")
        or not owner_verified(bot)
        or not bot.get("owner_open_id")
        or policy.get("version") != POLICY_VERSION
        or policy.get("enabled") is not True
        or policy.get("app_id") != bot.get("app_id")
        or policy.get("owner_open_id") != bot.get("owner_open_id")
        or not re.fullmatch(r"oc_[A-Za-z0-9]+", str(policy.get("chat_id", "")))
        or policy.get("bot_open_id_source") != "authenticated_bot_v3_info"
        or not re.fullmatch(r"ou_[A-Za-z0-9]+", str(policy.get("bot_open_id", "")))
        or policy.get("bot_open_id") == bot.get("owner_open_id")
    ):
        return None
    return policy


def configure_authorized_group(store, transport, *, chat_id, expected_app_name=None):
    """Activate one caller-authorized group; only network call is read-only bot info.

    This does not prove group membership or grant permission. The host must
    obtain authorization for chat_id before calling. Name is an optional extra
    check, never a substitute for authenticated app-scoped bot identity."""
    from .chat_io import binding_lock
    from .realtime_io import verified_identity

    bot, _ = verified_identity(store)
    if not re.fullmatch(r"oc_[A-Za-z0-9]+", str(chat_id)):
        raise BridgeError("An explicitly authorized group ID is required")
    if transport.app_id != bot["app_id"]:
        raise BridgeError("The authorized group belongs to a different bot")
    response = transport.session.request(
        "GET",
        transport.base + "/open-apis/bot/v3/info/",
        headers=transport._headers(),
        timeout=30,
        allow_redirects=False,
    )
    if response.status_code != 200:
        raise BridgeError("Bot identity lookup failed")
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("code") != 0:
        raise BridgeError("Bot identity lookup rejected")
    info = payload.get("bot") or {}
    open_id = info.get("open_id")
    if (
        expected_app_name is not None and info.get("app_name") != expected_app_name
    ) or not re.fullmatch(r"ou_[A-Za-z0-9]+", str(open_id)):
        raise BridgeError("Unexpected bot identity")
    policy = {
        "version": POLICY_VERSION,
        "enabled": True,
        "app_id": bot["app_id"],
        "chat_id": chat_id,
        "owner_open_id": bot["owner_open_id"],
        "bot_open_id": open_id,
        "bot_open_id_source": "authenticated_bot_v3_info",
        "verified_at_ms": time.time_ns() // 1_000_000,
    }
    with binding_lock(store):
        binding = store.read("binding")
        if not group_policy(bot, dict(binding, owner_mention_group=policy)):
            raise BridgeError("Invalid group policy")
        binding["owner_mention_group"] = policy
        store.write("binding", binding)
    return {
        "status": "group_policy_configured",
        "chat_id": chat_id,
        "bot_open_id": open_id,
        "requires_owner": True,
        "requires_bot_mention": True,
        "automatic_reply": False,
    }


def group_receipt(bot, policy, message_id):
    return {
        "source": "group",
        "chat_id": policy["chat_id"],
        "message_id": message_id,
        "app_id": bot["app_id"],
        "owner_open_id": bot["owner_open_id"],
        "bot_open_id": policy["bot_open_id"],
        "mentioned_bot": True,
    }


def resolve_reply_target(store, binding, reply_to=None, target_chat=None):
    """Explicit group target AND an already-read owner @message are mandatory."""
    private_chat = private_chat_id(binding)
    receipts = binding.get("group_read_receipts", {})
    if target_chat is None:
        if reply_to and (reply_to in receipts or reply_to not in binding.get("chat_seen_ids", [])):
            raise BridgeError("Private reply target is not a verified private owner message")
        return {"source": "private", "chat_id": private_chat}
    bot = store.read("bot")
    policy = group_policy(bot, binding)
    if not policy or target_chat != policy["chat_id"] or not reply_to:
        raise BridgeError("Explicit authorized group and read owner mention are required")
    expected = group_receipt(bot, policy, reply_to)
    if (
        receipts.get(reply_to) != expected
        or reply_to not in binding.get("group_seen_ids", {}).get(target_chat, [])
        or reply_to in binding.get("chat_seen_ids", [])
    ):
        raise BridgeError("Group reply target is not a read owner mention in this group")
    return {"source": "group", "chat_id": target_chat}
