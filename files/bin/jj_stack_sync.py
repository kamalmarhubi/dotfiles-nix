"""Observation, planning, application, and recovery for ``jj stack sync``."""

from __future__ import annotations

import fcntl
import http.client
import json
import os
import re
import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from urllib.parse import quote, urlparse
from markdown_it import MarkdownIt
from markdown_it.rules_inline.newline import newline as _parse_newline
from markdown_it.rules_inline.state_inline import StateInline


STATE_REF = "refs/jj-stack/state"
RECOVERY_REF = "refs/jj-stack/recovery"
GITHUB_API_VERSION = "2026-03-10"
GITHUB_CONNECT_TIMEOUT = 10.0
GITHUB_READ_TIMEOUT = 30.0
GITHUB_OVERALL_TIMEOUT = 45.0


class Error(Exception):
    pass


class ConcurrentUpdate(Error):
    pass


class LockBusy(Error):
    pass


class RemoteResolutionError(Error):
    pass


class SourceUnavailable(Error):
    pass


class MalformedSource(Error):
    pass


class IncompleteSource(Error):
    pass


class SourceMismatch(Error):
    pass


class GitPushError(Error):
    """A Git push whose remote outcome must be established by readback."""


class GitHubTransportError(Error):
    """A request whose complete HTTP response is not known."""

    def __init__(
        self,
        category: str,
        *,
        status: int | None = None,
        headers: tuple[tuple[str, str], ...] = (),
        body: bytes = b"",
    ) -> None:
        super().__init__(f"GitHub transport failed ({category})")
        self.category = category
        self.status = status
        self.headers = headers
        self.body = body


class GitHubHttpError(Error):
    """A complete non-successful GitHub HTTP response."""

    def __init__(self, response: GitHubHttpResponse, context: str) -> None:
        super().__init__(f"{context} returned HTTP {response.status}")
        self.response = response


@dataclass(frozen=True)
class GitHubHttpResponse:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


@dataclass(frozen=True)
class GitHubRepositoryId:
    """GitHub host plus the repository's immutable GraphQL node ID."""

    host: str
    node_id: str


@dataclass(frozen=True)
class GitHubRepository:
    identity: GitHubRepositoryId
    name_with_owner: str
    url: str
    default_branch: str | None


@dataclass(frozen=True)
class PullRequestId:
    """One repository-qualified pull request number."""

    repository: GitHubRepositoryId
    number: int

    def __post_init__(self) -> None:
        if type(self.number) is not int or self.number <= 0:
            raise ValueError("pull request number must be a positive integer")


@dataclass(frozen=True)
class GitHubStackId:
    repository: GitHubRepositoryId
    number: int

    def __post_init__(self) -> None:
        if type(self.number) is not int or self.number <= 0:
            raise ValueError("stack number must be a positive integer")


@dataclass(frozen=True)
class GitHubStackSummary:
    identity: GitHubStackId
    node_id: str
    base_branch: str


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


class PullRequestUpdateState(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class CommitStatusState(StrEnum):
    ERROR = "error"
    FAILURE = "failure"
    PENDING = "pending"
    SUCCESS = "success"


@dataclass(frozen=True)
class GitHubPullRequest:
    """Validated GitHub fields used by planning, not a raw API response."""

    identity: PullRequestId
    node_id: str
    state: PullRequestState
    draft: bool
    head_repository: GitHubRepositoryId | None
    head_branch: str
    head_oid: str | None
    base_branch: str
    base_oid: str | None
    auto_merge_enabled: bool
    in_merge_queue: bool
    title: str
    body: str
    stack: GitHubStackSummary | None

    @property
    def number(self) -> int:
        return self.identity.number


@dataclass(frozen=True)
class GitHubStack:
    identity: GitHubStackId
    node_id: str
    base_branch: str
    pull_requests: tuple[PullRequestId, ...]


class GitHubClient(Protocol):
    def resolve_repository(
        self, locator: str | GitHubRepositoryId
    ) -> GitHubRepository: ...

    def pull_requests(
        self, identities: Sequence[PullRequestId]
    ) -> tuple[GitHubPullRequest, ...]: ...

    def stack(
        self, repository: GitHubRepository, identity: GitHubStackId
    ) -> GitHubStack | None: ...

    def find_pull_requests(
        self,
        repository: GitHubRepositoryId,
        *,
        head_branches: Sequence[str] = (),
        base_branches: Sequence[str] = (),
        states: Collection[PullRequestState] | None = None,
    ) -> tuple[GitHubPullRequest, ...]: ...

    def create_pull_request(
        self,
        repository: GitHubRepository,
        *,
        head_branch: str,
        base_branch: str,
        title: str,
        body: str,
        draft: bool,
    ) -> PullRequestId: ...

    def create_stack(
        self,
        repository: GitHubRepository,
        *,
        pull_requests: Sequence[PullRequestId],
    ) -> GitHubStackSummary: ...

    def add_stack_members(
        self,
        repository: GitHubRepository,
        identity: GitHubStackId,
        *,
        pull_requests: Sequence[PullRequestId],
    ) -> GitHubStackSummary: ...

    def unstack(
        self, repository: GitHubRepository, identity: GitHubStackId
    ) -> GitHubStackSummary | None: ...

    def update_pull_request(
        self,
        repository: GitHubRepository,
        identity: PullRequestId,
        *,
        title: str | None = None,
        body: str | None = None,
        base_branch: str | None = None,
        state: PullRequestUpdateState | None = None,
    ) -> None: ...

    def commit_statuses(
        self,
        repository: GitHubRepository,
        commit_oid: str,
        *,
        contexts: Sequence[str],
    ) -> Mapping[str, CommitStatusState | None]: ...


class GitTransport(Protocol):
    """The complete authoritative remote-ref boundary for one workspace."""

    def observe_live_refs(
        self,
        push_url: str,
        repository: GitHubRepositoryId,
        full_names: Sequence[str],
    ) -> tuple[LiveRemoteRef, ...]: ...

    def push_exact_head_updates(
        self,
        push_url: str,
        updates: Sequence[PlannedHeadUpdate],
    ) -> None: ...

    def push_absent_heads(
        self,
        push_url: str,
        repository: GitHubRepositoryId,
        goals: Sequence[NewPullRequestGoal],
    ) -> None: ...


@dataclass(frozen=True)
class StandalonePullRequest:
    pr: PullRequestId


@dataclass(frozen=True)
class ServerStackMembership:
    """Complete ordered membership for the stack containing selected_pr."""

    selected_pr: PullRequestId
    stack: GitHubStackSummary
    ordered_prs: tuple[PullRequestId, ...]

    def __post_init__(self) -> None:
        if not self.ordered_prs:
            raise ValueError("server stack identity and membership must be nonempty")
        if self.selected_pr not in self.ordered_prs:
            raise ValueError("selected PR must belong to the observed server stack")
        if len(set(self.ordered_prs)) != len(self.ordered_prs):
            raise ValueError("server stack membership must not contain duplicates")
        if any(pr.repository != self.selected_pr.repository for pr in self.ordered_prs):
            raise ValueError("all server stack members must belong to one repository")
        if self.stack.identity.repository != self.selected_pr.repository:
            raise ValueError("server stack and members must belong to one repository")

    @property
    def base_branch(self) -> str:
        return self.stack.base_branch

    @property
    def server_stack_id(self) -> str:
        return self.stack.node_id

    @property
    def server_stack_number(self) -> int:
        return self.stack.identity.number


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


@dataclass(frozen=True)
class LastPublishedHead:
    """Last commit this tool published and verified for one exact branch."""

    pr: PullRequestId
    ref: RemoteBranchRef
    verified_commit_id: str


@dataclass(frozen=True)
class LastAdoptedHead:
    """An exact GitHub head accepted after a verified external restack."""

    pr: PullRequestId
    ref: RemoteBranchRef
    verified_commit_id: str


@dataclass(frozen=True)
class LastPublishedBoundary:
    """Verified ownership of a merged-prefix alias, never of the merged head.

    A boundary aliases the integration branch at the seam immediately after
    ``merged_pr``.  Keeping this receipt separate from head authority is
    intentional: equality with either a PR head or the integration ref does
    not grant permission to move the alias.
    """

    merged_pr: PullRequestId
    ref: RemoteBranchRef
    verified_commit_id: str


@dataclass(frozen=True)
class TrackedStack:
    repository: GitHubRepositoryId
    base_branch: str
    ordered_prs: tuple[PullRequestId, ...]


class DetachedDisposition(StrEnum):
    CLEANUP = "cleanup"
    KEEP = "keep"


@dataclass(frozen=True)
class DetachedAssociation:
    """Durable ownership evidence for a PR no longer in an active stack."""

    pr: PullRequestId
    ref: RemoteBranchRef
    verified_commit_id: str
    disposition: DetachedDisposition = DetachedDisposition.CLEANUP

    def __post_init__(self) -> None:
        if self.pr.repository != self.ref.repository or not self.verified_commit_id:
            raise ValueError("detached association ownership is malformed")


@dataclass(frozen=True)
class TrackedState:
    stacks: tuple[TrackedStack, ...]
    last_published_heads: tuple[LastPublishedHead, ...]
    last_adopted_heads: tuple[LastAdoptedHead, ...] = ()
    detached_associations: tuple[DetachedAssociation, ...] = ()
    last_published_boundaries: tuple[LastPublishedBoundary, ...] = ()


EMPTY_STATE = TrackedState((), (), (), (), ())


@dataclass(frozen=True)
class ToolStateRead:
    """Decoded tool state bound to the exact local blob ref observations."""

    state_blob_oid: str | None
    state: TrackedState
    recovery_blob_oid: str | None


@dataclass(frozen=True)
class Snapshot:
    """Complete validated inputs for one planning scope."""

    repository: GitHubRepositoryId
    push_url: str
    remote: str
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
class ExistingPRAssignment:
    pr_identity: PullRequestId
    commit_id: str


@dataclass(frozen=True)
class StackSelection:
    base_branch: str
    ordered: tuple[ExistingPRAssignment, ...]

    def __post_init__(self) -> None:
        if not self.base_branch:
            raise ValueError("stack selection must have a base")
        identities = tuple(item.pr_identity for item in self.ordered)
        if len(set(identities)) != len(identities):
            raise ValueError("stack selection must not assign a PR more than once")


@dataclass(frozen=True)
class PublicationAssignment:
    """An exact local commit to new GitHub branch assignment."""

    commit_id: str
    branch_name: str


@dataclass(frozen=True)
class FirstPublicationInput:
    """Complete, already-read evidence for planning a first publication."""

    repository: GitHubRepositoryId
    base_branch: str
    ordered_commit_ids: tuple[str, ...]
    local: LocalObservation
    explicit_assignments: tuple[PublicationAssignment, ...] | None
    template_results: tuple[PublicationAssignment, ...]
    managed_branch_names: tuple[str, ...]
    destinations: tuple[LiveRemoteRef, ...]
    historical_pull_requests: tuple[GitHubPullRequest, ...]
    recovery_facts: tuple[RecoveryEntry, ...] = ()


@dataclass(frozen=True)
class NewPullRequestGoal:
    commit_id: str
    branch_name: str
    base_branch: str
    title: str
    body: str

    def __post_init__(self) -> None:
        if (
            not all(
                isinstance(value, str)
                for value in (
                    self.commit_id,
                    self.branch_name,
                    self.base_branch,
                    self.title,
                    self.body,
                )
            )
            or not self.commit_id
            or not self.branch_name
            or not self.base_branch
            or not self.title
        ):
            raise ValueError("new pull request goal fields must be nonempty")


@dataclass(frozen=True)
class FirstPublicationGoal:
    repository: GitHubRepositoryId
    base_branch: str
    base_commit_id: str
    pull_requests: tuple[NewPullRequestGoal, ...]


@dataclass(frozen=True)
class FirstPublicationPlan:
    """A pure goal together with every observation on which it depends."""

    goal: FirstPublicationGoal
    local: LocalObservation
    destinations: tuple[LiveRemoteRef, ...]
    historical_pull_requests: tuple[GitHubPullRequest, ...]


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
    remote: str
    state_blob_oid: str | None
    recovery_blob_oid: str | None
    prs: tuple[GitHubPullRequest, ...]
    membership: PullRequestMembership
    live_heads: tuple[LiveRemoteRef, ...]
    live_bases: tuple[LiveRemoteRef, ...]


@dataclass(frozen=True)
class FastForward:
    pass


@dataclass(frozen=True)
class MatchesLastPublication:
    publication: LastPublishedHead


@dataclass(frozen=True)
class MatchesLastAdoption:
    adoption: LastAdoptedHead


Authority = (
    FastForward | MatchesLastPublication | MatchesLastAdoption
)


@dataclass(frozen=True)
class RemoteRestackBoundary:
    pr_identity: PullRequestId
    branch: str
    old_seam_commit_id: str
    new_seam_commit_id: str
    old_commit_id: str
    remote_commit_id: str


@dataclass(frozen=True)
class RemoteRestackAdoption:
    repository: GitHubRepositoryId
    remote: str
    state_blob_oid: str | None
    boundaries: tuple[RemoteRestackBoundary, ...]


@dataclass(frozen=True)
class AdoptionVerified:
    adopted_heads: tuple[LastAdoptedHead, ...]


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
    boundary_receipts: tuple[LastPublishedBoundary, ...] = ()


@dataclass(frozen=True)
class Apply:
    desired: DesiredStack
    head_updates: tuple[PlannedHeadUpdate, ...]
    metadata_updates: tuple[PRMetadataUpdate, ...]
    dependencies: Dependencies
    boundary_receipts: tuple[LastPublishedBoundary, ...] = ()


SyncPlan = Blocked | NoOp | Apply


@dataclass(frozen=True)
class Verified:
    head_published: bool = False
    metadata_updated: bool = False
    state_recorded: bool = False


@dataclass(frozen=True)
class Stopped:
    stage: str
    detail: str


ApplyResult = Verified | Stopped


@dataclass(frozen=True)
class HeadMutationAttempt:
    repository: GitHubRepositoryId
    push_url: str
    updates: tuple[PlannedHeadUpdate, ...]
    tracking: TrackedStack
    publications: tuple[LastPublishedHead, ...]
    boundaries: tuple[LastPublishedBoundary, ...] = ()

    def __post_init__(self) -> None:
        if not self.push_url or not self.updates:
            raise ValueError("head mutation requires a push URL and updates")
        if self.tracking.repository != self.repository or any(
            update.ref.repository != self.repository for update in self.updates
        ):
            raise ValueError("head mutation values must belong to its repository")
        expected = {(update.ref, update.new_commit_id) for update in self.updates}
        recorded = {
            (publication.ref, publication.verified_commit_id)
            for publication in self.publications
        } | {
            (boundary.ref, boundary.verified_commit_id)
            for boundary in self.boundaries
        }
        members = set(self.tracking.ordered_prs)
        if (
            recorded != expected
            or len(self.publications) + len(self.boundaries) != len(self.updates)
            or any(publication.pr not in members for publication in self.publications)
            or any(boundary.merged_pr not in members for boundary in self.boundaries)
        ):
            raise ValueError("head mutation receipts must exactly match its updates")


@dataclass(frozen=True)
class MetadataMutationAttempt:
    repository: GitHubRepositoryId
    update: PRMetadataUpdate
    expected_title: str
    expected_body: str

    def __post_init__(self) -> None:
        if self.update.pr_identity.repository != self.repository:
            raise ValueError("metadata mutation belongs to another repository")


@dataclass(frozen=True)
class BranchCreationAttempt:
    """One atomic absent-to-OID publication, before PR identities exist."""

    repository: GitHubRepositoryId
    push_url: str
    goals: tuple[NewPullRequestGoal, ...]

    def __post_init__(self) -> None:
        names = tuple(goal.branch_name for goal in self.goals)
        if (
            not isinstance(self.push_url, str)
            or not self.push_url
            or not self.goals
            or len(set(names)) != len(names)
        ):
            raise ValueError("branch creation requires a push URL and goals")


@dataclass(frozen=True)
class PullRequestCreationAttempt:
    repository: GitHubRepositoryId
    goal: NewPullRequestGoal
    pull_request: PullRequestId | None = None

    def __post_init__(self) -> None:
        if self.pull_request is not None and self.pull_request.repository != self.repository:
            raise ValueError("created pull request belongs to another repository")


@dataclass(frozen=True)
class StackCreationAttempt:
    repository: GitHubRepositoryId
    base_branch: str
    pull_requests: tuple[PullRequestId, ...]
    goals: tuple[NewPullRequestGoal, ...]

    def __post_init__(self) -> None:
        if (
            not self.pull_requests
            or len(set(self.pull_requests)) != len(self.pull_requests)
            or len(self.pull_requests) != len(self.goals)
            or len({goal.branch_name for goal in self.goals}) != len(self.goals)
            or any(
                pr.repository != self.repository for pr in self.pull_requests
            )
        ):
            raise ValueError("stack creation members must belong to its repository")


@dataclass(frozen=True)
class AdoptionAttempt:
    repository: GitHubRepositoryId
    remote: str
    state_blob_oid: str | None
    boundaries: tuple[RemoteRestackBoundary, ...]

    def __post_init__(self) -> None:
        if not self.remote or not self.boundaries:
            raise ValueError("adoption attempt must be nonempty")


MutationAttempt = (
    HeadMutationAttempt
    | MetadataMutationAttempt
    | AdoptionAttempt
    | BranchCreationAttempt
    | PullRequestCreationAttempt
    | StackCreationAttempt
)


@dataclass(frozen=True)
class RecoveryEntry:
    """One exact write sent (or about to be sent), with stable causal identity."""

    identity: str
    attempt: MutationAttempt
    possibly_live: bool = False

    def __post_init__(self) -> None:
        if not self.identity:
            raise ValueError("recovery entry identity must be nonempty")


@dataclass(frozen=True)
class RecoveryJournal:
    entries: tuple[RecoveryEntry, ...]

    def __post_init__(self) -> None:
        identities = tuple(entry.identity for entry in self.entries)
        if not identities or len(set(identities)) != len(identities):
            raise ValueError("recovery journal must be nonempty with unique entries")


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


def _github_token(host: str, *, cwd: str | Path | None = None) -> str:
    if not host or any(character.isspace() for character in host):
        raise ValueError("GitHub host must be nonempty and contain no whitespace")
    environment = os.environ.copy()
    environment["GH_PROMPT_DISABLED"] = "1"
    environment.pop("GH_DEBUG", None)
    environment.pop("DEBUG", None)
    result = subprocess.run(
        ["gh", "auth", "token", "--hostname", host],
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        text=True,
        capture_output=True,
        check=False,
        timeout=GITHUB_CONNECT_TIMEOUT,
        env=environment,
    )
    if result.returncode:
        raise SourceUnavailable(f"GitHub authentication failed for {host}")
    lines = result.stdout.splitlines()
    if len(lines) != 1 or not lines[0]:
        raise SourceUnavailable(f"GitHub authentication returned no token for {host}")
    return lines[0]


def _github_api_origin(host: str) -> str:
    host = host.lower()
    if host != "github.com":
        raise SourceUnavailable(f"unsupported GitHub host: {host}")
    return "https://api.github.com"


def _github_graphql_url(host: str) -> str:
    return f"{_github_api_origin(host)}/graphql"


def _ssh_github_host(alias: str, *, cwd: str | Path | None = None) -> str:
    result = subprocess.run(
        ["ssh", "-G", alias],
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        text=True,
        capture_output=True,
        check=False,
        timeout=GITHUB_CONNECT_TIMEOUT,
    )
    if result.returncode:
        raise SourceUnavailable("could not resolve GitHub SSH host")
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition(" ")
        if separator and key in {"hostname", "port"} and key not in values:
            values[key] = value.strip().lower()
    hostname = values.get("hostname")
    port = values.get("port", "22")
    if hostname == "github.com" and port == "22":
        return "github.com"
    if hostname == "ssh.github.com" and port == "443":
        return "github.com"
    raise SourceUnavailable("SSH remote does not resolve to canonical GitHub.com")


def _github_repository_locator(
    push_url: str, *, cwd: str | Path | None = None
) -> tuple[str, str]:
    parsed = urlparse(push_url)
    if parsed.scheme in {"http", "https"} and parsed.hostname is not None:
        host = parsed.hostname.lower()
        path = parsed.path
        if host != "github.com":
            raise SourceUnavailable(f"unsupported GitHub host: {host}")
    elif parsed.scheme == "ssh" and parsed.hostname is not None:
        host = _ssh_github_host(parsed.hostname, cwd=cwd)
        path = parsed.path
    else:
        match = re.fullmatch(r"(?:[^@]+@)?([^:]+):(.+)", push_url)
        if match is None:
            raise MalformedSource("GitHub remote URL has no host")
        host = _ssh_github_host(match.group(1), cwd=cwd)
        path = match.group(2)
    name_with_owner = path.removeprefix("/").removesuffix(".git")
    owner, separator, name = name_with_owner.partition("/")
    if not separator or not owner or not name or "/" in name:
        raise MalformedSource("GitHub remote URL has no owner/name repository")
    return host, name_with_owner


def _response_headers(response: object) -> tuple[tuple[str, str], ...]:
    headers = getattr(response, "headers", None)
    return tuple(headers.items()) if headers is not None else ()


def _read_github_response(response: object, status: int) -> GitHubHttpResponse:
    headers = _response_headers(response)
    try:
        body = response.read()  # type: ignore[attr-defined]
    except http.client.IncompleteRead as exc:
        raise GitHubTransportError(
            "truncated-body", status=status, headers=headers, body=exc.partial
        ) from exc
    except (OSError, TimeoutError, socket.timeout) as exc:
        raise GitHubTransportError("read", status=status, headers=headers) from exc
    lengths = tuple(
        int(value)
        for name, value in headers
        if name.lower() == "content-length" and value.isdecimal()
    )
    if lengths and (len(set(lengths)) != 1 or len(body) != lengths[0]):
        raise GitHubTransportError(
            "truncated-body", status=status, headers=headers, body=body
        )
    return GitHubHttpResponse(status, headers, body)


def github_http_request(
    host: str,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    cwd: str | Path | None = None,
) -> GitHubHttpResponse:
    """Send one non-retrying request and preserve its complete response bytes."""
    if not path.startswith("/"):
        raise ValueError("GitHub API path must be absolute")
    token = _github_token(host, cwd=cwd)
    request = urllib.request.Request(
        f"{_github_api_origin(host)}{path}",
        data=body,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "jj-stack",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        },
    )
    context = ssl.create_default_context()
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler(),
        urllib.request.HTTPSHandler(context=context),
        _NoRedirects(),
    )
    started = time.monotonic()
    try:
        response = opener.open(
            request, timeout=min(GITHUB_CONNECT_TIMEOUT, GITHUB_READ_TIMEOUT)
        )
    except urllib.error.HTTPError as exc:
        result = _read_github_response(exc, exc.code)
    except (urllib.error.URLError, OSError, TimeoutError, socket.timeout) as exc:
        category = (
            "timeout"
            if isinstance(getattr(exc, "reason", exc), TimeoutError)
            else "connect"
        )
        raise GitHubTransportError(category) from exc
    else:
        with response:
            result = _read_github_response(response, response.status)
    if time.monotonic() - started > GITHUB_OVERALL_TIMEOUT:
        raise GitHubTransportError(
            "overall-timeout",
            status=result.status,
            headers=result.headers,
            body=result.body,
        )
    return result


