"""Small, fail-closed Slack HTTP boundary using existing app credentials only.

Signature verification follows https://docs.slack.dev/authentication/verifying-requests-from-slack/.
No conversation/session identifier is derived here: a Slack thread is still the bound DM.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, fields
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

MAX_BODY_BYTES = 1024 * 1024
MAX_MESSAGE_CHARS = 40_000
SLACK_POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"
SLACK_HISTORY_URL = "https://slack.com/api/conversations.history"


def _required(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Missing or invalid {name}")
    return value


@dataclass(frozen=True)
class SlackBinding:
    workspace_id: str
    app_id: str
    owner_user_id: str
    dm_channel_id: str
    server_url: str
    company_id: str
    agent_id: str
    board_user_id: str = "local-board"

    def __post_init__(self) -> None:
        for field in fields(self):
            _required(getattr(self, field.name), field.name)


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def parse_slack_request(
    raw_body: bytes,
    headers: Mapping,
    signing_secret: str,
    binding: SlackBinding,
    now: float | None = None,
) -> dict:
    """Authenticate raw bytes before decoding, then enforce the fixed owner/DM binding.

    Timestamps prevent old replays; callers must deduplicate the returned stable
    client_request_id to handle Slack retries within the five-minute window.
    """
    _required(signing_secret, "signing secret")
    if not isinstance(raw_body, bytes) or len(raw_body) > MAX_BODY_BYTES:
        raise ValueError("Invalid Slack request body")
    normalized_headers = {}
    for key, value in headers.items():
        if not isinstance(key, str):
            raise ValueError("Invalid Slack headers")
        key = key.lower()
        if key in normalized_headers:
            raise ValueError("Duplicate Slack header")
        normalized_headers[key] = value
    stamp = normalized_headers.get("x-slack-request-timestamp")
    signature = normalized_headers.get("x-slack-signature")
    if not isinstance(stamp, str) or not re.fullmatch(r"[0-9]{1,20}", stamp):
        raise ValueError("Invalid Slack timestamp")
    clock = time.time() if now is None else now
    if not math.isfinite(clock) or abs(clock - int(stamp)) > 300:
        raise ValueError("Expired Slack timestamp")
    if not isinstance(signature, str) or not re.fullmatch(r"v0=[0-9a-f]{64}", signature):
        raise ValueError("Invalid Slack signature")
    expected = (
        "v0="
        + hmac.new(
            signing_secret.encode("utf-8"),
            b"v0:" + stamp.encode("ascii") + b":" + raw_body,
            hashlib.sha256,
        ).hexdigest()
    )
    if not hmac.compare_digest(expected, signature):
        raise ValueError("Invalid Slack signature")
    try:
        payload = json.loads(raw_body.decode("utf-8"), object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("Invalid Slack JSON") from None
    if not isinstance(payload, dict):
        raise ValueError("Invalid Slack payload")
    if payload.get("type") == "url_verification":
        for key, expected_value in (
            ("team_id", binding.workspace_id),
            ("api_app_id", binding.app_id),
        ):
            if key in payload and payload[key] != expected_value:
                raise ValueError("Slack verification binding mismatch")
        return {"challenge": _required(payload.get("challenge"), "challenge")}
    if payload.get("type") != "event_callback":
        raise ValueError("Unsupported Slack event")
    if (
        payload.get("team_id") != binding.workspace_id
        or payload.get("api_app_id") != binding.app_id
    ):
        raise ValueError("Slack workspace/app binding mismatch")
    event = payload.get("event")
    if not isinstance(event, dict) or event.get("type") != "message":
        raise ValueError("Unsupported Slack message")
    if any(key in event for key in ("subtype", "bot_id", "bot_profile", "app_id", "hidden")):
        raise ValueError("Only original human messages are accepted")
    if event.get("user") != binding.owner_user_id:
        raise ValueError("Slack owner binding mismatch")
    if event.get("channel") != binding.dm_channel_id or event.get("channel_type") != "im":
        raise ValueError("Slack DM binding mismatch")
    if "team" in event and event["team"] != binding.workspace_id:
        raise ValueError("Slack message workspace mismatch")
    message_ts = _required(event.get("ts"), "message timestamp")
    if not re.fullmatch(r"[0-9]+\.[0-9]+", message_ts):
        raise ValueError("Invalid Slack message timestamp")
    event_id = _required(payload.get("event_id"), "event ID")
    text = _required(event.get("text"), "message text")
    try:
        text_bytes = text.encode("utf-8")
    except UnicodeError:
        raise ValueError("Invalid Slack message text encoding") from None
    if len(text_bytes) > 32768:
        raise ValueError("Slack message text exceeds native adapter byte limit")
    identity = json.dumps(
        [binding.workspace_id, binding.app_id, binding.dm_channel_id, message_ts],
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return {
        "event_id": event_id,
        "client_request_id": str(uuid5(NAMESPACE_URL, "slack-message:" + identity)),
        "text": text,
        "workspace_id": binding.workspace_id,
        "app_id": binding.app_id,
        "user_id": binding.owner_user_id,
        "channel_id": binding.dm_channel_id,
        "message_ts": message_ts,
    }


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Slack redirects are not permitted")


class SlackSender:
    """Send to Slack's fixed HTTPS endpoint; never retry uncertain writes automatically.

    An injected opener must implement urllib's ``open`` interface and must not
    follow redirects. It is intended for offline tests, not endpoint substitution.
    """

    def __init__(self, token: str, *, timeout: float = 15, opener=None) -> None:
        self._token = _required(token, "Slack token")
        if any(char.isspace() for char in token):
            raise ValueError("Invalid Slack token")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Invalid Slack timeout")
        self._timeout = timeout
        self._opener = opener if opener is not None else urllib.request.build_opener(_NoRedirect())

    def send(self, channel_id: str, text: str, client_msg_id: str) -> dict:
        _required(channel_id, "channel ID")
        _required(text, "message text")
        if len(text) > MAX_MESSAGE_CHARS:
            raise ValueError("Slack message exceeds 40000 characters; refusing truncation")
        try:
            UUID(_required(client_msg_id, "client message ID"))
        except (ValueError, AttributeError):
            raise ValueError("Invalid client message UUID") from None
        body = json.dumps(
            {
                "channel": channel_id,
                "text": text,
                "client_msg_id": client_msg_id,
                "unfurl_links": False,
                "unfurl_media": False,
            },
            ensure_ascii=True,
        ).encode("utf-8")
        request = urllib.request.Request(
            SLACK_POST_MESSAGE_URL,
            data=body,
            method="POST",
            headers={"Authorization": "Bearer " + self._token, "Content-Type": "application/json"},
        )
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                if response.status != 200 or response.geturl() != SLACK_POST_MESSAGE_URL:
                    raise ValueError("Unverified Slack HTTP response")
                raw = response.read(MAX_BODY_BYTES + 1)
                if len(raw) > MAX_BODY_BYTES:
                    raise ValueError("Oversized Slack response")
            result = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        except (OSError, ValueError, UnicodeError, RecursionError):
            # Do not include exceptions that may contain credentials or response bodies.
            raise ValueError("Slack send failed or delivery is uncertain") from None
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise ValueError("Slack did not confirm delivery")
        if result.get("channel") != channel_id:
            raise ValueError("Slack delivery channel mismatch")
        _required(result.get("ts"), "Slack delivery timestamp")
        metadata = result.get("response_metadata", {})
        if not isinstance(metadata, dict):
            raise ValueError("Invalid Slack response metadata")
        warnings = metadata.get("warnings", [])
        if not isinstance(warnings, list) or any(not isinstance(item, str) for item in warnings):
            raise ValueError("Invalid Slack response warnings")
        if result.get("warning") == "message_truncated" or "message_truncated" in warnings:
            raise ValueError("Slack truncated the message")
        return result

    def reconcile(
        self,
        channel_id: str,
        text: str,
        client_msg_id: str,
        earliest_ts: str,
    ) -> dict | None:
        """Read at most 200 recent messages; never resend an uncertain delivery.

        None means unknown, including absent IDs, missing history scope, or an
        incomplete page. Only one exact ID/body match in a complete bounded
        history window confirms delivery. Slack may normalize text or omit IDs;
        that intentionally leaves delivery unknown rather than guessing.
        """
        _required(channel_id, "channel ID")
        _required(text, "message text")
        try:
            UUID(_required(client_msg_id, "client message ID"))
        except (ValueError, AttributeError):
            raise ValueError("Invalid client message UUID") from None
        if not isinstance(earliest_ts, str) or not re.fullmatch(r"[0-9]+\.[0-9]+", earliest_ts):
            raise ValueError("Invalid earliest Slack timestamp")
        url = (
            SLACK_HISTORY_URL
            + "?"
            + urllib.parse.urlencode(
                {
                    "channel": channel_id,
                    "oldest": earliest_ts,
                    "inclusive": "true",
                    "limit": 200,
                }
            )
        )
        request = urllib.request.Request(
            url,
            method="GET",
            headers={"Authorization": "Bearer " + self._token},
        )
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                if response.status != 200 or response.geturl() != url:
                    raise ValueError("Unverified Slack history response")
                raw = response.read(MAX_BODY_BYTES + 1)
                if len(raw) > MAX_BODY_BYTES:
                    raise ValueError("Oversized Slack history response")
            result = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        except (OSError, ValueError, UnicodeError, RecursionError):
            raise ValueError("Slack history unavailable; delivery remains unknown") from None
        if not isinstance(result, dict):
            raise ValueError("Invalid Slack history response")
        if result.get("ok") is not True:
            return None
        if "channel" in result and result["channel"] != channel_id:
            raise ValueError("Slack history channel mismatch")
        messages = result.get("messages")
        metadata = result.get("response_metadata", {})
        if not isinstance(messages, list) or not isinstance(metadata, dict):
            raise ValueError("Invalid Slack history response")
        if len(messages) > 200:
            raise ValueError("Slack history scan limit exceeded")
        if result.get("has_more", False) is not False or metadata.get("next_cursor"):
            return None
        matches = []
        for message in messages:
            if not isinstance(message, dict):
                raise ValueError("Invalid Slack history message")
            if "channel" in message and message["channel"] != channel_id:
                raise ValueError("Slack history message channel mismatch")
            if message.get("client_msg_id") != client_msg_id:
                continue
            # A conflicting occurrence of the same ID is ambiguity, not success.
            if message.get("type") != "message" or message.get("text") != text:
                return None
            ts = message.get("ts")
            if not isinstance(ts, str) or not re.fullmatch(r"[0-9]+\.[0-9]+", ts):
                raise ValueError("Invalid Slack history timestamp")
            if Decimal(ts) < Decimal(earliest_ts):
                return None
            matches.append(ts)
        if len(matches) != 1:
            return None
        return {"ok": True, "channel": channel_id, "ts": matches[0]}
