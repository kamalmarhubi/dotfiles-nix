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
    return sync.LocalObservation(
        "/repo", "op", config, (), (), (), (), (), "/git", remotes
    )


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

    tracked = sync.observe_local(jj_repo, revision="all()", config_keys=("git.push",))
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
    untracked = sync.observe_local(jj_repo, revision="all()", config_keys=("git.push",))
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
    absent = sync.observe_local(jj_repo, revision="all()", config_keys=("git.push",))
    assert all(bookmark.name != "topic" for bookmark in absent.local_bookmarks)


def test_private_inspection_fetch_is_invisible_to_jj_and_cleans_refs(
    jj_repo: Path, tmp_path: Path
) -> None:
    observed = sync.observe_local(jj_repo, revision="topic", config_keys=("git.push",))
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

    observed = sync.observe_local(jj_repo, revision="all()", config_keys=("git.push",))
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
        "boundary_aliases": [],
        "last_adopted_heads": [],
        "last_published_heads": [],
        "stacks": [],
    }
    assert sync.parse_state(payload) == sync.EMPTY_STATE


def test_boundary_alias_ownership_round_trip_is_distinct_from_head_authority() -> None:
    repository = sync.GitHubRepositoryId("github.com", "R_repo")
    ownership = sync.BoundaryAliasOwnership(
        sync.ServerStackIdentity(repository, "STACK_node", 17),
        sync.RemoteBranchRef(repository, "refs/heads/jj-stack/boundary/17"),
        True,
        "a" * 40,
    )
    state = sync.TrackedState((), (), (), (ownership,))

    assert sync.parse_state(sync.state_to_json(state)) == state
    assert state.last_published_heads == ()


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


def test_detached_state_is_strict_and_validates_bounded_membership() -> None:
    repository = sync.GitHubRepositoryId("github.com", "R_repo")
    active = sync.PullRequestId(repository, "PR_active")
    detached = sync.PullRequestId(repository, "PR_detached")
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

    other = sync.PullRequestId(repository, "PR_other")
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
    selected = sync.PullRequestId(repository, "PR_selected")
    other = sync.PullRequestId(repository, "PR_other")
    foreign = sync.PullRequestId(
        sync.GitHubRepositoryId("github.com", "R_other"), "PR_foreign"
    )

    assert sync.StandalonePullRequest(selected).pr == selected
    with pytest.raises(ValueError, match="nonempty"):
        sync.ServerStackIdentity(repository, "", 1)
    with pytest.raises(ValueError, match="nonempty"):
        sync.ServerStackMembership(
            selected, sync.ServerStackIdentity(repository, "stack", 1), "main", ()
        )
    for invalid in (0, -1, True):
        with pytest.raises(ValueError, match="positive integer"):
            sync.ServerStackIdentity(repository, "stack", invalid)
    with pytest.raises(ValueError, match="must belong"):
        sync.ServerStackMembership(
            selected, sync.ServerStackIdentity(repository, "stack", 1), "main", (other,)
        )
    with pytest.raises(ValueError, match="duplicates"):
        sync.ServerStackMembership(
            selected,
            sync.ServerStackIdentity(repository, "stack", 1),
            "main",
            (selected, selected),
        )
    with pytest.raises(ValueError, match="one repository"):
        sync.ServerStackMembership(
            selected,
            sync.ServerStackIdentity(repository, "stack", 1),
            "main",
            (selected, foreign),
        )
    with pytest.raises(ValueError, match="one repository"):
        sync.ServerStackMembership(
            selected,
            sync.ServerStackIdentity(foreign.repository, "stack", 1),
            "main",
            (selected,),
        )


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


def test_github_effects_send_exact_frozen_rest_requests(monkeypatch) -> None:
    repository = sync.GitHubRepositoryId("github.com", "R_repo")
    first = sync.PullRequestId(repository, "PR_1")
    second = sync.PullRequestId(repository, "PR_2")
    stack = sync.ServerStackIdentity(repository, "STACK_7", 7)
    calls = []

    def request(host, method, path, *, body=None, cwd=None):
        calls.append((host, method, path, body, cwd))
        return sync.GitHubResponse(200, (), b"{}")

    monkeypatch.setattr(sync, "github_http_request", request)
    effects = (
        sync.PullRequestBaseEffect(
            repository,
            "owner/repo",
            second,
            2,
            "main",
            "topic-1",
            sync.GitHubEffectPhase.POSSIBLY_SENT,
        ),
        sync.StackEffect(
            sync.StackEffectKind.CREATE,
            repository,
            "owner/repo",
            (1, 2),
            (),
            (first, second),
            phase=sync.GitHubEffectPhase.POSSIBLY_SENT,
        ),
        sync.StackEffect(
            sync.StackEffectKind.ADD,
            repository,
            "owner/repo",
            (2,),
            (first,),
            (first, second),
            stack,
            sync.GitHubEffectPhase.POSSIBLY_SENT,
        ),
        sync.StackEffect(
            sync.StackEffectKind.UNSTACK,
            repository,
            "owner/repo",
            (),
            (first, second),
            (),
            stack,
            sync.GitHubEffectPhase.POSSIBLY_SENT,
        ),
    )

    for effect in effects:
        sync.send_github_effect(effect, cwd="/workspace")

    assert calls == [
        (
            "github.com",
            "PATCH",
            "/repos/owner/repo/pulls/2",
            b'{"base":"topic-1"}',
            "/workspace",
        ),
        (
            "github.com",
            "POST",
            "/repos/owner/repo/stacks",
            b'{"pull_requests":[1,2]}',
            "/workspace",
        ),
        (
            "github.com",
            "POST",
            "/repos/owner/repo/stacks/7/add",
            b'{"pull_requests":[2]}',
            "/workspace",
        ),
        (
            "github.com",
            "POST",
            "/repos/owner/repo/stacks/7/unstack",
            None,
            "/workspace",
        ),
    ]
    with pytest.raises(ValueError, match="possibly-sent"):
        sync.send_github_effect(
            dataclasses.replace(effects[0], phase=sync.GitHubEffectPhase.NOT_ATTEMPTED)
        )


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


def test_complete_stack_source_validates_order_membership_and_base(monkeypatch) -> None:
    repository = sync.GitHubRepository(
        sync.GitHubRepositoryId("github.com", "R_repo"),
        "owner/repo",
        "https://github.com/owner/repo",
        "main",
    )
    records = [
        pr_source_record(
            id=f"PR_{number}",
            number=number,
            headRefName=f"topic-{number}",
            headRefOid=str(number) * 40,
            stack={"id": "STACK_node", "baseRefName": "main"},
        )
        for number in (1, 2, 3)
    ]
    response = {
        "data": {
            "node": {
                "id": "STACK_node",
                "number": 17,
                "baseRefName": "main",
                "entries": {
                    "nodes": [
                        {"position": position, "pullRequest": records[position - 1]}
                        for position in (3, 1, 2)
                    ],
                    "pageInfo": {"hasNextPage": False},
                },
            }
        }
    }
    monkeypatch.setattr(sync, "_github_graphql", lambda *_args, **_kwargs: response)

    stack = sync.observe_github_stack(repository, "STACK_node")

    assert stack.identity == sync.ServerStackIdentity(
        repository.identity, "STACK_node", 17
    )
    assert stack.base_branch == "main"
    assert tuple(pr.number for pr in stack.ordered_prs) == (1, 2, 3)

    response["data"]["node"]["entries"]["nodes"][0]["position"] = 4
    with pytest.raises(sync.IncompleteSource, match="positions"):
        sync.observe_github_stack(repository, "STACK_node")

    response["data"]["node"]["entries"]["nodes"][0]["position"] = 3
    response["data"]["node"]["number"] = True
    with pytest.raises(sync.MalformedSource, match="positive integer"):
        sync.observe_github_stack(repository, "STACK_node")


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
        identity = sync.PullRequestId(repository, f"PR_{index + 1}")
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
                index + 1,
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
            sync.ServerStackIdentity(repository, "STACK_node", 17),
            "main",
            tuple(pr.identity for pr in prs),
        ),
        tuple(live_refs[1:] + live_refs[:1]),
    )
    return observed, sync.StackSelection("main", tuple(assignments))


def preserved_prefix_case(
    *, boundary: str | None = "1" * 40, owned: bool = True
) -> tuple[sync.Snapshot, sync.DesiredStack, sync.GitHubRepository]:
    observed, _selection = stacked_snapshot(count=3, merged_prefix=1)
    repository = sync.GitHubRepository(
        observed.repository, "owner/repo", "https://github.com/owner/repo", "main"
    )
    integration = "b" * 40
    desired_ids = ("4" * 40, "5" * 40)
    commits = tuple(
        sync.ObservedCommit(
            oid,
            (integration if index == 0 else desired_ids[index - 1],),
            f"new-{index}",
            f"Title {index + 2}\n\nBody {index + 2}",
            False,
            False,
        )
        for index, oid in enumerate(desired_ids)
    )
    local = dataclasses.replace(
        observed.local,
        commits=commits,
        workspace_targets=(("default", desired_ids[-1]),),
    )
    stack = observed.membership.stack  # type: ignore[union-attr]
    alias_ref = sync.RemoteBranchRef(observed.repository, "refs/heads/topic-1")
    aliases = (
        (sync.BoundaryAliasOwnership(stack, alias_ref, True, boundary),)
        if owned and boundary is not None
        else ()
    )
    receipts = tuple(
        sync.LastPublishedHead(
            pr.identity,
            sync.RemoteBranchRef(observed.repository, f"refs/heads/{pr.head_branch}"),
            pr.reported_head_commit_id,
        )
        for pr in observed.pull_requests[1:]
    )
    state = dataclasses.replace(
        observed.tool_state.state,
        last_published_heads=receipts,
        boundary_aliases=aliases,
    )
    refs = tuple(
        dataclasses.replace(item, commit_id=boundary) if item.ref == alias_ref else item
        for item in observed.live_refs
    )
    observed = dataclasses.replace(
        observed,
        local=local,
        tool_state=sync.ToolStateRead("state-oid", state, None),
        live_refs=refs,
    )
    desired = sync.DesiredStack(
        observed.repository,
        "main",
        tuple(
            sync.DesiredExistingPR(pr.identity, oid, pr.title, pr.body)
            for pr, oid in zip(observed.pull_requests[1:], desired_ids, strict=True)
        ),
    )
    return observed, desired, repository


def preserved_prefix_operation(**kwargs: object) -> sync.PreservedPrefixRepair:
    observed, desired, repository = preserved_prefix_case(**kwargs)
    result = sync.plan_preserved_prefix_repair(observed, desired, repository, "origin")
    assert isinstance(result, sync.PreservedPrefixRepair)
    return result


