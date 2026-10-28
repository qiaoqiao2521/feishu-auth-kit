import datetime as dt
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts import upstream_watch  # noqa: E402
from scripts.upstream_watch import (  # noqa: E402
    CandidateCommit,
    EscalationLevel,
    TargetState,
    UpstreamStateStore,
    WatchTarget,
    classify_commit,
    evaluate_offline_corpus,
    get_current_cycle,
    render_digest_body,
)


def test_issue_target_defaults_to_migrated_repository(monkeypatch) -> None:
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    args = upstream_watch.parse_args([])
    assert (args.owner, args.repo) == ("qiaoqiao2521", "feishu-auth-kit")


def test_issue_target_follows_workflow_repository(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", "another-owner/renamed-kit")
    args = upstream_watch.parse_args([])
    assert (args.owner, args.repo) == ("another-owner", "renamed-kit")


def test_explicit_issue_target_overrides_workflow_repository(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", "another-owner/renamed-kit")
    args = upstream_watch.parse_args(["--owner", "explicit-owner", "--repo", "explicit-kit"])
    assert (args.owner, args.repo) == ("explicit-owner", "explicit-kit")


def test_partial_issue_target_override_uses_workflow_default(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", "another-owner/renamed-kit")
    args = upstream_watch.parse_args(["--repo", "explicit-kit"])
    assert (args.owner, args.repo) == ("another-owner", "explicit-kit")


@pytest.mark.parametrize("repository", ["missing-repo", "owner/", "owner/repo/extra"])
def test_invalid_workflow_repository_cannot_choose_issue_target(monkeypatch, repository) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", repository)
    with pytest.raises(SystemExit) as exc:
        upstream_watch.parse_args([])
    assert exc.value.code == 2


def test_get_current_cycle() -> None:
    sample_date = dt.datetime(2026, 9, 14, 12, 0, 0, tzinfo=dt.timezone.utc)
    assert get_current_cycle("weekly", sample_date) == "2026-W38"
    assert get_current_cycle("monthly", sample_date) == "2026-09"
    assert get_current_cycle("rolling", sample_date) == "rolling"


def test_classify_plugin_noise_and_relevant() -> None:
    target = WatchTarget(
        key="openclaw-lark",
        repo="larksuite/openclaw-lark",
        branch="main",
        mode="plugin",
    )

    # 1. Pure docs noise
    doc_detail = {
        "sha": "1111111111",
        "html_url": "https://github.com/larksuite/openclaw-lark/commit/1111111111",
        "commit": {
            "message": "docs: update readme setup",
            "committer": {"date": "2026-09-14T01:00:00Z"},
            "author": {"name": "alice"},
        },
        "files": [{"filename": "docs/setup.md"}, {"filename": "README.md"}],
    }
    assert classify_commit(target, doc_detail) is None

    # 2. Pure prettier/lint noise
    lint_detail = {
        "sha": "2222222222",
        "html_url": "https://github.com/larksuite/openclaw-lark/commit/2222222222",
        "commit": {
            "message": "chore: fix prettier violation in abort-detect.ts",
            "committer": {"date": "2026-09-14T01:00:00Z"},
            "author": {"name": "bob"},
        },
        "files": [{"filename": "src/channel/abort-detect.ts"}],
    }
    assert classify_commit(target, lint_detail) is None

    # 3. P1 Breaking change
    breaking_detail = {
        "sha": "3333333333",
        "html_url": "https://github.com/larksuite/openclaw-lark/commit/3333333333",
        "commit": {
            "message": "feat!: breaking change in card action response",
            "committer": {"date": "2026-09-14T01:00:00Z"},
            "author": {"name": "carol"},
        },
        "files": [{"filename": "src/card/reply-dispatcher.ts"}],
    }
    cand_breaking = classify_commit(target, breaking_detail)
    assert cand_breaking is not None
    assert cand_breaking.escalation == EscalationLevel.P1_CRITICAL
    assert "breaking-change" in cand_breaking.reasons
    assert "feishu-card" in cand_breaking.reasons

    # 4. P1 Auth / OAuth change
    auth_detail = {
        "sha": "4444444444",
        "html_url": "https://github.com/larksuite/openclaw-lark/commit/4444444444",
        "commit": {
            "message": "fix: update device-flow token refresh",
            "committer": {"date": "2026-09-14T01:00:00Z"},
            "author": {"name": "dave"},
        },
        "files": [{"filename": "src/auth/device-flow.ts"}],
    }
    cand_auth = classify_commit(target, auth_detail)
    assert cand_auth is not None
    assert cand_auth.escalation == EscalationLevel.P1_CRITICAL
    assert "feishu-auth" in cand_auth.reasons

    # 5. P2 Normal feature/fix
    p2_detail = {
        "sha": "5555555555",
        "html_url": "https://github.com/larksuite/openclaw-lark/commit/5555555555",
        "commit": {
            "message": "feat: transcribe inbound voice notes",
            "committer": {"date": "2026-09-14T01:00:00Z"},
            "author": {"name": "eve"},
        },
        "files": [{"filename": "src/messaging/inbound/handler.ts"}],
    }
    cand_p2 = classify_commit(target, p2_detail)
    assert cand_p2 is not None
    assert cand_p2.escalation == EscalationLevel.P2_NOTICE
    assert "feishu-native-inbound" in cand_p2.reasons


def test_classify_core_filtering() -> None:
    target = WatchTarget(
        key="openclaw-core",
        repo="openclaw/openclaw",
        branch="main",
        mode="core",
    )

    # 1. Unrelated core commit touching discord and doc baseline
    unrelated_detail = {
        "sha": "aaaaa11111",
        "html_url": "https://github.com/openclaw/openclaw/commit/aaaaa11111",
        "commit": {
            "message": "fix: use plain agent progress labels (#107260)",
            "committer": {"date": "2026-09-14T01:00:00Z"},
            "author": {"name": "frank"},
        },
        "files": [
            {"filename": "docs/.generated/plugin-sdk-api-baseline.sha256"},
            {"filename": "extensions/discord/src/monitor/message-handler.ts"},
            {"filename": "src/shared/progress-labels.ts"},
        ],
    }
    assert classify_commit(target, unrelated_detail) is None

    # 2. Genuine Feishu extension commit in core
    feishu_detail = {
        "sha": "bbbbb22222",
        "html_url": "https://github.com/openclaw/openclaw/commit/bbbbb22222",
        "commit": {
            "message": "refactor(feishu): remove unused exports",
            "committer": {"date": "2026-09-14T01:00:00Z"},
            "author": {"name": "grace"},
        },
        "files": [
            {"filename": "extensions/feishu/src/bot.ts"},
            {"filename": "extensions/feishu/src/card-action.ts"},
        ],
    }
    cand = classify_commit(target, feishu_detail)
    assert cand is not None
    assert cand.escalation == EscalationLevel.P2_NOTICE  # filename alone is not critical
    assert "feishu-card" in cand.reasons
    assert "feishu-extension" in cand.reasons

    # 3. Core channel contract in plugin-sdk
    contract_detail = {
        "sha": "ccccc33333",
        "html_url": "https://github.com/openclaw/openclaw/commit/ccccc33333",
        "commit": {
            "message": "refactor(channel): update streaming message dispatch contract",
            "committer": {"date": "2026-09-14T01:00:00Z"},
            "author": {"name": "heidi"},
        },
        "files": [
            {"filename": "src/plugin-sdk/channel-streaming.ts"},
        ],
    }
    cand_contract = classify_commit(target, contract_detail)
    assert cand_contract is not None
    assert cand_contract.escalation == EscalationLevel.P3_ROUTINE
    assert "core-channel-contract" in cand_contract.reasons


@pytest.mark.parametrize("mode", ["plugin", "core"])
@pytest.mark.parametrize(
    ("message", "path", "expected", "reason"),
    [
        (
            "test(feishu): remove low-value auth and card tests",
            "auth.test.ts",
            EscalationLevel.P3_ROUTINE,
            "feishu-test",
        ),
        (
            "refactor: deslop channel callbacks and UI owners",
            "card-interaction.ts",
            EscalationLevel.P2_NOTICE,
            "feishu-card",
        ),
        (
            "refactor(auth): simplify token owner wiring",
            "auth.ts",
            EscalationLevel.P2_NOTICE,
            "feishu-auth",
        ),
        (
            "fix(feishu): quick-action cards fail to send",
            "card-action.ts",
            EscalationLevel.P2_NOTICE,
            "feishu-card",
        ),
        (
            "fix(feishu): refresh expired OAuth tokens",
            "auth.ts",
            EscalationLevel.P1_CRITICAL,
            "auth-behavior-change",
        ),
        (
            "fix: replace zalouser import with command-auth for OpenClaw compatibility",
            "auth.ts",
            EscalationLevel.P2_NOTICE,
            "feishu-auth",
        ),
        (
            "fix(feishu): refresh expired tokens",
            "token-store.ts",
            EscalationLevel.P1_CRITICAL,
            "auth-behavior-change",
        ),
        (
            "fix(feishu): prevent credential leak in diagnostics",
            "client.ts",
            EscalationLevel.P1_CRITICAL,
            "security-change",
        ),
        (
            "feat(feishu): revise callback payload\n\nBREAKING CHANGE: operator identity moved",
            "card-action.ts",
            EscalationLevel.P1_CRITICAL,
            "breaking-change",
        ),
    ],
)
def test_severity_requires_runtime_behavior_evidence(mode, message, path, expected, reason) -> None:
    target = next(target for target in upstream_watch.TARGETS if target.mode == mode)
    prefix = "src/" if mode == "plugin" else "extensions/feishu/src/"
    candidate = classify_commit(
        target,
        {"sha": "severity", "commit": {"message": message}, "files": [{"filename": prefix + path}]},
    )
    assert candidate is not None
    assert candidate.escalation == expected
    assert reason in candidate.reasons


def test_unrelated_runtime_changes_do_not_escalate_feishu_test_only_change() -> None:
    target = next(target for target in upstream_watch.TARGETS if target.mode == "core")
    candidate = classify_commit(
        target,
        {
            "sha": "mixed",
            "commit": {"message": "fix(security): reject invalid credentials"},
            "files": [
                {"filename": "extensions/discord/src/auth.ts"},
                {"filename": "extensions/feishu/src/auth.test.ts"},
                {"filename": "test-mock-exports-baseline.txt"},
            ],
        },
    )
    assert candidate is not None
    assert candidate.escalation == EscalationLevel.P3_ROUTINE


@pytest.mark.parametrize("mode", ["plugin", "core"])
def test_security_test_fixtures_do_not_trigger_critical_alert(mode) -> None:
    target = next(target for target in upstream_watch.TARGETS if target.mode == mode)
    prefix = "src/" if mode == "plugin" else "extensions/feishu/src/"
    candidate = classify_commit(
        target,
        {
            "sha": "fixtures",
            "commit": {"message": "test(feishu): add authentication bypass regression"},
            "files": [
                {"filename": prefix + "auth.test.ts"},
                {"filename": prefix + "test-support/auth.ts"},
                {"filename": "docs/feishu-auth.md"},
            ],
        },
    )
    assert candidate is not None
    assert candidate.escalation == EscalationLevel.P3_ROUTINE


def test_persistent_state_roundtrip(tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    store = UpstreamStateStore()

    target_state = store.get_target_state("openclaw-lark")
    target_state.record_commit("commit1", "2026-09-14T01:00:00Z")
    target_state.record_commit("commit2", "2026-09-14T02:00:00Z")
    target_state.active_issue_number = 42
    target_state.active_cycle = "2026-W38"

    store.save_to_file(str(state_file))

    # Reload
    loaded = UpstreamStateStore.load_from_file(str(state_file))
    t_loaded = loaded.get_target_state("openclaw-lark")
    assert t_loaded.last_sha == "commit2"
    assert t_loaded.last_date == "2026-09-14T02:00:00Z"
    assert t_loaded.seen_shas == ["commit1", "commit2"]
    assert t_loaded.active_issue_number == 42


def test_extract_state_from_issue_comment() -> None:
    text = (
        "# Some issue title\n\n"
        "Commit ledger and checklist\n\n"
        '<!-- upstream-watch-state: {"version": 1, "target": "openclaw-lark", '
        '"last_sha": "abc1234", "seen_shas": ["abc1234"]} -->\n'
    )
    extracted = UpstreamStateStore.extract_from_text(text)
    assert extracted is not None
    assert extracted["target"] == "openclaw-lark"
    assert extracted["last_sha"] == "abc1234"


def test_render_digest_body_and_idempotence() -> None:
    target = WatchTarget(
        key="openclaw-lark",
        repo="larksuite/openclaw-lark",
        branch="main",
        mode="plugin",
    )
    cand = CandidateCommit(
        target_key="openclaw-lark",
        upstream_repo="larksuite/openclaw-lark",
        sha="96535757356d52f0415f750d1fdbdb536c097922",
        commit_date="2026-05-15T04:00:00Z",
        message="fix: resolve HTTP proxy url concat",
        filenames=["src/core/lark-client.ts"],
        reasons=["feishu-plugin-src"],
        escalation=EscalationLevel.P2_NOTICE,
        html_url="https://github.com/larksuite/openclaw-lark/commit/96535757356d52f0415f750d1fdbdb536c097922",
        author="easonlh",
    )
    state = TargetState(last_sha=cand.sha, last_date=cand.commit_date, seen_shas=[cand.sha])

    body1 = render_digest_body(
        target=target,
        cycle="2026-W38",
        candidates=[cand],
        state=state,
    )

    assert (
        "https://github.com/larksuite/openclaw-lark/commit/96535757356d52f0415f750d1fdbdb536c097922"
        in body1
    )
    assert "9653575" in body1
    assert "feishu-plugin-src" in body1
    assert "<!-- upstream-watch-state:" in body1

    extracted_state = UpstreamStateStore.extract_from_text(body1)
    assert extracted_state is not None
    assert extracted_state["last_sha"] == cand.sha


def test_dry_run_no_noise_on_duplicate_inputs(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    corpus_file = Path("tests/upstream_issues_corpus.json")
    if not corpus_file.exists():
        pytest.skip("Corpus file not found")

    stats = evaluate_offline_corpus(str(corpus_file))
    assert stats["total_issues_inspected"] == 340
    assert stats["escalation_distribution"]["FILTERED_NOISE"] > 180
    assert stats["escalation_distribution"]["P1_CRITICAL"] > 0
    assert stats["escalation_distribution"]["P2_NOTICE"] > 0


def test_pagination_stops_at_cursor() -> None:
    class MockSession(upstream_watch.GitHubSession):
        def __init__(self) -> None:
            super().__init__(token="mock")
            self.page_calls = []

        def request(self, method: str, path: str, *, params=None, body=None):
            page = params.get("page", 1)
            self.page_calls.append(page)
            if page == 1:
                return [
                    {"sha": "c3", "commit": {"committer": {"date": "2026-09-14T03:00:00Z"}}},
                    {"sha": "c2", "commit": {"committer": {"date": "2026-09-14T02:00:00Z"}}},
                ]
            elif page == 2:
                return [
                    {"sha": "c1", "commit": {"committer": {"date": "2026-09-14T01:00:00Z"}}},
                    {"sha": "c0", "commit": {"committer": {"date": "2026-09-14T00:00:00Z"}}},
                ]
            return []

    mock_sess = MockSession()
    # If cursor is c2, pagination should stop on page 1 after c3
    commits = upstream_watch.fetch_commits_with_cursor(
        mock_sess,
        repo="larksuite/openclaw-lark",
        branch="main",
        cursor_sha="c2",
        per_page=2,
    )
    assert len(commits) == 1
    assert commits[0]["sha"] == "c3"
    assert mock_sess.page_calls == [1]


def test_repeated_dry_run_zero_noise(tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    store = UpstreamStateStore()
    target_state = store.get_target_state("openclaw-lark")

    # Initial run: ingest c1, c2
    commits_data = [
        {"sha": "c1", "date": "2026-09-14T01:00:00Z"},
        {"sha": "c2", "date": "2026-09-14T02:00:00Z"},
    ]
    for c in commits_data:
        target_state.record_commit(c["sha"], c["date"])
    store.save_to_file(str(state_file))

    # Second run: upstream returns same commits [c2, c1]
    class MockSession(upstream_watch.GitHubSession):
        def __init__(self) -> None:
            super().__init__(token="mock")

        def request(self, method: str, path: str, *, params=None, body=None):
            return [
                {"sha": "c2", "commit": {"committer": {"date": "2026-09-14T02:00:00Z"}}},
                {"sha": "c1", "commit": {"committer": {"date": "2026-09-14T01:00:00Z"}}},
            ]

    loaded_store = UpstreamStateStore.load_from_file(str(state_file))
    t_state = loaded_store.get_target_state("openclaw-lark")
    mock_sess = MockSession()
    new_raw = upstream_watch.fetch_commits_with_cursor(
        mock_sess,
        repo="larksuite/openclaw-lark",
        branch="main",
        cursor_sha=t_state.last_sha,
        seen_shas=set(t_state.seen_shas),
    )
    # Exactly 0 new commits are returned on repeated run!
    assert len(new_raw) == 0


class CommitHistorySession(upstream_watch.GitHubSession):
    def __init__(self, count: int) -> None:
        super().__init__(token="mock")
        self.commits = [self.make_commit(index) for index in reversed(range(count))]
        self.calls: list[dict[str, Any]] = []

    @staticmethod
    def make_commit(index: int) -> dict[str, Any]:
        date = dt.datetime(2026, 9, 14, tzinfo=dt.timezone.utc) + dt.timedelta(minutes=index)
        return {
            "sha": f"c{index}",
            "commit": {"committer": {"date": date.strftime("%Y-%m-%dT%H:%M:%SZ")}},
        }

    def request(self, method: str, path: str, *, params=None, body=None):
        self.calls.append(dict(params))
        start = (params["page"] - 1) * params["per_page"]
        return self.commits[start : start + params["per_page"]]


def test_backlog_larger_than_limit_is_drained_without_gaps() -> None:
    session = CommitHistorySession(12)
    state = TargetState(last_sha="c0", seen_shas=["c0"])
    processed: list[str] = []

    for _ in range(6):
        commits = upstream_watch.fetch_commits_with_cursor(
            session,
            repo="owner/upstream",
            branch="main",
            cursor_sha=state.last_sha,
            seen_shas=set(state.seen_shas),
            per_page=2,
            total_limit=3,
        )
        for commit in commits:
            processed.append(commit["sha"])
            state.record_commit(commit["sha"], commit["commit"]["committer"]["date"])
        if not commits:
            break

    assert processed == [f"c{index}" for index in range(1, 12)]
    assert state.last_sha == "c11"
    # This backlog requires more than the old five-page limit.
    assert session.calls[5]["page"] == 6
    assert session.calls[0]["sha"] == "main"
    assert all(call["sha"] == "c11" for call in session.calls[1:6])


def test_existing_cursor_ignores_bootstrap_lookback() -> None:
    session = CommitHistorySession(5)
    commits = upstream_watch.fetch_commits_with_cursor(
        session,
        repo="owner/upstream",
        branch="main",
        cursor_sha="c0",
        since_date=dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc),
        per_page=2,
    )
    assert [commit["sha"] for commit in commits] == ["c1", "c2", "c3", "c4"]
    assert all("since" not in call for call in session.calls)


def test_bootstrap_starts_with_oldest_batch_in_lookback() -> None:
    session = CommitHistorySession(8)
    cutoff = dt.datetime(2026, 9, 14, 0, 2, tzinfo=dt.timezone.utc)
    commits = upstream_watch.fetch_commits_with_cursor(
        session,
        repo="owner/upstream",
        branch="main",
        since_date=cutoff,
        per_page=3,
        total_limit=2,
    )
    assert [commit["sha"] for commit in commits] == ["c2", "c3"]
    assert all(call["since"] == "2026-09-14T00:02:00Z" for call in session.calls)


def test_seen_sha_is_not_a_history_boundary() -> None:
    session = CommitHistorySession(5)
    commits = upstream_watch.fetch_commits_with_cursor(
        session,
        repo="owner/upstream",
        branch="main",
        cursor_sha="c0",
        seen_shas={"c0", "c3"},
        per_page=2,
    )
    assert [commit["sha"] for commit in commits] == ["c1", "c2", "c4"]


def test_missing_cursor_fails_instead_of_skipping_history() -> None:
    session = CommitHistorySession(5)
    with pytest.raises(RuntimeError, match="Saved cursor missing was not found"):
        upstream_watch.fetch_commits_with_cursor(
            session,
            repo="owner/upstream",
            branch="main",
            cursor_sha="missing",
            seen_shas={"c1"},
            per_page=2,
            total_limit=1,
        )
    assert [call["page"] for call in session.calls] == [1, 2, 3]


@pytest.mark.parametrize("cursor", ["c0", None])
def test_incomplete_scan_fails_without_returning_a_partial_batch(cursor: str | None) -> None:
    session = CommitHistorySession(5)
    with pytest.raises(RuntimeError, match="Incomplete commit history"):
        upstream_watch.fetch_commits_with_cursor(
            session,
            repo="owner/upstream",
            branch="main",
            cursor_sha=cursor,
            max_pages=1,
            per_page=2,
            total_limit=1,
        )
    assert len(session.calls) == 1


def test_issue_repository_defaults_to_workflow_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", "fork-owner/fork-repo")
    args = upstream_watch.parse_args([])
    assert (args.owner, args.repo) == ("fork-owner", "fork-repo")

    args = upstream_watch.parse_args(["--owner", "override", "--repo", "destination"])
    assert (args.owner, args.repo) == ("override", "destination")

    args = upstream_watch.parse_args(["--owner", "override"])
    assert (args.owner, args.repo) == ("override", "fork-repo")


def test_issue_repository_fallback_is_user_fork(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    args = upstream_watch.parse_args([])
    assert (args.owner, args.repo) == ("qiaoqiao2521", "feishu-auth-kit")


def test_invalid_inspection_limit_is_rejected() -> None:
    with pytest.raises(SystemExit, match="2"):
        upstream_watch.parse_args(["--per-target-limit", "0"])
