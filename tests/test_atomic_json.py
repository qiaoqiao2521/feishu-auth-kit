from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier, Lock

import pytest

from feishu_auth_kit import _atomic_json
from feishu_auth_kit.orchestration import FilePendingFlowRegistry, PendingAuthFlow
from feishu_auth_kit.runtime_cards import ContinuationState, FileContinuationStore


def test_nested_path_and_unicode_payload_round_trip(tmp_path):
    path = tmp_path / "new" / "nested" / "state.json"
    payload = {"message": "授权完成 ✓", "items": ["继续", 1, None]}

    _atomic_json.atomic_write_json(path, payload)

    content = path.read_text(encoding="utf-8")
    assert json.loads(content) == payload
    assert content.endswith("\n")
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize("failure", ["serialization", "file_fsync", "replace"])
def test_failure_before_replace_preserves_original_and_cleans_up(tmp_path, monkeypatch, failure):
    path = tmp_path / "state.json"
    original = b'{"state": "original"}\n'
    path.write_bytes(original)
    payload = {"state": "replacement"}

    def fail(*args):
        raise OSError("SYNTHETIC_WRITE_FAILURE")

    with monkeypatch.context() as patch:
        if failure == "serialization":
            payload["invalid"] = object()
        elif failure == "file_fsync":
            patch.setattr(_atomic_json.os, "fsync", fail)
        else:
            patch.setattr(_atomic_json.os, "replace", fail)

        error = TypeError if failure == "serialization" else OSError
        with pytest.raises(error):
            _atomic_json.atomic_write_json(path, payload)

    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]
    _atomic_json.atomic_write_json(path, {"state": "retry"})
    assert json.loads(path.read_text(encoding="utf-8")) == {"state": "retry"}


def test_concurrent_writes_use_distinct_complete_temporary_files(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    payloads = [{"writer": index, "body": str(index) * 4096} for index in range(2)]
    rendezvous = Barrier(2)
    staged_lock = Lock()
    staged = []
    real_replace = _atomic_json.os.replace

    def replace_together(source, destination):
        source = Path(source)
        with staged_lock:
            staged.append((source, json.loads(source.read_text(encoding="utf-8"))))
        rendezvous.wait(timeout=5)
        real_replace(source, destination)

    monkeypatch.setattr(_atomic_json.os, "replace", replace_together)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(_atomic_json.atomic_write_json, path, payload) for payload in payloads
        ]
        for future in futures:
            future.result(timeout=10)

    assert len({source for source, _ in staged}) == 2
    assert all(source.parent == path.parent for source, _ in staged)
    assert sorted((payload for _, payload in staged), key=lambda item: item["writer"]) == payloads
    assert json.loads(path.read_text(encoding="utf-8")) in payloads
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("store_kind", ["continuation", "pending_flow"])
def test_store_replace_failure_preserves_reloadable_state(tmp_path, monkeypatch, store_kind):
    path = tmp_path / "state.json"
    if store_kind == "continuation":
        store = FileContinuationStore(path)
        original = ContinuationState("op-SYNTHETIC", "cli-SYNTHETIC", "permission", "pending")
        write = store.save
        key = original.operation_id
    else:
        store = FilePendingFlowRegistry(path)
        original = PendingAuthFlow(
            "flow-SYNTHETIC", "op-SYNTHETIC", "cli-SYNTHETIC", None, "permission_card"
        )
        write = store.upsert
        key = original.flow_key
    write(original)
    before = path.read_bytes()
    updated = replace(original, status="confirmed")

    def fail_replace(*args):
        raise OSError("SYNTHETIC_REPLACE_FAILURE")

    with monkeypatch.context() as patch:
        patch.setattr(_atomic_json.os, "replace", fail_replace)
        with pytest.raises(OSError, match="SYNTHETIC_REPLACE_FAILURE"):
            write(updated)

    assert path.read_bytes() == before
    assert store.load(key) == original
    assert list(tmp_path.iterdir()) == [path]
    write(updated)
    assert store.load(key) == updated