def test_preserved_prefix_codec_is_strict_and_round_trips() -> None:
    operation = preserved_prefix_operation()
    payload = sync.preserved_prefix_repair_to_json(operation)
    assert sync.parse_preserved_prefix_repair(payload) == operation
    raw = json.loads(payload)
    raw["unknown"] = True
    with pytest.raises(sync.Error, match="unexpected fields"):
        sync.parse_preserved_prefix_repair(json.dumps(raw))
    raw = json.loads(payload)
    raw["ownership"]["unknown"] = True
    with pytest.raises(sync.Error, match="unexpected fields"):
        sync.parse_preserved_prefix_repair(json.dumps(raw))


def test_preserved_prefix_planner_alias_ownership_cases() -> None:
    observed, desired, repository = preserved_prefix_case()
    assert isinstance(
        sync.plan_preserved_prefix_repair(observed, desired, repository, "origin"),
        sync.PreservedPrefixRepair,
    )

    correct = dataclasses.replace(
        observed,
        live_refs=tuple(
            dataclasses.replace(item, commit_id="b" * 40)
            if item.ref.full_name == "refs/heads/topic-1"
            else item
            for item in observed.live_refs
        ),
        tool_state=dataclasses.replace(
            observed.tool_state,
            state=dataclasses.replace(
                observed.tool_state.state,
                boundary_aliases=(
                    dataclasses.replace(
                        observed.tool_state.state.boundary_aliases[0],
                        last_owned_commit_id="b" * 40,
                    ),
                ),
            ),
        ),
    )
    assert (
        sync.plan_preserved_prefix_repair(correct, desired, repository, "origin")
        is None
    )

    absent, absent_desired, _ = preserved_prefix_case(boundary=None, owned=False)
    created = sync.plan_preserved_prefix_repair(
        absent, absent_desired, repository, "origin"
    )
    assert isinstance(created, sync.PreservedPrefixRepair)
    assert created.plan.ownership.created

    unowned = dataclasses.replace(
        correct,
        tool_state=dataclasses.replace(
            correct.tool_state,
            state=dataclasses.replace(correct.tool_state.state, boundary_aliases=()),
        ),
    )
    blocked = sync.plan_preserved_prefix_repair(unowned, desired, repository, "origin")
    assert isinstance(blocked, sync.Blocked)
    assert blocked.reasons[0].code == "foreign-boundary"

    moved = dataclasses.replace(
        observed,
        live_refs=tuple(
            dataclasses.replace(item, commit_id="9" * 40)
            if item.ref.full_name == "refs/heads/topic-1"
            else item
            for item in observed.live_refs
        ),
    )
    blocked = sync.plan_preserved_prefix_repair(moved, desired, repository, "origin")
    assert isinstance(blocked, sync.Blocked)
    assert blocked.reasons[0].code == "boundary-moved"


@pytest.mark.parametrize(("boundary", "lease"), (("1" * 40, "1" * 40), (None, "")))
def test_preserved_prefix_atomic_push_contains_all_updates_and_exact_lease(
    monkeypatch, boundary: str | None, lease: str
) -> None:
    operation = preserved_prefix_operation(
        boundary=boundary, owned=boundary is not None
    )
    seen: list[str] = []
    monkeypatch.setattr(sync, "git_common_dir", lambda _workspace: Path("/git"))
    monkeypatch.setattr(
        sync.subprocess,
        "run",
        lambda argv, **_kwargs: seen.extend(argv)
        or subprocess.CompletedProcess(argv, 0, "", ""),
    )
    sync.push_preserved_prefix_updates("/repo", operation)
    assert "--atomic" in seen
    assert f"--force-with-lease=refs/heads/topic-1:{lease}" in seen
    for update in operation.plan.carrier.head_updates:
        assert f"{update.new_commit_id}:{update.ref.full_name}" in seen
        assert (
            f"--force-with-lease={update.ref.full_name}:{update.expected_old_commit_id}"
            in seen
        )
    assert f"{'b' * 40}:refs/heads/topic-1" in seen


def test_preserved_prefix_exact_same_stack_relink_effect(monkeypatch) -> None:
    operation = preserved_prefix_operation()
    effects = []
    monkeypatch.setattr(
        sync,
        "send_github_effect",
        lambda effect, **_kwargs: effects.append(effect)
        or sync.GitHubResponse(200, (), b"{}"),
    )
    sync.run_preserved_prefix_stack_link("/repo", operation)
    assert effects == [
        sync.StackEffect(
            sync.StackEffectKind.ADD,
            operation.plan.carrier.repository,
            operation.plan.carrier.repository_name,
            (2, 3),
            tuple(item.identity for item in operation.plan.merged_prefix),
            tuple(item.identity for item in operation.plan.carrier.dependencies.prs),
            operation.plan.stack,
            sync.GitHubEffectPhase.POSSIBLY_SENT,
        )
    ]


def test_preserved_prefix_final_state_separates_receipts_and_alias_ownership() -> None:
    operation = preserved_prefix_operation()
    initial = preserved_prefix_case()[0].tool_state.state
    final = sync._build_preserved_prefix_state(operation, initial)
    assert {item.pr for item in final.last_published_heads} == {
        item.pr_identity for item in operation.plan.carrier.desired.active
    }
    assert final.boundary_aliases == (operation.plan.ownership,)
    assert all(
        item.ref != operation.plan.boundary.ref for item in final.last_published_heads
    )


def test_preserved_prefix_render_apply_and_resume_dispatch(monkeypatch) -> None:
    operation = preserved_prefix_operation()
    assert "preserved-prefix repair" in sync.render_explicit(operation)
    monkeypatch.setattr(
        sync, "start_preserved_prefix_repair", lambda value, workspace: "fence"
    )
    monkeypatch.setattr(
        sync, "resume_preserved_prefix_repair", lambda workspace: "resumed"
    )
    assert sync.apply_explicit(operation, "/repo") == "resumed"
    monkeypatch.setattr(sync, "read_operation", lambda workspace: ("oid", operation))
    assert sync.resume_operation("/repo") == "resumed"


@pytest.mark.parametrize("readback", ("old", "final", "mixed"))
def test_preserved_prefix_push_classifies_old_final_and_mixed_readback(
    monkeypatch, readback: str
) -> None:
    operation = preserved_prefix_operation()
    plan = operation.plan.carrier
    old = tuple(item.expected_old_commit_id for item in plan.head_updates) + (
        operation.plan.boundary.commit_id,
        operation.plan.integration.commit_id,
    )
    final = tuple(item.new_commit_id for item in plan.head_updates) + (
        operation.plan.integration.commit_id,
        operation.plan.integration.commit_id,
    )
    values = list(old if readback in {"old", "mixed"} else final)
    if readback == "mixed":
        values[0] = final[0]
    pushed = []
    monkeypatch.setattr(sync, "repository_lock", lambda _workspace: nullcontext())
    monkeypatch.setattr(
        sync, "read_operation", lambda _workspace: ("op-oid", operation)
    )
    monkeypatch.setattr(sync, "read_ref_oid", lambda _workspace, _ref: "state-oid")
    monkeypatch.setattr(sync, "_preserved_error", lambda *_args: None)
    monkeypatch.setattr(
        sync,
        "_topology_refs",
        lambda _plan, _workspace, _refs: tuple(
            sync.LiveRemoteRef(ref, oid) for ref, oid in zip(_refs, values, strict=True)
        ),
    )
    monkeypatch.setattr(
        sync,
        "cas_write_preserved_prefix_repair",
        lambda *_args, **_kwargs: "next-op",
    )

    def push(_workspace, _operation):
        pushed.append(True)
        values[:] = final
        return subprocess.CompletedProcess([], 0)

    monkeypatch.setattr(sync, "push_preserved_prefix_updates", push)
    # Stop after publication so this test isolates its readback classifier.
    monkeypatch.setattr(
        sync,
        "run_stack_unstack",
        lambda *_args: (_ for _ in ()).throw(sync.Error("stop")),
    )
    result = sync.resume_preserved_prefix_repair("/repo")
    assert bool(pushed) is (readback == "old")
    if readback == "mixed":
        assert isinstance(result, sync.Stopped)
        assert "mixed-or-foreign" in result.detail


def test_preserved_prefix_partial_unstack_blocks_without_replaying(monkeypatch) -> None:
    operation = dataclasses.replace(
        preserved_prefix_operation(),
        phase=sync.PreservedPrefixRepairPhase.UNSTACKING,
        publication_possibly_sent=True,
    )
    plan = operation.plan.carrier
    final = tuple(item.new_commit_id for item in plan.head_updates) + (
        operation.plan.integration.commit_id,
        operation.plan.integration.commit_id,
    )
    calls = []
    monkeypatch.setattr(sync, "repository_lock", lambda _workspace: nullcontext())
    monkeypatch.setattr(sync, "read_operation", lambda _workspace: ("oid", operation))
    monkeypatch.setattr(sync, "read_ref_oid", lambda *_args: "state-oid")
    monkeypatch.setattr(
        sync, "cas_write_preserved_prefix_repair", lambda *_args: "oid2"
    )
    monkeypatch.setattr(
        sync,
        "_topology_refs",
        lambda _plan, _workspace, refs: tuple(
            sync.LiveRemoteRef(ref, oid) for ref, oid in zip(refs, final, strict=True)
        ),
    )
    monkeypatch.setattr(
        sync, "_preserved_error", lambda _op, _ws, expected: "partial membership"
    )
    monkeypatch.setattr(sync, "run_stack_unstack", lambda *_args: calls.append(True))
    result = sync.resume_preserved_prefix_repair("/repo")
    assert isinstance(result, sync.Stopped)
    assert result.stage == "unstack" and "partial" in result.detail
    assert calls == []


def test_preserved_prefix_final_relink_readback_does_not_replay(monkeypatch) -> None:
    operation = dataclasses.replace(
        preserved_prefix_operation(),
        phase=sync.PreservedPrefixRepairPhase.RELINKING,
        publication_possibly_sent=True,
        unstack_possibly_sent=True,
        relink_possibly_sent=True,
    )
    plan = operation.plan.carrier
    final = tuple(item.new_commit_id for item in plan.head_updates) + (
        operation.plan.integration.commit_id,
        operation.plan.integration.commit_id,
    )
    links = []
    monkeypatch.setattr(sync, "repository_lock", lambda _workspace: nullcontext())
    monkeypatch.setattr(sync, "read_operation", lambda _workspace: ("oid", operation))
    monkeypatch.setattr(sync, "read_ref_oid", lambda *_args: "state-oid")
    monkeypatch.setattr(
        sync,
        "_topology_refs",
        lambda _plan, _workspace, refs: tuple(
            sync.LiveRemoteRef(ref, oid) for ref, oid in zip(refs, final, strict=True)
        ),
    )
    monkeypatch.setattr(
        sync,
        "_preserved_error",
        lambda _op, _workspace, expected: "not prefix"
        if expected == "prefix"
        else None,
    )
    monkeypatch.setattr(
        sync, "run_preserved_prefix_stack_link", lambda *_args: links.append(True)
    )
    monkeypatch.setattr(
        sync,
        "cas_write_preserved_prefix_repair",
        lambda _workspace, _oid, saved: (
            (_ for _ in ()).throw(sync.Error("stop after classification"))
            if saved.phase is sync.PreservedPrefixRepairPhase.VERIFYING
            else "oid2"
        ),
    )
    result = sync.resume_preserved_prefix_repair("/repo")
    assert isinstance(result, sync.Stopped)
    assert links == []


