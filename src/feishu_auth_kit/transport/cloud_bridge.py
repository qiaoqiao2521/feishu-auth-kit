"""Owner-only cloud host for feishu-auth-kit. No network or state mutation occurs at import time.

The kit owns registration, tenant authentication and inbound normalization.
Transport follows ControlMesh's Feishu file upload/send/resource protocol.
Never invoke the upstream registration CLI: it prints app_secret.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import mimetypes
import os
import re
import stat
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import quote, urlparse

from cryptography.fernet import Fernet

ROOT = Path(
    os.environ.get("FEISHU_TRANSPORT_STATE_DIR", "~/.local/state/feishu-transport")
).expanduser()
KIT_COMMIT = "77cf53d4233d7d9b2ef38481ca4c30f00ca386a4"
MAX_FILE_BYTES = 30 * 1024 * 1024  # Local conservative limit, not a platform claim.
DOMAINS = {"feishu": "https://open.feishu.cn", "lark": "https://open.larksuite.com"}
SEND_SCOPES = ("im:message:send_as_bot", "im:resource")
EVENT_RECEIVE_SCOPES = ("im:message.p2p_msg:readonly", "im:resource")
HISTORY_RECEIVE_SCOPES = ("im:message:readonly", "im:resource")


class BridgeError(RuntimeError):
    """Messages are fixed local descriptions, never server response bodies."""


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise BridgeError("Unsafe storage directory")
    if stat.S_IMODE(path.stat().st_mode) != 0o700:
        raise BridgeError("Storage directory must have mode 0700")


def _read_private(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise BridgeError("Private file must be a regular file with mode 0600")
        return handle.read()


def _atomic_private(path: Path, payload: bytes) -> None:
    _private_dir(path.parent)
    fd, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class EncryptedStore:
    """Fernet at-rest encryption; master key stays separate from encrypted records.

    This protects accidental ciphertext exposure. It is not a KMS and cannot
    protect against an actor who can read both the key and ciphertext.
    No master key is generated unless initialize() is explicitly invoked.
    """

    def __init__(self, root: Path | None = None):
        root = (
            root
            or Path(
                os.environ.get("FEISHU_TRANSPORT_STATE_DIR", "~/.local/state/feishu-transport")
            ).expanduser()
        )
        self.private = Path(root) / "private"
        self.keys = self.private / "keys"
        self.records = self.private / "records"
        self.key_path = self.keys / "master.key"

    def initialize(self) -> None:
        for folder in (self.private, self.keys, self.records):
            _private_dir(folder)
        if self.key_path.exists():
            _read_private(self.key_path)
            return
        if any(self.records.iterdir()):
            raise BridgeError("Existing records require the original master key")
        fd = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(Fernet.generate_key())
            handle.flush()
            os.fsync(handle.fileno())

    def _cipher(self) -> Fernet:
        return Fernet(_read_private(self.key_path))

    def _path(self, name: str) -> Path:
        if name not in {"registration", "bot", "binding", "tenant_tokens"}:
            raise BridgeError("Unknown record")
        return self.records / (name + ".enc")

    def exists(self, name: str) -> bool:
        return self._path(name).exists()

    def write(self, name: str, value: dict) -> None:
        payload = json.dumps(value, separators=(",", ":")).encode("utf-8")
        _atomic_private(self._path(name), self._cipher().encrypt(payload))

    def read(self, name: str) -> dict:
        value = json.loads(self._cipher().decrypt(_read_private(self._path(name))))
        if not isinstance(value, dict):
            raise BridgeError("Invalid encrypted record")
        return value


class RegistrationBridge:
    """Call only after action-time approval for this new bot registration."""

    def __init__(self, kit_registration, store: EncryptedStore, now=time.time):
        self.client = kit_registration
        self.store = store
        self.now = now

    def begin(self) -> dict:
        if self.store.exists("bot"):
            raise BridgeError("A bot is already configured; refusing replacement")
        if self.store.exists("registration"):
            pending = self.store.read("registration")
            if pending.get("expires_at", 0) > self.now() and pending.get("status") == "waiting":
                raise BridgeError("A registration is already pending")
        self.client.init()
        begin = self.client.begin()
        uri = urlparse(begin.qr_url)
        if (
            uri.scheme != "https"
            or uri.hostname
            not in {
                "accounts.feishu.cn",
                "open.feishu.cn",
                "accounts.larksuite.com",
                "open.larksuite.com",
            }
            or uri.username
            or uri.password
            or uri.port not in {None, 443}
        ):
            raise BridgeError("Registration returned an unexpected verification host")
        self.store.write(
            "registration",
            {
                "device_code": begin.device_code,
                "interval": begin.interval,
                "expires_at": self.now() + begin.expires_in,
                "status": "waiting",
            },
        )
        return {
            "status": "authorization_required",
            "verification_url": begin.qr_url,
            "user_code": begin.user_code,
            "expires_in": begin.expires_in,
        }

    def poll(self, timeout: int = 60) -> dict:
        pending = self.store.read("registration")
        if pending.get("status") != "waiting":
            raise BridgeError("Registration is not pending")
        remaining = int(pending["expires_at"] - self.now())
        if remaining <= 0:
            return {"status": "expired"}
        outcome = self.client.poll(
            pending["device_code"],
            interval=pending["interval"],
            expires_in=remaining,
            poll_timeout=min(timeout, remaining),
        )
        if outcome.status != "success" or outcome.result is None:
            # Deliberately omit remote error text; it can contain sensitive data.
            return {
                "status": outcome.status
                if outcome.status in {"access_denied", "expired", "timeout", "error"}
                else "error"
            }
        result = outcome.result
        if result.domain not in DOMAINS:
            raise BridgeError("Unsupported bot domain")
        if self.store.exists("bot"):
            raise BridgeError("A bot is already configured; refusing replacement")
        self.store.write(
            "bot",
            {
                "app_id": result.app_id,
                "app_secret": result.app_secret,
                "brand": result.domain,
                "owner_open_id": result.open_id,
                "owner_source": "registration" if result.open_id else None,
                "kit_commit": KIT_COMMIT,
            },
        )
        self.store.write("registration", {"status": "completed"})
        return {
            "status": "configured" if result.open_id else "owner_verification_required",
            "credentials_saved": True,
            "owner_bound": bool(result.open_id),
        }


def safe_filename(value: str) -> str:
    name = re.split(r"[/\\]", str(value))[-1]
    name = re.sub(r"[\x00-\x1f\x7f]", "_", name).strip(" .")[:160]
    return name or "attachment.bin"


class OwnerOnlyTransport:
    """No arbitrary destination argument. The single owner is pinned per new app.

    `receive_event` requires an authenticated Feishu event delivered by a trusted
    SDK connection. It is not a public webhook and must not accept raw web input.
    No inbound text is executed. No listener or daemon is started here.
    """

    def __init__(
        self,
        *,
        auth_client,
        session,
        app_id: str,
        owner_open_id: str,
        brand: str,
        parse_context,
        root: Path = ROOT,
        token_store=None,
    ):
        if brand not in DOMAINS or not app_id or not owner_open_id:
            raise BridgeError("A verified bot and owner are required")
        if auth_client.app_id != app_id:
            raise BridgeError("Authentication client belongs to another app")
        self.auth = auth_client
        self.session = session
        self.app_id = app_id
        self.owner = owner_open_id
        self.base = DOMAINS[brand]
        self.parse_context = parse_context
        self.root = Path(root)
        self.outgoing = self.root / "files" / "outgoing"
        self.incoming = self.root / "files" / "incoming"
        self._cached_token = None
        self._token_expires_at = 0.0
        self.token_store = token_store
        self.last_auth_cache = None

    def prepare_file_dirs(self) -> None:
        for folder in (self.root / "files", self.outgoing, self.incoming):
            _private_dir(folder)

    def _headers(self, *, rejected_token=None) -> dict:
        # Expire the host cache because this kit version does not expire its own.
        now = time.monotonic()
        if (
            rejected_token is None
            and self._cached_token is not None
            and now < self._token_expires_at
        ):
            self.last_auth_cache = "memory"
            return {"Authorization": "Bearer " + self._cached_token}
        if self.token_store is not None:
            return self._persistent_headers(rejected_token)
        if (
            self._cached_token is None
            or now >= self._token_expires_at
            or rejected_token is not None
        ):
            token = self.auth.get_tenant_access_token(force_refresh=True)
            self._cached_token = token.token
            self._token_expires_at = now + max(
                0,
                int(
                    getattr(token, "expire", None)
                    if getattr(token, "expire", None) is not None
                    else 300
                )
                - 60,
            )
            self.last_auth_cache = "refreshed"
        return {"Authorization": "Bearer " + self._cached_token}

    def _persistent_headers(self, rejected_token=None) -> dict:
        """Encrypted app/domain-isolated cache with single-flight refresh.

        A rejected token is only a cache-invalidation signal; this method never
        replays the operation which used it. Wall-clock rollback invalidates the
        persisted entry. In-process expiration uses monotonic time.
        """
        store = self.token_store
        cache_key = hashlib.sha256((self.base + "\0" + self.app_id).encode()).hexdigest()
        fd = os.open(
            store.private / "tenant-token.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            cache = store.read("tenant_tokens") if store.exists("tenant_tokens") else {}
            entry = cache.get(cache_key) or {}
            now = time.time()
            valid = (
                entry.get("app_id") == self.app_id
                and entry.get("base") == self.base
                and isinstance(entry.get("token"), str)
                and entry["token"]
                and entry.get("issued_at", now + 1) <= now < entry.get("expires_at", 0)
                and entry["token"] != rejected_token
            )
            if valid:
                self.last_auth_cache = "encrypted_store"
            else:
                token = self.auth.get_tenant_access_token(force_refresh=True)
                lifetime = max(
                    0,
                    min(
                        7200,
                        int(
                            getattr(token, "expire", None)
                            if getattr(token, "expire", None) is not None
                            else 300
                        ),
                    )
                    - 60,
                )
                entry = {
                    "app_id": self.app_id,
                    "base": self.base,
                    "token": token.token,
                    "issued_at": now,
                    "expires_at": now + lifetime,
                }
                cache[cache_key] = entry
                store.write("tenant_tokens", cache)
                self.last_auth_cache = "refreshed"
            self._cached_token = entry["token"]
            self._token_expires_at = time.monotonic() + max(0, entry["expires_at"] - time.time())
            return {"Authorization": "Bearer " + self._cached_token}
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    @staticmethod
    def _json(response) -> dict:
        if response.status_code < 200 or response.status_code >= 300:
            raise BridgeError("Feishu request failed; inspect status without response body")
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("code", 0) not in {0, None}:
            raise BridgeError("Feishu API rejected the request")
        data = payload.get("data", {})
        if not isinstance(data, dict):
            raise BridgeError("Unexpected Feishu API response")
        return data

    def send_file(self, path: Path) -> dict:
        path = Path(path)
        resolved = path.resolve(strict=True)
        outgoing = self.outgoing.resolve(strict=True)
        if not resolved.is_relative_to(outgoing) or path.is_symlink() or not resolved.is_file():
            raise BridgeError("Upload is outside the outgoing allowlist")
        if resolved.stat().st_size > MAX_FILE_BYTES:
            raise BridgeError("File exceeds the configured local size limit")
        headers = self._headers()
        name = safe_filename(resolved.name)
        file_type = {
            ".pdf": "pdf",
            ".doc": "doc",
            ".docx": "doc",
            ".xls": "xls",
            ".xlsx": "xls",
            ".csv": "xls",
            ".ppt": "ppt",
            ".pptx": "ppt",
        }.get(resolved.suffix.lower(), "stream")
        fd = os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as handle:
            response = self.session.request(
                "POST",
                self.base + "/open-apis/im/v1/files",
                headers=headers,
                data={"file_type": file_type, "file_name": name},
                files={
                    "file": (
                        name,
                        handle,
                        mimetypes.guess_type(name)[0] or "application/octet-stream",
                    )
                },
                timeout=60,
                allow_redirects=False,
            )
            file_key = self._json(response).get("file_key")
        if not file_key:
            raise BridgeError("Upload did not return a file key")
        response = self.session.request(
            "POST",
            self.base + "/open-apis/im/v1/messages",
            headers=headers,
            params={"receive_id_type": "open_id"},
            json={
                "receive_id": self.owner,
                "msg_type": "file",
                "content": json.dumps({"file_key": file_key}),
                "uuid": str(uuid.uuid4()),
            },
            timeout=30,
            allow_redirects=False,
        )
        sent = self._json(response)
        if not sent.get("message_id"):
            raise BridgeError("Send did not return a message id; do not blindly retry")
        return {
            "status": "sent",
            "message_id": sent["message_id"],
            "chat_id": sent.get("chat_id"),
            "file_name": name,
        }

    def receive_event(self, payload: dict) -> dict:
        context = self.parse_context(payload)
        if (
            context.app_id != self.app_id
            or context.chat_type != "p2p"
            or context.sender_open_id != self.owner
            or not context.message_id
        ):
            raise BridgeError("Inbound message is not this app owner private chat")
        if context.message_type not in {"file", "image"}:
            return {"status": "ignored_non_file"}
        content = (payload.get("event") or {}).get("message", {}).get("content")
        if isinstance(content, str):
            content = json.loads(content)
        key_name = "image_key" if context.message_type == "image" else "file_key"
        if not isinstance(content, dict) or not content.get(key_name):
            raise BridgeError("File message has no resource key")
        return self._download(
            context.message_id,
            content[key_name],
            content.get("file_name")
            or ("image.jpg" if key_name == "image_key" else "attachment.bin"),
            resource_type="image" if key_name == "image_key" else "file",
        )

    def _download(
        self, message_id: str, file_key: str, file_name: str, resource_type: str = "file"
    ) -> dict:
        if resource_type not in {"file", "image"}:
            raise BridgeError("Unsupported resource type")
        self.prepare_file_dirs()
        identifier = hashlib.sha256((message_id + ":" + file_key).encode()).hexdigest()[:20]
        destination = self.incoming / (identifier + "-" + safe_filename(file_name))
        if destination.exists():
            if destination.is_symlink() or not destination.is_file():
                raise BridgeError("Unsafe existing download target")
            return {"status": "already_present", "path": str(destination)}
        url = self.base + "/open-apis/im/v1/messages/" + quote(message_id, safe="")
        url += "/resources/" + quote(file_key, safe="")
        response = self.session.request(
            "GET",
            url,
            headers=self._headers(),
            params={"type": resource_type},
            stream=True,
            timeout=60,
            allow_redirects=False,
        )
        fd, temporary = tempfile.mkstemp(prefix=".download-", dir=self.incoming)
        total = 0
        try:
            with os.fdopen(fd, "wb") as handle:
                if response.status_code >= 400 or response.status_code < 200:
                    raise BridgeError("Feishu file download failed")
                if response.status_code >= 300:
                    raise BridgeError("Unexpected download redirect")
                for chunk in response.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > MAX_FILE_BYTES:
                        raise BridgeError("Download exceeds the configured local size limit")
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            os.link(temporary, destination)  # No overwrite on duplicate/race.
            return {"status": "downloaded", "path": str(destination), "bytes": total}
        finally:
            response.close()
            if os.path.exists(temporary):
                os.unlink(temporary)


def create_registration_client():
    """Lazy upstream import; offline tests never import or execute repository code."""
    from feishu_auth_kit import AppRegistrationClient

    return AppRegistrationClient(brand="feishu")


def create_owner_transport(
    store: EncryptedStore, root: Path | None = None, *, auth_provider=None, session=None
) -> OwnerOnlyTransport:
    import requests

    from feishu_auth_kit import FeishuAuthClient
    from feishu_auth_kit.message_context import parse_feishu_message_context

    bot = store.read("bot")
    from .binding import owner_verified

    if not owner_verified(bot):
        raise BridgeError("Verified app owner binding is required")
    client = auth_provider or FeishuAuthClient(bot["app_id"], bot["app_secret"], brand=bot["brand"])
    if session is None:
        session = requests.Session()
        session.trust_env = False
    return OwnerOnlyTransport(
        auth_client=client,
        session=session,
        app_id=bot["app_id"],
        owner_open_id=bot["owner_open_id"],
        brand=bot["brand"],
        parse_context=parse_feishu_message_context,
        root=root or store.private.parent,
        token_store=None if auth_provider else store,
    )
