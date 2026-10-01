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
from contextlib import nullcontext
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "files" / "bin"))
import jj_stack_sync as sync  # noqa: E402
from fake_github import FakeGitHubClient, FakeGitHubServer  # noqa: E402


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


def test_commit_only_observation_uses_the_existing_operation(monkeypatch) -> None:
    commit_id = "1" * 40
    calls = []

    def jj(workspace, operation, *arguments):
        calls.append((workspace, operation, arguments))
        return json.dumps(
            {
                "commit_id": commit_id,
                "parent_commit_ids": ["0" * 40],
                "change": "change",
                "description": "Title",
                "conflicts": False,
                "hidden": False,
            }
        )

    monkeypatch.setattr(sync, "_jj", jj)

    observed = sync.observe_commits(
        "/repo", "pinned-operation", "topic", ancestry_order=True
    )

    assert tuple(item.commit_id for item in observed) == (commit_id,)
    assert len(calls) == 1
    assert calls[0][:2] == ("/repo", "pinned-operation")
    assert calls[0][2][:4] == ("log", "--no-graph", "--reversed", "-r")
    assert calls[0][2][4] == "topic"


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


def test_private_inspection_fetch_is_invisible_to_jj_and_cleans_refs(
    jj_repo: Path, tmp_path: Path
) -> None:
    observed = sync.observe_local(
        jj_repo, revision="topic", config_keys=("git.push",)
    )
    topic = next(
        item.target.commit_id
        for item in observed.local_bookmarks
        if item.name == "topic" and isinstance(item.target, sync.CommitTarget)
    )
    remote = tmp_path / "remote.git"
    run("git", "init", "--bare", remote)
    run(
        "git",
        f"--git-dir={observed.git_common_dir}",
        "push",
        remote,
        f"{topic}:refs/heads/topic",
    )
    namespace = sync._inspection_namespace(observed.operation_id)
    ordinary_before = sync._git_refs(
        observed.git_common_dir, "refs/heads", "refs/remotes"
    )
    fetch_head = Path(observed.git_common_dir) / "FETCH_HEAD"
    fetch_head_before = fetch_head.read_bytes() if fetch_head.exists() else None

    try:
        sync._fetch_inspection_refs(
            observed.git_common_dir, os.fspath(remote), namespace, ("topic",)
        )

        assert sync.read_ref_oid(jj_repo, f"{namespace}/topic") == topic
        assert (
            sync._git_refs(observed.git_common_dir, "refs/heads", "refs/remotes")
            == ordinary_before
        )
        assert sync.pin_operation(jj_repo) == observed.operation_id
        assert (
            fetch_head.read_bytes() if fetch_head.exists() else None
        ) == fetch_head_before
    finally:
        sync._cleanup_inspection_refs(observed.git_common_dir, namespace)

    assert sync._git_refs(observed.git_common_dir, namespace) == ""


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
    pr = sync.PullRequestId(repository, 7)
    ref = sync.RemoteBranchRef(repository, "refs/heads/topic")
    state = sync.TrackedState(
        (sync.TrackedStack(repository, "main", (pr,)),),
        (sync.LastPublishedHead(pr, ref, "a" * 40),),
    )
    serialized_stack = json.loads(sync.state_to_json(state))["stacks"][0]
    assert set(serialized_stack) == {
        "repository",
        "base_branch",
        "ordered_prs",
        "detached_prs",
    }
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
    assert json.loads(payload) == {
        "last_adopted_heads": [],
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


def test_detached_state_is_strict_and_validates_bounded_membership() -> None:
    repository = sync.GitHubRepositoryId("github.com", "R_repo")
    active = sync.PullRequestId(repository, 1)
    detached = sync.PullRequestId(repository, 2)
    state = sync.TrackedState(
        (sync.TrackedStack(repository, "main", (active,), (detached,)),), ()
    )
    assert sync.parse_state(sync.state_to_json(state)) == state

    missing = json.loads(sync.state_to_json(state))
    del missing["stacks"][0]["detached_prs"]
    with pytest.raises(sync.Error, match="unexpected fields"):
        sync.parse_state(json.dumps(missing))
    with pytest.raises(ValueError, match="overlap"):
        sync.state_to_json(
            sync.TrackedState(
                (sync.TrackedStack(repository, "main", (active,), (active,)),), ()
            )
        )

    cleanup = sync.TrackedState(
        (sync.TrackedStack(repository, "main", (), (detached,)),), ()
    )
    assert sync.parse_state(sync.state_to_json(cleanup)) == cleanup

    other = sync.PullRequestId(repository, 3)
    with pytest.raises(ValueError, match="overlapping"):
        sync.state_to_json(
            sync.TrackedState(
                (
                    sync.TrackedStack(repository, "main", (active,), (detached,)),
                    sync.TrackedStack(repository, "release", (other,), (detached,)),
                ),
                (),
            )
        )


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

    standalone = sync._parse_pull_request(pr_source_record(), repository, 7)
    stacked = sync._parse_pull_request(
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

    deleted_fork = sync._parse_pull_request(
        pr_source_record(headRepository=None), repository, 7
    )
    assert deleted_fork.head_repository is None

    missing_oids = sync._parse_pull_request(
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
        sync._parse_pull_request(pr_source_record(**changes), repository, 7)


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


def test_github_api_stack_mutations_send_exact_frozen_rest_requests(monkeypatch) -> None:
    repository = sync.GitHubRepository(
        sync.GitHubRepositoryId("github.com", "R_repo"),
        "owner/repo",
        "https://github.com/owner/repo",
        "main",
    )
    first = sync.PullRequestId(repository.identity, 1)
    second = sync.PullRequestId(repository.identity, 2)
    stack = sync.GitHubStackId(repository.identity, 7)
    calls = []

    def request(host, method, path, *, body=None, cwd=None):
        calls.append((host, method, path, body, cwd))
        if path.endswith("/unstack"):
            return sync.GitHubHttpResponse(204, (), b"")
        number = 1 if path.endswith("/stacks") else 7
        return sync.GitHubHttpResponse(
            200,
            (),
            json.dumps(
                {
                    "number": number,
                    "node_id": f"STACK_{number}",
                    "base": {"ref": "main"},
                }
            ).encode(),
        )

    monkeypatch.setattr(sync, "github_http_request", request)
    api = sync.GitHubAPI(workspace="/workspace")

    assert api.create_stack(
        repository, pull_requests=(first, second)
    ) == sync.GitHubStackSummary(
        sync.GitHubStackId(repository.identity, 1), "STACK_1", "main"
    )
    assert api.add_stack_members(
        repository, stack, pull_requests=(second,)
    ) == sync.GitHubStackSummary(stack, "STACK_7", "main")
    assert api.unstack(repository, stack) is None

    assert calls == [
        (
            "github.com",
            "POST",
            "/repos/owner/repo/stacks",
            b'{"pull_requests":[1,2]}',
            Path("/workspace"),
        ),
        (
            "github.com",
            "POST",
            "/repos/owner/repo/stacks/7/add",
            b'{"pull_requests":[2]}',
            Path("/workspace"),
        ),
        (
            "github.com",
            "POST",
            "/repos/owner/repo/stacks/7/unstack",
            None,
            Path("/workspace"),
        ),
    ]


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
    first = sync._parse_pull_request(pr_source_record(), repository, 7)
    second = sync._parse_pull_request(
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


def test_github_api_update_uses_keyword_fields_and_preserves_empty_body(
    monkeypatch,
) -> None:
    repository = sync.GitHubRepository(
        sync.GitHubRepositoryId("github.com", "R_repo"),
        "owner/repo",
        "https://github.com/owner/repo",
        "main",
    )
    pull_request = sync._parse_pull_request(pr_source_record(), repository, 7)
    api = sync.GitHubAPI(workspace="/repo")
    monkeypatch.setattr(api, "pull_requests", lambda identities: (pull_request,))
    requests = []

    def graphql(host, query, variables, *, cwd):
        requests.append((host, query, variables, cwd))
        return {
            "data": {
                "updatePullRequest": {
                    "pullRequest": {
                        "id": pull_request.node_id,
                        "number": pull_request.number,
                        "repository": {"id": repository.identity.node_id},
                    }
                }
            }
        }

    monkeypatch.setattr(sync, "_github_graphql", graphql)

    api.update_pull_request(
        repository,
        pull_request.identity,
        body="",
        state=sync.PullRequestUpdateState.CLOSED,
    )

    assert requests[0][0] == "github.com"
    assert requests[0][2] == {
        "id": pull_request.node_id,
        "body": "",
        "state": "CLOSED",
    }
    assert "body: $body" in requests[0][1]
    assert "state: $state" in requests[0][1]
    with pytest.raises(ValueError, match="at least one field"):
        api.update_pull_request(repository, pull_request.identity)
    assert len(requests) == 1


def test_github_api_finds_complete_canonical_pull_requests_by_head_or_base(
    monkeypatch,
) -> None:
    repository = sync.GitHubRepository(
        sync.GitHubRepositoryId("github.com", "R_repo"),
        "owner/repo",
        "https://github.com/owner/repo",
        "main",
    )
    api = sync.GitHubAPI(workspace="/repo")
    monkeypatch.setattr(api, "resolve_repository", lambda identity: repository)
    requests = []

    def graphql(host, query, variables, *, cwd):
        requests.append((host, query, variables, cwd))
        connection = {
            "nodes": [pr_source_record()],
            "pageInfo": {"hasNextPage": False},
        }
        return {
            "data": {
                "repository": {
                    "id": repository.identity.node_id,
                    "head0": connection,
                    "base0": connection,
                }
            }
        }

    monkeypatch.setattr(sync, "_github_graphql", graphql)

    found = api.find_pull_requests(
        repository.identity,
        head_branches=("topic",),
        base_branches=("main",),
    )

    assert tuple(pr.identity.number for pr in found) == (7,)
    assert requests[0][2] == {
        "owner": "owner",
        "name": "repo",
        "states": ["OPEN", "CLOSED", "MERGED"],
        "head0": "topic",
        "base0": "main",
    }
    assert "headRefName: $head0" in requests[0][1]
    assert "baseRefName: $base0" in requests[0][1]
    with pytest.raises(ValueError, match="head or base"):
        api.find_pull_requests(repository.identity)


def test_github_api_creates_one_pull_request_and_returns_only_identity(
    monkeypatch,
) -> None:
    repository = sync.GitHubRepository(
        sync.GitHubRepositoryId("github.com", "R_repo"),
        "owner/repo",
        "https://github.com/owner/repo",
        "main",
    )
    api = sync.GitHubAPI(workspace="/repo")
    requests = []

    def request(host, method, path, *, body, cwd):
        requests.append((host, method, path, json.loads(body), cwd))
        return sync.GitHubHttpResponse(
            201,
            (),
            json.dumps(
                {
                    "number": 17,
                    "base": {"repo": {"node_id": repository.identity.node_id}},
                }
            ).encode(),
        )

    monkeypatch.setattr(sync, "github_http_request", request)

    created = api.create_pull_request(
        repository,
        head_branch="topic",
        base_branch="main",
        title="Title",
        body="Body",
        draft=True,
    )

    assert created == sync.PullRequestId(repository.identity, 17)
    assert requests == [
        (
            "github.com",
            "POST",
            "/repos/owner/repo/pulls",
            {
                "head": "topic",
                "base": "main",
                "title": "Title",
                "body": "Body",
                "draft": True,
            },
            Path("/repo"),
        )
    ]


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


def snapshot(*, desired: str, live: str, parent: str, operation: str | None = None):
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
        sync.ToolStateRead(
            None,
            sync.TrackedState((sync.TrackedStack(repository, "main", (pr_key,)),), ()),
            operation,
        ),
        (pr,),
        sync.StandalonePullRequest(pr_key),
        (
            sync.LiveRemoteRef(head_ref, live),
            sync.LiveRemoteRef(base_ref, "b" * 40),
        ),
    )
    return observed, pr_key


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
        sync.ToolStateRead(
            None,
            sync.TrackedState(
                (
                    sync.TrackedStack(
                        repository, "main", tuple(pr.identity for pr in prs)
                    ),
                ),
                (),
            ),
            None,
        ),
        tuple(prs),
        sync.ServerStackMembership(
            selected,
            stack,
            tuple(pr.identity for pr in prs),
        ),
        tuple(live_refs[1:] + live_refs[:1]),
    )
    return observed, sync.StackSelection("main", tuple(assignments))


def test_bounded_commit_revision_includes_comparisons_bookmarks_and_receipts() -> None:
    observed, _selected = stacked_snapshot(count=2)
    repository = observed.repository
    pr = observed.pull_requests[0].identity
    local_target, remote_target, published, adopted = (
        character * 40 for character in "3456"
    )
    local = dataclasses.replace(
        observed.local,
        local_bookmarks=(
            sync.LocalBookmark("local", sync.CommitTarget(local_target)),
            sync.LocalBookmark("absent", sync.AbsentBookmarkTarget()),
        ),
        remote_bookmarks=(
            sync.JjRemoteBookmark(
                "origin",
                "remote",
                sync.CommitTarget(remote_target),
                sync.TrackingState.TRACKED,
            ),
        ),
    )
    state = sync.TrackedState(
        (),
        (
            sync.LastPublishedHead(
                pr,
                sync.RemoteBranchRef(repository, "refs/heads/published"),
                published,
            ),
        ),
        (
            sync.LastAdoptedHead(
                pr,
                sync.RemoteBranchRef(repository, "refs/heads/adopted"),
                adopted,
            ),
        ),
    )

    assert sync._bounded_commit_revision(
        "trunk()..topic", local, state, (("a" * 40, "b" * 40),)
    ) == " | ".join(
        (
            "(trunk()..topic)",
            f"{'a' * 40}::{'b' * 40}",
            local_target,
            remote_target,
            published,
            adopted,
        )
    )


def test_explicit_server_stack_observes_a_bounded_commit_union(monkeypatch) -> None:
    observed, _selected = stacked_snapshot(count=2)
    tracked = observed.tool_state.state.stacks[0]
    observed = dataclasses.replace(
        observed,
        tool_state=dataclasses.replace(
            observed.tool_state,
            state=dataclasses.replace(
                observed.tool_state.state,
                stacks=(tracked, dataclasses.replace(tracked, base_branch="other")),
            ),
        ),
    )
    local_calls = []
    commit_revisions = []

    def observe_local(_workspace, **kwargs):
        local_calls.append(kwargs)
        return observed.local

    def observe_commits(_workspace, operation, revision, **_kwargs):
        assert operation == observed.local.operation_id
        commit_revisions.append(revision)
        return observed.local.commits

    monkeypatch.setattr(sync, "observe_local", observe_local)
    monkeypatch.setattr(sync, "observe_snapshot", lambda *_args, **_kwargs: observed)
    monkeypatch.setattr(sync, "observe_commits", observe_commits)
    github = github_for_publication(first_publication_operation())

    result = sync.plan_explicit_existing(
        "/repo", github, sync.ExistingIntent(1, "trunk()..topic")
    )

    assert isinstance(result, sync.Blocked)
    assert result.reasons[0].code == "tracked-overlap"
    assert local_calls == [
        {
            "revision": "trunk()..topic",
            "config_keys": ("git.push",),
            "ancestry_order": True,
        }
    ]
    expected_terms = ["(trunk()..topic)"]
    expected_terms.extend(
        f"{pr.base_oid}::{pr.head_oid}"
        for pr in observed.pull_requests
    )
    expected_terms.extend(
        item.target.commit_id
        for item in observed.local.local_bookmarks
        if isinstance(item.target, sync.CommitTarget)
    )
    assert commit_revisions == [" | ".join(expected_terms)]
    assert "all()" not in commit_revisions[0]


def test_first_publication_observes_a_bounded_commit_union(monkeypatch) -> None:
    observed, _selected = stacked_snapshot(count=2)
    repository = sync.GitHubRepository(
        observed.repository,
        "owner/repo",
        "https://github.com/owner/repo",
        "main",
    )
    local_calls = []
    commit_revisions = []

    def observe_local(_workspace, **kwargs):
        local_calls.append(kwargs)
        return observed.local

    def observe_commits(_workspace, operation, revision, **_kwargs):
        assert operation == observed.local.operation_id
        commit_revisions.append(revision)
        return observed.local.commits

    blocked = sync._block("stop", "test", "assignment observation reached")
    monkeypatch.setattr(sync, "observe_local", observe_local)
    monkeypatch.setattr(sync, "observe_commits", observe_commits)
    monkeypatch.setattr(sync, "resolve_push_url", lambda *_args: observed.push_url)
    monkeypatch.setattr(sync, "observe_tool_state", lambda *_args: observed.tool_state)
    monkeypatch.setattr(
        sync, "resolve_first_publication_assignments", lambda *_args, **_kwargs: blocked
    )

    server = FakeGitHubServer()
    server.seed_repository(repository, aliases=(observed.push_url,))
    client = FakeGitHubClient(server)
    result = sync.plan_explicit_new(
        "/repo", client, sync.NewIntent("trunk()..topic")
    )

    assert result == blocked
    assert local_calls == [
        {
            "revision": "trunk()..topic",
            "config_keys": ("git.push",),
            "ancestry_order": True,
        }
    ]
    expected = " | ".join(
        (
            "(trunk()..topic)",
            *(
                item.target.commit_id
                for item in observed.local.local_bookmarks
                if isinstance(item.target, sync.CommitTarget)
            ),
        )
    )
    assert commit_revisions == [expected]
    assert "all()" not in commit_revisions[0]


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


def test_merged_selector_identifies_stack_without_becoming_active() -> None:
    observed, selected = stacked_snapshot(count=3, merged_prefix=1)

    desired = sync.derive_desired(observed, selected)
    plan = sync.plan_sync(observed, desired)

    assert isinstance(desired, sync.DesiredStack)
    assert tuple(item.pr_identity.number for item in desired.active) == (2, 3)
    assert observed.membership.selected_pr.number == 1
    assert isinstance(plan, sync.NoOp)


def test_tracked_and_explicit_selection_have_distinct_membership_policy() -> None:
    observed, selected = stacked_snapshot(count=2)
    observed = dataclasses.replace(
        observed,
        tool_state=dataclasses.replace(observed.tool_state, state=sync.EMPTY_STATE),
    )

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


def topology_input(
    desired_order: tuple[int, ...],
) -> sync.TopologyPlanningInput | sync.Blocked:
    observed, _ = stacked_snapshot(count=2)
    receipts = tuple(
        sync.LastPublishedHead(
            pr.identity,
            sync.RemoteBranchRef(
                observed.repository, f"refs/heads/{pr.head_branch}"
            ),
            pr.head_oid,
        )
        for pr in observed.pull_requests
    )
    observed = dataclasses.replace(
        observed,
        tool_state=dataclasses.replace(
            observed.tool_state,
            state=dataclasses.replace(
                observed.tool_state.state, last_published_heads=receipts
            ),
        ),
    )
    base = "b" * 40
    new_commits = (
        sync.ObservedCommit("3" * 40, (base,), "new-2", "Two", False, False),
        sync.ObservedCommit("4" * 40, ("3" * 40,), "new-1", "One", False, False),
    )
    observed = dataclasses.replace(
        observed,
        local=dataclasses.replace(
            observed.local, commits=observed.local.commits + new_commits
        ),
    )
    wanted = {1: "4" * 40, 2: "3" * 40}
    desired = sync.DesiredStack(
        observed.repository,
        "main",
        tuple(
            sync.DesiredExistingPR(
                observed.pull_requests[index - 1].identity,
                wanted[index],
                str(index),
                "",
            )
            for index in desired_order
        ),
    )
    repository = sync.GitHubRepository(
        observed.repository,
        "owner/repo",
        "https://github.com/owner/repo",
        "main",
    )
    source = sync.prepare_topology_planning(
        observed, repository, "origin", desired
    )
    assert isinstance(source, sync.TopologyPlanningSource)
    return sync.complete_topology_planning_input(
        source,
        tuple(sync.LiveRemoteRef(ref, None) for ref in source.temporary_base_refs),
    )


def tracked_restore_input(
    *, association: str = "none"
) -> sync.TrackedRestorePlanningInput:
    snapshot, selected = stacked_snapshot(count=3)
    tracked = snapshot.tool_state.state.stacks[0]
    prs = tuple(
        dataclasses.replace(
            pr,
            base_branch="main",
            base_oid="b" * 40,
            stack=None,
        )
        for pr in snapshot.pull_requests
    )
    stack = None
    stack_members: tuple[sync.GitHubPullRequest, ...] = ()
    if association == "partial":
        stack_members = prs[:2]
    elif association == "full":
        stack_members = prs
    if stack_members:
        identity = sync.GitHubStackId(snapshot.repository, 41)
        summary = sync.GitHubStackSummary(identity, "STACK_surviving", "main")
        stack = sync.GitHubStack(
            identity,
            "STACK_surviving",
            "main",
            tuple(pr.identity for pr in stack_members),
        )
        prs = tuple(
            dataclasses.replace(
                pr, stack=summary if pr in stack_members else None
            )
            for pr in prs
        )
    live = {
        item.ref: item
        for item in snapshot.live_refs
        if item.ref.full_name == "refs/heads/main"
        or item.ref.full_name.startswith("refs/heads/topic-")
    }
    desired = sync.DesiredStack(
        snapshot.repository,
        "main",
        tuple(
            sync.DesiredExistingPR(
                assignment.pr_identity,
                assignment.commit_id,
                prs[index].title,
                prs[index].body,
            )
            for index, assignment in enumerate(selected.ordered)
        ),
    )
    observation = sync.TrackedRestoreObservation(
        snapshot.local,
        snapshot.tool_state,
        sync.GitHubRepository(
            snapshot.repository,
            "owner/repo",
            "https://github.com/owner/repo",
            "main",
        ),
        snapshot.push_url,
        "origin",
        tracked,
        prs,
        stack,
        tuple(live.values()),
        desired,
    )
    source = sync.prepare_tracked_restore_planning(observation)
    assert isinstance(source, sync.TrackedRestorePlanningSource)
    completed = sync.complete_tracked_restore_planning_input(
        source,
        tuple(sync.LiveRemoteRef(ref, None) for ref in source.temporary_base_refs),
    )
    assert isinstance(completed, sync.TrackedRestorePlanningInput)
    return completed


@pytest.mark.parametrize("association", ("none", "partial"))
def test_tracked_restore_plans_flattened_or_partially_surviving_stack(
    association: str,
) -> None:
    plan = sync.plan_tracked_restore(tracked_restore_input(association=association))

    assert isinstance(plan, sync.TopologyPlan)
    assert isinstance(plan.source, sync.TrackedRestoreSource)
    assert (plan.source.current_stack is None) is (association == "none")
    assert plan.head_updates == ()
    assert len(plan.temporary_bases) == 3
    assert plan.tracking_update == plan.source.tracked
    assert sync.parse_topology_repair(
        sync.topology_repair_to_json(sync.TopologyRepair(plan))
    ) == sync.TopologyRepair(plan)


def test_tracked_restore_blocks_multiple_surviving_stacks() -> None:
    value = tracked_restore_input(association="partial")
    sources = value.observation.pull_requests
    other = sync.GitHubStackSummary(
        sync.GitHubStackId(value.observation.repository.identity, 99),
        "STACK_other",
        "main",
    )
    changed = dataclasses.replace(
        value.observation,
        pull_requests=(
            *sources[:2],
            dataclasses.replace(sources[2], stack=other),
        ),
    )
    result = sync.plan_tracked_restore(
        dataclasses.replace(value, observation=changed)
    )
    assert isinstance(result, sync.Blocked)
    assert result.reasons[0].code == "ambiguous-membership"


def test_tracked_restore_final_state_preserves_detached_without_head_receipts() -> None:
    value = tracked_restore_input()
    detached = sync.PullRequestId(value.observation.tracked.repository, 99)
    tracked = dataclasses.replace(value.observation.tracked, detached_prs=(detached,))
    state = dataclasses.replace(
        value.observation.tool_state.state, stacks=(tracked,)
    )
    observation = dataclasses.replace(
        value.observation,
        tracked=tracked,
        tool_state=dataclasses.replace(value.observation.tool_state, state=state),
    )
    plan = sync.plan_tracked_restore(
        dataclasses.replace(value, observation=observation)
    )
    assert isinstance(plan, sync.TopologyPlan)
    assert plan.head_updates == ()

    final = sync._build_topology_final_state(plan, state)

    assert final.stacks == (tracked,)
    assert final.last_published_heads == ()


@pytest.mark.parametrize(
    ("desired_order", "active", "detached"),
    (
        ((2, 1), (2, 1), ()),
        ((1,), (1,), (2,)),
        ((), (), (1, 2)),
    ),
)
def test_fully_open_topology_plans_reorder_subset_and_empty(
    desired_order: tuple[int, ...], active: tuple[int, ...], detached: tuple[int, ...]
) -> None:
    plan = sync.plan_topology(topology_input(desired_order))

    assert isinstance(plan, sync.TopologyPlan)
    assert plan.tracking_update is not None
    assert tuple(pr.number for pr in plan.tracking_update.ordered_prs) == active
    assert tuple(pr.number for pr in plan.detached_prs) == detached
    assert len(plan.temporary_bases) == len(active)
    if desired_order == (2, 1):
        assert tuple(
            (
                temporary_base.pr_identity.number,
                temporary_base.old_base_commit_id,
                temporary_base.new_base_commit_id,
            )
            for temporary_base in plan.temporary_bases
        ) == (
            (2, "1" * 40, "b" * 40),
            (1, "b" * 40, "3" * 40),
        )
    rendered = sync.render_topology(plan)
    assert "stack #17" in rendered
    assert "STACK_node" not in rendered
    assert "PR_" not in rendered
    assert f"to [{', '.join(f'#{number}' for number in desired_order)}]" in rendered
    for number in detached:
        assert f"detach PR #{number}" in rendered
    assert "leave open" in rendered or not detached


def test_topology_temporary_base_names_are_stable_and_require_absence() -> None:
    observed, _ = stacked_snapshot(count=2)
    desired = sync.DesiredStack(
        observed.repository,
        "main",
        (
            sync.DesiredExistingPR(
                observed.pull_requests[0].identity, "1" * 40, "T", ""
            ),
        ),
    )
    repository = sync.GitHubRepository(
        observed.repository,
        "owner/repo",
        "https://github.com/owner/repo",
        "main",
    )
    first = sync.prepare_topology_planning(observed, repository, "origin", desired)
    second = sync.prepare_topology_planning(observed, repository, "origin", desired)
    assert isinstance(first, sync.TopologyPlanningSource)
    assert isinstance(second, sync.TopologyPlanningSource)
    assert first.temporary_base_refs == second.temporary_base_refs

    changed_goal = dataclasses.replace(
        desired,
        active=(
            dataclasses.replace(desired.active[0], desired_commit_id="9" * 40),
        ),
    )
    changed = sync.prepare_topology_planning(
        observed, repository, "origin", changed_goal
    )
    assert isinstance(changed, sync.TopologyPlanningSource)
    assert changed.temporary_base_refs != first.temporary_base_refs

    excluded = dataclasses.replace(
        observed,
        local=dataclasses.replace(observed.local, operation_id="other-operation"),
        tool_state=dataclasses.replace(
            observed.tool_state, state_blob_oid="f" * 40, operation_blob_oid="e" * 40
        ),
        membership=dataclasses.replace(
            observed.membership,
            selected_pr=observed.pull_requests[1].identity,
            stack=dataclasses.replace(
                observed.membership.stack,
                identity=dataclasses.replace(
                    observed.membership.stack.identity, number=999
                ),
            ),
        ),
    )
    stable = sync.prepare_topology_planning(excluded, repository, "elsewhere", desired)
    assert isinstance(stable, sync.TopologyPlanningSource)
    assert stable.naming_digest == first.naming_digest
    assert stable.temporary_base_refs == first.temporary_base_refs
    assert tuple(ref.full_name.rsplit("/", 1)[1] for ref in first.temporary_base_refs) == (
        "1",
    )
    assert first.temporary_base_refs[0].full_name.startswith(
        "refs/heads/jj-stack/temporary-bases/"
    )

    missing = sync.complete_topology_planning_input(first, ())
    occupied = sync.complete_topology_planning_input(
        first, (sync.LiveRemoteRef(first.temporary_base_refs[0], "9" * 40),)
    )
    assert isinstance(missing, sync.Blocked)
    assert missing.reasons[0].code == "temporary-base-observation-missing"
    assert isinstance(occupied, sync.Blocked)
    assert occupied.reasons[0].code == "temporary-base-occupied"


def test_topology_naming_digest_is_sensitive_to_desired_order() -> None:
    observed, _ = stacked_snapshot(count=2)
    active = tuple(
        sync.DesiredExistingPR(
            pr.identity, pr.head_oid, pr.title, pr.body
        )
        for pr in observed.pull_requests
    )
    forward = sync.DesiredStack(observed.repository, "main", active)
    reverse = dataclasses.replace(forward, active=tuple(reversed(active)))

    assert sync._topology_naming_digest(
        observed, forward
    ) != sync._topology_naming_digest(observed, reverse)


def test_topology_planning_is_pure_and_checks_both_base_maps(monkeypatch) -> None:
    value = topology_input((2, 1))
    monkeypatch.setattr(sync.subprocess, "run", lambda *a, **k: pytest.fail("I/O"))
    assert isinstance(sync.plan_topology(value), sync.TopologyPlan)
    assert isinstance(value, sync.TopologyPlanningInput)

    old_map_unsafe = dataclasses.replace(
        value,
        snapshot=dataclasses.replace(
            value.snapshot,
            local=dataclasses.replace(
                value.snapshot.local,
                commits=tuple(
                    dataclasses.replace(c, parent_commit_ids=("b" * 40,))
                    if c.commit_id == "2" * 40
                    else c
                    for c in value.snapshot.local.commits
                ),
            ),
        ),
    )
    old_result = sync.plan_topology(old_map_unsafe)
    assert isinstance(old_result, sync.Blocked)
    assert old_result.reasons[0].code == "invalid-old-comparison"

    desired_map_unsafe = dataclasses.replace(
        value,
        snapshot=dataclasses.replace(
            value.snapshot,
            local=dataclasses.replace(
                value.snapshot.local,
                commits=tuple(
                    dataclasses.replace(c, parent_commit_ids=("b" * 40,))
                    if c.commit_id == "4" * 40
                    else c
                    for c in value.snapshot.local.commits
                ),
            ),
        ),
    )
    desired_result = sync.plan_topology(desired_map_unsafe)
    assert isinstance(desired_result, sync.Blocked)
    assert desired_result.reasons[0].code == "invalid-desired-comparison"


def test_topology_operation_round_trip_is_strict_and_exact() -> None:
    plan = sync.plan_topology(topology_input((2, 1)))
    assert isinstance(plan, sync.TopologyPlan)
    operation = sync.TopologyRepair(plan)

    encoded = sync.topology_repair_to_json(operation)
    payload = json.loads(encoded)

    assert payload["operation_kind"] == "topology-repair"
    assert payload["plan"]["dependencies"]["source"]["source_kind"] == "server-stack"
    assert sync.parse_operation(encoded) == operation
    del payload["operation_kind"]
    with pytest.raises(sync.Error, match="unknown operation_kind"):
        sync.parse_operation(json.dumps(payload))


def github_for_topology(
    plan: sync.TopologyPlan, *, stacked: bool = True
) -> tuple[FakeGitHubServer, FakeGitHubClient]:
    repository = sync.GitHubRepository(
        plan.repository,
        plan.repository_name,
        f"https://{plan.repository.host}/{plan.repository_name}",
        plan.desired.base_branch,
    )
    server = FakeGitHubServer()
    server.seed_repository(repository)
    for pull_request in plan.dependencies.prs:
        server.seed_pull_request(dataclasses.replace(pull_request, stack=None))
    if stacked and isinstance(plan.source, sync.ServerStackMembership):
        server.seed_stack(
            sync.GitHubStack(
                plan.source.stack.identity,
                plan.source.stack.node_id,
                plan.source.stack.base_branch,
                plan.source.ordered_prs,
            )
        )
    return server, FakeGitHubClient(server)


@pytest.mark.parametrize("initial", ("absent", "exact"))
def test_topology_creation_reconciles_complete_readback(
    monkeypatch, initial: str
) -> None:
    plan = sync.plan_topology(topology_input((2, 1)))
    assert isinstance(plan, sync.TopologyPlan)
    operation = sync.TopologyRepair(plan)
    _server, client = github_for_topology(plan)
    old = tuple(item.old_base_commit_id for item in plan.temporary_bases)
    absent = (None,) * len(old)
    observations = [absent if initial == "absent" else old]
    if initial == "absent":
        observations.append(old)
    pushes: list[tuple[sync.PlannedTemporaryBase, ...]] = []

    monkeypatch.setattr(sync, "repository_lock", lambda _workspace: nullcontext())
    monkeypatch.setattr(sync, "read_operation", lambda _workspace: ("op", operation))
    monkeypatch.setattr(sync, "cas_write_topology_repair", lambda *a, **k: "next")
    monkeypatch.setattr(
        sync,
        "_topology_refs",
        lambda plan, workspace, refs: tuple(
            sync.LiveRemoteRef(ref, oid)
            for ref, oid in zip(refs, observations.pop(0), strict=True)
        ),
    )
    monkeypatch.setattr(
        sync,
        "create_temporary_bases",
        lambda workspace, push_url, bases: pushes.append(tuple(bases)),
    )
    monkeypatch.setattr(
        sync,
        "_observe_source_association",
        lambda *a, **k: (_ for _ in ()).throw(sync.Error("stop after creation")),
    )

    result = sync.resume_topology_repair(".", client)

    assert isinstance(result, sync.Stopped)
    assert result.stage == sync.TopologyRepairPhase.UNSTACKING.value
    assert len(pushes) == (1 if initial == "absent" else 0)


def test_topology_publication_exact_readback_advances_without_repush(
    monkeypatch,
) -> None:
    plan = sync.plan_topology(topology_input((2, 1)))
    assert isinstance(plan, sync.TopologyPlan)
    operation = sync.TopologyRepair(plan, sync.TopologyRepairPhase.PUBLISHING)
    _server, client = github_for_topology(plan, stacked=False)
    desired = tuple(item.new_commit_id for item in plan.head_updates) + tuple(
        item.new_base_commit_id for item in plan.temporary_bases
    )
    repushes: list[object] = []

    monkeypatch.setattr(sync, "repository_lock", lambda _workspace: nullcontext())
    monkeypatch.setattr(sync, "read_operation", lambda _workspace: ("op", operation))
    monkeypatch.setattr(sync, "cas_write_topology_repair", lambda *a, **k: "next")
    monkeypatch.setattr(
        sync,
        "_topology_refs",
        lambda plan, workspace, refs: tuple(
            sync.LiveRemoteRef(ref, oid) for ref, oid in zip(refs, desired, strict=True)
        ),
    )
    monkeypatch.setattr(sync, "move_temporary_bases", lambda *a: repushes.append(a))
    monkeypatch.setattr(
        sync,
        "_topology_desired_bases",
        lambda _plan: (_ for _ in ()).throw(sync.Error("stop after publishing")),
    )

    result = sync.resume_topology_repair(".", client)

    assert isinstance(result, sync.Stopped)
    assert result.stage == sync.TopologyRepairPhase.RELINKING.value
    assert repushes == []


def test_topology_cleanup_foreign_ref_retains_operation_fence(monkeypatch) -> None:
    plan = sync.plan_topology(topology_input((2, 1)))
    assert isinstance(plan, sync.TopologyPlan)
    final_state = sync.state_to_json(sync.EMPTY_STATE)
    operation = sync.TopologyRepair(
        plan,
        sync.TopologyRepairPhase.COMMITTING,
        final_state_json=final_state,
        final_state_oid="f" * 40,
    )
    _server, client = github_for_topology(plan, stacked=False)
    deleted: list[str] = []

    monkeypatch.setattr(sync, "repository_lock", lambda _workspace: nullcontext())
    monkeypatch.setattr(sync, "read_operation", lambda _workspace: ("op", operation))
    monkeypatch.setattr(sync, "read_ref_oid", lambda *a: "f" * 40)
    monkeypatch.setattr(
        sync,
        "_topology_refs",
        lambda plan, workspace, refs: tuple(
            sync.LiveRemoteRef(ref, "9" * 40) for ref in refs
        ),
    )
    monkeypatch.setattr(
        sync, "cas_delete_operation", lambda *a: deleted.append("deleted")
    )

    result = sync.resume_topology_repair(".", client)

    assert isinstance(result, sync.Stopped)
    assert result.stage == "cleanup"
    assert deleted == []


def test_detached_association_list_and_forget_are_local_only(monkeypatch) -> None:
    repository = sync.GitHubRepositoryId("github.com", "R")
    active = sync.PullRequestId(repository, 1)
    detached = sync.PullRequestId(repository, 2)
    authority = sync.LastPublishedHead(
        active, sync.RemoteBranchRef(repository, "refs/heads/active"), "a" * 40
    )
    state = sync.TrackedState(
        (sync.TrackedStack(repository, "main", (active,), (detached,)),),
        (authority,),
    )
    selected = sync.DetachedAssociation(repository, "main", detached)
    written = []
    monkeypatch.setattr(sync, "read_state", lambda _workspace: ("old", state))
    monkeypatch.setattr(sync, "read_ref_oid", lambda *_args: None)
    monkeypatch.setattr(sync, "repository_lock", lambda _workspace: nullcontext())
    monkeypatch.setattr(
        sync,
        "cas_write_state",
        lambda _workspace, expected, value: written.append((expected, value)) or "new",
    )
    assert sync.list_detached_associations("/work") == (selected,)
    result = sync.forget_detached_association("/work", selected)

    assert result == sync.DetachedAssociationResult(selected, "new")
    assert written[0][1].stacks == (
        sync.TrackedStack(repository, "main", (active,), ()),
    )
    assert written[0][1].last_published_heads == (authority,)


def test_detached_number_resolution_is_local_and_bounded(monkeypatch) -> None:
    first_repo = sync.GitHubRepositoryId("github.com", "R_first")
    second_repo = sync.GitHubRepositoryId("github.example", "R_second")
    first = sync.DetachedAssociation(
        first_repo, "main", sync.PullRequestId(first_repo, 12)
    )
    second = sync.DetachedAssociation(
        second_repo, "trunk", sync.PullRequestId(second_repo, 42)
    )
    monkeypatch.setattr(
        sync, "list_detached_associations", lambda _workspace: (first, second)
    )
    assert sync.resolve_detached_association("/work", 42) is second
    assert isinstance(sync.resolve_detached_association("/work", 99), sync.Blocked)


def test_close_detached_accepts_lost_response_after_closed_readback(monkeypatch) -> None:
    repository = sync.GitHubRepositoryId("github.example", "R")
    detached = sync.PullRequestId(repository, 42)
    state = sync.TrackedState(
        (sync.TrackedStack(repository, "main", (), (detached,)),), ()
    )
    selected = sync.DetachedAssociation(repository, "main", detached)
    server = FakeGitHubServer()
    server.seed_repository(
        sync.GitHubRepository(
            repository,
            "owner/repo",
            "https://github.example/owner/repo",
            "main",
        )
    )
    server.seed_pull_request(
        sync.GitHubPullRequest(
            detached,
            "PR_42",
            sync.PullRequestState.OPEN,
            False,
            repository,
            "topic",
            "1" * 40,
            "main",
            "0" * 40,
            False,
            False,
            "Detached",
            "",
            None,
        )
    )
    client = FakeGitHubClient(server)
    written = []
    monkeypatch.setattr(sync, "read_state", lambda _workspace: ("old", state))
    monkeypatch.setattr(sync, "read_ref_oid", lambda *_args: None)
    monkeypatch.setattr(sync, "repository_lock", lambda _workspace: nullcontext())
    monkeypatch.setattr(
        sync,
        "cas_write_state",
        lambda _workspace, expected, value: written.append((expected, value)) or "new",
    )

    with client.lose_response(sync.GitHubClient.update_pull_request, pr=detached):
        sync.close_detached_association("/work", client, selected)

    assert written == [("old", sync.EMPTY_STATE)]
    assert (
        server.read_pull_requests((detached,))[0].state
        is sync.PullRequestState.CLOSED
    )


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
        sync.plan_sync(fenced, sync.derive_desired(fenced, selection(fenced, pr)))
    ).startswith("blocked [operation-fenced]")


def standalone_plan(
    *, desired: str, live: str, parent: str
) -> tuple[sync.Snapshot, sync.NoOp | sync.Apply]:
    observed, pr = snapshot(desired=desired, live=live, parent=parent)
    plan = sync.plan_sync(
        observed, sync.derive_desired(observed, selection(observed, pr))
    )
    assert isinstance(plan, sync.NoOp | sync.Apply)
    return observed, plan


def final_snapshot(
    observed: sync.Snapshot, plan: sync.NoOp | sync.Apply
) -> sync.Snapshot:
    desired = {item.pr_identity: item for item in plan.desired.active}
    heads = {
        f"refs/heads/{pr.head_branch}": (
            desired[pr.identity].desired_commit_id
            if pr.identity in desired
            else pr.head_oid
        )
        for pr in observed.pull_requests
    }
    heads.update(
        {
            ref.ref.full_name: ref.commit_id
            for ref in observed.live_refs
            if ref.ref.full_name not in heads
        }
    )
    prs = tuple(
        pr
        if pr.identity not in desired
        else dataclasses.replace(
            pr,
            head_oid=desired[pr.identity].desired_commit_id,
            base_oid=heads[f"refs/heads/{pr.base_branch}"],
            title=desired[pr.identity].title,
            body=desired[pr.identity].body,
        )
        for pr in observed.pull_requests
    )
    refs = tuple(
        dataclasses.replace(ref, commit_id=heads[ref.ref.full_name])
        if ref.ref.full_name in heads
        else ref
        for ref in observed.live_refs
    )
    return dataclasses.replace(observed, pull_requests=prs, live_refs=refs)


def github_for(
    observed: sync.Snapshot,
) -> tuple[FakeGitHubServer, FakeGitHubClient]:
    repository = sync.GitHubRepository(
        observed.repository,
        "owner/repo",
        "https://github.com/owner/repo",
        "main",
    )
    server = FakeGitHubServer()
    server.seed_repository(
        repository, aliases=(observed.push_url, observed.fetch_url)
    )
    for pull_request in observed.pull_requests:
        server.seed_pull_request(dataclasses.replace(pull_request, stack=None))
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


def remote_restack_plan() -> tuple[
    sync.Snapshot, sync.Snapshot, sync.RemoteRestackAdoption
]:
    old = "1" * 40
    new = "2" * 40
    base = "0" * 40
    observed, pr = snapshot(desired=old, live=new, parent=base)
    observed = dataclasses.replace(
        observed,
        fetch_url="ssh://git@github.com/o/r.git",
        local=dataclasses.replace(
            observed.local,
            remote_bookmarks=(
                sync.JjRemoteBookmark(
                    "origin",
                    "topic",
                    sync.CommitTarget(old),
                    sync.TrackingState.TRACKED,
                ),
            ),
        ),
    )
    boundary = sync.RemoteRestackBoundary(
        pr,
        "topic",
        old,
        old,
        new,
        (sync.RewriteCommitPair(old, new, "change", "patch"),),
    )
    plan = sync.RemoteRestackAdoption(
        observed.repository,
        "origin",
        observed.fetch_url,
        sync.operation_config(observed.local),
        observed.tool_state.state_blob_oid,
        observed.tool_state.operation_blob_oid,
        observed.membership,
        observed.pull_requests,
        observed.live_refs,
        (boundary,),
        (("default", "change"),),
    )
    adopted_commit = sync.ObservedCommit(
        new, (base,), "change", "Desired title\n\nDesired body", False, False
    )
    superseded_commit = sync.ObservedCommit(
        old, (base,), "change", "Desired title\n\nDesired body", False, True
    )
    after = dataclasses.replace(
        observed,
        local=dataclasses.replace(
            observed.local,
            workspace_targets=(("default", new),),
            local_bookmarks=(sync.LocalBookmark("topic", sync.CommitTarget(new)),),
            remote_bookmarks=(
                sync.JjRemoteBookmark(
                    "origin",
                    "topic",
                    sync.CommitTarget(new),
                    sync.TrackingState.TRACKED,
                ),
            ),
            commits=(adopted_commit, superseded_commit),
        ),
    )
    return observed, after, plan


def test_remote_restack_adoption_fetches_only_frozen_branches_and_records_authority(
    monkeypatch,
) -> None:
    before, after, plan = remote_restack_plan()
    observations = iter((before, after))
    commands: list[list[str]] = []
    recorded: list[tuple[sync.LastAdoptedHead, ...]] = []
    monkeypatch.setattr(sync, "repository_lock", lambda *_args: nullcontext())
    monkeypatch.setattr(
        sync, "reobserve_for_adoption", lambda *_args: next(observations)
    )
    monkeypatch.setattr(
        sync, "reobserve_after_adoption", lambda *_args: next(observations)
    )
    monkeypatch.setattr(
        sync.subprocess,
        "run",
        lambda command, **_kwargs: (
            commands.append(command) or subprocess.CompletedProcess(command, 0, "", "")
        ),
    )
    monkeypatch.setattr(
        sync,
        "record_verified_adoptions",
        lambda _workspace, _oid, _state, receipts: recorded.append(tuple(receipts)),
    )

    _server, client = github_for(before)
    result = sync.adopt_remote_restack(plan, "/repo", client)

    expected = sync.LastAdoptedHead(
        plan.boundaries[0].pr_identity,
        sync.RemoteBranchRef(plan.repository, "refs/heads/topic"),
        "2" * 40,
    )
    assert result == sync.AdoptionVerified((expected,))
    assert commands == [
        ["jj", "git", "fetch", "--remote", "origin", "--branch", "topic"]
    ]
    assert recorded == [(expected,)]


@pytest.mark.parametrize(
    ("commit_index", "is_hidden", "expected"),
    (
        (1, False, "superseded commit remains visible for topic"),
        (0, True, "adopted boundary is absent, hidden, or conflicted for topic"),
    ),
)
def test_remote_restack_adoption_requires_old_commit_hidden_and_new_commit_visible(
    commit_index: int, is_hidden: bool, expected: str
) -> None:
    _before, after, plan = remote_restack_plan()
    commits = list(after.local.commits)
    commits[commit_index] = dataclasses.replace(
        commits[commit_index], is_hidden=is_hidden
    )
    after = dataclasses.replace(
        after, local=dataclasses.replace(after.local, commits=tuple(commits))
    )

    assert sync._validate_adoption_poststate(plan, after) == expected


def test_adoption_pre_fetch_reobservation_never_resolves_remote_only_oids(
    monkeypatch,
) -> None:
    before, after, plan = remote_restack_plan()
    revisions: list[str] = []

    def observe(_workspace, _github, *, revision, **_kwargs):
        revisions.append(revision)
        return before if len(revisions) == 1 else after

    monkeypatch.setattr(sync, "observe_snapshot", observe)

    _server, client = github_for(before)
    assert sync.reobserve_for_adoption("/repo", client, plan) == before
    assert sync.reobserve_after_adoption("/repo", client, plan) == after
    old = plan.boundaries[0].local_commit_id
    remote_only = plan.boundaries[0].remote_commit_id
    assert old in revisions[0]
    assert remote_only not in revisions[0]
    assert old in revisions[1]
    assert remote_only in revisions[1]


def test_remote_restack_adoption_blocks_mixed_local_and_remote_movement() -> None:
    before, _after, plan = remote_restack_plan()
    moved = dataclasses.replace(
        before,
        local=dataclasses.replace(
            before.local,
            local_bookmarks=(sync.LocalBookmark("topic", sync.CommitTarget("3" * 40)),),
        ),
    )

    assert sync._validate_adoption_prestate(plan, moved) == "L/T changed for topic"


def test_adopted_authority_is_distinct_and_can_authorize_replacement() -> None:
    observed, pr = snapshot(desired="3" * 40, live="2" * 40, parent="0" * 40)
    ref = sync.RemoteBranchRef(observed.repository, "refs/heads/topic")
    receipt = sync.LastAdoptedHead(pr, ref, "2" * 40)
    observed = dataclasses.replace(
        observed,
        tool_state=dataclasses.replace(
            observed.tool_state,
            state=dataclasses.replace(
                observed.tool_state.state, last_adopted_heads=(receipt,)
            ),
        ),
    )

    plan = sync.plan_sync(
        observed, sync.derive_desired(observed, selection(observed, pr))
    )

    assert isinstance(plan, sync.Apply)
    assert plan.head_updates[0].authority == sync.MatchesLastAdoption(receipt)


def first_publication_operation(
    *,
    phase: sync.NewPRPhase = sync.NewPRPhase.NOT_ATTEMPTED,
    operation_phase: sync.FirstPublicationPhase = sync.FirstPublicationPhase.PREPARING_BOOKMARKS,
    setup: sync.BookmarkSetup = sync.BookmarkSetup.CREATE,
) -> sync.FirstPublication:
    repository = sync.GitHubRepositoryId("github.com", "R_repo")
    slot = sync.NewPRSlot(
        "slot-123",
        "jj-stack/change-slot123",
        "2" * 40,
        setup,
        "main",
        "New title",
        "New body",
        phase,
    )
    return sync.FirstPublication(
        repository,
        "owner/repo",
        "ssh://git@github.com/owner/repo.git",
        "origin",
        (),
        (("default", "2" * 40),),
        "main",
        None,
        sync.FirstPublicationTarget.STANDALONE,
        (),
        None,
        (slot,),
        sync.StackLinkPhase.NOT_REQUIRED,
        None,
        operation_phase,
    )


def github_for_publication(
    operation: sync.FirstPublication,
) -> tuple[FakeGitHubServer, FakeGitHubClient]:
    repository = sync.GitHubRepository(
        operation.repository,
        operation.repository_name,
        "https://github.com/owner/repo",
        operation.base_branch,
    )
    server = FakeGitHubServer()
    server.seed_repository(repository, aliases=(operation.push_url,))
    branches = {slot.base_branch: "0" * 40 for slot in operation.slots}
    branches.update({slot.branch: slot.commit_id for slot in operation.slots})
    for branch, oid in branches.items():
        server.seed_branch(operation.repository, branch, oid)
    return server, FakeGitHubClient(server)


def test_first_publication_operation_round_trip_is_exact() -> None:
    operation = first_publication_operation()

    assert (
        sync.parse_first_publication(sync.first_publication_to_json(operation))
        == operation
    )
    malformed = json.loads(sync.first_publication_to_json(operation))
    malformed["slots"][0]["unexpected"] = True
    with pytest.raises(sync.Error, match="operation payload"):
        sync.parse_first_publication(json.dumps(malformed))
    untagged = json.loads(sync.first_publication_to_json(operation))
    del untagged["operation_kind"]
    with pytest.raises(sync.Error, match="operation payload"):
        sync.parse_first_publication(json.dumps(untagged))


def test_standalone_first_publication_is_state_first_and_fence_last(
    jj_repo: Path, monkeypatch
) -> None:
    operation = first_publication_operation(setup=sync.BookmarkSetup.KEEP)
    slot = operation.slots[0]
    server, client = github_for_publication(operation)

    def observe(_workspace, current):
        published = (
            current.phase is not sync.FirstPublicationPhase.PREPARING_BOOKMARKS
            and current.slots[0].phase is not sync.NewPRPhase.NOT_ATTEMPTED
        )
        local = sync.LocalObservation(
            "/repo",
            "op",
            current.effective_config,
            current.workspace_targets,
            (sync.LocalBookmark(slot.branch, sync.CommitTarget(slot.commit_id)),),
            (),
            (),
            (),
            "/git",
        )
        return local, (
            sync.PublicationSlotReadback(
                slot.slot_id,
                sync.CommitTarget(slot.commit_id),
                sync.CommitTarget(slot.commit_id) if published else None,
                sync.TrackingState.TRACKED if published else None,
                slot.commit_id if published else None,
            ),
        )

    monkeypatch.setattr(sync, "observe_publication_slots", observe)
    monkeypatch.setattr(
        sync,
        "push_new_slot_refs",
        lambda *_args: subprocess.CompletedProcess([], 0, "", ""),
    )
    original_delete = sync.cas_delete_operation
    monkeypatch.setattr(
        sync,
        "cas_delete_operation",
        lambda *_args: (_ for _ in ()).throw(sync.Error("injected cleanup failure")),
    )

    sync.start_first_publication(operation, jj_repo)
    interrupted = sync.resume_first_publication(jj_repo, client)

    assert isinstance(interrupted, sync.Stopped)
    assert interrupted.stage == "fence"
    state_oid, state = sync.read_state(jj_repo)
    operation_oid, persisted = sync.read_first_publication(jj_repo)
    assert state_oid == persisted.final_state_oid
    assert persisted.phase is sync.FirstPublicationPhase.COMMITTING
    created = sync.PullRequestId(operation.repository, 1)
    assert state.stacks[0].ordered_prs == (created,)
    assert (
        state.last_published_heads[0].verified_commit_id == operation.slots[0].commit_id
    )

    monkeypatch.setattr(sync, "cas_delete_operation", original_delete)
    completed = sync.resume_first_publication(jj_repo, client)

    assert completed == sync.FirstPublicationVerified((created,))
    pull_request = server.read_pull_requests((created,))[0]
    assert (
        pull_request.head_branch,
        pull_request.head_oid,
        pull_request.base_branch,
        pull_request.title,
        pull_request.body,
    ) == (
        slot.branch,
        slot.commit_id,
        slot.base_branch,
        slot.title,
        slot.body,
    )
    assert sync.read_ref_oid(jj_repo, sync.OPERATION_REF) is None
    assert sync.read_ref_oid(jj_repo, sync.STATE_REF) == state_oid
    assert operation_oid is not None


def test_possibly_sent_pr_create_with_negative_readback_never_replays(
    jj_repo: Path, monkeypatch
) -> None:
    operation = first_publication_operation(
        phase=sync.NewPRPhase.READY,
        operation_phase=sync.FirstPublicationPhase.CREATING_PRS,
        setup=sync.BookmarkSetup.KEEP,
    )
    slot = operation.slots[0]
    server, client = github_for_publication(operation)
    local = sync.LocalObservation(
        "/repo",
        "op",
        operation.effective_config,
        operation.workspace_targets,
        (sync.LocalBookmark(slot.branch, sync.CommitTarget(slot.commit_id)),),
        (),
        (),
        (),
        "/git",
    )
    monkeypatch.setattr(
        sync,
        "observe_publication_slots",
        lambda *_args: (
            local,
            (
                sync.PublicationSlotReadback(
                    slot.slot_id,
                    sync.CommitTarget(slot.commit_id),
                    sync.CommitTarget(slot.commit_id),
                    sync.TrackingState.TRACKED,
                    slot.commit_id,
                ),
            ),
        ),
    )
    sync.cas_write_first_publication(jj_repo, None, operation)

    with client.fail_before(sync.GitHubClient.create_pull_request):
        first = sync.resume_first_publication(jj_repo, client)
    second = sync.resume_first_publication(jj_repo, client)

    assert isinstance(first, sync.Stopped) and first.stage == "create-pr"
    assert isinstance(second, sync.Stopped) and second.stage == "create-pr"
    assert server.read_find_pull_requests(
        operation.repository, head_branches=(slot.branch,)
    ) == ()
    _oid, persisted = sync.read_first_publication(jj_repo)
    assert persisted.slots[0].phase is sync.NewPRPhase.POSSIBLY_SENT


def test_publication_recovery_classifies_partial_slots_independently() -> None:
    operation = first_publication_operation(
        phase=sync.NewPRPhase.PUBLICATION_POSSIBLY_SENT,
        operation_phase=sync.FirstPublicationPhase.PUBLISHING,
        setup=sync.BookmarkSetup.KEEP,
    )
    prototype = operation.slots[0]
    slots = (
        dataclasses.replace(
            prototype, slot_id="ready", branch="ready", phase=sync.NewPRPhase.READY
        ),
        dataclasses.replace(prototype, slot_id="track", branch="track"),
        dataclasses.replace(prototype, slot_id="retry", branch="retry"),
    )
    operation = dataclasses.replace(operation, slots=slots)
    readbacks = (
        sync.PublicationSlotReadback(
            "ready",
            sync.CommitTarget(prototype.commit_id),
            sync.CommitTarget(prototype.commit_id),
            sync.TrackingState.TRACKED,
            prototype.commit_id,
        ),
        sync.PublicationSlotReadback(
            "track",
            sync.CommitTarget(prototype.commit_id),
            sync.CommitTarget(prototype.commit_id),
            sync.TrackingState.UNTRACKED,
            prototype.commit_id,
        ),
        sync.PublicationSlotReadback(
            "retry",
            sync.CommitTarget(prototype.commit_id),
            None,
            None,
            None,
        ),
    )

    assert sync.classify_publication_recovery(operation, readbacks) == (
        sync.PublicationRecoveryAction.READY,
        sync.PublicationRecoveryAction.TRACK,
        sync.PublicationRecoveryAction.PUSH,
    )

    disappeared = dataclasses.replace(readbacks[0], live_commit_id=None)
    blocked = sync.classify_publication_recovery(
        operation, (disappeared, *readbacks[1:])
    )
    assert isinstance(blocked, sync.Stopped)
    assert "drifted" in blocked.detail


def test_native_publication_command_freezes_remote_names_and_disables_signing(
    monkeypatch,
) -> None:
    operation = first_publication_operation(
        phase=sync.NewPRPhase.PUBLICATION_POSSIBLY_SENT,
        operation_phase=sync.FirstPublicationPhase.PUBLISHING,
        setup=sync.BookmarkSetup.KEEP,
    )
    calls: list[list[str]] = []
    monkeypatch.setattr(
        sync.subprocess,
        "run",
        lambda command, **_kwargs: (
            calls.append(command) or subprocess.CompletedProcess(command, 0, "", "")
        ),
    )

    sync.push_new_slot_refs("/repo", operation, operation.slots)

    assert calls == [
        [
            "jj",
            "--color=never",
            "--repository",
            "/repo",
            "--ignore-working-copy",
            "--config",
            "git.sign-on-push=false",
            "git",
            "push",
            "--remote",
            "origin",
            "--bookmark",
            operation.slots[0].branch,
        ]
    ]


@pytest.mark.parametrize(
    ("live", "remote", "tracking", "expected"),
    (
        (None, None, None, sync.PublicationRecoveryAction.PUSH),
        ("2" * 40, None, None, sync.PublicationRecoveryAction.PUSH),
        (
            "2" * 40,
            sync.CommitTarget("2" * 40),
            sync.TrackingState.UNTRACKED,
            sync.PublicationRecoveryAction.TRACK,
        ),
        (
            "2" * 40,
            sync.CommitTarget("2" * 40),
            sync.TrackingState.TRACKED,
            sync.PublicationRecoveryAction.READY,
        ),
    ),
)
def test_recorded_possible_publication_recovers_each_supported_readback(
    live: str | None,
    remote: sync.BookmarkTarget | None,
    tracking: sync.TrackingState | None,
    expected: sync.PublicationRecoveryAction,
) -> None:
    operation = first_publication_operation(
        phase=sync.NewPRPhase.PUBLICATION_POSSIBLY_SENT,
        operation_phase=sync.FirstPublicationPhase.PUBLISHING,
        setup=sync.BookmarkSetup.KEEP,
    )
    slot = operation.slots[0]
    readback = sync.PublicationSlotReadback(
        slot.slot_id,
        sync.CommitTarget(slot.commit_id),
        remote,
        tracking,
        live,
    )

    assert sync.classify_publication_recovery(operation, (readback,)) == (expected,)


def test_publication_never_creates_pr_before_all_ready(
    jj_repo: Path, monkeypatch
) -> None:
    operation = first_publication_operation(
        phase=sync.NewPRPhase.PUBLICATION_POSSIBLY_SENT,
        operation_phase=sync.FirstPublicationPhase.PUBLISHING,
        setup=sync.BookmarkSetup.KEEP,
    )
    slot = operation.slots[0]
    local = sync.LocalObservation(
        "/repo",
        "op",
        operation.effective_config,
        operation.workspace_targets,
        (sync.LocalBookmark(slot.branch, sync.CommitTarget(slot.commit_id)),),
        (),
        (),
        (),
        "/git",
    )
    readback = sync.PublicationSlotReadback(
        slot.slot_id, sync.CommitTarget(slot.commit_id), None, None, None
    )
    monkeypatch.setattr(
        sync, "observe_publication_slots", lambda *_args: (local, (readback,))
    )
    monkeypatch.setattr(
        sync,
        "push_new_slot_refs",
        lambda *_args: subprocess.CompletedProcess([], 1, "", "lost"),
    )
    server, client = github_for_publication(operation)
    sync.cas_write_first_publication(jj_repo, None, operation)

    stopped = sync.resume_first_publication(jj_repo, client)

    assert isinstance(stopped, sync.Stopped)
    assert stopped.stage == "publish"
    assert server.read_find_pull_requests(
        operation.repository, head_branches=(slot.branch,)
    ) == ()


def test_exact_untracked_publication_is_repaired_before_pr_creation(
    jj_repo: Path, monkeypatch
) -> None:
    operation = first_publication_operation(
        phase=sync.NewPRPhase.PUBLICATION_POSSIBLY_SENT,
        operation_phase=sync.FirstPublicationPhase.PUBLISHING,
        setup=sync.BookmarkSetup.KEEP,
    )
    slot = operation.slots[0]
    tracked = False
    local = sync.LocalObservation(
        "/repo",
        "op",
        operation.effective_config,
        operation.workspace_targets,
        (sync.LocalBookmark(slot.branch, sync.CommitTarget(slot.commit_id)),),
        (),
        (),
        (),
        "/git",
    )

    def observe(*_args):
        return local, (
            sync.PublicationSlotReadback(
                slot.slot_id,
                sync.CommitTarget(slot.commit_id),
                sync.CommitTarget(slot.commit_id),
                sync.TrackingState.TRACKED if tracked else sync.TrackingState.UNTRACKED,
                slot.commit_id,
            ),
        )

    def track(*_args):
        nonlocal tracked
        tracked = True
        return subprocess.CompletedProcess([], 0, "", "")

    server, client = github_for_publication(operation)
    monkeypatch.setattr(sync, "observe_publication_slots", observe)
    monkeypatch.setattr(sync, "track_publication_bookmark", track)
    sync.cas_write_first_publication(jj_repo, None, operation)

    with client.fail_before(sync.GitHubClient.create_pull_request):
        stopped = sync.resume_first_publication(jj_repo, client)

    assert tracked
    assert server.read_find_pull_requests(
        operation.repository, head_branches=(slot.branch,)
    ) == ()
    assert isinstance(stopped, sync.Stopped)
    assert stopped.stage == "create-pr"


@pytest.mark.parametrize("colocated", (True, False))
@pytest.mark.parametrize("setup", (sync.BookmarkSetup.CREATE, sync.BookmarkSetup.KEEP))
def test_native_first_publication_establishes_tracking_without_moving_workspace(
    tmp_path: Path,
    monkeypatch,
    colocated: bool,
    setup: sync.BookmarkSetup,
) -> None:
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    repo = tmp_path / "repo"
    run("git", "init", "--bare", "--initial-branch=main", remote)
    run("git", "clone", remote, seed)
    run("git", "-C", seed, "config", "user.name", "Test User")
    run("git", "-C", seed, "config", "user.email", "test@example.com")
    (seed / "base").write_text("base\n")
    run("git", "-C", seed, "add", "base")
    run("git", "-C", seed, "commit", "-m", "base")
    run("git", "-C", seed, "push", "origin", "main")

    run("jj", "git", "init", "--colocate" if colocated else "--no-colocate", repo)
    jj(repo, "config", "set", "--repo", "user.name", "Test User")
    jj(repo, "config", "set", "--repo", "user.email", "test@example.com")
    jj(
        repo,
        "config",
        "set",
        "--repo",
        'revset-aliases."immutable_heads()"',
        "trunk() | tags() | untracked_remote_bookmarks() | untracked_remote_tags()",
    )
    jj(repo, "git", "remote", "add", "origin", os.fspath(remote))
    jj(repo, "git", "fetch", "--remote", "origin")
    jj(repo, "new", "main@origin")
    (repo / "change").write_text("published\n")
    jj(repo, "describe", "-m", "Published title\n\nPublished body")
    branch = "arbitrary/publication-name"
    commit_id = jj(
        repo, "--ignore-working-copy", "log", "-r", "@", "--no-graph", "-T", "commit_id"
    )
    if setup is sync.BookmarkSetup.KEEP:
        jj(repo, "bookmark", "create", branch, "-r", commit_id)
    local = sync.observe_local(
        repo,
        revision=f"main@origin | {commit_id}",
        config_keys=("git.push",),
    )
    repository = sync.GitHubRepository(
        sync.GitHubRepositoryId("github.com", "R_repo"),
        "owner/repo",
        "https://github.com/owner/repo",
        "main",
    )
    operation = sync.prepare_standalone_first_publication(
        repo,
        local,
        sync.ToolStateRead(None, sync.EMPTY_STATE, None),
        repository,
        os.fspath(remote),
        "origin",
        "main",
        sync.PublicationAssignment(commit_id, branch),
        slot_id="slot-native",
    )
    assert isinstance(operation, sync.FirstPublication)
    assert operation.slots[0].bookmark_setup is setup

    (repo / "change").write_text("dirty but unsnapshotted\n")
    before = (
        jj(
            repo,
            "--ignore-working-copy",
            "log",
            "-r",
            "@",
            "--no-graph",
            "-T",
            "commit_id ++ '\t' ++ change_id",
        ),
        jj(
            repo,
            "--ignore-working-copy",
            "log",
            "-r",
            "all()",
            "--no-graph",
            "-T",
            "commit_id ++ '\t' ++ change_id ++ '\n'",
        ),
        jj(
            repo,
            "--ignore-working-copy",
            "workspace",
            "list",
            "-T",
            "name ++ '\t' ++ target.commit_id() ++ '\n'",
        ),
        (repo / "change").read_bytes(),
    )
    _server, client = github_for_publication(operation)

    sync.start_first_publication(operation, repo)
    with client.fail_before(sync.GitHubClient.create_pull_request):
        stopped = sync.resume_first_publication(repo, client)

    assert isinstance(stopped, sync.Stopped)
    assert stopped.stage == "create-pr"
    observed = sync.observe_local(
        repo, revision=commit_id, config_keys=("git.push",)
    )
    assert (
        sync.LocalBookmark(branch, sync.CommitTarget(commit_id))
        in observed.local_bookmarks
    )
    assert (
        sync.JjRemoteBookmark(
            "origin", branch, sync.CommitTarget(commit_id), sync.TrackingState.TRACKED
        )
        in observed.remote_bookmarks
    )
    assert (
        sync.observe_live_refs(
            os.fspath(remote), repository.identity, (f"refs/heads/{branch}",)
        )[0].commit_id
        == commit_id
    )
    after = (
        jj(
            repo,
            "--ignore-working-copy",
            "log",
            "-r",
            "@",
            "--no-graph",
            "-T",
            "commit_id ++ '\t' ++ change_id",
        ),
        jj(
            repo,
            "--ignore-working-copy",
            "log",
            "-r",
            "all()",
            "--no-graph",
            "-T",
            "commit_id ++ '\t' ++ change_id ++ '\n'",
        ),
        jj(
            repo,
            "--ignore-working-copy",
            "workspace",
            "list",
            "-T",
            "name ++ '\t' ++ target.commit_id() ++ '\n'",
        ),
        (repo / "change").read_bytes(),
    )
    assert after == before


def multi_publication_operation(
    *,
    slot_phases: tuple[sync.NewPRPhase, sync.NewPRPhase] = (
        sync.NewPRPhase.NOT_ATTEMPTED,
        sync.NewPRPhase.NOT_ATTEMPTED,
    ),
    operation_phase: sync.FirstPublicationPhase = sync.FirstPublicationPhase.PREPARING_BOOKMARKS,
) -> sync.FirstPublication:
    base = first_publication_operation()
    slots = tuple(
        dataclasses.replace(
            base.slots[0],
            slot_id=f"slot-{index}",
            branch=f"topic-{index}",
            commit_id=str(index) * 40,
            base_branch="main" if index == 1 else "topic-1",
            title=f"Title {index}",
            phase=phase,
        )
        for index, phase in zip((1, 2), slot_phases, strict=True)
    )
    return dataclasses.replace(
        base,
        workspace_targets=(("default", "2" * 40),),
        target=sync.FirstPublicationTarget.FRESH_STACK,
        slots=slots,
        stack_phase=sync.StackLinkPhase.NOT_ATTEMPTED,
        phase=operation_phase,
    )


def test_multi_publication_codec_and_final_state_preserve_complete_order() -> None:
    operation = multi_publication_operation(
        slot_phases=(sync.NewPRPhase.VERIFIED, sync.NewPRPhase.VERIFIED),
        operation_phase=sync.FirstPublicationPhase.LINKING_STACK,
    )
    identities = tuple(
        sync.PullRequestId(operation.repository, index) for index in (1, 2)
    )
    stack = sync.GitHubStackId(operation.repository, 31)
    operation = dataclasses.replace(
        operation,
        slots=tuple(
            dataclasses.replace(slot, pr_identity=identity)
            for slot, identity in zip(operation.slots, identities, strict=True)
        ),
        stack_phase=sync.StackLinkPhase.VERIFIED,
        resulting_stack=stack,
    )

    assert sync.parse_first_publication(sync.first_publication_to_json(operation)) == operation
    final = sync._build_first_publication_state(operation, sync.EMPTY_STATE)
    assert final.stacks[0].ordered_prs == identities
    assert tuple(item.verified_commit_id for item in final.last_published_heads) == (
        "1" * 40,
        "2" * 40,
    )


def test_partial_native_publication_recovers_before_any_pr_effect(
    jj_repo: Path, monkeypatch
) -> None:
    operation = multi_publication_operation(
        slot_phases=(
            sync.NewPRPhase.PUBLICATION_POSSIBLY_SENT,
            sync.NewPRPhase.PUBLICATION_POSSIBLY_SENT,
        ),
        operation_phase=sync.FirstPublicationPhase.PUBLISHING,
    )
    second_ready = False

    def observe(_workspace, current):
        local = sync.LocalObservation(
            "/repo", "op", current.effective_config, current.workspace_targets,
            tuple(
                sync.LocalBookmark(slot.branch, sync.CommitTarget(slot.commit_id))
                for slot in current.slots
            ),
            (), (), (), "/git",
        )
        return local, tuple(
            sync.PublicationSlotReadback(
                slot.slot_id,
                sync.CommitTarget(slot.commit_id),
                sync.CommitTarget(slot.commit_id)
                if index == 0 or second_ready else None,
                sync.TrackingState.TRACKED
                if index == 0 or second_ready else None,
                slot.commit_id if index == 0 or second_ready else None,
            )
            for index, slot in enumerate(current.slots)
        )

    pushed: list[tuple[str, ...]] = []

    def push(_workspace, _operation, slots):
        nonlocal second_ready
        pushed.append(tuple(slot.slot_id for slot in slots))
        second_ready = True
        return subprocess.CompletedProcess([], 1, "", "lost response")

    server, client = github_for_publication(operation)
    monkeypatch.setattr(sync, "observe_publication_slots", observe)
    monkeypatch.setattr(sync, "push_new_slot_refs", push)
    sync.cas_write_first_publication(jj_repo, None, operation)

    with client.fail_before(sync.GitHubClient.create_pull_request):
        stopped = sync.resume_first_publication(jj_repo, client)

    assert pushed == [(operation.slots[1].slot_id,)]
    assert server.read_find_pull_requests(
        operation.repository,
        head_branches=tuple(slot.branch for slot in operation.slots),
    ) == ()
    assert isinstance(stopped, sync.Stopped)
    assert stopped.stage == "create-pr"


@pytest.mark.parametrize(
    "target",
    (sync.FirstPublicationTarget.FRESH_STACK, sync.FirstPublicationTarget.APPEND),
)
def test_stack_mutation_uses_frozen_repository_and_only_new_append_suffix(
    target: sync.FirstPublicationTarget,
) -> None:
    operation = multi_publication_operation()
    server, client = github_for_publication(operation)
    if target is sync.FirstPublicationTarget.APPEND:
        repository = operation.repository
        member = sync.ExistingFirstPublicationPR(
            sync.PullRequestId(repository, 7),
            "existing",
            "7" * 40,
            "main",
            "0" * 40,
            True,
            "Existing",
            "",
        )
        operation = dataclasses.replace(
            operation,
            target=target,
            existing_members=(member,),
            existing_stack=sync.GitHubStackId(repository, 17),
        )
    operation = dataclasses.replace(
        operation,
        slots=tuple(
            dataclasses.replace(
                slot, pr_identity=sync.PullRequestId(operation.repository, index)
            )
            for index, slot in enumerate(operation.slots, start=1)
        ),
    )
    for slot in operation.slots:
        assert slot.pr_identity is not None
        server.seed_pull_request(
            sync.GitHubPullRequest(
                slot.pr_identity,
                f"PR_{slot.pr_identity.number}",
                sync.PullRequestState.OPEN,
                True,
                operation.repository,
                slot.branch,
                slot.commit_id,
                slot.base_branch,
                "0" * 40 if slot.base_branch == "main" else "1" * 40,
                False,
                False,
                slot.title,
                slot.body,
                None,
            )
        )
    if target is sync.FirstPublicationTarget.APPEND:
        member = operation.existing_members[0]
        server.seed_pull_request(
            sync.GitHubPullRequest(
                member.identity,
                "PR_existing",
                sync.PullRequestState.OPEN,
                member.draft,
                operation.repository,
                member.head_branch,
                member.head_commit_id,
                member.base_branch,
                member.base_commit_id,
                False,
                False,
                member.title,
                member.body,
                None,
            )
        )
        assert operation.existing_stack is not None
        server.seed_stack(
            sync.GitHubStack(
                operation.existing_stack,
                "STACK_existing",
                operation.base_branch,
                operation.existing_prs,
            )
        )

    sync.run_first_publication_stack_link(client, operation)

    new_members = tuple(
        slot.pr_identity for slot in operation.slots
    )
    pull_requests = server.read_pull_requests(new_members)
    summaries = tuple(pr.stack for pr in pull_requests)
    assert summaries[0] is not None
    assert all(summary == summaries[0] for summary in summaries)
    repository = server.read_repository(operation.repository)
    stack = server.read_stack(repository, summaries[0].identity)
    assert stack is not None
    assert stack.pull_requests == (
        operation.existing_prs + new_members
        if target is sync.FirstPublicationTarget.APPEND
        else new_members
    )


def multi_head_plan(*, merged_prefix: int = 0) -> tuple[sync.Snapshot, sync.Apply]:
    observed, selected = stacked_snapshot(
        count=merged_prefix + 2, merged_prefix=merged_prefix
    )
    base = "b" * 40
    comparison_base = base if merged_prefix == 0 else str(merged_prefix) * 40
    old_heads = (comparison_base, str(merged_prefix + 1) * 40)
    prs = tuple(
        pr
        if index < merged_prefix
        else dataclasses.replace(
            pr,
            head_oid=old_heads[index - merged_prefix],
            base_oid=(
                comparison_base
                if index == merged_prefix
                else old_heads[index - merged_prefix - 1]
            ),
        )
        for index, pr in enumerate(observed.pull_requests)
    )
    old_by_ref = {
        f"refs/heads/topic-{index + merged_prefix + 1}": old
        for index, old in enumerate(old_heads)
    }
    refs = tuple(
        dataclasses.replace(ref, commit_id=old_by_ref[ref.ref.full_name])
        if ref.ref.full_name in old_by_ref
        else ref
        for ref in observed.live_refs
    )
    observed = dataclasses.replace(observed, pull_requests=prs, live_refs=refs)
    plan = sync.plan_sync(observed, sync.derive_desired(observed, selected))
    assert isinstance(plan, sync.Apply)
    assert len(plan.head_updates) == 2
    return observed, plan


def test_apply_verifies_multi_pr_noop_without_side_effects(monkeypatch) -> None:
    observed, selected = stacked_snapshot(count=2)
    plan = sync.plan_sync(observed, sync.derive_desired(observed, selected))
    assert isinstance(plan, sync.NoOp)

    monkeypatch.setattr(sync, "repository_lock", lambda *_args: nullcontext())
    monkeypatch.setattr(
        sync,
        "reobserve_for_apply",
        lambda *_args: observed,
    )
    monkeypatch.setattr(
        sync,
        "push_exact_head_updates",
        lambda *_args: pytest.fail("NoOp attempted a push"),
    )
    monkeypatch.setattr(
        sync,
        "record_verified_state",
        lambda *_args: pytest.fail("NoOp attempted a state write"),
    )

    _server, client = github_for(observed)
    result = sync.apply(plan, "/unused", client)

    assert result == sync.Verified(False, False, False)


@pytest.mark.parametrize("merged_prefix", (0, 1))
def test_apply_publishes_multi_pr_heads_in_one_atomic_effect(
    merged_prefix: int, monkeypatch
) -> None:
    observed, plan = multi_head_plan(merged_prefix=merged_prefix)
    observations = iter((observed, final_snapshot(observed, plan)))
    pushes: list[tuple[sync.PlannedHeadUpdate, ...]] = []
    recorded: list[tuple[sync.LastPublishedHead, ...]] = []
    monkeypatch.setattr(sync, "repository_lock", lambda *_args: nullcontext())
    monkeypatch.setattr(sync, "reobserve_for_apply", lambda *_args: next(observations))
    monkeypatch.setattr(
        sync,
        "push_exact_head_updates",
        lambda _workspace, updates, _url: (
            pushes.append(tuple(updates)) or subprocess.CompletedProcess([], 0, "", "")
        ),
    )
    monkeypatch.setattr(
        sync,
        "record_verified_state",
        lambda _workspace, _oid, _state, tracking, publications: (
            assert_no_tracking_and_record(tracking, publications, recorded)
        ),
    )

    _server, client = github_for(observed)
    result = sync.apply(plan, "/unused", client)

    assert result == sync.Verified(True, False, True)
    assert pushes == [plan.head_updates]
    pr_by_ref = {f"refs/heads/{pr.head_branch}": pr for pr in plan.dependencies.prs}
    assert recorded == [
        tuple(
            sync.LastPublishedHead(
                pr_by_ref[update.ref.full_name].identity,
                update.ref,
                update.new_commit_id,
            )
            for update in plan.head_updates
        )
    ]


def assert_no_tracking_and_record(
    tracking: sync.TrackedStack | None,
    publications,
    recorded: list[tuple[sync.LastPublishedHead, ...]],
) -> None:
    assert tracking is None
    recorded.append(tuple(publications))


def test_explicit_selection_plans_and_persists_verified_tracking(monkeypatch) -> None:
    observed, selected = stacked_snapshot(count=2)
    observed = dataclasses.replace(
        observed,
        tool_state=dataclasses.replace(observed.tool_state, state=sync.EMPTY_STATE),
    )
    plan = sync.plan_sync(observed, sync.derive_desired(observed, selected))
    assert isinstance(plan, sync.Apply)
    assert plan.head_updates == ()
    assert plan.metadata_updates == ()
    assert plan.tracking_update == sync.TrackedStack(
        observed.repository,
        "main",
        tuple(pr.identity for pr in observed.pull_requests),
    )
    recorded: list[sync.TrackedStack | None] = []
    monkeypatch.setattr(sync, "repository_lock", lambda *_args: nullcontext())
    monkeypatch.setattr(sync, "reobserve_for_apply", lambda *_args: observed)
    monkeypatch.setattr(
        sync,
        "record_verified_state",
        lambda _workspace, _oid, _state, tracking, _publications: recorded.append(
            tracking
        ),
    )

    _server, client = github_for(observed)
    result = sync.apply(plan, "/unused", client)

    assert result == sync.Verified(False, False, False, True)
    assert recorded == [plan.tracking_update]


def test_explicit_selection_blocks_overlapping_tracked_topology() -> None:
    observed, selected = stacked_snapshot(count=2)
    conflicting = sync.TrackedStack(
        observed.repository,
        "release",
        (observed.pull_requests[0].identity,),
    )
    observed = dataclasses.replace(
        observed,
        tool_state=dataclasses.replace(
            observed.tool_state,
            state=sync.TrackedState((conflicting,), ()),
        ),
    )

    plan = sync.plan_sync(observed, sync.derive_desired(observed, selected))

    assert isinstance(plan, sync.Blocked)
    assert plan.reasons[0].code == "tracked-topology-changed"


def test_apply_updates_metadata_for_every_planned_pr(monkeypatch) -> None:
    observed, selected = stacked_snapshot(count=2)
    stale = dataclasses.replace(
        observed,
        pull_requests=tuple(
            dataclasses.replace(pr, title=f"Stale {pr.number}")
            for pr in observed.pull_requests
        ),
    )
    plan = sync.plan_sync(stale, sync.derive_desired(stale, selected))
    assert isinstance(plan, sync.Apply)
    assert len(plan.metadata_updates) == 2
    observations = iter((stale, final_snapshot(stale, plan)))
    server, client = github_for(stale)
    monkeypatch.setattr(sync, "repository_lock", lambda *_args: nullcontext())
    monkeypatch.setattr(sync, "reobserve_for_apply", lambda *_args: next(observations))
    monkeypatch.setattr(
        sync,
        "record_verified_state",
        lambda *_args: pytest.fail("metadata-only apply attempted a state write"),
    )

    result = sync.apply(plan, "/unused", client)

    assert result == sync.Verified(False, True, False)
    assert tuple(
        (pr.title, pr.body)
        for pr in server.read_pull_requests(
            tuple(pr.identity for pr in stale.pull_requests)
        )
    ) == tuple((wanted.title, wanted.body) for wanted in plan.desired.active)


def test_apply_revalidation_ignores_jj_operation_but_detects_relevant_drift() -> None:
    observed, plan = standalone_plan(desired="2" * 40, live="1" * 40, parent="1" * 40)
    unrelated = dataclasses.replace(
        observed,
        local=dataclasses.replace(observed.local, operation_id="new-jj-operation"),
    )
    assert sync.validate_frozen_dependencies(plan, unrelated) is None

    changed = (
        dataclasses.replace(observed, push_url="ssh://elsewhere/repo.git"),
        dataclasses.replace(
            observed,
            tool_state=dataclasses.replace(
                observed.tool_state, operation_blob_oid="f" * 40
            ),
        ),
        dataclasses.replace(
            observed,
            pull_requests=(
                dataclasses.replace(observed.pull_requests[0], base_branch="release"),
            ),
        ),
    )
    assert [
        sync.validate_frozen_dependencies(plan, item).reasons[0].code  # type: ignore[union-attr]
        for item in changed
    ] == ["push-url-changed", "operation-changed", "pr-changed"]


def test_frozen_metadata_effect_allows_race_but_noop_does_not() -> None:
    observed, no_op = standalone_plan(desired="1" * 40, live="1" * 40, parent="0" * 40)
    raced = dataclasses.replace(
        observed,
        pull_requests=(
            dataclasses.replace(observed.pull_requests[0], title="Concurrent title"),
        ),
    )
    blocked = sync.validate_frozen_dependencies(no_op, raced)
    assert isinstance(blocked, sync.Blocked)
    assert blocked.reasons[0].code == "metadata-changed"

    stale = dataclasses.replace(
        observed,
        pull_requests=(dataclasses.replace(observed.pull_requests[0], title="Stale"),),
    )
    desired = sync.derive_desired(
        stale, selection(stale, stale.pull_requests[0].identity)
    )
    metadata_plan = sync.plan_sync(stale, desired)
    assert isinstance(metadata_plan, sync.Apply)
    raced_again = dataclasses.replace(
        stale,
        pull_requests=(
            dataclasses.replace(stale.pull_requests[0], title="Concurrent title"),
        ),
    )
    assert sync.validate_frozen_dependencies(metadata_plan, raced_again) is None


def test_apply_publishes_verifies_and_records_authority(monkeypatch) -> None:
    observed, plan = standalone_plan(desired="2" * 40, live="1" * 40, parent="1" * 40)
    assert isinstance(plan, sync.Apply)
    observations = iter((observed, final_snapshot(observed, plan)))
    recorded: list[sync.LastPublishedHead] = []
    monkeypatch.setattr(sync, "repository_lock", lambda *_args: nullcontext())
    monkeypatch.setattr(sync, "reobserve_for_apply", lambda *_args: next(observations))
    monkeypatch.setattr(
        sync,
        "push_exact_head_updates",
        lambda *_args: subprocess.CompletedProcess([], 0, "", ""),
    )
    monkeypatch.setattr(
        sync,
        "record_verified_state",
        lambda _workspace, _oid, _state, _tracking, publications: recorded.extend(
            publications
        ),
    )

    _server, client = github_for(observed)
    result = sync.apply(plan, "/unused", client)

    assert result == sync.Verified(True, False, True)
    assert recorded == [
        sync.LastPublishedHead(
            plan.desired.active[0].pr_identity,
            plan.head_updates[0].ref,
            "2" * 40,
        )
    ]


def test_apply_noop_verifies_without_mutation_or_receipt(monkeypatch) -> None:
    observed, plan = standalone_plan(desired="1" * 40, live="1" * 40, parent="0" * 40)
    assert isinstance(plan, sync.NoOp)
    monkeypatch.setattr(sync, "repository_lock", lambda *_args: nullcontext())
    monkeypatch.setattr(sync, "reobserve_for_apply", lambda *_args: observed)
    monkeypatch.setattr(
        sync,
        "push_exact_head_updates",
        lambda *_args: pytest.fail("NoOp attempted a push"),
    )
    monkeypatch.setattr(
        sync,
        "record_verified_state",
        lambda *_args: pytest.fail("NoOp manufactured replacement authority"),
    )

    _server, client = github_for(observed)
    assert sync.apply(plan, "/unused", client) == sync.Verified(
        False, False, False
    )


def test_apply_metadata_only_overwrites_frozen_race_without_receipt(
    monkeypatch,
) -> None:
    observed, pr = snapshot(desired="1" * 40, live="1" * 40, parent="0" * 40)
    stale = dataclasses.replace(
        observed,
        pull_requests=(dataclasses.replace(observed.pull_requests[0], title="Stale"),),
    )
    plan = sync.plan_sync(stale, sync.derive_desired(stale, selection(stale, pr)))
    assert isinstance(plan, sync.Apply)
    raced = dataclasses.replace(
        stale,
        pull_requests=(dataclasses.replace(stale.pull_requests[0], title="Raced"),),
    )
    observations = iter((raced, final_snapshot(stale, plan)))
    server, client = github_for(stale)
    monkeypatch.setattr(sync, "repository_lock", lambda *_args: nullcontext())
    monkeypatch.setattr(sync, "reobserve_for_apply", lambda *_args: next(observations))
    monkeypatch.setattr(
        sync,
        "record_verified_state",
        lambda *_args: pytest.fail("metadata-only apply created a receipt"),
    )
    original_update = client.update_pull_request
    attempted = False

    def update_once(*args, **kwargs):
        nonlocal attempted
        if attempted:
            pytest.fail("replayed metadata update")
        attempted = True
        return original_update(*args, **kwargs)

    monkeypatch.setattr(client, "update_pull_request", update_once)

    with client.lose_response(sync.GitHubClient.update_pull_request, pr=pr):
        result = sync.apply(plan, "/unused", client)

    assert result == sync.Verified(False, True, False)
    updated = server.read_pull_requests((pr,))[0]
    assert (updated.title, updated.body) == ("Desired title", "Desired body")


def test_apply_does_not_update_metadata_after_published_head_is_superseded(
    monkeypatch,
) -> None:
    observed, pr = snapshot(desired="2" * 40, live="1" * 40, parent="1" * 40)
    stale = dataclasses.replace(
        observed,
        pull_requests=(dataclasses.replace(observed.pull_requests[0], title="Stale"),),
    )
    plan = sync.plan_sync(stale, sync.derive_desired(stale, selection(stale, pr)))
    assert isinstance(plan, sync.Apply)
    superseded = final_snapshot(stale, plan)
    superseded = dataclasses.replace(
        superseded,
        pull_requests=(
            dataclasses.replace(
                superseded.pull_requests[0], head_oid="3" * 40
            ),
        ),
        live_refs=tuple(
            dataclasses.replace(ref, commit_id="3" * 40)
            if ref.ref.full_name == "refs/heads/topic"
            else ref
            for ref in superseded.live_refs
        ),
    )
    observations = iter((stale, superseded))
    monkeypatch.setattr(sync, "repository_lock", lambda *_args: nullcontext())
    monkeypatch.setattr(sync, "reobserve_for_apply", lambda *_args: next(observations))
    monkeypatch.setattr(
        sync,
        "push_exact_head_updates",
        lambda *_args: subprocess.CompletedProcess([], 0, "", ""),
    )
    server, client = github_for(stale)

    result = sync.apply(plan, "/unused", client)

    assert isinstance(result, sync.Stopped)
    assert result.stage == "pre-metadata-verify"
    unchanged = server.read_pull_requests((pr,))[0]
    assert (unchanged.title, unchanged.body) == (
        stale.pull_requests[0].title,
        stale.pull_requests[0].body,
    )


def test_apply_lock_contention_raises_lock_busy(jj_repo: Path, monkeypatch) -> None:
    _observed, plan = standalone_plan(desired="1" * 40, live="1" * 40, parent="0" * 40)
    monkeypatch.setattr(
        sync,
        "reobserve_for_apply",
        lambda *_args: pytest.fail("contended apply performed observation"),
    )

    with sync.repository_lock(jj_repo):
        with pytest.raises(sync.LockBusy):
            server = FakeGitHubServer()
            sync.apply(plan, jj_repo, FakeGitHubClient(server))


@pytest.mark.parametrize(
    ("observed_head", "expected_stage", "published"),
    (
        ("2" * 40, None, True),
        ("1" * 40, "push", False),
        ("9" * 40, "push", False),
    ),
)
def test_apply_classifies_failed_push_from_authoritative_head(
    observed_head: str, expected_stage: str | None, published: bool, monkeypatch
) -> None:
    observed, plan = standalone_plan(desired="2" * 40, live="1" * 40, parent="1" * 40)
    assert isinstance(plan, sync.Apply)
    final = final_snapshot(observed, plan)
    observations = iter((observed, final))
    monkeypatch.setattr(sync, "repository_lock", lambda *_args: nullcontext())
    monkeypatch.setattr(sync, "reobserve_for_apply", lambda *_args: next(observations))
    monkeypatch.setattr(
        sync,
        "push_exact_head_updates",
        lambda *_args: subprocess.CompletedProcess([], 1, "", "lost response"),
    )
    monkeypatch.setattr(
        sync,
        "observe_live_refs",
        lambda *_args, **_kwargs: (
            sync.LiveRemoteRef(plan.head_updates[0].ref, observed_head),
        ),
    )
    monkeypatch.setattr(sync, "record_verified_state", lambda *_args: "oid")

    _server, client = github_for(observed)
    result = sync.apply(plan, "/unused", client)

    if expected_stage is None:
        assert result == sync.Verified(published, False, True)
    else:
        assert isinstance(result, sync.Stopped)
        assert result.stage == expected_stage


def test_verified_publication_reports_receipt_failure_honestly(monkeypatch) -> None:
    observed, plan = standalone_plan(desired="2" * 40, live="1" * 40, parent="1" * 40)
    assert isinstance(plan, sync.Apply)
    observations = iter((observed, final_snapshot(observed, plan)))
    monkeypatch.setattr(sync, "repository_lock", lambda *_args: nullcontext())
    monkeypatch.setattr(sync, "reobserve_for_apply", lambda *_args: next(observations))
    monkeypatch.setattr(
        sync,
        "push_exact_head_updates",
        lambda *_args: subprocess.CompletedProcess([], 0, "", ""),
    )
    monkeypatch.setattr(
        sync,
        "record_verified_state",
        lambda *_args: (_ for _ in ()).throw(sync.ConcurrentUpdate("stale state")),
    )

    _server, client = github_for(observed)
    result = sync.apply(plan, "/unused", client)

    assert isinstance(result, sync.Stopped)
    assert result.stage == "receipt"
    assert result.head_published
    assert result.final_state_verified
    assert not result.authority_persisted


def test_exact_head_push_uses_original_lease_and_has_no_fallback(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source"
    remote = tmp_path / "remote.git"
    run("git", "init", source)
    run("git", "init", "--bare", remote)
    run("git", "-C", source, "config", "user.name", "Test")
    run("git", "-C", source, "config", "user.email", "test@example.com")
    (source / "file").write_text("old\n")
    run("git", "-C", source, "add", "file")
    run("git", "-C", source, "commit", "-m", "old")
    old = run("git", "-C", source, "rev-parse", "HEAD")
    run("git", "-C", source, "push", remote, "HEAD:refs/heads/topic")
    (source / "file").write_text("new\n")
    run("git", "-C", source, "commit", "-am", "new")
    new = run("git", "-C", source, "rev-parse", "HEAD")
    repository = sync.GitHubRepositoryId("github.com", "R_repo")
    update = sync.PlannedHeadUpdate(
        sync.RemoteBranchRef(repository, "refs/heads/topic"),
        old,
        new,
        sync.FastForward(),
    )
    monkeypatch.setattr(sync, "git_common_dir", lambda *_args: source / ".git")

    success = sync.push_exact_head_updates(source, (update,), os.fspath(remote))
    (source / "file").write_text("foreign\n")
    run("git", "-C", source, "commit", "-am", "foreign")
    run("git", "-C", source, "push", remote, "HEAD:refs/heads/topic")
    foreign = run("git", "-C", source, "rev-parse", "HEAD")
    stale = sync.push_exact_head_updates(source, (update,), os.fspath(remote))

    assert success.returncode == 0
    assert stale.returncode != 0
    assert run("git", f"--git-dir={remote}", "rev-parse", "refs/heads/topic") == foreign


def test_atomic_multi_head_push_changes_nothing_when_one_lease_is_stale(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source"
    remote = tmp_path / "remote.git"
    run("git", "init", source)
    run("git", "init", "--bare", remote)
    run("git", "-C", source, "config", "user.name", "Test")
    run("git", "-C", source, "config", "user.email", "test@example.com")
    (source / "file").write_text("old\n")
    run("git", "-C", source, "add", "file")
    run("git", "-C", source, "commit", "-m", "old")
    old = run("git", "-C", source, "rev-parse", "HEAD")
    run(
        "git",
        "-C",
        source,
        "push",
        remote,
        "HEAD:refs/heads/one",
        "HEAD:refs/heads/two",
    )
    (source / "file").write_text("one\n")
    run("git", "-C", source, "commit", "-am", "one")
    one = run("git", "-C", source, "rev-parse", "HEAD")
    (source / "file").write_text("two\n")
    run("git", "-C", source, "commit", "-am", "two")
    two = run("git", "-C", source, "rev-parse", "HEAD")
    (source / "file").write_text("foreign\n")
    run("git", "-C", source, "commit", "-am", "foreign")
    foreign = run("git", "-C", source, "rev-parse", "HEAD")
    run("git", "-C", source, "push", remote, f"{foreign}:refs/heads/two")
    repository = sync.GitHubRepositoryId("github.com", "R_repo")
    updates = (
        sync.PlannedHeadUpdate(
            sync.RemoteBranchRef(repository, "refs/heads/one"),
            old,
            one,
            sync.FastForward(),
        ),
        sync.PlannedHeadUpdate(
            sync.RemoteBranchRef(repository, "refs/heads/two"),
            old,
            two,
            sync.FastForward(),
        ),
    )
    monkeypatch.setattr(sync, "git_common_dir", lambda *_args: source / ".git")

    result = sync.push_exact_head_updates(source, updates, os.fspath(remote))

    assert result.returncode != 0
    assert run("git", f"--git-dir={remote}", "rev-parse", "refs/heads/one") == old
    assert run("git", f"--git-dir={remote}", "rev-parse", "refs/heads/two") == foreign


def test_record_verified_state_combines_tracking_and_replaces_matching_authority(
    jj_repo: Path,
) -> None:
    repository = sync.GitHubRepositoryId("github.com", "R_repo")
    pr = sync.PullRequestId(repository, 7)
    other = sync.PullRequestId(repository, 8)
    ref = sync.RemoteBranchRef(repository, "refs/heads/topic")
    other_ref = sync.RemoteBranchRef(repository, "refs/heads/other")
    initial = sync.TrackedState(
        (),
        (
            sync.LastPublishedHead(pr, ref, "1" * 40),
            sync.LastPublishedHead(other, other_ref, "8" * 40),
        ),
    )
    oid = sync.cas_write_state(jj_repo, None, initial)
    publication = sync.LastPublishedHead(pr, ref, "2" * 40)
    tracking = sync.TrackedStack(repository, "main", (pr,))

    new_oid = sync.record_verified_state(
        jj_repo, oid, initial, tracking, (publication,)
    )

    assert sync.read_state(jj_repo) == (
        new_oid,
        sync.TrackedState(
            (tracking,),
            (sync.LastPublishedHead(other, other_ref, "8" * 40), publication),
        ),
    )


def publication_assignment_input(
    *,
    bookmarks: tuple[sync.LocalBookmark, ...] = (),
    explicit: tuple[sync.PublicationAssignment, ...] | None = None,
    templates: tuple[sync.PublicationAssignment, ...] = (),
    names: tuple[str, ...] = ("generated-one", "generated-two"),
    destinations: tuple[sync.LiveRemoteRef, ...] | None = None,
    history: tuple[sync.GitHubPullRequest, ...] = (),
) -> sync.PublicationAssignmentInput:
    repository = sync.GitHubRepositoryId("github.com", "R_repo")
    commits = ("1" * 40, "2" * 40)
    local = sync.LocalObservation(
        "/repo",
        "op",
        (),
        (("default", commits[-1]),),
        bookmarks,
        (),
        (),
        tuple(
            sync.ObservedCommit(commit, (), f"change-{index}", "title", False, False)
            for index, commit in enumerate(commits)
        ),
        "/git",
    )
    if destinations is None:
        destinations = tuple(
            sync.LiveRemoteRef(
                sync.RemoteBranchRef(repository, f"refs/heads/{name}"), None
            )
            for name in names
        )
    return sync.PublicationAssignmentInput(
        repository,
        commits,
        local,
        explicit,
        templates,
        ("main", "dev", "jj-stack/managed"),
        destinations,
        history,
    )


def historical_pr(
    *, head: str, base: str, fork: bool = False
) -> sync.GitHubPullRequest:
    repository = sync.GitHubRepositoryId("github.com", "R_repo")
    head_repository = (
        sync.GitHubRepositoryId("github.com", "R_fork") if fork else repository
    )
    return sync.GitHubPullRequest(
        sync.PullRequestId(repository, 7),
        f"PR_{head}_{base}_{fork}",
        sync.PullRequestState.CLOSED,
        False,
        head_repository,
        head,
        "9" * 40,
        base,
        "0" * 40,
        False,
        False,
        "old",
        "",
        None,
    )


def test_publication_assignment_explicit_mapping_wins_and_must_be_complete() -> None:
    commits = ("1" * 40, "2" * 40)
    explicit = tuple(
        sync.PublicationAssignment(commit, name)
        for commit, name in zip(
            commits, ("generated-one", "generated-two"), strict=True
        )
    )
    observed = publication_assignment_input(
        bookmarks=(
            sync.LocalBookmark("alias-a", sync.CommitTarget(commits[0])),
            sync.LocalBookmark("alias-b", sync.CommitTarget(commits[0])),
        ),
        explicit=explicit,
    )

    assert sync.resolve_publication_assignments(observed) == explicit

    incomplete = dataclasses.replace(observed, explicit=explicit[:1])
    blocked = sync.resolve_publication_assignments(incomplete)
    assert isinstance(blocked, sync.Blocked)
    assert blocked.reasons[0].code == "incomplete-explicit-assignment"


def test_publication_assignment_mixes_unique_bookmark_and_template_per_revision() -> (
    None
):
    commits = ("1" * 40, "2" * 40)
    observed = publication_assignment_input(
        bookmarks=(sync.LocalBookmark("intentional", sync.CommitTarget(commits[0])),),
        templates=(sync.PublicationAssignment(commits[1], "generated-two"),),
        names=("intentional", "generated-two"),
    )

    assert sync.resolve_publication_assignments(observed) == (
        sync.PublicationAssignment(commits[0], "intentional"),
        sync.PublicationAssignment(commits[1], "generated-two"),
    )


def test_publication_assignment_blocks_alias_ambiguity_without_fallback() -> None:
    commit = "1" * 40
    observed = publication_assignment_input(
        bookmarks=(
            sync.LocalBookmark("one", sync.CommitTarget(commit)),
            sync.LocalBookmark("two", sync.CommitTarget(commit)),
        ),
        templates=(sync.PublicationAssignment(commit, "generated-one"),),
    )

    blocked = sync.resolve_publication_assignments(observed)

    assert isinstance(blocked, sync.Blocked)
    assert blocked.reasons[0].code == "ambiguous-local-bookmark"


@pytest.mark.parametrize(
    ("assignment", "bookmarks", "code"),
    (
        ("main", (), "protected-publication-branch"),
        ("jj-stack/temporary-bases/manual/1", (), "protected-publication-branch"),
        ("bad..name", (), "invalid-publication-branch"),
        (
            "generated-one",
            (sync.LocalBookmark("generated-one", sync.AbsentBookmarkTarget()),),
            "local-publication-collision",
        ),
        (
            "generated-one",
            (sync.LocalBookmark("generated-one", sync.CommitTarget("2" * 40)),),
            "local-publication-collision",
        ),
    ),
)
def test_publication_assignment_validates_names_and_local_collisions(
    assignment: str,
    bookmarks: tuple[sync.LocalBookmark, ...],
    code: str,
) -> None:
    commits = ("1" * 40, "2" * 40)
    explicit = (
        sync.PublicationAssignment(commits[0], assignment),
        sync.PublicationAssignment(commits[1], "generated-two"),
    )
    observed = publication_assignment_input(
        bookmarks=bookmarks,
        explicit=explicit,
        names=(assignment, "generated-two"),
    )

    blocked = sync.resolve_publication_assignments(observed)

    assert isinstance(blocked, sync.Blocked)
    assert blocked.reasons[0].code == code


def test_publication_assignment_requires_absent_authoritative_destination() -> None:
    repository = sync.GitHubRepositoryId("github.com", "R_repo")
    occupied = sync.LiveRemoteRef(
        sync.RemoteBranchRef(repository, "refs/heads/generated-one"), "9" * 40
    )
    missing = publication_assignment_input(destinations=(occupied,))
    explicit = tuple(
        sync.PublicationAssignment(commit, name)
        for commit, name in zip(
            missing.ordered_commit_ids,
            ("generated-one", "generated-two"),
            strict=True,
        )
    )
    missing = dataclasses.replace(missing, explicit=explicit)

    blocked = sync.resolve_publication_assignments(missing)

    assert isinstance(blocked, sync.Blocked)
    assert blocked.reasons[0].code == "remote-publication-collision"


def test_publication_assignment_blocks_historical_base_and_same_repo_head_use() -> None:
    commits = ("1" * 40, "2" * 40)
    explicit = (
        sync.PublicationAssignment(commits[0], "generated-one"),
        sync.PublicationAssignment(commits[1], "generated-two"),
    )
    for old in (
        historical_pr(head="old", base="generated-one"),
        historical_pr(head="generated-one", base="main"),
    ):
        observed = publication_assignment_input(explicit=explicit, history=(old,))
        blocked = sync.resolve_publication_assignments(observed)
        assert isinstance(blocked, sync.Blocked)
        assert blocked.reasons[0].code == "pull-request-branch-collision"

    fork = historical_pr(head="generated-one", base="main", fork=True)
    observed = publication_assignment_input(explicit=explicit, history=(fork,))
    assert sync.resolve_publication_assignments(observed) == explicit


def test_historical_collision_observation_is_one_call_bounded_by_names(
) -> None:
    repository = sync.GitHubRepository(
        sync.GitHubRepositoryId("github.com", "R_repo"),
        "owner/repo",
        "https://github.com/owner/repo",
        "main",
    )
    branches = ("new-one", "new-two")
    server = FakeGitHubServer()
    server.seed_repository(repository)
    head = dataclasses.replace(
        historical_pr(head="new-one", base="main"),
        identity=sync.PullRequestId(repository.identity, 7),
    )
    base = dataclasses.replace(
        historical_pr(head="old", base="new-two"),
        identity=sync.PullRequestId(repository.identity, 8),
    )
    server.seed_pull_request(head)
    server.seed_pull_request(base)
    client = FakeGitHubClient(server)

    observed = sync.observe_historical_pull_request_collisions(
        client, repository, branches
    )

    assert tuple(item.number for item in observed) == (7, 8)


def test_selected_boundary_bookmark_names_uses_exact_and_change_aliases() -> None:
    observed = publication_assignment_input().local
    selected = observed.commits
    rewritten = dataclasses.replace(
        selected[1], commit_id="3" * 40, parent_commit_ids=(selected[1].commit_id,)
    )
    local = dataclasses.replace(
        observed,
        local_bookmarks=(
            sync.LocalBookmark("exact", sync.CommitTarget(selected[0].commit_id)),
            sync.LocalBookmark("change-alias", sync.CommitTarget(rewritten.commit_id)),
            sync.LocalBookmark("unselected", sync.CommitTarget("4" * 40)),
            sync.LocalBookmark("absent", sync.AbsentBookmarkTarget()),
        ),
        commits=(*selected, rewritten),
    )

    assert sync.selected_boundary_bookmark_names(local, selected) == (
        "exact",
        "change-alias",
    )


def test_standalone_membership_resolution_orders_source_chain_and_new_boundary() -> None:
    repository = sync.GitHubRepositoryId("github.com", "R_repo")
    first_id, new_id, last_id, rewritten_id = (character * 40 for character in "1234")
    commits = (
        sync.ObservedCommit(first_id, (), "change-1", "One", False, False),
        sync.ObservedCommit(new_id, (first_id,), "change-new", "New", False, False),
        sync.ObservedCommit(last_id, (new_id,), "change-2", "Two", False, False),
    )
    rewritten = dataclasses.replace(
        commits[-1], commit_id=rewritten_id, parent_commit_ids=(last_id,)
    )
    local = dataclasses.replace(
        publication_assignment_input().local,
        local_bookmarks=(
            sync.LocalBookmark("topic-1", sync.CommitTarget(first_id)),
            sync.LocalBookmark("topic-2", sync.CommitTarget(rewritten_id)),
        ),
        commits=(*commits, rewritten),
    )
    first_pr = sync.GitHubPullRequest(
        sync.PullRequestId(repository, 1), "PR_1", sync.PullRequestState.OPEN,
        False, repository, "topic-1", first_id, "dev", "0" * 40,
        False, False, "One", "", None,
    )
    last_pr = sync.GitHubPullRequest(
        sync.PullRequestId(repository, 2), "PR_2", sync.PullRequestState.OPEN,
        False, repository, "topic-2", rewritten_id, "topic-1", first_id,
        False, False, "Two", "", None,
    )
    first = first_pr
    last = last_pr

    result = sync.resolve_standalone_membership_boundaries(
        local, repository, first_pr.identity, (last, first), commits
    )

    assert result == (
        (
            sync.ExistingMembershipBoundary(first_pr.identity, first_id),
            sync.NewMembershipBoundary(new_id),
            sync.ExistingMembershipBoundary(last_pr.identity, last_id),
        ),
        (first, last),
        "dev",
    )


def test_open_pr_candidate_observation_is_one_call_bounded_by_selected_aliases(
) -> None:
    repository = sync.GitHubRepository(
        sync.GitHubRepositoryId("github.com", "R_repo"),
        "owner/repo",
        "https://github.com/owner/repo",
        "main",
    )
    branches = ("topic-4", "topic-1", "topic-2", "topic-3")
    server = FakeGitHubServer()
    server.seed_repository(repository)
    for index, branch in enumerate(branches, start=1):
        server.seed_pull_request(
            sync.GitHubPullRequest(
                sync.PullRequestId(repository.identity, index),
                f"PR_{index}", sync.PullRequestState.OPEN, False,
                repository.identity, branch, str(index) * 40, "main", "0" * 40,
                False, False, branch, "", None,
            )
        )

    client = FakeGitHubClient(server)
    observed = sync.observe_open_pull_request_candidates(client, repository, branches)

    assert tuple(item.head_branch for item in observed) == branches


def test_assignment_observer_mixes_exact_bookmark_and_template(monkeypatch) -> None:
    repository = sync.GitHubRepository(
        sync.GitHubRepositoryId("github.com", "R_repo"),
        "owner/repo",
        "https://github.com/owner/repo",
        "main",
    )
    commits = ("1" * 40, "2" * 40)
    local = dataclasses.replace(
        publication_assignment_input().local,
        local_bookmarks=(
            sync.LocalBookmark("intentional", sync.CommitTarget(commits[0])),
        ),
    )

    def jj(_workspace, _operation, *arguments):
        if arguments[:3] == ("config", "get", "templates.git_push_bookmark"):
            return '"generated-" ++ change_id.short()\n'
        commit = arguments[arguments.index("-r") + 1]
        return "generated-one\n" if commit == commits[0] else "generated-two\n"

    def refs(_push_url, identity, names, *, cwd):
        assert names == ("refs/heads/intentional", "refs/heads/generated-two")
        return tuple(
            sync.LiveRemoteRef(sync.RemoteBranchRef(identity, name), None)
            for name in names
        )

    monkeypatch.setattr(sync, "_jj", jj)
    monkeypatch.setattr(sync, "observe_live_refs", refs)
    monkeypatch.setattr(
        sync, "observe_historical_pull_request_collisions", lambda *_args, **_kwargs: ()
    )
    server = FakeGitHubServer()
    server.seed_repository(repository)
    client = FakeGitHubClient(server)

    assert sync.resolve_first_publication_assignments(
        "/repo",
        client,
        local,
        repository,
        "git@example/repo",
        commits,
        ("main",),
    ) == (
        sync.PublicationAssignment(commits[0], "intentional"),
        sync.PublicationAssignment(commits[1], "generated-two"),
    )


def test_observations_are_deeply_immutable() -> None:
    observed, _pr = snapshot(desired="1" * 40, live="1" * 40, parent="0" * 40)
    with pytest.raises(dataclasses.FrozenInstanceError):
        observed.local.operation_id = "other"  # type: ignore[misc]
    assert isinstance(observed.live_refs, tuple)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
