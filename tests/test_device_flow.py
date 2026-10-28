from __future__ import annotations

from typing import Any

import pytest

from feishu_auth_kit.device_flow import DeviceFlowClient, DeviceFlowError


class FakeResponse:
    def __init__(self, payload: Any, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def json(self) -> dict:
        if isinstance(self._payload, ValueError):
            raise self._payload
        return self._payload

    def raise_for_status(self) -> None:
        return None


class FakeSession:
    def __init__(self, responses: list[FakeResponse] | None = None) -> None:
        self.calls: list[dict] = []
        self.responses = responses

    def request(self, method: str, url: str, **kwargs) -> FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        if self.responses is not None:
            return self.responses.pop(0)
        return FakeResponse(
            {
                "device_code": "dev-123",
                "user_code": "ABCD-EFGH",
                "verification_uri": "https://example.test/verify",
                "verification_uri_complete": "https://example.test/verify?code=ABCD-EFGH",
                "expires_in": 600,
                "interval": 5,
            }
        )


def test_device_authorization_request_uses_form_body_and_adds_offline_access() -> None:
    session = FakeSession()
    client = DeviceFlowClient("cli_a1b2", "secret", session=session)

    auth = client.request_authorization(["im:message:readonly"])

    assert auth.device_code == "dev-123"
    assert len(session.calls) == 1
    call = session.calls[0]
    assert call["method"] == "POST"
    assert call["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    assert "scope=im%3Amessage%3Areadonly+offline_access" in call["data"]
    assert "client_id=cli_a1b2" in call["data"]


def poll_client(monkeypatch, responses: list[FakeResponse]):
    now = 0.0
    sleeps: list[float] = []

    def sleeper(seconds: float) -> None:
        nonlocal now
        sleeps.append(seconds)
        now += seconds

    monkeypatch.setattr("feishu_auth_kit.device_flow.time.monotonic", lambda: now)
    session = FakeSession(responses)
    client = DeviceFlowClient("cli_a1b2", "app-secret", session=session, sleeper=sleeper)
    return client, session, sleeps


@pytest.mark.parametrize("status", [200, 400])
def test_pending_slow_down_then_success(monkeypatch, status: int) -> None:
    client, session, sleeps = poll_client(
        monkeypatch,
        [
            FakeResponse({"error": "authorization_pending"}, status),
            FakeResponse({"error": "slow_down"}, status),
            FakeResponse({"error": "slow_down"}, status),
            FakeResponse({"access_token": "token", "refresh_token": "refresh", "expires_in": 7200}),
        ],
    )
    token = client.poll_for_token("device-code", interval=5, expires_in=100)
    assert token.access_token == "token"
    assert token.refresh_token == "refresh"
    assert token.expires_in == 7200
    assert sleeps == [5, 5, 10, 15]
    assert len(session.calls) == 4


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_other_http_failures_do_not_retry_pending(monkeypatch, status: int) -> None:
    client, session, _ = poll_client(
        monkeypatch, [FakeResponse({"error": "authorization_pending"}, status)]
    )
    with pytest.raises(DeviceFlowError, match=f"^HTTP error {status}$"):
        client.poll_for_token("device-code")
    assert len(session.calls) == 1


def test_access_token_in_http_400_is_not_success(monkeypatch) -> None:
    client, _, _ = poll_client(monkeypatch, [FakeResponse({"access_token": "token"}, 400)])
    with pytest.raises(DeviceFlowError, match="^HTTP error 400$"):
        client.poll_for_token("device-code")


def test_terminal_oauth_error_redacts_credentials(monkeypatch) -> None:
    client, session, _ = poll_client(
        monkeypatch,
        [FakeResponse({
            "error": "access_denied",
            "error_description": "Denied device-code for app-secret",
        }, 400)],
    )
    with pytest.raises(DeviceFlowError) as exc:
        client.poll_for_token("device-code")
    assert str(exc.value) == "Denied [REDACTED] for [REDACTED]"
    assert len(session.calls) == 1


@pytest.mark.parametrize("payload", [None, [], "invalid", {}, ValueError("raw app-secret")])
def test_malformed_token_envelope(monkeypatch, payload: Any) -> None:
    client, _, _ = poll_client(monkeypatch, [FakeResponse(payload)])
    with pytest.raises(DeviceFlowError):
        client.poll_for_token("device-code")


def test_non_json_http_error_does_not_echo_body(monkeypatch) -> None:
    client, _, _ = poll_client(monkeypatch, [FakeResponse(ValueError("raw app-secret"), 400)])
    with pytest.raises(DeviceFlowError, match="^HTTP error 400$"):
        client.poll_for_token("device-code")


def test_no_poll_when_initial_wait_reaches_expiry(monkeypatch) -> None:
    client, session, sleeps = poll_client(monkeypatch, [])
    with pytest.raises(DeviceFlowError, match="Device code expired"):
        client.poll_for_token("device-code", interval=5, expires_in=3)
    assert sleeps == [3]
    assert session.calls == []


def test_clock_reads_crossing_expiry_never_produce_negative_sleep(monkeypatch) -> None:
    clock_reads = iter([0.0, 0.9, 1.1])
    monkeypatch.setattr("feishu_auth_kit.device_flow.time.monotonic", lambda: next(clock_reads))
    sleeps: list[float] = []

    def sleeper(seconds: float) -> None:
        assert seconds >= 0
        sleeps.append(seconds)

    session = FakeSession([])
    client = DeviceFlowClient("cli_a1b2", "app-secret", session=session, sleeper=sleeper)
    with pytest.raises(DeviceFlowError, match="Device code expired"):
        client.poll_for_token("device-code", expires_in=1)
    assert sleeps == [pytest.approx(0.1)]
    assert session.calls == []


def test_pending_stops_at_expiry(monkeypatch) -> None:
    client, session, sleeps = poll_client(
        monkeypatch, [FakeResponse({"error": "authorization_pending"}, 400)]
    )
    with pytest.raises(DeviceFlowError, match="Device code expired"):
        client.poll_for_token("device-code", interval=5, expires_in=7)
    assert sleeps == [5, 2]
    assert len(session.calls) == 1


def test_authorization_http_error_does_not_echo_body() -> None:
    session = FakeSession([FakeResponse(ValueError("raw app-secret"), 502)])
    client = DeviceFlowClient("cli_a1b2", "app-secret", session=session)
    with pytest.raises(DeviceFlowError, match="^HTTP error 502$"):
        client.request_authorization([])
