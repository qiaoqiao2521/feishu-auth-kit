from __future__ import annotations

import json
from pathlib import Path

import pytest

from feishu_auth_kit.message_context import parse_feishu_message_context

POST_CASES = json.loads(
    Path(__file__).with_name("message_context_posts.json").read_text(encoding="utf-8")
)


def test_parse_feishu_message_context_extracts_native_fields() -> None:
    context = parse_feishu_message_context(
        {
            "schema": "2.0",
            "header": {
                "event_id": "evt_123",
                "event_type": "im.message.receive_v1",
                "app_id": "cli_xxx",
                "tenant_key": "tenant_123",
            },
            "event": {
                "sender": {
                    "sender_id": {
                        "open_id": "ou_user",
                        "user_id": "u_user",
                    }
                },
                "message": {
                    "message_id": "om_123",
                    "chat_id": "oc_123",
                    "chat_type": "p2p",
                    "message_type": "text",
                    "content": "{\"text\":\"@_user_1 帮我总结今天待办\"}",
                    "mentions": [
                        {
                            "key": "@_user_1",
                            "name": "bot",
                            "id": {"open_id": "ou_bot"},
                        }
                    ],
                },
            },
        }
    )

    assert context.schema == "feishu-auth-kit.message-context.v1"
    assert context.event_type == "im.message.receive_v1"
    assert context.app_id == "cli_xxx"
    assert context.tenant_key == "tenant_123"
    assert context.message_id == "om_123"
    assert context.chat_id == "oc_123"
    assert context.sender_open_id == "ou_user"
    assert context.text == "@_user_1 帮我总结今天待办"
    assert context.prompt_text() == "帮我总结今天待办"
    assert context.mentions[0].open_id == "ou_bot"


@pytest.mark.parametrize("sample", POST_CASES, ids=lambda sample: sample["name"])
def test_post_normalization(sample: dict) -> None:
    context = parse_feishu_message_context(
        {
            "message": {
                "message_type": "post",
                "content": sample["content"],
                "mentions": sample.get("mentions", []),
            }
        }
    )
    expected_prompt = sample.get("expected_prompt", sample["expected_text"])
    assert context.text == sample["expected_text"]
    assert context.prompt_text() == expected_prompt
    assert context.prompt_text(strip_mentions=False) == sample["expected_text"]
    assert context.to_dict()["prompt_text"] == expected_prompt


def test_multiline_text_preserves_code_and_only_strips_mentions_outside_fences() -> None:
    text = "@_user_1 Review:\n```text\n    @_user_1\n```\n  keep indentation"
    context = parse_feishu_message_context(
        {
            "message": {
                "message_type": "text",
                "content": {"text": text},
                "mentions": [{"key": "@_user_1"}],
            }
        }
    )
    assert context.prompt_text() == "Review:\n```text\n    @_user_1\n```\n  keep indentation"
    assert context.prompt_text(strip_mentions=False) == text


def test_single_line_whitespace_normalization_is_preserved() -> None:
    context = parse_feishu_message_context(
        {"content": {"text": "  @_user_1   hello\tworld  "}}
    )
    assert context.prompt_text() == "hello world"

