"""Transport tests must never contact the network."""

import socket

import pytest


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Network forbidden in transport tests")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
