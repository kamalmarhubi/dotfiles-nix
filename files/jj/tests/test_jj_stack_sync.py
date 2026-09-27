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


def test_private_state_cas_and_operation_fence_work_from_linked_workspace(
    jj_repo: Path, tmp_path: Path
) -> None:
    workspace = tmp_path / "second"
    jj(jj_repo, "workspace", "add", workspace, "--name", "second")
    repository = sync.GitHubRepositoryId("github.com", "R_repo")
    pr = sync.PullRequestId(repository, "PR_node")
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
    operation_blob = run(
        "git", f"--git-dir={common}", "hash-object", "-w", "--stdin", cwd=jj_repo
    )
    # hash-object above hashes empty stdin; only presence matters at this stage.
    run("git", f"--git-dir={common}", "update-ref", sync.OPERATION_REF, operation_blob)
    tool_state = sync.observe_tool_state(jj_repo)
    assert tool_state.operation_blob_oid == operation_blob
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
    assert json.loads(payload) == {"last_published_heads": [], "stacks": []}
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
            '{"host":"github.com","node_id":"R"},"node_id":"PR"},"ref":'
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
    pr = sync.PullRequestId(repository, "PR_node")
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
    selected = sync.PullRequestId(repository, "PR_selected")
    other = sync.PullRequestId(repository, "PR_other")
    foreign = sync.PullRequestId(
        sync.GitHubRepositoryId("github.com", "R_other"), "PR_foreign"
    )

    assert sync.StandalonePullRequest(selected).pr == selected
    with pytest.raises(ValueError, match="nonempty"):
        sync.ServerStackMembership(selected, "", (selected,))
    with pytest.raises(ValueError, match="nonempty"):
        sync.ServerStackMembership(selected, "stack", ())
    with pytest.raises(ValueError, match="must belong"):
        sync.ServerStackMembership(selected, "stack", (other,))
    with pytest.raises(ValueError, match="duplicates"):
        sync.ServerStackMembership(selected, "stack", (selected, selected))
    with pytest.raises(ValueError, match="one repository"):
        sync.ServerStackMembership(selected, "stack", (selected, foreign))


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

    standalone = sync._parse_pr_source(pr_source_record(), repository, 7)
    stacked = sync._parse_pr_source(
        pr_source_record(stack={"id": "STACK_node", "baseRefName": "main"}),
        repository,
        7,
    )

    assert standalone.stack_id is None
    assert standalone.stack_base_branch is None
    assert standalone.pr.body == ""
    assert stacked.stack_id == "STACK_node"
    assert stacked.stack_base_branch == "main"