def test_preserved_prefix_repeated_verification_commits_state_before_fence(
    monkeypatch,
) -> None:
    operation = dataclasses.replace(
        preserved_prefix_operation(), phase=sync.PreservedPrefixRepairPhase.VERIFYING
    )
    plan = operation.plan.carrier
    final = tuple(item.new_commit_id for item in plan.head_updates) + (
        operation.plan.integration.commit_id,
        operation.plan.integration.commit_id,
    )
    state = preserved_prefix_case()[0].tool_state.state
    current = ["state-oid"]
    events: list[str] = []
    projections: list[str] = []
    monkeypatch.setattr(sync, "repository_lock", lambda _workspace: nullcontext())
    monkeypatch.setattr(
        sync, "read_operation", lambda _workspace: ("operation-oid", operation)
    )
    monkeypatch.setattr(sync, "read_ref_oid", lambda _workspace, _ref: current[0])
    monkeypatch.setattr(sync, "read_state", lambda _workspace: (current[0], state))
    monkeypatch.setattr(
        sync,
        "_topology_refs",
        lambda _plan, _workspace, refs: tuple(
            sync.LiveRemoteRef(ref, oid) for ref, oid in zip(refs, final, strict=True)
        ),
    )
    monkeypatch.setattr(
        sync,
        "_preserved_error",
        lambda _op, _workspace, expected: projections.append(expected) or None,
    )
    monkeypatch.setattr(sync, "_run", lambda *_args, **_kwargs: "final-oid\n")
    monkeypatch.setattr(
        sync, "cas_write_preserved_prefix_repair", lambda *_args: "operation-oid-2"
    )

    def write_state(_workspace, _expected, _state):
        events.append("state")
        current[0] = "final-oid"
        return "final-oid"

    monkeypatch.setattr(sync, "cas_write_state", write_state)
    monkeypatch.setattr(
        sync, "cas_delete_operation", lambda *_args: events.append("fence")
    )
    result = sync.resume_preserved_prefix_repair("/repo")
    assert isinstance(result, sync.PreservedPrefixRepairVerified)
    assert projections.count("final") >= 3
    assert events == ["state", "fence"]


def test_explicit_merged_prefix_routes_to_preserved_prefix_repair(monkeypatch) -> None:
    observed, desired, repository = preserved_prefix_case(boundary=None, owned=False)
    desired_ids = tuple(item.desired_commit_id for item in desired.active)
    open_prs = tuple(
        pr for pr in observed.pull_requests if pr.state is sync.PullRequestState.OPEN
    )
    pull_requests = (
        observed.pull_requests[0],
        dataclasses.replace(
            open_prs[0],
            base_branch="main",
            reported_base_commit_id="b" * 40,
        ),
        open_prs[1],
    )
    local = dataclasses.replace(
        observed.local,
        local_bookmarks=tuple(
            sync.LocalBookmark(pr.head_branch, sync.CommitTarget(commit_id))
            for pr, commit_id in zip(open_prs, desired_ids, strict=True)
        ),
    )
    observed = dataclasses.replace(observed, local=local, pull_requests=pull_requests)
    monkeypatch.setattr(sync, "observe_local", lambda *_args, **_kwargs: local)
    monkeypatch.setattr(sync, "observe_snapshot", lambda *_args, **_kwargs: observed)
    monkeypatch.setattr(
        sync, "observe_revision_boundaries", lambda *_args, **_kwargs: local.commits
    )
    monkeypatch.setattr(
        sync, "observe_github_repository", lambda *_args, **_kwargs: repository
    )

    result = sync.plan_explicit_existing(
        "/repo", sync.ExistingIntent(open_prs[0].number, "selected")
    )

    assert isinstance(result, sync.PreservedPrefixRepair)
    assert tuple(
        item.pr_identity for item in result.plan.carrier.desired.active
    ) == tuple(pr.identity for pr in open_prs)


def test_explicit_completed_stack_routes_new_suffix_to_append(monkeypatch) -> None:
    observed, _selection = stacked_snapshot(count=2, merged_prefix=2)
    new_commit_id = "9" * 40
    new_commit = sync.ObservedCommit(
        new_commit_id,
        ("b" * 40,),
        "new-change",
        "New title\n\nNew body",
        False,
        False,
    )
    local = dataclasses.replace(
        observed.local,
        commits=(new_commit,),
        local_bookmarks=(sync.LocalBookmark("new", sync.CommitTarget(new_commit_id)),),
        workspace_targets=(("default", new_commit_id),),
    )
    observed = dataclasses.replace(observed, local=local)
    repository = sync.GitHubRepository(
        observed.repository, "owner/repo", "https://github.com/owner/repo", "main"
    )
    monkeypatch.setattr(sync, "observe_local", lambda *_args, **_kwargs: local)
    monkeypatch.setattr(sync, "observe_snapshot", lambda *_args, **_kwargs: observed)
    monkeypatch.setattr(
        sync, "observe_revision_boundaries", lambda *_args, **_kwargs: (new_commit,)
    )
    monkeypatch.setattr(
        sync, "observe_github_repository", lambda *_args, **_kwargs: repository
    )
    monkeypatch.setattr(sync, "resolve_push_url", lambda *_args: observed.push_url)
    monkeypatch.setattr(
        sync,
        "resolve_first_publication_assignments",
        lambda *_args, **_kwargs: (sync.PublicationAssignment(new_commit_id, "new"),),
    )

    def refs(_url, identity, names, *, cwd):
        values = {"refs/heads/main": "b" * 40, "refs/heads/new": None}
        return tuple(
            sync.LiveRemoteRef(sync.RemoteBranchRef(identity, name), values[name])
            for name in names
        )

    monkeypatch.setattr(sync, "observe_live_refs", refs)

    result = sync.plan_explicit_existing(
        "/repo", sync.ExistingIntent(observed.pull_requests[0].number, "selected")
    )

    assert isinstance(result, sync.FirstPublication)
    assert result.target is sync.FirstPublicationTarget.APPEND
    assert result.existing_stack == observed.membership.stack
    assert tuple(item.state for item in result.existing_members) == (
        sync.PullRequestState.MERGED,
        sync.PullRequestState.MERGED,
    )
    assert result.slots[0].base_branch == "main"


def standalone_set_planning_case():
    repository = sync.GitHubRepositoryId("github.com", "R_repo")
    base = "0" * 40
    desired_ids = tuple(str(index) * 40 for index in range(1, 7))
    old_ids = tuple(character * 40 for character in "abcd")
    commits = []
    # The selected stack and old PR comparisons may have external-root ancestry;
    # the PR head/base name graph, not Git parentage, proves those outer edges.
    previous = "9" * 40
    for index, commit_id in enumerate(desired_ids, start=1):
        commits.append(
            sync.ObservedCommit(
                commit_id,
                (previous,),
                f"change-{index}",
                f"Title {index}\n\nBody {index}",
                False,
                False,
            )
        )
        previous = commit_id
    previous = base
    for index, commit_id in enumerate(old_ids, start=1):
        parent = "e" * 40 if index == 3 else previous
        commits.append(
            sync.ObservedCommit(
                commit_id,
                (parent,),
                f"old-change-{index}",
                f"Old {index}",
                False,
                False,
            )
        )
        previous = commit_id
    commits.append(sync.ObservedCommit(base, (), "base", "Base", False, False))
    prs = []
    for index in range(1, 5):
        prs.append(
            sync.GitHubPullRequest(
                sync.PullRequestId(repository, f"PR_{index}"),
                index,
                sync.PullRequestState.OPEN,
                False,
                repository,
                f"topic-{index}",
                old_ids[index - 1],
                "dev" if index == 1 else f"topic-{index - 1}",
                base if index == 1 else old_ids[index - 2],
                False,
                False,
                f"Title {index}",
                f"Body {index}",
            )
        )
    prs = tuple(prs)
    desired_by_branch = {
        "topic-4": desired_ids[0],
        "topic-1": desired_ids[3],
        "topic-2": desired_ids[4],
        "topic-3": desired_ids[5],
    }
    local = sync.LocalObservation(
        "/repo",
        "op",
        (("git.push", "origin"),),
        (("default", desired_ids[-1]),),
        tuple(
            sync.LocalBookmark(
                pr.head_branch, sync.CommitTarget(desired_by_branch[pr.head_branch])
            )
            for pr in prs
        ),
        (),
        (),
        tuple(commits),
        "/git",
        ("origin",),
    )
    refs = (
        sync.LiveRemoteRef(
            sync.RemoteBranchRef(repository, "refs/heads/dev"), "8" * 40
        ),
        *(
            sync.LiveRemoteRef(
                sync.RemoteBranchRef(repository, f"refs/heads/topic-{index}"),
                old_ids[index - 1],
            )
            for index in range(1, 5)
        ),
    )
    snapshot = sync.Snapshot(
        repository,
        "ssh://git@github.com/owner/repo.git",
        "origin",
        local,
        sync.ToolStateRead(None, sync.EMPTY_STATE, None),
        (prs[0],),
        sync.StandalonePullRequest(prs[0].identity),
        (refs[0], refs[1]),
    )
    github_repository = sync.GitHubRepository(
        repository, "owner/repo", "https://github.com/owner/repo", "main"
    )
    sources = tuple(sync.GitHubPullRequestSource(pr, None, None) for pr in prs)
    boundaries = tuple(commits[:6])
    return snapshot, github_repository, sources, boundaries, refs, desired_ids, old_ids


