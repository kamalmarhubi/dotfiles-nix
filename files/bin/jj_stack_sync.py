"""Read-only observation and pure planning foundation for ``jj stack sync``.

This module intentionally has no command-line entry point and no remote mutation.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from enum import Enum, StrEnum
from pathlib import Path

from markdown_it import MarkdownIt
from markdown_it.rules_inline.newline import newline as _parse_newline
from markdown_it.rules_inline.state_inline import StateInline


STATE_REF = "refs/jj-stack/state"
OPERATION_REF = "refs/jj-stack/operation"


class Error(Exception):
    pass


class ConcurrentUpdate(Error):
    pass


class LockBusy(Error):
    pass


class RemoteResolutionError(Error):
    pass


@dataclass(frozen=True)
class GitHubRepositoryId:
    """GitHub host plus the repository's immutable GraphQL node ID."""

    host: str
    node_id: str


@dataclass(frozen=True)
class PullRequestId:
    repository: GitHubRepositoryId
    node_id: str


@dataclass(frozen=True)
class RemoteBranchRef:
    """A fully qualified branch in one logical GitHub repository."""

    repository: GitHubRepositoryId
    full_name: str

    def __post_init__(self) -> None:
        if (
            not self.full_name.startswith("refs/heads/")
            or self.full_name == "refs/heads/"
        ):
            raise ValueError("remote branch ref must use the refs/heads namespace")


@dataclass(frozen=True)
class AbsentBookmarkTarget:
    """jj returned a bookmark/ref record whose target is absent."""

    pass


@dataclass(frozen=True)
class CommitTarget:
    """The bookmark resolves to exactly one commit."""

    commit_id: str


@dataclass(frozen=True)
class BookmarkConflict:
    """The removed and added terms of an unresolved jj ref conflict."""

    removed_commit_ids: tuple[str, ...]
    added_commit_ids: tuple[str, ...]


BookmarkTarget = AbsentBookmarkTarget | CommitTarget | BookmarkConflict


class TrackingState(StrEnum):
    TRACKED = "tracked"
    UNTRACKED = "untracked"


@dataclass(frozen=True)
class LocalBookmark:
    name: str
    target: BookmarkTarget


@dataclass(frozen=True)
class JjRemoteBookmark:
    remote: str
    name: str
    target: BookmarkTarget
    tracking_state: TrackingState


@dataclass(frozen=True)
class GitRemoteTrackingRef:
    """A local Git refs/remotes entry; never publication authority."""

    full_name: str
    commit_id: str


@dataclass(frozen=True)
class LiveRemoteRef:
    """Authoritative destination read; None means confirmed absence."""

    ref: RemoteBranchRef
    commit_id: str | None


@dataclass(frozen=True)
class ObservedCommit:
    commit_id: str
    parent_commit_ids: tuple[str, ...]
    change_id: str
    description: str
    has_conflicts: bool
    is_hidden: bool