@pytest.mark.parametrize(
    ("changes", "error"),
    (
        ({"headRepository": None}, sync.IncompleteSource),
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
        sync._parse_pr_source(pr_source_record(**changes), repository, 7)


def test_github_json_transport_preserves_exact_strings_and_separates_status() -> None:
    title = "control:\u2028 paragraph:\u2029 replacement:\ufffd"
    body = json.dumps({"title": title}, ensure_ascii=False).encode()

    assert sync._github_json_response(
        sync.GitHubResponse(200, (("X-Test", "yes"),), body), "test"
    ) == {"title": title}
    with pytest.raises(sync.SourceUnavailable, match="HTTP 422"):
        sync._github_json_response(sync.GitHubResponse(422, (), body), "test")
    with pytest.raises(sync.MalformedSource, match="invalid JSON"):
        sync._github_json_response(sync.GitHubResponse(200, (), b"{"), "test")


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
        sync.observe_github_pull_request(repository, 7)

    monkeypatch.setattr(
        sync,
        "_github_graphql",
        lambda *_args, **_kwargs: {
            "data": {"repository": {"id": "R_other", "pullRequest": pr_source_record()}}
        },
    )
    with pytest.raises(sync.SourceMismatch):
        sync.observe_github_pull_request(repository, 7)

    monkeypatch.setattr(
        sync,
        "_github_graphql",
        lambda *_args, **_kwargs: {"data": {"repository": None}},
    )
    with pytest.raises(sync.IncompleteSource):
        sync.observe_github_pull_request(repository, 7)


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


def snapshot(*, desired: str, live: str, parent: str, operation: str | None = None):
    repository = sync.GitHubRepositoryId("github.com", "R_repo")
    pr_key = sync.PullRequestId(repository, "PR_node")
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
        7,
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
    )
    observed = sync.Snapshot(
        repository,
        "ssh://git@github.com/o/r.git",
        "origin",
        local,
        sync.ToolStateRead(None, sync.EMPTY_STATE, operation),
        (pr,),
        sync.StandalonePullRequest(pr_key),
        (
            sync.LiveRemoteRef(head_ref, live),
            sync.LiveRemoteRef(base_ref, "b" * 40),
        ),
    )
    return observed, pr_key


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
        selected = sync.PullRequestId(observed.repository, "PR_other")

    result = sync.derive_desired(observed, selected)

    assert isinstance(result, sync.Blocked)
    assert result.reasons[0].code == "selected-pr-unavailable"
    assert result.reasons[0].subject == selected.node_id


@pytest.mark.parametrize(
    ("bookmarks", "code"),
    (
        ((), "bookmark-absent"),
        (
            (sync.LocalBookmark("topic", sync.AbsentBookmarkTarget()),),
            "bookmark-absent",
        ),
        (
            (
                sync.LocalBookmark(
                    "topic", sync.BookmarkConflict(("1" * 40,), ("2" * 40,))
                ),
            ),
            "bookmark-conflicted",
        ),
    ),
)
def test_derive_rejects_absent_and_conflicted_local_bookmarks(
    bookmarks: tuple[sync.LocalBookmark, ...], code: str
) -> None:
    observed, pr = snapshot(desired="1" * 40, live="1" * 40, parent="0" * 40)
    observed = dataclasses.replace(
        observed,
        local=dataclasses.replace(observed.local, local_bookmarks=bookmarks),
    )

    result = sync.derive_desired(observed, pr)

    assert isinstance(result, sync.Blocked)
    assert result.reasons[0].code == code
    assert result.reasons[0].subject == "topic"


@pytest.mark.parametrize(
    "state", (sync.PullRequestState.CLOSED, sync.PullRequestState.MERGED)
)
def test_planner_rejects_non_open_pull_request(state: sync.PullRequestState) -> None:
    observed, pr = snapshot(desired="2" * 40, live="1" * 40, parent="1" * 40)
    observed = dataclasses.replace(
        observed,
        pull_requests=(dataclasses.replace(observed.pull_requests[0], state=state),),
    )

    plan = sync.plan_sync(observed, sync.derive_desired(observed, pr))

    assert isinstance(plan, sync.Blocked)
    assert plan.reasons[0].code == "pr-not-open"
    assert plan.reasons[0].subject == pr.node_id


def test_planner_rejects_fork_head_repository() -> None:
    observed, pr = snapshot(desired="2" * 40, live="1" * 40, parent="1" * 40)
    fork = sync.GitHubRepositoryId("github.com", "R_fork")
    observed = dataclasses.replace(
        observed,
        pull_requests=(
            dataclasses.replace(observed.pull_requests[0], head_repository=fork),
        ),
    )

    plan = sync.plan_sync(observed, sync.derive_desired(observed, pr))

    assert isinstance(plan, sync.Blocked)
    assert plan.reasons[0].code == "nonlocal-pr"
    assert plan.reasons[0].subject == pr.node_id


def test_planner_rejects_valid_multi_member_server_stack() -> None:
    observed, pr = snapshot(desired="2" * 40, live="1" * 40, parent="1" * 40)
    other = sync.PullRequestId(observed.repository, "PR_other")
    observed = dataclasses.replace(
        observed,
        membership=sync.ServerStackMembership(pr, "STACK_node", (pr, other)),
    )

    plan = sync.plan_sync(observed, sync.derive_desired(observed, pr))

    assert isinstance(plan, sync.Blocked)
    assert plan.reasons[0].code == "unsupported-membership"
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

    desired = sync.derive_desired(observed, pr)
    plan = sync.plan_sync(observed, desired)

    assert isinstance(plan, expected_type)
    assert sync.render(plan) == rendered
    assert sync.derive_desired(observed, pr) == desired
    assert sync.plan_sync(observed, desired) == plan


def test_pure_planner_distinguishes_fast_forward_from_tracking_equality() -> None:
    old, new = "1" * 40, "2" * 40
    observed, pr = snapshot(desired=new, live=old, parent=old)

    desired = sync.derive_desired(observed, pr)
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
    blocked = sync.plan_sync(divergent, sync.derive_desired(divergent, pr))
    assert isinstance(blocked, sync.Blocked)
    assert blocked.reasons[0].code == "replacement-unauthorized"


def test_planner_produces_noop_metadata_only_and_head_plus_metadata_effects() -> None:
    commit_id = "1" * 40
    observed, pr = snapshot(desired=commit_id, live=commit_id, parent="0" * 40)
    desired = sync.derive_desired(observed, pr)
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
    metadata_desired = sync.derive_desired(metadata_drift, pr)
    metadata_only = sync.plan_sync(metadata_drift, metadata_desired)
    assert isinstance(metadata_only, sync.Apply)
    assert metadata_drift.local.commits == observed.local.commits
    assert metadata_desired == desired
    assert metadata_only.head_updates == ()
    assert metadata_only.metadata_updates == (
        sync.PRMetadataUpdate(pr, "Desired title", "Desired body"),
    )

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
        sync.derive_desired(rewritten, pr),
    )
    assert isinstance(head_and_metadata, sync.Apply)
    assert len(head_and_metadata.head_updates) == 1
    assert len(head_and_metadata.metadata_updates) == 1


