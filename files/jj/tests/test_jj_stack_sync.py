# /// script
# requires-python = ">=3.12"
# dependencies = ["markdown-it-py==4.2.0", "pytest"]
# ///
from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "files" / "bin"))
import jj_stack_sync as sync  # noqa: E402
from fake_github import (  # noqa: E402
    FakeGitHubClient,
    FakeGitHubServer,
    FakeGitTransport,
    RefUpdatePolicy,
)


def run(*args: str | Path, cwd: Path | None = None) -> str:
    result = subprocess.run(
        [os.fspath(arg) for arg in args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        pytest.fail(
            f"`{' '.join(map(os.fspath, args))}` failed\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result.stdout.strip()


def jj(repo: Path, *args: str) -> str:
    return run("jj", "--repository", repo, *args)


@pytest.fixture
def jj_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    run("jj", "git", "init", "--colocate", repo)
    jj(repo, "config", "set", "--repo", "user.name", "Test User")
    jj(repo, "config", "set", "--repo", "user.email", "test@example.com")
    jj(repo, "config", "set", "--repo", "git.push", "origin")
    (repo / "tracked").write_text("committed\n")
    jj(repo, "describe", "-m", "Topic title\n\nTopic body")
    jj(repo, "bookmark", "create", "topic", "-r", "@")
    return repo


def op_id(repo: Path) -> str:
    return jj(
        repo,
        "--ignore-working-copy",
        "op",
        "log",
        "--no-graph",
        "-n",
        "1",
        "-T",
        "self.id()",
    )


def all_refs(repo: Path) -> str:
    return run("git", "-C", repo, "for-each-ref", "--format=%(refname)%09%(objectname)")


def local_with_remotes(
    remotes: tuple[str, ...], *, git_push: str | None = None
) -> sync.LocalObservation:
    config = () if git_push is None else (("git.push", git_push),)
    return sync.LocalObservation("/repo", "op", config, (), (), (), (), (), "/git", remotes)


def test_remote_resolution_uses_explicit_native_and_conventional_precedence() -> None:
    configured = local_with_remotes(("backup", "origin", "publish"), git_push="publish")
    assert sync.resolve_remote(configured, "backup") == "backup"
    assert sync.resolve_remote(configured) == "publish"
    assert sync.resolve_remote(local_with_remotes(("publish",))) == "publish"
    assert sync.resolve_remote(local_with_remotes(("backup", "origin"))) == "origin"


@pytest.mark.parametrize(
    ("local", "explicit", "detail"),
    (
        (local_with_remotes(()), None, "no Git remote"),
        (local_with_remotes(("first", "second")), None, "without origin"),
        (local_with_remotes(("origin",), git_push="missing"), None, "missing"),
        (local_with_remotes(("origin",)), "missing", "missing"),
    ),
)
def test_remote_resolution_fails_closed_without_falling_through(
    local: sync.LocalObservation, explicit: str | None, detail: str
) -> None:
    with pytest.raises(sync.RemoteResolutionError, match=detail):
        sync.resolve_remote(local, explicit)


def test_push_url_resolution_rejects_multiple_destinations(monkeypatch) -> None:
    def multiple_push_urls(command: list[str]) -> str:
        assert command == [
            "git",
            "--git-dir=/git",
            "remote",
            "get-url",
            "--push",
            "--all",
            "origin",
        ]
        return "ssh://github.com/owner/one.git\nssh://github.com/owner/two.git\n"

    monkeypatch.setattr(sync, "_run", multiple_push_urls)

    with pytest.raises(sync.SourceMismatch, match="one unambiguous push URL"):
        sync.resolve_push_url(local_with_remotes(("origin",)), "origin")


def test_json_lines_preserves_unicode_line_and_paragraph_separators() -> None:
    records = (
        {"description": "before\u2028middle\u2029after"},
        {"description": "next"},
    )
    output = (
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n"
    )

    assert sync._json_lines(output, "jj log") == records


def test_observation_is_pinned_and_does_not_snapshot_working_copy(
    jj_repo: Path,
) -> None:
    # An untracked edit distinguishes a genuinely read-only observer from one that
    # silently snapshots before adding --at-op to later commands.
    dirty = jj_repo / "untracked"
    dirty.write_text("must remain unobserved\n")
    before = (
        op_id(jj_repo),
        all_refs(jj_repo),
        jj(
            jj_repo,
            "--ignore-working-copy",
            "workspace",
            "list",
            "-T",
            'name ++ "\\t" ++ target.commit_id()',
        ),
        jj(jj_repo, "--ignore-working-copy", "bookmark", "list", "--all"),
        jj(jj_repo, "--ignore-working-copy", "config", "list", "git.push"),
        dirty.read_bytes(),
    )

    observed = sync.observe_local(
        jj_repo,
        revision="topic | parents(topic)",
        config_keys=("git.push",),
    )

    after = (
        op_id(jj_repo),
        all_refs(jj_repo),
        jj(
            jj_repo,
            "--ignore-working-copy",
            "workspace",
            "list",
            "-T",
            'name ++ "\\t" ++ target.commit_id()',
        ),
        jj(jj_repo, "--ignore-working-copy", "bookmark", "list", "--all"),
        jj(jj_repo, "--ignore-working-copy", "config", "list", "git.push"),
        dirty.read_bytes(),
    )
    assert after == before
    assert observed.operation_id == before[0]
    assert observed.effective_config == (("git.push", "origin"),)
    assert dict((item.name, item.target) for item in observed.local_bookmarks)["topic"]
    assert len(observed.commits) == 2


def test_observation_and_state_work_with_non_colocated_bare_store(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    run("jj", "git", "init", "--no-colocate", repo)
    jj(repo, "config", "set", "--repo", "user.name", "Test User")
    jj(repo, "config", "set", "--repo", "user.email", "test@example.com")
    jj(repo, "config", "set", "--repo", "git.push", "origin")
    jj(repo, "describe", "-m", "Topic")
    jj(repo, "bookmark", "create", "topic", "-r", "@")

    observed = sync.observe_local(repo, revision="topic", config_keys=("git.push",))
    state_oid = sync.cas_write_state(repo, None, sync.EMPTY_STATE)

    assert Path(observed.git_common_dir).is_dir()
    assert not (repo / ".git").exists()
    assert sync.read_state(repo) == (state_oid, sync.EMPTY_STATE)


def test_real_jj_observation_distinguishes_bookmark_and_tracking_sources(
    jj_repo: Path, tmp_path: Path
) -> None:
    remote = tmp_path / "remote.git"
    run("git", "init", "--bare", remote)
    run("git", "-C", jj_repo, "remote", "add", "origin", remote)
    jj(jj_repo, "config", "set", "--repo", "git.private-commits", "none()")
    jj(jj_repo, "git", "push", "--bookmark", "topic")
    commit_id = jj(jj_repo, "log", "-r", "topic", "--no-graph", "-T", "commit_id")

    tracked = sync.observe_local(
        jj_repo, revision="all()", config_keys=("git.push",)
    )
    assert sync.LocalBookmark("topic", sync.CommitTarget(commit_id)) in (
        tracked.local_bookmarks
    )
    assert (
        sync.JjRemoteBookmark(
            "origin",
            "topic",
            sync.CommitTarget(commit_id),
            sync.TrackingState.TRACKED,
        )
        in tracked.remote_bookmarks
    )
    assert (
        sync.GitRemoteTrackingRef("refs/remotes/origin/topic", commit_id)
        in tracked.git_remote_tracking_refs
    )

    jj(jj_repo, "bookmark", "untrack", "topic@origin")
    untracked = sync.observe_local(
        jj_repo, revision="all()", config_keys=("git.push",)
    )
    assert (
        sync.JjRemoteBookmark(
            "origin",
            "topic",
            sync.CommitTarget(commit_id),
            sync.TrackingState.UNTRACKED,
        )
        in untracked.remote_bookmarks
    )
    assert (
        sync.GitRemoteTrackingRef("refs/remotes/origin/topic", commit_id)
        in untracked.git_remote_tracking_refs
    )

    jj(jj_repo, "bookmark", "delete", "topic")
    absent = sync.observe_local(
        jj_repo, revision="all()", config_keys=("git.push",)
    )
    assert all(bookmark.name != "topic" for bookmark in absent.local_bookmarks)


def test_real_jj_observation_parses_conflicted_local_bookmark(jj_repo: Path) -> None:
    original = jj(jj_repo, "log", "-r", "topic", "--no-graph", "-T", "commit_id")
    jj(jj_repo, "new", "root()")
    jj(jj_repo, "describe", "-m", "Second")
    second = jj(jj_repo, "log", "-r", "@", "--no-graph", "-T", "commit_id")
    jj(jj_repo, "new", "root()")
    jj(jj_repo, "describe", "-m", "Third")
    third = jj(jj_repo, "log", "-r", "@", "--no-graph", "-T", "commit_id")
    operation = op_id(jj_repo)

    jj(
        jj_repo,
        f"--at-op={operation}",
        "bookmark",
        "set",
        "topic",
        "-r",
        second,
        "--allow-backwards",
    )
    jj(
        jj_repo,
        f"--at-op={operation}",
        "bookmark",
        "set",
        "topic",
        "-r",
        third,
        "--allow-backwards",
    )
    # Reconcile the deliberately concurrent fixture operations before invoking
    # the observer, which must pin one operation without doing that mutation.
    jj(jj_repo, "bookmark", "list", "--all")

    observed = sync.observe_local(
        jj_repo, revision="all()", config_keys=("git.push",)
    )
    topic = next(
        bookmark for bookmark in observed.local_bookmarks if bookmark.name == "topic"
    )
    assert isinstance(topic.target, sync.BookmarkConflict)
    assert topic.target.removed_commit_ids == (original,)
    assert set(topic.target.added_commit_ids) == {second, third}


def test_private_state_cas_and_recovery_observation_work_from_linked_workspace(
    jj_repo: Path, tmp_path: Path
) -> None:
    workspace = tmp_path / "second"
    jj(jj_repo, "workspace", "add", workspace, "--name", "second")
    repository = sync.GitHubRepositoryId("github.com", "R_repo")
    pr = sync.PullRequestId(repository, 7)
    ref = sync.RemoteBranchRef(repository, "refs/heads/topic")
    state = sync.TrackedState(
        (sync.TrackedStack(repository, "main", (pr,)),),
        (sync.LastPublishedHead(pr, ref, "a" * 40),),
    )
    serialized_stack = json.loads(sync.state_to_json(state))["stacks"][0]
    assert set(serialized_stack) == {"repository", "base_branch", "ordered_prs"}
    assert serialized_stack["base_branch"] == "main"

    first_oid = sync.cas_write_state(workspace, None, state)
    observed_ref, observed_state = sync.read_state(jj_repo)

    assert observed_ref == first_oid
    assert observed_state == state
    assert sync.git_common_dir(workspace) == sync.git_common_dir(jj_repo)

    second_oid = sync.cas_write_state(jj_repo, first_oid, sync.EMPTY_STATE)
    with pytest.raises(sync.ConcurrentUpdate):
        sync.cas_write_state(jj_repo, first_oid, state)
    assert sync.read_state(jj_repo) == (second_oid, sync.EMPTY_STATE)

    common = sync.git_common_dir(jj_repo)
    recovery_blob = run(
        "git", f"--git-dir={common}", "hash-object", "-w", "--stdin", cwd=jj_repo
    )
    # hash-object above hashes empty stdin; only presence matters at this stage.
    run("git", f"--git-dir={common}", "update-ref", sync.RECOVERY_REF, recovery_blob)
    tool_state = sync.observe_tool_state(jj_repo)
    assert tool_state.recovery_blob_oid == recovery_blob
    assert tool_state.state_blob_oid == second_oid
    assert tool_state.state == sync.EMPTY_STATE


def test_state_cas_distinguishes_storage_failure_from_stale_ref(
    jj_repo: Path, monkeypatch
) -> None:
    original_run = subprocess.run

    def fail_update_ref(args, **kwargs):
        if "update-ref" in args:
            return subprocess.CompletedProcess(
                args, 128, stdout="", stderr="cannot lock ref: permission denied"
            )
        return original_run(args, **kwargs)

    monkeypatch.setattr(sync.subprocess, "run", fail_update_ref)

    with pytest.raises(
        sync.Error, match="could not update state ref.*permission denied"
    ):
        sync.cas_write_state(jj_repo, None, sync.EMPTY_STATE)


def test_repository_lock_is_shared_by_workspaces(jj_repo: Path, tmp_path: Path) -> None:
    workspace = tmp_path / "second"
    jj(jj_repo, "workspace", "add", workspace, "--name", "second")
    with sync.repository_lock(jj_repo):
        with pytest.raises(sync.LockBusy):
            with sync.repository_lock(workspace):
                pytest.fail("second workspace acquired the shared-store lock")
        lock_path = sync.git_common_dir(workspace) / "jj-stack.lock"
        child = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import fcntl, os, sys; "
                    "fd=os.open(sys.argv[1], os.O_RDWR); "
                    "fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)"
                ),
                os.fspath(lock_path),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        assert child.returncode != 0
    with sync.repository_lock(workspace):
        pass


def test_state_round_trip_uses_unversioned_shape() -> None:
    payload = sync.state_to_json(sync.EMPTY_STATE)
    assert json.loads(payload) == {
        "detached_associations": [],
        "last_adopted_heads": [],
        "last_published_boundaries": [],
        "last_published_heads": [],
        "stacks": [],
    }
    assert sync.parse_state(payload) == sync.EMPTY_STATE


@pytest.mark.parametrize(
    "payload",
    (
        "{}",
        '{"entries":[]}',
        '{"entries":[{"kind":"unknown"}]}',
        '{"entries":[{"kind":"metadata","identity":"x",'
        '"possibly_live":false,"repository":{"host":"github.com",'
        '"node_id":"R"},"pr":7,"title":"t","body":"b",'
        '"expected_title":"old","expected_body":"b","extra":true}]}',
    ),
)
def test_recovery_journal_rejects_ambiguous_shapes(payload: str) -> None:
    with pytest.raises(sync.Error, match="invalid refs/jj-stack/recovery payload"):
        sync.parse_recovery(payload)


@pytest.mark.parametrize(
    "payload",
    (
        "[]",
        '{"schema_version":true,"stacks":[],"last_published_heads":[]}',
        '{"schema_version":1.0,"stacks":[],"last_published_heads":[]}',
        '{"stacks":"","last_published_heads":[]}',
        '{"stacks":[],"last_published_heads":""}',
        (
            '{"stacks":[{"repository":{"host":"github.com","node_id":"R"},'
            '"base_branch":"main","ordered_prs":""}],'
            '"last_published_heads":[]}'
        ),
        (
            '{"stacks":[],"last_published_heads":[{"pr":{"repository":'
            '{"host":"github.com","node_id":"R"},"number":7},"ref":'
            '{"repository":{"host":"github.com","node_id":"R"},"full_name":7},'
            '"verified_commit_id":"abc"}]}'
        ),
    ),
)
def test_parse_state_rejects_malformed_shapes(payload: str) -> None:
    with pytest.raises(sync.Error, match="invalid refs/jj-stack/state payload"):
        sync.parse_state(payload)


def test_state_rejects_malformed_scalars_and_duplicate_publication_authority() -> None:
    repository = sync.GitHubRepositoryId("github.com", "R_repo")
    pr = sync.PullRequestId(repository, 7)
    publication = sync.LastPublishedHead(
        pr, sync.RemoteBranchRef(repository, "refs/heads/topic"), "a" * 40
    )
    duplicate = sync.TrackedState((), (publication, publication))

    with pytest.raises(ValueError, match="ambiguous"):
        sync.state_to_json(duplicate)
    with pytest.raises(sync.Error, match="invalid .*state"):
        sync.parse_state(json.dumps(dataclasses.asdict(duplicate)))

    malformed = dataclasses.replace(publication, verified_commit_id=7)
    with pytest.raises(ValueError, match="nonempty strings"):
        sync.state_to_json(sync.TrackedState((), (malformed,)))


def test_membership_variants_reject_contradictory_server_stack_results() -> None:
    repository = sync.GitHubRepositoryId("github.com", "R_repo")
    selected = sync.PullRequestId(repository, 1)
    other = sync.PullRequestId(repository, 2)
    foreign = sync.PullRequestId(
        sync.GitHubRepositoryId("github.com", "R_other"), 3
    )

    assert sync.StandalonePullRequest(selected).pr == selected
    with pytest.raises(ValueError, match="positive integer"):
        sync.GitHubStackId(repository, 0)
    with pytest.raises(ValueError, match="nonempty"):
        sync.ServerStackMembership(
            selected,
            sync.GitHubStackSummary(sync.GitHubStackId(repository, 1), "stack", "main"),
            (),
        )
    for invalid in (0, -1, True):
        with pytest.raises(ValueError, match="positive integer"):
            sync.GitHubStackId(repository, invalid)
    with pytest.raises(ValueError, match="must belong"):
        sync.ServerStackMembership(
            selected,
            sync.GitHubStackSummary(sync.GitHubStackId(repository, 1), "stack", "main"),
            (other,),
        )
    with pytest.raises(ValueError, match="duplicates"):
        sync.ServerStackMembership(
            selected,
            sync.GitHubStackSummary(sync.GitHubStackId(repository, 1), "stack", "main"),
            (selected, selected),
        )
    with pytest.raises(ValueError, match="one repository"):
        sync.ServerStackMembership(
            selected,
            sync.GitHubStackSummary(sync.GitHubStackId(repository, 1), "stack", "main"),
            (selected, foreign),
        )
    with pytest.raises(ValueError, match="one repository"):
        sync.ServerStackMembership(
            selected,
            sync.GitHubStackSummary(
                sync.GitHubStackId(foreign.repository, 1), "stack", "main"
            ),
            (selected,),
        )

    with pytest.raises(ValueError, match="positive integer"):
        sync.PullRequestId(repository, 0)


def test_remote_branch_ref_requires_heads_namespace() -> None:
    repository = sync.GitHubRepositoryId("github.com", "R_repo")

    assert sync.RemoteBranchRef(repository, "refs/heads/topic").full_name == (
        "refs/heads/topic"
    )
    with pytest.raises(ValueError, match="refs/heads"):
        sync.RemoteBranchRef(repository, "refs/tags/topic")
    with pytest.raises(ValueError, match="refs/heads"):
        sync.RemoteBranchRef(repository, "topic")
    with pytest.raises(ValueError, match="refs/heads"):
        sync.RemoteBranchRef(repository, "refs/heads/")


def pr_source_record(**changes: object) -> dict[str, object]:
    record: dict[str, object] = {
        "id": "PR_node",
        "number": 7,
        "state": "OPEN",
        "isDraft": False,
        "headRepository": {"id": "R_repo"},
        "headRefName": "topic",
        "headRefOid": "1" * 40,
        "baseRefName": "main",
        "baseRefOid": "0" * 40,
        "autoMergeRequest": None,
        "mergeQueueEntry": None,
        "title": "Title",
        "body": None,
        "stack": None,
    }
    record.update(changes)
    return record


def test_github_source_parsing_distinguishes_confirmed_null_and_stack_membership() -> (
    None
):
    repository = sync.GitHubRepository(
        sync.GitHubRepositoryId("github.com", "R_repo"),
        "owner/repo",
        "https://github.com/owner/repo",
        "main",
    )

    standalone = sync._parse_github_pull_request(pr_source_record(), repository, 7)
    stacked = sync._parse_github_pull_request(
        pr_source_record(
            stack={"id": "STACK_node", "number": 3, "baseRefName": "main"}
        ),
        repository,
        7,
    )

    assert standalone.stack is None
    assert standalone.identity == sync.PullRequestId(repository.identity, 7)
    assert standalone.node_id == "PR_node"
    assert standalone.body == ""
    assert stacked.stack == sync.GitHubStackSummary(
        sync.GitHubStackId(repository.identity, 3), "STACK_node", "main"
    )

    deleted_fork = sync._parse_github_pull_request(
        pr_source_record(headRepository=None), repository, 7
    )
    assert deleted_fork.head_repository is None

    missing_oids = sync._parse_github_pull_request(
        pr_source_record(headRefOid=None, baseRefOid=None), repository, 7
    )
    assert missing_oids.head_oid is None
    assert missing_oids.base_oid is None


@pytest.mark.parametrize(
    ("changes", "error"),
    (
        ({"number": 8}, sync.SourceMismatch),
        ({"isDraft": 0}, sync.MalformedSource),
        ({"state": "UNKNOWN"}, sync.MalformedSource),
        ({"stack": {"id": "STACK_node"}}, sync.MalformedSource),
    ),
)
def test_github_source_rejects_incomplete_mismatched_and_malformed_records(
    changes: dict[str, object], error: type[Exception]
) -> None:
    repository = sync.GitHubRepository(
        sync.GitHubRepositoryId("github.com", "R_repo"),
        "owner/repo",
        "https://github.com/owner/repo",
        "main",
    )
    with pytest.raises(error):
        sync._parse_github_pull_request(pr_source_record(**changes), repository, 7)


def test_github_json_transport_preserves_exact_strings_and_separates_status() -> None:
    title = "control:\u2028 paragraph:\u2029 replacement:\ufffd"
    body = json.dumps({"title": title}, ensure_ascii=False).encode()

    assert sync._github_json_response(
        sync.GitHubHttpResponse(200, (("X-Test", "yes"),), body), "test"
    ) == {"title": title}
    with pytest.raises(sync.GitHubHttpError, match="HTTP 422"):
        sync._github_json_response(sync.GitHubHttpResponse(422, (), body), "test")
    with pytest.raises(sync.MalformedSource, match="invalid JSON"):
        sync._github_json_response(sync.GitHubHttpResponse(200, (), b"{"), "test")


def test_github_token_is_secret_safe_and_noninteractive(monkeypatch) -> None:
    captured = {}

    def fake_run(args, **kwargs):
        captured.update(args=args, kwargs=kwargs)
        return subprocess.CompletedProcess(args, 0, "secret-token\n", "")

    monkeypatch.setenv("GH_DEBUG", "api")
    monkeypatch.setenv("DEBUG", "1")
    monkeypatch.setattr(sync.subprocess, "run", fake_run)

    assert sync._github_token("github.com") == "secret-token"
    assert captured["args"] == ["gh", "auth", "token", "--hostname", "github.com"]
    assert captured["kwargs"]["stdin"] is subprocess.DEVNULL
    assert captured["kwargs"]["env"]["GH_PROMPT_DISABLED"] == "1"
    assert "GH_DEBUG" not in captured["kwargs"]["env"]
    assert "DEBUG" not in captured["kwargs"]["env"]
    assert "secret-token" not in captured["args"]


def test_github_routing_accepts_canonical_ssh_aliases_only(monkeypatch) -> None:
    def fake_run(args, **_kwargs):
        host = args[-1]
        output = {
            "work-github": "hostname github.com\nport 22\n",
            "github-443": "hostname ssh.github.com\nport 443\n",
            "enterprise": "hostname github.example.com\nport 22\n",
        }[host]
        return subprocess.CompletedProcess(args, 0, output, "")

    monkeypatch.setattr(sync.subprocess, "run", fake_run)

    assert sync._github_repository_locator("git@work-github:owner/repo.git") == (
        "github.com",
        "owner/repo",
    )
    assert sync._github_repository_locator("ssh://git@github-443/owner/repo.git") == (
        "github.com",
        "owner/repo",
    )
    with pytest.raises(sync.SourceUnavailable, match="canonical GitHub.com"):
        sync._github_repository_locator("git@enterprise:owner/repo.git")
    with pytest.raises(sync.SourceUnavailable, match="unsupported GitHub host"):
        sync._github_repository_locator("https://github.example.com/owner/repo.git")


def test_github_transport_rejects_errors_partial_data_and_repository_mismatch(
    monkeypatch,
) -> None:
    repository = sync.GitHubRepository(
        sync.GitHubRepositoryId("github.com", "R_repo"),
        "owner/repo",
        "https://github.com/owner/repo",
        "main",
    )

    monkeypatch.setattr(
        sync, "_github_graphql", lambda *_args, **_kwargs: {"errors": []}
    )
    with pytest.raises(sync.MalformedSource):
        sync._read_github_pull_request(repository, 7)

    monkeypatch.setattr(
        sync,
        "_github_graphql",
        lambda *_args, **_kwargs: {
            "data": {"repository": {"id": "R_other", "pullRequest": pr_source_record()}}
        },
    )
    with pytest.raises(sync.SourceMismatch):
        sync._read_github_pull_request(repository, 7)

    monkeypatch.setattr(
        sync,
        "_github_graphql",
        lambda *_args, **_kwargs: {"data": {"repository": None}},
    )
    with pytest.raises(sync.IncompleteSource):
        sync._read_github_pull_request(repository, 7)


def test_fake_github_derives_stack_associations_and_preserves_requested_order() -> None:
    repository = sync.GitHubRepository(
        sync.GitHubRepositoryId("github.com", "R_repo"),
        "owner/repo",
        "https://github.com/owner/repo",
        None,
    )
    first = sync._parse_github_pull_request(pr_source_record(), repository, 7)
    second = sync._parse_github_pull_request(
        pr_source_record(id="PR_second", number=8, headRefName="second"),
        repository,
        8,
    )
    stack = sync.GitHubStack(
        sync.GitHubStackId(repository.identity, 3),
        "STACK_node",
        "main",
        (first.identity, second.identity),
    )
    server = FakeGitHubServer()
    server.seed_repository(repository, aliases=("ssh://github/owner/repo",))
    server.seed_pull_request(first)
    server.seed_pull_request(second)
    server.seed_stack(stack)
    client = FakeGitHubClient(server)

    observed = client.pull_requests((second.identity, first.identity, second.identity))

    assert tuple(item.identity for item in observed) == (
        second.identity,
        first.identity,
        second.identity,
    )
    assert all(
        item.stack
        == sync.GitHubStackSummary(stack.identity, stack.node_id, stack.base_branch)
        for item in observed
    )
    assert client.stack(repository, stack.identity) == stack
    assert client.resolve_repository(repository.identity) == repository
    assert client.resolve_repository("ssh://github/owner/repo") == repository


def test_fake_git_transport_applies_atomic_git_lease_semantics() -> None:
    repository = sync.GitHubRepository(
        sync.GitHubRepositoryId("github.com", "R_repo"),
        "owner/repo",
        "https://github.com/owner/repo",
        "main",
    )
    push_url = "/nonexistent/fake-remote.git"
    server = FakeGitHubServer()
    server.seed_repository(repository, aliases=(push_url,))
    server.seed_branch(repository.identity, "one", "1" * 40)
    server.seed_branch(repository.identity, "two", "2" * 40)
    transport = FakeGitTransport(server)
    one = sync.RemoteBranchRef(repository.identity, "refs/heads/one")
    two = sync.RemoteBranchRef(repository.identity, "refs/heads/two")

    # The stale lease on the already-up-to-date ref is ignored, while the
    # changing member is applied atomically.
    transport.push_exact_head_updates(
        push_url,
        (
            sync.PlannedHeadUpdate(one, "9" * 40, "1" * 40, sync.FastForward()),
            sync.PlannedHeadUpdate(two, "2" * 40, "3" * 40, sync.FastForward()),
        ),
    )
    assert transport.observe_live_refs(
        push_url, repository.identity, (one.full_name, two.full_name)
    ) == (
        sync.LiveRemoteRef(one, "1" * 40),
        sync.LiveRemoteRef(two, "3" * 40),
    )

    with pytest.raises(sync.GitPushError, match="stale lease"):
        transport.push_exact_head_updates(
            push_url,
            (
                sync.PlannedHeadUpdate(one, "0" * 40, "4" * 40, sync.FastForward()),
                sync.PlannedHeadUpdate(two, "3" * 40, "5" * 40, sync.FastForward()),
            ),
        )
    assert tuple(
        item.commit_id
        for item in transport.observe_live_refs(
            push_url, repository.identity, (one.full_name, two.full_name)
        )
    ) == ("1" * 40, "3" * 40)


def test_fake_ref_transactions_refresh_only_live_open_pr_observations() -> None:
    repository = sync.GitHubRepository(
        sync.GitHubRepositoryId("github.com", "R_repo"),
        "owner/repo",
        "https://github.com/owner/repo",
        "main",
    )
    push_url = "/nonexistent/fake-remote.git"
    server = FakeGitHubServer()
    server.seed_repository(repository, aliases=(push_url,))
    server.seed_branch(repository.identity, "main", "0" * 40)
    server.seed_branch(repository.identity, "topic", "1" * 40)
    client = FakeGitHubClient(server)
    transport = FakeGitTransport(server)
    identity = client.create_pull_request(
        repository,
        head_branch="topic",
        base_branch="main",
        title="Topic",
        body="",
        draft=False,
    )
    topic = sync.RemoteBranchRef(repository.identity, "refs/heads/topic")

    transport.push_exact_head_updates(
        push_url,
        (
            sync.PlannedHeadUpdate(
                topic, "1" * 40, "2" * 40, sync.FastForward()
            ),
        ),
    )
    assert client.pull_requests((identity,))[0].head_oid == "2" * 40

    server.delete_branch(repository.identity, "topic")
    assert transport.observe_live_refs(
        push_url, repository.identity, (topic.full_name,)
    )[0].commit_id is None
    assert client.pull_requests((identity,))[0].head_oid == "2" * 40

    transport.push_absent_heads(
        push_url,
        repository.identity,
        (sync.NewPullRequestGoal("3" * 40, "topic", "main", "Topic", ""),),
    )
    assert client.pull_requests((identity,))[0].head_oid == "3" * 40

    server.close_pull_request(identity)
    server.move_branch(repository.identity, "topic", "4" * 40)
    assert client.pull_requests((identity,))[0].head_oid == "3" * 40


def test_fake_ref_transactions_respect_fork_identity_and_retarget_base_oid() -> None:
    base_id = sync.GitHubRepositoryId("github.com", "R_base")
    fork_id = sync.GitHubRepositoryId("github.com", "R_fork")
    base = sync.GitHubRepository(
        base_id, "owner/base", "https://github.com/owner/base", "main"
    )
    fork = sync.GitHubRepository(
        fork_id, "contributor/fork", "https://github.com/contributor/fork", "main"
    )
    server = FakeGitHubServer()
    server.seed_repository(base, aliases=("/base.git",))
    server.seed_repository(fork, aliases=("/fork.git",))
    server.seed_branch(base_id, "main", "0" * 40)
    server.seed_branch(base_id, "next", "1" * 40)
    server.seed_branch(base_id, "topic", "2" * 40)
    server.seed_branch(fork_id, "topic", "3" * 40)
    identity = sync.PullRequestId(base_id, 1)
    server.seed_pull_request(sync.GitHubPullRequest(
        identity,
        "PR_1",
        sync.PullRequestState.OPEN,
        False,
        fork_id,
        "topic",
        "3" * 40,
        "main",
        "0" * 40,
        False,
        False,
        "Topic",
        "",
        None,
    ))
    client = FakeGitHubClient(server)

    server.move_branch(base_id, "topic", "4" * 40)
    assert client.pull_requests((identity,))[0].head_oid == "3" * 40
    server.move_branch(fork_id, "topic", "5" * 40)
    assert client.pull_requests((identity,))[0].head_oid == "5" * 40

    client.update_pull_request(base, identity, base_branch="next")
    retargeted = client.pull_requests((identity,))[0]
    assert (retargeted.base_branch, retargeted.base_oid) == ("next", "1" * 40)


def test_fake_git_transport_faults_and_explicit_rejection_are_coherent() -> None:
    repository = sync.GitHubRepository(
        sync.GitHubRepositoryId("github.com", "R_repo"),
        "owner/repo",
        "https://github.com/owner/repo",
        "main",
    )
    push_url = "/nonexistent/fake-remote.git"
    server = FakeGitHubServer()
    server.seed_repository(repository, aliases=(push_url,))
    server.seed_branch(repository.identity, "topic", "1" * 40)
    transport = FakeGitTransport(server)
    ref = sync.RemoteBranchRef(repository.identity, "refs/heads/topic")

    with transport.lose_response(sync.GitTransport.push_exact_head_updates):
        with pytest.raises(sync.GitPushError, match="lost response"):
            transport.push_exact_head_updates(
                push_url,
                (sync.PlannedHeadUpdate(
                    ref, "1" * 40, "2" * 40, sync.FastForward()
                ),),
            )
    assert transport.observe_live_refs(
        push_url, repository.identity, (ref.full_name,)
    )[0].commit_id == "2" * 40

    with transport.hold_next(sync.GitTransport.push_exact_head_updates) as pending:
        with pytest.raises(sync.GitPushError, match="held request"):
            transport.push_exact_head_updates(
                push_url,
                (sync.PlannedHeadUpdate(
                    ref, "2" * 40, "3" * 40, sync.FastForward()
                ),),
            )
    server.move_branch(repository.identity, "topic", "4" * 40)
    with pytest.raises(sync.GitPushError, match="stale lease"):
        pending.release()

    server.set_ref_update_policy(ref, RefUpdatePolicy.REJECT)
    with pytest.raises(sync.GitPushError, match="ref rejection"):
        transport.push_exact_head_updates(
            push_url,
            (sync.PlannedHeadUpdate(
                ref, "4" * 40, "5" * 40, sync.FastForward()
            ),),
        )


def test_complete_stack_source_validates_order_membership_and_base() -> None:
    repository = sync.GitHubRepository(
        sync.GitHubRepositoryId("github.com", "R_repo"),
        "owner/repo",
        "https://github.com/owner/repo",
        "main",
    )
    response = {
        "id": 99,
        "number": 17,
        "node_id": "STACK_node",
        "url": "https://api.github.com/repos/owner/repo/stacks/17",
        "base": {"ref": "main"},
        "open": True,
        "created_at": "2026-01-01T00:00:00Z",
        "pull_requests": [
            {
                "number": number,
                "state": "open",
                "draft": False,
                "merged_at": None,
                "head": {"ref": f"topic-{number}", "sha": str(number) * 40},
            }
            for number in (1, 2, 3)
        ],
    }
    identity = sync.GitHubStackId(repository.identity, 17)

    stack = sync._parse_github_stack(response, repository.identity, identity)

    assert stack.identity == identity
    assert stack.node_id == "STACK_node"
    assert stack.base_branch == "main"
    assert tuple(pr.number for pr in stack.pull_requests) == (1, 2, 3)

    response["pull_requests"][2]["number"] = 2
    with pytest.raises(sync.SourceMismatch, match="duplicate"):
        sync._parse_github_stack(response, repository.identity, identity)

    response["pull_requests"][2]["number"] = 3
    response["number"] = 18
    with pytest.raises(sync.SourceMismatch, match="stack number"):
        sync._parse_github_stack(response, repository.identity, identity)


def test_live_ref_observation_uses_destination_and_preserves_confirmed_absence(
    tmp_path: Path,
) -> None:
    remote = tmp_path / "remote.git"
    source = tmp_path / "source"
    run("git", "init", "--bare", remote)
    run("git", "init", source)
    run("git", "-C", source, "config", "user.name", "Test")
    run("git", "-C", source, "config", "user.email", "test@example.com")
    (source / "file").write_text("content\n")
    run("git", "-C", source, "add", "file")
    run("git", "-C", source, "commit", "-m", "base")
    oid = run("git", "-C", source, "rev-parse", "HEAD")
    run("git", "-C", source, "push", os.fspath(remote), "HEAD:refs/heads/main")
    repository = sync.GitHubRepositoryId("github.com", "R_repo")

    refs = sync.observe_live_refs(
        os.fspath(remote),
        repository,
        ("refs/heads/main", "refs/heads/missing"),
    )

    assert refs == (
        sync.LiveRemoteRef(sync.RemoteBranchRef(repository, "refs/heads/main"), oid),
        sync.LiveRemoteRef(
            sync.RemoteBranchRef(repository, "refs/heads/missing"), None
        ),
    )


def test_real_git_transport_matches_atomic_up_to_date_lease_semantics(
    tmp_path: Path,
) -> None:
    remote = tmp_path / "remote.git"
    source = tmp_path / "source"
    run("git", "init", "--bare", remote)
    run("git", "init", source)
    run("jj", "git", "init", "--colocate", source)
    run("git", "-C", source, "config", "user.name", "Test")
    run("git", "-C", source, "config", "user.email", "test@example.com")
    oids: list[str] = []
    for index in range(5):
        (source / "file").write_text(f"{index}\n")
        run("git", "-C", source, "add", "file")
        run("git", "-C", source, "commit", "-m", f"commit {index}")
        oids.append(run("git", "-C", source, "rev-parse", "HEAD"))
    run(
        "git",
        "-C",
        source,
        "push",
        remote,
        f"{oids[0]}:refs/heads/one",
        f"{oids[1]}:refs/heads/two",
    )

    repository = sync.GitHubRepositoryId("github.com", "R_repo")
    one = sync.RemoteBranchRef(repository, "refs/heads/one")
    two = sync.RemoteBranchRef(repository, "refs/heads/two")
    transport = sync.SubprocessGitTransport(source)

    transport.push_exact_head_updates(
        os.fspath(remote),
        (
            sync.PlannedHeadUpdate(one, oids[4], oids[0], sync.FastForward()),
            sync.PlannedHeadUpdate(two, oids[1], oids[2], sync.FastForward()),
        ),
    )
    assert tuple(item.commit_id for item in transport.observe_live_refs(
        os.fspath(remote), repository, (one.full_name, two.full_name)
    )) == (oids[0], oids[2])

    with pytest.raises(sync.GitPushError):
        transport.push_exact_head_updates(
            os.fspath(remote),
            (
                sync.PlannedHeadUpdate(one, oids[4], oids[1], sync.FastForward()),
                sync.PlannedHeadUpdate(two, oids[2], oids[3], sync.FastForward()),
            ),
        )
    assert tuple(item.commit_id for item in transport.observe_live_refs(
        os.fspath(remote), repository, (one.full_name, two.full_name)
    )) == (oids[0], oids[2])

    transport.push_absent_heads(
        os.fspath(remote),
        repository,
        (
            sync.NewPullRequestGoal(oids[0], "one", "main", "One", ""),
            sync.NewPullRequestGoal(oids[3], "three", "main", "Three", ""),
        ),
    )
    observed = transport.observe_live_refs(
        os.fspath(remote),
        repository,
        ("refs/heads/one", "refs/heads/three"),
    )
    assert tuple(item.commit_id for item in observed) == (oids[0], oids[3])

    with pytest.raises(sync.GitPushError):
        transport.push_absent_heads(
            os.fspath(remote),
            repository,
            (
                sync.NewPullRequestGoal(oids[4], "one", "main", "One", ""),
                sync.NewPullRequestGoal(oids[4], "four", "main", "Four", ""),
            ),
        )
    assert transport.observe_live_refs(
        os.fspath(remote), repository, ("refs/heads/four",)
    )[0].commit_id is None


def test_observation_record_parsers_reject_wrong_types_and_contradictions() -> None:
    bookmark = {
        "name": "topic",
        "remote": "",
        "conflict": False,
        "target": "a" * 40,
        "removed": [],
        "added": ["a" * 40],
        "tracked": False,
    }
    commit = {
        "commit_id": "a" * 40,
        "parent_commit_ids": ["b" * 40],
        "change": "change",
        "description": "Title",
        "conflicts": False,
        "hidden": False,
    }

    _, _, absent, _ = sync._bookmark_record({**bookmark, "target": "", "added": []})
    assert isinstance(absent, sync.AbsentBookmarkTarget)
    with pytest.raises(sync.Error, match="jj bookmark list"):
        sync._bookmark_record({**bookmark, "conflict": "false"})
    with pytest.raises(sync.Error, match="jj bookmark list"):
        sync._bookmark_record({**bookmark, "conflict": True})
    with pytest.raises(sync.Error, match="jj log"):
        sync._commit_record({**commit, "commit_id": 7})
    with pytest.raises(sync.Error, match="jj log"):
        sync._commit_record({**commit, "conflicts": "false"})
    with pytest.raises(sync.Error, match="jj log"):
        sync._commit_record({**commit, "hidden": "false"})


def snapshot(*, desired: str, live: str, parent: str, recovery_oid: str | None = None):
    repository = sync.GitHubRepositoryId("github.com", "R_repo")
    pr_key = sync.PullRequestId(repository, 7)
    head_ref = sync.RemoteBranchRef(repository, "refs/heads/topic")
    base_ref = sync.RemoteBranchRef(repository, "refs/heads/main")
    commit = sync.ObservedCommit(
        desired, (parent,), "change", "Desired title\n\nDesired body", False, False
    )
    parent_commit = sync.ObservedCommit(parent, ("b" * 40,), "old", "Old", False, False)
    local = sync.LocalObservation(
        "/repo",
        "op",
        (("git.push", "origin"),),
        (("default", desired),),
        (sync.LocalBookmark("topic", sync.CommitTarget(desired)),),
        (
            sync.JjRemoteBookmark(
                "origin", "topic", sync.CommitTarget(live), sync.TrackingState.TRACKED
            ),
        ),
        (sync.GitRemoteTrackingRef("refs/remotes/origin/topic", live),),
        (commit, parent_commit),
        "/git",
        ("origin",),
    )
    pr = sync.GitHubPullRequest(
        pr_key,
        "PR_node",
        sync.PullRequestState.OPEN,
        False,
        repository,
        "topic",
        live,
        "main",
        "b" * 40,
        False,
        False,
        "Desired title",
        "Desired body",
        None,
    )
    observed = sync.Snapshot(
        repository,
        "ssh://git@github.com/o/r.git",
        "origin",
        local,
        sync.ToolStateRead(None, sync.EMPTY_STATE, recovery_oid),
        (pr,),
        sync.StandalonePullRequest(pr_key),
        (
            sync.LiveRemoteRef(head_ref, live),
            sync.LiveRemoteRef(base_ref, "b" * 40),
        ),
    )
    return observed, pr_key


def fake_github(observed: sync.Snapshot) -> tuple[FakeGitHubServer, FakeGitHubClient]:
    repository = sync.GitHubRepository(
        observed.repository,
        "o/r",
        "https://github.com/o/r",
        None,
    )
    server = FakeGitHubServer()
    server.seed_repository(repository, aliases=(observed.push_url,))
    for live_ref in observed.live_refs:
        if live_ref.commit_id is not None:
            server.seed_branch(
                live_ref.ref.repository,
                live_ref.ref.full_name.removeprefix("refs/heads/"),
                live_ref.commit_id,
            )
    for pr in observed.pull_requests:
        server.seed_pull_request(dataclasses.replace(pr, stack=None))
    if isinstance(observed.membership, sync.ServerStackMembership):
        server.seed_stack(
            sync.GitHubStack(
                observed.membership.stack.identity,
                observed.membership.stack.node_id,
                observed.membership.stack.base_branch,
                observed.membership.ordered_prs,
            )
        )
    return server, FakeGitHubClient(server)


def selection(observed: sync.Snapshot, *prs: sync.PullRequestId) -> sync.StackSelection:
    base = (
        observed.membership.base_branch
        if isinstance(observed.membership, sync.ServerStackMembership)
        else "main"
    )
    return sync.StackSelection(
        base,
        tuple(
            sync.ExistingPRAssignment(pr, observed.local.commits[index].commit_id)
            for index, pr in enumerate(prs)
        ),
    )


def stacked_snapshot(
    *, count: int, merged_prefix: int = 0
) -> tuple[sync.Snapshot, sync.StackSelection]:
    repository = sync.GitHubRepositoryId("github.com", "R_repo")
    stack = sync.GitHubStackSummary(
        sync.GitHubStackId(repository, 17), "STACK_node", "main"
    )
    base = "b" * 40
    prs: list[sync.GitHubPullRequest] = []
    commits: list[sync.ObservedCommit] = []
    bookmarks: list[sync.LocalBookmark] = []
    live_refs = [
        sync.LiveRemoteRef(sync.RemoteBranchRef(repository, "refs/heads/main"), base)
    ]
    assignments: list[sync.ExistingPRAssignment] = []
    previous = base
    previous_branch = "main"
    for index in range(count):
        identity = sync.PullRequestId(repository, index + 1)
        commit_id = str(index + 1) * 40
        branch = f"topic-{index + 1}"
        state = (
            sync.PullRequestState.MERGED
            if index < merged_prefix
            else sync.PullRequestState.OPEN
        )
        prs.append(
            sync.GitHubPullRequest(
                identity,
                f"PR_{index + 1}",
                state,
                False,
                repository,
                branch,
                commit_id,
                previous_branch,
                previous,
                False,
                False,
                f"Title {index + 1}",
                f"Body {index + 1}",
                stack,
            )
        )
        live_refs.append(
            sync.LiveRemoteRef(
                sync.RemoteBranchRef(repository, f"refs/heads/{branch}"), commit_id
            )
        )
        if state is sync.PullRequestState.OPEN:
            commits.append(
                sync.ObservedCommit(
                    commit_id,
                    (previous,),
                    f"change-{index + 1}",
                    f"Title {index + 1}\n\nBody {index + 1}",
                    False,
                    False,
                )
            )
            bookmarks.append(sync.LocalBookmark(branch, sync.CommitTarget(commit_id)))
            assignments.append(sync.ExistingPRAssignment(identity, commit_id))
        previous = commit_id
        previous_branch = branch
    selected = prs[0].identity
    local = sync.LocalObservation(
        "/repo",
        "op",
        (("git.push", "origin"),),
        (("default", previous),),
        tuple(bookmarks),
        (),
        (),
        tuple(commits),
        "/git",
        ("origin",),
    )
    observed = sync.Snapshot(
        repository,
        "ssh://git@github.com/o/r.git",
        "origin",
        local,
        sync.ToolStateRead(None, sync.EMPTY_STATE, None),
        tuple(prs),
        sync.ServerStackMembership(
            selected,
            stack,
            tuple(pr.identity for pr in prs),
        ),
        tuple(live_refs),
    )
    return observed, sync.StackSelection("main", tuple(assignments))


def remote_restack_snapshots() -> tuple[sync.Snapshot, sync.Snapshot]:
    before, _selection = stacked_snapshot(count=2)
    old_ids = tuple(pr.head_oid for pr in before.pull_requests)
    new_ids = ("3" * 40, "4" * 40)
    base = "b" * 40
    tracked = sync.TrackedStack(
        before.repository,
        "main",
        tuple(pr.identity for pr in before.pull_requests),
    )
    boundary = sync.LastPublishedBoundary(
        before.pull_requests[0].identity,
        sync.RemoteBranchRef(before.repository, "refs/heads/main"),
        base,
    )
    state = sync.TrackedState((tracked,), (), last_published_boundaries=(boundary,))
    pull_requests = tuple(
        dataclasses.replace(
            pr,
            head_oid=new_ids[index],
            base_oid=base if index == 0 else new_ids[index - 1],
        )
        for index, pr in enumerate(before.pull_requests)
    )
    live_refs = tuple(
        dataclasses.replace(ref, commit_id=new_ids[index])
        if ref.ref.full_name == f"refs/heads/topic-{index + 1}"
        else ref
        for ref in before.live_refs
        for index in range(2)
        if ref.ref.full_name
        in {"refs/heads/main", f"refs/heads/topic-{index + 1}"}
    )
    live_refs = tuple(dict((ref.ref, ref) for ref in live_refs).values())
    remote_bookmarks = tuple(
        sync.JjRemoteBookmark(
            "origin",
            f"topic-{index + 1}",
            sync.CommitTarget(old_ids[index]),
            sync.TrackingState.TRACKED,
        )
        for index in range(2)
    )
    before = dataclasses.replace(
        before,
        tool_state=sync.ToolStateRead(None, state, None),
        pull_requests=pull_requests,
        live_refs=live_refs,
        local=dataclasses.replace(
            before.local,
            workspace_targets=(*before.local.workspace_targets, ("unrelated", base)),
            remote_bookmarks=remote_bookmarks,
        ),
    )
    old_commits = tuple(
        dataclasses.replace(commit, is_hidden=True) for commit in before.local.commits
    )
    new_commits = tuple(
        sync.ObservedCommit(
            new_ids[index],
            (base if index == 0 else new_ids[index - 1],),
            before.local.commits[index].change_id,
            before.local.commits[index].description,
            False,
            False,
        )
        for index in range(2)
    )
    after = dataclasses.replace(
        before,
        local=dataclasses.replace(
            before.local,
            operation_id="after-fetch",
            workspace_targets=(("default", new_ids[-1]), ("unrelated", base)),
            local_bookmarks=tuple(
                sync.LocalBookmark(
                    f"topic-{index + 1}", sync.CommitTarget(new_ids[index])
                )
                for index in range(2)
            ),
            remote_bookmarks=tuple(
                dataclasses.replace(
                    bookmark, target=sync.CommitTarget(new_ids[index])
                )
                for index, bookmark in enumerate(remote_bookmarks)
            ),
            commits=(*new_commits, *old_commits),
        ),
    )
    return before, after


def prove_remote_restack(monkeypatch, *, mismatch: str | None = None) -> None:
    base = "b" * 40
    segments = {
        (base, "1" * 40): ("1" * 40,),
        ("1" * 40, "2" * 40): ("2" * 40,),
        (base, "3" * 40): ("3" * 40,),
        ("3" * 40, "4" * 40): ("4" * 40,),
    }
    fingerprints = {
        "1" * 40: ("change-1", "patch-1"),
        "2" * 40: ("change-2", "patch-2"),
        "3" * 40: (
            "different" if mismatch == "change" else "change-1",
            "patch-1",
        ),
        "4" * 40: (
            "change-2",
            "different" if mismatch == "patch" else "patch-2",
        ),
    }
    monkeypatch.setattr(
        sync, "_linear_segment", lambda _git_dir, start, end: segments[(start, end)]
    )
    monkeypatch.setattr(
        sync, "_commit_fingerprint", lambda _git_dir, commit: fingerprints[commit]
    )


@pytest.mark.parametrize("count", (2, 3))
def test_multi_pr_planner_aggregates_complete_unchanged_topology(count: int) -> None:
    observed, selected = stacked_snapshot(count=count)

    desired = sync.derive_desired(observed, selected)
    no_op = sync.plan_sync(observed, desired)
    stale = dataclasses.replace(
        observed,
        pull_requests=tuple(
            dataclasses.replace(pr, title=f"Stale {pr.number}")
            for pr in observed.pull_requests
        ),
    )
    metadata_plan = sync.plan_sync(stale, sync.derive_desired(stale, selected))

    assert isinstance(no_op, sync.NoOp)
    assert len(no_op.dependencies.prs) == count
    assert len(no_op.dependencies.live_heads) == count
    assert isinstance(metadata_plan, sync.Apply)
    assert metadata_plan.head_updates == ()
    assert len(metadata_plan.metadata_updates) == count


def test_multi_pr_planner_aggregates_multiple_head_updates() -> None:
    observed, selected = stacked_snapshot(count=2)
    base = "b" * 40
    old_heads = (base, "1" * 40)
    pull_requests = tuple(
        dataclasses.replace(
            pr,
            head_oid=old,
            base_oid=base if index == 0 else old_heads[index - 1],
        )
        for index, (pr, old) in enumerate(
            zip(observed.pull_requests, old_heads, strict=True)
        )
    )
    live_refs = tuple(
        dataclasses.replace(ref, commit_id=old_heads[index])
        if ref.ref.full_name == f"refs/heads/topic-{index + 1}"
        else ref
        for index in range(2)
        for ref in observed.live_refs
        if ref.ref.full_name in {"refs/heads/main", f"refs/heads/topic-{index + 1}"}
    )
    # The comprehension above repeats the base; retain one exact observation.
    live_refs = tuple(dict((ref.ref, ref) for ref in live_refs).values())
    observed = dataclasses.replace(
        observed, pull_requests=pull_requests, live_refs=live_refs
    )

    plan = sync.plan_sync(observed, sync.derive_desired(observed, selected))

    assert isinstance(plan, sync.Apply)
    assert (
        tuple(update.expected_old_commit_id for update in plan.head_updates)
        == old_heads
    )
    assert tuple(update.new_commit_id for update in plan.head_updates) == (
        "1" * 40,
        "2" * 40,
    )


def test_tracked_and_explicit_selection_have_distinct_membership_policy() -> None:
    observed, selected = stacked_snapshot(count=2)

    explicit = sync.select_explicit_stack(observed, selected.ordered)
    untracked = sync.select_tracked_stack(observed, selected.ordered)
    tracked_state = sync.TrackedState(
        (
            sync.TrackedStack(
                observed.repository,
                "main",
                observed.membership.ordered_prs,
            ),
        ),
        (),
    )
    tracked_snapshot = dataclasses.replace(
        observed,
        tool_state=dataclasses.replace(observed.tool_state, state=tracked_state),
    )
    tracked = sync.select_tracked_stack(tracked_snapshot, selected.ordered)

    assert explicit == selected
    assert isinstance(untracked, sync.Blocked)
    assert untracked.reasons[0].code == "untracked-membership"
    assert tracked == selected


def test_multi_pr_planner_validates_each_desired_predecessor_segment() -> None:
    observed, selected = stacked_snapshot(count=3)
    broken_commits = (
        observed.local.commits[0],
        dataclasses.replace(observed.local.commits[1], parent_commit_ids=("9" * 40,)),
        observed.local.commits[2],
    )
    observed = dataclasses.replace(
        observed,
        local=dataclasses.replace(observed.local, commits=broken_commits),
    )

    plan = sync.plan_sync(observed, sync.derive_desired(observed, selected))

    assert isinstance(plan, sync.Blocked)
    assert plan.reasons[0].code == "invalid-comparison"
    assert plan.reasons[0].subject == "2" * 40


def test_multi_pr_planner_blocks_changed_literal_base_topology() -> None:
    observed, selected = stacked_snapshot(count=2)
    second = dataclasses.replace(
        observed.pull_requests[1],
        base_branch="main",
        base_oid="b" * 40,
    )
    observed = dataclasses.replace(
        observed, pull_requests=(observed.pull_requests[0], second)
    )

    plan = sync.plan_sync(observed, sync.derive_desired(observed, selected))

    assert isinstance(plan, sync.Blocked)
    assert plan.reasons[0].code == "topology-changed"


@pytest.mark.parametrize("case", ("missing", "duplicate", "wrong"))
def test_derive_rejects_unavailable_selected_pr(case: str) -> None:
    observed, pr = snapshot(desired="1" * 40, live="1" * 40, parent="0" * 40)
    selected = pr
    if case == "missing":
        observed = dataclasses.replace(observed, pull_requests=())
    elif case == "duplicate":
        observed = dataclasses.replace(
            observed, pull_requests=(observed.pull_requests[0],) * 2
        )
    else:
        selected = sync.PullRequestId(observed.repository, 8)

    result = sync.derive_desired(observed, selection(observed, selected))

    assert isinstance(result, sync.Blocked)
    assert result.reasons[0].code == "selected-pr-unavailable"
    assert result.reasons[0].subject == "selected PR"


@pytest.mark.parametrize("bookmark_target", (None, "9" * 40))
def test_explicit_assignment_is_authoritative_after_resolution(
    bookmark_target: str | None,
) -> None:
    observed, pr = snapshot(desired="1" * 40, live="1" * 40, parent="0" * 40)
    bookmarks = (
        ()
        if bookmark_target is None
        else (sync.LocalBookmark("topic", sync.CommitTarget(bookmark_target)),)
    )
    observed = dataclasses.replace(
        observed,
        local=dataclasses.replace(observed.local, local_bookmarks=bookmarks),
    )

    result = sync.derive_desired(observed, selection(observed, pr))

    assert isinstance(result, sync.DesiredStack)
    assert result.active[0].desired_commit_id == "1" * 40


@pytest.mark.parametrize(
    "state", (sync.PullRequestState.CLOSED, sync.PullRequestState.MERGED)
)
def test_planner_rejects_non_open_pull_request(state: sync.PullRequestState) -> None:
    observed, pr = snapshot(desired="2" * 40, live="1" * 40, parent="1" * 40)
    observed = dataclasses.replace(
        observed,
        pull_requests=(dataclasses.replace(observed.pull_requests[0], state=state),),
    )

    plan = sync.plan_sync(
        observed, sync.derive_desired(observed, selection(observed, pr))
    )

    assert isinstance(plan, sync.Blocked)
    assert plan.reasons[0].code == (
        "unsupported-member-state"
        if state is sync.PullRequestState.CLOSED
        else "incomplete-selection"
    )
    assert plan.reasons[0].subject == (
        "PR #7" if state is sync.PullRequestState.CLOSED else "stack"
    )


def test_planner_rejects_fork_head_repository() -> None:
    observed, pr = snapshot(desired="2" * 40, live="1" * 40, parent="1" * 40)
    fork = sync.GitHubRepositoryId("github.com", "R_fork")
    observed = dataclasses.replace(
        observed,
        pull_requests=(
            dataclasses.replace(observed.pull_requests[0], head_repository=fork),
        ),
    )

    plan = sync.plan_sync(
        observed, sync.derive_desired(observed, selection(observed, pr))
    )

    assert isinstance(plan, sync.Blocked)
    assert plan.reasons[0].code == "nonlocal-pr"
    assert plan.reasons[0].subject == "PR #7"


def test_planner_rejects_valid_multi_member_server_stack() -> None:
    observed, pr = snapshot(desired="2" * 40, live="1" * 40, parent="1" * 40)
    other = sync.PullRequestId(observed.repository, 8)
    observed = dataclasses.replace(
        observed,
        membership=sync.ServerStackMembership(
            pr,
            sync.GitHubStackSummary(
                sync.GitHubStackId(observed.repository, 17), "STACK_node", "main"
            ),
            (pr, other),
        ),
    )

    plan = sync.plan_sync(
        observed, sync.derive_desired(observed, selection(observed, pr))
    )

    assert isinstance(plan, sync.Blocked)
    assert plan.reasons[0].code == "incomplete-membership"
    assert plan.reasons[0].subject == "stack"


@pytest.mark.parametrize(
    ("desired_commit", "live_commit", "parent_commit", "expected_type", "rendered"),
    (
        (
            "1" * 40,
            "1" * 40,
            "0" * 40,
            sync.NoOp,
            "no-op: observed head and metadata already equal desired state",
        ),
        (
            "2" * 40,
            "1" * 40,
            "1" * 40,
            sync.Apply,
            f"apply:\n  - publish {'2' * 40} to refs/heads/topic",
        ),
    ),
)
def test_successful_derive_plan_and_render_are_pure(
    desired_commit: str,
    live_commit: str,
    parent_commit: str,
    expected_type: type[sync.NoOp] | type[sync.Apply],
    rendered: str,
    monkeypatch,
) -> None:
    observed, pr = snapshot(
        desired=desired_commit, live=live_commit, parent=parent_commit
    )

    def unexpected_io(*_args, **_kwargs):
        pytest.fail("derive/plan/render performed I/O")

    monkeypatch.setattr(sync, "_run", unexpected_io)
    monkeypatch.setattr(sync.subprocess, "run", unexpected_io)

    selected = selection(observed, pr)
    desired = sync.derive_desired(observed, selected)
    plan = sync.plan_sync(observed, desired)

    assert isinstance(plan, expected_type)
    assert sync.render(plan) == rendered
    assert sync.derive_desired(observed, selected) == desired
    assert sync.plan_sync(observed, desired) == plan


def test_pure_planner_distinguishes_fast_forward_from_tracking_equality() -> None:
    old, new = "1" * 40, "2" * 40
    observed, pr = snapshot(desired=new, live=old, parent=old)

    desired = sync.derive_desired(observed, selection(observed, pr))
    plan = sync.plan_sync(observed, desired)

    assert isinstance(plan, sync.Apply)
    assert isinstance(plan.head_updates[0].authority, sync.FastForward)
    assert plan.head_updates[0] == sync.PlannedHeadUpdate(
        sync.RemoteBranchRef(observed.repository, "refs/heads/topic"),
        old,
        new,
        sync.FastForward(),
    )
    assert sync.plan_sync(observed, desired) == plan

    # A fetched/tracking value equal to the live head is deliberately not
    # replacement authority when that head is not an ancestor.
    divergent = dataclasses.replace(
        observed,
        local=dataclasses.replace(
            observed.local,
            commits=(
                dataclasses.replace(
                    observed.local.commits[0], parent_commit_ids=("3" * 40,)
                ),
                dataclasses.replace(observed.local.commits[1], commit_id="3" * 40),
            ),
        ),
    )
    blocked = sync.plan_sync(
        divergent, sync.derive_desired(divergent, selection(divergent, pr))
    )
    assert isinstance(blocked, sync.Blocked)
    assert blocked.reasons[0].code == "replacement-unauthorized"


def test_planner_produces_noop_metadata_only_and_head_plus_metadata_effects() -> None:
    commit_id = "1" * 40
    observed, pr = snapshot(desired=commit_id, live=commit_id, parent="0" * 40)
    desired = sync.derive_desired(observed, selection(observed, pr))
    no_op = sync.plan_sync(observed, desired)
    assert isinstance(no_op, sync.NoOp)

    metadata_drift = dataclasses.replace(
        observed,
        pull_requests=(
            dataclasses.replace(
                observed.pull_requests[0], title="Stale title", body="Stale body"
            ),
        ),
    )
    metadata_desired = sync.derive_desired(
        metadata_drift, selection(metadata_drift, pr)
    )
    metadata_only = sync.plan_sync(metadata_drift, metadata_desired)
    assert isinstance(metadata_only, sync.Apply)
    assert metadata_drift.local.commits == observed.local.commits
    assert metadata_desired == desired
    assert metadata_only.head_updates == ()
    assert metadata_only.metadata_updates == (
        sync.PRMetadataUpdate(pr, "Desired title", "Desired body"),
    )
    assert sync.render(metadata_only) == "apply:\n  - update metadata for PR #7"

    rewritten, pr = snapshot(desired="2" * 40, live=commit_id, parent=commit_id)
    rewritten = dataclasses.replace(
        rewritten,
        local=dataclasses.replace(
            rewritten.local,
            commits=(
                dataclasses.replace(
                    rewritten.local.commits[0], description="New title\n\nNew body"
                ),
                rewritten.local.commits[1],
            ),
        ),
    )
    head_and_metadata = sync.plan_sync(
        rewritten,
        sync.derive_desired(rewritten, selection(rewritten, pr)),
    )
    assert isinstance(head_and_metadata, sync.Apply)
    assert len(head_and_metadata.head_updates) == 1
    assert len(head_and_metadata.metadata_updates) == 1


def test_operation_id_is_observation_provenance_not_a_plan_dependency() -> None:
    observed, pr = snapshot(desired="2" * 40, live="1" * 40, parent="1" * 40)
    desired = sync.derive_desired(observed, selection(observed, pr))
    plan = sync.plan_sync(observed, desired)
    changed = dataclasses.replace(
        observed,
        local=dataclasses.replace(observed.local, operation_id="unrelated-operation"),
    )
    changed_desired = sync.derive_desired(changed, selection(changed, pr))
    changed_plan = sync.plan_sync(changed, changed_desired)

    assert changed.local == dataclasses.replace(
        observed.local, operation_id="unrelated-operation"
    )
    assert changed_desired == desired
    assert changed_plan == plan
    assert isinstance(plan, sync.Apply)
    assert changed_plan.dependencies == plan.dependencies
    assert changed_plan.head_updates == plan.head_updates
    assert changed_plan.metadata_updates == plan.metadata_updates


@pytest.mark.parametrize("automation_field", ("auto_merge_enabled", "in_merge_queue"))
def test_active_automation_blocks_mutation_but_allows_exact_noop(
    automation_field: str,
) -> None:
    live = "1" * 40
    mutating, pr = snapshot(desired="2" * 40, live=live, parent=live)
    automated_pr = dataclasses.replace(
        mutating.pull_requests[0], **{automation_field: True}
    )
    mutating = dataclasses.replace(mutating, pull_requests=(automated_pr,))
    blocked = sync.plan_sync(
        mutating,
        sync.derive_desired(mutating, selection(mutating, pr)),
    )
    assert isinstance(blocked, sync.Blocked)
    assert blocked.reasons[0].code == "active-automation"

    equal, pr = snapshot(desired=live, live=live, parent="0" * 40)
    equal = dataclasses.replace(
        equal,
        pull_requests=(
            dataclasses.replace(equal.pull_requests[0], **{automation_field: True}),
        ),
    )
    no_op = sync.plan_sync(
        equal,
        sync.derive_desired(equal, selection(equal, pr)),
    )
    assert isinstance(no_op, sync.NoOp)


def test_comparison_validation_covers_boundaries_and_multicommit_success() -> None:
    base = "b" * 40
    live = "1" * 40

    empty, pr = snapshot(desired=base, live=live, parent="0" * 40)
    merge, _ = snapshot(desired="2" * 40, live=live, parent=live)
    merge = dataclasses.replace(
        merge,
        local=dataclasses.replace(
            merge.local,
            commits=(
                dataclasses.replace(
                    merge.local.commits[0], parent_commit_ids=(live, "3" * 40)
                ),
                merge.local.commits[1],
            ),
        ),
    )
    conflicted, _ = snapshot(desired="2" * 40, live=live, parent=live)
    conflicted = dataclasses.replace(
        conflicted,
        local=dataclasses.replace(
            conflicted.local,
            commits=(
                dataclasses.replace(conflicted.local.commits[0], has_conflicts=True),
                conflicted.local.commits[1],
            ),
        ),
    )
    missing, _ = snapshot(desired="2" * 40, live=live, parent=live)
    missing = dataclasses.replace(
        missing,
        local=dataclasses.replace(
            missing.local,
            commits=(
                dataclasses.replace(
                    missing.local.commits[0], parent_commit_ids=("9" * 40,)
                ),
                missing.local.commits[1],
            ),
        ),
    )
    cycle, _ = snapshot(desired="2" * 40, live=live, parent=live)
    cycle = dataclasses.replace(
        cycle,
        local=dataclasses.replace(
            cycle.local,
            commits=(
                cycle.local.commits[0],
                dataclasses.replace(
                    cycle.local.commits[1], parent_commit_ids=("2" * 40,)
                ),
            ),
        ),
    )

    for observed, code in (
        (empty, "invalid-comparison"),
        (merge, "invalid-comparison"),
        (conflicted, "commit-conflicted"),
        (missing, "invalid-comparison"),
        (cycle, "invalid-comparison"),
    ):
        plan = sync.plan_sync(
            observed,
            sync.derive_desired(observed, selection(observed, pr)),
        )
        assert isinstance(plan, sync.Blocked)
        assert plan.reasons[0].code == code

    linear, pr = snapshot(desired="2" * 40, live=live, parent=live)
    plan = sync.plan_sync(
        linear,
        sync.derive_desired(linear, selection(linear, pr)),
    )
    assert isinstance(plan, sync.Apply)


def test_same_branch_in_another_repository_is_not_authoritative() -> None:
    old, new = "1" * 40, "2" * 40
    observed, pr = snapshot(desired=new, live=old, parent=old)
    other_repository = sync.GitHubRepositoryId(observed.repository.host, "R_other_repo")
    wrong_refs = tuple(
        dataclasses.replace(
            live_ref,
            ref=dataclasses.replace(live_ref.ref, repository=other_repository),
        )
        for live_ref in observed.live_refs
    )
    observed = dataclasses.replace(observed, live_refs=wrong_refs)

    blocked = sync.plan_sync(
        observed,
        sync.derive_desired(observed, selection(observed, pr)),
    )

    assert isinstance(blocked, sync.Blocked)
    assert blocked.reasons[0].code == "base-disagrees"


@pytest.mark.parametrize(
    ("branch", "commit_id", "code"),
    (
        ("topic", None, "head-disagrees"),
        ("topic", "9" * 40, "head-disagrees"),
        ("main", None, "base-disagrees"),
        ("main", "8" * 40, "base-disagrees"),
    ),
)
def test_planner_rejects_missing_and_disagreeing_live_refs(
    branch: str, commit_id: str | None, code: str
) -> None:
    observed, pr = snapshot(desired="2" * 40, live="1" * 40, parent="1" * 40)
    full_name = f"refs/heads/{branch}"
    observed = dataclasses.replace(
        observed,
        live_refs=tuple(
            dataclasses.replace(ref, commit_id=commit_id)
            if ref.ref.full_name == full_name
            else ref
            for ref in observed.live_refs
        ),
    )

    plan = sync.plan_sync(
        observed, sync.derive_desired(observed, selection(observed, pr))
    )

    assert isinstance(plan, sync.Blocked)
    assert plan.reasons[0].code == code
    assert plan.reasons[0].subject == full_name


def test_last_publication_uses_logical_repo_identity_not_push_transport() -> None:
    live, desired = "1" * 40, "2" * 40
    observed, pr = snapshot(desired=desired, live=live, parent="3" * 40)
    publication = sync.LastPublishedHead(
        pr,
        sync.RemoteBranchRef(observed.repository, "refs/heads/topic"),
        live,
    )
    state = sync.TrackedState((), (publication,))
    observed = dataclasses.replace(
        observed, tool_state=sync.ToolStateRead("a" * 40, state, None)
    )

    plan = sync.plan_sync(
        dataclasses.replace(observed, push_url="https://github.com/o/r.git"),
        sync.derive_desired(observed, selection(observed, pr)),
    )
    assert isinstance(plan, sync.Apply)
    assert plan.head_updates[0].authority == sync.MatchesLastPublication(publication)
    assert plan.dependencies.push_url == "https://github.com/o/r.git"

    stale = dataclasses.replace(publication, verified_commit_id="9" * 40)
    observed = dataclasses.replace(
        observed,
        tool_state=sync.ToolStateRead("b" * 40, sync.TrackedState((), (stale,)), None),
    )
    blocked = sync.plan_sync(
        observed,
        sync.derive_desired(observed, selection(observed, pr)),
    )
    assert isinstance(blocked, sync.Blocked)


def test_description_metadata_uses_existing_markdown_normalization() -> None:
    observed, pr = snapshot(desired="1" * 40, live="1" * 40, parent="0" * 40)
    description = "Title\n\nsoft\nwrapped\n\n- list\n- stays"
    observed = dataclasses.replace(
        observed,
        local=dataclasses.replace(
            observed.local,
            commits=(
                dataclasses.replace(observed.local.commits[0], description=description),
            ),
        ),
    )

    desired = sync.derive_desired(observed, selection(observed, pr))

    assert isinstance(desired, sync.DesiredStack)
    assert desired.active[0].title == "Title"
    assert desired.active[0].body == "soft wrapped\n\n- list\n- stays"


def test_observations_are_deeply_immutable() -> None:
    observed, _pr = snapshot(desired="1" * 40, live="1" * 40, parent="0" * 40)
    with pytest.raises(dataclasses.FrozenInstanceError):
        observed.local.operation_id = "other"  # type: ignore[misc]
    assert isinstance(observed.live_refs, tuple)


def test_remote_restack_adoption_fetches_exact_branches_and_records_authority(
    jj_repo: Path, monkeypatch
) -> None:
    before, after = remote_restack_snapshots()
    oid = sync.cas_write_state(jj_repo, None, before.tool_state.state)
    before = dataclasses.replace(
        before, tool_state=dataclasses.replace(before.tool_state, state_blob_oid=oid)
    )
    after = dataclasses.replace(
        after, tool_state=dataclasses.replace(after.tool_state, state_blob_oid=oid)
    )
    plan = sync.plan_remote_restack_adoption(before)
    assert isinstance(plan, sync.RemoteRestackAdoption)
    observations = iter((before, after))
    monkeypatch.setattr(
        sync, "_observe_adoption", lambda *_args, **_kwargs: next(observations)
    )
    prove_remote_restack(monkeypatch)
    commands: list[list[str]] = []
    original_run = sync.subprocess.run

    def run_fetch(command, **kwargs):
        if command[:3] == ["jj", "git", "fetch"]:
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")
        return original_run(command, **kwargs)

    monkeypatch.setattr(sync.subprocess, "run", run_fetch)
    _server, github = fake_github(before)

    result = sync.adopt_remote_restack(plan, jj_repo, github)

    receipts = tuple(
        sync.LastAdoptedHead(
            boundary.pr_identity,
            sync.RemoteBranchRef(
                before.repository, f"refs/heads/{boundary.branch}"
            ),
            boundary.remote_commit_id,
        )
        for boundary in plan.boundaries
    )
    assert result == sync.AdoptionVerified(receipts)
    assert commands == [
        [
            "jj",
            "git",
            "fetch",
            "--remote",
            "origin",
            "--branch",
            "topic-1",
            "--branch",
            "topic-2",
        ]
    ]
    assert sync.read_state(jj_repo)[1] == sync.TrackedState(
        before.tool_state.state.stacks, (), receipts,
        last_published_boundaries=before.tool_state.state.last_published_boundaries,
    )


def test_remote_restack_without_prefetch_base_evidence_blocks_before_fetch(
    jj_repo: Path, monkeypatch
) -> None:
    before, _after = remote_restack_snapshots()
    before = dataclasses.replace(
        before,
        tool_state=dataclasses.replace(
            before.tool_state,
            state=dataclasses.replace(
                before.tool_state.state, last_published_boundaries=()
            ),
        ),
    )
    fetched = False

    def reject_fetch(command, **kwargs):
        nonlocal fetched
        if command[:3] == ["jj", "git", "fetch"]:
            fetched = True
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(sync.subprocess, "run", reject_fetch)

    plan = sync.plan_remote_restack_adoption(before)

    assert isinstance(plan, sync.Blocked)
    assert plan.reasons[0].code == "boundary-unavailable"
    assert not fetched


def test_remote_restack_does_not_record_authority_after_stale_state_cas(
    jj_repo: Path, monkeypatch
) -> None:
    before, after = remote_restack_snapshots()
    oid = sync.cas_write_state(jj_repo, None, before.tool_state.state)
    before = dataclasses.replace(
        before, tool_state=dataclasses.replace(before.tool_state, state_blob_oid=oid)
    )
    after = dataclasses.replace(
        after, tool_state=dataclasses.replace(after.tool_state, state_blob_oid=oid)
    )
    plan = sync.plan_remote_restack_adoption(before)
    assert isinstance(plan, sync.RemoteRestackAdoption)
    prove_remote_restack(monkeypatch)
    observations = iter((before, after))

    def observe(*_args, **_kwargs):
        result = next(observations)
        if result is after:
            sync.cas_write_state(
                jj_repo,
                oid,
                sync.TrackedState(
                    before.tool_state.state.stacks,
                    (
                        sync.LastPublishedHead(
                            before.pull_requests[0].identity,
                            sync.RemoteBranchRef(
                                before.repository, "refs/heads/unrelated"
                            ),
                            "9" * 40,
                        ),
                    ),
                ),
            )
        return result

    monkeypatch.setattr(sync, "_observe_adoption", observe)
    original_run = sync.subprocess.run
    monkeypatch.setattr(
        sync.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, "", "")
        if command[:3] == ["jj", "git", "fetch"]
        else original_run(command, **kwargs),
    )
    _server, github = fake_github(before)

    result = sync.adopt_remote_restack(plan, jj_repo, github)

    assert isinstance(result, sync.Stopped)
    assert result.stage == "adoption"
    assert sync.read_state(jj_repo)[1].last_adopted_heads == ()


@pytest.mark.parametrize("mismatch", ("change", "patch"))
def test_remote_restack_requires_both_ordered_change_and_patch_identity(
    mismatch: str, monkeypatch
) -> None:
    before, after = remote_restack_snapshots()
    plan = sync.plan_remote_restack_adoption(before)
    assert isinstance(plan, sync.RemoteRestackAdoption)
    prove_remote_restack(monkeypatch, mismatch=mismatch)

    assert sync._verify_adoption(before, after, plan) == (
        "ordered change IDs or patch IDs differ for topic-1"
        if mismatch == "change"
        else "ordered change IDs or patch IDs differ for topic-2"
    )


def test_restack_fingerprint_survives_a_real_jj_rebase(jj_repo: Path) -> None:
    old = jj(jj_repo, "log", "-r", "topic", "--no-graph", "-T", "commit_id")
    jj(jj_repo, "new", "root()")
    (jj_repo / "base").write_text("new base\n")
    jj(jj_repo, "describe", "-m", "New base")
    base = jj(jj_repo, "log", "-r", "@", "--no-graph", "-T", "commit_id")
    jj(jj_repo, "rebase", "-b", "topic", "-d", base)
    new = jj(jj_repo, "log", "-r", "topic", "--no-graph", "-T", "commit_id")

    assert new != old
    assert sync._commit_fingerprint(sync.git_common_dir(jj_repo), old) == (
        sync._commit_fingerprint(sync.git_common_dir(jj_repo), new)
    )


def test_real_asymmetric_multicommit_restack_uses_each_graphs_seam(
    jj_repo: Path,
) -> None:
    jj(jj_repo, "new", "root()")
    (jj_repo / "old-main").write_text("old main\n")
    jj(jj_repo, "describe", "-m", "Old main")
    old_seam = jj(jj_repo, "log", "-r", "@", "--no-graph", "-T", "commit_id")
    jj(jj_repo, "rebase", "-b", "topic", "-d", old_seam)
    first_topic = jj(jj_repo, "log", "-r", "topic", "--no-graph", "-T", "commit_id")
    jj(jj_repo, "new", "topic")
    (jj_repo / "second").write_text("second\n")
    jj(jj_repo, "describe", "-m", "Second topic commit")
    jj(jj_repo, "bookmark", "set", "topic", "-r", "@")
    old_head = jj(jj_repo, "log", "-r", "topic", "--no-graph", "-T", "commit_id")
    jj(jj_repo, "new", "root()")
    (jj_repo / "advanced-main").write_text("advanced\n")
    jj(jj_repo, "describe", "-m", "Advanced main")
    new_seam = jj(jj_repo, "log", "-r", "@", "--no-graph", "-T", "commit_id")
    jj(jj_repo, "rebase", "-s", first_topic, "-d", new_seam)
    new_head = jj(jj_repo, "log", "-r", "topic", "--no-graph", "-T", "commit_id")
    git_dir = sync.git_common_dir(jj_repo)

    old_segment = sync._linear_segment(git_dir, old_seam, old_head)
    new_segment = sync._linear_segment(git_dir, new_seam, new_head)

    assert len(old_segment) == len(new_segment) == 2
    assert [sync._commit_fingerprint(git_dir, oid) for oid in old_segment] == [
        sync._commit_fingerprint(git_dir, oid) for oid in new_segment
    ]


def test_adoption_state_interruption_recovers_old_oid_authority(
    jj_repo: Path, monkeypatch
) -> None:
    before, after = remote_restack_snapshots()
    oid = sync.cas_write_state(jj_repo, None, before.tool_state.state)
    before = dataclasses.replace(
        before, tool_state=dataclasses.replace(before.tool_state, state_blob_oid=oid)
    )
    after = dataclasses.replace(
        after, tool_state=dataclasses.replace(after.tool_state, state_blob_oid=oid)
    )
    plan = sync.plan_remote_restack_adoption(before)
    assert isinstance(plan, sync.RemoteRestackAdoption)
    observations = iter((before, after, after))
    monkeypatch.setattr(sync, "_observe_adoption", lambda *_a, **_k: next(observations))
    prove_remote_restack(monkeypatch)
    original_run = sync.subprocess.run
    monkeypatch.setattr(
        sync.subprocess, "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, "", "")
        if command[:3] == ["jj", "git", "fetch"]
        else original_run(command, **kwargs),
    )
    original_record = sync._record_adoptions
    calls = 0

    def interrupted(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sync.Error("simulated state interruption")
        return original_record(*args, **kwargs)

    monkeypatch.setattr(sync, "_record_adoptions", interrupted)
    _server, github = fake_github(before)

    first = sync.adopt_remote_restack(plan, jj_repo, github)
    assert isinstance(first, sync.Stopped)
    _journal_oid, journal = sync.read_recovery(jj_repo)
    assert journal is not None
    attempt = journal.entries[0].attempt
    assert isinstance(attempt, sync.AdoptionAttempt)
    assert tuple(item.old_commit_id for item in attempt.boundaries) == ("1" * 40, "2" * 40)

    second = sync.adopt_remote_restack(plan, jj_repo, github)
    assert isinstance(second, sync.AdoptionVerified)
    assert {item.verified_commit_id for item in second.adopted_heads} == {"3" * 40, "4" * 40}
    assert sync.read_recovery(jj_repo) == (None, None)


def test_adoption_retry_retires_attempt_after_exact_receipts_were_persisted(
    jj_repo: Path, monkeypatch
) -> None:
    before, after = remote_restack_snapshots()
    oid = sync.cas_write_state(jj_repo, None, before.tool_state.state)
    before = dataclasses.replace(
        before, tool_state=dataclasses.replace(before.tool_state, state_blob_oid=oid)
    )
    after = dataclasses.replace(
        after, tool_state=dataclasses.replace(after.tool_state, state_blob_oid=oid)
    )
    plan = sync.plan_remote_restack_adoption(before)
    assert isinstance(plan, sync.RemoteRestackAdoption)
    observations = 0

    def observe(*_args, **_kwargs):
        nonlocal observations
        observations += 1
        if observations == 1:
            return before
        if observations == 2:
            return after
        state_oid, state = sync.read_state(jj_repo)
        return dataclasses.replace(
            after,
            tool_state=sync.ToolStateRead(state_oid, state, None),
        )

    monkeypatch.setattr(sync, "_observe_adoption", observe)
    prove_remote_restack(monkeypatch)
    original_run = sync.subprocess.run
    monkeypatch.setattr(
        sync.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, "", "")
        if command[:3] == ["jj", "git", "fetch"]
        else original_run(command, **kwargs),
    )
    remove = sync._remove_recovery_entry
    interrupted = True

    def interrupt_after_receipt(*args, **kwargs):
        nonlocal interrupted
        if interrupted:
            interrupted = False
            raise sync.Error("simulated interruption after receipt")
        return remove(*args, **kwargs)

    monkeypatch.setattr(sync, "_remove_recovery_entry", interrupt_after_receipt)
    _server, github = fake_github(before)

    first = sync.adopt_remote_restack(plan, jj_repo, github)
    state_after_first = sync.read_state(jj_repo)
    second = sync.adopt_remote_restack(plan, jj_repo, github)

    assert isinstance(first, sync.Stopped)
    assert isinstance(second, sync.AdoptionVerified)
    assert sync.read_state(jj_repo) == state_after_first
    assert sync.read_recovery(jj_repo) == (None, None)


def test_remote_restack_rejects_partial_and_mixed_movement() -> None:
    before, _after = remote_restack_snapshots()
    old_second = "2" * 40
    partial = dataclasses.replace(
        before,
        pull_requests=(
            before.pull_requests[0],
            dataclasses.replace(before.pull_requests[1], head_oid=old_second),
        ),
        live_refs=tuple(
            dataclasses.replace(ref, commit_id=old_second)
            if ref.ref.full_name == "refs/heads/topic-2"
            else ref
            for ref in before.live_refs
        ),
    )
    mixed = dataclasses.replace(
        before,
        local=dataclasses.replace(
            before.local,
            local_bookmarks=(
                dataclasses.replace(
                    before.local.local_bookmarks[0], target=sync.CommitTarget("9" * 40)
                ),
                *before.local.local_bookmarks[1:],
            ),
        ),
    )

    partial_plan = sync.plan_remote_restack_adoption(partial)
    mixed_plan = sync.plan_remote_restack_adoption(mixed)
    assert isinstance(partial_plan, sync.Blocked)
    assert partial_plan.reasons[0].code == "partial-restack"
    assert isinstance(mixed_plan, sync.Blocked)
    assert mixed_plan.reasons[0].code == "mixed-movement"


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("old-visible", "superseded commit remains visible or absent for topic-1"),
        ("new-hidden", "adopted commit is hidden, absent, or conflicted for topic-1"),
        ("new-conflicted", "adopted commit is hidden, absent, or conflicted for topic-1"),
        ("bookmark", "canonical PR/ref or bookmark state disagrees for topic-1"),
        ("workspace", "workspace default did not move to the unique adopted change"),
    ),
)
def test_remote_restack_verifies_complete_poststate(
    mutation: str, message: str, monkeypatch
) -> None:
    before, after = remote_restack_snapshots()
    plan = sync.plan_remote_restack_adoption(before)
    assert isinstance(plan, sync.RemoteRestackAdoption)
    prove_remote_restack(monkeypatch)
    commits = list(after.local.commits)
    bookmarks = list(after.local.local_bookmarks)
    workspaces = after.local.workspace_targets
    if mutation == "old-visible":
        commits[2] = dataclasses.replace(commits[2], is_hidden=False)
    elif mutation == "new-hidden":
        commits[0] = dataclasses.replace(commits[0], is_hidden=True)
    elif mutation == "new-conflicted":
        commits[0] = dataclasses.replace(commits[0], has_conflicts=True)
    elif mutation == "bookmark":
        bookmarks[0] = dataclasses.replace(
            bookmarks[0], target=sync.CommitTarget("9" * 40)
        )
    else:
        workspaces = (("default", "9" * 40), ("unrelated", "b" * 40))
    after = dataclasses.replace(
        after,
        local=dataclasses.replace(
            after.local,
            commits=tuple(commits),
            local_bookmarks=tuple(bookmarks),
            workspace_targets=workspaces,
        ),
    )

    assert sync._verify_adoption(before, after, plan) == message


