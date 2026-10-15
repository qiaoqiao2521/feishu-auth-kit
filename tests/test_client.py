from __future__ import annotations

import pytest

from feishu_auth_kit.client import (
    FeishuApiError,
    FeishuAuthClient,
    FeishuAuthKitError,
    build_permission_url,
)
from feishu_auth_kit.models import AppInfo


class FakeResponse:
    def __init__(
        self,
        payload: dict | ValueError,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def json(self) -> dict:
        if isinstance(self._payload, ValueError):
            raise self._payload
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.calls: list[dict] = []

    def request(self, method: str, url: str, **kwargs) -> FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.responses.pop(0)


def test_build_permission_url_includes_scope_token_type_and_source() -> None:
    url = build_permission_url(
        app_id="cli_a1b2",
        scopes=["offline_access", "im:message:readonly"],
        brand="feishu",
        token_type="user",
        op_from="feishu-auth-kit",
    )

    assert url.startswith("https://open.feishu.cn/app/cli_a1b2/auth?")
    assert "q=offline_access%2Cim%3Amessage%3Areadonly" in url
    assert "token_type=user" in url
    assert "op_from=feishu-auth-kit" in url


def test_get_app_info_parses_scopes_and_token_types() -> None:
    session = FakeSession(
        [
            FakeResponse(
                {
                    "code": 0,
                    "tenant_access_token": "tenant-token",
                    "expire": 7200,
                }
            ),
            FakeResponse(
                {
                    "code": 0,
                    "data": {
                        "app": {
                            "app_id": "cli_a1b2",
                            "creator_id": "ou_creator",
                            "owner": {"owner_id": "ou_owner", "owner_type": 2},
                            "scopes": [
                                {
                                    "scope": "im:message:readonly",
                                    "token_types": ["user"],
                                },
                                {
                                    "scope": "application:application:self_manage",
                                    "token_types": ["tenant"],
                                },
                            ],
                        }
                    },
                }
            ),
        ]
    )
    client = FeishuAuthClient("cli_a1b2", "secret", session=session)

    app_info = client.get_app_info()

    assert isinstance(app_info, AppInfo)
    assert app_info.app_id == "cli_a1b2"
    assert app_info.creator_id == "ou_creator"
    assert app_info.owner_open_id == "ou_owner"
    assert app_info.effective_owner_open_id == "ou_owner"
    assert app_info.scopes[0].token_types == ["user"]
    assert client.get_granted_scopes(token_type="user") == ["im:message:readonly"]
    assert client.get_granted_scopes(token_type="tenant") == ["application:application:self_manage"]


@pytest.mark.parametrize("code", [21001, "21001"])
def test_access_policy_denial_exposes_diagnostics_without_retry(code: int | str) -> None:
    message = "Access denied by access control policy. Contact the administrator for assistance."
    session = FakeSession(
        [
            FakeResponse(
                {
                    "code": code,
                    "msg": message,
                    "log_id": "body-log",
                    "access_token": "must-not-appear",
                },
                headers={"x-tt-logid": "header-log"},
            )
        ]
    )
    client = FeishuAuthClient("cli_test", "app-secret", session=session)

    with pytest.raises(FeishuApiError) as caught:
        client.get_tenant_access_token()

    error = caught.value
    assert isinstance(error, FeishuAuthKitError)
    assert error.code == code
    assert error.http_status == 200
    assert error.log_id == "header-log"
    assert error.api_message == message
    assert message in str(error)
    assert "IP 白名单" in error.hint
    assert "企业管理员" in error.hint
    assert "must-not-appear" not in str(vars(error))
    assert len(session.calls) == 1


def test_http_failure_retains_business_code_status_and_header_log_id() -> None:
    session = FakeSession(
        [
            FakeResponse(
                {"code": 99991672, "msg": "Forbidden", "error": {"log_id": "body-log"}},
                status_code=403,
                headers={"X-Tt-Logid": "http-log"},
            )
        ]
    )
    client = FeishuAuthClient("cli_test", "app-secret", session=session)

    with pytest.raises(FeishuApiError) as caught:
        client.get_tenant_access_token()

    error = caught.value
    assert str(error) == "Forbidden"
    assert error.code == 99991672
    assert error.http_status == 403
    assert error.log_id == "http-log"
    assert error.hint is None
    assert len(session.calls) == 1


@pytest.mark.parametrize(
    "details, expected_log_id",
    [
        ({"log_id": "root-log"}, "root-log"),
        ({"error": {"log_id": "nested-log"}}, "nested-log"),
    ],
)
def test_business_error_preserves_message_and_falls_back_to_payload_log_id(
    details: dict,
    expected_log_id: str,
) -> None:
    session = FakeSession([FakeResponse({"code": 10003, "msg": "invalid param", **details})])
    client = FeishuAuthClient("cli_test", "app-secret", session=session)

    with pytest.raises(FeishuApiError) as caught:
        client.get_tenant_access_token()

    error = caught.value
    assert str(error) == "invalid param"
    assert error.code == 10003
    assert error.http_status == 200
    assert error.log_id == expected_log_id
    assert error.hint is None


def test_non_json_http_failure_preserves_header_diagnostics() -> None:
    session = FakeSession(
        [
            FakeResponse(
                ValueError("app-secret access-token"),
                status_code=502,
                headers={"x-tt-logid": "gateway-log"},
            )
        ]
    )
    client = FeishuAuthClient("cli_test", "app-secret", session=session)

    with pytest.raises(FeishuApiError) as caught:
        client.get_tenant_access_token()

    error = caught.value
    assert str(error) == "HTTP error 502"
    assert error.code is None
    assert error.http_status == 502
    assert error.log_id == "gateway-log"
    assert "app-secret" not in str(vars(error))


def test_api_error_redacts_echoed_app_secret_and_tenant_token() -> None:
    session = FakeSession(
        [
            FakeResponse(
                {"code": 0, "tenant_access_token": "tenant-private-token", "expire": 7200}
            ),
            FakeResponse(
                {
                    "code": 10003,
                    "msg": "invalid app-private-secret / tenant-private-token",
                    "app_secret": "app-private-secret",
                    "access_token": "tenant-private-token",
                },
                status_code=400,
            ),
        ]
    )
    client = FeishuAuthClient("cli_test", "app-private-secret", session=session)

    with pytest.raises(FeishuApiError) as caught:
        client.get_app_info()

    error = caught.value
    assert str(error) == "invalid [REDACTED] / [REDACTED]"
    assert "app-private-secret" not in str(vars(error))
    assert "tenant-private-token" not in str(vars(error))
    assert len(session.calls) == 2


def test_ordinary_api_error_keeps_message_and_inheritance() -> None:
    error = FeishuApiError("existing message")

    assert isinstance(error, RuntimeError)
    assert isinstance(error, FeishuAuthKitError)
    assert str(error) == "existing message"
    assert error.code is None
    assert error.http_status is None
    assert error.log_id is None
    assert error.hint is None