class PullRequestState(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    MERGED = "MERGED"


@dataclass(frozen=True)
class GitHubPullRequest:
    """Validated GitHub fields used by planning, not a raw API response."""

    identity: PullRequestId
    number: int
    state: PullRequestState
    draft: bool
    head_repository: GitHubRepositoryId
    head_branch: str
    reported_head_commit_id: str
    base_branch: str
    reported_base_commit_id: str
    auto_merge_enabled: bool
    in_merge_queue: bool
    title: str
    body: str


@dataclass(frozen=True)
class StandalonePullRequest:
    pr: PullRequestId


@dataclass(frozen=True)
class ServerStackMembership:
    """Complete ordered membership for the stack containing selected_pr."""

    selected_pr: PullRequestId
    server_stack_id: str
    ordered_prs: tuple[PullRequestId, ...]

    def __post_init__(self) -> None:
        if not self.server_stack_id or not self.ordered_prs:
            raise ValueError("server stack identity and membership must be nonempty")
        if self.selected_pr not in self.ordered_prs:
            raise ValueError("selected PR must belong to the observed server stack")
        if len(set(self.ordered_prs)) != len(self.ordered_prs):
            raise ValueError("server stack membership must not contain duplicates")
        if any(pr.repository != self.selected_pr.repository for pr in self.ordered_prs):
            raise ValueError("all server stack members must belong to one repository")


PullRequestMembership = StandalonePullRequest | ServerStackMembership


@dataclass(frozen=True)
class LocalObservation:
    workspace: str
    operation_id: str
    effective_config: tuple[tuple[str, str], ...]
    workspace_targets: tuple[tuple[str, str], ...]
    local_bookmarks: tuple[LocalBookmark, ...]
    remote_bookmarks: tuple[JjRemoteBookmark, ...]
    git_remote_tracking_refs: tuple[GitRemoteTrackingRef, ...]
    commits: tuple[ObservedCommit, ...]
    git_common_dir: str
    git_remotes: tuple[str, ...] = ()


def resolve_remote(local: LocalObservation, explicit: str | None = None) -> str:
    """Choose one publication remote using native jj precedence."""
    configured = dict(local.effective_config).get("git.push")
    selected = explicit if explicit is not None else configured
    if selected is not None:
        if not selected or selected not in local.git_remotes:
            raise RemoteResolutionError(
                f"selected Git remote does not exist: {selected or '<empty>'}"
            )
        return selected
    if not local.git_remotes:
        raise RemoteResolutionError("no Git remote is configured")
    if len(local.git_remotes) == 1:
        return local.git_remotes[0]
    if "origin" in local.git_remotes:
        return "origin"
    raise RemoteResolutionError("multiple Git remotes are configured without origin")


def operation_config(local: LocalObservation) -> tuple[tuple[str, str], ...]:
    """Exclude remote-selection config once the destination has been frozen."""
    return tuple(item for item in local.effective_config if item[0] != "git.push")


@dataclass(frozen=True)
class LastPublishedHead:
    """Last commit this tool published and verified for one exact branch."""

    pr: PullRequestId
    ref: RemoteBranchRef
    verified_commit_id: str


@dataclass(frozen=True)
class TrackedStack:
    repository: GitHubRepositoryId
    base_branch: str
    ordered_prs: tuple[PullRequestId, ...]


@dataclass(frozen=True)
class TrackedState:
    stacks: tuple[TrackedStack, ...]
    last_published_heads: tuple[LastPublishedHead, ...]


EMPTY_STATE = TrackedState((), ())


@dataclass(frozen=True)
class ToolStateRead:
    """Decoded tool state bound to the exact local blob ref observations."""

    state_blob_oid: str | None
    state: TrackedState
    operation_blob_oid: str | None


@dataclass(frozen=True)
class Snapshot:
    """Complete validated inputs for one planning scope."""

    repository: GitHubRepositoryId
    push_url: str
    local: LocalObservation
    tool_state: ToolStateRead
    pull_requests: tuple[GitHubPullRequest, ...]
    membership: PullRequestMembership
    live_refs: tuple[LiveRemoteRef, ...]


@dataclass(frozen=True)
class DesiredExistingPR:
    pr_identity: PullRequestId
    desired_commit_id: str
    title: str
    body: str


@dataclass(frozen=True)
class DesiredStack:
    repository: GitHubRepositoryId
    base_branch: str
    active: tuple[DesiredExistingPR, ...]


@dataclass(frozen=True)
class Blocker:
    code: str
    subject: str
    detail: str


@dataclass(frozen=True)
class Blocked:
    reasons: tuple[Blocker, ...]


@dataclass(frozen=True)
class Dependencies:
    effective_config: tuple[tuple[str, str], ...]
    push_url: str
    state_blob_oid: str | None
    operation_blob_oid: str | None
    pr: GitHubPullRequest
    membership: PullRequestMembership
    bookmark: LocalBookmark
    live_head: LiveRemoteRef
    live_base: LiveRemoteRef


@dataclass(frozen=True)
class FastForward:
    pass


@dataclass(frozen=True)
class MatchesLastPublication:
    publication: LastPublishedHead


Authority = FastForward | MatchesLastPublication


@dataclass(frozen=True)
class PlannedHeadUpdate:
    ref: RemoteBranchRef
    expected_old_commit_id: str
    new_commit_id: str
    authority: Authority


@dataclass(frozen=True)
class PRMetadataUpdate:
    pr_identity: PullRequestId
    title: str
    body: str


@dataclass(frozen=True)
class NoOp:
    desired: DesiredStack
    dependencies: Dependencies


@dataclass(frozen=True)
class Apply:
    desired: DesiredStack
    head_updates: tuple[PlannedHeadUpdate, ...]
    metadata_updates: tuple[PRMetadataUpdate, ...]
    dependencies: Dependencies


SyncPlan = Blocked | NoOp | Apply


def _run(
    args: Sequence[str], *, cwd: str | Path | None = None, stdin: str | None = None
) -> str:
    result = subprocess.run(
        args, cwd=cwd, input=stdin, text=True, capture_output=True, check=False
    )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise Error(f"`{' '.join(args)}` failed" + (f": {detail}" if detail else ""))
    return result.stdout


def _jj(workspace: str | Path, operation: str | None, *args: str) -> str:
    command = ["jj", "--color=never", "--repository", os.fspath(workspace)]
    if operation is not None:
        command.append(f"--at-op={operation}")
    command.append("--ignore-working-copy")
    command.extend(args)
    return _run(command)


def pin_operation(workspace: str | Path) -> str:
    """Capture the current operation without snapshotting or reconciling the WC."""
    rows = _jj(
        workspace,
        "@",
        "op",
        "log",
        "--no-graph",
        "--limit",
        "1",
        "-T",
        'self.id() ++ "\\n"',
    ).splitlines()
    if len(rows) != 1 or not rows[0]:
        raise Error("could not capture exactly one jj operation")
    return rows[0]


def _json_lines(output: str, source: str) -> tuple[dict[str, object], ...]:
    try:
        values = tuple(json.loads(line) for line in output.split("\n") if line)
    except json.JSONDecodeError as exc:
        raise Error(f"invalid structured output from {source}") from exc
    if any(not isinstance(value, dict) for value in values):
        raise Error(f"invalid structured output from {source}")
    return values  # type: ignore[return-value]


def git_common_dir(workspace: str | Path, operation: str | None = None) -> Path:
    operation = operation or pin_operation(workspace)
    git_root = Path(_jj(workspace, operation, "git", "root").strip())
    value = _run(
        [
            "git",
            f"--git-dir={git_root}",
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        ]
    ).strip()
    return Path(value).resolve()


def read_ref_oid(
    workspace: str | Path, ref: str, operation: str | None = None
) -> str | None:
    common = git_common_dir(workspace, operation)
    result = subprocess.run(
        ["git", f"--git-dir={common}", "rev-parse", "--verify", "--quiet", ref],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode == 1:
        return None
    if result.returncode:
        raise Error(f"could not read private ref {ref}: {result.stderr.strip()}")
    return result.stdout.strip()


def _bookmark_record(
    record: dict[str, object],
) -> tuple[str, str, BookmarkTarget, TrackingState]:
    if set(record) != {
        "name",
        "remote",
        "conflict",
        "target",
        "removed",
        "added",
        "tracked",
    }:
        raise Error("invalid structured output from jj bookmark list")
    name, remote = record["name"], record["remote"]
    conflict, commit_id = record["conflict"], record["target"]
    removed, added, tracked = record["removed"], record["added"], record["tracked"]
    if (
        not isinstance(name, str)
        or not name
        or not isinstance(remote, str)
        or type(conflict) is not bool
        or not isinstance(commit_id, str)
        or not isinstance(removed, list)
        or not isinstance(added, list)
        or any(not isinstance(value, str) or not value for value in (*removed, *added))
        or type(tracked) is not bool
    ):
        raise Error("invalid structured output from jj bookmark list")
    if conflict:
        if commit_id or not removed and not added:
            raise Error("invalid structured output from jj bookmark list")
        target: BookmarkTarget = BookmarkConflict(tuple(removed), tuple(added))
    else:
        if removed or added != ([commit_id] if commit_id else []):
            raise Error("invalid structured output from jj bookmark list")
        target = CommitTarget(commit_id) if commit_id else AbsentBookmarkTarget()
    return (
        name,
        remote,
        target,
        TrackingState.TRACKED if tracked else TrackingState.UNTRACKED,
    )


def _commit_record(record: dict[str, object]) -> ObservedCommit:
    if set(record) != {
        "commit_id",
        "parent_commit_ids",
        "change",
        "description",
        "conflicts",
        "hidden",
    }:
        raise Error("invalid structured output from jj log")
    commit_id = record["commit_id"]
    parent_commit_ids = record["parent_commit_ids"]
    change_id = record["change"]
    description = record["description"]
    conflicts = record["conflicts"]
    hidden = record["hidden"]
    if (
        not isinstance(commit_id, str)
        or not commit_id
        or not isinstance(parent_commit_ids, list)
        or any(not isinstance(value, str) or not value for value in parent_commit_ids)
        or not isinstance(change_id, str)
        or not change_id
        or not isinstance(description, str)
        or type(conflicts) is not bool
        or type(hidden) is not bool
    ):
        raise Error("invalid structured output from jj log")
    return ObservedCommit(
        commit_id,
        tuple(parent_commit_ids),
        change_id,
        description,
        conflicts,
        hidden,
    )


def observe_local(
    workspace: str | Path,
    *,
    revision: str,
    config_keys: Sequence[str],
) -> LocalObservation:
    """Observe local jj/Git state at one pinned operation without mutating it."""
    workspace = Path(workspace).resolve()
    operation = pin_operation(workspace)
    bookmark_template = (
        "'{\"name\":' ++ name.escape_json()"
        " ++ ',\"remote\":' ++ if(remote, remote.escape_json(), '\"\"')"
        " ++ ',\"conflict\":' ++ conflict"
        " ++ ',\"target\":' ++ if(normal_target, stringify(normal_target.commit_id()).escape_json(), '\"\"')"
        " ++ ',\"removed\":' ++ json(removed_targets.map(|c| c.commit_id()))"
        " ++ ',\"added\":' ++ json(added_targets.map(|c| c.commit_id()))"
        " ++ ',\"tracked\":' ++ tracking_present ++ '}' ++ \"\\n\""
    )
    records = _json_lines(
        _jj(workspace, operation, "bookmark", "list", "--all", "-T", bookmark_template),
        "jj bookmark list",
    )
    locals_: list[LocalBookmark] = []
    remotes: list[JjRemoteBookmark] = []
    for record in records:
        name, remote, target, tracking_state = _bookmark_record(record)
        if remote:
            remotes.append(JjRemoteBookmark(remote, name, target, tracking_state))
        else:
            locals_.append(LocalBookmark(name, target))
    if len({item.name for item in locals_}) != len(locals_) or len(
        {(item.remote, item.name) for item in remotes}
    ) != len(remotes):
        raise Error("duplicate bookmark in structured output from jj bookmark list")

    commit_template = (
        "'{\"commit_id\":' ++ stringify(commit_id).escape_json()"
        " ++ ',\"parent_commit_ids\":' ++ json(parents.map(|p| p.commit_id()))"
        " ++ ',\"change\":' ++ stringify(change_id).escape_json()"
        " ++ ',\"description\":' ++ description.escape_json()"
        " ++ ',\"conflicts\":' ++ conflict"
        " ++ ',\"hidden\":' ++ hidden ++ '}' ++ \"\\n\""
    )
    commit_records = _json_lines(
        _jj(
            workspace,
            operation,
            "log",
            "--no-graph",
            "-r",
            revision,
            "-T",
            commit_template,
        ),
        "jj log",
    )
    git_root = Path(_jj(workspace, operation, "git", "root").strip())
    commits = tuple(_commit_record(row) for row in commit_records)
    if len({commit.commit_id for commit in commits}) != len(commits):
        raise Error("duplicate commit in structured output from jj log")
    workspace_rows = _jj(
        workspace,
        operation,
        "workspace",
        "list",
        "-T",
        'name ++ "\\t" ++ target.commit_id() ++ "\\n"',
    ).splitlines()
    workspace_targets: list[tuple[str, str]] = []
    for row in workspace_rows:
        fields = row.split("\t")
        if len(fields) != 2 or not all(fields):
            raise Error("invalid structured output from jj workspace list")
        workspace_targets.append((fields[0], fields[1]))
    if len({name for name, _target in workspace_targets}) != len(workspace_targets):
        raise Error("duplicate workspace in structured output from jj workspace list")
    config: list[tuple[str, str]] = []
    for key in config_keys:
        # Configuration is intentionally captured separately from the pinned op.
        try:
            value = _jj(workspace, None, "config", "get", key).rstrip("\n")
        except Error as exc:
            if key == "git.push" and "Value not found for git.push" in str(exc):
                continue
            raise
        config.append((key, value))

    remote_rows = _jj(workspace, operation, "git", "remote", "list").splitlines()
    git_remotes: list[str] = []
    for row in remote_rows:
        fields = row.split(maxsplit=1)
        if len(fields) != 2 or not all(fields):
            raise Error("invalid structured output from jj git remote list")
        git_remotes.append(fields[0])
    if len(set(git_remotes)) != len(git_remotes):
        raise Error("duplicate remote in structured output from jj git remote list")

    raw = _run(
        [
            "git",
            f"--git-dir={git_root}",
            "for-each-ref",
            "--format=%(refname)%09%(objectname)",
            "refs/remotes",
        ]
    ).splitlines()
    tracking: list[GitRemoteTrackingRef] = []
    for row in raw:
        fields = row.split("\t")
        if len(fields) != 2 or not all(fields):
            raise Error("invalid structured output from git for-each-ref")
        tracking.append(GitRemoteTrackingRef(fields[0], fields[1]))
    if len({item.full_name for item in tracking}) != len(tracking):
        raise Error("duplicate ref in structured output from git for-each-ref")
    common = git_common_dir(workspace, operation)
    return LocalObservation(
        os.fspath(workspace),
        operation,
        tuple(config),
        tuple(workspace_targets),
        tuple(sorted(locals_, key=lambda value: value.name)),
        tuple(sorted(remotes, key=lambda value: (value.remote, value.name))),
        tuple(tracking),
        commits,
        os.fspath(common),
        tuple(sorted(git_remotes)),
    )


def state_to_json(state: TrackedState) -> str:
    _validate_state(state)
    return json.dumps(asdict(state), sort_keys=True, separators=(",", ":")) + "\n"


def parse_state(data: str) -> TrackedState:
    def record(value: object, context: str) -> dict[str, object]:
        if not isinstance(value, dict):
            raise ValueError(f"{context} must be an object")
        return value

    def fields(
        value: dict[str, object], expected: set[str], context: str
    ) -> dict[str, object]:
        if set(value) != expected:
            raise ValueError(f"{context} has unexpected fields")
        return value

    def array(value: object, context: str) -> list[object]:
        if not isinstance(value, list):
            raise ValueError(f"{context} must be an array")
        return value

    def text(value: object, context: str) -> str:
        if not isinstance(value, str) or not value:
            raise ValueError(f"{context} must be a nonempty string")
        return value

    def repository(value: object, context: str) -> GitHubRepositoryId:
        raw = fields(record(value, context), {"host", "node_id"}, context)
        return GitHubRepositoryId(
            text(raw.get("host"), f"{context}.host"),
            text(raw.get("node_id"), f"{context}.node_id"),
        )

    def pull_request(value: object, context: str) -> PullRequestId:
        raw = fields(record(value, context), {"repository", "node_id"}, context)
        return PullRequestId(
            repository(raw.get("repository"), f"{context}.repository"),
            text(raw.get("node_id"), f"{context}.node_id"),
        )

    def tracked_stack(value: object, index: int) -> TrackedStack:
        context = f"stacks[{index}]"
        raw = fields(
            record(value, context),
            {"repository", "base_branch", "ordered_prs"},
            context,
        )
        return TrackedStack(
            repository(raw.get("repository"), f"{context}.repository"),
            text(raw.get("base_branch"), f"{context}.base_branch"),
            tuple(
                pull_request(pr, f"{context}.ordered_prs[{pr_index}]")
                for pr_index, pr in enumerate(
                    array(raw.get("ordered_prs"), f"{context}.ordered_prs")
                )
            ),
        )

    def last_published_head(value: object, index: int) -> LastPublishedHead:
        context = f"last_published_heads[{index}]"
        raw = fields(
            record(value, context),
            {"pr", "ref", "verified_commit_id"},
            context,
        )
        ref_context = f"{context}.ref"
        ref = fields(
            record(raw.get("ref"), ref_context),
            {"repository", "full_name"},
            ref_context,
        )
        return LastPublishedHead(
            pull_request(raw.get("pr"), f"{context}.pr"),
            RemoteBranchRef(
                repository(ref.get("repository"), f"{ref_context}.repository"),
                text(ref.get("full_name"), f"{ref_context}.full_name"),
            ),
            text(raw.get("verified_commit_id"), f"{context}.verified_commit_id"),
        )

    try:
        raw = fields(
            record(json.loads(data), "root"),
            {"stacks", "last_published_heads"},
            "root",
        )
        stacks = tuple(
            tracked_stack(value, index)
            for index, value in enumerate(array(raw.get("stacks"), "stacks"))
        )
        publications = tuple(
            last_published_head(value, index)
            for index, value in enumerate(
                array(raw.get("last_published_heads"), "last_published_heads")
            )
        )
        state = TrackedState(stacks, publications)
        _validate_state(state)
        return state
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise Error(f"invalid refs/jj-stack/state payload: {exc}") from exc


def _validate_state(state: TrackedState) -> None:
    def require_text(*values: object) -> None:
        if any(not isinstance(value, str) or not value for value in values):
            raise ValueError("state identity fields must be nonempty strings")

    publication_keys: set[tuple[GitHubRepositoryId, PullRequestId, str]] = set()
    memberships: set[tuple[GitHubRepositoryId, PullRequestId]] = set()
    for stack in state.stacks:
        require_text(
            stack.base_branch,
            stack.repository.host,
            stack.repository.node_id,
        )
        if not stack.ordered_prs:
            raise ValueError("invalid tracked stack")
        for pr in stack.ordered_prs:
            require_text(pr.repository.host, pr.repository.node_id, pr.node_id)
            if pr.repository != stack.repository:
                raise ValueError("stack contains a PR from another repository")
            membership = (stack.repository, pr)
            if membership in memberships:
                raise ValueError("overlapping tracked stack membership")
            memberships.add(membership)
    for publication in state.last_published_heads:
        require_text(
            publication.pr.repository.host,
            publication.pr.repository.node_id,
            publication.pr.node_id,
            publication.ref.repository.host,
            publication.ref.repository.node_id,
            publication.ref.full_name,
            publication.verified_commit_id,
        )
        if (
            publication.pr.repository != publication.ref.repository
            or not publication.ref.full_name.startswith("refs/heads/")
        ):
            raise ValueError("invalid last-published head")
        key = (publication.ref.repository, publication.pr, publication.ref.full_name)
        if key in publication_keys:
            raise ValueError("ambiguous last-published head")
        publication_keys.add(key)


def read_state(workspace: str | Path) -> tuple[str | None, TrackedState]:
    observed_oid = read_ref_oid(workspace, STATE_REF)
    if observed_oid is None:
        return None, EMPTY_STATE
    common = git_common_dir(workspace)
    payload = _run(["git", f"--git-dir={common}", "cat-file", "blob", observed_oid])
    return observed_oid, parse_state(payload)


def observe_tool_state(workspace: str | Path) -> ToolStateRead:
    state_oid, state = read_state(workspace)
    return ToolStateRead(
        state_oid,
        state,
        read_ref_oid(workspace, OPERATION_REF),
    )


def cas_write_state(
    workspace: str | Path, expected_oid: str | None, state: TrackedState
) -> str:
    common = git_common_dir(workspace)
    new_oid = _run(
        ["git", f"--git-dir={common}", "hash-object", "-w", "--stdin"],
        stdin=state_to_json(state),
    ).strip()
    expected = expected_oid or ("0" * len(new_oid))
    result = subprocess.run(
        ["git", f"--git-dir={common}", "update-ref", STATE_REF, new_oid, expected],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        if read_ref_oid(workspace, STATE_REF) != expected_oid:
            raise ConcurrentUpdate("state ref changed during compare-and-swap")
        detail = result.stderr.strip() or result.stdout.strip()
        raise Error("could not update state ref" + (f": {detail}" if detail else ""))
    return new_oid


@contextmanager
def repository_lock(workspace: str | Path) -> Iterator[None]:
    path = git_common_dir(workspace) / "jj-stack.lock"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LockBusy(
                "another jj-stack process holds the repository lock"
            ) from exc
        yield
    finally:
        os.close(descriptor)


def _block(code: str, subject: str, detail: str) -> Blocked:
    return Blocked((Blocker(code, subject, detail),))


def _record_newline(state: StateInline, silent: bool) -> bool:
    offset = state.pos
    token_count = len(state.tokens)
    matched = _parse_newline(state, silent)
    if (
        matched
        and not silent
        and len(state.tokens) > token_count
        and state.tokens[-1].type == "softbreak"
    ):
        state.tokens[-1].meta["inline_offset"] = offset
    return matched


_MARKDOWN = MarkdownIt("commonmark").enable("table")
_MARKDOWN.inline.ruler.at("newline", _record_newline)


def _reflow_markdown(body: str) -> str:
    tokens = _MARKDOWN.parse(body)
    line_endings = list(re.finditer(r"\r\n|\r|\n", body))
    edits: list[tuple[int, int]] = []
    for index, token in enumerate(tokens):
        if token.type != "paragraph_open" or token.level != 0:
            continue
        inline = tokens[index + 1]
        if inline.type != "inline" or inline.map is None:
            raise Error("markdown parser returned an unexpected paragraph structure")
        for child in inline.children or []:
            if child.type != "softbreak":
                continue
            offset = child.meta.get("inline_offset")
            if not isinstance(offset, int):
                raise Error("markdown parser returned a soft break without an offset")
            line = inline.map[0] + inline.content.count("\n", 0, offset)
            if line >= len(line_endings):
                raise Error("markdown parser returned an invalid soft-break offset")
            edits.append(line_endings[line].span())
    for start, end in reversed(edits):
        body = body[:start] + " " + body[end:]
    return body


def _metadata(description: str) -> tuple[str, str] | None:
    description = description.rstrip("\n")
    title, separator, body = description.partition("\n")
    if not title:
        return None
    if separator:
        body = _reflow_markdown(body.removeprefix("\n"))
    return title, body


def derive_desired(
    snapshot: Snapshot, selected_pr: PullRequestId
) -> DesiredStack | Blocked:
    matches = tuple(pr for pr in snapshot.pull_requests if pr.identity == selected_pr)
    if len(matches) != 1:
        return _block(
            "selected-pr-unavailable",
            selected_pr.node_id,
            "selected PR was not observed exactly once",
        )
    pr = matches[0]
    bookmark = next(
        (
            item
            for item in snapshot.local.local_bookmarks
            if item.name == pr.head_branch
        ),
        None,
    )
    if bookmark is None or isinstance(bookmark.target, AbsentBookmarkTarget):
        return _block(
            "bookmark-absent", pr.head_branch, "same-named local bookmark is absent"
        )
    if isinstance(bookmark.target, BookmarkConflict):
        return _block(
            "bookmark-conflicted",
            pr.head_branch,
            "same-named local bookmark is conflicted",
        )
    commit = next(
        (
            item
            for item in snapshot.local.commits
            if item.commit_id == bookmark.target.commit_id
        ),
        None,
    )
    if commit is None:
        return _block(
            "commit-unobserved",
            bookmark.target.commit_id,
            "bookmark target commit was not observed",
        )
    metadata = _metadata(commit.description)
    if metadata is None:
        return _block(
            "title-missing",
            commit.commit_id,
            "selected revision has no description title",
        )
    title, body = metadata
    return DesiredStack(
        snapshot.repository,
        pr.base_branch,
        (DesiredExistingPR(pr.identity, commit.commit_id, title, body),),
    )


def _dependencies(
    snapshot: Snapshot,
    pr: GitHubPullRequest,
    bookmark: LocalBookmark,
    head: LiveRemoteRef,
    base: LiveRemoteRef,
) -> Dependencies:
    return Dependencies(
        snapshot.local.effective_config,
        snapshot.push_url,
        snapshot.tool_state.state_blob_oid,
        snapshot.tool_state.operation_blob_oid,
        pr,
        snapshot.membership,
        bookmark,
        head,
        base,
    )


def _is_ancestor(
    commits: tuple[ObservedCommit, ...],
    ancestor_commit_id: str,
    descendant_commit_id: str,
) -> bool:
    parents = {commit.commit_id: commit.parent_commit_ids for commit in commits}
    pending = [descendant_commit_id]
    seen: set[str] = set()
    while pending:
        commit_id = pending.pop()
        if commit_id == ancestor_commit_id:
            return True
        if commit_id not in seen:
            seen.add(commit_id)
            pending.extend(parents.get(commit_id, ()))
    return False


def _comparison_is_nonempty_linear_and_conflict_free(
    commits: tuple[ObservedCommit, ...], base_commit_id: str, head_commit_id: str
) -> bool:
    if base_commit_id == head_commit_id:
        return False
    by_commit_id: dict[str, ObservedCommit] = {}
    for commit in commits:
        if commit.commit_id in by_commit_id:
            return False
        by_commit_id[commit.commit_id] = commit
    current = head_commit_id
    seen: set[str] = set()
    while current != base_commit_id:
        if current in seen:
            return False
        seen.add(current)
        commit = by_commit_id.get(current)
        if commit is None or commit.has_conflicts or len(commit.parent_commit_ids) != 1:
            return False
        current = commit.parent_commit_ids[0]
    return True


def _validate_plan_scope(
    snapshot: Snapshot, desired: DesiredStack
) -> GitHubPullRequest | Blocked:
    if snapshot.tool_state.operation_blob_oid is not None:
        return _block(
            "operation-fenced",
            OPERATION_REF,
            "a pending operation fences ordinary planning",
        )
    if desired.repository != snapshot.repository:
        return _block(
            "repository-mismatch",
            desired.repository.node_id,
            "desired repository differs from the observed repository",
        )
    if (
        len(desired.active) != 1
        or not isinstance(snapshot.membership, StandalonePullRequest)
        or snapshot.membership.pr != desired.active[0].pr_identity
    ):
        return _block(
            "unsupported-membership",
            "stack",
            "foundation planner requires one complete active member",
        )
    pr = next(
        (
            value
            for value in snapshot.pull_requests
            if value.identity == desired.active[0].pr_identity
        ),
        None,
    )
    if pr is None or pr.state is not PullRequestState.OPEN:
        return _block(
            "pr-not-open",
            desired.active[0].pr_identity.node_id,
            "selected PR is not open",
        )
    if (
        pr.identity.repository != snapshot.repository
        or pr.head_repository != snapshot.repository
    ):
        return _block(
            "nonlocal-pr",
            pr.identity.node_id,
            "foundation planner requires a repository-local PR",
        )
    if desired.base_branch != pr.base_branch:
        return _block(
            "target-mismatch",
            desired.base_branch,
            "desired target differs from the observed PR base branch",
        )
    return pr


def _validate_pr_dependencies(
    snapshot: Snapshot, desired: DesiredStack, pr: GitHubPullRequest
) -> tuple[DesiredExistingPR, LocalBookmark, LiveRemoteRef, LiveRemoteRef] | Blocked:
    head_name = f"refs/heads/{pr.head_branch}"
    base_name = f"refs/heads/{pr.base_branch}"
    head_ref = RemoteBranchRef(snapshot.repository, head_name)
    base_ref = RemoteBranchRef(snapshot.repository, base_name)
    heads = tuple(value for value in snapshot.live_refs if value.ref == head_ref)
    bases = tuple(value for value in snapshot.live_refs if value.ref == base_ref)
    if len(heads) != 1 or heads[0].commit_id != pr.reported_head_commit_id:
        return _block(
            "head-disagrees",
            head_name,
            "GitHub PR head and authoritative live ref disagree",
        )
    if len(bases) != 1 or bases[0].commit_id != pr.reported_base_commit_id:
        return _block(
            "base-disagrees",
            base_name,
            "GitHub PR base and authoritative live ref disagree",
        )
    wanted = desired.active[0]
    bookmarks = tuple(
        value
        for value in snapshot.local.local_bookmarks
        if value.name == pr.head_branch
    )
    if len(bookmarks) != 1 or not isinstance(bookmarks[0].target, CommitTarget):
        return _block(
            "bookmark-changed",
            pr.head_branch,
            "planned local bookmark is absent, duplicated, or conflicted",
        )
    if bookmarks[0].target.commit_id != wanted.desired_commit_id:
        return _block(
            "bookmark-changed",
            pr.head_branch,
            "planned local bookmark target differs from desired state",
        )
    commit = next(
        (
            value
            for value in snapshot.local.commits
            if value.commit_id == wanted.desired_commit_id
        ),
        None,
    )
    if commit is None or commit.has_conflicts:
        return _block(
            "commit-conflicted",
            wanted.desired_commit_id,
            "desired commit is absent or conflicted",
        )
    assert bases[0].commit_id is not None
    if not _comparison_is_nonempty_linear_and_conflict_free(
        snapshot.local.commits,
        bases[0].commit_id,
        wanted.desired_commit_id,
    ):
        return _block(
            "invalid-comparison",
            wanted.desired_commit_id,
            "selected base-to-head comparison is empty, nonlinear, incomplete, or conflicted",
        )
    return wanted, bookmarks[0], heads[0], bases[0]


def _plan_publication(
    snapshot: Snapshot,
    wanted: DesiredExistingPR,
    head: LiveRemoteRef,
) -> tuple[PlannedHeadUpdate, ...] | Blocked:
    assert head.commit_id is not None
    if wanted.desired_commit_id == head.commit_id:
        return ()
    if _is_ancestor(snapshot.local.commits, head.commit_id, wanted.desired_commit_id):
        return (
            PlannedHeadUpdate(
                head.ref,
                head.commit_id,
                wanted.desired_commit_id,
                FastForward(),
            ),
        )
    publications = tuple(
        item
        for item in snapshot.tool_state.state.last_published_heads
        if item.pr == wanted.pr_identity
        and item.ref == head.ref
        and item.verified_commit_id == head.commit_id
    )
    if len(publications) != 1:
        return _block(
            "replacement-unauthorized",
            head.ref.full_name,
            "live head is neither an ancestor nor one unambiguous last publication",
        )
    return (
        PlannedHeadUpdate(
            head.ref,
            head.commit_id,
            wanted.desired_commit_id,
            MatchesLastPublication(publications[0]),
        ),
    )


def _metadata_updates(
    pr: GitHubPullRequest, wanted: DesiredExistingPR
) -> tuple[PRMetadataUpdate, ...]:
    if (pr.title, pr.body) == (wanted.title, wanted.body):
        return ()
    return (PRMetadataUpdate(pr.identity, wanted.title, wanted.body),)


def plan_sync(
    snapshot: Snapshot,
    desired: DesiredStack | Blocked,
) -> SyncPlan:
    if isinstance(desired, Blocked):
        return desired
    pr = _validate_plan_scope(snapshot, desired)
    if isinstance(pr, Blocked):
        return pr
    pr_dependencies = _validate_pr_dependencies(snapshot, desired, pr)
    if isinstance(pr_dependencies, Blocked):
        return pr_dependencies
    wanted, bookmark, head, base = pr_dependencies
    head_updates = _plan_publication(snapshot, wanted, head)
    if isinstance(head_updates, Blocked):
        return head_updates
    metadata = _metadata_updates(pr, wanted)
    dependencies = _dependencies(snapshot, pr, bookmark, head, base)
    if not head_updates and not metadata:
        return NoOp(desired, dependencies)
    if pr.auto_merge_enabled or pr.in_merge_queue:
        return _block(
            "active-automation",
            pr.identity.node_id,
            "mutation is blocked while auto-merge or merge queue state is active",
        )
    return Apply(
        desired,
        head_updates,
        metadata,
        dependencies,
    )


def render(plan: SyncPlan) -> str:
    if isinstance(plan, Blocked):
        return "\n".join(
            f"blocked [{item.code}] {item.subject}: {item.detail}"
            for item in plan.reasons
        )
    if isinstance(plan, NoOp):
        return "no-op: observed head and metadata already equal desired state"
    consequences = [
        *(
            f"publish {item.new_commit_id} to {item.ref.full_name}"
            for item in plan.head_updates
        ),
        *(
            f"update metadata for PR {item.pr_identity.node_id}"
            for item in plan.metadata_updates
        ),
    ]
    return "apply:\n" + "\n".join(f"  - {item}" for item in consequences)