def test_operation_id_is_observation_provenance_not_a_plan_dependency() -> None:
    observed, pr = snapshot(desired="2" * 40, live="1" * 40, parent="1" * 40)
    desired = sync.derive_desired(observed, pr)
    plan = sync.plan_sync(observed, desired)
    changed = dataclasses.replace(
        observed,
        local=dataclasses.replace(observed.local, operation_id="unrelated-operation"),
    )
    changed_desired = sync.derive_desired(changed, pr)
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
        sync.derive_desired(mutating, pr),
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
        sync.derive_desired(equal, pr),
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
            sync.derive_desired(observed, pr),
        )
        assert isinstance(plan, sync.Blocked)
        assert plan.reasons[0].code == code

    linear, pr = snapshot(desired="2" * 40, live=live, parent=live)
    plan = sync.plan_sync(
        linear,
        sync.derive_desired(linear, pr),
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
        sync.derive_desired(observed, pr),
    )

    assert isinstance(blocked, sync.Blocked)
    assert blocked.reasons[0].code == "head-disagrees"


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

    plan = sync.plan_sync(observed, sync.derive_desired(observed, pr))

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
        sync.derive_desired(observed, pr),
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
        sync.derive_desired(observed, pr),
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

    desired = sync.derive_desired(observed, pr)

    assert isinstance(desired, sync.DesiredStack)
    assert desired.active[0].title == "Title"
    assert desired.active[0].body == "soft wrapped\n\n- list\n- stays"


def test_operation_fence_blocks_without_io(monkeypatch) -> None:
    observed, pr = snapshot(desired="1" * 40, live="1" * 40, parent="0" * 40)
    monkeypatch.setattr(
        sync, "_run", lambda *_args, **_kwargs: pytest.fail("planner performed I/O")
    )

    fenced = dataclasses.replace(
        observed,
        tool_state=dataclasses.replace(
            observed.tool_state,
            operation_blob_oid="f" * 40,
        ),
    )
    assert sync.render(
        sync.plan_sync(fenced, sync.derive_desired(fenced, pr))
    ).startswith("blocked [operation-fenced]")


def test_observations_are_deeply_immutable() -> None:
    observed, _pr = snapshot(desired="1" * 40, live="1" * 40, parent="0" * 40)
    with pytest.raises(dataclasses.FrozenInstanceError):
        observed.local.operation_id = "other"  # type: ignore[misc]
    assert isinstance(observed.live_refs, tuple)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