def test_remote_restack_ignores_main_movement_and_unrelated_workspace(
    monkeypatch,
) -> None:
    before, after = remote_restack_snapshots()
    plan = sync.plan_remote_restack_adoption(before)
    assert isinstance(plan, sync.RemoteRestackAdoption)
    prove_remote_restack(monkeypatch)
    after = dataclasses.replace(
        after,
        live_refs=tuple(
            dataclasses.replace(ref, commit_id="9" * 40)
            if ref.ref.full_name == "refs/heads/main"
            else ref
            for ref in after.live_refs
        ),
    )

    assert sync._verify_adoption(before, after, plan) is None


def test_adopted_head_is_distinct_exact_replacement_authority() -> None:
    observed, pr = snapshot(desired="3" * 40, live="2" * 40, parent="0" * 40)
    ref = sync.RemoteBranchRef(observed.repository, "refs/heads/topic")
    receipt = sync.LastAdoptedHead(pr, ref, "2" * 40)
    observed = dataclasses.replace(
        observed,
        tool_state=dataclasses.replace(
            observed.tool_state,
            state=sync.TrackedState((), (), (receipt,)),
        ),
    )

    plan = sync.plan_sync(
        observed, sync.derive_desired(observed, selection(observed, pr))
    )
    assert isinstance(plan, sync.Apply)
    assert plan.head_updates[0].authority == sync.MatchesLastAdoption(receipt)

    stale = dataclasses.replace(receipt, verified_commit_id="8" * 40)
    blocked_observation = dataclasses.replace(
        observed,
        tool_state=dataclasses.replace(
            observed.tool_state, state=sync.TrackedState((), (), (stale,))
        ),
    )
    blocked = sync.plan_sync(
        blocked_observation,
        sync.derive_desired(blocked_observation, selection(blocked_observation, pr)),
    )
    assert isinstance(blocked, sync.Blocked)


