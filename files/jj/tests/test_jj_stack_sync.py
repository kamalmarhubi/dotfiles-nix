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


def test_json_lines_preserves_unicode_line_and_paragraph_separators() -> None:
    records = (
        {"description": "before\u2028middle\u2029after"},
        {"description": "next"},
    )
    output = "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n"

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
