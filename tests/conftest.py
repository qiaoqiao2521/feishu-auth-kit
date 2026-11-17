"""All product tests use synthetic data; real network access is forbidden."""

import socket

import pytest


@pytest.fixture(autouse=True)
def deny_product_network(monkeypatch, tmp_path):
    def blocked(*args, **kwargs):
        raise AssertionError("Network forbidden in product tests")

    monkeypatch.setenv("FEISHU_AUTH_KIT_TOKEN_STORE", str(tmp_path / "synthetic-tokens.json"))
    monkeypatch.setenv("FEISHU_TRANSPORT_STATE_DIR", str(tmp_path / "synthetic-transport"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "synthetic-data"))
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