def test_verified_publication_supersedes_adopted_authority(jj_repo: Path) -> None:
    _observed, plan = head_plan()
    tracking = sync._plan_tracking(plan)
    publication = sync._plan_publications(plan)[0]
    adoption = sync.LastAdoptedHead(
        publication.pr,
        publication.ref,
        plan.head_updates[0].expected_old_commit_id,
    )
    sync.cas_write_state(
        jj_repo, None, sync.TrackedState((tracking,), (), (adoption,))
    )

    assert sync._record_verified_state(
        jj_repo, tracking, (publication,)
    )

    state = sync.read_state(jj_repo)[1]
    assert state.last_published_heads == (publication,)
    assert state.last_adopted_heads == ()

    with pytest.raises(ValueError, match="ambiguous head authority"):
        sync._validate_state(
            sync.TrackedState(
                (tracking,),
                (publication,),
                (
                    sync.LastAdoptedHead(
                        publication.pr,
                        publication.ref,
                        publication.verified_commit_id,
                    ),
                ),
            )
        )


def test_remote_restack_dry_run_does_not_fetch_or_record(
    jj_repo: Path, monkeypatch
) -> None:
    before, _after = remote_restack_snapshots()
    plan = sync.plan_remote_restack_adoption(before)
    assert isinstance(plan, sync.RemoteRestackAdoption)
    monkeypatch.setattr(
        sync,
        "_observe_adoption",
        lambda *_args, **_kwargs: before,
    )
    original_run = sync.subprocess.run

    def reject_fetch(command, **kwargs):
        if command[:3] == ["jj", "git", "fetch"]:
            pytest.fail("dry-run fetched")
        return original_run(command, **kwargs)

    monkeypatch.setattr(sync.subprocess, "run", reject_fetch)
    _server, github = fake_github(before)

    assert sync.adopt_remote_restack(
        plan, jj_repo, github, dry_run=True
    ) == sync.AdoptionVerified(())
    assert sync.read_state(jj_repo) == (None, sync.EMPTY_STATE)


