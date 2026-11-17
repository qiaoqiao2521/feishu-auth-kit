"""The encrypted tenant cache refresh lock must span processes."""

import multiprocessing
from types import SimpleNamespace as NS

from feishu_auth_kit.transport.cloud_bridge import EncryptedStore, OwnerOnlyTransport


def refresh_worker(root, calls, gate):
    store = EncryptedStore(root)

    def token(**kwargs):
        with calls.get_lock():
            calls.value += 1
        return NS(token="SYNTHETIC_PROCESS_TOKEN", expire=7200)

    auth = NS(app_id="cli_SYNTHETICPROCESS", get_tenant_access_token=token)
    transport = OwnerOnlyTransport(
        auth_client=auth,
        session=None,
        app_id=auth.app_id,
        owner_open_id="ou_SYNTHETICOWNER",
        brand="feishu",
        parse_context=None,
        root=root,
        token_store=store,
    )
    gate.wait(5)
    assert transport._headers()["Authorization"] == "Bearer SYNTHETIC_PROCESS_TOKEN"


def test_process_single_flight(tmp_path):
    store = EncryptedStore(tmp_path)
    store.initialize()
    context = multiprocessing.get_context("fork")
    calls = context.Value("i", 0)
    gate = context.Event()
    workers = [
        context.Process(target=refresh_worker, args=(tmp_path, calls, gate)) for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    gate.set()
    for worker in workers:
        worker.join(5)
        assert worker.exitcode == 0
    assert calls.value == 1
