from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from feishu_auth_kit.slack_transport import (
    MAX_BODY_BYTES,
    SLACK_POST_MESSAGE_URL,
    SlackBinding,
    SlackSender,
    _NoRedirect,
    parse_slack_request,
)

SECRET = "test-signing-secret"
NOW = 1_800_000_000
BINDING = SlackBinding("T1", "A1", "U1", "D1", "https://board.test", "company", "agent")


def payload():
    return {
        "type": "event_callback",
        "team_id": "T1",
        "api_app_id": "A1",
        "event_id": "Ev1",
        "event": {
            "type": "message",
            "user": "U1",
            "channel": "D1",
            "channel_type": "im",
            "text": "hello 世界",
            "ts": "1800000000.123456",
        },
    }


def signed(body, timestamp=NOW):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    base = b"v0:" + str(timestamp).encode() + b":" + raw
    return raw, {
        "X-Slack-Request-Timestamp": str(timestamp),
        "X-Slack-Signature": "v0=" + hmac.new(SECRET.encode(), base, hashlib.sha256).hexdigest(),
    }


def parse(body, timestamp=NOW):
    return parse_slack_request(*signed(body, timestamp), SECRET, BINDING, now=NOW)


def test_normalized_event_has_stable_uuid_across_retries_and_thread_replies():
    value = payload()
    first = parse(value)
    assert set(first) == {
        "event_id",
        "client_request_id",
        "text",
        "workspace_id",
        "app_id",
        "user_id",
        "channel_id",
        "message_ts",
    }
    assert UUID(first["client_request_id"]).version == 5
    value["event_id"] = "EvRetry"
    value["event"]["thread_ts"] = "1700000000.000001"
    assert parse(value)["client_request_id"] == first["client_request_id"]
    value["event"]["ts"] = "1800000000.123457"
    assert parse(value)["client_request_id"] != first["client_request_id"]


@pytest.mark.parametrize("field", BINDING.__dataclass_fields__)
@pytest.mark.parametrize("invalid", ["", "  ", None, 1])
def test_binding_requires_nonempty_strings(field, invalid):
    with pytest.raises(ValueError):
        replace(BINDING, **{field: invalid})


@pytest.mark.parametrize("offset", [-301, 301])
def test_old_and_future_requests_rejected(offset):
    with pytest.raises(ValueError):
        parse(payload(), NOW + offset)


@pytest.mark.parametrize("offset", [-300, 300])
def test_timestamp_boundary_accepted(offset):
    assert parse(payload(), NOW + offset)["event_id"] == "Ev1"


def test_raw_body_tamper_and_invalid_signature_rejected():
    raw, headers = signed(payload())
    with pytest.raises(ValueError):
        parse_slack_request(raw + b" ", headers, SECRET, BINDING, now=NOW)
    for signature in ["v1=" + "0" * 64, "v0=é", "v0=" + "0" * 64, None]:
        headers["X-Slack-Signature"] = signature
        with pytest.raises(ValueError):
            parse_slack_request(raw, headers, SECRET, BINDING, now=NOW)


def test_headers_case_insensitive_but_duplicates_rejected():
    raw, headers = signed(payload())
    assert parse_slack_request(
        raw, {k.lower(): v for k, v in headers.items()}, SECRET, BINDING, now=NOW
    )
    headers["x-slack-signature"] = headers["X-Slack-Signature"]
    with pytest.raises(ValueError):
        parse_slack_request(raw, headers, SECRET, BINDING, now=NOW)


@pytest.mark.parametrize(
    "key,value",
    [
        ("team_id", "T2"),
        ("api_app_id", "A2"),
        ("type", "app_rate_limited"),
        ("event_id", ""),
        ("event", []),
    ],
)
def test_envelope_binding_and_shape_rejected(key, value):
    body = payload()
    body[key] = value
    with pytest.raises(ValueError):
        parse(body)


@pytest.mark.parametrize(
    "key,value",
    [
        ("user", "U2"),
        ("channel", "D2"),
        ("channel_type", "channel"),
        ("team", "T2"),
        ("type", "app_mention"),
        ("subtype", "message_changed"),
        ("subtype", "bot_message"),
        ("subtype", None),
        ("bot_id", "B1"),
        ("bot_profile", {}),
        ("app_id", "A1"),
        ("hidden", True),
        ("ts", "bad"),
        ("text", ""),
        ("text", 1),
    ],
)
def test_only_original_owner_dm_text_allowed(key, value):
    body = payload()
    body["event"][key] = value
    with pytest.raises(ValueError):
        parse(body)


@pytest.mark.parametrize(
    "raw", [b"[]", b"null", b"{", b"\xff", b'{"x":1,"x":2}', b"x" * (MAX_BODY_BYTES + 1)]
)
def test_invalid_bodies_fail_closed(raw):
    with pytest.raises(ValueError):
        parse(raw)