def test_noop_records_membership_without_manufacturing_publication_authority(
    jj_repo: Path,
) -> None:
    observed, pr = snapshot(desired="1" * 40, live="1" * 40, parent="0" * 40)
    plan = sync.plan_sync(observed, sync.derive_desired(observed, selection(observed, pr)))
    _server, github = fake_github(observed)

    result = sync.apply(plan, jj_repo, github)

    assert result == sync.Verified(state_recorded=True)
    _oid, state = sync.read_state(jj_repo)
    assert state.stacks == (sync.TrackedStack(observed.repository, "main", (pr,)),)
    assert state.last_published_heads == ()


def head_plan() -> tuple[sync.Snapshot, sync.Apply]:
    observed, pr = snapshot(desired="2" * 40, live="1" * 40, parent="1" * 40)
    plan = sync.plan_sync(observed, sync.derive_desired(observed, selection(observed, pr)))
    assert isinstance(plan, sync.Apply)
    return observed, plan


def head_journal(plan: sync.Apply, *, possibly_live: bool) -> sync.RecoveryJournal:
    return sync.RecoveryJournal(
        (
            sync.RecoveryEntry(
                "head-effect",
                sync.HeadMutationAttempt(
                    plan.desired.repository,
                    plan.dependencies.push_url,
                    plan.head_updates,
                    sync._plan_tracking(plan),
                    sync._plan_publications(plan),
                ),
                possibly_live,
            ),
        ),
    )


