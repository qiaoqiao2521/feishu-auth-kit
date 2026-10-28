from __future__ import annotations

import base64
import time
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import urlencode

import requests

from .domains import resolve_domains
from .models import DeviceAuthorization, DeviceToken


class DeviceFlowError(RuntimeError):
    pass


class DeviceFlowClient:
    def __init__(
        self,
        app_id: str,
        app_secret: str,
        *,
        brand: str = "feishu",
        session: Any | None = None,
        timeout: int = 30,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.brand = brand
        self.domains = resolve_domains(brand)
        self.session = session or requests.Session()
        self.timeout = timeout
        self.sleeper = sleeper

    @staticmethod
    def _scope_string(scopes: Iterable[str]) -> str:
        items: list[str] = []
        seen: set[str] = set()
        for scope in scopes:
            normalized = scope.strip()
            if normalized and normalized not in seen:
                items.append(normalized)
                seen.add(normalized)
        if "offline_access" not in seen:
            items.append("offline_access")
        return " ".join(items)

    @staticmethod
    def _response_payload(response: Any) -> dict[str, Any]:
        status = response.status_code
        try:
            payload = response.json()
        except ValueError:
            detail = (
                "Invalid device flow response" if 200 <= status < 300 else f"HTTP error {status}"
            )
            raise DeviceFlowError(detail) from None
        if not isinstance(payload, dict):
            detail = (
                "Invalid device flow response" if 200 <= status < 300 else f"HTTP error {status}"
            )
            raise DeviceFlowError(detail)
        return payload

    def _error_detail(
        self, payload: dict[str, Any], fallback: str, device_code: str | None = None
    ) -> str:
        detail = next(
            (
                value
                for value in (payload.get("error_description"), payload.get("error"))
                if isinstance(value, str) and value
            ),
            fallback,
        )
        basic = base64.b64encode(f"{self.app_id}:{self.app_secret}".encode()).decode("ascii")
        for credential in (self.app_secret, basic, device_code):
            if credential:
                detail = detail.replace(credential, "[REDACTED]")
        return detail

    def request_authorization(self, scopes: Iterable[str]) -> DeviceAuthorization:
        scope_string = self._scope_string(scopes)
        basic = base64.b64encode(f"{self.app_id}:{self.app_secret}".encode()).decode("ascii")
        response = self.session.request(
            "POST",
            self.domains.device_authorization_url,
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data=urlencode({"client_id": self.app_id, "scope": scope_string}),
            timeout=self.timeout,
        )
        payload = self._response_payload(response)
        error = payload.get("error")
        if not 200 <= response.status_code < 300 or error:
            raise DeviceFlowError(self._error_detail(payload, f"HTTP error {response.status_code}"))
        return DeviceAuthorization(
            device_code=str(payload["device_code"]),
            user_code=str(payload["user_code"]),
            verification_uri=str(payload["verification_uri"]),
            verification_uri_complete=str(
                payload.get("verification_uri_complete", payload["verification_uri"])
            ),
            expires_in=int(payload.get("expires_in", 240)),
            interval=int(payload.get("interval", 5)),
        )

    def poll_for_token(
        self,
        device_code: str,
        *,
        interval: int = 5,
        expires_in: int = 240,
    ) -> DeviceToken:
        deadline = time.monotonic() + expires_in
        current_interval = max(interval, 1)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self.sleeper(min(current_interval, remaining))
            if time.monotonic() >= deadline:
                break
            response = self.session.request(
                "POST",
                self.domains.oauth_token_url,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data=urlencode(
                    {
                        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                        "device_code": device_code,
                        "client_id": self.app_id,
                        "client_secret": self.app_secret,
                    }
                ),
                timeout=self.timeout,
            )
            status = response.status_code
            # Only OAuth 400 and legacy 2xx envelopes may keep polling.
            if not 200 <= status < 300 and status != 400:
                raise DeviceFlowError(f"HTTP error {status}")
            payload = self._response_payload(response)
            error = payload.get("error")
            access_token = payload.get("access_token")
            if 200 <= status < 300 and not error and isinstance(access_token, str) and access_token:
                return DeviceToken(
                    access_token=str(payload["access_token"]),
                    refresh_token=payload.get("refresh_token"),
                    expires_in=payload.get("expires_in"),
                    refresh_expires_in=payload.get("refresh_expires_in"),
                    scope=payload.get("scope"),
                )
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                current_interval += 5
                continue
            fallback = "Device flow failed" if 200 <= status < 300 else f"HTTP error {status}"
            raise DeviceFlowError(self._error_detail(payload, fallback, device_code))
        raise DeviceFlowError("Device code expired before authorization completed")