def test_explicit_four_standalone_prs_plus_two_new_requires_scoped_bootstrap(
    monkeypatch,
) -> None:
    snapshot, repository, sources, boundaries, refs, desired_ids, old_ids = (
        standalone_set_planning_case()
    )
    monkeypatch.setattr(
        sync,
        "observe_local",
        lambda *_args, **kwargs: (
            dataclasses.replace(snapshot.local, commits=boundaries)
            if kwargs.get("ancestry_order")
            else snapshot.local
        ),
    )
    monkeypatch.setattr(sync, "observe_snapshot", lambda *_args, **_kwargs: snapshot)
    monkeypatch.setattr(
        sync, "observe_revision_boundaries", lambda *_args, **_kwargs: boundaries
    )
    monkeypatch.setattr(
        sync, "observe_github_repository", lambda *_args, **_kwargs: repository
    )
    candidate_calls = []
    monkeypatch.setattr(
        sync,
        "observe_open_pull_request_candidates",
        lambda _repository, branches, **_kwargs: candidate_calls.append(tuple(branches))
        or sources,
    )
    monkeypatch.setattr(
        sync,
        "resolve_first_publication_assignments",
        lambda *_args, **_kwargs: (
            sync.PublicationAssignment(desired_ids[1], "topic-new-1"),
            sync.PublicationAssignment(desired_ids[2], "topic-new-2"),
        ),
    )

    def observe_refs(_url, identity, names, *, cwd):
        by_name = {item.ref.full_name: item.commit_id for item in refs}
        return tuple(
            sync.LiveRemoteRef(sync.RemoteBranchRef(identity, name), by_name.get(name))
            for name in names
        )

    monkeypatch.setattr(sync, "observe_live_refs", observe_refs)

    blocked = sync.plan_explicit_existing("/repo", sync.ExistingIntent(1, "selected"))
    operation = sync.plan_explicit_existing(
        "/repo", sync.ExistingIntent(1, "selected", bootstrap_local_wins=True)
    )

    assert isinstance(blocked, sync.Blocked)
    assert blocked.reasons[0].code == "replacement-unauthorized"
    assert isinstance(operation, sync.MembershipRepair)
    assert operation.source_membership == sync.StandalonePullRequestSet(
        tuple(source.pr.identity for source in sources)
    )
    assert operation.source_tracked is None
    assert operation.base_branch == "dev"
    assert len(operation.desired) == 6
    assert len(operation.slots) == 2
    assert candidate_calls == [("topic-1", "topic-2", "topic-3", "topic-4")] * 2
    assert tuple(
        update.expected_old_commit_id for update in operation.head_updates
    ) == (old_ids[3], old_ids[0], old_ids[1], old_ids[2])
    assert tuple(update.new_commit_id for update in operation.head_updates) == (
        desired_ids[0],
        desired_ids[3],
        desired_ids[4],
        desired_ids[5],
    )
    assert tuple(
        item.pr_identity if isinstance(item, sync.ExistingMembershipEntry) else None
        for item in operation.desired
    ) == (
        sources[3].pr.identity,
        None,
        None,
        sources[0].pr.identity,
        sources[1].pr.identity,
        sources[2].pr.identity,
    )
    assert all(
        isinstance(update.authority, sync.BootstrapLocalWins)
        for update in operation.head_updates
    )
    assert (
        sync.parse_membership_repair(sync.membership_repair_to_json(operation))
        == operation
    )
    rendered = sync.render_explicit(operation)
    assert "ultimate base: dev" in rendered
    assert "one-shot bootstrap replacements" in rendered
    assert f"{old_ids[3]} -> {desired_ids[0]}" in rendered
    assert f"{old_ids[0]} -> {desired_ids[3]}" in rendered
    bound = dataclasses.replace(
        operation,
        slots=tuple(
            dataclasses.replace(
                slot,
                phase=sync.NewPRPhase.VERIFIED,
                pr_identity=sync.PullRequestId(operation.repository, f"PR_new_{index}"),
                pr_number=10 + index,
            )
            for index, slot in enumerate(operation.slots, start=1)
        ),
    )
    final_state = sync._build_membership_final_state(bound, sync.EMPTY_STATE)
    assert len(final_state.stacks) == 1
    assert final_state.last_adopted_heads == ()
    assert {
        (receipt.pr, receipt.ref, receipt.verified_commit_id)
        for receipt in final_state.last_published_heads
    } == {
        (
            source.pr.identity,
            sync.RemoteBranchRef(
                operation.repository, f"refs/heads/{source.pr.head_branch}"
            ),
            desired_commit,
        )
        for source, desired_commit in zip(
            sources,
            (desired_ids[3], desired_ids[4], desired_ids[5], desired_ids[0]),
            strict=True,
        )
    } | {
        (
            slot.pr_identity,
            sync.RemoteBranchRef(operation.repository, f"refs/heads/{slot.branch}"),
            slot.commit_id,
        )
        for slot in bound.slots
    }


@pytest.mark.parametrize(
    ("mutation", "code"),
    (
        ("mixed", "mixed-stack-membership"),
        ("foreign", "foreign-desired"),
        ("incomplete", "incomplete-observation"),
        ("ambiguous", "ambiguous-boundary"),
    ),
)
def test_standalone_set_resolution_blocks_unproven_associations(
    mutation: str, code: str
) -> None:
    snapshot, _repository, sources, boundaries, _refs, _desired, _old = (
        standalone_set_planning_case()
    )
    candidates = list(sources)
    commits = boundaries
    if mutation == "mixed":
        candidates[1] = dataclasses.replace(
            candidates[1], stack_id="STACK", stack_base_branch="main"
        )
    elif mutation == "foreign":
        fork = sync.GitHubRepositoryId("github.com", "R_fork")
        candidates[1] = dataclasses.replace(
            candidates[1],
            pr=dataclasses.replace(candidates[1].pr, head_repository=fork),
        )
    elif mutation == "incomplete":
        commits = (*boundaries[:3], *boundaries[4:])
    else:
        candidates.append(
            dataclasses.replace(
                candidates[0],
                pr=dataclasses.replace(
                    candidates[0].pr,
                    identity=sync.PullRequestId(snapshot.repository, "PR_ambiguous"),
                    number=99,
                ),
            )
        )

    result = sync.resolve_standalone_membership_boundaries(
        snapshot.local,
        snapshot.repository,
        sources[0].pr.identity,
        candidates,
        commits,
    )

    assert isinstance(result, sync.Blocked)
    assert result.reasons[0].code == code


@pytest.mark.parametrize(
    ("shape", "code"),
    (
        ("branched", "branched-source-graph"),
        ("multiple-root", "multiple-root-source-graph"),
        ("disconnected-cycle", "disconnected-source-graph"),
    ),
)
def test_standalone_source_graph_fails_closed(shape: str, code: str) -> None:
    snapshot, _repository, sources, boundaries, _refs, _desired, old = (
        standalone_set_planning_case()
    )
    candidates = list(sources)
    if shape == "branched":
        candidates[2] = dataclasses.replace(
            candidates[2],
            pr=dataclasses.replace(
                candidates[2].pr,
                base_branch="topic-1",
                reported_base_commit_id=old[0],
            ),
        )
    elif shape == "multiple-root":
        candidates[1] = dataclasses.replace(
            candidates[1],
            pr=dataclasses.replace(
                candidates[1].pr,
                base_branch="other",
                reported_base_commit_id="f" * 40,
            ),
        )
    else:
        candidates[2] = dataclasses.replace(
            candidates[2],
            pr=dataclasses.replace(
                candidates[2].pr,
                base_branch="topic-4",
                reported_base_commit_id=old[3],
            ),
        )

    result = sync.resolve_standalone_membership_boundaries(
        snapshot.local,
        snapshot.repository,
        sources[0].pr.identity,
        candidates,
        boundaries,
    )

    assert isinstance(result, sync.Blocked)
    assert result.reasons[0].code == code


def test_standalone_set_membership_repair_revalidates_without_unstack(
    monkeypatch,
) -> None:
    operation = membership_operation()
    source = sync.StandalonePullRequestSet(
        tuple(pr.identity for pr in operation.source_prs)
    )
    operation = dataclasses.replace(
        operation,
        source_membership=source,
        source_tracked=None,
        phase=sync.MembershipRepairPhase.UNSTACKING_SOURCE,
        slots=(
            dataclasses.replace(
                operation.slots[0],
                phase=sync.NewPRPhase.VERIFIED,
                pr_identity=sync.PullRequestId(operation.repository, "PR_new"),
                pr_number=9,
            ),
        ),
    )
    repository = sync.GitHubRepository(
        operation.repository,
        operation.repository_name,
        "https://github.com/owner/repo",
        "main",
    )
    monkeypatch.setattr(sync, "repository_lock", lambda _workspace: nullcontext())
    monkeypatch.setattr(
        sync, "read_operation", lambda _workspace: ("operation-oid", operation)
    )
    monkeypatch.setattr(
        sync, "_frozen_repository", lambda *_args, **_kwargs: repository
    )
    monkeypatch.setattr(
        sync, "read_ref_oid", lambda *_args: operation.expected_state_oid
    )
    monkeypatch.setattr(sync, "_membership_source_refs_error", lambda *_args: None)
    monkeypatch.setattr(
        sync,
        "_observe_topology_pr",
        lambda _repository, pr, **_kwargs: sync.GitHubPullRequestSource(pr, None, None),
    )
    monkeypatch.setattr(
        sync, "run_stack_unstack", lambda *_args: pytest.fail("unstack requested")
    )
    monkeypatch.setattr(
        sync, "cas_write_membership_repair", lambda *_args: "operation-oid"
    )
    monkeypatch.setattr(
        sync,
        "_membership_projection",
        lambda *_args: (_ for _ in ()).throw(
            sync.Error("stop after association check")
        ),
    )

    result = sync.resume_membership_repair("/repo")

    assert isinstance(result, sync.Stopped)
    assert result.stage == sync.MembershipRepairPhase.TRANSITIONING_BASES


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
            reported_head_commit_id=old,
            reported_base_commit_id=base if index == 0 else old_heads[index - 1],
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
    assert tuple(item.pr_identity.node_id for item in desired.active) == (
        "PR_2",
        "PR_3",
    )
    assert observed.membership.selected_pr.node_id == "PR_1"
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
        reported_base_commit_id="b" * 40,
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
            sync.RemoteBranchRef(observed.repository, f"refs/heads/{pr.head_branch}"),
            pr.reported_head_commit_id,
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
    source = sync.prepare_topology_planning(observed, repository, "origin", desired)
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
            reported_base_commit_id="b" * 40,
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
        stack = sync.GitHubStackSource(
            sync.ServerStackIdentity(snapshot.repository, "STACK_surviving", 41),
            "main",
            stack_members,
        )
    sources = tuple(
        sync.GitHubPullRequestSource(
            pr,
            stack.stack_id if stack is not None and pr in stack_members else None,
            stack.base_branch if stack is not None and pr in stack_members else None,
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
        sources,
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
    sources = value.observation.pr_sources
    changed = dataclasses.replace(
        value.observation,
        pr_sources=(
            *sources[:2],
            dataclasses.replace(
                sources[2], stack_id="STACK_other", stack_base_branch="main"
            ),
        ),
    )
    result = sync.plan_tracked_restore(dataclasses.replace(value, observation=changed))
    assert isinstance(result, sync.Blocked)
    assert result.reasons[0].code == "ambiguous-membership"


def test_tracked_restore_final_state_preserves_detached_without_head_receipts() -> None:
    value = tracked_restore_input()
    detached = sync.PullRequestId(value.observation.tracked.repository, "PR_detached")
    tracked = dataclasses.replace(value.observation.tracked, detached_prs=(detached,))
    state = dataclasses.replace(value.observation.tool_state.state, stacks=(tracked,))
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
        ((2, 1), ("PR_2", "PR_1"), ()),
        ((1,), ("PR_1",), ("PR_2",)),
        ((), (), ("PR_1", "PR_2")),
    ),
)
def test_fully_open_topology_plans_reorder_subset_and_empty(
    desired_order: tuple[int, ...], active: tuple[str, ...], detached: tuple[str, ...]
) -> None:
    plan = sync.plan_topology(topology_input(desired_order))

    assert isinstance(plan, sync.TopologyPlan)
    assert plan.tracking_update is not None
    assert tuple(pr.node_id for pr in plan.tracking_update.ordered_prs) == active
    assert tuple(pr.node_id for pr in plan.detached_prs) == detached
    assert len(plan.temporary_bases) == len(active)
    if desired_order == (2, 1):
        assert tuple(
            (
                temporary_base.pr_identity.node_id,
                temporary_base.old_base_commit_id,
                temporary_base.new_base_commit_id,
            )
            for temporary_base in plan.temporary_bases
        ) == (
            ("PR_2", "1" * 40, "b" * 40),
            ("PR_1", "b" * 40, "3" * 40),
        )
    rendered = sync.render_topology(plan)
    assert "stack #17 (STACK_node)" in rendered
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
        active=(dataclasses.replace(desired.active[0], desired_commit_id="9" * 40),),
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
            stack=dataclasses.replace(observed.membership.stack, number=999),
        ),
    )
    stable = sync.prepare_topology_planning(excluded, repository, "elsewhere", desired)
    assert isinstance(stable, sync.TopologyPlanningSource)
    assert stable.naming_digest == first.naming_digest
    assert stable.temporary_base_refs == first.temporary_base_refs
    assert tuple(
        ref.full_name.rsplit("/", 1)[1] for ref in first.temporary_base_refs
    ) == ("1",)
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
            pr.identity, pr.reported_head_commit_id, pr.title, pr.body
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


