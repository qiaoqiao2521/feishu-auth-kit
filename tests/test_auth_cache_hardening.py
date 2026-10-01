import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

from feishu_auth_kit.client import FeishuAuthClient


def client_with(fetch):
    client = FeishuAuthClient("cli_SYNTHETIC", "SYNTHETIC_SECRET")
    client._request_json = fetch
    return client


def test_token_expiry_and_force_refresh(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("feishu_auth_kit.client.time.monotonic", lambda: clock[0])
    fetch = Mock(return_value={"tenant_access_token": "SYNTHETIC_TOKEN", "expire": 100})
    client = client_with(fetch)
    token = client.get_tenant_access_token()
    clock[0] = 180
    assert client.get_tenant_access_token() is token
    clock[0] = 191
    assert client.get_tenant_access_token() is not token
    client.get_tenant_access_token(force_refresh=True)
    assert fetch.call_count == 3


@pytest.mark.parametrize("ttl", [0, -1, "invalid", float("inf"), float("nan")])
def test_zero_or_invalid_ttl_not_cached(ttl):
    fetch = Mock(return_value={"tenant_access_token": "SYNTHETIC_TOKEN", "expire": ttl})
    client = client_with(fetch)
    client.get_tenant_access_token()
    client.get_tenant_access_token()
    assert fetch.call_count == 2


def test_refresh_failure_does_not_freeze_cache(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("feishu_auth_kit.client.time.monotonic", lambda: clock[0])
    fetch = Mock(
        side_effect=[
            {"tenant_access_token": "SYNTHETIC_TOKEN", "expire": 100},
            RuntimeError("SYNTHETIC_FAILURE"),
            {"tenant_access_token": "SYNTHETIC_NEW", "expire": 100},
        ]
    )
    client = client_with(fetch)
    client.get_tenant_access_token()
    clock[0] = 200
    with pytest.raises(RuntimeError):
        client.get_tenant_access_token()
    assert client.get_tenant_access_token().token == "SYNTHETIC_NEW"


def test_single_flight_threads():
    calls = []

    def fetch(*args, **kwargs):
        calls.append(1)
        time.sleep(0.03)
        return {"tenant_access_token": "SYNTHETIC_TOKEN", "expire": 7200}

    client = client_with(fetch)
    gate = threading.Barrier(4)

    def get(_):
        gate.wait(2)
        return client.get_tenant_access_token()

    with ThreadPoolExecutor(4) as pool:
        tokens = list(pool.map(get, range(4)))
    assert len(calls) == 1 and all(token is tokens[0] for token in tokens)
