#!/usr/bin/env python3
"""Watch channel upstreams and maintain rolling/cycle sync digests.

Features:
- Per-upstream / per-cycle updatable sync digest issue (avoids issue spam)
- Persistent cursor (file-based and issue-body-embedded fallback)
- Robust multi-page commit fetching (prevents pagination omissions)
- Refined signal classification and clear escalation rules (P1/P2/P3/Noise)
- GitHub API retry with exponential backoff and rate-limit handling
- Concurrency-safe, idempotent dry-run and live updates
- Offline corpus evaluation support
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import asdict, dataclass, field
from enum import Enum
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

API_ROOT = "https://api.github.com"
DEFAULT_OWNER = "qiaoqiao2521"
DEFAULT_REPO = "feishu-auth-kit"
LABEL_NAME = "upstream-watch"
LABEL_COLOR = "0E8A16"
LABEL_DESCRIPTION = "Automated upstream monitoring issue"
USER_AGENT = "feishu-auth-kit-upstream-watch"
DEFAULT_STATE_FILE = ".upstream-watch/state.json"
DEFAULT_CONFIG_FILE = Path(__file__).resolve().parents[1] / "upstream" / "targets.json"

STATE_MARKER_REGEX = re.compile(
    r"<!--\s*upstream-watch-state:\s*(\{.*?\})\s*-->",
    re.DOTALL,
)

LOW_SIGNAL_KINDS = {"docs", "style"}
FORMAT_HINTS = ("format", "fmt", "prettier", "lint", "whitespace")
MERGE_NOISE_PREFIXES = (
    "merge branch 'main' into",
    'merge remote-tracking branch "origin/main" into',
    "merge remote-tracking branch 'origin/main' into",
    "merge branch 'master' into",
)

DOC_EXTENSIONS = (".md", ".txt", ".sha256", ".rst")
IGNORE_PATHS = (
    "docs/",
    ".github/",
    ".vscode/",
    ".idea/",
    ".gitignore",
    "LICENSE",
)
UNRELATED_EXTENSIONS = (
    "extensions/discord/",
    "extensions/mattermost/",
    "extensions/matrix/",
    "extensions/qqbot/",
    "extensions/telegram/",
    "extensions/whatsapp/",
    "apps/android/",
    "apps/ios/",
    "apps/desktop/",
)


class EscalationLevel(str, Enum):
    P1_CRITICAL = "P1_CRITICAL"
    P2_NOTICE = "P2_NOTICE"
    P3_ROUTINE = "P3_ROUTINE"
    FILTERED = "FILTERED"


@dataclass(frozen=True)
class WatchRoute:
    platform: str
    contracts: tuple[str, ...]
    paths: tuple[str, ...] = ()
    docs: tuple[str, ...] = ()


@dataclass(frozen=True)
class WatchTarget:
    key: str
    repo: str
    branch: str
    mode: str  # Legacy "plugin"/"core" classification, or configured "selective" routes.
    routes: tuple[WatchRoute, ...] = ()


TARGETS: tuple[WatchTarget, ...] = (
    WatchTarget(
        key="openclaw-lark",
        repo="larksuite/openclaw-lark",
        branch="main",
        mode="plugin",
    ),
    WatchTarget(
        key="openclaw-core",
        repo="openclaw/openclaw",
        branch="main",
        mode="core",
    ),
)


def load_watch_targets(filepath: str | Path) -> tuple[WatchTarget, ...]:
    """Load versioned selectors; glob paths are relative to the upstream repository root."""
    with open(filepath, encoding="utf-8") as config_file:
        config = json.load(config_file)
    if not isinstance(config, dict) or set(config) != {"version", "targets"}:
        raise ValueError("Config must contain only version and targets")
    if type(config["version"]) is not int or config["version"] != 1:
        raise ValueError("Unsupported upstream config version (expected 1)")
    if not isinstance(config["targets"], list) or not config["targets"]:
        raise ValueError("Config targets must be a nonempty list")

    def identifier(value: Any, label: str) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9-]*", value):
            raise ValueError(f"{label} must be a lowercase identifier")
        return value

    def selectors(value: Any, label: str) -> tuple[str, ...]:
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError(f"{label} must be a list of repository-relative globs")
        for item in value:
            if (
                not item
                or item.startswith("/")
                or "\\" in item
                or any(part in {"", ".", ".."} for part in item.split("/"))
            ):
                raise ValueError(f"{label} contains an invalid repository-relative glob: {item!r}")
        return tuple(value)

    targets: list[WatchTarget] = []
    keys: set[str] = set()
    for entry in config["targets"]:
        if not isinstance(entry, dict) or set(entry) != {"key", "repo", "branch", "mode", "routes"}:
            raise ValueError("Each target requires key, repo, branch, mode and routes")
        key = identifier(entry["key"], "Target key")
        if key in keys:
            raise ValueError(f"Duplicate target key: {key}")
        keys.add(key)
        repo = entry["repo"]
        if not isinstance(repo, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", repo
        ):
            raise ValueError(f"Invalid repository for {key}: expected owner/repository")
        branch = entry["branch"]
        if (
            not isinstance(branch, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]*", branch)
            or ".." in branch
        ):
            raise ValueError(f"Invalid branch for {key}")
        if entry["mode"] not in ("plugin", "core", "selective"):
            raise ValueError(f"Invalid mode for {key}")
        if not isinstance(entry["routes"], list) or not entry["routes"]:
            raise ValueError(f"Target {key} requires at least one route")
        routes: list[WatchRoute] = []
        for route in entry["routes"]:
            if (
                not isinstance(route, dict)
                or not {"platform", "contracts"} <= set(route)
                or set(route) - {"platform", "contracts", "paths", "docs"}
            ):
                raise ValueError(f"Invalid route fields for {key}")
            platform = identifier(route["platform"], f"Platform in {key}")
            contracts = route["contracts"]
            if not isinstance(contracts, list) or not contracts:
                raise ValueError(f"Route contracts for {key}/{platform} must be a nonempty list")
            contract_ids = tuple(identifier(item, "Contract") for item in contracts)
            paths = selectors(route.get("paths", []), f"Paths in {key}/{platform}")
            docs = selectors(route.get("docs", []), f"Docs in {key}/{platform}")
            if not paths and not docs:
                raise ValueError(f"Route {key}/{platform} needs paths or explicit docs selectors")
            routes.append(WatchRoute(platform, contract_ids, paths, docs))
        targets.append(WatchTarget(key, repo, branch, entry["mode"], tuple(routes)))
    return tuple(targets)


@dataclass
class TargetState:
    last_sha: str = ""
    last_date: str = ""
    seen_shas: list[str] = field(default_factory=list)
    last_sync_at: str = ""
    active_issue_number: int | None = None
    active_cycle: str = ""

    def record_commit(self, sha: str, commit_date: str) -> None:
        self.last_sha = sha
        self.last_date = commit_date
        if sha not in self.seen_shas:
            self.seen_shas.append(sha)
            if len(self.seen_shas) > 300:
                self.seen_shas = self.seen_shas[-300:]


@dataclass
class UpstreamStateStore:
    version: int = 1
    targets: dict[str, TargetState] = field(default_factory=dict)

    def get_target_state(self, key: str) -> TargetState:
        if key not in self.targets:
            self.targets[key] = TargetState()
        return self.targets[key]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UpstreamStateStore:
        store = cls(version=data.get("version", 1))
        for key, t_data in data.get("targets", {}).items():
            store.targets[key] = TargetState(
                last_sha=t_data.get("last_sha", ""),
                last_date=t_data.get("last_date", ""),
                seen_shas=list(t_data.get("seen_shas", [])),
                last_sync_at=t_data.get("last_sync_at", ""),
                active_issue_number=t_data.get("active_issue_number"),
                active_cycle=t_data.get("active_cycle", ""),
            )
        return store

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "targets": {k: asdict(v) for k, v in self.targets.items()},
        }

    def save_to_file(self, filepath: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
        tmp_path = f"{filepath}.tmp.{os.getpid()}"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, filepath)

    @classmethod
    def load_from_file(cls, filepath: str) -> UpstreamStateStore:
        if not os.path.exists(filepath):
            return cls()
        try:
            with open(filepath, encoding="utf-8") as f:
                data = json.load(f)
            return cls.from_dict(data)
        except Exception as exc:
            print(f"Warning: Failed to load state file {filepath}: {exc}", file=sys.stderr)
            return cls()

    @classmethod
    def extract_from_text(cls, text: str) -> dict[str, Any] | None:
        m = STATE_MARKER_REGEX.search(text)
        if not m:
            return None
        try:
            return json.loads(m.group(1))
        except Exception:
            return None


@dataclass
class CandidateCommit:
    target_key: str
    upstream_repo: str
    sha: str
    commit_date: str
    message: str
    filenames: list[str]
    reasons: list[str]
    escalation: EscalationLevel
    html_url: str
    author: str = ""
    platforms: list[str] = field(default_factory=list)
    contracts: list[str] = field(default_factory=list)

    @property
    def short_sha(self) -> str:
        return self.sha[:7]


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Watch configured channel upstreams and maintain rolling/cycle sync digests.",
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_FILE),
        help="Versioned target/route JSON (default: upstream/targets.json beside this repository).",
    )
    parser.add_argument("--owner", help="Issue target owner (default: current workflow owner).")
    parser.add_argument("--repo", help="Issue target repository (default: current workflow repo).")
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=14,
        help="Only consider commits newer than this many days if no cursor exists.",
    )
    parser.add_argument(
        "--per-target-limit",
        type=int,
        default=100,
        help="Maximum commits to inspect per target; oldest pending commits are processed first.",
    )
    parser.add_argument(
        "--cycle",
        choices=["weekly", "monthly", "rolling"],
        default="weekly",
        help="Digest cycle window (default: weekly).",
    )
    parser.add_argument(
        "--state-file",
        default=DEFAULT_STATE_FILE,
        help="Path to persistent state JSON file.",
    )
    parser.add_argument(
        "--target",
        help="Only watch a specific target key (e.g. openclaw-lark or openclaw-core).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print digest changes without creating or modifying GitHub issues.",
    )
    parser.add_argument(
        "--offline-corpus",
        help="Path to offline JSON issues corpus for offline evaluation / testing.",
    )
    parsed = parser.parse_args(args)
    if parsed.per_target_limit < 1:
        parser.error("--per-target-limit must be at least 1")
    if parsed.owner is None or parsed.repo is None:
        repository = os.environ.get("GITHUB_REPOSITORY") or f"{DEFAULT_OWNER}/{DEFAULT_REPO}"
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", repository):
            parser.error("GITHUB_REPOSITORY must be an owner/repository pair")
        owner, repo = repository.split("/")
        if parsed.owner is None:
            parsed.owner = owner
        if parsed.repo is None:
            parsed.repo = repo
    return parsed


class GitHubSession:
    """GitHub API client with retry, backoff, and rate limit handling."""

    def __init__(
        self,
        *,
        token: str,
        max_retries: int = 3,
        initial_backoff: float = 1.0,
    ) -> None:
        self._token = token
        self._max_retries = max_retries
        self._initial_backoff = initial_backoff

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{API_ROOT}/{path.lstrip('/')}"
        if params:
            query = urllib.parse.urlencode(params, doseq=True)
            url = f"{url}?{query}"

        payload = None
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        retries = 0
        backoff = self._initial_backoff

        while True:
            req = urllib.request.Request(url, data=payload, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=30) as response:
                    raw = response.read().decode("utf-8")
                    rate_remaining = response.headers.get("X-RateLimit-Remaining")
                    if rate_remaining is not None and int(rate_remaining) < 10:
                        print(
                            f"Warning: GitHub API rate limit low: {rate_remaining} remaining",
                            file=sys.stderr,
                        )
                    if not raw:
                        return None
                    return json.loads(raw)
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                status = exc.code
                rate_remaining = exc.headers.get("X-RateLimit-Remaining")
                retry_after = exc.headers.get("Retry-After")

                is_transient = status in (429, 500, 502, 503, 504)
                if status == 403 and rate_remaining == "0":
                    is_transient = True

                if is_transient and retries < self._max_retries:
                    wait_sec = float(retry_after) if retry_after else backoff
                    print(
                        f"GitHub API {method} {path} failed with {status}, "
                        f"retrying in {wait_sec:.1f}s "
                        f"(attempt {retries + 1}/{self._max_retries})...",
                        file=sys.stderr,
                    )
                    time.sleep(wait_sec)
                    retries += 1
                    backoff *= 2
                    continue

                raise RuntimeError(
                    f"GitHub API {method} {url} failed ({status}): {detail}"
                ) from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                if retries < self._max_retries:
                    print(
                        f"Network error on {method} {path}: {exc}, retrying in {backoff:.1f}s...",
                        file=sys.stderr,
                    )
                    time.sleep(backoff)
                    retries += 1
                    backoff *= 2
                    continue
                raise RuntimeError(f"Network failure calling {method} {url}: {exc}") from exc


def get_current_cycle(cycle_type: str, now: dt.datetime | None = None) -> str:
    """Compute cycle string (e.g. 2026-W37 or 2026-09 or rolling)."""
    if now is None:
        now = dt.datetime.now(dt.timezone.utc)
    if cycle_type == "weekly":
        year, week, _ = now.isocalendar()
        return f"{year}-W{week:02d}"
    elif cycle_type == "monthly":
        return now.strftime("%Y-%m")
    return "rolling"


def is_low_signal_commit(message: str) -> bool:
    prefix = message.split(":", 1)[0].strip().lower()
    kind = prefix.split("(", 1)[0].strip()
    if kind in LOW_SIGNAL_KINDS:
        return True
    if kind == "chore" and any(h in message.lower() for h in FORMAT_HINTS):
        return True
    if message.lower().startswith(MERGE_NOISE_PREFIXES):
        return True
    return False


ISOLATED_CORE_SCOPES = (
    "gateway",
    "memory",
    "workboard",
    "media",
    "desktop",
    "discord",
    "telegram",
    "qqbot",
    "matrix",
    "mattermost",
    "whatsapp",
    "slack",
    "android",
    "ios",
    "windows",
    "ui",
    "docs",
    "style",
    "ci",
    "build",
    "test",
    "chore",
)


def can_skip_commit_detail(target: WatchTarget, message: str) -> bool:
    """Determine if a commit can be skipped before making an API call for files."""
    if target.routes:
        # A title cannot prove a path exclusion: protocol docs, dependencies and
        # cross-channel changes can be relevant even under another commit scope.
        return False
    if is_low_signal_commit(message):
        return True

    if target.mode == "core":
        low = message.lower()
        if any(k in low for k in ("feishu", "lark", "openclaw-lark")):
            return False
        for sc in ISOLATED_CORE_SCOPES:
            if f"({sc})" in low or f"({sc}:" in low or f"({sc}/" in low:
                return True
        if low.startswith(("docs:", "style:", "ci:", "build:", "test:", "chore:")):
            return True

    return False


def is_all_noise_files(filenames: list[str]) -> bool:
    if not filenames:
        return False
    for fn in filenames:
        low = fn.lower()
        is_doc = any(low.endswith(ext) for ext in DOC_EXTENSIONS) or any(
            low.startswith(p) for p in IGNORE_PATHS
        )
        if not is_doc:
            return False
    return True


def is_breaking_change(message: str) -> bool:
    lower = message.lower()
    prefix = message.split(":", 1)[0].strip()
    if "!" in prefix:
        return True
    return any(
        kw in lower
        for kw in (
            "breaking change",
            "breaking:",
            "break:",
            "deprecated",
            "deprecation",
            "migration required",
        )
    )


def is_test_file(filename: str) -> bool:
    """Recognize tests and shared fixtures even when they live beside runtime code."""
    parts = filename.lower().split("/")
    basename = parts[-1]
    return (
        any(
            part in {"test", "tests", "__tests__", "fixtures", "__fixtures__", "test-support"}
            for part in parts
        )
        or basename.startswith(("test_", "test-"))
        or any(
            marker in basename for marker in (".test.", ".spec.", "test-support", "test-helpers")
        )
    )


def critical_change_reason(message: str, *, is_auth: bool) -> str | None:
    """Require an explicit behavior signal; sensitive filenames alone are not critical."""
    if is_breaking_change(message):
        return "breaking-change"
    if re.search(
        r"\b(?:security|cve-\d{4}-\d+|vulnerability|ssrf|csrf|credential leak|"
        r"token leak|auth(?:entication|orization)? bypass|permission bypass)\b",
        message,
        re.IGNORECASE,
    ):
        return "security-change"
    first_line = message.splitlines()[0].lower()
    if (
        is_auth
        and re.match(r"(?:fix|feat)(?:\([^)]*\))?!?:", first_line)
        and re.search(
            r"\b(?:(?<![-\w])auth(?![-\w])|oauth|authentication|authorization|"
            r"tokens?|device[- ]flow|"
            r"scope|scopes|permission|permissions|secretref|app[- ]registration)\b",
            first_line,
        )
    ):
        return "auth-behavior-change"
    return None


def classify_routed_commit(
    target: WatchTarget, detail: dict[str, Any], raw_message: str, filenames: list[str]
) -> CandidateCommit | None:
    """Select channel contracts from paths, retaining explicitly watched protocol docs."""
    matched_files: set[str] = set()
    protocol_docs: set[str] = set()
    platforms: set[str] = set()
    contracts: set[str] = set()
    for route in target.routes:
        route_files: set[str] = set()
        for filename in filenames:
            explicit_doc = any(fnmatchcase(filename, pattern) for pattern in route.docs)
            source_match = any(fnmatchcase(filename, pattern) for pattern in route.paths)
            if explicit_doc or (source_match and not is_all_noise_files([filename])):
                route_files.add(filename)
            if explicit_doc:
                protocol_docs.add(filename)
        if route_files:
            matched_files.update(route_files)
            platforms.add(route.platform)
            contracts.update(route.contracts)
    if not matched_files:
        return None
    first_line = raw_message.splitlines()[0]
    if is_low_signal_commit(first_line) and not protocol_docs:
        return None
    runtime_files = {
        filename for filename in matched_files
        if not is_test_file(filename) and not is_all_noise_files([filename])
    }
    reasons = [f"platform:{platform}" for platform in sorted(platforms)]
    reasons.extend(f"contract:{contract}" for contract in sorted(contracts))
    critical_reason = critical_change_reason(raw_message, is_auth="auth" in contracts)
    if runtime_files and critical_reason:
        reasons.append(critical_reason)
        escalation = EscalationLevel.P1_CRITICAL
    elif runtime_files or protocol_docs:
        escalation = EscalationLevel.P2_NOTICE
    else:
        reasons.append("test-only")
        escalation = EscalationLevel.P3_ROUTINE
    if protocol_docs:
        reasons.append("protocol-doc")
    commit = detail.get("commit", {})
    sha = detail.get("sha", "")
    return CandidateCommit(
        target_key=target.key,
        upstream_repo=target.repo,
        sha=sha,
        commit_date=(commit.get("committer", {}).get("date")
                     or commit.get("author", {}).get("date") or ""),
        message=first_line,
        filenames=filenames,
        reasons=reasons,
        escalation=escalation,
        html_url=detail.get("html_url") or f"https://github.com/{target.repo}/commit/{sha}",
        author=(commit.get("author", {}).get("name")
                or detail.get("author", {}).get("login") or ""),
        platforms=sorted(platforms),
        contracts=sorted(contracts),
    )


def classify_commit(target: WatchTarget, detail: dict[str, Any]) -> CandidateCommit | None:
    commit_info = detail.get("commit", {})
    raw_message = commit_info.get("message", "").strip()
    first_line = raw_message.splitlines()[0].strip() if raw_message else ""
    if not first_line:
        return None

    files = detail.get("files") or []
    filenames = [str(item.get("filename") or "") for item in files]
    if target.routes:
        return classify_routed_commit(target, detail, raw_message, filenames)
    lower_message = first_line.lower()
    lower_files = [name.lower() for name in filenames]
    commit_date = (
        commit_info.get("committer", {}).get("date")
        or commit_info.get("author", {}).get("date")
        or ""
    )
    author = (
        commit_info.get("author", {}).get("name") or detail.get("author", {}).get("login") or ""
    )
    sha = detail.get("sha", "")
    html_url = detail.get("html_url") or f"https://github.com/{target.repo}/commit/{sha}"

    if is_low_signal_commit(first_line):
        return None

    # Noise checks
    if is_all_noise_files(filenames):
        return None

    # Check lockfile only
    if filenames and all(
        fn.endswith(("-lock.yaml", ".lock", "package-lock.json", "npm-shrinkwrap.json"))
        for fn in filenames
    ):
        return None

    # Version sync noise in plugin mode
    if target.mode == "plugin" and lower_message.startswith(
        ("feat: sync version", "chore: sync version", "bump version")
    ):
        if not any(f.startswith("src/") for f in lower_files):
            return None

    reasons: list[str] = []
    breaking = is_breaking_change(raw_message)
    if breaking:
        reasons.append("breaking-change")

    if target.mode == "plugin":
        # Target is larksuite/openclaw-lark (the dedicated Feishu/Lark plugin)
        has_src = any(f.startswith("src/") for f in lower_files)
        has_tests = any(is_test_file(f) for f in lower_files)
        runtime_files = [
            f for f in lower_files if not is_test_file(f) and not is_all_noise_files([f])
        ]
        has_runtime_src = any(f.startswith("src/") for f in runtime_files)
        has_manifest = any("openclaw.plugin.json" in f for f in lower_files)
        has_dependency = any("package.json" in f for f in lower_files)

        if not (has_src or has_tests or has_manifest or has_dependency):
            return None

        # Check signal areas
        is_card = any(
            k in lower_message or any(k in f for f in lower_files)
            for k in (
                "card",
                "interactive",
                "reply-dispatcher",
                "cardkit",
                "card-action",
                "form-submit",
            )
        )
        is_auth = any(
            k in lower_message or any(k in f for f in lower_files)
            for k in (
                "auth",
                "oauth",
                "token",
                "device-flow",
                "scope",
                "secretref",
                "app-registration",
            )
        )
        is_inbound = any(
            k in lower_message or any(k in f for f in lower_files)
            for k in ("inbound", "webhook", "event", "messaging/inbound", "dispatch-builder")
        )

        if is_auth:
            reasons.append("feishu-auth")
        if is_card:
            reasons.append("feishu-card")
        if is_inbound:
            reasons.append("feishu-native-inbound")

        if not reasons:
            if has_src:
                reasons.append("feishu-plugin-src")
            elif has_manifest:
                reasons.append("feishu-manifest")
            elif has_dependency:
                reasons.append("feishu-dependency")
            elif has_tests:
                reasons.append("feishu-test")

        critical_reason = critical_change_reason(raw_message, is_auth=is_auth)
        if runtime_files and critical_reason:
            reasons.append(critical_reason)
            escalation = EscalationLevel.P1_CRITICAL
        elif has_runtime_src:
            escalation = EscalationLevel.P2_NOTICE
        else:
            escalation = EscalationLevel.P3_ROUTINE
            if has_tests:
                reasons.append("feishu-test")

        return CandidateCommit(
            target_key=target.key,
            upstream_repo=target.repo,
            sha=sha,
            commit_date=commit_date,
            message=first_line,
            filenames=filenames,
            reasons=sorted(set(reasons)),
            escalation=escalation,
            html_url=html_url,
            author=author,
        )

    else:
        # Target is openclaw/openclaw (core agent mono-repo)
        # 1. Direct Feishu/Lark relevance
        feishu_in_msg = any(k in lower_message for k in ("feishu", "lark", "openclaw-lark"))
        feishu_files = [f for f in lower_files if "feishu" in f or "lark" in f]
        direct_feishu = feishu_in_msg or bool(feishu_files)

        # Filter out commits touching only other platforms
        unrelated_only = False
        if not direct_feishu:
            other_platform_files = [
                f for f in lower_files if any(f.startswith(p) for p in UNRELATED_EXTENSIONS)
            ]
            if other_platform_files and len(other_platform_files) == len(
                [f for f in lower_files if not is_all_noise_files([f])]
            ):
                unrelated_only = True

        if unrelated_only:
            return None

        # 2. Check for Core Channel / CardKit Contract relevance in plugin-sdk
        contract_files = [
            f
            for f in lower_files
            if ("plugin-sdk" in f or "src/plugin-sdk" in f)
            and not f.endswith(DOC_EXTENSIONS)
            and not f.startswith("docs/")
            and not f.endswith(".sha256")
            and any(k in f for k in ("channel", "card", "interactive", "session-runner"))
        ]

        if direct_feishu:
            if feishu_in_msg:
                reasons.append("feishu-mention")
            if feishu_files:
                reasons.append("feishu-extension")

            is_card = (
                any("card" in f or "action" in f for f in feishu_files) or "card" in lower_message
            )
            is_auth = any(
                k in lower_message or any(k in f for f in feishu_files)
                for k in (
                    "auth", "oauth", "token", "device-flow", "scope", "secretref", "registration"
                )
            )
            if is_card:
                reasons.append("feishu-card")
            if is_auth:
                reasons.append("feishu-auth")

            # Only relevant production files can make this a runtime escalation.
            relevant_files = feishu_files + contract_files
            runtime_files = [
                f for f in relevant_files if not is_test_file(f) and not is_all_noise_files([f])
            ]
            critical_reason = critical_change_reason(raw_message, is_auth=is_auth)
            if runtime_files and critical_reason:
                reasons.append(critical_reason)
                escalation = EscalationLevel.P1_CRITICAL
            elif runtime_files:
                escalation = EscalationLevel.P2_NOTICE
            else:
                escalation = EscalationLevel.P3_ROUTINE
                if any(is_test_file(f) for f in relevant_files):
                    reasons.append("feishu-test")

        elif contract_files:
            reasons.append("core-channel-contract")
            critical_reason = critical_change_reason(raw_message, is_auth=False)
            if any(not is_test_file(f) for f in contract_files) and critical_reason:
                reasons.append(critical_reason)
                escalation = EscalationLevel.P1_CRITICAL
            else:
                escalation = EscalationLevel.P3_ROUTINE
        else:
            # Neither direct Feishu nor core channel contract
            return None

        return CandidateCommit(
            target_key=target.key,
            upstream_repo=target.repo,
            sha=sha,
            commit_date=commit_date,
            message=first_line,
            filenames=filenames,
            reasons=sorted(set(reasons)),
            escalation=escalation,
            html_url=html_url,
            author=author,
        )


def fetch_commits_with_cursor(
    session: GitHubSession,
    *,
    repo: str,
    branch: str,
    cursor_sha: str | None = None,
    seen_shas: set[str] | None = None,
    since_date: dt.datetime | None = None,
    max_pages: int | None = None,
    per_page: int = 30,
    total_limit: int = 50,
) -> list[dict[str, Any]]:
    """Return the oldest pending batch after verifying its history boundary.

    The inspection limit must not stop history traversal: moving the cursor to
    the newest limited batch would permanently skip the rest of a backlog.
    Pin subsequent pages to the first page's HEAD, find the saved cursor, then
    return at most ``total_limit`` commits nearest that cursor. Lookback applies
    only to bootstrap runs. A missing cursor or incomplete scan fails closed.
    """
    if not 1 <= per_page <= 100 or total_limit < 1:
        raise ValueError("per_page must be between 1 and 100 and total_limit must be positive")
    if max_pages is not None and max_pages < 1:
        raise ValueError("max_pages must be positive when specified")

    pending: deque[dict[str, Any]] = deque(maxlen=total_limit)
    seen = seen_shas or set()
    bootstrap_since = since_date if not cursor_sha else None
    snapshot_sha = branch
    page = 1

    while True:
        if max_pages is not None and page > max_pages:
            raise RuntimeError(
                f"Incomplete commit history for {repo}: reached {max_pages} pages; "
                "cursor was not advanced. Increase max_pages and retry."
            )
        params: dict[str, Any] = {
            "sha": snapshot_sha,
            "per_page": per_page,
            "page": page,
        }
        if bootstrap_since:
            params["since"] = bootstrap_since.strftime("%Y-%m-%dT%H:%M:%SZ")

        commits = session.request("GET", f"repos/{repo}/commits", params=params)
        if page == 1 and commits:
            snapshot_sha = commits[0]["sha"]

        for item in commits or []:
            sha = item.get("sha", "")
            if cursor_sha and sha == cursor_sha:
                return list(reversed(pending))
            if sha in seen:
                # Seen commits need not be contiguous; only last_sha is a boundary.
                continue

            date_str = item.get("commit", {}).get("committer", {}).get("date") or ""
            if bootstrap_since and date_str:
                c_date = parse_github_datetime(date_str)
                if c_date < bootstrap_since:
                    continue

            pending.append(item)

        if not commits or len(commits) < per_page:
            if cursor_sha:
                raise RuntimeError(
                    f"Saved cursor {cursor_sha} was not found in {repo}@{branch}; "
                    "cursor was not advanced. Check for rewritten upstream history "
                    "before resetting state."
                )
            break
        page += 1

    return list(reversed(pending))


def fetch_commit_detail(session: GitHubSession, *, repo: str, sha: str) -> dict[str, Any]:
    return session.request("GET", f"repos/{repo}/commits/{sha}")


def ensure_labels(
    session: GitHubSession, *, owner: str, repo: str, targets: tuple[WatchTarget, ...] = TARGETS
) -> None:
    labels_to_ensure = [
        (LABEL_NAME, LABEL_COLOR, LABEL_DESCRIPTION),
        ("p1-critical", "B60205", "Critical breaking upstream changes"),
        ("p2-notice", "D93F0B", "Relevant channel feature / bugfix"),
    ]
    labels_to_ensure.extend(
        (f"upstream:{target.key}", "1D76DB", f"{target.key} upstream updates") for target in targets
    )
    for name, color, desc in labels_to_ensure:
        try:
            session.request("GET", f"repos/{owner}/{repo}/labels/{name}")
        except RuntimeError as exc:
            if "404" in str(exc):
                try:
                    session.request(
                        "POST",
                        f"repos/{owner}/{repo}/labels",
                        body={"name": name, "color": color, "description": desc},
                    )
                except Exception as inner_exc:
                    print(f"Warning: Failed to create label {name}: {inner_exc}", file=sys.stderr)
            else:
                print(f"Warning: Failed to check label {name}: {exc}", file=sys.stderr)


def parse_github_datetime(value: str) -> dt.datetime:
    return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)


def find_existing_digest_issue(
    session: GitHubSession,
    *,
    owner: str,
    repo: str,
    target_key: str,
    cycle: str,
) -> dict[str, Any] | None:
    """Find an open digest using the issues endpoint to avoid search rate limits."""
    try:
        issues = session.request(
            "GET",
            f"repos/{owner}/{repo}/issues",
            params={
                "labels": f"{LABEL_NAME},upstream:{target_key}",
                "state": "open",
                "per_page": 20,
            },
        )
    except Exception as exc:
        print(f"Warning: Failed to list issues for {target_key}: {exc}", file=sys.stderr)
        return None

    if not issues:
        return None

    # Check for matching cycle or rolling
    for issue in issues:
        title = issue.get("title", "")
        if target_key in title:
            if cycle == "rolling" or f"({cycle})" in title:
                return issue

    # If in rolling mode or matching target is open, return the first one
    if cycle == "rolling" and issues:
        return issues[0]

    return None


def generate_digest_title(target_key: str, cycle: str) -> str:
    if cycle == "rolling":
        return f"[upstream-watch] {target_key} sync digest"
    return f"[upstream-watch] {target_key} sync digest ({cycle})"


def parse_existing_table_entries(existing_body: str) -> list[dict[str, Any]]:
    """Extract existing commit rows from previous digest markdown table."""
    entries: list[dict[str, Any]] = []
    if not existing_body or "| Level | Commit |" not in existing_body:
        return entries

    table_part = existing_body.split("| Level | Commit |")[-1]
    if "<details>" in table_part:
        table_part = table_part.split("<details>")[0]

    for line in table_part.splitlines():
        line = line.strip()
        if not line.startswith("|") or line.startswith("|:---"):
            continue
        cols = [c.strip() for c in line.split("|")[1:-1]]
        if len(cols) < 5:
            continue
        level_col, commit_col, msg_col, sig_col, files_col = cols[:5]
        m_link = re.search(r"\[`?([0-9a-fA-F]+)`?\]\((https?://\S+)\)", commit_col)
        if not m_link:
            continue
        short_sha, url = m_link.groups()
        level = (
            EscalationLevel.P1_CRITICAL
            if "P1" in level_col
            else EscalationLevel.P2_NOTICE
            if "P2" in level_col
            else EscalationLevel.P3_ROUTINE
        )
        signals = [s.strip("` ") for s in sig_col.split(",") if s.strip("` ")]
        entries.append(
            {
                "short_sha": short_sha,
                "url": url,
                "message": msg_col.replace("\\|", "|"),
                "signals": signals,
                "level": level,
                "files_sample": files_col.strip("` "),
            }
        )
    return entries


def render_digest_body(
    target: WatchTarget,
    cycle: str,
    candidates: list[CandidateCommit],
    state: TargetState,
    existing_body: str = "",
) -> str:
    """Render comprehensive, updatable Markdown digest body."""
    now_iso = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    existing_entries = parse_existing_table_entries(existing_body)

    combined_candidates = list(candidates)

    # Convert existing entries that aren't in new candidates into CandidateCommit objects
    for ent in existing_entries:
        if not any(
            c.short_sha == ent["short_sha"] or c.sha.startswith(ent["short_sha"])
            for c in candidates
        ):
            combined_candidates.append(
                CandidateCommit(
                    target_key=target.key,
                    upstream_repo=target.repo,
                    sha=ent["short_sha"],
                    commit_date="",
                    message=ent["message"],
                    filenames=[ent["files_sample"]] if ent.get("files_sample") else [],
                    reasons=ent["signals"],
                    escalation=ent["level"],
                    html_url=ent["url"],
                    author="",
                )
            )

    p1_items = [c for c in combined_candidates if c.escalation == EscalationLevel.P1_CRITICAL]
    p2_items = [c for c in combined_candidates if c.escalation == EscalationLevel.P2_NOTICE]
    p3_items = [c for c in combined_candidates if c.escalation == EscalationLevel.P3_ROUTINE]

    # Build checklist
    checklist_lines: list[str] = []
    if p1_items:
        checklist_lines.append(f"### 🔥 P1 Critical Escalations ({len(p1_items)})")
        for item in p1_items:
            checklist_lines.append(
                f"- [ ] **[Critical]** [`{item.short_sha}`]({item.html_url}) "
                f"{item.message} (`{', '.join(item.reasons)}`)"
            )
    if p2_items:
        checklist_lines.append(f"\n### ⚡ P2 Channel Features & Fixes ({len(p2_items)})")
        for item in p2_items[:15]:
            checklist_lines.append(f"- [ ] [`{item.short_sha}`]({item.html_url}) {item.message}")
        if len(p2_items) > 15:
            checklist_lines.append(f"- *...and {len(p2_items) - 15} more P2 items (see ledger)*")

    checklist_block = (
        "\n".join(checklist_lines)
        if checklist_lines
        else "- [x] No pending action items in this cycle."
    )

    # Build table
    table_lines = [
        "| Level | Commit | Summary | Signals | Files |",
        "|:---:|:---:|:---|:---|:---|",
    ]
    for item in combined_candidates:
        level_badge = {
            EscalationLevel.P1_CRITICAL: "🔥 **P1**",
            EscalationLevel.P2_NOTICE: "⚡ **P2**",
            EscalationLevel.P3_ROUTINE: "ℹ️ P3",
        }.get(item.escalation, "ℹ️")

        short_msg = item.message.replace("|", "\\|")
        if len(short_msg) > 70:
            short_msg = short_msg[:67] + "..."
        sig_str = ", ".join(item.reasons)
        files_sample = ", ".join(os.path.basename(f) for f in item.filenames[:3])
        if len(item.filenames) > 3:
            files_sample += f" (+{len(item.filenames) - 3})"

        table_lines.append(
            f"| {level_badge} | [`{item.short_sha}`]({item.html_url}) | {short_msg} "
            f"| `{sig_str}` | `{files_sample}` |"
        )
    table_block = "\n".join(table_lines)

    # Build details accordion
    detail_blocks: list[str] = []
    for item in combined_candidates:
        files_list = "\n".join(f"  - `{fn}`" for fn in item.filenames[:12])
        if len(item.filenames) > 12:
            files_list += f"\n  - *...and {len(item.filenames) - 12} more files*"
        detail_blocks.append(
            f"#### [`{item.short_sha}`]({item.html_url}) - {item.message}\n"
            f"- **Escalation**: `{item.escalation.value}`\n"
            f"- **Signals**: `{', '.join(item.reasons)}`\n"
            f"- **Date**: `{item.commit_date}` | **Author**: `{item.author}`\n"
            f"- **Changed files**:\n{files_list}"
        )
    details_block = "\n\n".join(detail_blocks)

    # Machine-readable state payload
    state_payload = {
        "version": 1,
        "target": target.key,
        "cycle": cycle,
        "last_sha": state.last_sha,
        "last_date": state.last_date,
        "seen_shas": state.seen_shas[-50:],
        "last_sync_at": now_iso,
        "total_commits": len(combined_candidates),
    }
    state_marker = f"<!-- upstream-watch-state: {json.dumps(state_payload, ensure_ascii=False)} -->"

    body = (
        f"# Upstream Sync Digest: `{target.key}`\n\n"
        f"- **Upstream**: [`{target.repo}`](https://github.com/{target.repo}) (`{target.branch}`)\n"
        f"- **Cycle**: `{cycle}`\n"
        f"- **Last Synced**: `{now_iso}`\n"
        f"- **Monitored Commits**: {len(combined_candidates)} "
        f"(🔥 {len(p1_items)} critical, ⚡ {len(p2_items)} notice, "
        f"ℹ️ {len(p3_items)} routine)\n\n"
        f"## 🚦 Sync Action Items\n\n"
        f"{checklist_block}\n\n"
        f"## 📋 Commit Ledger\n\n"
        f"{table_block}\n\n"
        f"<details>\n"
        f"<summary>🔍 Detailed Commit Inspect & File Diff Lists</summary>\n\n"
        f"{details_block}\n"
        f"</details>\n\n"
        f"## 🛠 Upstream Sync Guide\n\n"
        f"1. For **P1 Critical** changes: verify affected authentication, security "
        f"and breaking contracts.\n"
        f"2. For **P2 Notice** changes: follow the platform/contract signals to "
        f"update adapters, dependency compatibility and protocol fixtures.\n"
        f"3. Check off items above as they are verified or integrated.\n\n"
        f"---\n"
        f"{state_marker}\n"
    )
    return body


def evaluate_offline_corpus(
    corpus_path: str,
    targets: tuple[WatchTarget, ...] = TARGETS,
) -> dict[str, Any]:
    """Run offline evaluation on saved issues corpus."""
    with open(corpus_path, encoding="utf-8") as f:
        issues = json.load(f)

    target_map = {t.key: t for t in targets}
    repo_to_target = {t.repo: t for t in targets}

    stats = {
        "total_issues_inspected": len(issues),
        "by_target": {},
        "escalation_distribution": {
            EscalationLevel.P1_CRITICAL.value: 0,
            EscalationLevel.P2_NOTICE.value: 0,
            EscalationLevel.P3_ROUTINE.value: 0,
            "FILTERED_NOISE": 0,
        },
        "filtered_reasons": {},
        "classified_commits": [],
    }

    for item in issues:
        body = item.get("body", "")
        m_up = re.search(r"- Upstream:\s*`([^`]+)`", body)
        m_sha = re.search(r"- Commit:\s*`([^`]+)`", body)
        m_url = re.search(r"- URL:\s*(\S+)", body)
        m_sum = re.search(r"- Summary:\s*(.+)", body)

        repo = m_up.group(1) if m_up else ""
        sha = m_sha.group(1) if m_sha else ""
        summary = m_sum.group(1) if m_sum else ""
        url = m_url.group(1) if m_url else ""

        files: list[str] = []
        if "## Changed files" in body:
            f_part = body.split("## Changed files")[-1].split("## Sync checklist")[0]
            files = re.findall(r"- `([^`]+)`", f_part)

        target = repo_to_target.get(repo)
        if not target and not repo:
            if "openclaw-lark" in body:
                target = target_map.get("openclaw-lark")
            else:
                target = target_map.get("openclaw-core")

        if not target:
            continue

        mock_detail = {
            "sha": sha,
            "html_url": url,
            "commit": {
                "message": summary,
                "committer": {"date": item.get("createdAt", "2026-01-01T00:00:00Z")},
                "author": {"name": "offline-sample"},
            },
            "files": [{"filename": f} for f in files],
        }

        cand = classify_commit(target, mock_detail)
        if cand is None:
            stats["escalation_distribution"]["FILTERED_NOISE"] += 1
            if is_low_signal_commit(summary):
                reason = "low_signal_commit"
            elif is_all_noise_files(files):
                reason = "doc_or_config_noise"
            else:
                reason = "core_unrelated_to_feishu"
            stats["filtered_reasons"][reason] = stats["filtered_reasons"].get(reason, 0) + 1
        else:
            stats["escalation_distribution"][cand.escalation.value] += 1
            stats["classified_commits"].append(
                {
                    "issue_number": item.get("number"),
                    "target": target.key,
                    "sha": cand.short_sha,
                    "summary": cand.message,
                    "escalation": cand.escalation.value,
                    "signals": cand.reasons,
                    "platforms": cand.platforms,
                    "contracts": cand.contracts,
                    "url": cand.html_url,
                }
            )

    return stats


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        configured_targets = load_watch_targets(args.config)
    except (OSError, ValueError) as exc:
        print(f"Invalid upstream config {args.config}: {exc}", file=sys.stderr)
        return 2
    active_targets = tuple(t for t in configured_targets if not args.target or t.key == args.target)
    if not active_targets:
        print(f"Unknown target in upstream config: {args.target}", file=sys.stderr)
        return 2

    if args.offline_corpus:
        print(f"Running offline evaluation on corpus: {args.offline_corpus}...")
        eval_results = evaluate_offline_corpus(args.offline_corpus, targets=active_targets)
        print(json.dumps(eval_results, indent=2, ensure_ascii=False))
        return 0

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token and not args.dry_run:
        print("Missing GITHUB_TOKEN or GH_TOKEN", file=sys.stderr)
        return 2

    # In dry-run mode without token, we can mock or use anonymous requests if needed
    session = GitHubSession(token=token or "")
    cycle = get_current_cycle(args.cycle)

    # Load persistent state
    state_store = UpstreamStateStore.load_from_file(args.state_file)

    now_utc = dt.datetime.now(dt.timezone.utc)
    since_date = now_utc - dt.timedelta(days=args.lookback_days)

    if not args.dry_run and token:
        ensure_labels(session, owner=args.owner, repo=args.repo, targets=active_targets)

    summary_results: list[dict[str, Any]] = []

    for target in active_targets:
        target_state = state_store.get_target_state(target.key)

        # Check existing digest issue on GitHub
        existing_issue: dict[str, Any] | None = None
        if token:
            existing_issue = find_existing_digest_issue(
                session,
                owner=args.owner,
                repo=args.repo,
                target_key=target.key,
                cycle=cycle,
            )

        # Recover cursor from issue if state file was cold/empty
        if existing_issue and not target_state.last_sha:
            issue_body_text = existing_issue.get("body", "")
            embedded_state = UpstreamStateStore.extract_from_text(issue_body_text)
            if embedded_state:
                print(
                    f"Recovered cursor for {target.key} from issue #{existing_issue['number']}: "
                    f"{embedded_state.get('last_sha')}"
                )
                target_state.last_sha = embedded_state.get("last_sha", "")
                target_state.last_date = embedded_state.get("last_date", "")
                target_state.seen_shas = embedded_state.get("seen_shas", [])

        print(
            f"[{target.key}] Fetching commits from {target.repo} ({target.branch})...",
            file=sys.stderr,
        )
        raw_commits = fetch_commits_with_cursor(
            session,
            repo=target.repo,
            branch=target.branch,
            cursor_sha=target_state.last_sha or None,
            seen_shas=set(target_state.seen_shas),
            since_date=since_date,
            per_page=100,
            total_limit=args.per_target_limit,
        )
        print(f"[{target.key}] Inspecting {len(raw_commits)} commits...", file=sys.stderr)

        new_candidates: list[CandidateCommit] = []
        for c in raw_commits:
            sha = c["sha"]
            c_msg = c.get("commit", {}).get("message", "").splitlines()[0].strip()
            c_date = (
                c.get("commit", {}).get("committer", {}).get("date")
                or c.get("commit", {}).get("author", {}).get("date")
                or ""
            )

            if can_skip_commit_detail(target, c_msg):
                target_state.record_commit(sha, c_date)
                continue

            detail = fetch_commit_detail(session, repo=target.repo, sha=sha)
            cand = classify_commit(target, detail)
            if cand is not None:
                new_candidates.append(cand)
            target_state.record_commit(sha, c_date)

        target_state.last_sync_at = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        target_state.active_cycle = cycle

        if not new_candidates and existing_issue:
            summary_results.append(
                {
                    "target": target.key,
                    "action": "no_change",
                    "cycle": cycle,
                    "issue_number": existing_issue.get("number"),
                    "new_commits": 0,
                }
            )
            continue

        if not new_candidates and not existing_issue:
            summary_results.append(
                {
                    "target": target.key,
                    "action": "no_candidates",
                    "cycle": cycle,
                    "new_commits": 0,
                }
            )
            continue

        # Render new or updated digest body
        title = generate_digest_title(target.key, cycle)
        body = render_digest_body(
            target=target,
            cycle=cycle,
            candidates=new_candidates,
            state=target_state,
            existing_body=existing_issue.get("body", "") if existing_issue else "",
        )

        if args.dry_run:
            print(f"=== [DRY RUN] {title} ===")
            print(f"Action: {'UPDATE' if existing_issue else 'CREATE'}")
            print(f"New candidate commits ({len(new_candidates)}):")
            for cand in new_candidates:
                print(
                    f"  [{cand.escalation.value}] {cand.short_sha}: {cand.message} "
                    f"({', '.join(cand.reasons)})"
                )
            summary_results.append(
                {
                    "target": target.key,
                    "action": "dry_run_update" if existing_issue else "dry_run_create",
                    "cycle": cycle,
                    "new_commits": len(new_candidates),
                    "title": title,
                }
            )
        else:
            if existing_issue:
                # Update existing issue
                issue_num = existing_issue["number"]
                session.request(
                    "PATCH",
                    f"repos/{args.owner}/{args.repo}/issues/{issue_num}",
                    body={"body": body},
                )
                target_state.active_issue_number = issue_num
                summary_results.append(
                    {
                        "target": target.key,
                        "action": "updated",
                        "cycle": cycle,
                        "issue_number": issue_num,
                        "new_commits": len(new_candidates),
                    }
                )
            else:
                # Create new digest issue
                labels = [LABEL_NAME, f"upstream:{target.key}", f"cycle:{cycle}"]
                created = session.request(
                    "POST",
                    f"repos/{args.owner}/{args.repo}/issues",
                    body={"title": title, "body": body, "labels": labels},
                )
                target_state.active_issue_number = created["number"]
                summary_results.append(
                    {
                        "target": target.key,
                        "action": "created",
                        "cycle": cycle,
                        "issue_number": created["number"],
                        "new_commits": len(new_candidates),
                    }
                )

    # Save state to disk
    if not args.dry_run:
        state_store.save_to_file(args.state_file)

    print(json.dumps({"results": summary_results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
