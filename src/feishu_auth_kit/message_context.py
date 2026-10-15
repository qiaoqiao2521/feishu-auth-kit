from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {"text": value}
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    return {}


def _first_string(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value
    return None


@dataclass(frozen=True)
class FeishuMention:
    key: str | None = None
    name: str | None = None
    open_id: str | None = None
    user_id: str | None = None
    union_id: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "key": self.key,
            "name": self.name,
            "open_id": self.open_id,
            "user_id": self.user_id,
            "union_id": self.union_id,
        }


@dataclass(frozen=True)
class FeishuMessageContext:
    schema: str = "feishu-auth-kit.message-context.v1"
    source: str = "feishu"
    delivery: str = "event_callback"
    event_id: str | None = None
    event_type: str | None = None
    app_id: str | None = None
    tenant_key: str | None = None
    chat_id: str | None = None
    chat_type: str | None = None
    message_id: str | None = None
    message_type: str = "text"
    sender_open_id: str | None = None
    sender_user_id: str | None = None
    sender_union_id: str | None = None
    text: str = ""
    mentions: list[FeishuMention] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def prompt_text(self, *, strip_mentions: bool = True) -> str:
        prompt = self.text.strip()
        if strip_mentions:
            prompt = _strip_mentions_outside_code(prompt, self.mentions)
        if self.message_type == "post" or "\n" in prompt:
            return prompt.strip()
        return " ".join(prompt.split())

    def to_dict(self, *, include_raw: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema": self.schema,
            "source": self.source,
            "delivery": self.delivery,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "app_id": self.app_id,
            "tenant_key": self.tenant_key,
            "chat_id": self.chat_id,
            "chat_type": self.chat_type,
            "message_id": self.message_id,
            "message_type": self.message_type,
            "sender_open_id": self.sender_open_id,
            "sender_user_id": self.sender_user_id,
            "sender_union_id": self.sender_union_id,
            "text": self.text,
            "prompt_text": self.prompt_text(),
            "mentions": [mention.to_dict() for mention in self.mentions],
        }
        if include_raw:
            payload["raw"] = self.raw
        return payload


def _escape_markdown_text(text: str) -> str:
    return re.sub(r"([\\`*_{}[\]()#+\-!|>~])", r"\\\1", text)


def _strip_mentions_outside_code(text: str, mentions: list[FeishuMention]) -> str:
    # Mention placeholders inside a fenced example are literal code.
    fence: str | None = None
    lines: list[str] = []
    for line in text.split("\n"):
        marker = re.match(r"^[ \t]{0,3}(`{3,}|~{3,})(.*)$", line)
        if fence:
            if (
                marker
                and marker[1][0] == fence[0]
                and len(marker[1]) >= len(fence)
                and not marker[2].strip()
            ):
                fence = None
        elif marker:
            fence = marker[1]
        else:
            for mention in mentions:
                if mention.key:
                    line = line.replace(_escape_markdown_text(mention.key), " ")
                    line = line.replace(mention.key, " ")
            line = re.sub(r"(^|\s)@_[A-Za-z0-9_:-]+", " ", line)
            line = re.sub(r"(^|\s)@\\_[A-Za-z0-9\\_:-]+", " ", line)
        lines.append(line)
    return "\n".join(lines)