def test_url_verification_requires_authentication_and_optional_binding():
    body = {"type": "url_verification", "challenge": "test"}
    assert parse(body) == {"challenge": "test"}
    body.update(team_id="T1", api_app_id="A1")
    assert parse(body) == {"challenge": "test"}
    for key in ["team_id", "api_app_id"]:
        bad = dict(body, **{key: "wrong"})
        with pytest.raises(ValueError):
            parse(bad)
    raw, headers = signed(body)
    with pytest.raises(ValueError):
        parse_slack_request(raw, headers, "other-secret", BINDING, now=NOW)


class Response:
    status = 200

    def __init__(self, value, *, url=SLACK_POST_MESSAGE_URL):
        self.raw = value if isinstance(value, bytes) else json.dumps(value).encode()
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def geturl(self):
        return self.url

    def read(self, size):
        return self.raw[:size]


class Opener:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def open(self, request, *, timeout):
        self.calls.append((request, timeout))
        return self.response


def test_sender_posts_exact_payload_and_verifies_delivery():
    response = {"ok": True, "channel": "D1", "ts": "1800000000.1"}
    opener = Opener(Response(response))
    sender = SlackSender("test-token", opener=opener)
    request_id = str(uuid4())
    assert sender.send("D1", "hello 世界", request_id) == response
    request, timeout = opener.calls[0]
    assert request.full_url == SLACK_POST_MESSAGE_URL
    assert request.method == "POST"
    assert request.get_header("Authorization") == "Bearer test-token"
    assert json.loads(request.data) == {
        "channel": "D1",
        "text": "hello 世界",
        "client_msg_id": request_id,
        "unfurl_links": False,
        "unfurl_media": False,
    }
    assert timeout == 15


@pytest.mark.parametrize(
    "result",
    [
        {"ok": False, "error": "bad_token"},
        {"ok": 1, "channel": "D1", "ts": "1"},
        {"ok": True, "channel": "D2", "ts": "1"},
        {"ok": True, "channel": "D1", "ts": ""},
        {"ok": True, "channel": "D1", "ts": 123},
        [],
        b"not json",
        {"ok": True, "channel": "D1", "ts": "1", "warning": "message_truncated"},
        {
            "ok": True,
            "channel": "D1",
            "ts": "1",
            "response_metadata": {"warnings": ["message_truncated"]},
        },
        b"x" * (MAX_BODY_BYTES + 1),
    ],
)
def test_sender_requires_verified_untruncated_response(result):
    opener = Opener(Response(result))
    with pytest.raises(ValueError):
        SlackSender("test-token", opener=opener).send("D1", "hello", str(uuid4()))
    assert len(opener.calls) == 1  # no unsafe automatic retries


def test_sender_rejects_oversized_text_without_calling_slack():
    opener = Opener(Response({}))
    with pytest.raises(ValueError):
        SlackSender("test-token", opener=opener).send("D1", "x" * 40001, str(uuid4()))
    assert not opener.calls


def test_sender_refuses_redirects():
    with pytest.raises(ValueError):
        _NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://evil.test")
    opener = Opener(Response({"ok": True, "channel": "D1", "ts": "1"}, url="https://evil.test"))
    with pytest.raises(ValueError):
        SlackSender("test-token", opener=opener).send("D1", "hello", str(uuid4()))


def test_transport_errors_do_not_leak_tokens_or_response_bodies():
    class BrokenOpener:
        def open(self, *args, **kwargs):
            raise OSError("secret-token sensitive body")

    with pytest.raises(ValueError) as exc:
        SlackSender("secret-token", opener=BrokenOpener()).send("D1", "hello", str(uuid4()))
    assert "secret-token" not in str(exc.value)
    assert "sensitive body" not in str(exc.value)


def test_sender_rejects_http_failure_without_retry():
    response = Response({"ok": True, "channel": "D1", "ts": "1"})
    response.status = 429
    opener = Opener(response)
    with pytest.raises(ValueError):
        SlackSender("test-token", opener=opener).send("D1", "hello", str(uuid4()))
    assert len(opener.calls) == 1


@pytest.mark.parametrize(
    "channel,text,request_id",
    [
        ("", "hello", str(uuid4())),
        ("D1", "", str(uuid4())),
        ("D1", "hello", "not-uuid"),
    ],
)
def test_sender_invalid_inputs_never_make_network_call(channel, text, request_id):
    opener = Opener(Response({}))
    with pytest.raises(ValueError):
        SlackSender("test-token", opener=opener).send(channel, text, request_id)
    assert opener.calls == []


def test_default_sender_disables_redirects():
    sender = SlackSender("test-token")
    assert any(isinstance(handler, _NoRedirect) for handler in sender._opener.handlers)


def test_maximum_length_text_is_sent_without_truncation():
    opener = Opener(Response({"ok": True, "channel": "D1", "ts": "1"}))
    text = "字" * 40000
    SlackSender("test-token", opener=opener).send("D1", text, str(uuid4()))
    assert json.loads(opener.calls[0][0].data)["text"] == text


@pytest.mark.parametrize("timestamp", ["", "nan", "-1", "1.1", "1" * 21, None])
def test_malformed_timestamp_header_is_rejected(timestamp):
    raw, headers = signed(payload())
    headers["X-Slack-Request-Timestamp"] = timestamp
    with pytest.raises(ValueError):
        parse_slack_request(raw, headers, SECRET, BINDING, now=NOW)