def test_recovery_distinguishes_prepared_from_possibly_live_head_attempt(
    jj_repo: Path, monkeypatch
) -> None:
    observed, plan = head_plan()
    _server, github = fake_github(observed)
    monkeypatch.setattr(
        sync.SubprocessGitTransport,
        "observe_live_refs",
        lambda *_args, **_kwargs: plan.dependencies.live_heads,
    )

    sync.cas_write_recovery(jj_repo, None, head_journal(plan, possibly_live=False))
    assert sync.settle_recovery(jj_repo, github) == ()
    assert sync.read_recovery(jj_repo) == (None, None)
    assert sync.read_state(jj_repo) == (None, sync.EMPTY_STATE)

    sync.cas_write_recovery(jj_repo, None, head_journal(plan, possibly_live=True))
    assert sync.settle_recovery(jj_repo, github) == head_journal(
        plan, possibly_live=True
    ).entries
    assert sync.read_recovery(jj_repo)[1] is not None


def test_applied_head_recovery_records_receipt_before_retiring_journal(
    jj_repo: Path, monkeypatch
) -> None:
    observed, plan = head_plan()
    _server, github = fake_github(observed)
    final_refs = tuple(
        dataclasses.replace(item, commit_id=plan.head_updates[0].new_commit_id)
        for item in plan.dependencies.live_heads
    )
    monkeypatch.setattr(
        sync.SubprocessGitTransport,
        "observe_live_refs",
        lambda *_args, **_kwargs: final_refs,
    )
    sync.cas_write_recovery(jj_repo, None, head_journal(plan, possibly_live=True))

    assert sync.settle_recovery(jj_repo, github) == ()

    assert sync.read_recovery(jj_repo) == (None, None)
    _oid, state = sync.read_state(jj_repo)
    assert state.stacks == (sync._plan_tracking(plan),)
    assert state.last_published_heads == sync._plan_publications(plan)


