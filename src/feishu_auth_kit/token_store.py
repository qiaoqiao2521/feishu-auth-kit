from __future__ import annotations

import json
import os
import stat
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .models import DeviceToken


def default_token_store_path() -> Path:
    configured = os.getenv("FEISHU_AUTH_KIT_TOKEN_STORE")
    if configured:
        return Path(configured).expanduser()
    data_home = os.getenv("XDG_DATA_HOME")
    if data_home:
        base = Path(data_home).expanduser()
    else:
        base = Path.home() / ".local" / "share"
    return base / "feishu-auth-kit" / "user_tokens.json"


@dataclass(frozen=True)
class StoredUserToken:
    app_id: str
    user_open_id: str
    access_token: str
    refresh_token: str | None = None
    expires_at: int | None = None
    refresh_expires_at: int | None = None
    scope: str | None = None

    @property
    def storage_key(self) -> str:
        return FileTokenStore.storage_key(self.app_id, self.user_open_id)


@dataclass(frozen=True)
class TokenStatus:
    app_id: str
    user_open_id: str
    exists: bool
    storage_path: Path
    scope: str | None = None
    expires_at: int | None = None
    refresh_expires_at: int | None = None


class FileTokenStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path).expanduser() if path else default_token_store_path()

    @staticmethod
    def storage_key(app_id: str, user_open_id: str) -> str:
        return f"{app_id}:{user_open_id}"

    _lock_timeout = 2.0

    def _prepare_parent(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.parent.is_symlink() or self.path.parent.stat().st_mode & 0o022:
            raise ValueError("Token storage directory is unsafe")

    @contextmanager
    def _transaction(self):
        self._prepare_parent()
        lock = self.path.with_name(self.path.name + ".lock")
        deadline = time.monotonic() + self._lock_timeout
        while True:
            try:
                lock.mkdir(mode=0o700)
                break
            except FileExistsError:
                try:
                    lock_info = lock.lstat()
                except FileNotFoundError:
                    # The owner released the lock after our mkdir saw EEXIST.
                    continue
                if not stat.S_ISDIR(lock_info.st_mode):
                    raise ValueError("Unsafe token-store lock") from None
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "Token store is locked; fence the writer before recovery"
                    ) from None
                time.sleep(0.02)
        try:
            yield
        finally:
            lock.rmdir()

    def _read_all(self) -> dict[str, dict[str, Any]]:
        if self.path.is_symlink():
            raise ValueError("Token file must not be a symlink")
        try:
            fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return {}
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
                raise ValueError("Token file requires mode 0600; explicit migration is needed")
            try:
                payload = json.load(handle)
            except (ValueError, UnicodeError):
                raise ValueError("Corrupt token store; original data was preserved") from None
        if not isinstance(payload, dict):
            raise ValueError("Corrupt token store; original data was preserved")
        tokens = payload.get("tokens", payload)
        if not isinstance(tokens, dict):
            raise ValueError("Corrupt token store; original data was preserved")
        for item in tokens.values():
            if not isinstance(item, dict) or any(
                not isinstance(item.get(key), str)
                for key in ("app_id", "user_open_id", "access_token")
            ):
                raise ValueError("Corrupt token record; original data was preserved")
        return tokens

    def _write_all(self, tokens: dict[str, dict[str, Any]]) -> None:
        self._prepare_parent()
        fd, name = tempfile.mkstemp(prefix="." + self.path.name + "-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"tokens": tokens}, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self.path)
            directory_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def load(self, app_id: str, user_open_id: str) -> StoredUserToken | None:
        item = self._read_all().get(self.storage_key(app_id, user_open_id))
        if not item:
            return None
        return StoredUserToken(
            app_id=str(item["app_id"]),
            user_open_id=str(item["user_open_id"]),
            access_token=str(item["access_token"]),
            refresh_token=item.get("refresh_token"),
            expires_at=item.get("expires_at"),
            refresh_expires_at=item.get("refresh_expires_at"),
            scope=item.get("scope"),
        )

    def save(self, token: StoredUserToken) -> StoredUserToken:
        with self._transaction():
            tokens = self._read_all()
            tokens[token.storage_key] = asdict(token)
            self._write_all(tokens)
        return token

    def save_device_token(
        self,
        app_id: str,
        user_open_id: str,
        token: DeviceToken,
        *,
        now: int | None = None,
    ) -> StoredUserToken:
        current = int(time.time()) if now is None else now
        stored = StoredUserToken(
            app_id=app_id,
            user_open_id=user_open_id,
            access_token=token.access_token,
            refresh_token=token.refresh_token,
            expires_at=current + token.expires_in if token.expires_in else None,
            refresh_expires_at=current + token.refresh_expires_in
            if token.refresh_expires_in
            else None,
            scope=token.scope,
        )
        return self.save(stored)

    def remove(self, app_id: str, user_open_id: str) -> bool:
        with self._transaction():
            tokens = self._read_all()
            removed = tokens.pop(self.storage_key(app_id, user_open_id), None)
            if removed is None:
                return False
            self._write_all(tokens)
            return True

    def status(self, app_id: str, user_open_id: str) -> TokenStatus:
        current = self.load(app_id, user_open_id)
        if not current:
            return TokenStatus(
                app_id=app_id,
                user_open_id=user_open_id,
                exists=False,
                storage_path=self.path,
            )
        return TokenStatus(
            app_id=app_id,
            user_open_id=user_open_id,
            exists=True,
            storage_path=self.path,
            scope=current.scope,
            expires_at=current.expires_at,
            refresh_expires_at=current.refresh_expires_at,
        )