def _github_json_response(response: GitHubHttpResponse, context: str) -> object:
    if not 200 <= response.status < 300:
        raise GitHubHttpError(response, context)
    try:
        return json.loads(response.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MalformedSource(f"{context} returned invalid JSON") from exc


def _github_graphql(
    host: str,
    query: str,
    variables: dict[str, object],
    *,
    cwd: str | Path | None = None,
) -> object:
    payload = json.dumps(
        {"query": query, "variables": variables},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    response = github_http_request(
        host, "POST", urlparse(_github_graphql_url(host)).path, body=payload, cwd=cwd
    )
    value = _github_json_response(response, "GitHub GraphQL response")
    if not isinstance(value, dict) or "errors" in value:
        raise MalformedSource("GitHub GraphQL response contains errors or is malformed")
    return value


def _exact_record(value: object, fields: set[str], context: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != fields:
        raise MalformedSource(f"{context} has an unexpected shape")
    return value


def _text(value: object, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise MalformedSource(f"{context} must be a nonempty string")
    return value


_REPOSITORY_QUERY_FIELDS = "id nameWithOwner url defaultBranchRef { name }"


def _parse_github_repository(
    value: object,
    host: str,
    *,
    expected_identity: GitHubRepositoryId | None = None,
    expected_name: str | None = None,
) -> GitHubRepository:
    raw = _exact_record(
        value,
        {"id", "nameWithOwner", "url", "defaultBranchRef"},
        "GitHub repository response",
    )
    identity = GitHubRepositoryId(host, _text(raw["id"], "repository ID"))
    name_with_owner = _text(raw["nameWithOwner"], "repository name")
    url = _text(raw["url"], "repository URL")
    response_host = urlparse(url).hostname
    if response_host is None:
        raise MalformedSource("repository URL has no host")
    if response_host.lower() != host:
        raise SourceMismatch("GitHub returned a repository on a different host")
    if expected_identity is not None and identity != expected_identity:
        raise SourceMismatch("GitHub returned a different repository identity")
    if expected_name is not None and name_with_owner.lower() != expected_name.lower():
        raise SourceMismatch("GitHub returned a different repository locator")
    default = raw["defaultBranchRef"]
    default_branch = None
    if default is not None:
        default_branch = _text(
            _exact_record(default, {"name"}, "default branch")["name"],
            "default branch name",
        )
    return GitHubRepository(identity, name_with_owner, url, default_branch)


def _resolve_github_repository(
    locator: str | GitHubRepositoryId, *, cwd: str | Path | None = None
) -> GitHubRepository:
    if isinstance(locator, GitHubRepositoryId):
        response = _exact_record(
            _github_graphql(
                locator.host,
                f"""
                query($id: ID!) {{
                  node(id: $id) {{
                    ... on Repository {{ {_REPOSITORY_QUERY_FIELDS} }}
                  }}
                }}
                """,
                {"id": locator.node_id},
                cwd=cwd,
            ),
            {"data"},
            "GitHub repository GraphQL response",
        )
        data = _exact_record(response["data"], {"node"}, "repository GraphQL data")
        if data["node"] is None:
            raise IncompleteSource("GitHub repository is unavailable")
        return _parse_github_repository(
            data["node"], locator.host, expected_identity=locator
        )

    host, name_with_owner = _github_repository_locator(locator, cwd=cwd)
    owner, separator, name = name_with_owner.partition("/")
    assert separator and owner and name
    response = _exact_record(
        _github_graphql(
            host,
            f"""
            query($owner: String!, $name: String!) {{
              repository(owner: $owner, name: $name) {{ {_REPOSITORY_QUERY_FIELDS} }}
            }}
            """,
            {"owner": owner, "name": name},
            cwd=cwd,
        ),
        {"data"},
        "GitHub repository GraphQL response",
    )
    data = _exact_record(response["data"], {"repository"}, "repository GraphQL data")
    if data["repository"] is None:
        raise IncompleteSource("GitHub repository is unavailable")
    return _parse_github_repository(
        data["repository"], host, expected_name=name_with_owner
    )


_PR_SOURCE_FIELDS = {
    "id",
    "number",
    "state",
    "isDraft",
    "headRepository",
    "headRefName",
    "headRefOid",
    "baseRefName",
    "baseRefOid",
    "autoMergeRequest",
    "mergeQueueEntry",
    "title",
    "body",
    "stack",
}

_PR_SOURCE_QUERY_FIELDS = """
id number state isDraft
headRepository { id }
headRefName headRefOid baseRefName baseRefOid
autoMergeRequest { enabledAt }
mergeQueueEntry { id }
title body
stack { id number baseRefName }
"""

_PR_SOURCE_QUERY = f"""
query($owner: String!, $name: String!, $number: Int!) {{
  repository(owner: $owner, name: $name) {{
    id
    pullRequest(number: $number) {{ {_PR_SOURCE_QUERY_FIELDS} }}
  }}
}}
"""


def _parse_github_pull_request(
    value: object,
    repository: GitHubRepository,
    expected_number: int | None = None,
) -> GitHubPullRequest:
    raw = _exact_record(value, _PR_SOURCE_FIELDS, "pull request")
    number = raw["number"]
    if (
        type(number) is not int
        or number <= 0
        or (expected_number is not None and number != expected_number)
    ):
        raise SourceMismatch("GitHub returned a different pull request number")
    try:
        state = PullRequestState(_text(raw["state"], "pull request state"))
    except ValueError as exc:
        raise MalformedSource("pull request state is unsupported") from exc
    draft = raw["isDraft"]
    if type(draft) is not bool:
        raise MalformedSource("pull request draft state must be boolean")
    head_repository = raw["headRepository"]
    head_repository_identity = None
    if head_repository is not None:
        head_repository_record = _exact_record(
            head_repository, {"id"}, "pull request head repository"
        )
        head_repository_identity = GitHubRepositoryId(
            repository.identity.host,
            _text(head_repository_record["id"], "head repository ID"),
        )
    auto_merge = raw["autoMergeRequest"]
    if auto_merge is not None:
        _exact_record(auto_merge, {"enabledAt"}, "auto-merge request")
    merge_queue = raw["mergeQueueEntry"]
    if merge_queue is not None:
        _exact_record(merge_queue, {"id"}, "merge queue entry")
    body = raw["body"]
    if body is None:
        body = ""
    elif not isinstance(body, str):
        raise MalformedSource("pull request body must be text or null")
    stack = raw["stack"]
    stack_summary = None
    if stack is not None:
        stack_record = _exact_record(
            stack, {"id", "number", "baseRefName"}, "pull request stack"
        )
        stack_number = stack_record["number"]
        if type(stack_number) is not int or stack_number <= 0:
            raise MalformedSource("stack number must be a positive integer")
        stack_summary = GitHubStackSummary(
            GitHubStackId(repository.identity, stack_number),
            _text(stack_record["id"], "stack node ID"),
            _text(stack_record["baseRefName"], "stack base branch"),
        )
    node_id = _text(raw["id"], "pull request ID")
    identity = PullRequestId(repository.identity, number)
    head_oid = raw["headRefOid"]
    if head_oid is not None:
        head_oid = _text(head_oid, "head commit OID")
    base_oid = raw["baseRefOid"]
    if base_oid is not None:
        base_oid = _text(base_oid, "base commit OID")
    return GitHubPullRequest(
        identity,
        node_id,
        state,
        draft,
        head_repository_identity,
        _text(raw["headRefName"], "head branch"),
        head_oid,
        _text(raw["baseRefName"], "base branch"),
        base_oid,
        auto_merge is not None,
        merge_queue is not None,
        _text(raw["title"], "pull request title"),
        body,
        stack_summary,
    )


def _read_github_pull_request(
    repository: GitHubRepository,
    number: int,
    *,
    cwd: str | Path | None = None,
) -> GitHubPullRequest:
    owner, separator, name = repository.name_with_owner.partition("/")
    if not separator or not owner or not name or "/" in name:
        raise ValueError("repository name must have owner/name form")
    response = _exact_record(
        _github_graphql(
            repository.identity.host,
            _PR_SOURCE_QUERY,
            {"owner": owner, "name": name, "number": number},
            cwd=cwd,
        ),
        {"data"},
        "GitHub GraphQL response",
    )
    data = _exact_record(response["data"], {"repository"}, "GraphQL data")
    raw_repository = data["repository"]
    if raw_repository is None:
        raise IncompleteSource("GitHub repository is unavailable")
    repository_record = _exact_record(
        raw_repository, {"id", "pullRequest"}, "GraphQL repository"
    )
    if (
        _text(repository_record["id"], "GraphQL repository ID")
        != repository.identity.node_id
    ):
        raise SourceMismatch("push destination and GraphQL repository differ")
    pull_request = repository_record["pullRequest"]
    if pull_request is None:
        raise IncompleteSource(f"pull request #{number} is unavailable")
    return _parse_github_pull_request(pull_request, repository, number)


def _github_repository_path(name_with_owner: str) -> str:
    owner, separator, name = name_with_owner.partition("/")
    if not separator or not owner or not name or "/" in name:
        raise ValueError("repository name must have owner/name form")
    return f"repos/{quote(owner, safe='')}/{quote(name, safe='')}"


def _parse_github_stack(
    value: object,
    repository: GitHubRepositoryId,
    expected: GitHubStackId,
) -> GitHubStack:
    raw = _exact_record(
        value,
        {
            "id",
            "number",
            "node_id",
            "url",
            "base",
            "open",
            "created_at",
            "pull_requests",
        },
        "pull request stack",
    )
    if type(raw["id"]) is not int or raw["id"] <= 0:
        raise MalformedSource("stack database ID must be a positive integer")
    if raw["number"] != expected.number:
        raise SourceMismatch("GitHub returned a different stack number")
    if type(raw["open"]) is not bool:
        raise MalformedSource("stack open state must be boolean")
    _text(raw["url"], "stack URL")
    _text(raw["created_at"], "stack creation time")
    base = _exact_record(raw["base"], {"ref"}, "stack base")
    base_branch = _text(base["ref"], "stack base branch")
    members = raw["pull_requests"]
    if not isinstance(members, list) or not members:
        raise IncompleteSource("pull request stack has no members")
    identities: list[PullRequestId] = []
    for member in members:
        item = _exact_record(
            member,
            {"number", "state", "draft", "merged_at", "head"},
            "stack pull request",
        )
        number = item["number"]
        if type(number) is not int or number <= 0:
            raise MalformedSource("stack pull request number must be positive")
        if item["state"] not in {"open", "closed"} or type(item["draft"]) is not bool:
            raise MalformedSource("stack pull request state is malformed")
        if item["merged_at"] is not None and not isinstance(item["merged_at"], str):
            raise MalformedSource("stack pull request merge time is malformed")
        head = _exact_record(item["head"], {"ref", "sha"}, "stack pull request head")
        _text(head["ref"], "stack pull request head branch")
        _text(head["sha"], "stack pull request head OID")
        identities.append(PullRequestId(repository, number))
    if len(set(identities)) != len(identities):
        raise SourceMismatch("stack contains duplicate pull request identities")
    return GitHubStack(
        expected,
        _text(raw["node_id"], "stack node ID"),
        base_branch,
        tuple(identities),
    )


def _parse_stack_summary(
    value: object,
    repository: GitHubRepositoryId,
    expected: GitHubStackId | None = None,
) -> GitHubStackSummary:
    if not isinstance(value, dict):
        raise MalformedSource("stack mutation response must be an object")
    number = value.get("number")
    if type(number) is not int or number <= 0:
        raise MalformedSource("stack mutation number must be a positive integer")
    identity = GitHubStackId(repository, number)
    if expected is not None and identity != expected:
        raise SourceMismatch("stack mutation returned a different stack")
    base = value.get("base")
    if not isinstance(base, dict):
        raise MalformedSource("stack mutation base is malformed")
    return GitHubStackSummary(
        identity,
        _text(value.get("node_id"), "stack mutation node ID"),
        _text(base.get("ref"), "stack mutation base branch"),
    )


def _stack_pull_request_numbers(
    repository: GitHubRepository, pull_requests: Sequence[PullRequestId]
) -> list[int]:
    identities = tuple(pull_requests)
    if not identities or len(set(identities)) != len(identities):
        raise ValueError("stack pull requests must be nonempty and unique")
    if any(identity.repository != repository.identity for identity in identities):
        raise ValueError("stack pull request belongs to another repository")
    return [identity.number for identity in identities]


class GitHubAPI:
    def __init__(self, *, workspace: str | Path) -> None:
        self.workspace = Path(workspace)

    def resolve_repository(
        self, locator: str | GitHubRepositoryId
    ) -> GitHubRepository:
        return _resolve_github_repository(locator, cwd=self.workspace)

    def commit_statuses(
        self,
        repository: GitHubRepository,
        commit_oid: str,
        *,
        contexts: Sequence[str],
    ) -> Mapping[str, CommitStatusState | None]:
        requested = tuple(contexts)
        if any(not context for context in requested):
            raise ValueError("commit status contexts must be nonempty")
        if not requested:
            return {}
        wanted: dict[str, list[str]] = {}
        for context in requested:
            wanted.setdefault(context.casefold(), []).append(context)
        latest: dict[str, CommitStatusState] = {}
        page = 1
        while set(latest) != set(wanted):
            response = github_http_request(
                repository.identity.host,
                "GET",
                f"/{_github_repository_path(repository.name_with_owner)}/commits/"
                f"{quote(commit_oid, safe='')}/statuses?per_page=100&page={page}",
                cwd=self.workspace,
            )
            values = _github_json_response(response, "commit status lookup")
            if not isinstance(values, list):
                raise MalformedSource(
                    "commit status lookup returned a non-array response"
                )
            for value in values:
                if not isinstance(value, dict):
                    raise MalformedSource(
                        "commit status lookup returned a malformed status"
                    )
                context, state = value.get("context"), value.get("state")
                if not isinstance(context, str) or not isinstance(state, str):
                    raise MalformedSource(
                        "commit status lookup returned a malformed status"
                    )
                key = context.casefold()
                if key not in wanted or key in latest:
                    continue
                try:
                    latest[key] = CommitStatusState(state.lower())
                except ValueError as exc:
                    raise MalformedSource(
                        "commit status lookup returned an unknown state"
                    ) from exc
            if len(values) < 100:
                break
            page += 1
        return {context: latest.get(context.casefold()) for context in requested}

    def pull_requests(
        self, identities: Sequence[PullRequestId]
    ) -> tuple[GitHubPullRequest, ...]:
        result = []
        repositories: dict[GitHubRepositoryId, GitHubRepository] = {}
        for identity in identities:
            repository = repositories.get(identity.repository)
            if repository is None:
                repository = self.resolve_repository(identity.repository)
                repositories[identity.repository] = repository
            result.append(
                _read_github_pull_request(
                    repository, identity.number, cwd=self.workspace
                )
            )
        return tuple(result)

    def stack(
        self, repository: GitHubRepository, identity: GitHubStackId
    ) -> GitHubStack | None:
        if identity.repository != repository.identity:
            raise ValueError("stack identity belongs to another repository")
        response = github_http_request(
            repository.identity.host,
            "GET",
            f"/{_github_repository_path(repository.name_with_owner)}/stacks/{identity.number}",
            cwd=self.workspace,
        )
        if response.status == 404:
            return None
        value = _github_json_response(response, "GitHub stack response")
        return _parse_github_stack(value, repository.identity, identity)

    def find_pull_requests(
        self,
        repository: GitHubRepositoryId,
        *,
        head_branches: Sequence[str] = (),
        base_branches: Sequence[str] = (),
        states: Collection[PullRequestState] | None = None,
    ) -> tuple[GitHubPullRequest, ...]:
        heads = tuple(dict.fromkeys(head_branches))
        bases = tuple(dict.fromkeys(base_branches))
        if not heads and not bases:
            raise ValueError("pull request search requires a head or base branch")
        if any(not branch for branch in heads + bases):
            raise ValueError("pull request search branches must be nonempty")
        selected_states = (
            tuple(PullRequestState) if states is None else tuple(dict.fromkeys(states))
        )
        if not selected_states:
            raise ValueError("pull request search states must be nonempty")
        if any(not isinstance(state, PullRequestState) for state in selected_states):
            raise ValueError("pull request search states are invalid")

        resolved = self.resolve_repository(repository)
        owner, separator, name = resolved.name_with_owner.partition("/")
        if not separator or not owner or not name or "/" in name:
            raise ValueError("repository name must have owner/name form")
        declarations = [
            "$owner: String!",
            "$name: String!",
            "$states: [PullRequestState!]!",
        ]
        variables: dict[str, object] = {
            "owner": owner,
            "name": name,
            "states": [state.value for state in selected_states],
        }
        connections: list[str] = []
        aliases: list[str] = []
        for prefix, argument, branches in (
            ("head", "headRefName", heads),
            ("base", "baseRefName", bases),
        ):
            for index, branch in enumerate(branches):
                alias = f"{prefix}{index}"
                declarations.append(f"${alias}: String!")
                variables[alias] = branch
                aliases.append(alias)
                connections.append(
                    f"""
                    {alias}: pullRequests(
                      first: 100, states: $states, {argument}: ${alias}
                    ) {{
                      nodes {{ {_PR_SOURCE_QUERY_FIELDS} }}
                      pageInfo {{ hasNextPage }}
                    }}
                    """
                )
        response = _exact_record(
            _github_graphql(
                repository.host,
                f"""
                query({', '.join(declarations)}) {{
                  repository(owner: $owner, name: $name) {{
                    id
                    {''.join(connections)}
                  }}
                }}
                """,
                variables,
                cwd=self.workspace,
            ),
            {"data"},
            "pull request search response",
        )
        data = _exact_record(response["data"], {"repository"}, "search data")
        if data["repository"] is None:
            raise IncompleteSource("GitHub repository is unavailable")
        raw_repository = _exact_record(
            data["repository"], {"id", *aliases}, "search repository"
        )
        if raw_repository["id"] != repository.node_id:
            raise SourceMismatch("pull request search returned another repository")
        found: dict[PullRequestId, GitHubPullRequest] = {}
        for alias in aliases:
            connection = _exact_record(
                raw_repository[alias], {"nodes", "pageInfo"}, "search connection"
            )
            page = _exact_record(
                connection["pageInfo"], {"hasNextPage"}, "search page info"
            )
            if page["hasNextPage"] is not False:
                raise IncompleteSource("pull request search was truncated")
            nodes = connection["nodes"]
            if not isinstance(nodes, list):
                raise MalformedSource("pull request search nodes must be an array")
            for node in nodes:
                pull_request = _parse_github_pull_request(node, resolved)
                previous = found.get(pull_request.identity)
                if previous is not None and previous != pull_request:
                    raise SourceMismatch(
                        "pull request search returned contradictory observations"
                    )
                found[pull_request.identity] = pull_request
        return tuple(found.values())

    def create_pull_request(
        self,
        repository: GitHubRepository,
        *,
        head_branch: str,
        base_branch: str,
        title: str,
        body: str,
        draft: bool,
    ) -> PullRequestId:
        if not head_branch or not base_branch or not title:
            raise ValueError("pull request branches and title must be nonempty")
        response = github_http_request(
            repository.identity.host,
            "POST",
            f"/{_github_repository_path(repository.name_with_owner)}/pulls",
            body=json.dumps(
                {
                    "head": head_branch,
                    "base": base_branch,
                    "title": title,
                    "body": body,
                    "draft": draft,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode(),
            cwd=self.workspace,
        )
        value = _github_json_response(response, "create pull request response")
        if not isinstance(value, dict):
            raise MalformedSource("create pull request response must be an object")
        number = value.get("number")
        base = value.get("base")
        if type(number) is not int or number <= 0 or not isinstance(base, dict):
            raise MalformedSource("created pull request identity is malformed")
        raw_repository = base.get("repo")
        if not isinstance(raw_repository, dict):
            raise MalformedSource("created pull request repository is malformed")
        if raw_repository.get("node_id") != repository.identity.node_id:
            raise SourceMismatch("GitHub created a pull request in another repository")
        return PullRequestId(repository.identity, number)

    def create_stack(
        self,
        repository: GitHubRepository,
        *,
        pull_requests: Sequence[PullRequestId],
    ) -> GitHubStackSummary:
        numbers = _stack_pull_request_numbers(repository, pull_requests)
        response = github_http_request(
            repository.identity.host,
            "POST",
            f"/{_github_repository_path(repository.name_with_owner)}/stacks",
            body=json.dumps(
                {"pull_requests": numbers}, separators=(",", ":")
            ).encode(),
            cwd=self.workspace,
        )
        return _parse_stack_summary(
            _github_json_response(response, "create stack response"),
            repository.identity,
        )

    def add_stack_members(
        self,
        repository: GitHubRepository,
        identity: GitHubStackId,
        *,
        pull_requests: Sequence[PullRequestId],
    ) -> GitHubStackSummary:
        if identity.repository != repository.identity:
            raise ValueError("stack identity belongs to another repository")
        numbers = _stack_pull_request_numbers(repository, pull_requests)
        response = github_http_request(
            repository.identity.host,
            "POST",
            f"/{_github_repository_path(repository.name_with_owner)}/stacks/{identity.number}/add",
            body=json.dumps(
                {"pull_requests": numbers}, separators=(",", ":")
            ).encode(),
            cwd=self.workspace,
        )
        return _parse_stack_summary(
            _github_json_response(response, "add stack members response"),
            repository.identity,
            identity,
        )

    def unstack(
        self, repository: GitHubRepository, identity: GitHubStackId
    ) -> GitHubStackSummary | None:
        if identity.repository != repository.identity:
            raise ValueError("stack identity belongs to another repository")
        response = github_http_request(
            repository.identity.host,
            "POST",
            f"/{_github_repository_path(repository.name_with_owner)}/stacks/{identity.number}/unstack",
            cwd=self.workspace,
        )
        if response.status == 204 and not response.body:
            return None
        return _parse_stack_summary(
            _github_json_response(response, "unstack response"),
            repository.identity,
            identity,
        )

    def update_pull_request(
        self,
        repository: GitHubRepository,
        identity: PullRequestId,
        *,
        title: str | None = None,
        body: str | None = None,
        base_branch: str | None = None,
        state: PullRequestUpdateState | None = None,
    ) -> None:
        if identity.repository != repository.identity:
            raise ValueError("pull request identity belongs to another repository")
        if title is None and body is None and base_branch is None and state is None:
            raise ValueError("pull request update must change at least one field")
        if base_branch is not None and not base_branch:
            raise ValueError("pull request base branch must be nonempty")
        pull_request = self.pull_requests((identity,))[0]
        declarations = ["$id: ID!"]
        arguments = ["pullRequestId: $id"]
        variables: dict[str, object] = {"id": pull_request.node_id}
        updates = (
            ("title", "String!", title),
            ("body", "String!", body),
            ("baseRefName", "String!", base_branch),
            ("state", "PullRequestState!", state.name if state is not None else None),
        )
        for field, graphql_type, value in updates:
            if value is None:
                continue
            variable = "base" if field == "baseRefName" else field
            declarations.append(f"${variable}: {graphql_type}")
            arguments.append(f"{field}: ${variable}")
            variables[variable] = value
        response = _exact_record(
            _github_graphql(
                repository.identity.host,
                f"""
                mutation({', '.join(declarations)}) {{
                  updatePullRequest(input: {{{', '.join(arguments)}}}) {{
                    pullRequest {{ id number repository {{ id }} }}
                  }}
                }}
                """,
                variables,
                cwd=self.workspace,
            ),
            {"data"},
            "update pull request response",
        )
        data = _exact_record(
            response["data"], {"updatePullRequest"}, "update pull request data"
        )
        payload = _exact_record(
            data["updatePullRequest"], {"pullRequest"}, "update pull request payload"
        )
        updated = _exact_record(
            payload["pullRequest"],
            {"id", "number", "repository"},
            "updated pull request",
        )
        updated_repository = _exact_record(
            updated["repository"], {"id"}, "updated pull request repository"
        )
        if (
            updated["id"] != pull_request.node_id
            or updated["number"] != identity.number
            or updated_repository["id"] != repository.identity.node_id
        ):
            raise SourceMismatch("GitHub updated a different pull request")


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


def observe_commits(
    workspace: str | Path,
    operation: str,
    revision: str,
    *,
    ancestry_order: bool = False,
) -> tuple[ObservedCommit, ...]:
    """Observe only commit records at one already-pinned operation."""
    commit_template = (
        "'{\"commit_id\":' ++ stringify(commit_id).escape_json()"
        " ++ ',\"parent_commit_ids\":' ++ json(parents.map(|p| p.commit_id()))"
        " ++ ',\"change\":' ++ stringify(change_id).escape_json()"
        " ++ ',\"description\":' ++ description.escape_json()"
        " ++ ',\"conflicts\":' ++ conflict"
        " ++ ',\"hidden\":' ++ hidden ++ '}' ++ \"\\n\""
    )
    arguments = ["log", "--no-graph"]
    if ancestry_order:
        arguments.append("--reversed")
    arguments.extend(("-r", revision, "-T", commit_template))
    records = _json_lines(
        _jj(workspace, operation, *arguments),
        "jj log",
    )
    commits = tuple(_commit_record(row) for row in records)
    if len({commit.commit_id for commit in commits}) != len(commits):
        raise Error("duplicate commit in structured output from jj log")
    return commits


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

    git_root = Path(_jj(workspace, operation, "git", "root").strip())
    commits = observe_commits(workspace, operation, revision)
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
            if key in {
                "git.push",
            } and f"Value not found for {key}" in str(exc):
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


def resolve_push_url(local: LocalObservation, remote: str) -> str:
    if not remote:
        raise ValueError("push remote must be nonempty")
    urls = _run(
        [
            "git",
            f"--git-dir={local.git_common_dir}",
            "remote",
            "get-url",
            "--push",
            "--all",
            remote,
        ]
    ).splitlines()
    if len(urls) != 1 or not urls[0]:
        raise SourceMismatch("selected remote does not have one unambiguous push URL")
    return urls[0]


class SubprocessGitTransport:
    """Git transport backed by the workspace's local object store and Git."""

    def __init__(self, workspace: str | Path) -> None:
        self._workspace = workspace

    def observe_live_refs(
        self,
        push_url: str,
        repository: GitHubRepositoryId,
        full_names: Sequence[str],
    ) -> tuple[LiveRemoteRef, ...]:
        if len(set(full_names)) != len(full_names):
            raise ValueError("requested live refs must be unique")
        refs = tuple(RemoteBranchRef(repository, name) for name in full_names)
        result = subprocess.run(
            ["git", "ls-remote", "--heads", push_url, *full_names],
            cwd=self._workspace,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            detail = result.stderr.strip() or result.stdout.strip()
            raise SourceUnavailable(
                "could not read authoritative destination refs"
                + (f": {detail}" if detail else "")
            )
        observed: dict[str, str] = {}
        for row in result.stdout.splitlines():
            fields = row.split("\t")
            if (
                len(fields) != 2
                or not re.fullmatch(r"[0-9a-f]{40}", fields[0])
                or fields[1] not in full_names
                or fields[1] in observed
            ):
                raise MalformedSource("git ls-remote returned an unexpected ref record")
            observed[fields[1]] = fields[0]
        return tuple(LiveRemoteRef(ref, observed.get(ref.full_name)) for ref in refs)

    def push_exact_head_updates(
        self,
        push_url: str,
        updates: Sequence[PlannedHeadUpdate],
    ) -> None:
        if not updates:
            raise ValueError("atomic publication requires at least one update")
        result = subprocess.run(
            [
                "git",
                f"--git-dir={git_common_dir(self._workspace)}",
                "push",
                "--atomic",
                "--no-follow-tags",
                "--recurse-submodules=no",
                *(
                    f"--force-with-lease={u.ref.full_name}:{u.expected_old_commit_id}"
                    for u in updates
                ),
                push_url,
                *(f"{u.new_commit_id}:{u.ref.full_name}" for u in updates),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            detail = result.stderr.strip() or result.stdout.strip()
            raise GitPushError(
                "atomic exact-head push failed"
                + (f": {detail}" if detail else "")
            )

    def push_absent_heads(
        self,
        push_url: str,
        repository: GitHubRepositoryId,
        goals: Sequence[NewPullRequestGoal],
    ) -> None:
        del repository
        if not goals:
            raise ValueError("atomic publication requires at least one update")
        result = subprocess.run(
            [
                "git",
                f"--git-dir={git_common_dir(self._workspace)}",
                "push",
                "--atomic",
                "--no-follow-tags",
                "--recurse-submodules=no",
                *(
                    f"--force-with-lease=refs/heads/{goal.branch_name}:"
                    for goal in goals
                ),
                push_url,
                *(
                    f"{goal.commit_id}:refs/heads/{goal.branch_name}"
                    for goal in goals
                ),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            detail = result.stderr.strip() or result.stdout.strip()
            raise GitPushError(
                "atomic absent-head push failed"
                + (f": {detail}" if detail else "")
            )


def observe_live_refs(
    push_url: str,
    repository: GitHubRepositoryId,
    full_names: Sequence[str],
    *,
    cwd: str | Path | None = None,
) -> tuple[LiveRemoteRef, ...]:
    """Compatibility entry point for direct adapter conformance tests."""
    return SubprocessGitTransport(cwd or Path.cwd()).observe_live_refs(
        push_url, repository, full_names
    )


def observe_snapshot(
    workspace: str | Path,
    github: GitHubClient,
    *,
    revision: str,
    config_keys: Sequence[str],
    remote: str,
    selected_pr_number: int,
    local: LocalObservation | None = None,
    git_transport: GitTransport | None = None,
) -> Snapshot:
    """Assemble complete source facts for one explicitly selected PR's stack."""
    if local is None:
        local = observe_local(workspace, revision=revision, config_keys=config_keys)
    push_url = resolve_push_url(local, remote)
    repository = github.resolve_repository(push_url)
    selected_pr = github.pull_requests(
        (PullRequestId(repository.identity, selected_pr_number),)
    )[0]
    if selected_pr.stack is None:
        pull_requests = (selected_pr,)
        membership: PullRequestMembership = StandalonePullRequest(selected_pr.identity)
        base_branch = selected_pr.base_branch
    else:
        summary = selected_pr.stack
        stack = github.stack(repository, summary.identity)
        if stack is None:
            raise IncompleteSource("pull request stack is unavailable")
        if (
            stack.identity != summary.identity
            or stack.node_id != summary.node_id
            or stack.base_branch != summary.base_branch
            or selected_pr.identity not in stack.pull_requests
        ):
            raise SourceMismatch("selected pull request and complete stack disagree")
        pull_requests = github.pull_requests(stack.pull_requests)
        if any(pr.stack != summary for pr in pull_requests):
            raise SourceMismatch("stack members report different associations")
        membership = ServerStackMembership(
            selected_pr.identity,
            summary,
            stack.pull_requests,
        )
        base_branch = stack.base_branch
    ref_names = tuple(
        dict.fromkeys(
            (
                *(f"refs/heads/{pr.head_branch}" for pr in pull_requests),
                *(f"refs/heads/{pr.base_branch}" for pr in pull_requests),
                f"refs/heads/{base_branch}",
            )
        )
    )
    git = git_transport or SubprocessGitTransport(workspace)
    live_refs = git.observe_live_refs(
        push_url,
        repository.identity,
        ref_names,
    )
    return Snapshot(
        repository.identity,
        push_url,
        remote,
        local,
        observe_tool_state(workspace),
        pull_requests,
        membership,
        live_refs,
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

    def positive_integer(value: object, context: str) -> int:
        if type(value) is not int or value <= 0:
            raise ValueError(f"{context} must be a positive integer")
        return value

    def repository(value: object, context: str) -> GitHubRepositoryId:
        raw = fields(record(value, context), {"host", "node_id"}, context)
        return GitHubRepositoryId(
            text(raw.get("host"), f"{context}.host"),
            text(raw.get("node_id"), f"{context}.node_id"),
        )

    def pull_request(value: object, context: str) -> PullRequestId:
        raw = fields(record(value, context), {"repository", "number"}, context)
        return PullRequestId(
            repository(raw.get("repository"), f"{context}.repository"),
            positive_integer(raw.get("number"), f"{context}.number"),
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

    def last_adopted_head(value: object, index: int) -> LastAdoptedHead:
        context = f"last_adopted_heads[{index}]"
        raw = fields(
            record(value, context), {"pr", "ref", "verified_commit_id"}, context
        )
        ref_context = f"{context}.ref"
        ref = fields(
            record(raw.get("ref"), ref_context),
            {"repository", "full_name"},
            ref_context,
        )
        return LastAdoptedHead(
            pull_request(raw.get("pr"), f"{context}.pr"),
            RemoteBranchRef(
                repository(ref.get("repository"), f"{ref_context}.repository"),
                text(ref.get("full_name"), f"{ref_context}.full_name"),
            ),
            text(raw.get("verified_commit_id"), f"{context}.verified_commit_id"),
        )

    def detached_association(value: object, index: int) -> DetachedAssociation:
        context = f"detached_associations[{index}]"
        raw = fields(record(value, context),
            {"pr", "ref", "verified_commit_id", "disposition"}, context)
        ref_context = f"{context}.ref"
        ref = fields(record(raw.get("ref"), ref_context),
            {"repository", "full_name"}, ref_context)
        return DetachedAssociation(
            pull_request(raw.get("pr"), f"{context}.pr"),
            RemoteBranchRef(repository(ref.get("repository"), f"{ref_context}.repository"),
                text(ref.get("full_name"), f"{ref_context}.full_name")),
            text(raw.get("verified_commit_id"), f"{context}.verified_commit_id"),
            DetachedDisposition(raw.get("disposition")),
        )

    def published_boundary(value: object, index: int) -> LastPublishedBoundary:
        context = f"last_published_boundaries[{index}]"
        raw = fields(record(value, context),
            {"merged_pr", "ref", "verified_commit_id"}, context)
        ref_context = f"{context}.ref"
        ref = fields(record(raw.get("ref"), ref_context),
            {"repository", "full_name"}, ref_context)
        return LastPublishedBoundary(
            pull_request(raw.get("merged_pr"), f"{context}.merged_pr"),
            RemoteBranchRef(repository(ref.get("repository"), f"{ref_context}.repository"),
                text(ref.get("full_name"), f"{ref_context}.full_name")),
            text(raw.get("verified_commit_id"), f"{context}.verified_commit_id"),
        )

    try:
        raw = record(json.loads(data), "root")
        old_fields = {"stacks", "last_published_heads", "last_adopted_heads"}
        optional_fields = {"detached_associations", "last_published_boundaries"}
        if not old_fields <= set(raw) or not set(raw) <= old_fields | optional_fields:
            raise ValueError("root has unexpected fields")
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
        adoptions = tuple(
            last_adopted_head(value, index)
            for index, value in enumerate(
                array(raw.get("last_adopted_heads"), "last_adopted_heads")
            )
        )
        detached = tuple(detached_association(value, index) for index, value in enumerate(
            array(raw.get("detached_associations", []), "detached_associations")))
        boundaries = tuple(published_boundary(value, index) for index, value in enumerate(
            array(raw.get("last_published_boundaries", []), "last_published_boundaries")))
        state = TrackedState(stacks, publications, adoptions, detached, boundaries)
        _validate_state(state)
        return state
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise Error(f"invalid refs/jj-stack/state payload: {exc}") from exc


def _validate_state(state: TrackedState) -> None:
    def require_text(*values: object) -> None:
        if any(not isinstance(value, str) or not value for value in values):
            raise ValueError("state identity fields must be nonempty strings")

    authority_keys: set[tuple[GitHubRepositoryId, PullRequestId, str]] = set()
    memberships: set[tuple[GitHubRepositoryId, PullRequestId]] = set()
    detached_ids: set[PullRequestId] = set()
    for association in state.detached_associations:
        require_text(association.pr.repository.host, association.pr.repository.node_id,
            association.ref.full_name, association.verified_commit_id)
        if association.pr in detached_ids or association.pr.repository != association.ref.repository:
            raise ValueError("ambiguous detached association")
        detached_ids.add(association.pr)
    for stack in state.stacks:
        require_text(
            stack.base_branch,
            stack.repository.host,
            stack.repository.node_id,
        )
        if not stack.ordered_prs:
            raise ValueError("invalid tracked stack")
        for pr in stack.ordered_prs:
            require_text(pr.repository.host, pr.repository.node_id)
            if pr.repository != stack.repository:
                raise ValueError("stack contains a PR from another repository")
            membership = (stack.repository, pr)
            if membership in memberships:
                raise ValueError("overlapping tracked stack membership")
            memberships.add(membership)
    if detached_ids.intersection(pr for _repository, pr in memberships):
        raise ValueError("detached association is still active")
    for publication in (*state.last_published_heads, *state.last_adopted_heads):
        require_text(
            publication.pr.repository.host,
            publication.pr.repository.node_id,
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
        if key in authority_keys:
            raise ValueError("ambiguous head authority")
        authority_keys.add(key)
    if detached_ids.intersection(pr for _repository, pr, _ref in authority_keys):
        raise ValueError("detached ownership is recorded twice")
    boundary_refs: set[RemoteBranchRef] = set()
    for boundary in state.last_published_boundaries:
        require_text(boundary.merged_pr.repository.host,
            boundary.merged_pr.repository.node_id, boundary.ref.full_name,
            boundary.verified_commit_id)
        if (boundary.merged_pr.repository != boundary.ref.repository
                or boundary.ref in boundary_refs):
            raise ValueError("ambiguous boundary authority")
        boundary_refs.add(boundary.ref)


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
        read_ref_oid(workspace, RECOVERY_REF),
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


def recovery_to_json(journal: RecoveryJournal) -> str:
    return json.dumps(
        {"entries": [_recovery_entry_to_dict(entry) for entry in journal.entries]},
        sort_keys=True, separators=(",", ":"),
    ) + "\n"


def _recovery_entry_to_dict(entry: RecoveryEntry) -> dict[str, object]:
    attempt = entry.attempt
    if isinstance(attempt, HeadMutationAttempt):
        payload: dict[str, object] = {
            "kind": "heads",
            "identity": entry.identity,
            "possibly_live": entry.possibly_live,
            "repository": asdict(attempt.repository),
            "push_url": attempt.push_url,
            "updates": [
                {'ref': update.ref.full_name, 'old': update.expected_old_commit_id, 'new': update.new_commit_id, **{}}
                for update in attempt.updates
            ],
            "tracking": {
                "base_branch": attempt.tracking.base_branch,
                "ordered_prs": [pr.number for pr in attempt.tracking.ordered_prs],
            },
            "publications": [
                {
                    "pr": publication.pr.number,
                    "ref": publication.ref.full_name,
                    "commit": publication.verified_commit_id,
                }
                for publication in attempt.publications
            ],
            "boundaries": [
                {"merged_pr": boundary.merged_pr.number,
                 "ref": boundary.ref.full_name,
                 "commit": boundary.verified_commit_id}
                for boundary in attempt.boundaries
            ],
        }
    elif isinstance(attempt, MetadataMutationAttempt):
        payload = {
            "kind": "metadata",
            "identity": entry.identity,
            "possibly_live": entry.possibly_live,
            "repository": asdict(attempt.repository),
            "pr": attempt.update.pr_identity.number,
            "title": attempt.update.title,
            "body": attempt.update.body,
            "expected_title": attempt.expected_title,
            "expected_body": attempt.expected_body,
        }
    elif isinstance(attempt, AdoptionAttempt):
        payload = {"kind": "adoption", "identity": entry.identity,
            "possibly_live": entry.possibly_live, "repository": asdict(attempt.repository),
            "remote": attempt.remote, "state_blob_oid": attempt.state_blob_oid,
            "boundaries": [{"pr": x.pr_identity.number, "branch": x.branch,
                "old_seam": x.old_seam_commit_id, "new_seam": x.new_seam_commit_id,
                "old": x.old_commit_id, "new": x.remote_commit_id} for x in attempt.boundaries]}
    elif isinstance(attempt, BranchCreationAttempt):
        payload = {
            "kind": "create-branches", "identity": entry.identity,
            "possibly_live": entry.possibly_live,
            "repository": asdict(attempt.repository), "push_url": attempt.push_url,
            "goals": [asdict(goal) for goal in attempt.goals],
        }
    elif isinstance(attempt, PullRequestCreationAttempt):
        payload = {
            "kind": "create-pr", "identity": entry.identity,
            "possibly_live": entry.possibly_live,
            "repository": asdict(attempt.repository), "goal": asdict(attempt.goal),
            "pull_request": (
                None if attempt.pull_request is None else attempt.pull_request.number
            ),
        }
    elif isinstance(attempt, StackCreationAttempt):
        payload = {
            "kind": "create-stack", "identity": entry.identity,
            "possibly_live": entry.possibly_live,
            "repository": asdict(attempt.repository),
            "base_branch": attempt.base_branch,
            "pull_requests": [pr.number for pr in attempt.pull_requests],
            "goals": [asdict(goal) for goal in attempt.goals],
        }
    else:
        raise ValueError("unknown recovery attempt")
    return payload


def parse_recovery(data: str) -> RecoveryJournal:
    try:
        raw = json.loads(data)
        if not isinstance(raw, dict) or set(raw) != {"entries"}:
            raise ValueError("root fields are invalid")
        if not isinstance(raw["entries"], list):
            raise ValueError("entries must be an array")
        return RecoveryJournal(tuple(_parse_recovery_entry(item) for item in raw["entries"]))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise Error(f"invalid {RECOVERY_REF} payload: {exc}") from exc


def _parse_recovery_entry(raw: object) -> RecoveryEntry:
    if not isinstance(raw, dict):
        raise ValueError("entry must be an object")
    kind = raw.get("kind")
    common_fields = {"kind", "identity", "possibly_live", "repository"}
    expected_fields = (
        common_fields | {"push_url", "updates", "tracking", "publications", "boundaries"}
        if kind == "heads"
        else common_fields
        | {"pr", "title", "body", "expected_title", "expected_body"}
        if kind == "metadata"
        else common_fields | {"remote", "state_blob_oid", "boundaries"}
        if kind == "adoption"
        else common_fields | {'push_url', 'goals'}
        if kind == 'create-branches'
        else common_fields | {'goal', 'pull_request'}
        if kind == 'create-pr'
        else common_fields | {'base_branch', 'pull_requests', 'goals'}
        if kind == 'create-stack'
        else None
    )
    if expected_fields is None or set(raw) != expected_fields:
        raise ValueError("entry kind or fields are invalid")
    identity = raw["identity"]
    if not isinstance(identity, str):
        raise ValueError("entry identity must be a string")
    repository_raw = raw["repository"]
    if not isinstance(repository_raw, dict) or set(repository_raw) != {
        "host",
        "node_id",
    }:
        raise ValueError("repository must be an object")
    repository = GitHubRepositoryId(
        repository_raw["host"], repository_raw["node_id"]
    )
    possibly_live = raw["possibly_live"]
    if not isinstance(possibly_live, bool):
        raise ValueError("possibly_live must be a boolean")
    if kind == "adoption":
        boundaries_raw = raw["boundaries"]
        if not isinstance(raw["remote"], str) or not isinstance(boundaries_raw, list):
            raise ValueError("adoption attempt is malformed")
        if raw["state_blob_oid"] is not None and not isinstance(raw["state_blob_oid"], str):
            raise ValueError("adoption state OID is malformed")
        fields = {"pr", "branch", "old_seam", "new_seam", "old", "new"}
        boundaries = []
        for value in boundaries_raw:
            if not isinstance(value, dict) or set(value) != fields:
                raise ValueError("adoption boundary is malformed")
            boundaries.append(RemoteRestackBoundary(
                PullRequestId(repository, value["pr"]), value["branch"],
                value["old_seam"], value["new_seam"], value["old"], value["new"]
            ))
        attempt = AdoptionAttempt(
            repository, raw["remote"], raw["state_blob_oid"], tuple(boundaries)
        )
    elif kind == "heads":
        updates_raw = raw["updates"]
        if not isinstance(updates_raw, list):
            raise ValueError("head updates must be an array")
        updates = tuple(
            PlannedHeadUpdate(
                RemoteBranchRef(repository, item["ref"]),
                item["old"],
                item["new"],
                FastForward(),
            )
            for item in updates_raw
            if isinstance(item, dict)
            and (
                set(item) == {"ref", "old", "new"}
                or set(item) == {"ref", "old", "new", "explicit_local_wins"}
                and item["explicit_local_wins"] is True
            )
        )
        if not updates or len(updates) != len(updates_raw):
            raise ValueError("head updates are malformed")
        tracking_raw = raw["tracking"]
        if not isinstance(tracking_raw, dict) or set(tracking_raw) != {
            "base_branch",
            "ordered_prs",
        }:
            raise ValueError("tracking receipt is malformed")
        ordered_prs = tracking_raw["ordered_prs"]
        if not isinstance(ordered_prs, list):
            raise ValueError("tracked pull requests must be an array")
        tracking = TrackedStack(
            repository,
            tracking_raw["base_branch"],
            tuple(
                PullRequestId(repository, number)
                for number in ordered_prs
            ),
        )
        publications_raw = raw["publications"]
        if not isinstance(publications_raw, list):
            raise ValueError("publication receipts must be an array")
        publications = tuple(
            LastPublishedHead(
                PullRequestId(repository, item["pr"]),
                RemoteBranchRef(repository, item["ref"]),
                item["commit"],
            )
            for item in publications_raw
            if isinstance(item, dict) and set(item) == {"pr", "ref", "commit"}
        )
        if len(publications) != len(publications_raw):
            raise ValueError("publication receipts are malformed")
        boundaries_raw = raw["boundaries"]
        if not isinstance(boundaries_raw, list):
            raise ValueError("boundary receipts must be an array")
        boundaries = tuple(
            LastPublishedBoundary(PullRequestId(repository, item["merged_pr"]),
                RemoteBranchRef(repository, item["ref"]), item["commit"])
            for item in boundaries_raw
            if isinstance(item, dict) and set(item) == {"merged_pr", "ref", "commit"}
        )
        if len(boundaries) != len(boundaries_raw):
            raise ValueError("boundary receipts are malformed")
        attempt: MutationAttempt = HeadMutationAttempt(
            repository, raw["push_url"], updates, tracking, publications, boundaries
        )
    elif kind == "metadata":
        attempt = MetadataMutationAttempt(
            repository,
            PRMetadataUpdate(
                PullRequestId(repository, raw["pr"]), raw["title"], raw["body"]
            ),
            raw["expected_title"],
            raw["expected_body"],
        )
    elif kind == "create-branches":
        goals = raw["goals"]
        if not isinstance(goals, list):
            raise ValueError("branch creation goals must be an array")
        parsed_goals = tuple(NewPullRequestGoal(**goal) for goal in goals)
        attempt = BranchCreationAttempt(
            repository, raw["push_url"], parsed_goals
        )
    elif kind == "create-pr":
        goal = raw["goal"]
        if not isinstance(goal, dict) or set(goal) != {
            "commit_id", "branch_name", "base_branch", "title", "body"
        }:
            raise ValueError("pull request creation goal is malformed")
        number = raw["pull_request"]
        if number is not None and not isinstance(number, int):
            raise ValueError("created pull request identity is malformed")
        attempt = PullRequestCreationAttempt(
            repository, NewPullRequestGoal(**goal),
            None if number is None else PullRequestId(repository, number),
        )
    elif kind == "create-stack":
        numbers = raw["pull_requests"]
        goals = raw["goals"]
        if not isinstance(numbers, list) or not isinstance(goals, list):
            raise ValueError("stack creation members must be an array")
        attempt = StackCreationAttempt(
            repository, raw["base_branch"],
            tuple(PullRequestId(repository, number) for number in numbers),
            tuple(NewPullRequestGoal(**goal) for goal in goals),
        )
    else:
        raise ValueError("unknown recovery attempt")
    return RecoveryEntry(identity, attempt, possibly_live)


def read_recovery(workspace: str | Path) -> tuple[str | None, RecoveryJournal | None]:
    oid = read_ref_oid(workspace, RECOVERY_REF)
    if oid is None:
        return None, None
    payload = _run(
        ["git", f"--git-dir={git_common_dir(workspace)}", "cat-file", "blob", oid]
    )
    return oid, parse_recovery(payload)


def cas_write_recovery(
    workspace: str | Path,
    expected_oid: str | None,
    journal: RecoveryJournal | None,
) -> str | None:
    common = git_common_dir(workspace)
    current = read_ref_oid(workspace, RECOVERY_REF)
    if current != expected_oid:
        raise ConcurrentUpdate("recovery journal changed during compare-and-swap")
    expected = expected_oid or ("0" * 40)
    if journal is None:
        if expected_oid is None:
            return None
        command = ["git", f"--git-dir={common}", "update-ref", "-d", RECOVERY_REF, expected]
        new_oid = None
    else:
        new_oid = _run(
            ["git", f"--git-dir={common}", "hash-object", "-w", "--stdin"],
            stdin=recovery_to_json(journal),
        ).strip()
        command = ["git", f"--git-dir={common}", "update-ref", RECOVERY_REF, new_oid, expected]
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode:
        raise ConcurrentUpdate("recovery journal changed during compare-and-swap")
    return new_oid


def _append_recovery(
    workspace: str | Path, attempt: MutationAttempt
) -> tuple[str, RecoveryEntry]:
    oid, journal = read_recovery(workspace)
    entry = RecoveryEntry(os.urandom(16).hex(), attempt)
    updated = RecoveryJournal((journal.entries if journal is not None else ()) + (entry,))
    new_oid = cas_write_recovery(workspace, oid, updated)
    assert new_oid is not None
    return new_oid, entry


def _replace_recovery_entry(
    workspace: str | Path,
    expected_oid: str,
    replacement: RecoveryEntry,
) -> str:
    oid, journal = read_recovery(workspace)
    if oid != expected_oid or journal is None:
        raise ConcurrentUpdate("recovery journal changed during compare-and-swap")
    if sum(entry.identity == replacement.identity for entry in journal.entries) != 1:
        raise ConcurrentUpdate("recovery entry changed during compare-and-swap")
    entries = tuple(
        replacement if entry.identity == replacement.identity else entry
        for entry in journal.entries
    )
    new_oid = cas_write_recovery(workspace, oid, RecoveryJournal(entries))
    assert new_oid is not None
    return new_oid


def _remove_recovery_entry(
    workspace: str | Path, expected_oid: str, identity: str
) -> str | None:
    oid, journal = read_recovery(workspace)
    if oid != expected_oid or journal is None:
        raise ConcurrentUpdate("recovery journal changed during compare-and-swap")
    if sum(entry.identity == identity for entry in journal.entries) != 1:
        raise ConcurrentUpdate("recovery entry changed during compare-and-swap")
    entries = tuple(entry for entry in journal.entries if entry.identity != identity)
    return cas_write_recovery(
        workspace, oid,
        RecoveryJournal(entries)
        if entries else None
    )


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


def _valid_publication_branch(name: str) -> bool:
    """Pure equivalent of check-ref-format for ``refs/heads/<name>``."""
    return not (
        not name
        or name == "@"
        or name.startswith("/")
        or name.endswith(("/", "."))
        or "//" in name
        or ".." in name
        or "@{" in name
        or any(ord(character) < 32 or ord(character) == 127 for character in name)
        or any(character in " ~^:?*[\\" for character in name)
        or any(
            not component or component.startswith(".") or component.endswith(".lock")
            for component in name.split("/")
        )
    )


def resolve_publication_assignments(
    observed: FirstPublicationInput,
) -> tuple[PublicationAssignment, ...] | Blocked:
    """Resolve names solely from supplied evidence, without performing reads."""
    commits = observed.ordered_commit_ids
    if not commits or len(set(commits)) != len(commits):
        return _block(
            "invalid-publication-selection",
            "selection",
            "selected commits must be nonempty and unique",
        )
    if observed.explicit_assignments is not None:
        assignments = observed.explicit_assignments
        if tuple(item.commit_id for item in assignments) != commits:
            return _block(
                "incomplete-explicit-assignment",
                "selection",
                "explicit assignments must exactly cover the ordered selection",
            )
    else:
        templates: dict[str, str] = {}
        for item in observed.template_results:
            if item.commit_id not in commits or item.commit_id in templates:
                return _block(
                    "invalid-template-result",
                    item.commit_id,
                    "template results must uniquely identify selected commits",
                )
            templates[item.commit_id] = item.branch_name
        ineligible = set(observed.managed_branch_names) | {observed.base_branch}
        selected: list[PublicationAssignment] = []
        for commit_id in commits:
            bookmarks = tuple(
                bookmark.name
                for bookmark in observed.local.local_bookmarks
                if bookmark.name not in ineligible
                and isinstance(bookmark.target, CommitTarget)
                and bookmark.target.commit_id == commit_id
            )
            if len(bookmarks) > 1:
                return _block(
                    "ambiguous-local-bookmark",
                    commit_id,
                    "selected commit has multiple eligible exact-target bookmarks",
                )
            name = bookmarks[0] if bookmarks else templates.get(commit_id, "")
            if not name:
                return _block(
                    "template-name-unavailable",
                    commit_id,
                    "no eligible bookmark or evaluated template result is available",
                )
            selected.append(PublicationAssignment(commit_id, name))
        assignments = tuple(selected)

    names = tuple(item.branch_name for item in assignments)
    if len(set(names)) != len(names):
        return _block(
            "duplicate-publication-branch", "selection", "branch names must be unique"
        )
    reserved = set(observed.managed_branch_names) | {observed.base_branch}
    for assignment in assignments:
        name = assignment.branch_name
        if not _valid_publication_branch(name):
            return _block("invalid-publication-branch", name, "branch name is invalid")
        if name in reserved:
            return _block(
                "protected-publication-branch",
                name,
                "target, managed, or protected names cannot be published",
            )
        same_name = tuple(
            bookmark
            for bookmark in observed.local.local_bookmarks
            if bookmark.name == name
        )
        if len(same_name) > 1 or any(
            not isinstance(bookmark.target, CommitTarget)
            or bookmark.target.commit_id != assignment.commit_id
            for bookmark in same_name
        ):
            return _block(
                "local-publication-collision",
                name,
                "local bookmark is deleted, conflicted, or targets another commit",
            )

    destinations: dict[str, LiveRemoteRef] = {}
    for destination in observed.destinations:
        if destination.ref.repository != observed.repository:
            return _block(
                "destination-repository-mismatch",
                destination.ref.full_name,
                "destination belongs to another repository",
            )
        name = destination.ref.full_name.removeprefix("refs/heads/")
        if name in destinations:
            return _block(
                "ambiguous-destination", name, "destination was observed twice"
            )
        destinations[name] = destination
    owned_branches = {
        goal.branch_name: goal.commit_id
        for entry in observed.recovery_facts
        if entry.possibly_live
        and isinstance(entry.attempt, BranchCreationAttempt)
        and entry.attempt.repository == observed.repository
        for goal in entry.attempt.goals
    }
    owned_prs = {
        entry.attempt.pull_request: entry.attempt.goal
        for entry in observed.recovery_facts
        if entry.possibly_live
        and isinstance(entry.attempt, PullRequestCreationAttempt)
        and entry.attempt.repository == observed.repository
        and entry.attempt.pull_request is not None
    }
    for assignment in assignments:
        name = assignment.branch_name
        destination = destinations.get(name)
        if destination is None:
            return _block(
                "destination-unavailable",
                name,
                "confirmed destination absence was not observed",
            )
        owned_commit_id = owned_branches.get(name)
        if owned_commit_id is not None and owned_commit_id != assignment.commit_id:
            return _block(
                "publication-attempt-unresolved",
                name,
                "an earlier candidate for this branch may still land",
            )
        if destination.commit_id is not None and (
            destination.commit_id != assignment.commit_id
            or owned_commit_id != assignment.commit_id
        ):
            return _block(
                "remote-publication-collision",
                name,
                "destination branch already exists",
            )
    for pull_request in observed.historical_pull_requests:
        if pull_request.identity.repository != observed.repository:
            return _block(
                "history-repository-mismatch",
                f"PR #{pull_request.number}",
                "history belongs to another repository",
            )
        for name in names:
            if pull_request.base_branch == name or (
                pull_request.head_repository == observed.repository
                and pull_request.head_branch == name
            ):
                goal = owned_prs.get(pull_request.identity)
                if goal is not None and (
                    pull_request.head_repository == observed.repository
                    and pull_request.head_branch == goal.branch_name
                    and pull_request.head_oid == goal.commit_id
                    and pull_request.base_branch == goal.base_branch
                    and pull_request.title == goal.title
                    and pull_request.body == goal.body
                    and not pull_request.draft
                ):
                    continue
                return _block(
                    "pull-request-branch-collision",
                    name,
                    "branch has historical same-repository pull request use",
                )
    return assignments


def plan_first_publication(
    observed: FirstPublicationInput,
) -> FirstPublicationPlan | Blocked:
    """Produce a deeply immutable first-publication plan; never perform I/O."""
    assignments = resolve_publication_assignments(observed)
    if isinstance(assignments, Blocked):
        return assignments
    commits: dict[str, ObservedCommit] = {}
    for commit in observed.local.commits:
        if commit.commit_id in commits:
            return _block(
                "ambiguous-commit", commit.commit_id, "commit was observed twice"
            )
        commits[commit.commit_id] = commit
    base_ref = f"refs/heads/{observed.base_branch}"
    base_reads = tuple(
        item for item in observed.destinations if item.ref.full_name == base_ref
    )
    if len(base_reads) != 1 or base_reads[0].commit_id is None:
        return _block(
            "base-unavailable",
            observed.base_branch,
            "live base was not observed exactly once",
        )
    base_commit_id = base_reads[0].commit_id
    goals: list[NewPullRequestGoal] = []
    expected_parent = base_commit_id
    expected_base = observed.base_branch
    for assignment in assignments:
        commit = commits.get(assignment.commit_id)
        if (
            commit is None
            or commit.is_hidden
            or commit.has_conflicts
            or not commit.change_id
            or commit.parent_commit_ids != (expected_parent,)
        ):
            return _block(
                "invalid-publication-commit",
                assignment.commit_id,
                "selection must be observed, visible, conflict-free, and one complete linear chain",
            )
        metadata = _metadata(commit.description)
        if metadata is None:
            return _block(
                "title-missing",
                commit.commit_id,
                "selected commit has no description title",
            )
        title, body = metadata
        goals.append(
            NewPullRequestGoal(
                commit.commit_id,
                assignment.branch_name,
                expected_base,
                title,
                body,
            )
        )
        expected_parent = commit.commit_id
        expected_base = assignment.branch_name
    goal = FirstPublicationGoal(
        observed.repository, observed.base_branch, base_commit_id, tuple(goals)
    )
    return FirstPublicationPlan(
        goal,
        observed.local,
        observed.destinations,
        observed.historical_pull_requests,
    )


def _complete_selection(
    snapshot: Snapshot,
    assignments: Sequence[ExistingPRAssignment],
) -> StackSelection | Blocked:
    if isinstance(snapshot.membership, StandalonePullRequest):
        membership = (snapshot.membership.pr,)
        matches = tuple(
            pr for pr in snapshot.pull_requests if pr.identity == snapshot.membership.pr
        )
        if len(matches) != 1:
            return _block(
                "incomplete-membership",
                "stack",
                "standalone membership was not observed exactly once",
            )
        base_branch = matches[0].base_branch
    else:
        membership = snapshot.membership.ordered_prs
        base_branch = snapshot.membership.base_branch
    active = tuple(
        pr.identity
        for pr in snapshot.pull_requests
        if pr.state is PullRequestState.OPEN
    )
    if tuple(item.pr_identity for item in assignments) != active:
        return _block(
            "incomplete-selection",
            "stack",
            "assignments must exactly cover the ordered open membership",
        )
    if tuple(pr.identity for pr in snapshot.pull_requests) != membership:
        return _block(
            "incomplete-membership",
            "stack",
            "observed PRs do not exactly cover membership",
        )
    return StackSelection(base_branch, tuple(assignments))


def select_explicit_stack(
    snapshot: Snapshot, assignments: Sequence[ExistingPRAssignment]
) -> StackSelection | Blocked:
    """Resolve explicit complete intent without silently adding server members."""
    return _complete_selection(snapshot, assignments)


def select_tracked_stack(
    snapshot: Snapshot, assignments: Sequence[ExistingPRAssignment]
) -> StackSelection | Blocked:
    """Resolve implicit intent only from one exact persisted membership."""
    selected = _complete_selection(snapshot, assignments)
    if isinstance(selected, Blocked):
        return selected
    membership = (
        (snapshot.membership.pr,)
        if isinstance(snapshot.membership, StandalonePullRequest)
        else snapshot.membership.ordered_prs
    )
    matches = tuple(
        stack
        for stack in snapshot.tool_state.state.stacks
        if stack.repository == snapshot.repository
        and stack.base_branch == selected.base_branch
        and stack.ordered_prs == membership
    )
    if len(matches) != 1:
        return _block(
            "untracked-membership",
            "stack",
            "implicit selection requires one exact tracked membership",
        )
    return selected


def derive_desired(
    snapshot: Snapshot, selection: StackSelection
) -> DesiredStack | Blocked:
    desired: list[DesiredExistingPR] = []
    for assignment in selection.ordered:
        matches = tuple(
            pr for pr in snapshot.pull_requests if pr.identity == assignment.pr_identity
        )
        if len(matches) != 1:
            return _block(
                "selected-pr-unavailable",
                "selected PR",
                "assigned PR was not observed exactly once",
            )
        pr = matches[0]
        commits = tuple(
            item
            for item in snapshot.local.commits
            if item.commit_id == assignment.commit_id
        )
        if len(commits) != 1:
            return _block(
                "commit-unobserved",
                assignment.commit_id,
                "assigned commit was not observed exactly once",
            )
        metadata = _metadata(commits[0].description)
        if metadata is None:
            return _block(
                "title-missing",
                commits[0].commit_id,
                "selected revision has no description title",
            )
        title, body = metadata
        desired.append(
            DesiredExistingPR(pr.identity, commits[0].commit_id, title, body)
        )
    return DesiredStack(
        snapshot.repository,
        selection.base_branch,
        tuple(desired),
    )


def _dependencies(
    snapshot: Snapshot,
    prs: tuple[GitHubPullRequest, ...],
    heads: tuple[LiveRemoteRef, ...],
    bases: tuple[LiveRemoteRef, ...],
) -> Dependencies:
    return Dependencies(
        tuple(
            item for item in snapshot.local.effective_config if item[0] != "git.push"
        ),
        snapshot.push_url,
        snapshot.remote,
        snapshot.tool_state.state_blob_oid,
        snapshot.tool_state.recovery_blob_oid,
        prs,
        snapshot.membership,
        heads,
        bases,
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
) -> tuple[GitHubPullRequest, ...] | Blocked:
    if desired.repository != snapshot.repository:
        return _block(
            "repository-mismatch",
            "repository",
            "desired repository differs from the observed repository",
        )
    observed = {pr.identity: pr for pr in snapshot.pull_requests}
    if len(observed) != len(snapshot.pull_requests):
        return _block(
            "ambiguous-membership",
            "stack",
            "pull request observations contain duplicate identities",
        )
    if isinstance(snapshot.membership, StandalonePullRequest):
        ordered_members = (snapshot.membership.pr,)
        observed_base = observed.get(snapshot.membership.pr)
        base_branch = observed_base.base_branch if observed_base is not None else None
    else:
        ordered_members = snapshot.membership.ordered_prs
        base_branch = snapshot.membership.base_branch
    if tuple(observed) != ordered_members:
        return _block(
            "incomplete-membership",
            "stack",
            "observed PRs do not exactly match complete ordered membership",
        )
    if desired.base_branch != base_branch:
        return _block(
            "target-mismatch",
            desired.base_branch,
            "desired target differs from the observed stack base branch",
        )
    seen_open = False
    for pr in snapshot.pull_requests:
        if (
            pr.identity.repository != snapshot.repository
            or pr.head_repository != snapshot.repository
        ):
            return _block(
                "nonlocal-pr",
                f"PR #{pr.number}",
                "planner requires repository-local PRs",
            )
        if pr.state is PullRequestState.OPEN:
            seen_open = True
        elif pr.state is PullRequestState.CLOSED or seen_open:
            return _block(
                "unsupported-member-state",
                f"PR #{pr.number}",
                "membership must be a merged prefix followed by open PRs",
            )
    active = tuple(
        pr for pr in snapshot.pull_requests if pr.state is PullRequestState.OPEN
    )
    if tuple(item.pr_identity for item in desired.active) != tuple(
        pr.identity for pr in active
    ):
        return _block(
            "incomplete-selection",
            "stack",
            "desired assignments must exactly cover the ordered open membership",
        )
    return active


def _validate_pr_dependencies(
    snapshot: Snapshot,
    desired: DesiredStack,
    prs: tuple[GitHubPullRequest, ...],
) -> (
    tuple[
        tuple[LiveRemoteRef, ...],
        tuple[LiveRemoteRef, ...],
        tuple[LiveRemoteRef, ...],
    ]
    | Blocked
):
    base_name = f"refs/heads/{desired.base_branch}"
    base_ref = RemoteBranchRef(snapshot.repository, base_name)
    bases = tuple(value for value in snapshot.live_refs if value.ref == base_ref)
    if len(bases) != 1 or bases[0].commit_id is None:
        return _block(
            "base-disagrees",
            base_name,
            "authoritative stack base ref is absent or ambiguous",
        )
    all_heads: list[LiveRemoteRef] = []
    all_bases: list[LiveRemoteRef] = []
    heads_by_pr: dict[PullRequestId, LiveRemoteRef] = {}
    previous_pr: GitHubPullRequest | None = None
    for pr in snapshot.pull_requests:
        head_name = f"refs/heads/{pr.head_branch}"
        head_ref = RemoteBranchRef(snapshot.repository, head_name)
        heads = tuple(value for value in snapshot.live_refs if value.ref == head_ref)
        if len(heads) != 1 or heads[0].commit_id != pr.head_oid:
            return _block(
                "head-disagrees",
                head_name,
                "GitHub PR head and authoritative live ref disagree",
            )
        expected_base_branch = (
            previous_pr.head_branch if previous_pr is not None else desired.base_branch
        )
        if pr.base_branch != expected_base_branch:
            return _block(
                "topology-changed",
                f"PR #{pr.number}",
                "PR literal base does not match unchanged stack order",
            )
        pr_base_name = f"refs/heads/{pr.base_branch}"
        pr_base_ref = RemoteBranchRef(snapshot.repository, pr_base_name)
        pr_bases = tuple(
            value for value in snapshot.live_refs if value.ref == pr_base_ref
        )
        if len(pr_bases) != 1 or pr_bases[0].commit_id != pr.base_oid:
            return _block(
                "base-disagrees",
                pr_base_name,
                "GitHub PR base and authoritative live ref disagree",
            )
        all_heads.append(heads[0])
        if pr_bases[0] not in all_bases:
            all_bases.append(pr_bases[0])
        heads_by_pr[pr.identity] = heads[0]
        previous_pr = pr
    merged = tuple(
        pr for pr in snapshot.pull_requests if pr.state is PullRequestState.MERGED
    )
    comparison_base = merged[-1].head_oid if merged else bases[0].commit_id
    active_heads: list[LiveRemoteRef] = []
    for pr, wanted in zip(prs, desired.active, strict=True):
        commits = tuple(
            value
            for value in snapshot.local.commits
            if value.commit_id == wanted.desired_commit_id
        )
        if len(commits) != 1 or commits[0].has_conflicts:
            return _block(
                "commit-conflicted",
                wanted.desired_commit_id,
                "desired commit is absent, duplicated, or conflicted",
            )
        if not _comparison_is_nonempty_linear_and_conflict_free(
            snapshot.local.commits,
            comparison_base,
            wanted.desired_commit_id,
        ):
            return _block(
                "invalid-comparison",
                wanted.desired_commit_id,
                "desired predecessor-to-head segment is empty, nonlinear, incomplete, or conflicted",
            )
        active_heads.append(heads_by_pr[pr.identity])
        comparison_base = wanted.desired_commit_id
    return (
        tuple(active_heads),
        tuple(all_heads),
        tuple(all_bases),
    )


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
    adoptions = tuple(
        item
        for item in snapshot.tool_state.state.last_adopted_heads
        if item.pr == wanted.pr_identity
        and item.ref == head.ref
        and item.verified_commit_id == head.commit_id
    )
    if len(publications) + len(adoptions) != 1:
        return _block(
            "replacement-unauthorized",
            head.ref.full_name,
            "live head is neither an ancestor nor one unambiguous verified authority",
        )
    authority: Authority = (
        MatchesLastPublication(publications[0])
        if publications
        else MatchesLastAdoption(adoptions[0])
    )
    return (
        PlannedHeadUpdate(
            head.ref,
            head.commit_id,
            wanted.desired_commit_id,
            authority,
        ),
    )


def _metadata_updates(
    pr: GitHubPullRequest, wanted: DesiredExistingPR
) -> tuple[PRMetadataUpdate, ...]:
    if (pr.title, pr.body) == (wanted.title, wanted.body):
        return ()
    return (PRMetadataUpdate(pr.identity, wanted.title, wanted.body),)


def _one_bookmark_target(
    local: LocalObservation, remote: str | None, name: str
) -> str | None:
    targets = (
        (item.target for item in local.local_bookmarks if remote is None and item.name == name)
        if remote is None
        else (
            item.target
            for item in local.remote_bookmarks
            if item.remote == remote
            and item.name == name
            and item.tracking_state is TrackingState.TRACKED
        )
    )
    values = tuple(targets)
    if len(values) != 1 or not isinstance(values[0], CommitTarget):
        return None
    return values[0].commit_id


def plan_remote_restack_adoption(snapshot: Snapshot) -> RemoteRestackAdoption | Blocked:
    """Recognize an externally moved, already-tracked complete stack."""
    membership = tuple(pr.identity for pr in snapshot.pull_requests)
    tracked = tuple(
        stack
        for stack in snapshot.tool_state.state.stacks
        if stack.repository == snapshot.repository
        and stack.base_branch
        == (
            snapshot.membership.base_branch
            if isinstance(snapshot.membership, ServerStackMembership)
            else snapshot.pull_requests[0].base_branch
        )
        and stack.ordered_prs == membership
    )
    if len(tracked) != 1:
        return _block("untracked-membership", "stack", "exact complete membership is not tracked")
    live = {ref.ref.full_name: ref.commit_id for ref in snapshot.live_refs}
    boundaries: list[RemoteRestackBoundary] = []
    previous_old: str | None = None
    previous_new: str | None = None
    base_ref = RemoteBranchRef(
        snapshot.repository, f"refs/heads/{tracked[0].base_branch}"
    )
    boundary_evidence = tuple(
        item.verified_commit_id
        for item in snapshot.tool_state.state.last_published_boundaries
        if item.ref == base_ref
    )
    old_base_seam = _one_bookmark_target(snapshot.local, None, tracked[0].base_branch)
    if old_base_seam is None and len(boundary_evidence) == 1:
        old_base_seam = boundary_evidence[0]
    if old_base_seam is None:
        old_base_seam = _one_bookmark_target(
            snapshot.local, snapshot.remote, tracked[0].base_branch
        )
    if old_base_seam is None:
        return _block(
            "boundary-unavailable", tracked[0].base_branch,
            "tracked base has no unique local boundary",
        )
    for pr in snapshot.pull_requests:
        if pr.state is not PullRequestState.OPEN:
            continue
        local = _one_bookmark_target(snapshot.local, None, pr.head_branch)
        baseline = _one_bookmark_target(snapshot.local, snapshot.remote, pr.head_branch)
        remote = live.get(f"refs/heads/{pr.head_branch}")
        if local is None or baseline is None or remote is None or remote != pr.head_oid:
            return _block("boundary-unavailable", pr.head_branch, "L, T, PR, and live head must agree uniquely")
        if local != baseline:
            return _block("mixed-movement", pr.head_branch, "local and remote movement cannot be combined")
        old = local
        if local == remote:
            ref = RemoteBranchRef(
                snapshot.repository, f"refs/heads/{pr.head_branch}"
            )
            prior = tuple(
                item.verified_commit_id
                for item in (
                    snapshot.tool_state.state.last_published_heads
                    + snapshot.tool_state.state.last_adopted_heads
                )
                if item.pr == pr.identity
                and item.ref == ref
                and item.verified_commit_id != remote
            )
            if len(prior) > 1:
                return _block(
                    "ambiguous-head-authority",
                    pr.head_branch,
                    "converged bookmarks have multiple prior authorities",
                )
            if prior:
                old = prior[0]
        if previous_old is None:
            # The local tracked-base bookmark is explicit pre-fetch boundary
            # evidence.  Do not infer this seam from however many ancestors the
            # caller happened to include in its local commit observation.
            old_seam = old_base_seam
        else:
            old_seam = previous_old
        new_seam = previous_new or pr.base_oid
        if not new_seam:
            return _block("boundary-unavailable", pr.head_branch, "new segment seam is absent")
        boundaries.append(
            RemoteRestackBoundary(
                pr.identity, pr.head_branch, old_seam, new_seam, old, remote
            )
        )
        previous_old = old
        previous_new = remote
    if not boundaries or all(item.old_commit_id == item.remote_commit_id for item in boundaries):
        return _block("remote-unchanged", "stack", "no external restack is present")
    # A restack is stack-wide: unchanged boundaries interspersed with changed ones
    # indicate a partial rewrite, not a coherent replacement.
    changed = tuple(item.old_commit_id != item.remote_commit_id for item in boundaries)
    if not all(changed):
        return _block("partial-restack", "stack", "not every open boundary was rewritten")
    return RemoteRestackAdoption(
        snapshot.repository,
        snapshot.remote,
        snapshot.tool_state.state_blob_oid,
        tuple(boundaries),
    )


def _commit_fingerprint(git_dir: str | Path, commit_id: str) -> tuple[str, str]:
    payload = _run(["git", f"--git-dir={git_dir}", "cat-file", "-p", commit_id])
    header = payload.partition("\n\n")[0]
    changes = tuple(
        line.removeprefix("change-id ")
        for line in header.splitlines()
        if line.startswith("change-id ")
    )
    if len(changes) != 1 or not changes[0]:
        raise SourceMismatch(f"commit {commit_id} has no unique raw jj change ID")
    patch = _run(
        ["git", f"--git-dir={git_dir}", "show", "--pretty=format:", "--no-ext-diff", "--binary", commit_id]
    )
    fields = _run(["git", "patch-id", "--stable"], stdin=patch).split()
    if len(fields) != 2 or re.fullmatch(r"[0-9a-f]{40}", fields[0]) is None:
        raise SourceMismatch(f"commit {commit_id} has no stable patch ID")
    return changes[0], fields[0]


def _linear_segment(git_dir: str | Path, base: str, head: str) -> tuple[str, ...]:
    commits = tuple(_run(["git", f"--git-dir={git_dir}", "rev-list", "--reverse", "--ancestry-path", f"{base}..{head}"]).splitlines())
    predecessor = base
    for commit in commits:
        row = _run(["git", f"--git-dir={git_dir}", "rev-list", "--parents", "-n", "1", commit]).split()
        if len(row) != 2 or row[1] != predecessor:
            raise SourceMismatch("restack segment is not a complete linear chain")
        predecessor = commit
    if not commits or commits[-1] != head:
        raise SourceMismatch("restack segment is empty or disconnected")
    return commits


def plan_sync(
    snapshot: Snapshot,
    desired: DesiredStack | Blocked,
) -> SyncPlan:
    if isinstance(desired, Blocked):
        return desired
    prs = _validate_plan_scope(snapshot, desired)
    if isinstance(prs, Blocked):
        return prs
    pr_dependencies = _validate_pr_dependencies(snapshot, desired, prs)
    if isinstance(pr_dependencies, Blocked):
        return pr_dependencies
    heads, all_heads, all_bases = pr_dependencies
    planned_heads: list[PlannedHeadUpdate] = []
    planned_metadata: list[PRMetadataUpdate] = []
    for pr, wanted, head in zip(prs, desired.active, heads, strict=True):
        head_updates = _plan_publication(snapshot, wanted, head)
        if isinstance(head_updates, Blocked):
            return head_updates
        planned_heads.extend(head_updates)
        planned_metadata.extend(_metadata_updates(pr, wanted))
    head_updates = tuple(planned_heads)
    metadata = tuple(planned_metadata)
    dependencies = _dependencies(snapshot, snapshot.pull_requests, all_heads, all_bases)
    if not head_updates and not metadata:
        return NoOp(desired, dependencies)
    for pr in prs:
        if pr.auto_merge_enabled or pr.in_merge_queue:
            return _block(
                "active-automation",
                f"PR #{pr.number}",
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
    pr_numbers = {pr.identity: pr.number for pr in plan.dependencies.prs}
    consequences = [
        *(
            (
                f"publish {item.new_commit_id} to {item.ref.full_name}"
            )
            for item in plan.head_updates
        ),
        *(
            f"update metadata for PR #{pr_numbers[item.pr_identity]}"
            for item in plan.metadata_updates
        ),
    ]
    return "apply:\n" + "\n".join(f"  - {item}" for item in consequences)


def push_exact_head_updates(
    workspace: str | Path,
    updates: Sequence[PlannedHeadUpdate],
    push_url: str,
) -> None:
    """Compatibility entry point for direct adapter conformance tests."""
    SubprocessGitTransport(workspace).push_exact_head_updates(push_url, updates)


def _classify_attempt(
    workspace: str | Path,
    github: GitHubClient,
    attempt: MutationAttempt,
    git_transport: GitTransport | None = None,
) -> str:
    """Return applied/not-applied/foreign; read failures remain unresolved."""
    git = git_transport or SubprocessGitTransport(workspace)
    if isinstance(attempt, HeadMutationAttempt):
        observed = git.observe_live_refs(
            attempt.push_url,
            attempt.repository,
            tuple(update.ref.full_name for update in attempt.updates),
        )
        actual = tuple(item.commit_id for item in observed)
        wanted = tuple(item.new_commit_id for item in attempt.updates)
        old = tuple(item.expected_old_commit_id for item in attempt.updates)
        if actual == wanted:
            return "applied"
        if actual == old:
            return "not-applied"
        return "foreign"
    if isinstance(attempt, BranchCreationAttempt):
        observed = git.observe_live_refs(
            attempt.push_url, attempt.repository,
            tuple(f"refs/heads/{goal.branch_name}" for goal in attempt.goals),
        )
        actual = tuple(item.commit_id for item in observed)
        wanted = tuple(goal.commit_id for goal in attempt.goals)
        if actual == wanted:
            return "applied"
        if actual == (None,) * len(wanted):
            return "not-applied"
        return "foreign"
    if isinstance(attempt, PullRequestCreationAttempt):
        goal = attempt.goal
        matches = github.find_pull_requests(
            attempt.repository,
            head_branches=(goal.branch_name,),
            states=tuple(PullRequestState),
        )
        exact = tuple(
            pr for pr in matches
            if pr.head_repository == attempt.repository
            and pr.head_branch == goal.branch_name
            and pr.head_oid == goal.commit_id
            and pr.base_branch == goal.base_branch
            and pr.title == goal.title and pr.body == goal.body and not pr.draft
        )
        if (
            len(exact) == 1 and len(matches) == 1
            and (attempt.pull_request is None or exact[0].identity == attempt.pull_request)
        ):
            return "applied"
        return "not-applied" if not matches else "foreign"
    if isinstance(attempt, StackCreationAttempt):
        prs = github.pull_requests(attempt.pull_requests)
        summaries = {pr.stack for pr in prs}
        if summaries == {None}:
            return "not-applied"
        if len(summaries) != 1 or None in summaries:
            return "foreign"
        summary = next(iter(summaries))
        assert summary is not None
        repository = github.resolve_repository(attempt.repository)
        stack = github.stack(repository, summary.identity)
        return (
            "applied"
            if stack is not None
            and stack.base_branch == attempt.base_branch
            and stack.pull_requests == attempt.pull_requests
            else "foreign"
        )
    assert isinstance(attempt, MetadataMutationAttempt)
    prs = github.pull_requests((attempt.update.pr_identity,))
    if len(prs) != 1:
        return "foreign"
    metadata = (prs[0].title, prs[0].body)
    if metadata == (attempt.update.title, attempt.update.body):
        return "applied"
    if metadata == (attempt.expected_title, attempt.expected_body):
        return "not-applied"
    return "foreign"


def settle_recovery(
    workspace: str | Path,
    github: GitHubClient,
    *,
    dry_run: bool = False,
    git_transport: GitTransport | None = None,
) -> tuple[RecoveryEntry, ...]:
    """Retire resolved facts and return only effects that can still conflict."""
    oid, journal = read_recovery(workspace)
    if journal is None:
        return ()
    remaining: list[RecoveryEntry] = []
    applied_heads: list[HeadMutationAttempt] = []
    applied_stacks: list[StackCreationAttempt] = []
    for entry in journal.entries:
        # Obligation effects are durable facts until the core receipt handoff.
        # The generic driver, not legacy settlement, reconciles or retires them.
        if not entry.possibly_live:
            continue
        if isinstance(entry.attempt, AdoptionAttempt):
            attempt = entry.attempt
            plan = RemoteRestackAdoption(
                attempt.repository, attempt.remote, attempt.state_blob_oid,
                attempt.boundaries,
            )
            try:
                after = _observe_adoption(
                    workspace,
                    github,
                    plan,
                    include_remote=True,
                    git_transport=git_transport,
                )
                if _verify_adoption(after, after, plan, recovery=True) is not None:
                    remaining.append(entry)
                    continue
                receipts = tuple(
                    LastAdoptedHead(
                        item.pr_identity,
                        RemoteBranchRef(attempt.repository, f"refs/heads/{item.branch}"),
                        item.remote_commit_id,
                    )
                    for item in attempt.boundaries
                )
                if not dry_run:
                    current_oid, current = read_state(workspace)
                    keys = {(item.pr, item.ref) for item in receipts}
                    exact = {
                        (item.pr, item.ref): item for item in current.last_adopted_heads
                        if (item.pr, item.ref) in keys
                    }
                    if tuple(exact.get((item.pr, item.ref)) for item in receipts) != receipts:
                        # Recovery may only consume the state version frozen by
                        # the attempt.  A later unrelated state cannot be
                        # overwritten using the repeatedly stale original CAS.
                        if current_oid != attempt.state_blob_oid:
                            remaining.append(entry)
                            continue
                        _record_adoptions(
                            workspace, current_oid, current, receipts
                        )
            except Error:
                remaining.append(entry)
            continue
        try:
            classification = _classify_attempt(
                workspace, github, entry.attempt, git_transport
            )
        except Error:
            remaining.append(entry)
            continue
        if classification != "applied":
            remaining.append(entry)
            continue
        if isinstance(entry.attempt, HeadMutationAttempt):
            applied_heads.append(entry.attempt)
        elif isinstance(entry.attempt, StackCreationAttempt):
            applied_stacks.append(entry.attempt)
        elif isinstance(entry.attempt, PullRequestCreationAttempt):
            goal = entry.attempt.goal
            matches = github.find_pull_requests(
                entry.attempt.repository, head_branches=(goal.branch_name,),
                states=tuple(PullRequestState),
            )
            # Classification established that this is the sole exact match.
            remaining.append(replace(
                entry,
                attempt=replace(entry.attempt, pull_request=matches[0].identity),
            ))
        elif isinstance(entry.attempt, BranchCreationAttempt):
            remaining.append(entry)
    if not dry_run:
        for attempt in applied_heads:
            _state_oid, current = read_state(workspace)
            if (set(attempt.publications).issubset(current.last_published_heads)
                    and set(attempt.boundaries).issubset(
                        current.last_published_boundaries
                    )):
                # A later core handoff may have absorbed this exact head
                # authority into newer membership before journal retirement.
                # Never restore the attempt's deliberately provisional
                # tracking over that committed result.
                continue
            _record_verified_state(
                workspace,
                attempt.tracking,
                attempt.publications,
                attempt.boundaries,
            )
        for attempt in applied_stacks:
            _record_verified_state(
                workspace,
                TrackedStack(attempt.repository, attempt.base_branch, attempt.pull_requests),
                tuple(
                    LastPublishedHead(
                        pr, RemoteBranchRef(attempt.repository, f"refs/heads/{goal.branch_name}"),
                        goal.commit_id,
                    )
                    for pr, goal in zip(attempt.pull_requests, attempt.goals, strict=True)
                ),
            )
            goals = set(attempt.goals)
            members = set(attempt.pull_requests)
            remaining = [
                entry for entry in remaining
                if not (
                    isinstance(entry.attempt, BranchCreationAttempt)
                    and entry.attempt.repository == attempt.repository
                    and set(entry.attempt.goals) == goals
                    or isinstance(entry.attempt, PullRequestCreationAttempt)
                    and entry.attempt.repository == attempt.repository
                    and entry.attempt.goal in goals
                    and entry.attempt.pull_request in members
                )
            ]
        updated = tuple(remaining)
        if updated != journal.entries:
            cas_write_recovery(
                workspace,
                oid,
                RecoveryJournal(updated)
                if updated else None,
            )
    return tuple(remaining)


def _attempt_conflicts_plan(entry: RecoveryEntry, plan: NoOp | Apply) -> bool:
    attempt = entry.attempt
    # Scope recovery to complete logical membership and all observed refs;
    # omitted and merged members can still carry unresolved writes.
    active_prs = {pr.identity for pr in plan.dependencies.prs}
    if isinstance(attempt, MetadataMutationAttempt):
        return attempt.update.pr_identity in active_prs
    active_refs = {
        item.ref
        for item in plan.dependencies.live_heads + plan.dependencies.live_bases
    } | {
        RemoteBranchRef(plan.desired.repository, f"refs/heads/{pr.head_branch}")
        for pr in plan.dependencies.prs
    }
    if isinstance(attempt, AdoptionAttempt):
        adoption_prs = {boundary.pr_identity for boundary in attempt.boundaries}
        adoption_refs = {
            RemoteBranchRef(attempt.repository, f"refs/heads/{boundary.branch}")
            for boundary in attempt.boundaries
        }
        return bool(active_prs.intersection(adoption_prs)
                    or active_refs.intersection(adoption_refs))
    if isinstance(attempt, HeadMutationAttempt):
        return bool(active_refs.intersection(update.ref for update in attempt.updates))
    if isinstance(attempt, BranchCreationAttempt):
        refs = {
            RemoteBranchRef(
                attempt.repository, f"refs/heads/{goal.branch_name}"
            )
            for goal in attempt.goals
        }
        return bool(active_refs.intersection(refs))
    if isinstance(attempt, PullRequestCreationAttempt):
        ref = RemoteBranchRef(
            attempt.repository, f"refs/heads/{attempt.goal.branch_name}"
        )
        return ref in active_refs or attempt.pull_request in active_prs
    if isinstance(attempt, StackCreationAttempt):
        members = attempt.pull_requests
    else:
        raise AssertionError(f"unhandled recovery attempt: {type(attempt).__name__}")
    return bool(active_prs.intersection(members))


def _plan_tracking(plan: NoOp | Apply) -> TrackedStack:
    membership = plan.dependencies.membership
    ordered = (
        (membership.pr,)
        if isinstance(membership, StandalonePullRequest)
        else membership.ordered_prs
    )
    return TrackedStack(plan.desired.repository, plan.desired.base_branch, ordered)


def _plan_publications(plan: Apply) -> tuple[LastPublishedHead, ...]:
    prs_by_ref = {
        RemoteBranchRef(plan.desired.repository, f"refs/heads/{pr.head_branch}"): pr
        for pr in plan.dependencies.prs
    }
    return tuple(
        LastPublishedHead(
            prs_by_ref[update.ref].identity,
            update.ref,
            update.new_commit_id,
        )
        for update in plan.head_updates
        if update.ref in prs_by_ref
        and not any(boundary.ref == update.ref for boundary in plan.boundary_receipts)
    )


_UNSPECIFIED_OID = object()


def _record_verified_state(
    workspace: str | Path,
    tracking: TrackedStack,
    publications: tuple[LastPublishedHead, ...],
    boundaries: tuple[LastPublishedBoundary, ...] = (),
    *,
    expected_oid: str | None | object = _UNSPECIFIED_OID,
) -> bool:
    state_oid, state = read_state(workspace)
    if expected_oid is not _UNSPECIFIED_OID and state_oid != expected_oid:
        raise ConcurrentUpdate("tracked state changed before receipt handoff")
    members = set(tracking.ordered_prs)
    stacks = tuple(
        stack
        for stack in state.stacks
        if stack.repository != tracking.repository
        or not members.intersection(stack.ordered_prs)
    ) + (tracking,)
    keys = {(item.pr, item.ref) for item in publications}
    recorded_publications = tuple(
        item
        for item in state.last_published_heads
        if (item.pr, item.ref) not in keys
        and not any(
            boundary.merged_pr == item.pr and boundary.ref == item.ref
            for boundary in boundaries
        )
    ) + publications
    recorded_adoptions = tuple(
        item
        for item in state.last_adopted_heads
        if (item.pr, item.ref) not in keys
    )
    updated = TrackedState(
        stacks,
        recorded_publications,
        recorded_adoptions,
        state.detached_associations,
        tuple(item for item in state.last_published_boundaries
            if item.ref not in {boundary.ref for boundary in boundaries}) + boundaries,
    )
    if updated == state:
        return False
    cas_write_state(workspace, state_oid, updated)
    return True


def _revalidate_sync_dependencies(
    workspace: str | Path, github: GitHubClient, plan: NoOp | Apply
) -> None:
    """Revalidate mutation authority immediately before journaling/sending."""
    dependencies = plan.dependencies
    state_oid, _state = read_state(workspace)
    if state_oid != dependencies.state_blob_oid:
        raise ConcurrentUpdate("tracked state changed since confirmation")
    current_prs = github.pull_requests(tuple(pr.identity for pr in dependencies.prs))
    if current_prs != dependencies.prs:
        raise ConcurrentUpdate("pull request state or topology changed since confirmation")


def _observe_adoption(
    workspace: str | Path,
    github: GitHubClient,
    plan: RemoteRestackAdoption,
    *,
    include_remote: bool,
    git_transport: GitTransport | None = None,
) -> Snapshot:
    revision = " | ".join(
        (
            f"{boundary.old_commit_id}:: | {boundary.remote_commit_id}::"
            if include_remote
            else f"{boundary.old_commit_id}::"
        )
        for boundary in plan.boundaries
    )
    return observe_snapshot(
        workspace,
        github,
        revision=revision,
        config_keys=("git.push",),
        remote=plan.remote,
        selected_pr_number=plan.boundaries[0].pr_identity.number,
        git_transport=git_transport,
    )


def _verify_adoption(
    before: Snapshot, after: Snapshot, plan: RemoteRestackAdoption, *, recovery: bool = False
) -> str | None:
    current = plan_remote_restack_adoption(before) if not recovery else plan
    if current != plan:
        return "adoption inputs no longer describe the same restack"
    if after.repository != plan.repository or after.membership != before.membership:
        return "repository or complete membership changed during adoption"
    post_prs = {pr.identity: pr for pr in after.pull_requests}
    live_heads = {item.ref.full_name: item.commit_id for item in after.live_refs}
    commits = {commit.commit_id: commit for commit in after.local.commits}
    git_dir = after.local.git_common_dir
    rewritten_old_ids: set[str] = set()
    for boundary in plan.boundaries:
        pr = post_prs.get(boundary.pr_identity)
        if (
            pr is None
            or pr.head_oid != boundary.remote_commit_id
            or live_heads.get(f"refs/heads/{boundary.branch}")
            != boundary.remote_commit_id
            or _one_bookmark_target(after.local, None, boundary.branch)
            != boundary.remote_commit_id
            or _one_bookmark_target(after.local, plan.remote, boundary.branch)
            != boundary.remote_commit_id
        ):
            return f"canonical PR/ref or bookmark state disagrees for {boundary.branch}"
        old = commits.get(boundary.old_commit_id)
        new = commits.get(boundary.remote_commit_id)
        if old is None or not old.is_hidden:
            return f"superseded commit remains visible or absent for {boundary.branch}"
        if new is None or new.is_hidden or new.has_conflicts:
            return f"adopted commit is hidden, absent, or conflicted for {boundary.branch}"
        try:
            old_segment = _linear_segment(
                git_dir, boundary.old_seam_commit_id, boundary.old_commit_id
            )
            new_segment = _linear_segment(
                git_dir, boundary.new_seam_commit_id, boundary.remote_commit_id
            )
            if len(old_segment) != len(new_segment) or any(
                _commit_fingerprint(git_dir, old_id)
                != _commit_fingerprint(git_dir, new_id)
                for old_id, new_id in zip(old_segment, new_segment, strict=True)
            ):
                return f"ordered change IDs or patch IDs differ for {boundary.branch}"
            rewritten_old_ids.update(old_segment)
        except Error as exc:
            return str(exc)
    visible_by_change: dict[str, list[str]] = {}
    for commit in after.local.commits:
        if not commit.is_hidden:
            visible_by_change.setdefault(commit.change_id, []).append(commit.commit_id)
    before_commits = {commit.commit_id: commit for commit in before.local.commits}
    if recovery:
        return None
    for workspace, old_target in before.local.workspace_targets:
        if old_target not in rewritten_old_ids:
            continue
        old = before_commits.get(old_target)
        targets = tuple(target for name, target in after.local.workspace_targets if name == workspace)
        if old is None or len(targets) != 1 or visible_by_change.get(old.change_id) != [targets[0]]:
            return f"workspace {workspace} did not move to the unique adopted change"
    return None


def _record_adoptions(
    workspace: str | Path,
    expected_oid: str | None,
    state: TrackedState,
    receipts: tuple[LastAdoptedHead, ...],
) -> None:
    keys = {(item.pr, item.ref) for item in receipts}
    publications = tuple(
        item for item in state.last_published_heads if (item.pr, item.ref) not in keys
    )
    adoptions = tuple(
        item for item in state.last_adopted_heads if (item.pr, item.ref) not in keys
    ) + receipts
    cas_write_state(
        workspace,
        expected_oid,
        TrackedState(
            state.stacks,
            publications,
            adoptions,
            state.detached_associations,
            state.last_published_boundaries,
        ),
    )


def adopt_remote_restack(
    plan: RemoteRestackAdoption,
    workspace: str | Path,
    github: GitHubClient,
    *,
    dry_run: bool = False,
    git_transport: GitTransport | None = None,
) -> AdoptionVerified | Stopped:
    """Fetch exact stack branches and persist authority only after full verification."""
    if dry_run:
        try:
            before = _observe_adoption(
                workspace,
                github,
                plan,
                include_remote=False,
                git_transport=git_transport,
            )
            if plan_remote_restack_adoption(before) != plan:
                return Stopped(
                    "recompute", "current facts no longer describe this restack"
                )
            return AdoptionVerified(())
        except Error as exc:
            return Stopped("adoption", str(exc))
    with repository_lock(workspace):
        try:
            recovery_oid, journal = read_recovery(workspace)
            attempt = AdoptionAttempt(
                plan.repository, plan.remote, plan.state_blob_oid, plan.boundaries
            )
            matching = tuple(
                entry for entry in (() if journal is None else journal.entries)
                if entry.attempt == attempt
            )
            if matching:
                if len(matching) != 1 or recovery_oid is None:
                    return Stopped("adoption", "adoption recovery authority is ambiguous")
                after = _observe_adoption(
                    workspace,
                    github,
                    plan,
                    include_remote=True,
                    git_transport=git_transport,
                )
                mismatch = _verify_adoption(after, after, plan, recovery=True)
                if mismatch is not None:
                    return Stopped("verify", mismatch)
                receipts = tuple(
                    LastAdoptedHead(
                        boundary.pr_identity,
                        RemoteBranchRef(plan.repository, f"refs/heads/{boundary.branch}"),
                        boundary.remote_commit_id,
                    )
                    for boundary in plan.boundaries
                )
                recorded = {
                    (item.pr, item.ref, item.verified_commit_id)
                    for item in after.tool_state.state.last_adopted_heads
                }
                wanted = {
                    (item.pr, item.ref, item.verified_commit_id) for item in receipts
                }
                if wanted <= recorded:
                    _remove_recovery_entry(
                        workspace, recovery_oid, matching[0].identity
                    )
                    return AdoptionVerified(receipts)
                _record_adoptions(
                    workspace, plan.state_blob_oid, after.tool_state.state, receipts
                )
                _remove_recovery_entry(workspace, recovery_oid, matching[0].identity)
                return AdoptionVerified(receipts)
            before = _observe_adoption(
                workspace,
                github,
                plan,
                include_remote=False,
                git_transport=git_transport,
            )
            current = plan_remote_restack_adoption(before)
            if current != plan:
                return Stopped("recompute", "current facts no longer describe this restack")
            recovery_oid, recovery_entry = _append_recovery(workspace, attempt)
            live_entry = RecoveryEntry(
                recovery_entry.identity, attempt, possibly_live=True
            )
            recovery_oid = _replace_recovery_entry(
                workspace, recovery_oid, live_entry
            )
            result = subprocess.run(
                [
                    "jj", "git", "fetch", "--remote", plan.remote,
                    *(arg for boundary in plan.boundaries for arg in ("--branch", boundary.branch)),
                ],
                cwd=workspace,
                text=True,
                capture_output=True,
                check=False,
            )
            if result.returncode:
                _remove_recovery_entry(workspace, recovery_oid, live_entry.identity)
                return Stopped("fetch", result.stderr.strip() or result.stdout.strip())
            after = _observe_adoption(
                workspace,
                github,
                plan,
                include_remote=True,
                git_transport=git_transport,
            )
            mismatch = _verify_adoption(before, after, plan)
            if mismatch is not None:
                return Stopped("verify", mismatch)
            receipts = tuple(
                LastAdoptedHead(
                    boundary.pr_identity,
                    RemoteBranchRef(plan.repository, f"refs/heads/{boundary.branch}"),
                    boundary.remote_commit_id,
                )
                for boundary in plan.boundaries
            )
            _record_adoptions(
                workspace, plan.state_blob_oid, after.tool_state.state, receipts
            )
            _remove_recovery_entry(workspace, recovery_oid, live_entry.identity)
            return AdoptionVerified(receipts)
        except Error as exc:
            return Stopped("adoption", str(exc))


def push_absent_heads(
    workspace: str | Path,
    goals: Sequence[NewPullRequestGoal],
    push_url: str,
) -> None:
    """Compatibility entry point for direct adapter conformance tests."""
    repository = GitHubRepositoryId("compatibility.invalid", "compatibility")
    SubprocessGitTransport(workspace).push_absent_heads(
        push_url, repository, goals
    )


def _first_publication_conflict(
    entry: RecoveryEntry, plan: FirstPublicationPlan
) -> bool:
    names = {goal.branch_name for goal in plan.goal.pull_requests}
    attempt = entry.attempt
    if isinstance(attempt, PullRequestCreationAttempt):
        return (
            attempt.repository == plan.goal.repository
            and attempt.goal.branch_name in names
            and attempt.pull_request is None
        )
    if isinstance(attempt, StackCreationAttempt):
        return (
            attempt.repository == plan.goal.repository
            and any(goal.branch_name in names for goal in attempt.goals)
        )
    if isinstance(attempt, HeadMutationAttempt):
        return attempt.repository == plan.goal.repository and any(
            update.ref.full_name.removeprefix("refs/heads/") in names
            for update in attempt.updates
        )
    if isinstance(attempt, BranchCreationAttempt):
        if attempt.repository != plan.goal.repository:
            return False
        observed = {item.ref.full_name: item.commit_id for item in plan.destinations}
        return any(
            goal.branch_name in names
            and observed.get(f"refs/heads/{goal.branch_name}") != goal.commit_id
            for goal in attempt.goals
        )
    return False


def _finish_creation_attempt(
    workspace: str | Path,
    github: GitHubClient,
    entry: RecoveryEntry,
) -> str:
    classification = _classify_attempt(workspace, github, entry.attempt)
    if classification == "applied":
        return "applied"
    # A negative read cannot prove that a request which lost its response will
    # not arrive later. Only a conclusive HTTP rejection is retired by callers.
    return classification


def _retire_first_publication_facts(
    workspace: str | Path,
    expected_oid: str,
    attempt: StackCreationAttempt,
) -> None:
    """Hand exact temporary ownership facts to an already-written state receipt."""
    oid, journal = read_recovery(workspace)
    if oid != expected_oid or journal is None:
        raise ConcurrentUpdate("recovery journal changed during receipt handoff")
    goals = set(attempt.goals)
    members = set(attempt.pull_requests)
    entries = tuple(
        entry for entry in journal.entries
        if not (
            isinstance(entry.attempt, StackCreationAttempt) and entry.attempt == attempt or (isinstance(entry.attempt, BranchCreationAttempt) and entry.attempt.repository == attempt.repository and (set(entry.attempt.goals) == goals)) or (isinstance(entry.attempt, PullRequestCreationAttempt) and entry.attempt.goal in goals and (entry.attempt.pull_request in members))
        )
    )
    cas_write_recovery(
        workspace, expected_oid,
        RecoveryJournal(entries)
        if entries else None,
    )


def apply_first_publication(
    plan: FirstPublicationPlan,
    workspace: str | Path,
    github: GitHubClient,
    push_url: str,
    *,
    dry_run: bool = False,
    git_transport: GitTransport | None = None,
) -> ApplyResult:
    """Publish a fully planned new stack, recovering uncertain creations first."""
    git = git_transport or SubprocessGitTransport(workspace)
    _oid, journal = read_recovery(workspace)
    if dry_run:
        try:
            unresolved = settle_recovery(
                workspace, github, dry_run=True, git_transport=git
            )
        except Error as exc:
            return Stopped("recovery", str(exc))
        if any(_first_publication_conflict(entry, plan) for entry in unresolved):
            return Stopped("recovery", "a conflicting mutation remains unresolved")
        return Verified()
    with repository_lock(workspace):
        _locked_oid, locked_journal = read_recovery(workspace)
        try:
            unresolved = settle_recovery(
                workspace, github, git_transport=git
            )
            repository = github.resolve_repository(plan.goal.repository)
            goals = plan.goal.pull_requests
            refs = git.observe_live_refs(
                push_url, plan.goal.repository,
                tuple(f"refs/heads/{goal.branch_name}" for goal in goals),
            )
        except Error as exc:
            return Stopped("observe", str(exc))
        if any(_first_publication_conflict(entry, plan) for entry in unresolved):
            return Stopped("recovery", "a conflicting mutation remains unresolved")
        current = tuple(item.commit_id for item in refs)
        wanted = tuple(goal.commit_id for goal in goals)
        head_published = False
        owns_temporary_heads = any(
            isinstance(entry.attempt, BranchCreationAttempt)
            and entry.attempt.repository == plan.goal.repository
            and entry.attempt.goals == goals
            for entry in unresolved
        )
        _state_oid, state = read_state(workspace)
        recorded_heads = {
            (item.ref.full_name, item.verified_commit_id): item.pr
            for item in state.last_published_heads
            if item.ref.repository == plan.goal.repository
        }
        owns_recorded_heads = all(
            (f"refs/heads/{goal.branch_name}", goal.commit_id) in recorded_heads
            for goal in goals
        )
        owns_heads = owns_temporary_heads or owns_recorded_heads
        if current == wanted and not owns_heads:
            return Stopped("heads", "occupied publication destinations are not owned")
        if current != wanted:
            if any(value is not None for value in current):
                return Stopped("heads", "a publication destination changed")
            attempt = BranchCreationAttempt(plan.goal.repository, push_url, goals)
            oid, entry = _append_recovery(workspace, attempt)
            live = replace(entry, possibly_live=True)
            oid = _replace_recovery_entry(workspace, oid, live)
            try:
                git.push_absent_heads(push_url, plan.goal.repository, goals)
            except GitPushError:
                pass
            try:
                refs = git.observe_live_refs(
                    push_url, plan.goal.repository,
                    tuple(item.ref.full_name for item in refs),
                )
            except Error as exc:
                return Stopped("heads", f"publication readback is unresolved: {exc}")
            if tuple(item.commit_id for item in refs) != wanted:
                return Stopped(
                    "heads", "atomic branch publication remains unresolved",
                )
            head_published = True

        identities: list[PullRequestId] = []
        for goal in goals:
            attempt = PullRequestCreationAttempt(plan.goal.repository, goal)
            matches = github.find_pull_requests(
                plan.goal.repository, head_branches=(goal.branch_name,),
                states=tuple(PullRequestState),
            )
            exact = tuple(pr for pr in matches if
                pr.head_repository == plan.goal.repository
                and pr.head_oid == goal.commit_id and pr.base_branch == goal.base_branch
                and pr.title == goal.title and pr.body == goal.body and not pr.draft)
            if len(exact) == 1 and len(matches) == 1:
                owned = any(
                    isinstance(item.attempt, PullRequestCreationAttempt)
                    and item.attempt.goal == goal
                    and item.attempt.pull_request == exact[0].identity
                    for item in unresolved
                ) or recorded_heads.get(
                    (f"refs/heads/{goal.branch_name}", goal.commit_id)
                ) == exact[0].identity
                if not owned:
                    return Stopped("pull-request", f"branch {goal.branch_name} has a foreign PR")
                identities.append(exact[0].identity)
                continue
            if matches:
                return Stopped("pull-request", f"branch {goal.branch_name} has a foreign PR")
            oid, entry = _append_recovery(workspace, attempt)
            live = RecoveryEntry(entry.identity, attempt, True)
            oid = _replace_recovery_entry(workspace, oid, live)
            try:
                github.create_pull_request(
                    repository, head_branch=goal.branch_name,
                    base_branch=goal.base_branch, title=goal.title,
                    body=goal.body, draft=False,
                )
            except GitHubTransportError:
                pass
            except GitHubHttpError:
                _remove_recovery_entry(workspace, oid, entry.identity)
                return Stopped("pull-request", "pull request creation was rejected")
            try:
                result = _finish_creation_attempt(workspace, github, live)
            except Error as exc:
                return Stopped("pull-request", f"creation readback is unresolved: {exc}")
            if result != "applied":
                return Stopped("pull-request", f"pull request creation is {result}")
            match = github.find_pull_requests(
                plan.goal.repository, head_branches=(goal.branch_name,),
                states=tuple(PullRequestState),
            )
            identities.append(match[0].identity)
            _replace_recovery_entry(
                workspace, oid,
                replace(live, attempt=replace(attempt, pull_request=match[0].identity)),
            )

        members = tuple(identities)

        if len(members) == 1:
            publication = LastPublishedHead(
                members[0],
                RemoteBranchRef(
                    plan.goal.repository, f"refs/heads/{goals[0].branch_name}"
                ),
                goals[0].commit_id,
            )
            receipt_oid, receipt_journal = read_recovery(workspace)
            if receipt_oid is None or receipt_journal is None:
                if owns_recorded_heads and TrackedStack(
                    plan.goal.repository, plan.goal.base_branch, members
                ) in state.stacks:
                    return Verified()
                return Stopped(
                    "receipt", "temporary publication ownership is unavailable"
                )
            handoff = StackCreationAttempt(
                plan.goal.repository, plan.goal.base_branch, members, goals
            )
            try:
                changed = _record_verified_state(
                    workspace,
                    TrackedStack(plan.goal.repository, plan.goal.base_branch, members),
                    (publication,),
                )
                _retire_first_publication_facts(workspace, receipt_oid, handoff)
            except Error as exc:
                return Stopped("receipt", str(exc))
            return Verified(head_published, False, changed)
        if any(
            isinstance(entry.attempt, StackCreationAttempt)
            and entry.attempt.repository == plan.goal.repository
            and set(entry.attempt.pull_requests).intersection(members)
            for entry in unresolved
        ):
            return Stopped("recovery", "a conflicting stack creation remains unresolved")
        prs = github.pull_requests(members)
        summaries = {pr.stack for pr in prs}
        if summaries == {None}:
            attempt = StackCreationAttempt(
                plan.goal.repository, plan.goal.base_branch, members, goals
            )
            oid, entry = _append_recovery(workspace, attempt)
            live = RecoveryEntry(entry.identity, attempt, True)
            oid = _replace_recovery_entry(workspace, oid, live)
            try:
                github.create_stack(repository, pull_requests=members)
            except GitHubTransportError:
                pass
            except GitHubHttpError:
                _remove_recovery_entry(workspace, oid, entry.identity)
                return Stopped("stack", "stack creation was rejected")
            try:
                result = _finish_creation_attempt(workspace, github, live)
            except Error as exc:
                return Stopped("stack", f"creation readback is unresolved: {exc}")
            if result != "applied":
                return Stopped("stack", f"stack creation is {result}")
            try:
                changed = _record_verified_state(
                    workspace,
                    TrackedStack(plan.goal.repository, plan.goal.base_branch, members),
                    tuple(
                        LastPublishedHead(pr, RemoteBranchRef(plan.goal.repository,
                            f"refs/heads/{goal.branch_name}"), goal.commit_id)
                        for pr, goal in zip(members, goals, strict=True)
                    ),
                )
            except Error as exc:
                return Stopped("receipt", str(exc))
            try:
                _retire_first_publication_facts(workspace, oid, attempt)
            except Error as exc:
                return Stopped("receipt", str(exc))
            return Verified(head_published, False, changed)
        if len(summaries) != 1 or None in summaries:
            return Stopped("stack", "pull requests have foreign stack membership")
        # Partial-progress recovery: verify the existing stack before handing off receipts.
        summary = next(iter(summaries))
        assert summary is not None
        stack = github.stack(repository, summary.identity)
        if stack is None or stack.pull_requests != members or stack.base_branch != plan.goal.base_branch:
            return Stopped("stack", "existing stack does not match the goal")
        publications = tuple(
            LastPublishedHead(pr, RemoteBranchRef(plan.goal.repository,
                f"refs/heads/{goal.branch_name}"), goal.commit_id)
            for pr, goal in zip(members, goals, strict=True)
        )
        receipt_oid, receipt_journal = read_recovery(workspace)
        if receipt_oid is None or receipt_journal is None:
            if owns_recorded_heads and TrackedStack(
                plan.goal.repository, plan.goal.base_branch, members
            ) in state.stacks:
                return Verified()
            return Stopped("receipt", "temporary publication ownership is unavailable")
        try:
            changed = _record_verified_state(
                workspace,
                TrackedStack(plan.goal.repository, plan.goal.base_branch, members),
                publications,
            )
            _retire_first_publication_facts(
                workspace,
                receipt_oid,
                StackCreationAttempt(
                    plan.goal.repository, plan.goal.base_branch, members, goals
                ),
            )
        except Error as exc:
            return Stopped("receipt", str(exc))
        return Verified(state_recorded=changed)


def execute_first_publication(
    workspace: str | Path,
    github: GitHubClient,
    push_url: str,
    recompute: Callable[[tuple[RecoveryEntry, ...]], FirstPublicationPlan | Blocked],
    *,
    confirm: Callable[[FirstPublicationPlan], bool] | None = None,
    dry_run: bool = False,
    git_transport: GitTransport | None = None,
) -> tuple[FirstPublicationPlan | Blocked | None, ApplyResult]:
    """Settle recovery facts, recompute the current goal, confirm, then publish."""
    try:
        with repository_lock(workspace):
            facts = settle_recovery(
                workspace,
                github,
                dry_run=dry_run,
                git_transport=git_transport,
            )
    except Error as exc:
        return None, Stopped("recovery", str(exc))
    try:
        plan = recompute(facts)
    except Error as exc:
        return None, Stopped("observe", str(exc))
    if isinstance(plan, Blocked):
        return plan, Stopped("plan", "a blocked plan cannot be applied")
    if confirm is not None and not confirm(plan):
        return plan, Stopped("confirmation", "publication was not confirmed")
    return plan, apply_first_publication(
        plan,
        workspace,
        github,
        push_url,
        dry_run=dry_run,
        git_transport=git_transport,
    )


def apply(
    plan: SyncPlan,
    workspace: str | Path,
    github: GitHubClient,
    *,
    dry_run: bool = False,
    git_transport: GitTransport | None = None,
) -> ApplyResult:
    """Apply an existing complete stack; a later invocation recovers automatically."""
    git = git_transport or SubprocessGitTransport(workspace)
    if isinstance(plan, Blocked):
        return Stopped("plan", "a blocked plan cannot be applied")
    _oid, journal = read_recovery(workspace)
    if dry_run:
        try:
            unresolved = settle_recovery(
                workspace, github, dry_run=True, git_transport=git
            )
        except Error as exc:
            return Stopped("recovery", str(exc))
        if any(_attempt_conflicts_plan(entry, plan) for entry in unresolved):
            return Stopped("recovery", "a conflicting mutation remains unresolved")
        return Verified()
    with repository_lock(workspace):
        _locked_oid, locked_journal = read_recovery(workspace)
        try:
            unresolved = settle_recovery(
                workspace, github, git_transport=git
            )
        except Error as exc:
            return Stopped("recovery", str(exc))
        if any(_attempt_conflicts_plan(entry, plan) for entry in unresolved):
            return Stopped("recovery", "a conflicting mutation remains unresolved")
        try:
            _revalidate_sync_dependencies(workspace, github, plan)
        except Error as exc:
            return Stopped("revalidate", str(exc))
        if isinstance(plan, NoOp):
            try:
                return Verified(
                    state_recorded=_record_verified_state(
                        workspace,
                        _plan_tracking(plan),
                        (),
                        plan.boundary_receipts,
                        expected_oid=plan.dependencies.state_blob_oid,
                    )
                )
            except Error as exc:
                return Stopped("receipt", str(exc))

        head_published = False
        metadata_updated = False
        state_recorded = False
        pending_journal_oid: str | None = None
        pending_entry_identity: str | None = None
        if plan.head_updates:
            tracking = _plan_tracking(plan)
            publications = _plan_publications(plan)
            attempt = HeadMutationAttempt(
                plan.desired.repository,
                plan.dependencies.push_url,
                plan.head_updates,
                tracking,
                publications,
                tuple(
                    boundary
                    for boundary in plan.boundary_receipts
                    if any(update.ref == boundary.ref for update in plan.head_updates)
                ),
            )
            oid, entry = _append_recovery(workspace, attempt)
            entry = RecoveryEntry(entry.identity, attempt, possibly_live=True)
            oid = _replace_recovery_entry(
                workspace,
                oid,
                entry,
            )
            try:
                git.push_exact_head_updates(
                    plan.dependencies.push_url, plan.head_updates
                )
            except GitPushError:
                pass
            try:
                result = _classify_attempt(workspace, github, attempt, git)
            except Error as exc:
                return Stopped("heads", f"publication readback is unresolved: {exc}")
            if result != "applied":
                # A negative read after send cannot prove that the push will
                # not land later. Keep the possibly-live exact attempt unless
                # the transport conclusively rejected it before any effect.
                return Stopped("heads", f"atomic publication was {result}")
            head_published = True
            try:
                state_recorded = _record_verified_state(
                    workspace,
                    tracking,
                    publications,
                    plan.boundary_receipts,
                    expected_oid=plan.dependencies.state_blob_oid,
                )
            except Error as exc:
                return Stopped("receipt", str(exc))
            try:
                _remove_recovery_entry(workspace, oid, entry.identity)
            except Error as exc:
                return Stopped("recovery-retirement", str(exc))

        old_prs = {pr.identity: pr for pr in plan.dependencies.prs}
        for update in plan.metadata_updates:
            if pending_journal_oid is not None:
                assert pending_entry_identity is not None
                _remove_recovery_entry(
                    workspace, pending_journal_oid, pending_entry_identity
                )
                pending_journal_oid = None
                pending_entry_identity = None
            old = old_prs[update.pr_identity]
            attempt = MetadataMutationAttempt(
                plan.desired.repository, update, old.title, old.body
            )
            oid, entry = _append_recovery(workspace, attempt)
            try:
                before = _classify_attempt(workspace, github, attempt, git)
            except Error as exc:
                return Stopped("metadata", f"metadata preflight is unresolved: {exc}")
            if before == "foreign":
                _remove_recovery_entry(workspace, oid, entry.identity)
                return Stopped("metadata", "metadata changed before mutation")
            if before == "not-applied":
                try:
                    repository = github.resolve_repository(plan.desired.repository)
                except Error as exc:
                    return Stopped("metadata", f"metadata preflight is unresolved: {exc}")
                entry = RecoveryEntry(entry.identity, attempt, possibly_live=True)
                oid = _replace_recovery_entry(
                    workspace,
                    oid,
                    entry,
                )
                try:
                    github.update_pull_request(
                        repository,
                        update.pr_identity,
                        title=update.title,
                        body=update.body,
                    )
                except GitHubTransportError:
                    pass
                except GitHubHttpError:
                    _remove_recovery_entry(workspace, oid, entry.identity)
                    return Stopped("metadata", "metadata mutation was rejected")
            try:
                result = _classify_attempt(workspace, github, attempt, git)
            except Error as exc:
                return Stopped("metadata", f"metadata readback is unresolved: {exc}")
            if result != "applied":
                return Stopped(
                    "metadata",
                    "metadata mutation is unresolved"
                    if result == "not-applied"
                    else f"metadata mutation was {result}",
                )
            metadata_updated = True
            pending_journal_oid = oid
            pending_entry_identity = entry.identity
        if not head_published:
            try:
                state_recorded = (
                    _record_verified_state(
                        workspace,
                        _plan_tracking(plan),
                        (),
                        plan.boundary_receipts,
                        expected_oid=plan.dependencies.state_blob_oid,
                    )
                    or state_recorded
                )
            except Error as exc:
                return Stopped("receipt", str(exc))
        if pending_journal_oid is not None:
            assert pending_entry_identity is not None
            try:
                _remove_recovery_entry(
                    workspace, pending_journal_oid, pending_entry_identity
                )
            except Error as exc:
                return Stopped("recovery-retirement", str(exc))
        return Verified(head_published, metadata_updated, state_recorded)


def execute(
    workspace: str | Path,
    github: GitHubClient,
    recompute: Callable[[], SyncPlan],
    *,
    confirm: Callable[[Apply], bool] | None = None,
    dry_run: bool = False,
    git_transport: GitTransport | None = None,
) -> tuple[SyncPlan | None, ApplyResult]:
    """Settle uncertainty, recompute from current inputs, then optionally apply.

    Recovery precedes recomputation and confirmation: an effect authorized before a
    crash is only read back, never reconfirmed or resent.
    """
    try:
        with repository_lock(workspace):
            settle_recovery(
                workspace,
                github,
                dry_run=dry_run,
                git_transport=git_transport,
            )
    except Error as exc:
        return None, Stopped("recovery", str(exc))
    try:
        plan = recompute()
    except Error as exc:
        return None, Stopped("observe", str(exc))
    if isinstance(plan, Apply) and confirm is not None and not confirm(plan):
        return plan, Stopped("confirmation", "application was not confirmed")
    if isinstance(plan, Apply) and confirm is not None and not dry_run:
        try:
            current = recompute()
        except Error as exc:
            return plan, Stopped("revalidate", str(exc))
        if current != plan:
            return plan, Stopped(
                "revalidate", "authority-bearing facts changed during confirmation"
            )
    return plan, apply(
        plan,
        workspace,
        github,
        dry_run=dry_run,
        git_transport=git_transport,
    )