def test_apply_publishes_once_with_exact_lease_and_records_authority(
    jj_repo: Path,
) -> None:
    observed, plan = head_plan()
    server, github = fake_github(observed)
    git = FakeGitTransport(server)

    result = sync.apply(plan, jj_repo, github, git_transport=git)

    assert result == sync.Verified(head_published=True, state_recorded=True)
    assert plan.head_updates[0].expected_old_commit_id == "1" * 40
    assert git.observe_live_refs(
        observed.push_url,
        observed.repository,
        (plan.head_updates[0].ref.full_name,),
    )[0].commit_id == plan.head_updates[0].new_commit_id
    assert github.pull_requests((observed.pull_requests[0].identity,))[0].head_oid == (
        plan.head_updates[0].new_commit_id
    )
    assert sync.read_state(jj_repo)[1].last_published_heads == sync._plan_publications(
        plan
    )
    assert sync.read_recovery(jj_repo) == (None, None)


def test_apply_resolves_lost_git_push_response_by_authoritative_readback(
    jj_repo: Path,
) -> None:
    observed, plan = head_plan()
    server, github = fake_github(observed)
    git = FakeGitTransport(server)

    with git.lose_response(sync.GitTransport.push_exact_head_updates):
        result = sync.apply(plan, jj_repo, github, git_transport=git)

    assert result == sync.Verified(head_published=True, state_recorded=True)
    assert git.observe_live_refs(
        observed.push_url,
        observed.repository,
        (plan.head_updates[0].ref.full_name,),
    )[0].commit_id == plan.head_updates[0].new_commit_id
    assert sync.read_recovery(jj_repo) == (None, None)