def membership_operation() -> sync.MembershipRepair:
    plan = sync.plan_topology(topology_input((2, 1)))
    assert isinstance(plan, sync.TopologyPlan)
    assert isinstance(plan.source, sync.ServerStackMembership)
    source = plan.source
    slot = sync.NewPRSlot(
        "slot-new",
        "new-branch",
        "9" * 40,
        sync.BookmarkSetup.CREATE,
        "topic-1",
        "New",
        "body",
    )
    return sync.MembershipRepair(
        plan.repository,
        plan.repository_name,
        plan.dependencies.push_url,
        plan.remote,
        plan.dependencies.effective_config,
        (("default", "change-id"),),
        "main",
        plan.dependencies.state_blob_oid,
        source,
        tuple(
            pr for pr in plan.dependencies.prs if pr.state is sync.PullRequestState.OPEN
        ),
        sync.TrackedStack(plan.repository, "main", source.ordered_prs, ()),
        (
            sync.ExistingMembershipEntry(source.ordered_prs[0]),
            sync.NewMembershipEntry(slot.slot_id),
        ),
        (slot,),
        plan.head_updates,
        plan.temporary_bases,
    )


def test_membership_repair_round_trip_is_exact_and_strict() -> None:
    operation = membership_operation()
    encoded = sync.membership_repair_to_json(operation)
    assert sync.parse_membership_repair(encoded) == operation
    assert sync.parse_operation(encoded) == operation
    malformed = json.loads(encoded)
    malformed["slots"][0]["unknown"] = True
    with pytest.raises(sync.Error, match="unexpected fields"):
        sync.parse_membership_repair(json.dumps(malformed))


@pytest.mark.parametrize("duplicate", ("identity", "slot"))
def test_membership_repair_rejects_duplicate_desired_entries(duplicate: str) -> None:
    operation = membership_operation()
    entry = operation.desired[0 if duplicate == "identity" else 1]
    with pytest.raises(ValueError, match="desired membership"):
        sync.membership_repair_to_json(
            dataclasses.replace(operation, desired=operation.desired + (entry,))
        )


def test_membership_repair_rejects_source_goal_mismatch() -> None:
    operation = membership_operation()
    foreign = sync.PullRequestId(operation.repository, "PR_not_in_source")
    with pytest.raises(ValueError, match="desired membership"):
        sync.membership_repair_to_json(
            dataclasses.replace(
                operation,
                desired=(sync.ExistingMembershipEntry(foreign), *operation.desired[1:]),
            )
        )


def test_membership_repair_committing_requires_frozen_final_state() -> None:
    initial = membership_operation()
    verified_slot = dataclasses.replace(
        initial.slots[0],
        phase=sync.NewPRPhase.VERIFIED,
        pr_identity=initial.source_tracked.ordered_prs[1],
        pr_number=2,
    )
    operation = dataclasses.replace(
        initial,
        slots=(verified_slot,),
        phase=sync.MembershipRepairPhase.COMMITTING,
    )
    with pytest.raises(ValueError, match="freeze final state"):
        sync.membership_repair_to_json(operation)
    final_json = sync.state_to_json(sync.EMPTY_STATE)
    committed = dataclasses.replace(
        operation, final_state_json=final_json, final_state_oid="f" * 40
    )
    assert (
        sync.parse_membership_repair(sync.membership_repair_to_json(committed))
        == committed
    )


def test_membership_repair_reobserves_source_after_unstack(monkeypatch) -> None:
    initial = membership_operation()
    slot = dataclasses.replace(
        initial.slots[0],
        phase=sync.NewPRPhase.VERIFIED,
        pr_identity=sync.PullRequestId(initial.repository, "PR_new"),
        pr_number=9,
    )
    operation = dataclasses.replace(
        initial,
        slots=(slot,),
        phase=sync.MembershipRepairPhase.UNSTACKING_SOURCE,
    )
    repository = sync.GitHubRepository(
        operation.repository,
        operation.repository_name,
        "https://github.com/owner/repo",
        "main",
    )
    frozen = tuple(
        sync.GitHubPullRequestSource(
            pr, operation.source_membership.server_stack_id, "main"
        )
        for pr in operation.source_prs
    )
    standalone = tuple(
        sync.GitHubPullRequestSource(pr, None, None) for pr in operation.source_prs
    )
    observations = iter(
        (
            (
                frozen,
                sync.GitHubStackSource(
                    operation.source_membership.stack, "main", operation.source_prs
                ),
            ),
            (standalone, None),
        )
    )
    calls: list[str] = []

    monkeypatch.setattr(sync, "repository_lock", lambda _workspace: nullcontext())
    monkeypatch.setattr(
        sync, "read_operation", lambda _workspace: ("operation-oid", operation)
    )
    monkeypatch.setattr(
        sync, "_frozen_repository", lambda *_args, **_kwargs: repository
    )
    monkeypatch.setattr(
        sync, "read_ref_oid", lambda *_args: operation.expected_state_oid
    )
    monkeypatch.setattr(sync, "_membership_source_refs_error", lambda *_args: None)
    monkeypatch.setattr(
        sync,
        "_observe_source_association",
        lambda *_args, **_kwargs: next(observations),
    )
    monkeypatch.setattr(
        sync, "run_stack_unstack", lambda *_args: calls.append("unstack")
    )
    monkeypatch.setattr(
        sync,
        "prove_stack_dissolved",
        lambda *_args, **_kwargs: sync.StackDissolution.ABSENT,
    )
    monkeypatch.setattr(
        sync, "cas_write_membership_repair", lambda *_args: "operation-oid"
    )
    monkeypatch.setattr(
        sync,
        "_membership_projection",
        lambda *_args: (_ for _ in ()).throw(sync.Error("stop after unstack")),
    )

    result = sync.resume_membership_repair("/repo")

    assert calls == ["unstack"]
    assert isinstance(result, sync.Stopped)
    assert result.stage == sync.MembershipRepairPhase.TRANSITIONING_BASES


@pytest.mark.parametrize("initial", ("absent", "exact"))
def test_topology_creation_reconciles_complete_readback(
    monkeypatch, initial: str
) -> None:
    plan = sync.plan_topology(topology_input((2, 1)))
    assert isinstance(plan, sync.TopologyPlan)
    operation = sync.TopologyRepair(plan)
    old = tuple(item.old_base_commit_id for item in plan.temporary_bases)
    absent = (None,) * len(old)
    observations = [absent if initial == "absent" else old]
    if initial == "absent":
        observations.append(old)
    pushes: list[tuple[sync.PlannedTemporaryBase, ...]] = []

    monkeypatch.setattr(sync, "repository_lock", lambda _workspace: nullcontext())
    monkeypatch.setattr(sync, "read_operation", lambda _workspace: ("op", operation))
    monkeypatch.setattr(sync, "_frozen_repository", lambda *a, **k: object())
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

    result = sync.resume_topology_repair(".")

    assert isinstance(result, sync.Stopped)
    assert result.stage == sync.TopologyRepairPhase.UNSTACKING.value
    assert len(pushes) == (1 if initial == "absent" else 0)


def test_topology_publication_exact_readback_advances_without_repush(
    monkeypatch,
) -> None:
    plan = sync.plan_topology(topology_input((2, 1)))
    assert isinstance(plan, sync.TopologyPlan)
    operation = sync.TopologyRepair(plan, sync.TopologyRepairPhase.PUBLISHING)
    desired = tuple(item.new_commit_id for item in plan.head_updates) + tuple(
        item.new_base_commit_id for item in plan.temporary_bases
    )
    repushes: list[object] = []

    monkeypatch.setattr(sync, "repository_lock", lambda _workspace: nullcontext())
    monkeypatch.setattr(sync, "read_operation", lambda _workspace: ("op", operation))
    monkeypatch.setattr(sync, "_frozen_repository", lambda *a, **k: object())
    monkeypatch.setattr(sync, "cas_write_topology_repair", lambda *a, **k: "next")
    monkeypatch.setattr(
        sync,
        "_observe_topology_pr",
        lambda repository, pr, **kwargs: sync.GitHubPullRequestSource(pr, None, None),
    )
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

    result = sync.resume_topology_repair(".")

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
    deleted: list[str] = []

    monkeypatch.setattr(sync, "repository_lock", lambda _workspace: nullcontext())
    monkeypatch.setattr(sync, "read_operation", lambda _workspace: ("op", operation))
    monkeypatch.setattr(sync, "_frozen_repository", lambda *a, **k: object())
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

    result = sync.resume_topology_repair(".")

    assert isinstance(result, sync.Stopped)
    assert result.stage == "cleanup"
    assert deleted == []