def test_nan_clock_rejected():
    with pytest.raises(ValueError):
        parse_slack_request(*signed(payload()), SECRET, BINDING, now=float("nan"))


class HistoryOpener(Opener):
    def open(self, request, *, timeout):
        self.response.url = request.full_url
        return super().open(request, timeout=timeout)


def history_message(request_id, **updates):
    return dict(
        type="message", text="reply", client_msg_id=request_id, ts="1800000001.000001", **updates
    )


def test_reconcile_exact_unique_message_without_sending():
    from urllib.parse import parse_qs, urlsplit

    request_id = str(uuid4())
    opener = HistoryOpener(Response({"ok": True, "messages": [history_message(request_id)]}))
    result = SlackSender("test-token", opener=opener).reconcile(
        "D1",
        "reply",
        request_id,
        "1800000000.123456",
    )
    assert result == {"ok": True, "channel": "D1", "ts": "1800000001.000001"}
    request, _ = opener.calls[0]
    assert request.method == "GET"
    assert request.data is None
    assert request.get_header("Authorization") == "Bearer test-token"
    url = urlsplit(request.full_url)
    assert (url.scheme, url.netloc, url.path) == (
        "https",
        "slack.com",
        "/api/conversations.history",
    )
    assert parse_qs(url.query) == {
        "channel": ["D1"],
        "oldest": ["1800000000.123456"],
        "inclusive": ["true"],
        "limit": ["200"],
    }
    assert len(opener.calls) == 1


@pytest.mark.parametrize(
    "scenario",
    [
        "no_match",
        "text_only",
        "wrong_text",
        "duplicate",
        "has_more",
        "cursor",
        "missing_scope",
        "old_timestamp",
    ],
)
def test_reconcile_ambiguous_or_unavailable_stays_unknown(scenario):
    request_id = str(uuid4())
    message = history_message(request_id)
    body = {"ok": True, "messages": [message]}
    if scenario == "no_match":
        body["messages"] = []
    elif scenario == "text_only":
        message.pop("client_msg_id")
    elif scenario == "wrong_text":
        message["text"] = "other reply"
    elif scenario == "duplicate":
        body["messages"].append(dict(message))
    elif scenario == "has_more":
        body["has_more"] = True
    elif scenario == "cursor":
        body["response_metadata"] = {"next_cursor": "more"}
    elif scenario == "missing_scope":
        body = {"ok": False, "error": "missing_scope"}
    elif scenario == "old_timestamp":
        message["ts"] = "1700000000.000001"
    opener = HistoryOpener(Response(body))
    assert (
        SlackSender("test-token", opener=opener).reconcile(
            "D1",
            "reply",
            request_id,
            "1800000000.123456",
        )
        is None
    )
    assert len(opener.calls) == 1
    assert opener.calls[0][0].method == "GET"


@pytest.mark.parametrize(
    "scenario",
    ["channel", "message_channel", "bad_ts", "limit", "bad_json", "bad_metadata", "bad_messages"],
)
def test_reconcile_rejects_unverified_response(scenario):
    request_id = str(uuid4())
    message = history_message(request_id)
    body = {"ok": True, "messages": [message]}
    if scenario == "channel":
        body["channel"] = "D2"
    elif scenario == "message_channel":
        message["channel"] = "D2"
    elif scenario == "bad_ts":
        message["ts"] = "nan"
    elif scenario == "limit":
        body["messages"] = [message] * 201
    elif scenario == "bad_json":
        body = b"not JSON"
    elif scenario == "bad_metadata":
        body["response_metadata"] = []
    elif scenario == "bad_messages":
        body["messages"] = {}
    opener = HistoryOpener(Response(body))
    with pytest.raises(ValueError):
        SlackSender("test-token", opener=opener).reconcile(
            "D1",
            "reply",
            request_id,
            "1800000000.123456",
        )
    assert len(opener.calls) == 1


def test_reconcile_rejects_redirected_response():
    opener = Opener(Response({"ok": True, "messages": []}, url="https://evil.test"))
    with pytest.raises(ValueError):
        SlackSender("test-token", opener=opener).reconcile(
            "D1",
            "reply",
            str(uuid4()),
            "1800000000.123456",
        )


@pytest.mark.parametrize("timestamp", ["", "nan", "1&channel=D2", None, 123])
def test_reconcile_invalid_timestamp_no_network(timestamp):
    opener = HistoryOpener(Response({}))
    with pytest.raises(ValueError):
        SlackSender("test-token", opener=opener).reconcile("D1", "reply", str(uuid4()), timestamp)
    assert opener.calls == []


@pytest.mark.parametrize("text", ["x" * 32769, "字" * 10923, "\ud800"])
def test_ingress_rejects_native_adapter_oversized_or_invalid_utf8_text(text):
    body = payload()
    body["event"]["text"] = text
    with pytest.raises(ValueError):
        parse(body)


@pytest.mark.parametrize("text", ["x" * 32768, "字" * 10922 + "ab"])
def test_ingress_accepts_native_adapter_byte_limit(text):
    body = payload()
    body["event"]["text"] = text
    assert parse(body)["text"] == text