def test_atomic_head_push_uses_every_exact_observed_lease(monkeypatch) -> None:
    repository = sync.GitHubRepositoryId("github.com", "R_repo")
    updates = tuple(
        sync.PlannedHeadUpdate(
            sync.RemoteBranchRef(repository, f"refs/heads/topic-{index}"),
            str(index) * 40,
            str(index + 2) * 40,
            sync.FastForward(),
        )
        for index in (1, 2)
    )
    commands: list[list[str]] = []
    monkeypatch.setattr(sync, "git_common_dir", lambda _workspace: Path("/git"))
    monkeypatch.setattr(
        sync.subprocess,
        "run",
        lambda command, **_kwargs: (
            commands.append(command) or subprocess.CompletedProcess(command, 0, "", "")
        ),
    )

    sync.push_exact_head_updates("/workspace", updates, "ssh://example/repo")

    assert commands == [
        [
            "git",
            "--git-dir=/git",
            "push",
            "--atomic",
            "--no-follow-tags",
            "--recurse-submodules=no",
            f"--force-with-lease=refs/heads/topic-1:{'1' * 40}",
            f"--force-with-lease=refs/heads/topic-2:{'2' * 40}",
            "ssh://example/repo",
            f"{'3' * 40}:refs/heads/topic-1",
            f"{'4' * 40}:refs/heads/topic-2",
        ]
    ]


def test_state_cas_before_journal_retirement_is_idempotently_recoverable(
    jj_repo: Path, monkeypatch
) -> None:
    observed, plan = head_plan()
    _server, github = fake_github(observed)
    final_refs = tuple(
        dataclasses.replace(item, commit_id=plan.head_updates[0].new_commit_id)
        for item in plan.dependencies.live_heads
    )
    monkeypatch.setattr(
        sync.SubprocessGitTransport,
        "observe_live_refs",
        lambda *_args, **_kwargs: final_refs,
    )
    sync.cas_write_recovery(jj_repo, None, head_journal(plan, possibly_live=True))
    original = sync.cas_write_recovery

    def fail_retirement(workspace, expected_oid, journal):
        if journal is None:
            raise sync.ConcurrentUpdate("injected retirement crash")
        return original(workspace, expected_oid, journal)

    monkeypatch.setattr(sync, "cas_write_recovery", fail_retirement)
    with pytest.raises(sync.ConcurrentUpdate, match="retirement crash"):
        sync.settle_recovery(jj_repo, github)
    assert sync.read_state(jj_repo)[1].last_published_heads == sync._plan_publications(
        plan
    )
    assert sync.read_recovery(jj_repo)[1] is not None

    monkeypatch.setattr(sync, "cas_write_recovery", original)
    assert sync.settle_recovery(jj_repo, github) == ()
    assert sync.read_recovery(jj_repo) == (None, None)


def test_lost_metadata_response_is_verified_and_retired(jj_repo: Path) -> None:
    observed, pr = snapshot(desired="1" * 40, live="1" * 40, parent="0" * 40)
    stale = dataclasses.replace(
        observed,
        pull_requests=(dataclasses.replace(observed.pull_requests[0], title="Stale"),),
    )
    plan = sync.plan_sync(stale, sync.derive_desired(stale, selection(stale, pr)))
    assert isinstance(plan, sync.Apply)
    server, github = fake_github(stale)

    with github.lose_response(sync.GitHubClient.update_pull_request, pr=pr):
        result = sync.apply(plan, jj_repo, github)

    assert result == sync.Verified(metadata_updated=True, state_recorded=True)
    assert server.read_pull_requests((pr,))[0].title == "Desired title"
    assert sync.read_recovery(jj_repo) == (None, None)
    assert sync.read_state(jj_repo)[1].last_published_heads == ()