def test_detached_association_list_and_forget_are_local_only(monkeypatch) -> None:
    repository = sync.GitHubRepositoryId("github.com", "R")
    active = sync.PullRequestId(repository, "PR_active")
    detached = sync.PullRequestId(repository, "PR_detached")
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
    monkeypatch.setattr(
        sync, "_github_graphql", lambda *_args, **_kwargs: pytest.fail("remote call")
    )

    assert sync.list_detached_associations("/work") == (selected,)
    result = sync.forget_detached_association("/work", selected)

    assert result == sync.DetachedAssociationResult(selected, "new")
    assert written[0][1].stacks == (
        sync.TrackedStack(repository, "main", (active,), ()),
    )
    assert written[0][1].last_published_heads == (authority,)


def test_detached_number_resolution_queries_only_bounded_nodes(monkeypatch) -> None:
    first_repo = sync.GitHubRepositoryId("github.com", "R_first")
    second_repo = sync.GitHubRepositoryId("github.example", "R_second")
    first = sync.DetachedAssociation(
        first_repo, "main", sync.PullRequestId(first_repo, "PR_first")
    )
    second = sync.DetachedAssociation(
        second_repo, "trunk", sync.PullRequestId(second_repo, "PR_second")
    )
    calls = []
    monkeypatch.setattr(
        sync, "list_detached_associations", lambda _workspace: (first, second)
    )

    def query(workspace, association, graphql):
        calls.append((workspace, association, graphql))
        number = 12 if association is first else 42
        return {
            "data": {
                "node": {
                    "id": association.pr.node_id,
                    "number": number,
                    "repository": {"id": association.repository.node_id},
                }
            }
        }

    monkeypatch.setattr(sync, "_detached_pr_graphql", query)

    assert sync.resolve_detached_association("/work", 42) is second
    assert [item[1].repository.host for item in calls] == [
        "github.com",
        "github.example",
    ]
    assert isinstance(sync.resolve_detached_association("/work", 99), sync.Blocked)


def test_close_detached_accepts_lost_response_after_closed_readback(
    monkeypatch,
) -> None:
    repository = sync.GitHubRepositoryId("github.example", "R")
    detached = sync.PullRequestId(repository, "PR_detached")
    state = sync.TrackedState(
        (sync.TrackedStack(repository, "main", (), (detached,)),), ()
    )
    selected = sync.DetachedAssociation(repository, "main", detached)
    written = []
    monkeypatch.setattr(sync, "read_state", lambda _workspace: ("old", state))
    monkeypatch.setattr(sync, "read_ref_oid", lambda *_args: None)
    monkeypatch.setattr(sync, "repository_lock", lambda _workspace: nullcontext())
    monkeypatch.setattr(
        sync,
        "_close_detached_pr",
        lambda *_args: (_ for _ in ()).throw(sync.SourceUnavailable("lost")),
    )
    monkeypatch.setattr(
        sync, "_observe_detached_pr_state", lambda *_args: sync.PullRequestState.CLOSED
    )
    monkeypatch.setattr(
        sync,
        "cas_write_state",
        lambda _workspace, expected, value: written.append((expected, value)) or "new",
    )

    sync.close_detached_association("/work", selected)

    assert written == [("old", sync.EMPTY_STATE)]


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

    result = sync.derive_desired(observed, selection(observed, selected))

    assert isinstance(result, sync.Blocked)
    assert result.reasons[0].code == "selected-pr-unavailable"
    assert result.reasons[0].subject == selected.node_id


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
        pr.node_id if state is sync.PullRequestState.CLOSED else "stack"
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
    assert plan.reasons[0].subject == pr.node_id


