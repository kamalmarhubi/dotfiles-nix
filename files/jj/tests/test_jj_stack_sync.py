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


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