def _string_value(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _post_payload(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict) and isinstance(value.get("content"), list):
        return value
    return None


def _resolve_post_payload(value: dict[str, Any]) -> dict[str, Any] | None:
    direct = _post_payload(value)
    if direct is not None:
        return direct
    for candidate in (value.get("post"), value):
        nested = _post_payload(candidate)
        if nested is not None:
            return nested
        if isinstance(candidate, dict):
            for locale in candidate.values():
                localized = _post_payload(locale)
                if localized is not None:
                    return localized
    return None


def _render_post_element(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    text = _string_value(value.get("text"))
    tag = _string_value(value.get("tag")).strip().lower()
    if tag == "a":
        href = _string_value(value.get("href")).strip()
        label = _escape_markdown_text(text or href)
        return f"[{label}]({href})" if href else label
    if tag == "at":
        mention = (
            _string_value(value.get("user_name"))
            or _string_value(value.get("user_id"))
            or _string_value(value.get("open_id"))
        )
        return f"@{_escape_markdown_text(mention)}" if mention else ""
    if tag in ("code_block", "pre"):
        language = re.sub(
            r"[^A-Za-z0-9_+#.-]",
            "",
            (_string_value(value.get("language")) or _string_value(value.get("lang"))).strip(),
        )
        code = (text or _string_value(value.get("content"))).replace("\r\n", "\n")
        # Feishu code blocks can contain Markdown fences of their own.
        length = max([3, *(len(run) + 1 for run in re.findall(r"`+", code))])
        fence = "`" * length
        trailing_newline = "" if code.endswith("\n") else "\n"
        return f"{fence}{language}\n{code}{trailing_newline}{fence}"
    if tag in ("md", "lark_md"):
        return text or _string_value(value.get("content"))
    if tag == "br":
        return "\n"
    if tag == "img":
        return "![image]"
    if tag == "media":
        return "[media]"
    return _escape_markdown_text(text)


def _render_post_paragraph(row: list[Any]) -> str:
    rendered = ""
    after_code_block = False
    for element in row:
        text = _render_post_element(element)
        if not text:
            continue
        tag = _string_value(element.get("tag")).strip().lower()
        code_block = tag in ("code_block", "pre")
        if (code_block and rendered and not rendered.endswith("\n")) or (
            after_code_block and not text.startswith("\n")
        ):
            rendered += "\n"
        rendered += text
        after_code_block = code_block
    return rendered


def _parse_post_text(content: Any) -> str:
    value = content
    if isinstance(content, str):
        try:
            value = json.loads(content)
        except json.JSONDecodeError:
            return "[Rich text message]"
    payload = _resolve_post_payload(_as_dict(value))
    if payload is None:
        return "[Rich text message]"
    paragraphs = [
        _render_post_paragraph(row)
        for row in payload["content"]
        if isinstance(row, list)
    ]
    title = _escape_markdown_text(_string_value(payload.get("title")).strip())
    body = "\n".join(paragraphs).strip()
    return "\n\n".join(part for part in (title, body) if part) or "[Rich text message]"


def _parse_text(message_type: str, content: Any) -> str:
    if message_type == "post":
        return _parse_post_text(content)
    content_payload = _as_dict(content)
    if "text" in content_payload:
        return str(content_payload["text"])
    if message_type == "text" and isinstance(content, str):
        return content
    if content_payload:
        return json.dumps(content_payload, ensure_ascii=False, sort_keys=True)
    return ""


def _parse_mentions(message: dict[str, Any]) -> list[FeishuMention]:
    mentions: list[FeishuMention] = []
    for item in message.get("mentions") or []:
        if not isinstance(item, dict):
            continue
        mention_id = item.get("id") if isinstance(item.get("id"), dict) else {}
        mentions.append(
            FeishuMention(
                key=_first_string(item.get("key")),
                name=_first_string(item.get("name")),
                open_id=_first_string(item.get("open_id"), mention_id.get("open_id")),
                user_id=_first_string(item.get("user_id"), mention_id.get("user_id")),
                union_id=_first_string(item.get("union_id"), mention_id.get("union_id")),
            )
        )
    return mentions


def parse_feishu_message_context(payload: dict[str, Any]) -> FeishuMessageContext:
    header = _as_dict(payload.get("header"))
    event = _as_dict(payload.get("event") or payload)
    message = _as_dict(event.get("message") or payload.get("message"))
    sender = _as_dict(event.get("sender") or payload.get("sender"))
    sender_id = _as_dict(sender.get("sender_id") or sender.get("id") or sender)
    message_type = _first_string(message.get("message_type"), payload.get("message_type")) or "text"

    return FeishuMessageContext(
        event_id=_first_string(header.get("event_id"), payload.get("event_id")),
        event_type=_first_string(header.get("event_type"), payload.get("event_type")),
        app_id=_first_string(header.get("app_id"), payload.get("app_id")),
        tenant_key=_first_string(
            header.get("tenant_key"),
            event.get("tenant_key"),
            sender.get("tenant_key"),
            payload.get("tenant_key"),
        ),
        chat_id=_first_string(message.get("chat_id"), payload.get("chat_id")),
        chat_type=_first_string(message.get("chat_type"), payload.get("chat_type")),
        message_id=_first_string(
            message.get("message_id"),
            message.get("open_message_id"),
            payload.get("message_id"),
            payload.get("open_message_id"),
        ),
        message_type=message_type,
        sender_open_id=_first_string(sender_id.get("open_id"), payload.get("sender_open_id")),
        sender_user_id=_first_string(sender_id.get("user_id"), payload.get("sender_user_id")),
        sender_union_id=_first_string(sender_id.get("union_id"), payload.get("sender_union_id")),
        text=_parse_text(message_type, message.get("content", payload.get("content"))),
        mentions=_parse_mentions(message),
        raw=payload,
    )