def test_planner_rejects_valid_multi_member_server_stack() -> None:
    observed, pr = snapshot(desired="2" * 40, live="1" * 40, parent="1" * 40)
    other = sync.PullRequestId(observed.repository, "PR_other")
    observed = dataclasses.replace(
        observed,
        membership=sync.ServerStackMembership(
            pr,
            sync.ServerStackIdentity(observed.repository, "STACK_node", 17),
            "main",
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
            else pr.reported_head_commit_id
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
            reported_head_commit_id=desired[pr.identity].desired_commit_id,
            reported_base_commit_id=heads[f"refs/heads/{pr.base_branch}"],
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

    result = sync.adopt_remote_restack(plan, "/repo")

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


def test_remote_restack_adoption_uses_merged_head_as_old_suffix_base(
    monkeypatch,
) -> None:
    observed, _selection = stacked_snapshot(count=3, merged_prefix=1)
    old_heads = ("2" * 40, "3" * 40)
    new_heads = ("4" * 40, "5" * 40)
    base = "b" * 40
    open_prs = tuple(
        pr for pr in observed.pull_requests if pr.state is sync.PullRequestState.OPEN
    )
    pull_requests = (
        observed.pull_requests[0],
        dataclasses.replace(
            open_prs[0],
            reported_head_commit_id=new_heads[0],
            base_branch="main",
            reported_base_commit_id=base,
        ),
        dataclasses.replace(
            open_prs[1],
            reported_head_commit_id=new_heads[1],
            reported_base_commit_id=new_heads[0],
        ),
    )
    live_refs = tuple(
        dataclasses.replace(
            item,
            commit_id={
                "refs/heads/topic-2": new_heads[0],
                "refs/heads/topic-3": new_heads[1],
            }.get(item.ref.full_name, item.commit_id),
        )
        for item in observed.live_refs
    )
    local = dataclasses.replace(
        observed.local,
        remote_bookmarks=tuple(
            sync.JjRemoteBookmark(
                "origin",
                pr.head_branch,
                sync.CommitTarget(old),
                sync.TrackingState.TRACKED,
            )
            for pr, old in zip(open_prs, old_heads, strict=True)
        ),
    )
    observed = dataclasses.replace(
        observed,
        fetch_url="ssh://git@github.com/o/r.git",
        local=local,
        tool_state=dataclasses.replace(
            observed.tool_state,
            state=dataclasses.replace(
                observed.tool_state.state,
                stacks=(
                    sync.TrackedStack(
                        observed.repository,
                        "main",
                        tuple(pr.identity for pr in open_prs),
                    ),
                ),
            ),
        ),
        pull_requests=pull_requests,
        live_refs=live_refs,
    )
    repository = sync.GitHubRepository(
        observed.repository, "owner/repo", "https://github.com/owner/repo", "main"
    )
    segments: list[tuple[str, str]] = []
    monkeypatch.setattr(
        sync, "observe_github_repository", lambda *_args, **_kwargs: repository
    )
    monkeypatch.setattr(
        sync,
        "_git_refs",
        lambda _git_dir, *names: "ordinary"
        if names == ("refs/heads", "refs/remotes")
        else "",
    )
    monkeypatch.setattr(sync, "_fetch_inspection_refs", lambda *_args: None)
    monkeypatch.setattr(sync, "pin_operation", lambda *_args: local.operation_id)
    monkeypatch.setattr(sync, "_cleanup_inspection_refs", lambda *_args: None)

    def run(command, **_kwargs):
        name = command[-1].rsplit("/", 1)[-1]
        return {
            "main": base,
            "topic-2": new_heads[0],
            "topic-3": new_heads[1],
        }[name]

    def segment(_git_dir, start, end):
        segments.append((start, end))
        return (end,)

    monkeypatch.setattr(sync, "_run", run)
    monkeypatch.setattr(sync, "_linear_segment", segment)
    monkeypatch.setattr(
        sync,
        "_commit_fingerprint",
        lambda _git_dir, commit: (
            "change-b" if commit in {old_heads[0], new_heads[0]} else "change-c",
            "patch-b" if commit in {old_heads[0], new_heads[0]} else "patch-c",
        ),
    )

    result = sync.inspect_remote_restack(observed, "origin")

    assert isinstance(result, sync.RemoteRestackAdoption)
    assert segments == [
        ("1" * 40, old_heads[0]),
        (base, new_heads[0]),
        (old_heads[0], old_heads[1]),
        (new_heads[0], new_heads[1]),
    ]


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

    def observe(_workspace, *, revision, **_kwargs):
        revisions.append(revision)
        return before if len(revisions) == 1 else after

    monkeypatch.setattr(sync, "observe_snapshot", observe)

    assert sync.reobserve_for_adoption("/repo", plan) == before
    assert sync.reobserve_after_adoption("/repo", plan) == after
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


def publication_pr(
    operation: sync.FirstPublication, *, initial: bool
) -> sync.GitHubPullRequest:
    slot = operation.slots[0]
    return sync.GitHubPullRequest(
        sync.PullRequestId(operation.repository, "PR_new"),
        17,
        sync.PullRequestState.OPEN,
        True,
        operation.repository,
        slot.branch,
        slot.commit_id,
        slot.base_branch,
        "0" * 40,
        False,
        False,
        slot.title,
        slot.initial_body if initial else slot.body,
    )


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
    initial_pr = publication_pr(operation, initial=True)
    final_pr = publication_pr(operation, initial=False)
    slot = operation.slots[0]

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
    monkeypatch.setattr(sync, "create_slot_pull_request", lambda *_args: initial_pr)
    monkeypatch.setattr(sync, "update_frozen_pr_metadata", lambda *_args: None)
    monkeypatch.setattr(
        sync,
        "find_slot_pull_requests",
        lambda *_args, **_kwargs: (final_pr,),
    )
    original_delete = sync.cas_delete_operation
    monkeypatch.setattr(
        sync,
        "cas_delete_operation",
        lambda *_args: (_ for _ in ()).throw(sync.Error("injected cleanup failure")),
    )

    sync.start_first_publication(operation, jj_repo)
    interrupted = sync.resume_first_publication(jj_repo)

    assert isinstance(interrupted, sync.Stopped)
    assert interrupted.stage == "fence"
    state_oid, state = sync.read_state(jj_repo)
    operation_oid, persisted = sync.read_first_publication(jj_repo)
    assert state_oid == persisted.final_state_oid
    assert persisted.phase is sync.FirstPublicationPhase.COMMITTING
    assert state.stacks[0].ordered_prs == (initial_pr.identity,)
    assert (
        state.last_published_heads[0].verified_commit_id == operation.slots[0].commit_id
    )

    monkeypatch.setattr(sync, "cas_delete_operation", original_delete)
    completed = sync.resume_first_publication(jj_repo)

    assert completed == sync.FirstPublicationVerified((initial_pr.identity,))
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
    creates: list[str] = []
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
    monkeypatch.setattr(
        sync,
        "create_slot_pull_request",
        lambda *_args: (
            creates.append("attempt")
            or (_ for _ in ()).throw(sync.SourceUnavailable("lost response"))
        ),
    )
    monkeypatch.setattr(sync, "find_slot_pull_requests", lambda *_args, **_kwargs: ())
    sync.cas_write_first_publication(jj_repo, None, operation)

    first = sync.resume_first_publication(jj_repo)
    second = sync.resume_first_publication(jj_repo)

    assert isinstance(first, sync.Stopped) and first.stage == "create-pr"
    assert isinstance(second, sync.Stopped) and second.stage == "create-pr"
    assert creates == ["attempt"]
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
    creates: list[str] = []
    monkeypatch.setattr(
        sync, "create_slot_pull_request", lambda *_args: creates.append("created")
    )
    sync.cas_write_first_publication(jj_repo, None, operation)

    stopped = sync.resume_first_publication(jj_repo)

    assert isinstance(stopped, sync.Stopped)
    assert stopped.stage == "publish"
    assert creates == []


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

    creates: list[str] = []
    monkeypatch.setattr(sync, "observe_publication_slots", observe)
    monkeypatch.setattr(sync, "track_publication_bookmark", track)
    monkeypatch.setattr(
        sync,
        "create_slot_pull_request",
        lambda *_args: (
            creates.append("created")
            or (_ for _ in ()).throw(sync.SourceUnavailable("lost"))
        ),
    )
    monkeypatch.setattr(sync, "find_slot_pull_requests", lambda *_args, **_kwargs: ())
    sync.cas_write_first_publication(jj_repo, None, operation)

    stopped = sync.resume_first_publication(jj_repo)

    assert tracked
    assert creates == ["created"]
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
    monkeypatch.setattr(
        sync,
        "create_slot_pull_request",
        lambda *_args: (_ for _ in ()).throw(
            sync.SourceUnavailable("stop before GitHub")
        ),
    )
    monkeypatch.setattr(sync, "find_slot_pull_requests", lambda *_args, **_kwargs: ())

    sync.start_first_publication(operation, repo)
    stopped = sync.resume_first_publication(repo)

    assert isinstance(stopped, sync.Stopped)
    assert stopped.stage == "create-pr"
    observed = sync.observe_local(repo, revision=commit_id, config_keys=("git.push",))
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
        sync.PullRequestId(operation.repository, f"PR_{index}") for index in (1, 2)
    )
    stack = sync.ServerStackIdentity(operation.repository, "STACK_new", 31)
    operation = dataclasses.replace(
        operation,
        slots=tuple(
            dataclasses.replace(slot, pr_identity=identity, pr_number=index)
            for index, (slot, identity) in enumerate(
                zip(operation.slots, identities, strict=True), start=1
            )
        ),
        stack_phase=sync.StackLinkPhase.VERIFIED,
        resulting_stack=stack,
    )

    assert (
        sync.parse_first_publication(sync.first_publication_to_json(operation))
        == operation
    )
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
            "/repo",
            "op",
            current.effective_config,
            current.workspace_targets,
            tuple(
                sync.LocalBookmark(slot.branch, sync.CommitTarget(slot.commit_id))
                for slot in current.slots
            ),
            (),
            (),
            (),
            "/git",
        )
        return local, tuple(
            sync.PublicationSlotReadback(
                slot.slot_id,
                sync.CommitTarget(slot.commit_id),
                sync.CommitTarget(slot.commit_id)
                if index == 0 or second_ready
                else None,
                sync.TrackingState.TRACKED if index == 0 or second_ready else None,
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

    creates: list[str] = []
    monkeypatch.setattr(sync, "observe_publication_slots", observe)
    monkeypatch.setattr(sync, "push_new_slot_refs", push)
    monkeypatch.setattr(
        sync,
        "create_slot_pull_request",
        lambda *_args: (
            creates.append("create")
            or (_ for _ in ()).throw(sync.SourceUnavailable("stop"))
        ),
    )
    monkeypatch.setattr(sync, "find_slot_pull_requests", lambda *_args, **_kwargs: ())
    sync.cas_write_first_publication(jj_repo, None, operation)

    stopped = sync.resume_first_publication(jj_repo)

    assert pushed == [(operation.slots[1].slot_id,)]
    assert creates == ["create"]
    assert isinstance(stopped, sync.Stopped)
    assert stopped.stage == "create-pr"


@pytest.mark.parametrize(
    "target",
    (sync.FirstPublicationTarget.FRESH_STACK, sync.FirstPublicationTarget.APPEND),
)
def test_stack_mutation_uses_frozen_repository_and_only_new_append_suffix(
    monkeypatch, target: sync.FirstPublicationTarget
) -> None:
    operation = multi_publication_operation()
    if target is sync.FirstPublicationTarget.APPEND:
        repository = operation.repository
        member = sync.ExistingFirstPublicationPR(
            sync.PullRequestId(repository, "PR_existing"),
            7,
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
            existing_stack=sync.ServerStackIdentity(repository, "STACK_existing", 17),
        )
    operation = dataclasses.replace(
        operation,
        slots=tuple(
            dataclasses.replace(
                slot,
                pr_identity=sync.PullRequestId(operation.repository, f"PR_{index}"),
                pr_number=index,
            )
            for index, slot in enumerate(operation.slots, start=1)
        ),
    )
    effects = []
    monkeypatch.setattr(sync, "_prevalidate_operation_repository", lambda *_args: None)
    monkeypatch.setattr(
        sync,
        "send_github_effect",
        lambda effect, **_kwargs: effects.append(effect)
        or sync.GitHubResponse(200, (), b"{}"),
    )

    sync.run_first_publication_stack_link("/repo", operation)

    assert len(effects) == 1
    effect = effects[0]
    assert effect.repository == operation.repository
    assert effect.repository_name == "owner/repo"
    assert effect.pull_request_numbers == (1, 2)
    assert effect.phase is sync.GitHubEffectPhase.POSSIBLY_SENT
    if target is sync.FirstPublicationTarget.APPEND:
        assert effect.kind is sync.StackEffectKind.ADD
        assert effect.stack == operation.existing_stack
        assert effect.expected_before == operation.existing_prs
        assert effect.desired_after == operation.existing_prs + tuple(
            slot.pr_identity for slot in operation.slots
        )
    else:
        assert effect.kind is sync.StackEffectKind.CREATE
        assert effect.stack is None
        assert effect.expected_before == ()


@pytest.mark.parametrize("merged_prefix", (1, 2))
def test_append_preserves_merged_prefix_and_uses_current_integration_anchor(
    monkeypatch, merged_prefix: int
) -> None:
    observed, _selection = stacked_snapshot(count=2, merged_prefix=merged_prefix)
    base = "b" * 40
    predecessor = "2" * 40 if merged_prefix == 1 else base
    new = "9" * 40
    observed = dataclasses.replace(
        observed,
        local=dataclasses.replace(
            observed.local,
            commits=(
                *observed.local.commits,
                sync.ObservedCommit(
                    new, (predecessor,), "new", "New\n\nBody", False, False
                ),
            ),
        ),
    )
    repository = sync.GitHubRepository(
        observed.repository,
        "owner/repo",
        "https://github.com/owner/repo",
        "main",
    )

    monkeypatch.setattr(sync, "resolve_push_url", lambda *_args: observed.push_url)

    def refs(_url, identity, names, *, cwd):
        values = {
            "refs/heads/main": base,
            "refs/heads/topic-2": "2" * 40,
            "refs/heads/new": None,
        }
        return tuple(
            sync.LiveRemoteRef(sync.RemoteBranchRef(identity, name), values[name])
            for name in names
        )

    monkeypatch.setattr(sync, "observe_live_refs", refs)
    operation = sync.prepare_multi_first_publication(
        "/repo",
        observed.local,
        observed.tool_state,
        repository,
        observed.push_url,
        "origin",
        "main",
        (sync.PublicationAssignment(new, "new"),),
        slot_ids=("new-slot",),
        existing_stack=sync.GitHubStackSource(
            observed.membership.stack,
            "main",
            observed.pull_requests,
        ),
    )

    assert isinstance(operation, sync.FirstPublication)
    assert tuple(member.state for member in operation.existing_members) == tuple(
        pr.state for pr in observed.pull_requests
    )
    assert operation.slots[0].base_branch == (
        "topic-2" if merged_prefix == 1 else "main"
    )
    assert (
        sync.parse_first_publication(sync.first_publication_to_json(operation))
        == operation
    )


def test_completed_stack_append_records_only_reopened_active_members() -> None:
    operation = multi_publication_operation(
        slot_phases=(sync.NewPRPhase.VERIFIED, sync.NewPRPhase.VERIFIED),
        operation_phase=sync.FirstPublicationPhase.LINKING_STACK,
    )
    repository = operation.repository
    merged = tuple(
        sync.ExistingFirstPublicationPR(
            sync.PullRequestId(repository, f"PR_merged_{number}"),
            number,
            f"merged-{number}",
            str(number) * 40,
            "main" if number == 1 else "merged-1",
            "0" * 40 if number == 1 else "1" * 40,
            False,
            f"Merged {number}",
            "",
            sync.PullRequestState.MERGED,
        )
        for number in (1, 2)
    )
    new_ids = tuple(
        sync.PullRequestId(repository, f"PR_new_{number}") for number in (3, 4)
    )
    detached = sync.PullRequestId(repository, "PR_detached")
    source = sync.TrackedStack(
        repository, "main", tuple(item.identity for item in merged), (detached,)
    )
    operation = dataclasses.replace(
        operation,
        target=sync.FirstPublicationTarget.APPEND,
        existing_members=merged,
        existing_stack=sync.ServerStackIdentity(repository, "STACK_same", 17),
        slots=tuple(
            dataclasses.replace(slot, pr_identity=identity, pr_number=number)
            for slot, identity, number in zip(
                operation.slots, new_ids, (3, 4), strict=True
            )
        ),
        stack_phase=sync.StackLinkPhase.VERIFIED,
        resulting_stack=sync.ServerStackIdentity(repository, "STACK_same", 17),
    )

    final = sync._build_first_publication_state(
        operation, sync.TrackedState((source,), (), ())
    )

    assert final.stacks == (
        sync.TrackedStack(repository, "main", new_ids, (detached,)),
    )
    assert tuple(item.pr for item in final.last_published_heads) == new_ids

    without_active_record = sync._build_first_publication_state(
        operation, sync.EMPTY_STATE
    )
    assert without_active_record.stacks == (
        sync.TrackedStack(repository, "main", new_ids),
    )


def test_completed_stack_append_accepts_native_link_base_rewrite_everywhere(
    monkeypatch,
) -> None:
    operation = multi_publication_operation(
        slot_phases=(sync.NewPRPhase.VERIFIED, sync.NewPRPhase.VERIFIED),
        operation_phase=sync.FirstPublicationPhase.LINKING_STACK,
    )
    repository = operation.repository
    merged = tuple(
        sync.ExistingFirstPublicationPR(
            sync.PullRequestId(repository, f"PR_merged_{number}"),
            number,
            f"merged-{number}",
            str(number) * 40,
            "main" if number == 1 else "merged-1",
            "0" * 40 if number == 1 else "1" * 40,
            False,
            f"Merged {number}",
            "",
            sync.PullRequestState.MERGED,
        )
        for number in (1, 2)
    )
    slots = tuple(
        dataclasses.replace(
            slot,
            pr_identity=sync.PullRequestId(repository, f"PR_new_{number}"),
            pr_number=number,
        )
        for slot, number in zip(operation.slots, (3, 4), strict=True)
    )
    operation = dataclasses.replace(
        operation,
        target=sync.FirstPublicationTarget.APPEND,
        existing_members=merged,
        existing_stack=sync.ServerStackIdentity(repository, "STACK_same", 17),
        slots=slots,
    )
    linked = tuple(
        sync.GitHubPullRequest(
            item.identity,
            item.number,
            item.state,
            item.draft,
            repository,
            item.head_branch,
            item.head_commit_id,
            item.base_branch,
            item.base_commit_id,
            False,
            False,
            item.title,
            item.body,
        )
        for item in merged
    ) + tuple(
        sync.GitHubPullRequest(
            slot.pr_identity,
            slot.pr_number,
            sync.PullRequestState.OPEN,
            True,
            repository,
            slot.branch,
            slot.commit_id,
            merged[-1].head_branch if index == 0 else slots[index - 1].branch,
            merged[-1].head_commit_id if index == 0 else slots[index - 1].commit_id,
            False,
            False,
            slot.title,
            slot.body,
        )
        for index, slot in enumerate(slots)
    )
    monkeypatch.setattr(
        sync,
        "observe_github_stack",
        lambda *_args, **_kwargs: sync.GitHubStackSource(
            operation.existing_stack, "main", linked
        ),
    )

    result = sync.observe_first_publication_stack("/repo", operation)

    assert result is not None
    assert tuple(pr.identity for pr in result.ordered_prs) == tuple(
        item.identity for item in merged
    ) + tuple(slot.pr_identity for slot in slots)

    verified = dataclasses.replace(
        operation,
        stack_phase=sync.StackLinkPhase.VERIFIED,
        resulting_stack=operation.existing_stack,
    )
    monkeypatch.setattr(
        sync,
        "observe_publication_slots",
        lambda *_args: (
            verified.effective_config,
            tuple(
                sync.PublicationSlotReadback(
                    slot.slot_id,
                    sync.CommitTarget(slot.commit_id),
                    sync.CommitTarget(slot.commit_id),
                    sync.TrackingState.TRACKED,
                    slot.commit_id,
                )
                for slot in slots
            ),
        ),
    )
    by_identity = {pr.identity: pr for pr in linked}
    monkeypatch.setattr(
        sync,
        "find_slot_pull_requests",
        lambda _workspace, _operation, slot, **_kwargs: (
            by_identity[slot.pr_identity],
        ),
    )

    assert sync._verify_first_publication_external(verified, "/repo") is None


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
            reported_head_commit_id=old_heads[index - merged_prefix],
            reported_base_commit_id=(
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

    result = sync.apply(plan, "/unused")

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

    result = sync.apply(plan, "/unused")

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

    result = sync.apply(plan, "/unused")

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
    updates: list[sync.PRMetadataUpdate] = []
    monkeypatch.setattr(sync, "repository_lock", lambda *_args: nullcontext())
    monkeypatch.setattr(sync, "reobserve_for_apply", lambda *_args: next(observations))
    monkeypatch.setattr(
        sync,
        "update_frozen_pr_metadata",
        lambda _workspace, update: updates.append(update),
    )
    monkeypatch.setattr(
        sync,
        "record_verified_state",
        lambda *_args: pytest.fail("metadata-only apply attempted a state write"),
    )

    result = sync.apply(plan, "/unused")

    assert result == sync.Verified(False, True, False)
    assert updates == list(plan.metadata_updates)


def test_apply_revalidation_ignores_operation_and_git_push_but_detects_drift() -> None:
    observed, plan = standalone_plan(desired="2" * 40, live="1" * 40, parent="1" * 40)
    unrelated = dataclasses.replace(
        observed,
        local=dataclasses.replace(
            observed.local,
            operation_id="new-jj-operation",
            effective_config=tuple(
                item
                for item in observed.local.effective_config
                if item[0] != "git.push"
            )
            + (("git.push", "later-default"),),
        ),
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

    result = sync.apply(plan, "/unused")

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

    assert sync.apply(plan, "/unused") == sync.Verified(False, False, False)


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
    updates: list[sync.PRMetadataUpdate] = []
    monkeypatch.setattr(sync, "repository_lock", lambda *_args: nullcontext())
    monkeypatch.setattr(sync, "reobserve_for_apply", lambda *_args: next(observations))
    monkeypatch.setattr(
        sync,
        "update_frozen_pr_metadata",
        lambda _workspace, update: updates.append(update),
    )
    monkeypatch.setattr(
        sync,
        "record_verified_state",
        lambda *_args: pytest.fail("metadata-only apply created a receipt"),
    )

    result = sync.apply(plan, "/unused")

    assert result == sync.Verified(False, True, False)
    assert updates == list(plan.metadata_updates)


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
                superseded.pull_requests[0], reported_head_commit_id="3" * 40
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
    monkeypatch.setattr(
        sync,
        "update_frozen_pr_metadata",
        lambda *_args: pytest.fail("metadata changed after head was superseded"),
    )

    result = sync.apply(plan, "/unused")

    assert isinstance(result, sync.Stopped)
    assert result.stage == "pre-metadata-verify"


def test_apply_lock_contention_raises_lock_busy(jj_repo: Path, monkeypatch) -> None:
    _observed, plan = standalone_plan(desired="1" * 40, live="1" * 40, parent="0" * 40)
    monkeypatch.setattr(
        sync,
        "reobserve_for_apply",
        lambda *_args: pytest.fail("contended apply performed observation"),
    )

    with sync.repository_lock(jj_repo):
        with pytest.raises(sync.LockBusy):
            sync.apply(plan, jj_repo)


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

    result = sync.apply(plan, "/unused")

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

    result = sync.apply(plan, "/unused")

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
    pr = sync.PullRequestId(repository, "PR_node")
    other = sync.PullRequestId(repository, "PR_other")
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
        sync.PullRequestId(repository, f"PR_{head}_{base}_{fork}"),
        7,
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
    monkeypatch,
) -> None:
    repository = sync.GitHubRepository(
        sync.GitHubRepositoryId("github.com", "R_repo"),
        "owner/repo",
        "https://github.com/owner/repo",
        "main",
    )
    branches = ("new-one", "new-two")
    requests = []

    def response(host, query, variables, *, cwd):
        requests.append((host, query, variables))
        return {
            "data": {
                "repository": {
                    "id": "R_repo",
                    "head0": {
                        "nodes": [
                            pr_source_record(
                                id="PR_head", number=7, headRefName="new-one"
                            )
                        ],
                        "pageInfo": {"hasNextPage": False},
                    },
                    "base0": {"nodes": [], "pageInfo": {"hasNextPage": False}},
                    "head1": {"nodes": [], "pageInfo": {"hasNextPage": False}},
                    "base1": {
                        "nodes": [
                            pr_source_record(
                                id="PR_base", number=8, baseRefName="new-two"
                            )
                        ],
                        "pageInfo": {"hasNextPage": False},
                    },
                }
            }
        }

    monkeypatch.setattr(sync, "_github_graphql", response)

    observed = sync.observe_historical_pull_request_collisions(repository, branches)

    assert len(requests) == 1
    assert requests[0][0] == "github.com"
    assert requests[0][2] == {
        "owner": "owner",
        "name": "repo",
        "branch0": "new-one",
        "branch1": "new-two",
    }
    assert tuple(item.number for item in observed) == (8, 7)


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


def test_standalone_membership_resolution_orders_source_chain_and_new_boundary() -> (
    None
):
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
        sync.PullRequestId(repository, "PR_1"),
        1,
        sync.PullRequestState.OPEN,
        False,
        repository,
        "topic-1",
        first_id,
        "dev",
        "0" * 40,
        False,
        False,
        "One",
        "",
    )
    last_pr = sync.GitHubPullRequest(
        sync.PullRequestId(repository, "PR_2"),
        2,
        sync.PullRequestState.OPEN,
        False,
        repository,
        "topic-2",
        rewritten_id,
        "topic-1",
        first_id,
        False,
        False,
        "Two",
        "",
    )
    first = sync.GitHubPullRequestSource(first_pr, None, None)
    last = sync.GitHubPullRequestSource(last_pr, None, None)

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
    monkeypatch,
) -> None:
    repository = sync.GitHubRepository(
        sync.GitHubRepositoryId("github.com", "R_repo"),
        "owner/repo",
        "https://github.com/owner/repo",
        "main",
    )
    branches = ("topic-4", "topic-1", "topic-2", "topic-3")
    requests = []

    def response(host, query, variables, *, cwd):
        requests.append((host, query, variables))
        return {
            "data": {
                "repository": {
                    "id": "R_repo",
                    **{
                        f"candidate{index}": {
                            "nodes": [
                                pr_source_record(
                                    id=f"PR_{index}",
                                    number=index + 1,
                                    headRefName=branch,
                                )
                            ],
                            "pageInfo": {"hasNextPage": False},
                        }
                        for index, branch in enumerate(branches)
                    },
                }
            }
        }

    monkeypatch.setattr(sync, "_github_graphql", response)

    observed = sync.observe_open_pull_request_candidates(repository, branches)

    assert len(requests) == 1
    assert requests[0][0] == "github.com"
    assert requests[0][2] == {
        "owner": "owner",
        "name": "repo",
        **{f"head{index}": branch for index, branch in enumerate(branches)},
    }
    assert tuple(item.pr.head_branch for item in observed) == branches


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

    assert sync.resolve_first_publication_assignments(
        "/repo",
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
