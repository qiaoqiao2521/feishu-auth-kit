"""Experimental offline iLink text boundary; no login, transport or credential store."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"missing or invalid {name}")
    return value


@dataclass(frozen=True)
class ILinkTextTurn:
    account_id: str
    sender_id: str
    message_id: str
    text: str
    context_token: str = field(repr=False, compare=False)

    def to_dict(self) -> dict[str, str]:
        """Safe runner input: deliberately excludes the opaque reply capability."""
        return {
            "schema": "feishu-auth-kit.weixin-ilink-text.v1",
            "source": "weixin_ilink",
            "account_id": self.account_id,
            "sender_id": self.sender_id,
            "message_id": self.message_id,
            "text": self.text,
        }


def parse_text_turn(
    message: dict[str, Any], *, account_id: str, allowed_senders: set[str]
) -> ILinkTextTurn:
    """Accept only explicitly allowed, complete, direct, pure-text user messages.

    Account and sender policies must come from trusted host configuration, not
    message text. Host owns durable deduplication and authentication of transport.
    """
    _text(account_id, "account_id")
    if not isinstance(message, dict):
        raise ValueError("message must be an object")
    if message.get("group_id"):
        raise ValueError("group messages are unsupported")
    if type(message.get("message_type")) is not int or message["message_type"] != 1:
        raise ValueError("not a user message")
    if type(message.get("message_state")) is not int or message["message_state"] != 2:
        raise ValueError("message is not complete")
    if message.get("to_user_id") != account_id:
        raise ValueError("account mismatch")
    sender = _text(message.get("from_user_id"), "sender")
    if sender not in allowed_senders:
        raise ValueError("sender not allowed")
    items = message.get("item_list")
    if not isinstance(items, list) or not items:
        raise ValueError("missing text items")
    texts = []
    for item in items:
        if not isinstance(item, dict) or type(item.get("type")) is not int or item["type"] != 1:
            raise ValueError("only pure text is supported")
        if item.get("ref_msg"):
            raise ValueError("quoted messages require host resolution")
        body = item.get("text_item")
        if not isinstance(body, dict):
            raise ValueError("invalid text item")
        texts.append(_text(body.get("text"), "text"))
    return ILinkTextTurn(
        account_id, sender, _text(message.get("message_id"), "message_id"),
        "\n".join(texts), _text(message.get("context_token"), "context_token"),
    )


def build_text_reply(turn: ILinkTextTurn, text: str, *, client_id: str) -> dict[str, Any]:
    """Build only; the returned body contains a capability and must not be logged.

    This is not sending approval. The host must separately authorize sending,
    supply base_info/headers, enforce expiry and retain unknown-send outcomes.
    """
    return {"msg": {
        "from_user_id": "", "to_user_id": _text(turn.sender_id, "sender"),
        "client_id": _text(client_id, "client_id"), "message_type": 2,
        "message_state": 2,
        "item_list": [{"type": 1, "text_item": {"text": _text(text, "text")}}],
        "context_token": _text(turn.context_token, "context_token"),
    }}
