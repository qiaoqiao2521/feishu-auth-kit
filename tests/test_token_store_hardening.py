import json
import multiprocessing
import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from feishu_auth_kit.token_store import FileTokenStore, StoredUserToken


def record(index):
    return StoredUserToken("cli_SYNTHETIC", "ou_SYNTHETIC" + str(index), "SYNTHETIC_TOKEN")


def writer(path, index):
    for offset in range(5):
        FileTokenStore(path).save(record(index * 5 + offset))


def test_permissions_under_permissive_umask(tmp_path):
    path = tmp_path / "new" / "tokens.json"
    before = os.umask(0)
    try:
        FileTokenStore(path).save(record(0))
    finally:
        os.umask(before)
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("content", ["{broken", "[]", '{"tokens": []}', '{"tokens":{"bad":{}}}'])
def test_corruption_preserved(tmp_path, content):
    path = tmp_path / "tokens.json"
    path.write_text(content)
    path.chmod(0o600)
    store = FileTokenStore(path)
    with pytest.raises(ValueError):
        store.save(record(0))
    with pytest.raises(ValueError):
        store.remove("cli_SYNTHETIC", "ou_SYNTHETIC0")
    assert path.read_text() == content
    assert not Path(str(path) + ".lock").exists()


def test_legacy_permissions_not_silently_changed(tmp_path):
    path = tmp_path / "tokens.json"
    path.write_text("{}")
    path.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        FileTokenStore(path).save(record(0))
    assert stat.S_IMODE(path.stat().st_mode) == 0o644 and path.read_text() == "{}"


def test_atomic_write_fault_preserves_original_and_releases_lock(tmp_path):
    path = tmp_path / "tokens.json"
    store = FileTokenStore(path)
    store.save(record(0))
    before = path.read_bytes()
    with patch("feishu_auth_kit.token_store.os.replace", side_effect=OSError("SYNTHETIC_FAULT")):
        with pytest.raises(OSError):
            store.save(record(1))
    assert path.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["tokens.json"]
    store.save(record(1))
    assert store.load("cli_SYNTHETIC", "ou_SYNTHETIC1") is not None


def test_processes_preserve_all_records(tmp_path):
    path = tmp_path / "tokens.json"
    context = multiprocessing.get_context("fork")
    workers = [context.Process(target=writer, args=(path, index)) for index in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(5)
        assert worker.exitcode == 0
    assert len(json.loads(path.read_text())["tokens"]) == 20


def test_abandoned_lock_fails_closed(tmp_path):
    path = tmp_path / "tokens.json"
    lock = Path(str(path) + ".lock")
    lock.mkdir(mode=0o700)
    store = FileTokenStore(path)
    store._lock_timeout = 0.02
    with pytest.raises(TimeoutError):
        store.save(record(0))
    assert lock.exists() and not path.exists()


def test_symlink_rejected(tmp_path):
    target = tmp_path / "original"
    target.write_text("{}")
    target.chmod(0o600)
    path = tmp_path / "tokens.json"
    path.symlink_to(target)
    with pytest.raises(ValueError):
        FileTokenStore(path).save(record(0))
    assert target.read_text() == "{}"


def test_lock_released_before_inspection(tmp_path, monkeypatch):
    store = FileTokenStore(tmp_path / "tokens.json")
    lock = store.path.with_name(store.path.name + ".lock")
    lock.mkdir()
    original = Path.lstat
    released = False

    def racing_lstat(path):
        nonlocal released
        if path == lock and not released:
            released = True
            lock.rmdir()
            raise FileNotFoundError("synthetic released lock")
        return original(path)

    monkeypatch.setattr(Path, "lstat", racing_lstat)
    store.save(StoredUserToken("cli_SYNTHETIC", "ou_SYNTHETIC", "SYNTHETIC_TOKEN"))
    assert store.load("cli_SYNTHETIC", "ou_SYNTHETIC") is not None