def test_dry_run_interprets_plan_without_any_writes(jj_repo: Path) -> None:
    observed, pr = snapshot(desired="1" * 40, live="1" * 40, parent="0" * 40)
    stale = dataclasses.replace(
        observed,
        pull_requests=(dataclasses.replace(observed.pull_requests[0], title="Stale"),),
    )
    plan = sync.plan_sync(stale, sync.derive_desired(stale, selection(stale, pr)))
    assert isinstance(plan, sync.Apply)
    server, github = fake_github(stale)

    assert sync.apply(plan, jj_repo, github, dry_run=True) == sync.Verified()

    assert server.read_pull_requests((pr,))[0].title == "Stale"
    assert sync.read_state(jj_repo) == (None, sync.EMPTY_STATE)
    assert sync.read_recovery(jj_repo) == (None, None)


def test_held_metadata_request_remains_journaled_until_explicit_release(
    jj_repo: Path,
) -> None:
    observed, pr = snapshot(desired="1" * 40, live="1" * 40, parent="0" * 40)
    stale = dataclasses.replace(
        observed,
        pull_requests=(dataclasses.replace(observed.pull_requests[0], title="Stale"),),
    )
    plan = sync.plan_sync(stale, sync.derive_desired(stale, selection(stale, pr)))
    assert isinstance(plan, sync.Apply)
    _server, github = fake_github(stale)

    with github.hold_next(sync.GitHubClient.update_pull_request, pr=pr) as pending:
        result = sync.apply(plan, jj_repo, github)

    assert isinstance(result, sync.Stopped)
    assert result.stage == "metadata"
    assert sync.settle_recovery(jj_repo, github)

    pending.release()
    assert sync.settle_recovery(jj_repo, github) == ()
    assert sync.read_recovery(jj_repo) == (None, None)


def test_fail_before_metadata_remains_conservatively_unresolved(jj_repo: Path) -> None:
    observed, pr = snapshot(desired="1" * 40, live="1" * 40, parent="0" * 40)
    stale = dataclasses.replace(
        observed,
        pull_requests=(dataclasses.replace(observed.pull_requests[0], title="Stale"),),
    )
    plan = sync.plan_sync(stale, sync.derive_desired(stale, selection(stale, pr)))
    assert isinstance(plan, sync.Apply)
    _server, github = fake_github(stale)

    with github.fail_before(sync.GitHubClient.update_pull_request, pr=pr):
        result = sync.apply(plan, jj_repo, github)

    assert isinstance(result, sync.Stopped)
    assert result.stage == "metadata"
    unresolved = sync.settle_recovery(jj_repo, github)
    assert len(unresolved) == 1
    assert unresolved[0].possibly_live


def test_unavailable_recovery_read_and_dry_run_preserve_journal(
    jj_repo: Path,
) -> None:
    observed, pr = snapshot(desired="1" * 40, live="1" * 40, parent="0" * 40)
    attempt = sync.MetadataMutationAttempt(
        observed.repository,
        sync.PRMetadataUpdate(pr, "Desired title", "Desired body"),
        "Stale",
        "Desired body",
    )
    journal = sync.RecoveryJournal(
        (sync.RecoveryEntry("metadata-effect", attempt, possibly_live=True),)
    )
    oid = sync.cas_write_recovery(jj_repo, None, journal)
    _server, github = fake_github(
        dataclasses.replace(
            observed,
            pull_requests=(dataclasses.replace(observed.pull_requests[0], title="Stale"),),
        )
    )

    with github.unavailable_reads():
        assert sync.settle_recovery(jj_repo, github) == journal.entries
    assert sync.settle_recovery(jj_repo, github, dry_run=True) == journal.entries
    assert sync.read_recovery(jj_repo) == (oid, journal)


def test_unresolved_effect_blocks_only_the_stack_it_can_change(jj_repo: Path) -> None:
    observed, pr = snapshot(desired="1" * 40, live="1" * 40, parent="0" * 40)
    plan = sync.plan_sync(observed, sync.derive_desired(observed, selection(observed, pr)))
    assert isinstance(plan, sync.NoOp)
    unrelated_pr = sync.PullRequestId(observed.repository, 99)
    attempt = sync.MetadataMutationAttempt(
        observed.repository,
        sync.PRMetadataUpdate(unrelated_pr, "New", "Body"),
        "Old",
        "Body",
    )
    journal = sync.RecoveryJournal(
        (sync.RecoveryEntry("unrelated", attempt, possibly_live=True),)
    )
    sync.cas_write_recovery(jj_repo, None, journal)
    _server, github = fake_github(observed)

    result = sync.apply(plan, jj_repo, github)

    assert result == sync.Verified(state_recorded=True)
    assert sync.read_recovery(jj_repo)[1] == journal

    conflicting = dataclasses.replace(
        attempt,
        update=dataclasses.replace(attempt.update, pr_identity=pr),
    )
    old_oid, _old = sync.read_recovery(jj_repo)
    conflicting_journal = sync.RecoveryJournal(
        (sync.RecoveryEntry("conflicting", conflicting, possibly_live=True),)
    )
    sync.cas_write_recovery(jj_repo, old_oid, conflicting_journal)
    stopped = sync.apply(plan, jj_repo, github)
    assert isinstance(stopped, sync.Stopped)
    assert stopped.stage == "recovery"


def test_first_publication_planning_is_pure_ordered_and_base_sensitive() -> None:
    repository = sync.GitHubRepositoryId("github.com", "repo")
    base, first, second = (value * 40 for value in "012")
    commits = (
        sync.ObservedCommit(first, (base,), "c1", "First\n\nbody one", False, False),
        sync.ObservedCommit(second, (first,), "c2", "Second\nbody two", False, False),
    )
    local = sync.LocalObservation("/repo", "op", (), (), (), (), (), commits, "/git")
    names = ("topic/first", "topic/second")
    destinations = (
        sync.LiveRemoteRef(sync.RemoteBranchRef(repository, "refs/heads/main"), base),
        *(
            sync.LiveRemoteRef(
                sync.RemoteBranchRef(repository, f"refs/heads/{name}"), None
            )
            for name in names
        ),
    )
    observed = sync.FirstPublicationInput(
        repository,
        "main",
        (first, second),
        local,
        None,
        tuple(
            sync.PublicationAssignment(oid, name)
            for oid, name in zip((first, second), names, strict=True)
        ),
        ("managed",),
        destinations,
        (),
    )

    plan = sync.plan_first_publication(observed)

    assert isinstance(plan, sync.FirstPublicationPlan)
    assert plan.goal.base_commit_id == base
    assert tuple(
        (item.branch_name, item.base_branch, item.title, item.body)
        for item in plan.goal.pull_requests
    ) == (
        (names[0], "main", "First", "body one"),
        (names[1], names[0], "Second", "body two"),
    )
    assert observed.local == local
    with pytest.raises(dataclasses.FrozenInstanceError):
        plan.goal.base_branch = "other"  # type: ignore[misc]

    moved = "9" * 40
    replanned = sync.plan_first_publication(
        dataclasses.replace(
            observed,
            local=dataclasses.replace(
                local,
                commits=(
                    dataclasses.replace(commits[0], parent_commit_ids=(moved,)),
                    commits[1],
                ),
            ),
            destinations=(
                dataclasses.replace(destinations[0], commit_id=moved),
                *destinations[1:],
            ),
        )
    )
    assert isinstance(replanned, sync.FirstPublicationPlan)
    assert replanned.goal.base_commit_id == moved


def test_first_publication_assignment_precedence_completeness_and_ambiguity() -> None:
    repository = sync.GitHubRepositoryId("github.com", "repo")
    base, first, second = (value * 40 for value in "012")
    bookmark = sync.LocalBookmark("intentional", sync.CommitTarget(first))
    local = sync.LocalObservation(
        "/repo",
        "op",
        (),
        (),
        (bookmark,),
        (),
        (),
        (
            sync.ObservedCommit(first, (base,), "c1", "First", False, False),
            sync.ObservedCommit(second, (first,), "c2", "Second", False, False),
        ),
        "/git",
    )
    destinations = tuple(
        sync.LiveRemoteRef(
            sync.RemoteBranchRef(repository, f"refs/heads/{name}"),
            base if name == "main" else None,
        )
        for name in (
            "main",
            "intentional",
            "generated-two",
            "explicit-one",
            "explicit-two",
        )
    )
    observed = sync.FirstPublicationInput(
        repository,
        "main",
        (first, second),
        local,
        None,
        (
            sync.PublicationAssignment(first, "generated-one"),
            sync.PublicationAssignment(second, "generated-two"),
        ),
        (),
        destinations,
        (),
    )
    assert sync.resolve_publication_assignments(observed) == (
        sync.PublicationAssignment(first, "intentional"),
        sync.PublicationAssignment(second, "generated-two"),
    )
    explicit = (
        sync.PublicationAssignment(first, "explicit-one"),
        sync.PublicationAssignment(second, "explicit-two"),
    )
    assert (
        sync.resolve_publication_assignments(
            dataclasses.replace(observed, explicit_assignments=explicit)
        )
        == explicit
    )
    incomplete = sync.resolve_publication_assignments(
        dataclasses.replace(observed, explicit_assignments=explicit[:1])
    )
    assert isinstance(incomplete, sync.Blocked)
    assert incomplete.reasons[0].code == "incomplete-explicit-assignment"
    ambiguous_local = dataclasses.replace(
        local,
        local_bookmarks=(
            bookmark,
            sync.LocalBookmark("other", sync.CommitTarget(first)),
        ),
    )
    ambiguous = sync.resolve_publication_assignments(
        dataclasses.replace(observed, local=ambiguous_local)
    )
    assert isinstance(ambiguous, sync.Blocked)
    assert ambiguous.reasons[0].code == "ambiguous-local-bookmark"


def test_first_publication_rejects_local_and_historical_name_reuse() -> None:
    repository = sync.GitHubRepositoryId("github.com", "repo")
    fork = sync.GitHubRepositoryId("github.com", "fork")
    base, commit_id = (value * 40 for value in "01")
    assignment = sync.PublicationAssignment(commit_id, "topic")
    observed = sync.FirstPublicationInput(
        repository,
        "main",
        (commit_id,),
        sync.LocalObservation(
            "/repo",
            "op",
            (),
            (),
            (),
            (),
            (),
            (sync.ObservedCommit(commit_id, (base,), "c1", "Title", False, False),),
            "/git",
        ),
        (assignment,),
        (),
        (),
        (
            sync.LiveRemoteRef(
                sync.RemoteBranchRef(repository, "refs/heads/main"), base
            ),
            sync.LiveRemoteRef(
                sync.RemoteBranchRef(repository, "refs/heads/topic"), None
            ),
        ),
        (),
    )
    historical = sync.GitHubPullRequest(
        sync.PullRequestId(repository, 1),
        "PR_node",
        sync.PullRequestState.CLOSED,
        False,
        fork,
        "topic",
        "2" * 40,
        "other",
        "3" * 40,
        False,
        False,
        "Old",
        "",
        None,
    )

    # A fork can independently use the same head name.
    assert sync.resolve_publication_assignments(
        dataclasses.replace(observed, historical_pull_requests=(historical,))
    ) == (assignment,)

    same_repository = sync.resolve_publication_assignments(
        dataclasses.replace(
            observed,
            historical_pull_requests=(
                dataclasses.replace(historical, head_repository=repository),
            ),
        )
    )
    assert isinstance(same_repository, sync.Blocked)
    assert same_repository.reasons[0].code == "pull-request-branch-collision"

    used_as_base = sync.resolve_publication_assignments(
        dataclasses.replace(
            observed,
            historical_pull_requests=(
                dataclasses.replace(historical, base_branch="topic"),
            ),
        )
    )
    assert isinstance(used_as_base, sync.Blocked)
    assert used_as_base.reasons[0].code == "pull-request-branch-collision"

    local_collision = sync.resolve_publication_assignments(
        dataclasses.replace(
            observed,
            local=dataclasses.replace(
                observed.local,
                local_bookmarks=(
                    sync.LocalBookmark("topic", sync.AbsentBookmarkTarget()),
                ),
            ),
        )
    )
    assert isinstance(local_collision, sync.Blocked)
    assert local_collision.reasons[0].code == "local-publication-collision"


@pytest.mark.parametrize(
    ("change", "code"),
    (
        ("invalid-ref", "invalid-publication-branch"),
        ("missing-destination", "destination-unavailable"),
        ("occupied-destination", "remote-publication-collision"),
        ("nonlinear", "invalid-publication-commit"),
        ("conflicted", "invalid-publication-commit"),
    ),
)
def test_first_publication_blocks_bad_evidence(change: str, code: str) -> None:
    repository = sync.GitHubRepositoryId("github.com", "repo")
    base, first, second = (value * 40 for value in "012")
    commits = (
        sync.ObservedCommit(
            first, (base,), "c1", "First", change == "conflicted", False
        ),
        sync.ObservedCommit(
            second,
            ((base if change == "nonlinear" else first),),
            "c2",
            "Second",
            False,
            False,
        ),
    )
    name = "bad..ref" if change == "invalid-ref" else "one"
    destinations = [
        sync.LiveRemoteRef(sync.RemoteBranchRef(repository, "refs/heads/main"), base)
    ]
    if change != "missing-destination":
        destinations.append(
            sync.LiveRemoteRef(
                sync.RemoteBranchRef(repository, f"refs/heads/{name}"),
                "8" * 40 if change == "occupied-destination" else None,
            )
        )
    destinations.append(
        sync.LiveRemoteRef(sync.RemoteBranchRef(repository, "refs/heads/two"), None)
    )
    observed = sync.FirstPublicationInput(
        repository,
        "main",
        (first, second),
        sync.LocalObservation("/repo", "op", (), (), (), (), (), commits, "/git"),
        None,
        (
            sync.PublicationAssignment(first, name),
            sync.PublicationAssignment(second, "two"),
        ),
        (),
        tuple(destinations),
        (),
    )
    blocked = sync.plan_first_publication(observed)
    assert isinstance(blocked, sync.Blocked)
    assert blocked.reasons[0].code == code


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
