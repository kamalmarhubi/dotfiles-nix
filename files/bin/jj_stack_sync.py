"""Observation, planning, and durable execution for ``jj stack sync``.

The installed facade owns argument parsing and confirmation; this module owns
the immutable plans, mutation protocols, and recovery state machines.
"""

from __future__ import annotations

import dataclasses
import fcntl
import hashlib
import http.client
import json
import os
import re
import secrets
import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Collection, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from enum import Enum, StrEnum
from pathlib import Path
from typing import Protocol
from urllib.parse import quote, urlparse

from markdown_it import MarkdownIt
from markdown_it.rules_inline.newline import newline as _parse_newline
from markdown_it.rules_inline.state_inline import StateInline

STATE_REF = "refs/jj-stack/state"
OPERATION_REF = "refs/jj-stack/operation"
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


@dataclass(frozen=True)
class StandalonePullRequest:
    pr: PullRequestId


@dataclass(frozen=True)
class StandalonePullRequestSet:
    """Explicitly resolved repository-local PRs with no server stack association."""

    prs: tuple[PullRequestId, ...]

    def __post_init__(self) -> None:
        if not self.prs or len(set(self.prs)) != len(self.prs):
            raise ValueError("standalone PR set must be nonempty and unique")
        if any(pr.repository != self.prs[0].repository for pr in self.prs):
            raise ValueError("standalone PR set must belong to one repository")
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
MembershipRepairSource = PullRequestMembership | StandalonePullRequestSet


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
class LastAdoptedHead:
    """Last adopted GitHub restack whose change IDs and patch IDs matched."""

    pr: PullRequestId
    ref: RemoteBranchRef
    verified_commit_id: str


@dataclass(frozen=True)
class TrackedStack:
    repository: GitHubRepositoryId
    base_branch: str
    ordered_prs: tuple[PullRequestId, ...]
    detached_prs: tuple[PullRequestId, ...] = ()


@dataclass(frozen=True)
class TrackedRestoreSource:
    """Tracked intent plus the only surviving server stack, when one exists."""

    tracked: TrackedStack
    current_stack: ServerStackMembership | None


TopologySource = PullRequestMembership | StandalonePullRequestSet | TrackedRestoreSource


@dataclass(frozen=True)
class TrackedState:
    stacks: tuple[TrackedStack, ...]
    last_published_heads: tuple[LastPublishedHead, ...]
    last_adopted_heads: tuple[LastAdoptedHead, ...] = ()


@dataclass(frozen=True)
class DetachedAssociation:
    """One bounded cleanup association; it conveys no publication authority."""

    repository: GitHubRepositoryId
    base_branch: str
    pr: PullRequestId

    def __post_init__(self) -> None:
        if not self.base_branch:
            raise ValueError("detached association base branch must be nonempty")
        if self.pr.repository != self.repository:
            raise ValueError("detached association PR belongs to another repository")


@dataclass(frozen=True)
class DetachedAssociationResult:
    association: DetachedAssociation
    state_blob_oid: str


EMPTY_STATE = TrackedState((), (), ())


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
    remote: str
    local: LocalObservation
    tool_state: ToolStateRead
    pull_requests: tuple[GitHubPullRequest, ...]
    membership: PullRequestMembership
    live_refs: tuple[LiveRemoteRef, ...]
    fetch_url: str = ""


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
    """One exact selected commit and the branch chosen before publication planning."""

    commit_id: str
    branch_name: str


@dataclass(frozen=True)
class PublicationAssignmentInput:
    """Complete read-only evidence for resolving first-publication branch names."""

    repository: GitHubRepositoryId
    ordered_commit_ids: tuple[str, ...]
    local: LocalObservation
    explicit: tuple[PublicationAssignment, ...] | None
    template_results: tuple[PublicationAssignment, ...]
    protected_branch_names: tuple[str, ...]
    destinations: tuple[LiveRemoteRef, ...]
    historical_pull_requests: tuple[GitHubPullRequest, ...]


@dataclass(frozen=True)
class DesiredStack:
    repository: GitHubRepositoryId
    base_branch: str
    active: tuple[DesiredExistingPR, ...]


@dataclass(frozen=True)
class TopologyPlanningSource:
    """Intent resolved before temporary base names can be authoritatively observed."""

    snapshot: Snapshot
    repository: GitHubRepository
    remote: str
    desired: DesiredStack
    naming_digest: str
    temporary_base_refs: tuple[RemoteBranchRef, ...]


@dataclass(frozen=True)
class TopologyPlanningInput:
    """Complete immutable topology input, including exact temporary-base absence reads."""

    snapshot: Snapshot
    repository: GitHubRepository
    remote: str
    desired: DesiredStack
    naming_digest: str
    temporary_base_observations: tuple[LiveRemoteRef, ...]


@dataclass(frozen=True)
class TrackedRestoreObservation:
    local: LocalObservation
    tool_state: ToolStateRead
    repository: GitHubRepository
    push_url: str
    remote: str
    tracked: TrackedStack
    pull_requests: tuple[GitHubPullRequest, ...]
    current_stack: GitHubStack | None
    live_refs: tuple[LiveRemoteRef, ...]
    desired: DesiredStack


@dataclass(frozen=True)
class TrackedRestorePlanningSource:
    observation: TrackedRestoreObservation
    naming_digest: str
    temporary_base_refs: tuple[RemoteBranchRef, ...]


@dataclass(frozen=True)
class TrackedRestorePlanningInput:
    observation: TrackedRestoreObservation
    naming_digest: str
    temporary_base_observations: tuple[LiveRemoteRef, ...]


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
    operation_blob_oid: str | None
    prs: tuple[GitHubPullRequest, ...]
    membership: PullRequestMembership
    live_heads: tuple[LiveRemoteRef, ...]
    live_bases: tuple[LiveRemoteRef, ...]


@dataclass(frozen=True)
class TopologyDependencies:
    effective_config: tuple[tuple[str, str], ...]
    push_url: str
    state_blob_oid: str | None
    operation_blob_oid: str | None
    prs: tuple[GitHubPullRequest, ...]
    source: TopologySource
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


Authority = FastForward | MatchesLastPublication | MatchesLastAdoption


@dataclass(frozen=True)
class RewriteCommitPair:
    old_commit_id: str
    new_commit_id: str
    change_id: str
    patch_id: str


@dataclass(frozen=True)
class RemoteRestackBoundary:
    pr_identity: PullRequestId
    branch: str
    local_commit_id: str
    tracked_commit_id: str
    remote_commit_id: str
    rewrite: tuple[RewriteCommitPair, ...]


@dataclass(frozen=True)
class RemoteRestackAdoption:
    repository: GitHubRepositoryId
    remote: str
    fetch_url: str
    effective_config: tuple[tuple[str, str], ...]
    state_blob_oid: str | None
    operation_blob_oid: str | None
    membership: PullRequestMembership
    prs: tuple[GitHubPullRequest, ...]
    live_refs: tuple[LiveRemoteRef, ...]
    boundaries: tuple[RemoteRestackBoundary, ...]
    workspace_change_ids: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class AdoptionVerified:
    adopted_heads: tuple[LastAdoptedHead, ...]


class BookmarkSetup(StrEnum):
    CREATE = "create"
    KEEP = "keep"


class NewPRPhase(StrEnum):
    NOT_ATTEMPTED = "not-attempted"
    PUBLICATION_POSSIBLY_SENT = "publication-possibly-sent"
    READY = "ready"
    POSSIBLY_SENT = "possibly-sent"
    BOUND = "bound"
    VERIFIED = "verified"


class FirstPublicationPhase(StrEnum):
    PREPARING_BOOKMARKS = "preparing-bookmarks"
    PUBLISHING = "publishing"
    CREATING_PRS = "creating-prs"
    LINKING_STACK = "linking-stack"
    COMMITTING = "committing"


class FirstPublicationTarget(StrEnum):
    STANDALONE = "standalone"
    FRESH_STACK = "fresh-stack"
    APPEND = "append"


class StackLinkPhase(StrEnum):
    NOT_REQUIRED = "not-required"
    NOT_ATTEMPTED = "not-attempted"
    POSSIBLY_SENT = "possibly-sent"
    VERIFIED = "verified"


@dataclass(frozen=True)
class NewPRSlot:
    slot_id: str
    branch: str
    commit_id: str
    bookmark_setup: BookmarkSetup
    base_branch: str
    title: str
    body: str
    phase: NewPRPhase = NewPRPhase.NOT_ATTEMPTED
    pr_identity: PullRequestId | None = None

    @property
    def marker(self) -> str:
        return f"<!-- jj-stack-slot:{self.slot_id} -->"

    @property
    def initial_body(self) -> str:
        return f"{self.body}\n\n{self.marker}" if self.body else self.marker


@dataclass(frozen=True)
class ExistingFirstPublicationPR:
    identity: PullRequestId
    head_branch: str
    head_commit_id: str
    base_branch: str
    base_commit_id: str
    draft: bool
    title: str
    body: str
    state: PullRequestState = PullRequestState.OPEN


@dataclass(frozen=True)
class FirstPublication:
    repository: GitHubRepositoryId
    repository_name: str
    push_url: str
    remote: str
    effective_config: tuple[tuple[str, str], ...]
    workspace_targets: tuple[tuple[str, str], ...]
    base_branch: str
    expected_state_oid: str | None
    target: FirstPublicationTarget
    existing_members: tuple[ExistingFirstPublicationPR, ...]
    existing_stack: GitHubStackId | None
    slots: tuple[NewPRSlot, ...]
    stack_phase: StackLinkPhase = StackLinkPhase.NOT_REQUIRED
    resulting_stack: GitHubStackId | None = None
    phase: FirstPublicationPhase = FirstPublicationPhase.PREPARING_BOOKMARKS
    final_state_json: str | None = None
    final_state_oid: str | None = None

    @property
    def existing_prs(self) -> tuple[PullRequestId, ...]:
        return tuple(member.identity for member in self.existing_members)

    @property
    def active_existing_prs(self) -> tuple[PullRequestId, ...]:
        return tuple(
            member.identity
            for member in self.existing_members
            if member.state is PullRequestState.OPEN
        )


@dataclass(frozen=True)
class FirstPublicationVerified:
    ordered_prs: tuple[PullRequestId, ...]


class GitHubEffectPhase(StrEnum):
    NOT_ATTEMPTED = "not-attempted"
    POSSIBLY_SENT = "possibly-sent"
    VERIFIED = "verified"


@dataclass(frozen=True)
class PullRequestBaseEffect:
    repository: GitHubRepositoryId
    repository_name: str
    pr_identity: PullRequestId
    expected_base: str
    desired_base: str
    phase: GitHubEffectPhase = GitHubEffectPhase.NOT_ATTEMPTED


class StackEffectKind(StrEnum):
    CREATE = "create"
    ADD = "add"
    UNSTACK = "unstack"


@dataclass(frozen=True)
class StackEffect:
    kind: StackEffectKind
    repository: GitHubRepositoryId
    repository_name: str
    expected_before: tuple[PullRequestId, ...]
    desired_after: tuple[PullRequestId, ...]
    stack: GitHubStackId | None = None
    phase: GitHubEffectPhase = GitHubEffectPhase.NOT_ATTEMPTED
    resulting_stack: GitHubStackSummary | None = None


GitHubEffect = PullRequestBaseEffect | StackEffect


class TopologyRepairPhase(StrEnum):
    CREATING_TEMPORARY_BASES = "creating-temporary-bases"
    UNSTACKING = "unstacking"
    TEMPORARY_BASES = "temporary-bases"
    PUBLISHING = "publishing"
    RELINKING = "relinking"
    VERIFYING = "verifying"
    COMMITTING = "committing"


@dataclass(frozen=True)
class PlannedHeadUpdate:
    ref: RemoteBranchRef
    expected_old_commit_id: str
    new_commit_id: str
    authority: Authority


@dataclass(frozen=True)
class PlannedTemporaryBase:
    pr_identity: PullRequestId
    ref: RemoteBranchRef
    old_base_commit_id: str
    new_base_commit_id: str


@dataclass(frozen=True)
class TopologyPlan:
    repository: GitHubRepositoryId
    repository_name: str
    remote: str
    desired: DesiredStack
    detached_prs: tuple[PullRequestId, ...]
    head_updates: tuple[PlannedHeadUpdate, ...]
    temporary_bases: tuple[PlannedTemporaryBase, ...]
    tracking_update: TrackedStack | None
    dependencies: TopologyDependencies

    @property
    def source(self) -> TopologySource:
        return self.dependencies.source


@dataclass(frozen=True)
class TopologyRepair:
    """Durable execution record for one approved topology plan."""

    plan: TopologyPlan
    phase: TopologyRepairPhase = TopologyRepairPhase.CREATING_TEMPORARY_BASES
    unstack_possibly_sent: bool = False
    temporary_bases_possibly_sent: tuple[PullRequestId, ...] = ()
    relink_possibly_sent: bool = False
    final_state_json: str | None = None
    final_state_oid: str | None = None


@dataclass(frozen=True)
class TopologyRepairVerified:
    ordered_prs: tuple[PullRequestId, ...]


class MembershipRepairPhase(StrEnum):
    PREPARING_BOOKMARKS = "preparing-bookmarks"
    PUBLISHING_NEW_REFS = "publishing-new-refs"
    CREATING_PRS = "creating-prs"
    UNSTACKING_SOURCE = "unstacking-source"
    TRANSITIONING_BASES = "transitioning-bases"
    PUBLISHING = "publishing"
    RELINKING = "relinking"
    VERIFYING = "verifying"
    COMMITTING = "committing"


@dataclass(frozen=True)
class ExistingMembershipEntry:
    pr_identity: PullRequestId


@dataclass(frozen=True)
class NewMembershipEntry:
    slot_id: str


DesiredMembershipEntry = ExistingMembershipEntry | NewMembershipEntry


@dataclass(frozen=True)
class MembershipRepair:
    """Frozen transaction for a complete mixed existing/new membership goal."""

    repository: GitHubRepositoryId
    repository_name: str
    push_url: str
    remote: str
    effective_config: tuple[tuple[str, str], ...]
    workspace_targets: tuple[tuple[str, str], ...]
    base_branch: str
    expected_state_oid: str | None
    source_membership: MembershipRepairSource
    source_prs: tuple[GitHubPullRequest, ...]
    source_tracked: TrackedStack | None
    desired: tuple[DesiredMembershipEntry, ...]
    slots: tuple[NewPRSlot, ...]
    head_updates: tuple[PlannedHeadUpdate, ...]
    temporary_bases: tuple[PlannedTemporaryBase, ...]
    metadata_updates: tuple[PRMetadataUpdate, ...] = ()
    phase: MembershipRepairPhase = MembershipRepairPhase.PREPARING_BOOKMARKS
    source_unstack_possibly_sent: bool = False
    base_transitions_possibly_sent: tuple[PullRequestId, ...] = ()
    publication_possibly_sent: tuple[RemoteBranchRef, ...] = ()
    relink_possibly_sent: bool = False
    resulting_stack: GitHubStackId | None = None
    final_state_json: str | None = None
    final_state_oid: str | None = None


@dataclass(frozen=True)
class MembershipRepairVerified:
    ordered_prs: tuple[PullRequestId, ...]


class StackDissolution(StrEnum):
    ABSENT = "absent"
    PRESENT = "present"
    UNKNOWN = "unknown"


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
    tracking_update: TrackedStack | None
    dependencies: Dependencies


SyncPlan = Blocked | NoOp | Apply
TopologyPlanResult = Blocked | TopologyPlan


@dataclass(frozen=True)
class Verified:
    head_published: bool
    metadata_updated: bool
    authority_persisted: bool
    tracking_persisted: bool = False


@dataclass(frozen=True)
class Stopped:
    stage: str
    detail: str
    head_published: bool = False
    metadata_updated: bool = False
    final_state_verified: bool = False
    authority_persisted: bool = False
    tracking_persisted: bool = False


ApplyResult = Verified | Stopped
AdoptionResult = AdoptionVerified | Stopped
FirstPublicationResult = FirstPublicationVerified | Stopped
TopologyRepairResult = TopologyRepairVerified | Stopped
MembershipRepairResult = MembershipRepairVerified | Stopped
Operation = FirstPublication | TopologyRepair | MembershipRepair


@dataclass(frozen=True)
class ExistingIntent:
    pr_number: int
    revision: str | None = None


@dataclass(frozen=True)
class NewIntent:
    revision: str
    base_branch: str | None = None


@dataclass(frozen=True)
class ExistingMembershipBoundary:
    """A selected boundary proven to be one particular existing PR."""

    pr_identity: PullRequestId
    commit_id: str


@dataclass(frozen=True)
class NewMembershipBoundary:
    """A selected boundary with no existing PR identity."""

    commit_id: str


CompleteMembershipBoundary = ExistingMembershipBoundary | NewMembershipBoundary


@dataclass(frozen=True)
class ResumeIntent:
    pass


@dataclass(frozen=True)
class PersistedOperation:
    operation: Operation


ExplicitIntent = ExistingIntent | NewIntent | ResumeIntent
ExplicitPlan = (
    Blocked
    | NoOp
    | Apply
    | RemoteRestackAdoption
    | FirstPublication
    | TopologyPlan
    | MembershipRepair
    | PersistedOperation
)
ExplicitResult = (
    ApplyResult
    | AdoptionResult
    | FirstPublicationResult
    | TopologyRepairResult
    | MembershipRepairResult
    | Blocked
)


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


def send_github_effect(
    effect: GitHubEffect, github: GitHubClient
) -> GitHubStackSummary | None:
    """Send one already-journaled effect exactly once; callers own reconciliation."""
    if effect.phase is not GitHubEffectPhase.POSSIBLY_SENT:
        raise ValueError("GitHub effect must be journaled as possibly-sent before I/O")
    repository = github.resolve_repository(effect.repository)
    if isinstance(effect, PullRequestBaseEffect):
        if effect.pr_identity.repository != effect.repository:
            raise ValueError("pull request effect belongs to another repository")
        github.update_pull_request(
            repository, effect.pr_identity, base_branch=effect.desired_base
        )
        return None
    if effect.kind is StackEffectKind.CREATE:
        if effect.stack is not None:
            raise ValueError("stack creation cannot freeze an existing stack")
        return github.create_stack(
            repository, pull_requests=effect.desired_after
        )
    if effect.stack is None or effect.stack.repository != effect.repository:
        raise ValueError("stack mutation requires the frozen stack identity")
    if effect.kind is StackEffectKind.ADD:
        additions = tuple(
            pr for pr in effect.desired_after if pr not in effect.expected_before
        )
        return github.add_stack_members(
            repository, effect.stack, pull_requests=additions
        )
    return github.unstack(repository, effect.stack)


def github_effect_to_record(effect: GitHubEffect) -> dict[str, object]:
    record = asdict(effect)
    record["effect_kind"] = (
        "pull-request-base" if isinstance(effect, PullRequestBaseEffect) else "stack"
    )
    return record


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


_PULL_REQUEST_FIELDS = {
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

_PULL_REQUEST_QUERY_FIELDS = """
id number state isDraft
headRepository { id }
headRefName headRefOid baseRefName baseRefOid
autoMergeRequest { enabledAt }
mergeQueueEntry { id }
title body
stack { id number baseRefName }
"""

_PULL_REQUEST_QUERY = f"""
query($owner: String!, $name: String!, $number: Int!) {{
  repository(owner: $owner, name: $name) {{
    id
    pullRequest(number: $number) {{ {_PULL_REQUEST_QUERY_FIELDS} }}
  }}
}}
"""

def _parse_pull_request(
    value: object,
    repository: GitHubRepository,
    expected_number: int | None = None,
) -> GitHubPullRequest:
    raw = _exact_record(value, _PULL_REQUEST_FIELDS, "pull request")
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
            _PULL_REQUEST_QUERY,
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
    return _parse_pull_request(pull_request, repository, number)


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
                      nodes {{ {_PULL_REQUEST_QUERY_FIELDS} }}
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
                pull_request = _parse_pull_request(node, resolved)
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
    ancestry_order: bool = False,
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
    commits = observe_commits(
        workspace, operation, revision, ancestry_order=ancestry_order
    )
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


def resolve_remote_url(
    local: LocalObservation, remote: str, *, push: bool = False
) -> str:
    if not remote:
        raise ValueError("remote must be nonempty")
    command = [
        "git",
        f"--git-dir={local.git_common_dir}",
        "remote",
        "get-url",
    ]
    if push:
        command.extend(("--push", "--all"))
    command.append(remote)
    urls = _run(command).splitlines()
    if not urls or any(not url for url in urls):
        raise SourceMismatch("selected remote URL is unavailable")
    if push and len(urls) != 1:
        raise SourceMismatch("selected remote does not have one unambiguous push URL")
    return urls[0]


def resolve_push_url(local: LocalObservation, remote: str) -> str:
    return resolve_remote_url(local, remote, push=True)


def observe_live_refs(
    push_url: str,
    repository: GitHubRepositoryId,
    full_names: Sequence[str],
    *,
    cwd: str | Path | None = None,
) -> tuple[LiveRemoteRef, ...]:
    if len(set(full_names)) != len(full_names):
        raise ValueError("requested live refs must be unique")
    refs = tuple(RemoteBranchRef(repository, name) for name in full_names)
    result = subprocess.run(
        ["git", "ls-remote", "--heads", push_url, *full_names],
        cwd=cwd,
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


def observe_snapshot(
    workspace: str | Path,
    github: GitHubClient,
    *,
    revision: str,
    config_keys: Sequence[str],
    remote: str,
    selected_pr_number: int,
    local: LocalObservation | None = None,
) -> Snapshot:
    """Assemble complete source facts for one explicitly selected PR's stack."""
    local = local or observe_local(workspace, revision=revision, config_keys=config_keys)
    push_url = resolve_push_url(local, remote)
    fetch_url = resolve_remote_url(local, remote)
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
    live_refs = observe_live_refs(
        push_url,
        repository.identity,
        ref_names,
        cwd=workspace,
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
        fetch_url,
    )


def observe_tracked_restore(
    workspace: str | Path,
    github: GitHubClient,
    *,
    local: LocalObservation,
    tool_state: ToolStateRead,
    repository: GitHubRepository,
    push_url: str,
    remote: str,
    tracked: TrackedStack,
    desired: DesiredStack,
    pull_requests: Sequence[GitHubPullRequest] | None = None,
) -> TrackedRestoreObservation:
    """Observe every tracked active PR independently of current stack shape."""
    prs = (
        tuple(pull_requests)
        if pull_requests is not None
        else github.pull_requests(tracked.ordered_prs)
    )
    if tuple(pr.identity for pr in prs) != tracked.ordered_prs:
        raise SourceMismatch("tracked PR observations are incomplete or out of order")
    stack_ids = {pr.stack.identity for pr in prs if pr.stack is not None}
    current_stack = (
        github.stack(repository, next(iter(stack_ids)))
        if len(stack_ids) == 1
        else None
    )
    ref_names = tuple(
        dict.fromkeys(
            (
                f"refs/heads/{tracked.base_branch}",
                *(f"refs/heads/{pr.head_branch}" for pr in prs),
                *(f"refs/heads/{pr.base_branch}" for pr in prs),
            )
        )
    )
    return TrackedRestoreObservation(
        local,
        tool_state,
        repository,
        push_url,
        remote,
        tracked,
        prs,
        current_stack,
        observe_live_refs(push_url, repository.identity, ref_names, cwd=workspace),
        desired,
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
            {"repository", "base_branch", "ordered_prs", "detached_prs"},
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
            tuple(
                pull_request(pr, f"{context}.detached_prs[{pr_index}]")
                for pr_index, pr in enumerate(
                    array(raw.get("detached_prs"), f"{context}.detached_prs")
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
        return LastAdoptedHead(
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
            {"stacks", "last_published_heads", "last_adopted_heads"},
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
        adoptions = tuple(
            last_adopted_head(value, index)
            for index, value in enumerate(
                array(raw.get("last_adopted_heads"), "last_adopted_heads")
            )
        )
        state = TrackedState(stacks, publications, adoptions)
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
    for stack in state.stacks:
        require_text(
            stack.base_branch,
            stack.repository.host,
            stack.repository.node_id,
        )
        if not stack.ordered_prs and not stack.detached_prs:
            raise ValueError("exhausted tracked stack must be absent")
        if set(stack.ordered_prs).intersection(stack.detached_prs):
            raise ValueError("active and detached tracked identities overlap")
        for pr in (*stack.ordered_prs, *stack.detached_prs):
            require_text(pr.repository.host, pr.repository.node_id)
            if pr.repository != stack.repository:
                raise ValueError("stack contains a PR from another repository")
            membership = (stack.repository, pr)
            if membership in memberships:
                raise ValueError("overlapping tracked stack membership")
            memberships.add(membership)
    for authority in (*state.last_published_heads, *state.last_adopted_heads):
        require_text(
            authority.pr.repository.host,
            authority.pr.repository.node_id,
            authority.ref.repository.host,
            authority.ref.repository.node_id,
            authority.ref.full_name,
            authority.verified_commit_id,
        )
        if (
            authority.pr.repository != authority.ref.repository
            or not authority.ref.full_name.startswith("refs/heads/")
        ):
            raise ValueError("invalid head authority")
        key = (authority.ref.repository, authority.pr, authority.ref.full_name)
        if key in authority_keys:
            raise ValueError("ambiguous head authority")
        authority_keys.add(key)


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


def list_detached_associations(
    workspace: str | Path,
) -> tuple[DetachedAssociation, ...]:
    """List all locally bounded detached PRs without contacting GitHub."""
    _oid, state = read_state(workspace)
    return tuple(
        DetachedAssociation(stack.repository, stack.base_branch, pr)
        for stack in state.stacks
        for pr in stack.detached_prs
    )


def _selected_detached_stack(
    state: TrackedState, selected: DetachedAssociation
) -> tuple[int, TrackedStack]:
    matches = tuple(
        (index, stack)
        for index, stack in enumerate(state.stacks)
        if stack.repository == selected.repository
        and stack.base_branch == selected.base_branch
        and selected.pr in stack.detached_prs
    )
    if len(matches) != 1:
        raise Error("selected detached association is absent, foreign, or ambiguous")
    return matches[0]


def _remove_detached_association(
    state: TrackedState, selected: DetachedAssociation
) -> TrackedState:
    index, stack = _selected_detached_stack(state, selected)
    updated = replace(
        stack,
        detached_prs=tuple(pr for pr in stack.detached_prs if pr != selected.pr),
    )
    stacks = list(state.stacks)
    if updated.ordered_prs or updated.detached_prs:
        stacks[index] = updated
    else:
        del stacks[index]
    return replace(state, stacks=tuple(stacks))


def resolve_detached_association(
    workspace: str | Path, pr_number: int
) -> DetachedAssociation | Blocked:
    """Resolve a human PR number only within locally bounded associations."""
    if type(pr_number) is not int or pr_number <= 0:
        raise Error("pull request number must be a positive integer")
    matches = [
        association
        for association in list_detached_associations(workspace)
        if association.pr.number == pr_number
    ]
    if len(matches) != 1:
        return _block(
            "detached-pr-resolution",
            f"PR #{pr_number}",
            "no unique bounded detached association has that PR number",
        )
    return matches[0]


def forget_detached_association(
    workspace: str | Path, selected: DetachedAssociation
) -> DetachedAssociationResult:
    """Forget one local cleanup association without making a remote call."""
    with repository_lock(workspace):
        state_oid, state = read_state(workspace)
        if read_ref_oid(workspace, OPERATION_REF) is not None:
            raise ConcurrentUpdate("another operation is already active")
        written = cas_write_state(
            workspace, state_oid, _remove_detached_association(state, selected)
        )
        return DetachedAssociationResult(selected, written)


def close_detached_association(
    workspace: str | Path, github: GitHubClient, selected: DetachedAssociation
) -> DetachedAssociationResult:
    """Close exactly one bounded detached PR, verify it, then forget it."""
    with repository_lock(workspace):
        state_oid, state = read_state(workspace)
        if read_ref_oid(workspace, OPERATION_REF) is not None:
            raise ConcurrentUpdate("another operation is already active")
        _selected_detached_stack(state, selected)
        repository = github.resolve_repository(selected.repository)
        observed = github.pull_requests((selected.pr,))[0]
        if observed.identity != selected.pr:
            raise SourceMismatch("GitHub returned a foreign pull request")
        if observed.state is PullRequestState.OPEN:
            try:
                github.update_pull_request(
                    repository,
                    selected.pr,
                    state=PullRequestUpdateState.CLOSED,
                )
            except Error:
                pass
        elif observed.state is not PullRequestState.CLOSED:
            raise Error("detached pull request cannot be closed")
        observed = github.pull_requests((selected.pr,))[0]
        if observed.state is not PullRequestState.CLOSED:
            raise Error("detached pull request is not closed")
        written = cas_write_state(
            workspace, state_oid, _remove_detached_association(state, selected)
        )
        return DetachedAssociationResult(selected, written)


def _validate_first_publication(operation: FirstPublication) -> None:
    if (
        not operation.repository.host
        or not operation.repository.node_id
        or not operation.repository_name
        or not operation.push_url
        or not operation.remote
        or not operation.base_branch
        or not operation.slots
    ):
        raise ValueError("first-publication identity and goal must be nonempty")
    owner, separator, name = operation.repository_name.partition("/")
    if not separator or not owner or not name or "/" in name:
        raise ValueError("first-publication repository name must be owner/name")
    if len({key for key, _value in operation.effective_config}) != len(
        operation.effective_config
    ) or any(not key for key, _value in operation.effective_config):
        raise ValueError("first-publication effective config must have unique keys")
    if (
        not operation.workspace_targets
        or len({name for name, _target in operation.workspace_targets})
        != len(operation.workspace_targets)
        or any(not name or not target for name, target in operation.workspace_targets)
    ):
        raise ValueError("first-publication workspace targets must be complete")
    if len({slot.slot_id for slot in operation.slots}) != len(operation.slots):
        raise ValueError("first-publication slot IDs must be unique")
    if len({slot.branch for slot in operation.slots}) != len(operation.slots):
        raise ValueError("first-publication branches must be unique")
    if operation.target is FirstPublicationTarget.STANDALONE:
        if (
            len(operation.slots) != 1
            or operation.existing_members
            or operation.existing_stack is not None
            or operation.stack_phase is not StackLinkPhase.NOT_REQUIRED
        ):
            raise ValueError("standalone publication must contain exactly one new slot")
    elif operation.target is FirstPublicationTarget.FRESH_STACK:
        if (
            len(operation.slots) < 2
            or operation.existing_members
            or operation.existing_stack is not None
            or operation.stack_phase is StackLinkPhase.NOT_REQUIRED
        ):
            raise ValueError("fresh stack publication requires at least two new slots")
    elif (
        not operation.existing_members
        or operation.existing_stack is None
        or operation.stack_phase is StackLinkPhase.NOT_REQUIRED
    ):
        raise ValueError("append publication requires a frozen existing stack")
    if operation.existing_stack is not None and (
        operation.existing_stack.repository != operation.repository
    ):
        raise ValueError("existing stack belongs to another repository")
    if (operation.stack_phase is StackLinkPhase.VERIFIED) != (
        operation.resulting_stack is not None
    ):
        raise ValueError("verified stack link must record the resulting stack")
    if operation.resulting_stack is not None and (
        operation.resulting_stack.repository != operation.repository
    ):
        raise ValueError("resulting stack belongs to another repository")
    for member in operation.existing_members:
        if (
            member.identity.repository != operation.repository
            or not member.head_branch
            or not member.head_commit_id
            or not member.base_branch
            or not member.base_commit_id
            or not member.title
        ):
            raise ValueError("existing append member is malformed")
    for slot in operation.slots:
        if (
            not slot.slot_id
            or not slot.branch
            or not slot.commit_id
            or not slot.base_branch
            or not slot.title
            or slot.marker in slot.body
        ):
            raise ValueError("first-publication slot is malformed")
        bound = slot.phase in {NewPRPhase.BOUND, NewPRPhase.VERIFIED}
        if bound != (slot.pr_identity is not None):
            raise ValueError("slot binding does not match its phase")
        if (
            slot.pr_identity is not None
            and slot.pr_identity.repository != operation.repository
        ):
            raise ValueError("slot PR belongs to another repository")
    phases = {slot.phase for slot in operation.slots}
    if operation.phase is FirstPublicationPhase.PREPARING_BOOKMARKS and phases != {
        NewPRPhase.NOT_ATTEMPTED
    }:
        raise ValueError("bookmark preparation cannot follow publication effects")
    if operation.phase is FirstPublicationPhase.PUBLISHING and not phases <= {
        NewPRPhase.NOT_ATTEMPTED,
        NewPRPhase.PUBLICATION_POSSIBLY_SENT,
        NewPRPhase.READY,
    }:
        raise ValueError("publishing phase has invalid slot progress")
    if operation.phase is FirstPublicationPhase.CREATING_PRS and not phases <= {
        NewPRPhase.READY,
        NewPRPhase.POSSIBLY_SENT,
        NewPRPhase.BOUND,
        NewPRPhase.VERIFIED,
    }:
        raise ValueError("PR creation phase has invalid slot progress")
    if operation.phase is FirstPublicationPhase.LINKING_STACK and phases != {
        NewPRPhase.VERIFIED
    }:
        raise ValueError("stack linking requires every PR verified")
    if operation.phase is FirstPublicationPhase.COMMITTING and phases != {
        NewPRPhase.VERIFIED
    }:
        raise ValueError("committing requires every slot verified")
    if (
        operation.target is not FirstPublicationTarget.STANDALONE
        and operation.phase is FirstPublicationPhase.COMMITTING
        and operation.stack_phase is not StackLinkPhase.VERIFIED
    ):
        raise ValueError("stack publication cannot commit before topology verification")
    committing = operation.phase is FirstPublicationPhase.COMMITTING
    if committing != (
        operation.final_state_json is not None and operation.final_state_oid is not None
    ):
        raise ValueError("committing phase must freeze the exact final state blob")
    if committing:
        assert operation.final_state_json is not None
        parse_state(operation.final_state_json)


def first_publication_to_json(operation: FirstPublication) -> str:
    _validate_first_publication(operation)
    payload = asdict(operation)
    payload["operation_kind"] = "first-publication"
    return json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"


def parse_first_publication(data: str) -> FirstPublication:
    def exact(value: object, names: set[str], context: str) -> dict[str, object]:
        if not isinstance(value, dict) or set(value) != names:
            raise ValueError(f"{context} has unexpected fields")
        return value

    def text(value: object, context: str) -> str:
        if not isinstance(value, str) or not value:
            raise ValueError(f"{context} must be nonempty text")
        return value

    def optional_text(value: object, context: str) -> str | None:
        return None if value is None else text(value, context)

    def repository(value: object, context: str) -> GitHubRepositoryId:
        raw = exact(value, {"host", "node_id"}, context)
        return GitHubRepositoryId(
            text(raw["host"], f"{context}.host"),
            text(raw["node_id"], f"{context}.node_id"),
        )

    def pr(value: object, context: str) -> PullRequestId:
        raw = exact(value, {"repository", "number"}, context)
        number = raw["number"]
        if type(number) is not int or number <= 0:
            raise ValueError(f"{context}.number must be a positive integer")
        return PullRequestId(
            repository(raw["repository"], f"{context}.repository"),
            number,
        )

    def stack_identity(value: object, context: str) -> GitHubStackId | None:
        if value is None:
            return None
        raw = exact(value, {"repository", "number"}, context)
        number = raw["number"]
        if type(number) is not int or number <= 0:
            raise ValueError(f"{context}.number must be a positive integer")
        return GitHubStackId(
            repository(raw["repository"], f"{context}.repository"),
            number,
        )

    try:
        raw = exact(
            json.loads(data),
            {
                "operation_kind",
                "repository",
                "repository_name",
                "push_url",
                "remote",
                "effective_config",
                "workspace_targets",
                "base_branch",
                "expected_state_oid",
                "target",
                "existing_members",
                "existing_stack",
                "slots",
                "stack_phase",
                "resulting_stack",
                "phase",
                "final_state_json",
                "final_state_oid",
            },
            "operation",
        )
        if raw["operation_kind"] != "first-publication":
            raise ValueError("operation kind is invalid")
        raw_slots = raw["slots"]
        raw_existing = raw["existing_members"]
        raw_config = raw["effective_config"]
        raw_workspaces = raw["workspace_targets"]
        if (
            not isinstance(raw_slots, list)
            or not isinstance(raw_existing, list)
            or not isinstance(raw_config, list)
            or not isinstance(raw_workspaces, list)
        ):
            raise ValueError("operation slots, config, and workspaces must be arrays")

        def pairs(value: list[object], context: str) -> tuple[tuple[str, str], ...]:
            parsed: list[tuple[str, str]] = []
            for index, item in enumerate(value):
                if (
                    not isinstance(item, list)
                    or len(item) != 2
                    or not all(isinstance(part, str) for part in item)
                ):
                    raise ValueError(f"{context}[{index}] must be a text pair")
                parsed.append((item[0], item[1]))  # type: ignore[arg-type]
            return tuple(parsed)

        existing_members: list[ExistingFirstPublicationPR] = []
        for index, value in enumerate(raw_existing):
            context = f"existing_members[{index}]"
            member = exact(
                value,
                {
                    "identity",
                    "head_branch",
                    "head_commit_id",
                    "base_branch",
                    "base_commit_id",
                    "draft",
                    "title",
                    "body",
                },
                context,
            )
            draft = member["draft"]
            body = member["body"]
            if type(draft) is not bool or not isinstance(body, str):
                raise ValueError(f"{context} scalar fields are malformed")
            existing_members.append(
                ExistingFirstPublicationPR(
                    pr(member["identity"], f"{context}.identity"),
                    text(member["head_branch"], f"{context}.head_branch"),
                    text(member["head_commit_id"], f"{context}.head_commit_id"),
                    text(member["base_branch"], f"{context}.base_branch"),
                    text(member["base_commit_id"], f"{context}.base_commit_id"),
                    draft,
                    text(member["title"], f"{context}.title"),
                    body,
                )
            )

        slots: list[NewPRSlot] = []
        for index, value in enumerate(raw_slots):
            context = f"slots[{index}]"
            slot = exact(
                value,
                {
                    "slot_id",
                    "branch",
                    "commit_id",
                    "bookmark_setup",
                    "base_branch",
                    "title",
                    "body",
                    "phase",
                    "pr_identity",
                },
                context,
            )
            slots.append(
                NewPRSlot(
                    text(slot["slot_id"], f"{context}.slot_id"),
                    text(slot["branch"], f"{context}.branch"),
                    text(slot["commit_id"], f"{context}.commit_id"),
                    BookmarkSetup(
                        text(slot["bookmark_setup"], f"{context}.bookmark_setup")
                    ),
                    text(slot["base_branch"], f"{context}.base_branch"),
                    text(slot["title"], f"{context}.title"),
                    slot["body"] if isinstance(slot["body"], str) else None,  # type: ignore[arg-type]
                    NewPRPhase(text(slot["phase"], f"{context}.phase")),
                    (
                        None
                        if slot["pr_identity"] is None
                        else pr(slot["pr_identity"], f"{context}.pr_identity")
                    ),
                )
            )
            if slots[-1].body is None:
                raise ValueError(f"{context}.body must be text")
        expected_oid = raw["expected_state_oid"]
        if expected_oid is not None and not isinstance(expected_oid, str):
            raise ValueError("expected_state_oid must be text or null")
        operation = FirstPublication(
            repository(raw["repository"], "operation.repository"),
            text(raw["repository_name"], "operation.repository_name"),
            text(raw["push_url"], "operation.push_url"),
            text(raw["remote"], "operation.remote"),
            pairs(raw_config, "effective_config"),
            pairs(raw_workspaces, "workspace_targets"),
            text(raw["base_branch"], "operation.base_branch"),
            expected_oid,
            FirstPublicationTarget(text(raw["target"], "operation.target")),
            tuple(existing_members),
            stack_identity(raw["existing_stack"], "operation.existing_stack"),
            tuple(slots),
            StackLinkPhase(text(raw["stack_phase"], "operation.stack_phase")),
            stack_identity(raw["resulting_stack"], "operation.resulting_stack"),
            FirstPublicationPhase(text(raw["phase"], "operation.phase")),
            optional_text(raw["final_state_json"], "operation.final_state_json"),
            optional_text(raw["final_state_oid"], "operation.final_state_oid"),
        )
        _validate_first_publication(operation)
        return operation
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise Error(f"invalid refs/jj-stack/operation payload: {exc}") from exc


def topology_repair_to_json(operation: TopologyRepair) -> str:
    _validate_topology_repair(operation)
    payload = asdict(operation)
    payload["operation_kind"] = "topology-repair"
    source = operation.plan.source
    encoded_source = payload["plan"]["dependencies"]["source"]
    if isinstance(source, TrackedRestoreSource):
        encoded_source["source_kind"] = "tracked-restore"
    elif isinstance(source, StandalonePullRequestSet):
        encoded_source["source_kind"] = "standalone-set"
    else:
        encoded_source["source_kind"] = (
            "standalone" if isinstance(source, StandalonePullRequest) else "server-stack"
        )
    for encoded, update in zip(
        payload["plan"]["head_updates"], operation.plan.head_updates, strict=True
    ):
        encoded["authority"]["authority_kind"] = (
            "fast-forward"
            if isinstance(update.authority, FastForward)
            else (
                "last-publication"
                if isinstance(update.authority, MatchesLastPublication)
                else "last-adoption"
            )
        )
    return (
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            default=lambda value: (
                value.value if isinstance(value, Enum) else TypeError()
            ),
        )
        + "\n"
    )


def _validate_topology_repair(operation: TopologyRepair) -> None:
    plan = operation.plan
    if (
        not plan.remote
        or plan.repository != plan.desired.repository
        or not re.fullmatch(r"[^/]+/[^/]+", plan.repository_name)
        or len({update.ref for update in plan.head_updates}) != len(plan.head_updates)
    ):
        raise ValueError("topology repair repository and remote must be frozen")
    committing = operation.phase is TopologyRepairPhase.COMMITTING
    if committing != (
        operation.final_state_json is not None and operation.final_state_oid is not None
    ):
        raise ValueError("committing topology repair must freeze final state")
    if operation.final_state_json is not None:
        parse_state(operation.final_state_json)


def parse_topology_repair(data: str) -> TopologyRepair:
    """Strict decoder for the durable topology operation format."""

    def record(value: object, names: set[str], where: str) -> dict[str, object]:
        if not isinstance(value, dict) or set(value) != names:
            raise ValueError(f"{where} has unexpected fields")
        return value

    def seq(value: object, where: str) -> list[object]:
        if not isinstance(value, list):
            raise ValueError(f"{where} must be an array")
        return value

    def text(value: object, where: str) -> str:
        if not isinstance(value, str) or not value:
            raise ValueError(f"{where} must be nonempty text")
        return value

    def boolean(value: object, where: str) -> bool:
        if type(value) is not bool:
            raise ValueError(f"{where} must be boolean")
        return value

    def rid(value: object) -> GitHubRepositoryId:
        raw = record(value, {"host", "node_id"}, "repository")
        return GitHubRepositoryId(
            text(raw["host"], "host"), text(raw["node_id"], "repository ID")
        )

    def pid(value: object) -> PullRequestId:
        raw = record(value, {"repository", "number"}, "PR")
        number = raw["number"]
        if type(number) is not int or number <= 0:
            raise ValueError("PR number must be a positive integer")
        return PullRequestId(rid(raw["repository"]), number)

    def sid(value: object) -> GitHubStackId:
        raw = record(value, {"repository", "number"}, "stack identity")
        number = raw["number"]
        if type(number) is not int or number <= 0:
            raise ValueError("stack number must be a positive integer")
        return GitHubStackId(rid(raw["repository"]), number)

    def stack_summary(value: object) -> GitHubStackSummary:
        raw = record(value, {"identity", "node_id", "base_branch"}, "stack")
        return GitHubStackSummary(
            sid(raw["identity"]),
            text(raw["node_id"], "stack node ID"),
            text(raw["base_branch"], "stack base"),
        )

    def ref(value: object) -> RemoteBranchRef:
        raw = record(value, {"repository", "full_name"}, "ref")
        return RemoteBranchRef(
            rid(raw["repository"]), text(raw["full_name"], "ref name")
        )

    def live(value: object) -> LiveRemoteRef:
        raw = record(value, {"ref", "commit_id"}, "live ref")
        oid = raw["commit_id"]
        if oid is not None and not isinstance(oid, str):
            raise ValueError("live ref OID must be text or null")
        return LiveRemoteRef(ref(raw["ref"]), oid)

    def tracked(value: object) -> TrackedStack:
        raw = record(
            value,
            {"repository", "base_branch", "ordered_prs", "detached_prs"},
            "tracked stack",
        )
        return TrackedStack(
            rid(raw["repository"]),
            text(raw["base_branch"], "tracked base"),
            tuple(pid(item) for item in seq(raw["ordered_prs"], "tracked PRs")),
            tuple(pid(item) for item in seq(raw["detached_prs"], "detached PRs")),
        )

    def membership(value: object) -> TopologySource:
        if not isinstance(value, dict):
            raise ValueError("source must be an object")
        raw = dict(value)
        kind = raw.pop("source_kind", None)
        if kind == "tracked-restore":
            restore = record(raw, {"tracked", "current_stack"}, "restore source")
            current = restore["current_stack"]
            if current is None:
                parsed_current = None
            else:
                current_raw = dict(current) if isinstance(current, dict) else {}
                current_raw["source_kind"] = "server-stack"
                parsed = membership(current_raw)
                if not isinstance(parsed, ServerStackMembership):
                    raise ValueError("restore current stack must be a server stack")
                parsed_current = parsed
            return TrackedRestoreSource(tracked(restore["tracked"]), parsed_current)
        if kind == "standalone":
            return StandalonePullRequest(
                pid(record(raw, {"pr"}, "standalone source")["pr"])
            )
        if kind == "standalone-set":
            return StandalonePullRequestSet(
                tuple(
                    pid(item)
                    for item in seq(
                        record(raw, {"prs"}, "standalone-set source")["prs"],
                        "standalone-set PRs",
                    )
                )
            )
        source = record(raw, {"selected_pr", "stack", "ordered_prs"}, "stack source")
        if kind != "server-stack":
            raise ValueError("server stack source is invalid")
        return ServerStackMembership(
            pid(source["selected_pr"]),
            stack_summary(source["stack"]),
            tuple(pid(item) for item in seq(source["ordered_prs"], "stack PRs")),
        )

    def receipt(value: object, adopted: bool) -> LastPublishedHead | LastAdoptedHead:
        raw = record(value, {"pr", "ref", "verified_commit_id"}, "receipt")
        values = (
            pid(raw["pr"]),
            ref(raw["ref"]),
            text(raw["verified_commit_id"], "receipt OID"),
        )
        return LastAdoptedHead(*values) if adopted else LastPublishedHead(*values)

    def authority(value: object) -> Authority:
        if not isinstance(value, dict):
            raise ValueError("authority must be an object")
        raw = dict(value)
        kind = raw.pop("authority_kind", None)
        if kind == "fast-forward":
            record(raw, set(), "fast-forward authority")
            return FastForward()
        field = "publication" if kind == "last-publication" else "adoption"
        item = record(raw, {field}, "receipt authority")[field]
        if kind == "last-publication":
            return MatchesLastPublication(receipt(item, False))  # type: ignore[arg-type]
        if kind == "last-adoption":
            return MatchesLastAdoption(receipt(item, True))  # type: ignore[arg-type]
        raise ValueError("authority kind is invalid")

    try:
        top = record(
            json.loads(data),
            {
                "operation_kind",
                "plan",
                "phase",
                "unstack_possibly_sent",
                "temporary_bases_possibly_sent",
                "relink_possibly_sent",
                "final_state_json",
                "final_state_oid",
            },
            "operation",
        )
        if top["operation_kind"] != "topology-repair":
            raise ValueError("operation kind is invalid")
        plan_raw = record(
            top["plan"],
            {field.name for field in dataclasses.fields(TopologyPlan)},
            "plan",
        )
        desired_raw = record(
            plan_raw["desired"], {"repository", "base_branch", "active"}, "desired"
        )
        desired = DesiredStack(
            rid(desired_raw["repository"]),
            text(desired_raw["base_branch"], "desired base"),
            tuple(
                DesiredExistingPR(
                    pid(item["pr_identity"]),
                    text(item["desired_commit_id"], "desired OID"),
                    text(item["title"], "title"),
                    item["body"]
                    if isinstance(item["body"], str)
                    else (_ for _ in ()).throw(ValueError("body must be text")),
                )
                for item in (
                    record(
                        value,
                        {"pr_identity", "desired_commit_id", "title", "body"},
                        "desired PR",
                    )
                    for value in seq(desired_raw["active"], "desired PRs")
                )
            ),
        )
        dep_raw = record(
            plan_raw["dependencies"],
            {field.name for field in dataclasses.fields(TopologyDependencies)},
            "dependencies",
        )
        prs = tuple(
            _parse_topology_pr(value, record, rid, pid, sid, text, boolean)
            for value in seq(dep_raw["prs"], "PRs")
        )
        dependencies = TopologyDependencies(
            tuple(tuple(item) for item in seq(dep_raw["effective_config"], "config")),
            text(dep_raw["push_url"], "push URL"),
            dep_raw["state_blob_oid"],
            dep_raw["operation_blob_oid"],
            prs,
            membership(dep_raw["source"]),
            tuple(live(item) for item in seq(dep_raw["live_heads"], "heads")),
            tuple(live(item) for item in seq(dep_raw["live_bases"], "bases")),
        )
        updates = tuple(
            PlannedHeadUpdate(
                ref(item["ref"]),
                text(item["expected_old_commit_id"], "old OID"),
                text(item["new_commit_id"], "new OID"),
                authority(item["authority"]),
            )
            for item in (
                record(
                    value,
                    {"ref", "expected_old_commit_id", "new_commit_id", "authority"},
                    "head update",
                )
                for value in seq(plan_raw["head_updates"], "head updates")
            )
        )
        bases = tuple(
            PlannedTemporaryBase(
                pid(item["pr_identity"]),
                ref(item["ref"]),
                text(item["old_base_commit_id"], "old base"),
                text(item["new_base_commit_id"], "new base"),
            )
            for item in (
                record(
                    value,
                    {"pr_identity", "ref", "old_base_commit_id", "new_base_commit_id"},
                    "temporary base",
                )
                for value in seq(plan_raw["temporary_bases"], "temporary bases")
            )
        )
        tracking_raw = plan_raw["tracking_update"]
        tracking = None
        if tracking_raw is not None:
            item = record(
                tracking_raw,
                {"repository", "base_branch", "ordered_prs", "detached_prs"},
                "tracking update",
            )
            tracking = TrackedStack(
                rid(item["repository"]),
                text(item["base_branch"], "tracked base"),
                tuple(pid(value) for value in seq(item["ordered_prs"], "tracked PRs")),
                tuple(
                    pid(value) for value in seq(item["detached_prs"], "detached PRs")
                ),
            )
        plan = TopologyPlan(
            rid(plan_raw["repository"]),
            text(plan_raw["repository_name"], "repository name"),
            text(plan_raw["remote"], "remote"),
            desired,
            tuple(pid(item) for item in seq(plan_raw["detached_prs"], "detached PRs")),
            updates,
            bases,
            tracking,
            dependencies,
        )
        final_json, final_oid = top["final_state_json"], top["final_state_oid"]
        if (
            final_json is not None
            and not isinstance(final_json, str)
            or final_oid is not None
            and not isinstance(final_oid, str)
        ):
            raise ValueError("final state fields must be text or null")
        operation = TopologyRepair(
            plan,
            TopologyRepairPhase(text(top["phase"], "phase")),
            boolean(top["unstack_possibly_sent"], "unstack marker"),
            tuple(
                pid(item)
                for item in seq(
                    top["temporary_bases_possibly_sent"], "temporary bases marker"
                )
            ),
            boolean(top["relink_possibly_sent"], "relink marker"),
            final_json,
            final_oid,
        )
        _validate_topology_repair(operation)
        return operation
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise Error(f"invalid refs/jj-stack/operation payload: {exc}") from exc


def _parse_topology_pr(value, record, rid, pid, sid, text, boolean) -> GitHubPullRequest:
    raw = record(
        value, {field.name for field in dataclasses.fields(GitHubPullRequest)}, "PR"
    )
    if not isinstance(raw["body"], str):
        raise ValueError("PR scalar fields are malformed")
    head_repository = raw["head_repository"]
    if head_repository is not None:
        head_repository = rid(head_repository)
    stack = raw["stack"]
    if stack is not None:
        stack_raw = record(stack, {"identity", "node_id", "base_branch"}, "PR stack")
        stack = GitHubStackSummary(
            sid(stack_raw["identity"]),
            text(stack_raw["node_id"], "stack node ID"),
            text(stack_raw["base_branch"], "stack base"),
        )
    return GitHubPullRequest(
        pid(raw["identity"]),
        text(raw["node_id"], "PR node ID"),
        PullRequestState(text(raw["state"], "PR state")),
        boolean(raw["draft"], "draft"),
        head_repository,
        text(raw["head_branch"], "head branch"),
        text(raw["head_oid"], "head OID"),
        text(raw["base_branch"], "base branch"),
        text(raw["base_oid"], "base OID"),
        boolean(raw["auto_merge_enabled"], "auto merge"),
        boolean(raw["in_merge_queue"], "merge queue"),
        text(raw["title"], "title"),
        raw["body"],
        stack,
    )


def _validate_membership_repair(operation: MembershipRepair) -> None:
    if (
        not re.fullmatch(r"[^/]+/[^/]+", operation.repository_name)
        or not operation.push_url
        or not operation.remote
        or not operation.base_branch
    ):
        raise ValueError("membership repair repository and source must be frozen")
    if (
        len({key for key, _value in operation.effective_config})
        != len(operation.effective_config)
        or any(not key for key, _value in operation.effective_config)
        or not operation.workspace_targets
        or len({name for name, _target in operation.workspace_targets})
        != len(operation.workspace_targets)
        or any(not name or not target for name, target in operation.workspace_targets)
    ):
        raise ValueError("membership repair config and workspaces must be complete")
    source = (
        (operation.source_membership.pr,)
        if isinstance(operation.source_membership, StandalonePullRequest)
        else operation.source_membership.prs
        if isinstance(operation.source_membership, StandalonePullRequestSet)
        else operation.source_membership.ordered_prs
    )
    observed = tuple(pr.identity for pr in operation.source_prs)
    if (
        observed != source
        or any(pr.state is not PullRequestState.OPEN for pr in operation.source_prs)
        or any(identity.repository != operation.repository for identity in source)
    ):
        raise ValueError("membership repair source observations do not match")
    if isinstance(operation.source_membership, StandalonePullRequestSet):
        if operation.source_tracked is not None:
            raise ValueError("standalone-set repair must not claim a tracked source")
    elif (
        operation.source_tracked is None
        or operation.source_tracked.repository != operation.repository
        or operation.source_tracked.base_branch != operation.base_branch
        or operation.source_tracked.ordered_prs != source
    ):
        raise ValueError("membership repair tracked source does not match")
    existing = tuple(
        item.pr_identity
        for item in operation.desired
        if isinstance(item, ExistingMembershipEntry)
    )
    desired_slots = tuple(
        item.slot_id
        for item in operation.desired
        if isinstance(item, NewMembershipEntry)
    )
    slot_ids = tuple(slot.slot_id for slot in operation.slots)
    if (
        not operation.desired
        or len(set(existing)) != len(existing)
        or any(identity not in source for identity in existing)
        or len(set(desired_slots)) != len(desired_slots)
        or desired_slots != tuple(item for item in desired_slots if item)
        or len(set(slot_ids)) != len(slot_ids)
        or set(desired_slots) != set(slot_ids)
        or len(desired_slots) != len(slot_ids)
    ):
        raise ValueError("membership repair desired membership does not match source and slots")
    slot_by_id = {slot.slot_id: slot for slot in operation.slots}
    pr_by_id = {pr.identity: pr for pr in operation.source_prs}
    previous_branch = operation.base_branch
    previous_commit: str | None = None
    for item in operation.desired:
        if isinstance(item, ExistingMembershipEntry):
            pr = pr_by_id[item.pr_identity]
            previous_branch = pr.head_branch
            updates = tuple(
                update for update in operation.head_updates
                if update.ref.full_name == f"refs/heads/{pr.head_branch}"
            )
            previous_commit = updates[0].new_commit_id if updates else pr.head_oid
        else:
            slot = slot_by_id[item.slot_id]
            if slot.base_branch != previous_branch:
                raise ValueError("membership repair slot predecessor is not frozen exactly")
            previous_branch, previous_commit = slot.branch, slot.commit_id
    metadata_ids = tuple(item.pr_identity for item in operation.metadata_updates)
    if len(set(metadata_ids)) != len(metadata_ids) or any(identity not in existing for identity in metadata_ids):
        raise ValueError("membership repair metadata updates are not desired existing PRs")
    refs = tuple(update.ref for update in operation.head_updates)
    temporary_refs = tuple(item.ref for item in operation.temporary_bases)
    if (
        len(set(refs)) != len(refs)
        or len(set(temporary_refs)) != len(temporary_refs)
        or set(refs) & set(temporary_refs)
        or len({item.pr_identity for item in operation.temporary_bases})
        != len(operation.temporary_bases)
    ):
        raise ValueError("membership repair refs and temporary resources must be unique")
    phase_order = list(MembershipRepairPhase)
    phase_index = phase_order.index(operation.phase)
    slot_phases = {slot.phase for slot in operation.slots}
    if operation.slots and operation.phase is MembershipRepairPhase.PREPARING_BOOKMARKS and slot_phases != {
        NewPRPhase.NOT_ATTEMPTED
    }:
        raise ValueError("bookmark preparation has incompatible slot progress")
    if operation.phase is MembershipRepairPhase.PUBLISHING_NEW_REFS and not slot_phases <= {
        NewPRPhase.NOT_ATTEMPTED,
        NewPRPhase.PUBLICATION_POSSIBLY_SENT,
        NewPRPhase.READY,
    }:
        raise ValueError("new-ref publication has incompatible slot progress")
    if operation.phase is MembershipRepairPhase.CREATING_PRS and not slot_phases <= {
        NewPRPhase.READY,
        NewPRPhase.POSSIBLY_SENT,
        NewPRPhase.BOUND,
        NewPRPhase.VERIFIED,
    }:
        raise ValueError("PR creation has incompatible slot progress")
    if operation.slots and phase_index >= 3 and slot_phases != {NewPRPhase.VERIFIED}:
        raise ValueError("source mutation requires every new PR verified")
    if operation.source_unstack_possibly_sent and phase_index < 3:
        raise ValueError("source unstack marker is incompatible with phase")
    if (
        isinstance(operation.source_membership, StandalonePullRequestSet)
        and operation.source_unstack_possibly_sent
    ):
        raise ValueError("standalone-set repair cannot send an unstack request")
    if operation.base_transitions_possibly_sent and phase_index < 4:
        raise ValueError("base transition markers are incompatible with phase")
    if operation.publication_possibly_sent and phase_index < 5:
        raise ValueError("publication markers are incompatible with phase")
    if operation.relink_possibly_sent and phase_index < 6:
        raise ValueError("relink marker is incompatible with phase")
    if not set(operation.base_transitions_possibly_sent) <= {
        item.pr_identity for item in operation.temporary_bases
    } or not set(operation.publication_possibly_sent) <= set(refs):
        raise ValueError("membership repair progress references unowned resources")
    committing = operation.phase is MembershipRepairPhase.COMMITTING
    if committing != (
        operation.final_state_json is not None and operation.final_state_oid is not None
    ):
        raise ValueError("committing membership repair must freeze final state")
    if operation.final_state_json is not None:
        parse_state(operation.final_state_json)


def _membership_carrier(operation: MembershipRepair) -> TopologyRepair:
    """Reuse the strict topology leaf codec without nesting an operation on disk."""
    dependencies = TopologyDependencies(
        operation.effective_config,
        operation.push_url,
        operation.expected_state_oid,
        None,
        operation.source_prs,
        operation.source_membership,
        tuple(LiveRemoteRef(ref, None) for ref in operation.publication_possibly_sent),
        (),
    )
    plan = TopologyPlan(
        operation.repository,
        operation.repository_name,
        operation.remote,
        DesiredStack(operation.repository, operation.base_branch, ()),
        operation.base_transitions_possibly_sent,
        operation.head_updates,
        operation.temporary_bases,
        operation.source_tracked,
        dependencies,
    )
    return TopologyRepair(plan)


def membership_repair_to_json(operation: MembershipRepair) -> str:
    _validate_membership_repair(operation)
    carrier = json.loads(topology_repair_to_json(_membership_carrier(operation)))
    plan = carrier["plan"]
    payload = asdict(operation)
    payload["operation_kind"] = "membership-repair"
    payload["source_membership"] = plan["dependencies"]["source"]
    payload["source_prs"] = plan["dependencies"]["prs"]
    payload["source_tracked"] = plan["tracking_update"]
    payload["head_updates"] = plan["head_updates"]
    payload["temporary_bases"] = plan["temporary_bases"]
    payload["base_transitions_possibly_sent"] = plan["detached_prs"]
    payload["publication_possibly_sent"] = [
        item["ref"] for item in plan["dependencies"]["live_heads"]
    ]
    payload["desired"] = [
        {
            "entry_kind": "existing",
            "pr_identity": asdict(item.pr_identity),
        }
        if isinstance(item, ExistingMembershipEntry)
        else {"entry_kind": "new", "slot_id": item.slot_id}
        for item in operation.desired
    ]
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=lambda value: value.value if isinstance(value, Enum) else TypeError()) + "\n"


def parse_membership_repair(data: str) -> MembershipRepair:
    try:
        raw = json.loads(data)
        names = {field.name for field in dataclasses.fields(MembershipRepair)} | {"operation_kind"}
        if not isinstance(raw, dict) or set(raw) != names:
            raise ValueError("operation has unexpected fields")
        if raw["operation_kind"] != "membership-repair":
            raise ValueError("operation kind is invalid")
        if not isinstance(raw["desired"], list) or not isinstance(raw["slots"], list):
            raise ValueError("desired and slots must be arrays")
        # The topology decoder is the canonical strict codec for shared source,
        # PR, authority, ref, temporary-base, and tracked-state leaf values.
        carrier = {
            "operation_kind": "topology-repair",
            "plan": {
                "repository": raw["repository"],
                "repository_name": raw["repository_name"],
                "remote": raw["remote"],
                "desired": {"repository": raw["repository"], "base_branch": raw["base_branch"], "active": []},
                "detached_prs": raw["base_transitions_possibly_sent"],
                "head_updates": raw["head_updates"],
                "temporary_bases": raw["temporary_bases"],
                "tracking_update": raw["source_tracked"],
                "dependencies": {
                    "effective_config": raw["effective_config"], "push_url": raw["push_url"],
                    "state_blob_oid": raw["expected_state_oid"], "operation_blob_oid": None,
                    "prs": raw["source_prs"], "source": raw["source_membership"],
                    "live_heads": [{"ref": item, "commit_id": None} for item in raw["publication_possibly_sent"]],
                    "live_bases": [],
                },
            },
            "phase": "creating-temporary-bases", "unstack_possibly_sent": False,
            "temporary_bases_possibly_sent": [], "relink_possibly_sent": False,
            "final_state_json": None, "final_state_oid": None,
        }
        parsed = parse_topology_repair(json.dumps(carrier)).plan
        desired: list[DesiredMembershipEntry] = []
        for item in raw["desired"]:
            if not isinstance(item, dict):
                raise ValueError("desired entry must be an object")
            kind = item.get("entry_kind")
            if kind == "existing" and set(item) == {"entry_kind", "pr_identity"}:
                probe = dict(carrier)
                probe["plan"] = dict(carrier["plan"])
                probe["plan"]["detached_prs"] = [item["pr_identity"]]
                identity = parse_topology_repair(json.dumps(probe)).plan.detached_prs[0]
                desired.append(ExistingMembershipEntry(identity))
            elif kind == "new" and set(item) == {"entry_kind", "slot_id"} and isinstance(item["slot_id"], str) and item["slot_id"]:
                desired.append(NewMembershipEntry(item["slot_id"]))
            else:
                raise ValueError("desired entry has unexpected fields")
        slots: list[NewPRSlot] = []
        slot_names = {field.name for field in dataclasses.fields(NewPRSlot)}
        for item in raw["slots"]:
            if not isinstance(item, dict) or set(item) != slot_names:
                raise ValueError("slot has unexpected fields")
            probe = dict(carrier)
            probe["plan"] = dict(carrier["plan"])
            identity = item["pr_identity"]
            parsed_identity = None
            if identity is not None:
                probe["plan"]["detached_prs"] = [identity]
                parsed_identity = parse_topology_repair(json.dumps(probe)).plan.detached_prs[0]
            scalar_names = ("slot_id", "branch", "commit_id", "base_branch", "title")
            if any(not isinstance(item[name], str) or not item[name] for name in scalar_names) or not isinstance(item["body"], str):
                raise ValueError("slot scalar fields are malformed")
            slots.append(NewPRSlot(item["slot_id"], item["branch"], item["commit_id"], BookmarkSetup(item["bookmark_setup"]), item["base_branch"], item["title"], item["body"], NewPRPhase(item["phase"]), parsed_identity))
        stack = raw["resulting_stack"]
        resulting = None
        if stack is not None:
            if not isinstance(stack, dict) or set(stack) != {"repository", "number"}:
                raise ValueError("resulting stack is malformed")
            repository_id = stack["repository"]
            number = stack["number"]
            if (
                not isinstance(repository_id, dict)
                or set(repository_id) != {"host", "node_id"}
                or not isinstance(repository_id["host"], str)
                or not repository_id["host"]
                or not isinstance(repository_id["node_id"], str)
                or not repository_id["node_id"]
                or type(number) is not int
                or number <= 0
            ):
                raise ValueError("resulting stack is malformed")
            resulting = GitHubStackId(
                GitHubRepositoryId(repository_id["host"], repository_id["node_id"]),
                number,
            )
        final_json, final_oid = raw["final_state_json"], raw["final_state_oid"]
        if (final_json is not None and not isinstance(final_json, str)) or (final_oid is not None and not isinstance(final_oid, str)):
            raise ValueError("final state fields must be text or null")
        # Decode metadata identities through the already strict desired identity decoder.
        metadata_by_number = {
            item.pr_identity.number: item.pr_identity
            for item in desired
            if isinstance(item, ExistingMembershipEntry)
        }
        if not isinstance(raw["metadata_updates"], list) or any(
            not isinstance(item, dict)
            or set(item) != {"pr_identity", "title", "body"}
            or not isinstance(item["pr_identity"], dict)
            or set(item["pr_identity"]) != {"repository", "number"}
            or item["pr_identity"].get("number") not in metadata_by_number
            or item["pr_identity"] != asdict(
                metadata_by_number.get(item["pr_identity"].get("number"))
            )
            or not isinstance(item["title"], str)
            or not isinstance(item["body"], str)
            for item in raw["metadata_updates"]
        ):
            raise ValueError("metadata updates are malformed")
        decoded_metadata = tuple(
            PRMetadataUpdate(
                metadata_by_number[item["pr_identity"]["number"]],
                item["title"],
                item["body"],
            )
            for item in raw["metadata_updates"]
        )
        operation = MembershipRepair(parsed.repository, parsed.repository_name, parsed.dependencies.push_url, parsed.remote, parsed.dependencies.effective_config, tuple(tuple(item) for item in raw["workspace_targets"]), parsed.desired.base_branch, parsed.dependencies.state_blob_oid, parsed.source, parsed.dependencies.prs, parsed.tracking_update, tuple(desired), tuple(slots), parsed.head_updates, parsed.temporary_bases, decoded_metadata, MembershipRepairPhase(raw["phase"]), raw["source_unstack_possibly_sent"], parsed.detached_prs, tuple(item.ref for item in parsed.dependencies.live_heads), raw["relink_possibly_sent"], resulting, final_json, final_oid)  # type: ignore[arg-type]
        if type(operation.source_unstack_possibly_sent) is not bool or type(operation.relink_possibly_sent) is not bool or any(not isinstance(item, list) or len(item) != 2 or not all(isinstance(value, str) for value in item) for item in raw["workspace_targets"]):
            raise ValueError("progress or workspace fields are malformed")
        _validate_membership_repair(operation)
        return operation
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError, Error) as exc:
        raise Error(f"invalid refs/jj-stack/operation payload: {exc}") from exc


def parse_operation(data: str) -> Operation:
    try:
        raw = json.loads(data)
    except json.JSONDecodeError as exc:
        raise Error(f"invalid refs/jj-stack/operation payload: {exc}") from exc
    if not isinstance(raw, dict):
        raise Error("invalid refs/jj-stack/operation payload: expected object")
    kind = raw.get("operation_kind")
    if kind == "first-publication":
        return parse_first_publication(data)
    if kind == "topology-repair":
        return parse_topology_repair(data)
    if kind == "membership-repair":
        return parse_membership_repair(data)
    raise Error("invalid refs/jj-stack/operation payload: unknown operation_kind")


def read_operation(workspace: str | Path) -> tuple[str, Operation]:
    oid = read_ref_oid(workspace, OPERATION_REF)
    if oid is None:
        raise Error("there is no active operation")
    payload = _run(
        ["git", f"--git-dir={git_common_dir(workspace)}", "cat-file", "blob", oid]
    )
    return oid, parse_operation(payload)


def read_first_publication(
    workspace: str | Path,
) -> tuple[str, FirstPublication]:
    oid = read_ref_oid(workspace, OPERATION_REF)
    if oid is None:
        raise Error("there is no active first-publication operation")
    common = git_common_dir(workspace)
    payload = _run(["git", f"--git-dir={common}", "cat-file", "blob", oid])
    return oid, parse_first_publication(payload)


def cas_write_first_publication(
    workspace: str | Path,
    expected_oid: str | None,
    operation: FirstPublication,
) -> str:
    common = git_common_dir(workspace)
    new_oid = _run(
        ["git", f"--git-dir={common}", "hash-object", "-w", "--stdin"],
        stdin=first_publication_to_json(operation),
    ).strip()
    expected = expected_oid or ("0" * len(new_oid))
    result = subprocess.run(
        [
            "git",
            f"--git-dir={common}",
            "update-ref",
            OPERATION_REF,
            new_oid,
            expected,
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        if read_ref_oid(workspace, OPERATION_REF) != expected_oid:
            raise ConcurrentUpdate("operation ref changed during compare-and-swap")
        detail = result.stderr.strip() or result.stdout.strip()
        raise Error(
            "could not update operation ref" + (f": {detail}" if detail else "")
        )
    return new_oid


def cas_write_topology_repair(
    workspace: str | Path,
    expected_oid: str | None,
    operation: TopologyRepair,
) -> str:
    common = git_common_dir(workspace)
    new_oid = _run(
        ["git", f"--git-dir={common}", "hash-object", "-w", "--stdin"],
        stdin=topology_repair_to_json(operation),
    ).strip()
    expected = expected_oid or ("0" * len(new_oid))
    result = subprocess.run(
        ["git", f"--git-dir={common}", "update-ref", OPERATION_REF, new_oid, expected],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        if read_ref_oid(workspace, OPERATION_REF) != expected_oid:
            raise ConcurrentUpdate("operation ref changed during compare-and-swap")
        detail = result.stderr.strip() or result.stdout.strip()
        raise Error(
            "could not update operation ref" + (f": {detail}" if detail else "")
        )
    return new_oid


def cas_write_membership_repair(
    workspace: str | Path,
    expected_oid: str | None,
    operation: MembershipRepair,
) -> str:
    common = git_common_dir(workspace)
    new_oid = _run(
        ["git", f"--git-dir={common}", "hash-object", "-w", "--stdin"],
        stdin=membership_repair_to_json(operation),
    ).strip()
    expected = expected_oid or ("0" * len(new_oid))
    result = subprocess.run(
        ["git", f"--git-dir={common}", "update-ref", OPERATION_REF, new_oid, expected],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        if read_ref_oid(workspace, OPERATION_REF) != expected_oid:
            raise ConcurrentUpdate("operation ref changed during compare-and-swap")
        detail = result.stderr.strip() or result.stdout.strip()
        raise Error("could not update operation ref" + (f": {detail}" if detail else ""))
    return new_oid


def cas_delete_operation(workspace: str | Path, expected_oid: str) -> None:
    common = git_common_dir(workspace)
    result = subprocess.run(
        [
            "git",
            f"--git-dir={common}",
            "update-ref",
            "-d",
            OPERATION_REF,
            expected_oid,
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        if read_ref_oid(workspace, OPERATION_REF) != expected_oid:
            raise ConcurrentUpdate(
                "operation ref changed during compare-and-swap delete"
            )
        raise Error("could not delete completed operation fence")


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


def _valid_publication_branch(name: str) -> bool:
    """Pure equivalent of git-check-ref-format rules for refs/heads/<name>."""
    if (
        not name
        or name == "@"
        or name.startswith("/")
        or name.endswith(("/", "."))
        or "//" in name
        or ".." in name
        or "@{" in name
        or any(ord(character) < 32 or ord(character) == 127 for character in name)
        or any(character in " ~^:?*[\\" for character in name)
    ):
        return False
    return all(
        component and not component.startswith(".") and not component.endswith(".lock")
        for component in name.split("/")
    )


def resolve_publication_assignments(
    observed: PublicationAssignmentInput,
) -> tuple[PublicationAssignment, ...] | Blocked:
    """Choose exact names without mutating jj, Git, GitHub, or durable state."""
    commits = observed.ordered_commit_ids
    if not commits or len(set(commits)) != len(commits):
        return _block(
            "invalid-publication-selection",
            "selection",
            "selected commits must be nonempty and unique",
        )
    local_commits = {commit.commit_id: commit for commit in observed.local.commits}
    if len(local_commits) != len(observed.local.commits) or any(
        commit_id not in local_commits or local_commits[commit_id].has_conflicts
        for commit_id in commits
    ):
        return _block(
            "commit-unavailable",
            "selection",
            "every selected commit must be observed exactly once and conflict-free",
        )

    if observed.explicit is not None:
        assignments = observed.explicit
        if tuple(item.commit_id for item in assignments) != commits:
            return _block(
                "incomplete-explicit-assignment",
                "selection",
                "explicit publication assignments must exactly cover the ordered selection",
            )
    else:
        template_by_commit: dict[str, str] = {}
        for item in observed.template_results:
            if item.commit_id not in commits or item.commit_id in template_by_commit:
                return _block(
                    "invalid-template-result",
                    item.commit_id,
                    "template results must uniquely identify selected commits",
                )
            template_by_commit[item.commit_id] = item.branch_name
        protected = set(observed.protected_branch_names)
        chosen: list[PublicationAssignment] = []
        for commit_id in commits:
            aliases = tuple(
                bookmark.name
                for bookmark in observed.local.local_bookmarks
                if bookmark.name not in protected
                and isinstance(bookmark.target, CommitTarget)
                and bookmark.target.commit_id == commit_id
            )
            if len(aliases) > 1:
                return _block(
                    "ambiguous-local-bookmark",
                    commit_id,
                    "selected revision has multiple eligible exact-target bookmarks",
                )
            if aliases:
                branch_name = aliases[0]
            else:
                branch_name = template_by_commit.get(commit_id, "")
                if not branch_name:
                    return _block(
                        "template-name-unavailable",
                        commit_id,
                        "no eligible bookmark or evaluated push-template name is available",
                    )
            chosen.append(PublicationAssignment(commit_id, branch_name))
        assignments = tuple(chosen)

    names = tuple(item.branch_name for item in assignments)
    if len(set(names)) != len(names):
        return _block(
            "duplicate-publication-branch",
            "selection",
            "publication branch names must be unique",
        )
    protected = set(observed.protected_branch_names)
    for item in assignments:
        name = item.branch_name
        if not _valid_publication_branch(name):
            return _block("invalid-publication-branch", name, "branch name is invalid")
        if name in protected or name.startswith("jj-stack/"):
            return _block(
                "protected-publication-branch",
                name,
                "target, managed, or protected branch names cannot be published",
            )
        same_name = tuple(
            bookmark
            for bookmark in observed.local.local_bookmarks
            if bookmark.name == name
        )
        if len(same_name) > 1 or any(
            not isinstance(bookmark.target, CommitTarget)
            or bookmark.target.commit_id != item.commit_id
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
                "destination observation belongs to another repository",
            )
        name = destination.ref.full_name.removeprefix("refs/heads/")
        if name in destinations:
            return _block(
                "ambiguous-destination",
                name,
                "destination branch was observed more than once",
            )
        destinations[name] = destination
    for name in names:
        destination = destinations.get(name)
        if destination is None:
            return _block(
                "destination-unavailable",
                name,
                "authoritative destination absence was not observed",
            )
        if destination.commit_id is not None:
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
                "historical pull request observation belongs to another repository",
            )
        for name in names:
            same_repository_head = (
                pull_request.head_repository == observed.repository
                and pull_request.head_branch == name
            )
            if same_repository_head or pull_request.base_branch == name:
                return _block(
                    "pull-request-branch-collision",
                    name,
                    "branch name has historical same-repository pull request use",
                )
    return assignments


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


def prepare_standalone_first_publication(
    workspace: str | Path,
    local: LocalObservation,
    tool_state: ToolStateRead,
    repository: GitHubRepository,
    push_url: str,
    remote: str,
    base_branch: str,
    assignment: PublicationAssignment,
    *,
    slot_id: str | None = None,
) -> FirstPublication | Blocked:
    """Freeze one resolved unpublished revision and its exact bookmark setup."""
    if tool_state.operation_blob_oid is not None:
        return _block("operation-fenced", OPERATION_REF, "another operation is active")
    commit_id = assignment.commit_id
    commits = tuple(commit for commit in local.commits if commit.commit_id == commit_id)
    if len(commits) != 1 or commits[0].has_conflicts:
        return _block(
            "commit-unavailable",
            commit_id,
            "revision is absent, duplicated, or conflicted",
        )
    metadata = _metadata(commits[0].description)
    if metadata is None:
        return _block("title-missing", commit_id, "revision has no description title")
    base_name = f"refs/heads/{base_branch}"
    base_observation = observe_live_refs(
        push_url, repository.identity, (base_name,), cwd=workspace
    )[0]
    if (
        base_observation.commit_id is None
        or not _comparison_is_nonempty_linear_and_conflict_free(
            local.commits, base_observation.commit_id, commit_id
        )
    ):
        return _block(
            "invalid-comparison",
            commit_id,
            "base-to-revision segment is empty, nonlinear, incomplete, or conflicted",
        )
    nonce = slot_id or secrets.token_hex(16)
    branch = assignment.branch_name
    local_matches = tuple(item for item in local.local_bookmarks if item.name == branch)
    if not local_matches:
        bookmark_setup = BookmarkSetup.CREATE
    elif (
        len(local_matches) == 1
        and isinstance(local_matches[0].target, CommitTarget)
        and local_matches[0].target.commit_id == commit_id
    ):
        bookmark_setup = BookmarkSetup.KEEP
    else:
        return _block(
            "bookmark-setup-changed",
            branch,
            "resolved publication bookmark is no longer absent or exact",
        )
    try:
        observed_push_url = resolve_push_url(local, remote)
    except Error as exc:
        return _block("push-remote-unavailable", remote, str(exc))
    if observed_push_url != push_url:
        return _block("push-url-changed", remote, "publication remote changed")
    head = observe_live_refs(
        push_url,
        repository.identity,
        (f"refs/heads/{branch}",),
        cwd=workspace,
    )[0]
    if head.commit_id is not None:
        return _block("branch-collision", branch, "generated branch exists remotely")
    title, body = metadata
    return FirstPublication(
        repository.identity,
        repository.name_with_owner,
        push_url,
        remote,
        tuple(item for item in local.effective_config if item[0] != "git.push"),
        local.workspace_targets,
        base_branch,
        tool_state.state_blob_oid,
        FirstPublicationTarget.STANDALONE,
        (),
        None,
        (
            NewPRSlot(
                nonce,
                branch,
                commit_id,
                bookmark_setup,
                base_branch,
                title,
                body,
            ),
        ),
    )


def prepare_multi_first_publication(
    workspace: str | Path,
    local: LocalObservation,
    tool_state: ToolStateRead,
    repository: GitHubRepository,
    push_url: str,
    remote: str,
    base_branch: str,
    assignments: Sequence[PublicationAssignment],
    *,
    slot_ids: Sequence[str] | None = None,
    existing_stack: GitHubStack | None = None,
    existing_pull_requests: Sequence[GitHubPullRequest] = (),
) -> FirstPublication | Blocked:
    """Freeze a fresh stack or append while reusing per-slot publication recovery."""
    assignments = tuple(assignments)
    if not assignments:
        return _block("empty-publication", "stack", "at least one revision is required")
    if existing_stack is None and len(assignments) < 2:
        return _block(
            "standalone-required", "stack", "one revision uses standalone publication"
        )
    if tool_state.operation_blob_oid is not None:
        return _block("operation-fenced", OPERATION_REF, "another operation is active")
    if len({item.commit_id for item in assignments}) != len(assignments) or len(
        {item.branch_name for item in assignments}
    ) != len(assignments):
        return _block(
            "invalid-publication-assignment",
            "stack",
            "publication assignments must have unique commits and branches",
        )
    try:
        observed_push_url = resolve_push_url(local, remote)
    except Error as exc:
        return _block("push-remote-unavailable", remote, str(exc))
    if observed_push_url != push_url:
        return _block("push-url-changed", remote, "publication remote changed")

    existing_prs = tuple(existing_pull_requests)
    if existing_stack is not None:
        if (
            existing_stack.identity.repository != repository.identity
            or existing_stack.base_branch != base_branch
            or not existing_prs
            or tuple(pr.identity for pr in existing_prs)
            != existing_stack.pull_requests
            or any(pr.state is not PullRequestState.OPEN for pr in existing_prs)
            or any(pr.auto_merge_enabled or pr.in_merge_queue for pr in existing_prs)
        ):
            return _block(
                "append-unsupported",
                "stack",
                "append requires one completely open repository-local stack",
            )
        tracked = tuple(
            stack
            for stack in tool_state.state.stacks
            if stack.repository == repository.identity
            and stack.base_branch == base_branch
            and stack.ordered_prs == tuple(pr.identity for pr in existing_prs)
        )
        if len(tracked) != 1:
            return _block(
                "untracked-membership",
                "stack",
                "append requires exact tracked membership",
            )

    nonces = (
        tuple(secrets.token_hex(16) for _assignment in assignments)
        if slot_ids is None
        else tuple(slot_ids)
    )
    if len(nonces) != len(assignments) or len(set(nonces)) != len(nonces):
        return _block("slot-collision", "stack", "slot IDs must be complete and unique")
    by_commit = {commit.commit_id: commit for commit in local.commits}
    names = tuple(item.branch_name for item in assignments)
    ref_names = tuple(
        dict.fromkeys(
            (
                f"refs/heads/{base_branch}",
                *(f"refs/heads/{pr.head_branch}" for pr in existing_prs),
                *(f"refs/heads/{name}" for name in names),
            )
        )
    )
    live = observe_live_refs(push_url, repository.identity, ref_names, cwd=workspace)
    by_name = {item.ref.full_name: item.commit_id for item in live}
    previous_branch = existing_prs[-1].head_branch if existing_prs else base_branch
    if existing_prs:
        if any(
            by_name.get(f"refs/heads/{pr.head_branch}") != pr.head_oid
            for pr in existing_prs
        ):
            return _block("stack-moved", "stack", "an existing append head moved")
        previous_commit = existing_prs[-1].head_oid
    else:
        previous_commit = by_name.get(f"refs/heads/{base_branch}")
    if previous_commit is None:
        return _block("base-unavailable", base_branch, "publication base is absent")

    slots: list[NewPRSlot] = []
    for nonce, assignment in zip(nonces, assignments, strict=True):
        commit = by_commit.get(assignment.commit_id)
        if commit is None or commit.has_conflicts:
            return _block(
                "commit-unavailable", assignment.commit_id, "revision is absent or conflicted"
            )
        metadata = _metadata(commit.description)
        if metadata is None:
            return _block("title-missing", assignment.commit_id, "revision has no title")
        if by_name.get(f"refs/heads/{assignment.branch_name}") is not None:
            return _block(
                "branch-collision", assignment.branch_name, "destination branch exists"
            )
        local_matches = tuple(
            item for item in local.local_bookmarks if item.name == assignment.branch_name
        )
        if not local_matches:
            setup = BookmarkSetup.CREATE
        elif (
            len(local_matches) == 1
            and isinstance(local_matches[0].target, CommitTarget)
            and local_matches[0].target.commit_id == assignment.commit_id
        ):
            setup = BookmarkSetup.KEEP
        else:
            return _block(
                "bookmark-setup-changed",
                assignment.branch_name,
                "resolved publication bookmark is no longer absent or exact",
            )
        if not _comparison_is_nonempty_linear_and_conflict_free(
            local.commits, previous_commit, assignment.commit_id
        ):
            return _block(
                "invalid-comparison",
                assignment.commit_id,
                "each new predecessor-to-head segment must be complete and linear",
            )
        title, body = metadata
        slots.append(
            NewPRSlot(
                nonce,
                assignment.branch_name,
                assignment.commit_id,
                setup,
                previous_branch,
                title,
                body,
            )
        )
        previous_branch = assignment.branch_name
        previous_commit = assignment.commit_id

    target = (
        FirstPublicationTarget.APPEND
        if existing_stack is not None
        else FirstPublicationTarget.FRESH_STACK
    )
    return FirstPublication(
        repository.identity,
        repository.name_with_owner,
        push_url,
        remote,
        tuple(item for item in local.effective_config if item[0] != "git.push"),
        local.workspace_targets,
        base_branch,
        tool_state.state_blob_oid,
        target,
        tuple(
            ExistingFirstPublicationPR(
                pr.identity,
                pr.head_branch,
                pr.head_oid,
                pr.base_branch,
                pr.base_oid,
                pr.draft,
                pr.title,
                pr.body,
            )
            for pr in existing_prs
        ),
        existing_stack.identity if existing_stack is not None else None,
        tuple(slots),
        StackLinkPhase.NOT_ATTEMPTED,
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
        snapshot.tool_state.operation_blob_oid,
        prs,
        snapshot.membership,
        heads,
        bases,
    )


def _topology_dependencies(
    snapshot: Snapshot,
    prs: tuple[GitHubPullRequest, ...],
    source: PullRequestMembership,
    heads: tuple[LiveRemoteRef, ...],
    bases: tuple[LiveRemoteRef, ...],
) -> TopologyDependencies:
    return TopologyDependencies(
        tuple(
            item for item in snapshot.local.effective_config if item[0] != "git.push"
        ),
        snapshot.push_url,
        snapshot.tool_state.state_blob_oid,
        snapshot.tool_state.operation_blob_oid,
        prs,
        source,
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
    if snapshot.tool_state.operation_blob_oid is not None:
        return _block(
            "operation-fenced",
            OPERATION_REF,
            "a pending operation fences ordinary planning",
        )
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
    return _plan_head_update(
        snapshot.local.commits, snapshot.tool_state.state, wanted, head
    )


def _plan_head_update(
    commits: tuple[ObservedCommit, ...],
    state: TrackedState,
    wanted: DesiredExistingPR,
    head: LiveRemoteRef,
) -> tuple[PlannedHeadUpdate, ...] | Blocked:
    assert head.commit_id is not None
    if wanted.desired_commit_id == head.commit_id:
        return ()
    if _is_ancestor(commits, head.commit_id, wanted.desired_commit_id):
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
        for item in state.last_published_heads
        if item.pr == wanted.pr_identity
        and item.ref == head.ref
        and item.verified_commit_id == head.commit_id
    )
    if len(publications) != 1:
        adoptions = tuple(
            item
            for item in state.last_adopted_heads
            if item.pr == wanted.pr_identity
            and item.ref == head.ref
            and item.verified_commit_id == head.commit_id
        )
        if len(adoptions) != 1:
            return _block(
                "replacement-unauthorized",
                head.ref.full_name,
                "live head is neither an ancestor nor one unambiguous verified authority",
            )
        authority: Authority = MatchesLastAdoption(adoptions[0])
    else:
        authority = MatchesLastPublication(publications[0])
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


def _tracking_update(snapshot: Snapshot) -> TrackedStack | Blocked | None:
    ordered_prs = (
        (snapshot.membership.pr,)
        if isinstance(snapshot.membership, StandalonePullRequest)
        else snapshot.membership.ordered_prs
    )
    base_branch = (
        snapshot.pull_requests[0].base_branch
        if isinstance(snapshot.membership, StandalonePullRequest)
        else snapshot.membership.base_branch
    )
    exact = tuple(
        stack
        for stack in snapshot.tool_state.state.stacks
        if stack.repository == snapshot.repository
        and stack.base_branch == base_branch
        and stack.ordered_prs == ordered_prs
    )
    if len(exact) == 1:
        return None
    candidate = TrackedStack(snapshot.repository, base_branch, ordered_prs)
    members = set(ordered_prs)
    if any(
        stack.repository == snapshot.repository
        and members.intersection((*stack.ordered_prs, *stack.detached_prs))
        for stack in snapshot.tool_state.state.stacks
    ):
        return _block(
            "tracked-topology-changed",
            "stack",
            "observed membership overlaps a different tracked stack",
        )
    return candidate


def _topology_naming_digest(snapshot: Snapshot, desired: DesiredStack) -> str:
    """Hash only the minimum immutable topology naming preimage."""
    return _topology_naming_digest_parts(
        snapshot.repository,
        tuple(pr.identity for pr in snapshot.pull_requests),
        desired,
    )


def _topology_naming_digest_parts(
    repository: GitHubRepositoryId,
    source_prs: tuple[PullRequestId, ...],
    desired: DesiredStack,
) -> str:
    semantic = [
        [repository.host, repository.node_id],
        [pr.number for pr in source_prs],
        desired.base_branch,
        [
            [item.pr_identity.number, item.desired_commit_id]
            for item in desired.active
        ],
    ]
    canonical = json.dumps(semantic, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:24]


def _topology_temporary_base_refs(
    repository: GitHubRepositoryId,
    desired: DesiredStack,
    naming_digest: str,
) -> tuple[RemoteBranchRef, ...]:
    return tuple(
        RemoteBranchRef(
            repository,
            f"refs/heads/jj-stack/temporary-bases/{naming_digest}/{ordinal}",
        )
        for ordinal, _item in enumerate(desired.active, 1)
    )


def prepare_topology_planning(
    snapshot: Snapshot,
    repository: GitHubRepository,
    remote: str,
    desired: DesiredStack | Blocked,
) -> TopologyPlanningSource | Blocked:
    """Freeze intent and names before authoritative temporary-base reads."""
    if isinstance(desired, Blocked):
        return desired
    if desired.repository != snapshot.repository:
        return _block(
            "repository-mismatch",
            desired.repository.node_id,
            "desired repository differs",
        )
    if repository.identity != snapshot.repository or not remote:
        return _block(
            "routing-mismatch",
            "repository",
            "topology routing does not match the observed repository",
        )
    identities = tuple(item.pr_identity for item in desired.active)
    if len(set(identities)) != len(identities):
        return _block(
            "duplicate-desired", "stack", "desired membership contains duplicates"
        )
    observed = {pr.identity for pr in snapshot.pull_requests}
    if any(
        identity.repository != snapshot.repository or identity not in observed
        for identity in identities
    ):
        return _block(
            "foreign-desired", "stack", "desired membership is not a source subset"
        )
    naming_digest = _topology_naming_digest(snapshot, desired)
    source_order = tuple(pr.identity for pr in snapshot.pull_requests)
    refs = ()
    if identities != source_order:
        refs = _topology_temporary_base_refs(
            snapshot.repository,
            desired,
            naming_digest,
        )
    return TopologyPlanningSource(
        snapshot, repository, remote, desired, naming_digest, refs
    )


def complete_topology_planning_input(
    source: TopologyPlanningSource,
    temporary_base_observations: Sequence[LiveRemoteRef],
) -> TopologyPlanningInput | Blocked:
    """Accept authoritative observations of every frozen temporary-base ref."""
    observations = tuple(temporary_base_observations)
    if tuple(item.ref for item in observations) != source.temporary_base_refs:
        return _block(
            "temporary-base-observation-missing",
            "stack",
            "every exact frozen temporary-base ref must be authoritatively observed once",
        )
    if any(item.commit_id is not None for item in observations):
        occupied = next(item for item in observations if item.commit_id is not None)
        return _block(
            "temporary-base-occupied",
            occupied.ref.full_name,
            "temporary base branch already exists",
        )
    return TopologyPlanningInput(
        source.snapshot,
        source.repository,
        source.remote,
        source.desired,
        source.naming_digest,
        observations,
    )


def plan_topology(value: TopologyPlanningInput | Blocked) -> TopologyPlanResult:
    """Purely plan a fully-open membership transition; perform no observation."""
    if isinstance(value, Blocked):
        return value
    snapshot, desired = value.snapshot, value.desired
    if snapshot.tool_state.operation_blob_oid is not None:
        return _block(
            "operation-fenced", OPERATION_REF, "a pending operation fences planning"
        )
    if any(pr.state is not PullRequestState.OPEN for pr in snapshot.pull_requests):
        return _block(
            "not-fully-open", "stack", "topology planning requires a fully open source"
        )
    source_ids = tuple(pr.identity for pr in snapshot.pull_requests)
    membership_ids = (
        (snapshot.membership.pr,)
        if isinstance(snapshot.membership, StandalonePullRequest)
        else snapshot.membership.ordered_prs
    )
    if source_ids != membership_ids or len(set(source_ids)) != len(source_ids):
        return _block(
            "incomplete-membership",
            "stack",
            "source observations must exactly match membership",
        )
    tracked = tuple(
        stack
        for stack in snapshot.tool_state.state.stacks
        if stack.repository == snapshot.repository
        and stack.base_branch == desired.base_branch
        and stack.ordered_prs == source_ids
    )
    if len(tracked) != 1:
        return _block(
            "tracked-source-mismatch",
            "stack",
            "source must have one exact tracked record",
        )
    desired_ids = tuple(item.pr_identity for item in desired.active)
    if len(set(desired_ids)) != len(desired_ids) or not set(desired_ids).issubset(
        source_ids
    ):
        return _block(
            "invalid-desired-membership",
            "stack",
            "desired identities must be a unique source subset",
        )
    if desired.base_branch != tracked[0].base_branch:
        return _block(
            "target-mismatch",
            desired.base_branch,
            "desired target differs from tracked source",
        )

    refs = {item.ref: item for item in snapshot.live_refs}
    base_ref = RemoteBranchRef(snapshot.repository, f"refs/heads/{desired.base_branch}")
    base = refs.get(base_ref)
    if base is None or base.commit_id is None:
        return _block(
            "base-disagrees",
            base_ref.full_name,
            "target branch was not authoritatively observed",
        )
    previous_branch = desired.base_branch
    for pr in snapshot.pull_requests:
        head = refs.get(
            RemoteBranchRef(snapshot.repository, f"refs/heads/{pr.head_branch}")
        )
        literal = refs.get(
            RemoteBranchRef(snapshot.repository, f"refs/heads/{pr.base_branch}")
        )
        if (
            head is None
            or head.commit_id != pr.head_oid
            or literal is None
            or literal.commit_id != pr.base_oid
            or pr.base_branch != previous_branch
            or not _comparison_is_nonempty_linear_and_conflict_free(
                snapshot.local.commits,
                pr.base_oid,
                pr.head_oid,
            )
        ):
            return _block(
                "invalid-old-comparison",
                f"PR #{pr.number}",
                "old comparison is not exact, linear, and nonempty",
            )
        previous_branch = pr.head_branch

    desired_by_id = {item.pr_identity: item for item in desired.active}
    predecessor = base.commit_id
    desired_bases: dict[PullRequestId, str] = {}
    head_updates: list[PlannedHeadUpdate] = []
    for identity in desired_ids:
        wanted = desired_by_id[identity]
        if not _comparison_is_nonempty_linear_and_conflict_free(
            snapshot.local.commits, predecessor, wanted.desired_commit_id
        ):
            return _block(
                "invalid-desired-comparison",
                f"PR #{identity.number}",
                "desired comparison is not linear and nonempty",
            )
        desired_bases[identity] = predecessor
        pr = next(pr for pr in snapshot.pull_requests if pr.identity == identity)
        head = refs[
            RemoteBranchRef(snapshot.repository, f"refs/heads/{pr.head_branch}")
        ]
        updates = _plan_publication(snapshot, wanted, head)
        if isinstance(updates, Blocked):
            return updates
        head_updates.extend(updates)
        predecessor = wanted.desired_commit_id

    expected_temporary_base_refs = _topology_temporary_base_refs(
        snapshot.repository,
        desired,
        value.naming_digest,
    )
    if desired_ids == source_ids:
        expected_temporary_base_refs = ()
    if tuple(
        item.ref for item in value.temporary_base_observations
    ) != expected_temporary_base_refs or any(
        item.commit_id is not None for item in value.temporary_base_observations
    ):
        return _block(
            "temporary-base-observation-invalid",
            "stack",
            "temporary-base absence observations are incomplete or occupied",
        )
    temporary_bases: list[PlannedTemporaryBase] = []
    for identity, absence in zip(
        desired_ids, value.temporary_base_observations, strict=True
    ):
        pr = next(pr for pr in snapshot.pull_requests if pr.identity == identity)
        wanted = desired_by_id[identity]
        if not _comparison_is_nonempty_linear_and_conflict_free(
            snapshot.local.commits,
            pr.base_oid,
            pr.head_oid,
        ) or not _comparison_is_nonempty_linear_and_conflict_free(
            snapshot.local.commits,
            desired_bases[identity],
            wanted.desired_commit_id,
        ):
            return _block(
                "unsafe-temporary-base",
                f"PR #{identity.number}",
                "temporary base is unsafe under old or desired comparison maps",
            )
        temporary_bases.append(
            PlannedTemporaryBase(
                identity,
                absence.ref,
                pr.base_oid,
                desired_bases[identity],
            )
        )

    newly_detached = tuple(
        identity for identity in source_ids if identity not in set(desired_ids)
    )
    detached = tuple(dict.fromkeys((*tracked[0].detached_prs, *newly_detached)))
    final = (
        TrackedStack(snapshot.repository, desired.base_branch, desired_ids, detached)
        if desired_ids or detached
        else None
    )
    dependencies = _topology_dependencies(
        snapshot,
        snapshot.pull_requests,
        snapshot.membership,
        tuple(
            refs[
                RemoteBranchRef(snapshot.repository, f"refs/heads/{pr.head_branch}")
            ]
            for pr in snapshot.pull_requests
        ),
        tuple(
            dict.fromkeys(
                (
                    base,
                    *(
                        refs[
                            RemoteBranchRef(
                                snapshot.repository, f"refs/heads/{pr.base_branch}"
                            )
                        ]
                        for pr in snapshot.pull_requests
                    ),
                )
            )
        ),
    )
    return TopologyPlan(
        value.repository.identity,
        value.repository.name_with_owner,
        value.remote,
        desired,
        newly_detached,
        tuple(head_updates),
        tuple(temporary_bases),
        final,
        dependencies,
    )


def prepare_tracked_restore_planning(
    observation: TrackedRestoreObservation,
) -> TrackedRestorePlanningSource | Blocked:
    tracked = observation.tracked
    if (
        tracked.repository != observation.repository.identity
        or observation.desired.repository != tracked.repository
        or observation.desired.base_branch != tracked.base_branch
        or tuple(item.pr_identity for item in observation.desired.active)
        != tracked.ordered_prs
    ):
        return _block(
            "restore-intent-mismatch",
            "stack",
            "restoration must preserve exact tracked base, membership, and order",
        )
    digest = _topology_naming_digest_parts(
        tracked.repository, tracked.ordered_prs, observation.desired
    )
    return TrackedRestorePlanningSource(
        observation,
        digest,
        _topology_temporary_base_refs(tracked.repository, observation.desired, digest),
    )


def complete_tracked_restore_planning_input(
    source: TrackedRestorePlanningSource,
    temporary_base_observations: Sequence[LiveRemoteRef],
) -> TrackedRestorePlanningInput | Blocked:
    observations = tuple(temporary_base_observations)
    if tuple(item.ref for item in observations) != source.temporary_base_refs:
        return _block(
            "temporary-base-observation-missing",
            "stack",
            "every frozen temporary-base ref must be observed once",
        )
    occupied = next((item for item in observations if item.commit_id is not None), None)
    if occupied is not None:
        return _block(
            "temporary-base-occupied",
            occupied.ref.full_name,
            "temporary base branch already exists",
        )
    return TrackedRestorePlanningInput(
        source.observation, source.naming_digest, observations
    )


def plan_tracked_restore(
    value: TrackedRestorePlanningInput | Blocked,
) -> TopologyPlanResult:
    """Purely plan restoration of one fully open tracked stack."""
    if isinstance(value, Blocked):
        return value
    observed = value.observation
    tracked = observed.tracked
    desired = observed.desired
    if observed.tool_state.operation_blob_oid is not None:
        return _block("operation-fenced", OPERATION_REF, "a pending operation fences planning")
    if tuple(stack for stack in observed.tool_state.state.stacks if stack == tracked) != (
        tracked,
    ):
        return _block("tracked-source-mismatch", "stack", "exact tracked source is absent")
    desired_ids = tuple(item.pr_identity for item in desired.active)
    if (
        desired.repository != tracked.repository
        or desired.base_branch != tracked.base_branch
        or desired_ids != tracked.ordered_prs
        or len(set(desired_ids)) != len(desired_ids)
    ):
        return _block(
            "restore-intent-mismatch",
            "stack",
            "restoration cannot add, drop, reorder, or retarget tracked PRs",
        )
    sources = observed.pull_requests
    if tuple(pr.identity for pr in sources) != tracked.ordered_prs:
        return _block(
            "incomplete-membership", "stack", "node-ID observations are incomplete"
        )
    if any(
        pr.state is not PullRequestState.OPEN or pr.head_repository != tracked.repository
        for pr in sources
    ):
        return _block("not-fully-open", "stack", "restoration requires open local PRs")
    if any(pr.auto_merge_enabled or pr.in_merge_queue for pr in sources):
        return _block("active-automation", "stack", "automation blocks restoration")

    stack_ids = {pr.stack.identity for pr in sources if pr.stack is not None}
    if len(stack_ids) > 1:
        return _block("ambiguous-membership", "stack", "multiple stacks survive")
    current_membership = None
    if not stack_ids:
        if observed.current_stack is not None:
            return _block("association-mismatch", "stack", "standalone evidence is incomplete")
    else:
        stack_id = next(iter(stack_ids))
        stack = observed.current_stack
        if stack is None or stack.identity != stack_id:
            return _block("association-mismatch", str(stack_id), "surviving stack is incomplete")
        members = stack.pull_requests
        positions = tuple(tracked.ordered_prs.index(identity) for identity in members if identity in tracked.ordered_prs)
        if (
            len(members) != len(positions)
            or tuple(sorted(positions)) != positions
            or any(
                (pr.stack is not None and pr.stack.identity == stack_id)
                != (pr.identity in members)
                for pr in sources
            )
        ):
            return _block("foreign-association", str(stack_id), "surviving stack is not a tracked subset")
        current_membership = ServerStackMembership(
            members[0],
            GitHubStackSummary(stack.identity, stack.node_id, stack.base_branch),
            members,
        )

    refs = {item.ref: item for item in observed.live_refs}
    base_ref = RemoteBranchRef(tracked.repository, f"refs/heads/{tracked.base_branch}")
    target = refs.get(base_ref)
    if target is None or target.commit_id is None:
        return _block("base-disagrees", base_ref.full_name, "tracked target is unavailable")
    if len(value.temporary_base_observations) != len(desired_ids):
        return _block("temporary-base-observation-invalid", "stack", "absence reads are incomplete")
    by_id = {pr.identity: pr for pr in sources}
    wanted_by_id = {item.pr_identity: item for item in desired.active}
    previous = target.commit_id
    heads: list[LiveRemoteRef] = []
    bases: list[LiveRemoteRef] = [target]
    updates: list[PlannedHeadUpdate] = []
    temporary: list[PlannedTemporaryBase] = []
    for identity, absence in zip(desired_ids, value.temporary_base_observations, strict=True):
        pr = by_id[identity]
        wanted = wanted_by_id[identity]
        head = refs.get(RemoteBranchRef(tracked.repository, f"refs/heads/{pr.head_branch}"))
        literal = refs.get(RemoteBranchRef(tracked.repository, f"refs/heads/{pr.base_branch}"))
        if head is None or head.commit_id != pr.head_oid:
            return _block("head-disagrees", pr.head_branch, "PR head and live ref disagree")
        if literal is None or literal.commit_id != pr.base_oid:
            return _block("base-disagrees", pr.base_branch, "PR base and live ref disagree")
        if not _comparison_is_nonempty_linear_and_conflict_free(
            observed.local.commits, pr.base_oid, pr.head_oid
        ):
            return _block(
                "invalid-old-comparison",
                f"PR #{identity.number}",
                "current comparison is unsafe",
            )
        if not _comparison_is_nonempty_linear_and_conflict_free(
            observed.local.commits, previous, wanted.desired_commit_id
        ):
            return _block(
                "invalid-desired-comparison",
                f"PR #{identity.number}",
                "desired comparison is unsafe",
            )
        planned = _plan_head_update(
            observed.local.commits, observed.tool_state.state, wanted, head
        )
        if isinstance(planned, Blocked):
            return planned
        updates.extend(planned)
        heads.append(head)
        if literal not in bases:
            bases.append(literal)
        temporary.append(
            PlannedTemporaryBase(identity, absence.ref, pr.base_oid, previous)
        )
        previous = wanted.desired_commit_id
    source = TrackedRestoreSource(tracked, current_membership)
    dependencies = TopologyDependencies(
        tuple(
            item for item in observed.local.effective_config if item[0] != "git.push"
        ),
        observed.push_url,
        observed.tool_state.state_blob_oid,
        observed.tool_state.operation_blob_oid,
        tuple(sources),
        source,
        tuple(heads),
        tuple(bases),
    )
    return TopologyPlan(
        tracked.repository,
        observed.repository.name_with_owner,
        observed.remote,
        desired,
        (),
        tuple(updates),
        tuple(temporary),
        tracked,
        dependencies,
    )


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
    tracking = _tracking_update(snapshot)
    if isinstance(tracking, Blocked):
        return tracking
    dependencies = _dependencies(snapshot, snapshot.pull_requests, all_heads, all_bases)
    if not head_updates and not metadata and tracking is None:
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
        tracking,
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
            f"publish {item.new_commit_id} to {item.ref.full_name}"
            for item in plan.head_updates
        ),
        *(
            f"update metadata for PR #{pr_numbers[item.pr_identity]}"
            for item in plan.metadata_updates
        ),
        *(
            ("record verified stack membership",)
            if plan.tracking_update is not None
            else ()
        ),
    ]
    return "apply:\n" + "\n".join(f"  - {item}" for item in consequences)


def render_topology(plan: TopologyPlanResult) -> str:
    if isinstance(plan, Blocked):
        return "\n".join(
            f"blocked [{item.code}] {item.subject}: {item.detail}"
            for item in plan.reasons
        )
    pr_numbers = {pr.identity: pr.number for pr in plan.dependencies.prs}
    source = plan.source
    if isinstance(source, TrackedRestoreSource):
        source_label = "tracked stack without a surviving server stack"
        if source.current_stack is not None:
            source_label = f"tracked stack with surviving stack #{source.current_stack.server_stack_number}"
    elif isinstance(source, StandalonePullRequest):
        source_label = f"standalone PR #{pr_numbers[source.pr]}"
    else:
        source_label = f"stack #{source.server_stack_number}"
    desired = ", ".join(
        f"#{pr_numbers[item.pr_identity]}" for item in plan.desired.active
    )
    consequences = [
        f"reconcile {source_label} to [{desired}]",
        *(
            f"detach PR #{pr_numbers[item]} (leave open)"
            for item in plan.detached_prs
        ),
        *(
            f"use temporary base {item.ref.full_name} from "
            f"{item.old_base_commit_id} to {item.new_base_commit_id}"
            for item in plan.temporary_bases
        ),
    ]
    return "topology repair:\n" + "\n".join(
        f"  - {item}" for item in consequences
    )


def _apply_shape(plan: SyncPlan) -> Stopped | None:
    if isinstance(plan, Blocked):
        return Stopped("plan", "a blocked plan cannot be applied")
    membership = plan.dependencies.membership
    ordered_members = (
        (membership.pr,)
        if isinstance(membership, StandalonePullRequest)
        else membership.ordered_prs
    )
    if tuple(pr.identity for pr in plan.dependencies.prs) != ordered_members:
        return Stopped("shape", "plan does not contain complete ordered membership")
    active = tuple(
        pr for pr in plan.dependencies.prs if pr.state is PullRequestState.OPEN
    )
    if tuple(pr.identity for pr in active) != tuple(
        wanted.pr_identity for wanted in plan.desired.active
    ):
        return Stopped("shape", "plan does not cover every open pull request")
    if isinstance(plan, Apply):
        expected_refs = {
            RemoteBranchRef(plan.desired.repository, f"refs/heads/{pr.head_branch}")
            for pr in active
        }
        if len({update.ref for update in plan.head_updates}) != len(
            plan.head_updates
        ) or any(update.ref not in expected_refs for update in plan.head_updates):
            return Stopped("shape", "plan changes an unexpected head branch")
        active_ids = {pr.identity for pr in active}
        if len({update.pr_identity for update in plan.metadata_updates}) != len(
            plan.metadata_updates
        ) or any(
            update.pr_identity not in active_ids for update in plan.metadata_updates
        ):
            return Stopped("shape", "plan changes unexpected pull request metadata")
        if plan.tracking_update is not None:
            expected_tracking = TrackedStack(
                plan.desired.repository,
                plan.desired.base_branch,
                ordered_members,
            )
            if plan.tracking_update != expected_tracking:
                return Stopped("shape", "plan records unexpected tracked membership")
    return None


def _apply_revision(plan: NoOp | Apply) -> str:
    desired = plan.desired.active[-1].desired_commit_id
    merged = tuple(
        pr for pr in plan.dependencies.prs if pr.state is PullRequestState.MERGED
    )
    base = merged[-1].head_oid if merged else None
    if base is None:
        base_ref = RemoteBranchRef(
            plan.desired.repository, f"refs/heads/{plan.desired.base_branch}"
        )
        matches = tuple(
            item for item in plan.dependencies.live_bases if item.ref == base_ref
        )
        if len(matches) == 1:
            base = matches[0].commit_id
    if base is None:
        raise Error("planned base ref is absent")
    return f"{base}::{desired}"


def reobserve_for_apply(
    workspace: str | Path, github: GitHubClient, plan: NoOp | Apply
) -> Snapshot:
    """Reobserve only the frozen existing-stack plan's publication dependencies."""
    config_keys = tuple(key for key, _value in plan.dependencies.effective_config)
    selected = (
        plan.dependencies.membership.pr
        if isinstance(plan.dependencies.membership, StandalonePullRequest)
        else plan.dependencies.membership.selected_pr
    )
    selected_prs = tuple(pr for pr in plan.dependencies.prs if pr.identity == selected)
    if len(selected_prs) != 1:
        raise SourceMismatch("selected pull request is not unique in frozen membership")
    return observe_snapshot(
        workspace,
        github,
        revision=_apply_revision(plan),
        config_keys=config_keys,
        remote=plan.dependencies.remote,
        selected_pr_number=selected_prs[0].number,
    )


def _pr_without_metadata(pr: GitHubPullRequest) -> tuple[object, ...]:
    return (
        pr.identity,
        pr.node_id,
        pr.state,
        pr.draft,
        pr.head_repository,
        pr.head_branch,
        pr.head_oid,
        pr.base_branch,
        pr.base_oid,
        pr.auto_merge_enabled,
        pr.in_merge_queue,
        pr.stack,
    )


def _dependency_live_refs(dependencies: Dependencies) -> tuple[LiveRemoteRef, ...]:
    return tuple(dict.fromkeys(dependencies.live_heads + dependencies.live_bases))


def validate_frozen_dependencies(plan: NoOp | Apply, fresh: Snapshot) -> Blocked | None:
    dependencies = plan.dependencies
    if fresh.repository != plan.desired.repository:
        return _block("repository-changed", "repository", "repository identity changed")
    effective_config = tuple(
        item for item in fresh.local.effective_config if item[0] != "git.push"
    )
    if effective_config != dependencies.effective_config:
        return _block("config-changed", "config", "effective configuration changed")
    if fresh.push_url != dependencies.push_url:
        return _block("push-url-changed", "push-url", "push destination changed")
    if fresh.tool_state.state_blob_oid != dependencies.state_blob_oid:
        return _block("state-changed", STATE_REF, "private state changed")
    if fresh.tool_state.operation_blob_oid != dependencies.operation_blob_oid:
        return _block("operation-changed", OPERATION_REF, "operation fence changed")
    if fresh.membership != dependencies.membership:
        return _block("membership-changed", "stack", "pull request membership changed")
    if fresh.live_refs != _dependency_live_refs(dependencies):
        return _block("refs-changed", "destination", "live head or base refs changed")
    if len(fresh.pull_requests) != len(dependencies.prs):
        return _block("pr-changed", "pull-request", "pull request observation changed")
    metadata_planned = (
        {update.pr_identity for update in plan.metadata_updates}
        if isinstance(plan, Apply)
        else set()
    )
    for old_pr, new_pr in zip(dependencies.prs, fresh.pull_requests, strict=True):
        if _pr_without_metadata(new_pr) != _pr_without_metadata(old_pr):
            return _block(
                "pr-changed", f"PR #{old_pr.number}", "pull request state changed"
            )
        if old_pr.identity not in metadata_planned and (
            new_pr.title,
            new_pr.body,
        ) != (old_pr.title, old_pr.body):
            return _block(
                "metadata-changed",
                f"PR #{old_pr.number}",
                "unplanned pull request metadata changed",
            )
    active = _validate_plan_scope(fresh, plan.desired)
    if isinstance(active, Blocked):
        return active
    checked = _validate_pr_dependencies(fresh, plan.desired, active)
    if isinstance(checked, Blocked):
        return checked
    return None


def push_exact_head_updates(
    workspace: str | Path,
    updates: Sequence[PlannedHeadUpdate],
    push_url: str,
) -> subprocess.CompletedProcess[str]:
    if not updates:
        raise ValueError("atomic publication requires at least one head update")
    common = git_common_dir(workspace)
    return subprocess.run(
        [
            "git",
            f"--git-dir={common}",
            "push",
            "--atomic",
            "--no-follow-tags",
            "--recurse-submodules=no",
            *(
                f"--force-with-lease={update.ref.full_name}:{update.expected_old_commit_id}"
                for update in updates
            ),
            push_url,
            *(f"{update.new_commit_id}:{update.ref.full_name}" for update in updates),
        ],
        text=True,
        capture_output=True,
        check=False,
    )


def _slot_pr_matches(
    repository: GitHubRepositoryId,
    slot: NewPRSlot,
    pr: GitHubPullRequest,
    *,
    initial: bool,
    stacked: bool = False,
) -> bool:
    return (
        pr.identity.repository == repository
        and pr.head_repository == repository
        and pr.head_branch == slot.branch
        and pr.head_oid == slot.commit_id
        and pr.base_branch == slot.base_branch
        and pr.title == slot.title
        and pr.body == (slot.initial_body if initial else slot.body)
        and (pr.stack is not None if stacked else pr.stack is None)
        and not pr.auto_merge_enabled
        and not pr.in_merge_queue
        and pr.state is PullRequestState.OPEN
        and pr.draft
    )


@dataclass(frozen=True)
class PublicationSlotReadback:
    slot_id: str
    local_target: BookmarkTarget | None
    remote_target: BookmarkTarget | None
    remote_tracking: TrackingState | None
    live_commit_id: str | None


class PublicationRecoveryAction(StrEnum):
    PUSH = "push"
    TRACK = "track"
    READY = "ready"


def _exact_target(target: BookmarkTarget | None, commit_id: str) -> bool:
    return isinstance(target, CommitTarget) and target.commit_id == commit_id


def _absent_target(target: BookmarkTarget | None) -> bool:
    return target is None or isinstance(target, AbsentBookmarkTarget)


def classify_publication_recovery(
    operation: FirstPublication,
    readbacks: Sequence[PublicationSlotReadback],
) -> tuple[PublicationRecoveryAction, ...] | Stopped:
    """Classify every slot from durable progress plus authoritative readback."""
    if len(readbacks) != len(operation.slots):
        return Stopped("publish", "publication readback is incomplete")
    actions: list[PublicationRecoveryAction] = []
    for slot, readback in zip(operation.slots, readbacks, strict=True):
        if readback.slot_id != slot.slot_id:
            return Stopped("publish", "publication readback identifies another slot")
        if not _exact_target(readback.local_target, slot.commit_id):
            return Stopped(
                "bookmark", f"local bookmark {slot.branch} moved or conflicted"
            )
        live = readback.live_commit_id
        remote = readback.remote_target
        if slot.phase is NewPRPhase.NOT_ATTEMPTED:
            if live is not None:
                return Stopped(
                    "publish",
                    f"destination {slot.branch} existed before the first attempt",
                )
            if not _absent_target(remote):
                return Stopped(
                    "tracking",
                    f"remote bookmark {slot.branch}@{operation.remote} is stale or conflicted",
                )
            actions.append(PublicationRecoveryAction.PUSH)
            continue
        if slot.phase is NewPRPhase.PUBLICATION_POSSIBLY_SENT:
            if live == slot.commit_id:
                if _exact_target(remote, slot.commit_id):
                    actions.append(
                        PublicationRecoveryAction.READY
                        if readback.remote_tracking is TrackingState.TRACKED
                        else PublicationRecoveryAction.TRACK
                    )
                elif _absent_target(remote):
                    actions.append(PublicationRecoveryAction.PUSH)
                else:
                    return Stopped(
                        "tracking",
                        f"remote bookmark {slot.branch}@{operation.remote} is foreign",
                    )
                continue
            if live is None and _absent_target(remote):
                actions.append(PublicationRecoveryAction.PUSH)
                continue
            return Stopped(
                "publish",
                f"destination or remote bookmark for {slot.branch} is foreign",
            )
        if slot.phase is NewPRPhase.READY:
            if (
                live == slot.commit_id
                and _exact_target(remote, slot.commit_id)
                and readback.remote_tracking is TrackingState.TRACKED
            ):
                actions.append(PublicationRecoveryAction.READY)
                continue
            return Stopped(
                "publish",
                f"established publication {slot.branch} drifted",
            )
        return Stopped("publish", f"slot {slot.slot_id} is past publication recovery")
    return tuple(actions)


def _observe_publication_local(
    workspace: str | Path, operation: FirstPublication
) -> LocalObservation:
    local = observe_local(
        workspace,
        revision=" | ".join(slot.commit_id for slot in operation.slots),
        config_keys=tuple(key for key, _value in operation.effective_config),
    )
    if local.effective_config != operation.effective_config:
        raise SourceMismatch("effective configuration changed during publication")
    if local.workspace_targets != operation.workspace_targets:
        raise SourceMismatch("workspace target changed during publication")
    if resolve_push_url(local, operation.remote) != operation.push_url:
        raise SourceMismatch("publication remote push URL changed")
    commits = {commit.commit_id for commit in local.commits}
    if commits != {slot.commit_id for slot in operation.slots}:
        raise SourceMismatch("frozen publication commits are unavailable")
    return local


def _one_local_bookmark(local: LocalObservation, name: str) -> BookmarkTarget | None:
    matches = tuple(item.target for item in local.local_bookmarks if item.name == name)
    if len(matches) > 1:
        raise SourceMismatch(f"local bookmark {name} is ambiguous")
    return matches[0] if matches else None


def _one_remote_bookmark(
    local: LocalObservation, remote: str, name: str
) -> tuple[BookmarkTarget | None, TrackingState | None]:
    matches = tuple(
        item
        for item in local.remote_bookmarks
        if item.remote == remote and item.name == name
    )
    if len(matches) > 1:
        raise SourceMismatch(f"remote bookmark {name}@{remote} is ambiguous")
    if not matches:
        return None, None
    return matches[0].target, matches[0].tracking_state


def observe_publication_slots(
    workspace: str | Path, operation: FirstPublication
) -> tuple[LocalObservation, tuple[PublicationSlotReadback, ...]]:
    local = _observe_publication_local(workspace, operation)
    live = _observe_slot_refs(operation, workspace)
    if len(live) != len(operation.slots):
        raise SourceMismatch("publication destination readback is incomplete")
    readbacks: list[PublicationSlotReadback] = []
    for slot, destination in zip(operation.slots, live, strict=True):
        expected = RemoteBranchRef(operation.repository, f"refs/heads/{slot.branch}")
        if destination.ref != expected:
            raise SourceMismatch("publication destination readback is misordered")
        remote_target, tracking = _one_remote_bookmark(
            local, operation.remote, slot.branch
        )
        readbacks.append(
            PublicationSlotReadback(
                slot.slot_id,
                _one_local_bookmark(local, slot.branch),
                remote_target,
                tracking,
                destination.commit_id,
            )
        )
    return local, tuple(readbacks)


def _run_jj_publication(
    workspace: str | Path, *arguments: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "jj",
            "--color=never",
            "--repository",
            os.fspath(workspace),
            "--ignore-working-copy",
            "--config",
            "git.sign-on-push=false",
            *arguments,
        ],
        text=True,
        capture_output=True,
        check=False,
    )


def create_publication_bookmark(
    workspace: str | Path, slot: NewPRSlot
) -> subprocess.CompletedProcess[str]:
    return _run_jj_publication(
        workspace, "bookmark", "create", slot.branch, "-r", slot.commit_id
    )


def track_publication_bookmark(
    workspace: str | Path, operation: FirstPublication, slot: NewPRSlot
) -> subprocess.CompletedProcess[str]:
    return _run_jj_publication(
        workspace, "bookmark", "track", f"{slot.branch}@{operation.remote}"
    )


def push_new_slot_refs(
    workspace: str | Path,
    operation: FirstPublication,
    slots: Sequence[NewPRSlot],
) -> subprocess.CompletedProcess[str]:
    if not slots:
        raise ValueError("new-ref publication requires at least one pending slot")
    return _run_jj_publication(
        workspace,
        "git",
        "push",
        "--remote",
        operation.remote,
        *(argument for slot in slots for argument in ("--bookmark", slot.branch)),
    )


def _operation_repository(operation: FirstPublication) -> GitHubRepository:
    return GitHubRepository(
        operation.repository,
        operation.repository_name,
        f"https://{operation.repository.host}/{operation.repository_name}",
        operation.base_branch,
    )


def _prevalidate_operation_repository(
    github: GitHubClient, operation: FirstPublication
) -> GitHubRepository:
    observed = github.resolve_repository(
        f"https://{operation.repository.host}/{operation.repository_name}"
    )
    if (
        observed.identity != operation.repository
        or observed.name_with_owner != operation.repository_name
    ):
        raise SourceMismatch("frozen GitHub repository locator changed identity")
    return observed


def run_first_publication_stack_link(
    github: GitHubClient, operation: FirstPublication
) -> GitHubStackSummary:
    if operation.target is FirstPublicationTarget.STANDALONE:
        raise ValueError("standalone publication has no stack-link request")
    repository = _prevalidate_operation_repository(github, operation)
    new_ids = tuple(
        slot.pr_identity for slot in operation.slots if slot.pr_identity is not None
    )
    if len(new_ids) != len(operation.slots):
        raise ValueError("stack request requires every new PR to be bound")
    if operation.target is FirstPublicationTarget.APPEND:
        assert operation.existing_stack is not None
        return github.add_stack_members(
            repository,
            operation.existing_stack,
            pull_requests=new_ids,
        )
    return github.create_stack(repository, pull_requests=new_ids)


def _existing_member_matches(
    member: ExistingFirstPublicationPR, pr: GitHubPullRequest
) -> bool:
    return (
        pr.identity == member.identity
        and pr.state is PullRequestState.OPEN
        and pr.head_branch == member.head_branch
        and pr.head_oid == member.head_commit_id
        and pr.base_branch == member.base_branch
        and pr.base_oid == member.base_commit_id
        and pr.draft == member.draft
        and pr.title == member.title
        and pr.body == member.body
        and not pr.auto_merge_enabled
        and not pr.in_merge_queue
    )


def append_source_is_unchanged(
    github: GitHubClient, operation: FirstPublication
) -> bool:
    if operation.target is not FirstPublicationTarget.APPEND:
        return True
    assert operation.existing_stack is not None
    repository = _prevalidate_operation_repository(github, operation)
    stack = github.stack(repository, operation.existing_stack)
    if stack is None:
        return False
    prs = github.pull_requests(stack.pull_requests)
    return (
        stack.identity == operation.existing_stack
        and stack.base_branch == operation.base_branch
        and len(prs) == len(operation.existing_members)
        and all(
            _existing_member_matches(member, pr)
            for member, pr in zip(
                operation.existing_members, prs, strict=True
            )
        )
    )


def observe_first_publication_stack(
    github: GitHubClient, operation: FirstPublication
) -> GitHubStack | None:
    repository = _prevalidate_operation_repository(github, operation)
    if operation.target is FirstPublicationTarget.APPEND:
        assert operation.existing_stack is not None
        stack = github.stack(repository, operation.existing_stack)
        if stack is None:
            return None
        if stack.identity != operation.existing_stack:
            return None
    else:
        new_ids = tuple(slot.pr_identity for slot in operation.slots)
        if any(identity is None for identity in new_ids):
            raise ValueError("stack readback requires every new PR to be bound")
        prs = github.pull_requests(
            tuple(identity for identity in new_ids if identity is not None)
        )
        if any(pr.stack is None for pr in prs):
            return None
        stack_ids = {pr.stack.identity for pr in prs if pr.stack is not None}
        if len(stack_ids) != 1:
            return None
        stack = github.stack(repository, stack_ids.pop())
        if stack is None:
            return None
    expected_ids = operation.existing_prs + tuple(
        slot.pr_identity for slot in operation.slots if slot.pr_identity is not None
    )
    if (
        stack.base_branch != operation.base_branch
        or stack.pull_requests != expected_ids
    ):
        return None
    prs = github.pull_requests(stack.pull_requests)
    if any(pr.state is not PullRequestState.OPEN for pr in prs):
        return None
    expected_existing = {
        member.identity: member for member in operation.existing_members
    }
    expected_slots = {
        slot.pr_identity: slot
        for slot in operation.slots
        if slot.pr_identity is not None
    }
    for pr in prs:
        member = expected_existing.get(pr.identity)
        if member is not None:
            if not _existing_member_matches(member, pr):
                return None
            continue
        slot = expected_slots.get(pr.identity)
        if slot is None or not _slot_pr_matches(
            operation.repository, slot, pr, initial=False, stacked=True
        ):
            return None
    return stack


def _expected_final_prs(plan: NoOp | Apply) -> tuple[GitHubPullRequest, ...]:
    desired = {item.pr_identity: item for item in plan.desired.active}
    final_refs = {
        item.ref.full_name: item.commit_id
        for item in _dependency_live_refs(plan.dependencies)
    }
    for pr in plan.dependencies.prs:
        wanted = desired.get(pr.identity)
        if wanted is not None:
            final_refs[f"refs/heads/{pr.head_branch}"] = wanted.desired_commit_id
    expected = []
    for pr in plan.dependencies.prs:
        wanted = desired.get(pr.identity)
        expected.append(
            replace(
                pr,
                head_oid=(
                    wanted.desired_commit_id if wanted is not None else pr.head_oid
                ),
                base_oid=final_refs[f"refs/heads/{pr.base_branch}"],
                title=wanted.title if wanted is not None else pr.title,
                body=wanted.body if wanted is not None else pr.body,
            )
        )
    return tuple(expected)


def verify_final_state(
    workspace: str | Path, github: GitHubClient, plan: NoOp | Apply
) -> tuple[Snapshot | None, Stopped | None]:
    try:
        fresh = reobserve_for_apply(workspace, github, plan)
    except Error as exc:
        return None, Stopped("verify", str(exc))
    expected_prs = _expected_final_prs(plan)
    desired_heads = {
        RemoteBranchRef(plan.desired.repository, f"refs/heads/{pr.head_branch}"): (
            pr.head_oid
        )
        for pr in expected_prs
    }
    expected_refs = tuple(
        replace(
            observed,
            commit_id=desired_heads.get(observed.ref, observed.commit_id),
        )
        for observed in _dependency_live_refs(plan.dependencies)
    )
    effective_config = tuple(
        item for item in fresh.local.effective_config if item[0] != "git.push"
    )
    if (
        fresh.repository != plan.desired.repository
        or effective_config != plan.dependencies.effective_config
        or fresh.push_url != plan.dependencies.push_url
        or fresh.tool_state.state_blob_oid != plan.dependencies.state_blob_oid
        or fresh.tool_state.operation_blob_oid != plan.dependencies.operation_blob_oid
        or fresh.membership != plan.dependencies.membership
        or fresh.pull_requests != expected_prs
        or fresh.live_refs != expected_refs
    ):
        return fresh, Stopped(
            "verify", "authoritative final state does not match the frozen plan"
        )
    return fresh, None


def record_verified_state(
    workspace: str | Path,
    expected_state_oid: str | None,
    state: TrackedState,
    tracking_update: TrackedStack | None,
    publications: Sequence[LastPublishedHead],
) -> str:
    publication_keys = {(item.pr, item.ref) for item in publications}
    retained = tuple(
        item
        for item in state.last_published_heads
        if (item.pr, item.ref) not in publication_keys
    )
    stacks = state.stacks + ((tracking_update,) if tracking_update is not None else ())
    return cas_write_state(
        workspace,
        expected_state_oid,
        TrackedState(
            stacks,
            retained + tuple(publications),
            state.last_adopted_heads,
        ),
    )


def _one_bookmark_target(
    local: LocalObservation, remote: str | None, name: str
) -> str | None:
    values = (
        tuple(item.target for item in local.local_bookmarks if item.name == name)
        if remote is None
        else tuple(
            item.target
            for item in local.remote_bookmarks
            if item.remote == remote
            and item.name == name
            and item.tracking_state is TrackingState.TRACKED
        )
    )
    if len(values) != 1 or not isinstance(values[0], CommitTarget):
        return None
    return values[0].commit_id


def _commit_fingerprint(git_dir: str | Path, commit_id: str) -> tuple[str, str]:
    payload = _run(["git", f"--git-dir={git_dir}", "cat-file", "-p", commit_id])
    header, _separator, _message = payload.partition("\n\n")
    change_ids = tuple(
        line.removeprefix("change-id ")
        for line in header.splitlines()
        if line.startswith("change-id ")
    )
    if len(change_ids) != 1 or not change_ids[0]:
        raise SourceMismatch(f"commit {commit_id} has no unique raw jj change ID")
    patch = _run(
        [
            "git",
            f"--git-dir={git_dir}",
            "show",
            "--pretty=format:",
            "--no-ext-diff",
            "--binary",
            commit_id,
        ]
    )
    patch_id_output = _run(["git", "patch-id", "--stable"], stdin=patch).split()
    if len(patch_id_output) != 2 or not re.fullmatch(
        r"[0-9a-f]{40}", patch_id_output[0]
    ):
        raise SourceMismatch(f"commit {commit_id} has no stable patch ID")
    return change_ids[0], patch_id_output[0]


def _linear_segment(
    git_dir: str | Path, base_commit_id: str, head_commit_id: str
) -> tuple[str, ...]:
    commits = tuple(
        _run(
            [
                "git",
                f"--git-dir={git_dir}",
                "rev-list",
                "--reverse",
                "--ancestry-path",
                f"{base_commit_id}..{head_commit_id}",
            ]
        ).splitlines()
    )
    if not commits:
        raise SourceMismatch("restack segment is empty or disconnected")
    predecessor = base_commit_id
    for commit_id in commits:
        fields = _run(
            [
                "git",
                f"--git-dir={git_dir}",
                "rev-list",
                "--parents",
                "-n",
                "1",
                commit_id,
            ]
        ).split()
        if len(fields) != 2 or fields[1] != predecessor:
            raise SourceMismatch("restack segment is not a complete linear chain")
        predecessor = commit_id
    if commits[-1] != head_commit_id:
        raise SourceMismatch("restack segment does not end at the expected head")
    return commits


def _inspection_namespace(operation_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", operation_id):
        raise SourceMismatch("jj operation ID is unsafe for a private inspection ref")
    return f"refs/jj-stack-inspect/{operation_id}"


def _git_refs(git_dir: str | Path, *prefixes: str) -> str:
    return _run(
        [
            "git",
            f"--git-dir={git_dir}",
            "for-each-ref",
            "--format=%(refname)%09%(objectname)",
            *prefixes,
        ]
    )


def _cleanup_inspection_refs(git_dir: str | Path, namespace: str) -> None:
    rows = _git_refs(git_dir, namespace).splitlines()
    for row in rows:
        fields = row.split("\t")
        if len(fields) != 2 or not all(fields):
            raise Error("could not parse private inspection ref during cleanup")
        ref, oid = fields
        result = subprocess.run(
            ["git", f"--git-dir={git_dir}", "update-ref", "-d", ref, oid],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            detail = result.stderr.strip() or result.stdout.strip()
            raise Error(
                f"could not delete private inspection ref {ref}"
                + (f": {detail}" if detail else "")
            )
    if _git_refs(git_dir, namespace):
        raise Error("private inspection refs remain after cleanup")


def _fetch_inspection_refs(
    git_dir: str | Path,
    fetch_url: str,
    namespace: str,
    branch_names: Sequence[str],
) -> None:
    _run(
        [
            "git",
            f"--git-dir={git_dir}",
            "fetch",
            "--no-tags",
            "--no-write-fetch-head",
            fetch_url,
            *(f"+refs/heads/{name}:{namespace}/{name}" for name in branch_names),
        ]
    )


def inspect_remote_restack(
    snapshot: Snapshot, github: GitHubClient, remote: str
) -> RemoteRestackAdoption | Blocked:
    """Match every old/new change ID and patch ID, then freeze adoption."""
    if remote != snapshot.remote:
        return _block("remote-changed", remote, "selected remote differs from observation")
    if not snapshot.fetch_url:
        return _block("fetch-url-missing", remote, "fetch transport was not observed")
    try:
        fetch_repository = github.resolve_repository(snapshot.fetch_url)
    except Error as exc:
        return _block("fetch-repository-unavailable", remote, str(exc))
    if fetch_repository.identity != snapshot.repository:
        return _block(
            "fetch-repository-changed",
            remote,
            "fetch and authoritative push transports identify different repositories",
        )
    tracked_stacks = tuple(
        stack
        for stack in snapshot.tool_state.state.stacks
        if stack.repository == snapshot.repository
        and stack.ordered_prs == tuple(pr.identity for pr in snapshot.pull_requests)
    )
    if len(tracked_stacks) != 1:
        return _block(
            "untracked-membership",
            "stack",
            "adoption requires exact tracked membership",
        )
    active = tuple(
        pr for pr in snapshot.pull_requests if pr.state is PullRequestState.OPEN
    )
    if not active:
        return _block("empty-suffix", "stack", "there is no open suffix to adopt")
    live_by_name = {item.ref.full_name: item.commit_id for item in snapshot.live_refs}
    raw_boundaries: list[tuple[GitHubPullRequest, str, str, str]] = []
    moved = False
    for pr in active:
        local = _one_bookmark_target(snapshot.local, None, pr.head_branch)
        tracked = _one_bookmark_target(snapshot.local, remote, pr.head_branch)
        live = live_by_name.get(f"refs/heads/{pr.head_branch}")
        if local is None or tracked is None or live is None:
            return _block(
                "boundary-unavailable",
                pr.head_branch,
                "L, T, and R must each be unique",
            )
        if local != tracked:
            return _block(
                "local-moved",
                pr.head_branch,
                "local bookmark differs from tracked baseline",
            )
        if live != pr.head_oid:
            return _block(
                "remote-moved",
                pr.head_branch,
                "live ref and pull request head disagree",
            )
        moved |= tracked != live
        raw_boundaries.append((pr, local, tracked, live))
    if not moved:
        return _block("remote-unchanged", "stack", "no remote rewrite needs adoption")

    base_name = tracked_stacks[0].base_branch
    base = live_by_name.get(f"refs/heads/{base_name}")
    if base is None:
        return _block("base-unavailable", base_name, "stack base ref is unavailable")
    git_dir = snapshot.local.git_common_dir
    try:
        namespace = _inspection_namespace(snapshot.local.operation_id)
        if _git_refs(git_dir, namespace):
            raise SourceMismatch("private inspection namespace is already in use")
        ordinary_refs = _git_refs(git_dir, "refs/heads", "refs/remotes")
        names = (base_name, *(pr.head_branch for pr in active))
        expected = {
            base_name: base,
            **{pr.head_branch: live for pr, _local, _tracked, live in raw_boundaries},
        }
        try:
            _fetch_inspection_refs(
                git_dir,
                snapshot.fetch_url,
                namespace,
                names,
            )
            for name, wanted in expected.items():
                fetched = _run(
                    [
                        "git",
                        f"--git-dir={git_dir}",
                        "rev-parse",
                        f"{namespace}/{name}",
                    ]
                ).strip()
                if fetched != wanted:
                    raise SourceMismatch(
                        f"remote branch {name} moved during private inspection"
                    )
            if _git_refs(git_dir, "refs/heads", "refs/remotes") != ordinary_refs:
                raise SourceMismatch("private inspection moved an ordinary Git ref")
            if pin_operation(snapshot.local.workspace) != snapshot.local.operation_id:
                raise SourceMismatch(
                    "jj did not ignore the private inspection ref namespace"
                )
            old_base = base
            new_base = base
            boundaries: list[RemoteRestackBoundary] = []
            for pr, local, tracked, live in raw_boundaries:
                old_segment = _linear_segment(git_dir, old_base, tracked)
                new_segment = _linear_segment(git_dir, new_base, live)
                if len(old_segment) != len(new_segment):
                    raise SourceMismatch(
                        "old and rewritten segments have different lengths"
                    )
                pairs: list[RewriteCommitPair] = []
                for old, new in zip(old_segment, new_segment, strict=True):
                    old_change, old_patch = _commit_fingerprint(git_dir, old)
                    new_change, new_patch = _commit_fingerprint(git_dir, new)
                    if (old_change, old_patch) != (new_change, new_patch):
                        raise SourceMismatch(
                            "remote rewrite changed a jj change ID or stable patch ID"
                        )
                    pairs.append(RewriteCommitPair(old, new, old_change, old_patch))
                boundaries.append(
                    RemoteRestackBoundary(
                        pr.identity,
                        pr.head_branch,
                        local,
                        tracked,
                        live,
                        tuple(pairs),
                    )
                )
                old_base = tracked
                new_base = live
        finally:
            _cleanup_inspection_refs(git_dir, namespace)
    except Error as exc:
        return _block("rewrite-unverified", "stack", str(exc))
    commits = {item.commit_id: item for item in snapshot.local.commits}
    workspace_change_ids: list[tuple[str, str]] = []
    for name, target in snapshot.local.workspace_targets:
        commit = commits.get(target)
        if commit is None or commit.has_conflicts:
            return _block(
                "workspace-unverified", name, "workspace target is absent or conflicted"
            )
        workspace_change_ids.append((name, commit.change_id))
    return RemoteRestackAdoption(
        snapshot.repository,
        remote,
        snapshot.fetch_url,
        operation_config(snapshot.local),
        snapshot.tool_state.state_blob_oid,
        snapshot.tool_state.operation_blob_oid,
        snapshot.membership,
        snapshot.pull_requests,
        snapshot.live_refs,
        tuple(boundaries),
        tuple(workspace_change_ids),
    )


def _adoption_pre_fetch_revision(plan: RemoteRestackAdoption) -> str:
    old = " | ".join(boundary.local_commit_id for boundary in plan.boundaries)
    return f"all() & ({old})::"


def _adoption_post_fetch_revision(plan: RemoteRestackAdoption) -> str:
    old = " | ".join(boundary.local_commit_id for boundary in plan.boundaries)
    new = " | ".join(boundary.remote_commit_id for boundary in plan.boundaries)
    return f"all() & (({old}):: | ({new})::)"


def _reobserve_adoption(
    workspace: str | Path,
    github: GitHubClient,
    plan: RemoteRestackAdoption,
    revision: str,
) -> Snapshot:
    selected = (
        plan.membership.pr
        if isinstance(plan.membership, StandalonePullRequest)
        else plan.membership.selected_pr
    )
    selected_pr = next((pr for pr in plan.prs if pr.identity == selected), None)
    if selected_pr is None:
        raise SourceMismatch("selected PR is absent from frozen membership")
    return observe_snapshot(
        workspace,
        github,
        revision=revision,
        config_keys=tuple(key for key, _value in plan.effective_config),
        remote=plan.remote,
        selected_pr_number=selected_pr.number,
    )


def reobserve_for_adoption(
    workspace: str | Path, github: GitHubClient, plan: RemoteRestackAdoption
) -> Snapshot:
    """Revalidate pre-fetch state without resolving remote-only commit IDs."""
    return _reobserve_adoption(
        workspace, github, plan, _adoption_pre_fetch_revision(plan)
    )


def reobserve_after_adoption(
    workspace: str | Path, github: GitHubClient, plan: RemoteRestackAdoption
) -> Snapshot:
    """Observe old and newly fetched commit closures for final verification."""
    return _reobserve_adoption(
        workspace, github, plan, _adoption_post_fetch_revision(plan)
    )


def _validate_adoption_prestate(
    plan: RemoteRestackAdoption, fresh: Snapshot
) -> str | None:
    if (
        fresh.repository != plan.repository
        or fresh.fetch_url != plan.fetch_url
        or operation_config(fresh.local) != plan.effective_config
        or fresh.tool_state.state_blob_oid != plan.state_blob_oid
        or fresh.tool_state.operation_blob_oid != plan.operation_blob_oid
        or fresh.membership != plan.membership
        or fresh.pull_requests != plan.prs
        or fresh.live_refs != plan.live_refs
    ):
        return "frozen repository, transport, state, membership, PRs, or refs changed"
    for boundary in plan.boundaries:
        if (
            _one_bookmark_target(fresh.local, None, boundary.branch)
            != boundary.local_commit_id
            or _one_bookmark_target(fresh.local, plan.remote, boundary.branch)
            != boundary.tracked_commit_id
        ):
            return f"L/T changed for {boundary.branch}"
    return None


def _validate_adoption_poststate(
    plan: RemoteRestackAdoption, fresh: Snapshot
) -> str | None:
    if (
        fresh.repository != plan.repository
        or fresh.fetch_url != plan.fetch_url
        or operation_config(fresh.local) != plan.effective_config
        or fresh.tool_state.state_blob_oid != plan.state_blob_oid
        or fresh.tool_state.operation_blob_oid != plan.operation_blob_oid
        or fresh.membership != plan.membership
        or fresh.pull_requests != plan.prs
        or fresh.live_refs != plan.live_refs
    ):
        return "remote or frozen local dependencies changed during adoption"
    commits = {commit.commit_id: commit for commit in fresh.local.commits}
    visible_by_change: dict[str, list[ObservedCommit]] = {}
    for commit in fresh.local.commits:
        if not commit.is_hidden:
            visible_by_change.setdefault(commit.change_id, []).append(commit)
    for boundary in plan.boundaries:
        if (
            _one_bookmark_target(fresh.local, None, boundary.branch)
            != boundary.remote_commit_id
            or _one_bookmark_target(fresh.local, plan.remote, boundary.branch)
            != boundary.remote_commit_id
        ):
            return f"native jj fetch did not adopt {boundary.branch} exactly"
        if boundary.local_commit_id != boundary.remote_commit_id:
            superseded = commits.get(boundary.local_commit_id)
            if superseded is None:
                return f"superseded commit is absent for {boundary.branch}"
            if not superseded.is_hidden:
                return f"superseded commit remains visible for {boundary.branch}"
        adopted = commits.get(boundary.remote_commit_id)
        if adopted is None or adopted.is_hidden or adopted.has_conflicts:
            return f"adopted boundary is absent, hidden, or conflicted for {boundary.branch}"
    for workspace, change_id in plan.workspace_change_ids:
        targets = tuple(
            target
            for name, target in fresh.local.workspace_targets
            if name == workspace
        )
        visible = visible_by_change.get(change_id, [])
        if len(targets) != 1 or len(visible) != 1:
            return f"workspace {workspace} was not reconciled by jj"
        if visible[0].commit_id != targets[0] or visible[0].has_conflicts:
            return f"workspace {workspace} is divergent or conflicted"
    return None


def record_verified_adoptions(
    workspace: str | Path,
    expected_state_oid: str | None,
    state: TrackedState,
    adoptions: Sequence[LastAdoptedHead],
) -> str:
    keys = {(item.pr, item.ref) for item in adoptions}
    if any((item.pr, item.ref) in keys for item in state.last_published_heads):
        publications = tuple(
            item
            for item in state.last_published_heads
            if (item.pr, item.ref) not in keys
        )
    else:
        publications = state.last_published_heads
    retained = tuple(
        item for item in state.last_adopted_heads if (item.pr, item.ref) not in keys
    )
    return cas_write_state(
        workspace,
        expected_state_oid,
        TrackedState(state.stacks, publications, retained + tuple(adoptions)),
    )


def adopt_remote_restack(
    plan: RemoteRestackAdoption, workspace: str | Path, github: GitHubClient
) -> AdoptionResult:
    """Adopt one frozen restack whose change IDs and patch IDs matched."""
    with repository_lock(workspace):
        try:
            before = reobserve_for_adoption(workspace, github, plan)
        except Error as exc:
            return Stopped("reobserve", str(exc))
        drift = _validate_adoption_prestate(plan, before)
        if drift is not None:
            return Stopped("revalidate", drift)
        result = subprocess.run(
            [
                "jj",
                "git",
                "fetch",
                "--remote",
                plan.remote,
                *(
                    argument
                    for boundary in plan.boundaries
                    for argument in ("--branch", boundary.branch)
                ),
            ],
            cwd=workspace,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            detail = result.stderr.strip() or result.stdout.strip()
            return Stopped("fetch", detail or "native jj fetch failed")
        try:
            after = reobserve_after_adoption(workspace, github, plan)
        except Error as exc:
            return Stopped(
                "verify",
                f"local adoption may have completed: {exc}",
                head_published=False,
            )
        mismatch = _validate_adoption_poststate(plan, after)
        if mismatch is not None:
            return Stopped("verify", f"local adoption may have completed: {mismatch}")
        pr_by_id = {pr.identity: pr for pr in plan.prs}
        receipts = tuple(
            LastAdoptedHead(
                boundary.pr_identity,
                RemoteBranchRef(
                    plan.repository,
                    f"refs/heads/{pr_by_id[boundary.pr_identity].head_branch}",
                ),
                boundary.remote_commit_id,
            )
            for boundary in plan.boundaries
            if boundary.remote_commit_id != boundary.tracked_commit_id
        )
        try:
            record_verified_adoptions(
                workspace,
                plan.state_blob_oid,
                after.tool_state.state,
                receipts,
            )
        except Error as exc:
            return Stopped(
                "receipt",
                f"local adoption verified but authority persistence failed: {exc}",
                final_state_verified=True,
            )
        return AdoptionVerified(receipts)


def start_first_publication(operation: FirstPublication, workspace: str | Path) -> str:
    """Install the durable fence before the first publication side effect."""
    _validate_first_publication(operation)
    if operation.phase is not FirstPublicationPhase.PREPARING_BOOKMARKS:
        raise ValueError("a new first-publication operation must start before effects")
    with repository_lock(workspace):
        state_oid, _state = read_state(workspace)
        if state_oid != operation.expected_state_oid:
            raise ConcurrentUpdate("private state changed before operation start")
        if read_ref_oid(workspace, OPERATION_REF) is not None:
            raise ConcurrentUpdate("another operation is already active")
        _local, readbacks = observe_publication_slots(workspace, operation)
        for slot, readback in zip(operation.slots, readbacks, strict=True):
            local_absent = readback.local_target is None
            local_exact = _exact_target(readback.local_target, slot.commit_id)
            if slot.bookmark_setup is BookmarkSetup.CREATE and not local_absent:
                raise ConcurrentUpdate(
                    f"CREATE bookmark {slot.branch} is no longer absent"
                )
            if slot.bookmark_setup is BookmarkSetup.KEEP and not local_exact:
                raise ConcurrentUpdate(f"KEEP bookmark {slot.branch} moved")
            if readback.live_commit_id is not None:
                raise ConcurrentUpdate(
                    f"publication destination {slot.branch} is no longer absent"
                )
            if not _absent_target(readback.remote_target):
                raise ConcurrentUpdate(
                    f"publication remote bookmark {slot.branch} is not absent"
                )
        return cas_write_first_publication(workspace, None, operation)


def _topology_refs(
    plan: TopologyPlan, workspace: str | Path, refs: Sequence[RemoteBranchRef]
) -> tuple[LiveRemoteRef, ...]:
    if not refs:
        return ()
    return observe_live_refs(
        plan.dependencies.push_url,
        plan.repository,
        tuple(ref.full_name for ref in refs),
        cwd=workspace,
    )


def start_topology_repair(plan: TopologyPlan, workspace: str | Path) -> str:
    """Reserve the complete temporary-base set before any external effect."""
    operation = TopologyRepair(plan)
    _validate_topology_repair(operation)
    with repository_lock(workspace):
        if read_ref_oid(workspace, STATE_REF) != plan.dependencies.state_blob_oid:
            raise ConcurrentUpdate("private state changed before operation start")
        if read_ref_oid(workspace, OPERATION_REF) is not None:
            raise ConcurrentUpdate("another operation is already active")
        expected = tuple(LiveRemoteRef(item.ref, None) for item in plan.temporary_bases)
        if (
            _topology_refs(
                plan, workspace, tuple(item.ref for item in plan.temporary_bases)
            )
            != expected
        ):
            raise ConcurrentUpdate("a frozen temporary base branch is occupied")
        return cas_write_topology_repair(workspace, None, operation)


def _frozen_repository(
    github: GitHubClient, identity: GitHubRepositoryId, name: str
) -> GitHubRepository:
    observed = github.resolve_repository(f"https://{identity.host}/{name}")
    if observed.identity != identity or observed.name_with_owner != name:
        raise SourceMismatch("frozen GitHub repository locator changed identity")
    return observed


def _observe_frozen_topology_sources(
    github: GitHubClient,
    plan: TopologyPlan,
    *,
    compare_head: bool = True,
    compare_base: bool = True,
    compare_metadata: bool = True,
    cwd: str | Path | None = None,
) -> tuple[GitHubPullRequest, ...]:
    sources = github.pull_requests(
        tuple(expected.identity for expected in plan.dependencies.prs)
    )
    for expected, actual in zip(plan.dependencies.prs, sources, strict=True):
        if (actual.identity, actual.number, actual.state, actual.draft) != (
            expected.identity,
            expected.number,
            expected.state,
            expected.draft,
        ):
            raise SourceMismatch(
                f"pull request #{expected.number} identity or state changed"
            )
        if compare_head and (
            actual.head_repository,
            actual.head_branch,
            actual.head_oid,
        ) != (
            expected.head_repository,
            expected.head_branch,
            expected.head_oid,
        ):
            raise SourceMismatch(f"pull request #{expected.number} head changed")
        if compare_base and (actual.base_branch, actual.base_oid) != (
            expected.base_branch,
            expected.base_oid,
        ):
            raise SourceMismatch(f"pull request #{expected.number} base changed")
        if compare_metadata and (actual.title, actual.body) != (
            expected.title,
            expected.body,
        ):
            raise SourceMismatch(f"pull request #{expected.number} metadata changed")
        if actual.auto_merge_enabled or actual.in_merge_queue:
            raise SourceMismatch(
                f"pull request #{expected.number} has active automation"
            )
    return sources


def _observe_source_association(
    github: GitHubClient,
    plan: TopologyPlan,
    repository: GitHubRepository,
    *,
    allow_dissolved: bool,
    compare_head: bool = True,
    compare_base: bool = True,
    cwd: str | Path | None = None,
) -> tuple[tuple[GitHubPullRequest, ...], GitHubStack | None]:
    sources = _observe_frozen_topology_sources(
        github,
        plan,
        compare_head=compare_head,
        compare_base=compare_base,
        cwd=cwd,
    )
    if all(item.stack is None for item in sources):
        if allow_dissolved or isinstance(plan.source, StandalonePullRequest) or (
            isinstance(plan.source, TrackedRestoreSource)
            and plan.source.current_stack is None
        ):
            return sources, None
        raise SourceMismatch("frozen server stack disappeared before unstack")
    expected = (
        plan.source.current_stack
        if isinstance(plan.source, TrackedRestoreSource)
        else plan.source
    )
    if not isinstance(expected, ServerStackMembership):
        raise SourceMismatch("standalone source gained a stack association")
    if any(
        (item.identity in expected.ordered_prs)
        != (item.stack is not None and item.stack.identity == expected.stack.identity)
        or (
            item.identity in expected.ordered_prs
            and (item.stack is None or item.stack.base_branch != expected.base_branch)
        )
        for item in sources
    ):
        raise SourceMismatch("frozen source stack associations changed")
    stack = github.stack(repository, expected.stack.identity)
    if stack is None:
        raise SourceMismatch("frozen source stack disappeared")
    if (
        stack.identity != expected.stack.identity
        or stack.base_branch != expected.base_branch
        or stack.pull_requests != expected.ordered_prs
    ):
        raise SourceMismatch("frozen source stack identity or membership changed")
    return sources, stack


def create_temporary_bases(
    workspace: str | Path,
    push_url: str,
    temporary_bases: Sequence[PlannedTemporaryBase],
) -> subprocess.CompletedProcess[str]:
    if not temporary_bases:
        raise ValueError("temporary-base creation requires branches")
    return subprocess.run(
        [
            "git",
            f"--git-dir={git_common_dir(workspace)}",
            "push",
            "--atomic",
            "--no-follow-tags",
            "--recurse-submodules=no",
            *(f"--force-with-lease={item.ref.full_name}:" for item in temporary_bases),
            push_url,
            *(
                f"{item.old_base_commit_id}:{item.ref.full_name}"
                for item in temporary_bases
            ),
        ],
        text=True,
        capture_output=True,
        check=False,
    )


def move_temporary_bases(
    workspace: str | Path,
    push_url: str,
    heads: Sequence[PlannedHeadUpdate],
    temporary_bases: Sequence[PlannedTemporaryBase],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "git",
            f"--git-dir={git_common_dir(workspace)}",
            "push",
            "--atomic",
            "--no-follow-tags",
            "--recurse-submodules=no",
            *(
                f"--force-with-lease={item.ref.full_name}:{item.expected_old_commit_id}"
                for item in heads
            ),
            *(
                f"--force-with-lease={item.ref.full_name}:{item.old_base_commit_id}"
                for item in temporary_bases
            ),
            push_url,
            *(f"{item.new_commit_id}:{item.ref.full_name}" for item in heads),
            *(
                f"{item.new_base_commit_id}:{item.ref.full_name}"
                for item in temporary_bases
            ),
        ],
        text=True,
        capture_output=True,
        check=False,
    )


def _replace_slot(
    operation: FirstPublication, index: int, slot: NewPRSlot, **changes: object
) -> FirstPublication:
    slots = list(operation.slots)
    slots[index] = slot
    return replace(operation, slots=tuple(slots), **changes)


def _observe_slot_refs(
    operation: FirstPublication, workspace: str | Path
) -> tuple[LiveRemoteRef, ...]:
    return observe_live_refs(
        operation.push_url,
        operation.repository,
        tuple(f"refs/heads/{slot.branch}" for slot in operation.slots),
        cwd=workspace,
    )


def _verify_first_publication_external(
    operation: FirstPublication, workspace: str | Path, github: GitHubClient
) -> str | None:
    try:
        _local, readbacks = observe_publication_slots(workspace, operation)
        for slot, readback in zip(operation.slots, readbacks, strict=True):
            if (
                not _exact_target(readback.local_target, slot.commit_id)
                or readback.live_commit_id != slot.commit_id
                or not _exact_target(readback.remote_target, slot.commit_id)
                or readback.remote_tracking is not TrackingState.TRACKED
            ):
                return f"publication readiness changed for {slot.branch}"
        for slot in operation.slots:
            assert slot.pr_identity is not None
            prs = github.find_pull_requests(
                operation.repository, head_branches=(slot.branch,)
            )
            matches = tuple(pr for pr in prs if pr.identity == slot.pr_identity)
            if len(matches) != 1 or not _slot_pr_matches(
                operation.repository, slot, matches[0], initial=False
            ):
                return f"pull request for slot {slot.slot_id} does not match the goal"
        if operation.target is not FirstPublicationTarget.STANDALONE:
            stack = observe_first_publication_stack(github, operation)
            if (
                operation.stack_phase is not StackLinkPhase.VERIFIED
                or operation.resulting_stack is None
                or stack is None
                or stack.identity != operation.resulting_stack
            ):
                return "published stack topology does not match the frozen goal"
    except Error as exc:
        return str(exc)
    return None


def _build_first_publication_state(
    operation: FirstPublication, state: TrackedState
) -> TrackedState:
    identities = tuple(slot.pr_identity for slot in operation.slots)
    if any(identity is None for identity in identities):
        raise Error("cannot commit first publication before every slot is bound")
    new_prs = tuple(identity for identity in identities if identity is not None)
    ordered = operation.existing_prs + new_prs
    if len(set(ordered)) != len(ordered):
        raise Error("first-publication final membership contains duplicate PRs")
    if operation.target is FirstPublicationTarget.APPEND:
        matches = tuple(
            stack
            for stack in state.stacks
            if stack.repository == operation.repository
            and stack.base_branch == operation.base_branch
            and stack.ordered_prs == operation.existing_prs
        )
        if len(matches) != 1:
            raise Error("append source no longer has one exact tracked stack")
        stacks = tuple(stack for stack in state.stacks if stack != matches[0])
    else:
        if any(
            stack.repository == operation.repository
            and set(stack.ordered_prs).intersection(ordered)
            for stack in state.stacks
        ):
            raise Error("new PR membership overlaps an existing tracked stack")
        stacks = state.stacks
    final_stack = TrackedStack(operation.repository, operation.base_branch, ordered)
    refs = tuple(
        RemoteBranchRef(operation.repository, f"refs/heads/{slot.branch}")
        for slot in operation.slots
    )
    keys = set(zip(new_prs, refs, strict=True))
    publications = tuple(
        item for item in state.last_published_heads if (item.pr, item.ref) not in keys
    ) + tuple(
        LastPublishedHead(pr, ref, slot.commit_id)
        for pr, ref, slot in zip(new_prs, refs, operation.slots, strict=True)
    )
    adoptions = tuple(
        item for item in state.last_adopted_heads if (item.pr, item.ref) not in keys
    )
    return TrackedState(stacks + (final_stack,), publications, adoptions)


def _finish_first_publication(
    operation_oid: str,
    operation: FirstPublication,
    workspace: str | Path,
    github: GitHubClient,
) -> FirstPublicationResult:
    state_oid, state = read_state(workspace)
    if operation.phase is not FirstPublicationPhase.COMMITTING:
        if state_oid != operation.expected_state_oid:
            return Stopped("state", "private state changed before final commit")
        external_error = _verify_first_publication_external(
            operation, workspace, github
        )
        if external_error is not None:
            return Stopped("verify", external_error)
        final_state = _build_first_publication_state(operation, state)
        final_json = state_to_json(final_state)
        common = git_common_dir(workspace)
        final_oid = _run(
            ["git", f"--git-dir={common}", "hash-object", "-w", "--stdin"],
            stdin=final_json,
        ).strip()
        operation = replace(
            operation,
            phase=FirstPublicationPhase.COMMITTING,
            final_state_json=final_json,
            final_state_oid=final_oid,
        )
        operation_oid = cas_write_first_publication(workspace, operation_oid, operation)
    assert operation.final_state_json is not None
    assert operation.final_state_oid is not None
    current_state_oid = read_ref_oid(workspace, STATE_REF)
    if current_state_oid == operation.expected_state_oid:
        external_error = _verify_first_publication_external(
            operation, workspace, github
        )
        if external_error is not None:
            return Stopped("verify", external_error)
        final_state = parse_state(operation.final_state_json)
        written = cas_write_state(workspace, operation.expected_state_oid, final_state)
        if written != operation.final_state_oid:
            return Stopped("state", "final state blob differs from the frozen payload")
    elif current_state_oid != operation.final_state_oid:
        return Stopped("state", "private state is neither pre-commit nor final")
    try:
        cas_delete_operation(workspace, operation_oid)
    except Error as exc:
        return Stopped(
            "fence",
            f"state committed but operation fence cleanup failed: {exc}",
            final_state_verified=True,
            authority_persisted=True,
            tracking_persisted=True,
        )
    return FirstPublicationVerified(
        operation.existing_prs
        + tuple(slot.pr_identity for slot in operation.slots if slot.pr_identity is not None)
    )


def _prepare_publication_bookmarks(
    workspace: str | Path, operation: FirstPublication
) -> Stopped | None:
    for slot in operation.slots:
        try:
            _local, readbacks = observe_publication_slots(workspace, operation)
        except Error as exc:
            return Stopped("bookmark", str(exc))
        readback = readbacks[operation.slots.index(slot)]
        if slot.bookmark_setup is BookmarkSetup.KEEP:
            if not _exact_target(readback.local_target, slot.commit_id):
                return Stopped("bookmark", f"KEEP bookmark {slot.branch} moved")
            continue
        if readback.local_target is None:
            create_publication_bookmark(workspace, slot)
            try:
                _local, readbacks = observe_publication_slots(workspace, operation)
            except Error as exc:
                return Stopped("bookmark", f"bookmark result is unknown: {exc}")
            readback = readbacks[operation.slots.index(slot)]
        if not _exact_target(readback.local_target, slot.commit_id):
            return Stopped(
                "bookmark",
                f"CREATE bookmark {slot.branch} is absent, moved, or conflicted",
            )
    return None


def _other_remote_is_tracked(
    local: LocalObservation, operation: FirstPublication, slot: NewPRSlot
) -> bool:
    return any(
        item.name == slot.branch
        and item.remote != operation.remote
        and item.tracking_state is TrackingState.TRACKED
        for item in local.remote_bookmarks
    )


def _prepare_publication_tracking(
    workspace: str | Path,
    operation: FirstPublication,
) -> Stopped | None:
    """Narrowly pre-track an absent destination only when another remote requires it."""
    try:
        local, readbacks = observe_publication_slots(workspace, operation)
    except Error as exc:
        return Stopped("tracking", str(exc))
    for index, slot in enumerate(operation.slots):
        if slot.phase is not NewPRPhase.NOT_ATTEMPTED:
            continue
        readback = readbacks[index]
        if not _other_remote_is_tracked(local, operation, slot):
            continue
        if readback.live_commit_id is not None or not _absent_target(
            readback.remote_target
        ):
            return Stopped(
                "tracking",
                f"cannot prepare tracking for non-absent destination {slot.branch}",
            )
        if readback.remote_tracking is not TrackingState.TRACKED:
            track_publication_bookmark(workspace, operation, slot)
            try:
                local, readbacks = observe_publication_slots(workspace, operation)
            except Error as exc:
                return Stopped("tracking", f"tracking result is unknown: {exc}")
            readback = readbacks[index]
        if (
            not _absent_target(readback.remote_target)
            or readback.remote_tracking is not TrackingState.TRACKED
        ):
            return Stopped(
                "tracking",
                f"absent destination {slot.branch}@{operation.remote} is not tracked",
            )
    return None


def _publication_readiness_error(
    workspace: str | Path, operation: FirstPublication
) -> str | None:
    try:
        _local, readbacks = observe_publication_slots(workspace, operation)
    except Error as exc:
        return str(exc)
    for slot, readback in zip(operation.slots, readbacks, strict=True):
        if (
            not _exact_target(readback.local_target, slot.commit_id)
            or readback.live_commit_id != slot.commit_id
            or not _exact_target(readback.remote_target, slot.commit_id)
            or readback.remote_tracking is not TrackingState.TRACKED
        ):
            return f"slot {slot.slot_id} is not publication-ready"
    return None


def resume_first_publication(
    workspace: str | Path, github: GitHubClient
) -> FirstPublicationResult:
    """Continue only the exact frozen goal stored in the operation ref."""
    with repository_lock(workspace):
        try:
            operation_oid, operation = read_first_publication(workspace)
        except Error as exc:
            return Stopped("operation", str(exc))
        if operation.phase is FirstPublicationPhase.COMMITTING:
            return _finish_first_publication(
                operation_oid, operation, workspace, github
            )
        state_oid, _state = read_state(workspace)
        if state_oid != operation.expected_state_oid:
            return Stopped("state", "private state changed while operation is active")

        if operation.phase is FirstPublicationPhase.PREPARING_BOOKMARKS:
            stopped = _prepare_publication_bookmarks(workspace, operation)
            if stopped is not None:
                return stopped
            operation = replace(operation, phase=FirstPublicationPhase.PUBLISHING)
            operation_oid = cas_write_first_publication(
                workspace, operation_oid, operation
            )

        if operation.phase is FirstPublicationPhase.PUBLISHING:
            stopped = _prepare_publication_tracking(workspace, operation)
            if stopped is not None:
                return stopped
            try:
                _local, readbacks = observe_publication_slots(workspace, operation)
            except Error as exc:
                return Stopped("publish", str(exc))
            actions = classify_publication_recovery(operation, readbacks)
            if isinstance(actions, Stopped):
                return actions
            first_attempt = tuple(
                slot
                for slot, action in zip(operation.slots, actions, strict=True)
                if slot.phase is NewPRPhase.NOT_ATTEMPTED
                and action is PublicationRecoveryAction.PUSH
            )
            if first_attempt:
                operation = replace(
                    operation,
                    slots=tuple(
                        replace(slot, phase=NewPRPhase.PUBLICATION_POSSIBLY_SENT)
                        if slot in first_attempt
                        else slot
                        for slot in operation.slots
                    ),
                )
                operation_oid = cas_write_first_publication(
                    workspace, operation_oid, operation
                )
                push_new_slot_refs(workspace, operation, first_attempt)
            else:
                retries = tuple(
                    slot
                    for slot, action in zip(operation.slots, actions, strict=True)
                    if action is PublicationRecoveryAction.PUSH
                )
                if retries:
                    push_new_slot_refs(workspace, operation, retries)
            try:
                _local, readbacks = observe_publication_slots(workspace, operation)
            except Error as exc:
                return Stopped("publish", f"publication result is unknown: {exc}")
            actions = classify_publication_recovery(operation, readbacks)
            if isinstance(actions, Stopped):
                return actions
            for slot, action in zip(operation.slots, actions, strict=True):
                if action is PublicationRecoveryAction.TRACK:
                    track_publication_bookmark(workspace, operation, slot)
            if PublicationRecoveryAction.TRACK in actions:
                try:
                    _local, readbacks = observe_publication_slots(workspace, operation)
                except Error as exc:
                    return Stopped("tracking", f"tracking result is unknown: {exc}")
                actions = classify_publication_recovery(operation, readbacks)
                if isinstance(actions, Stopped):
                    return actions
            if any(action is not PublicationRecoveryAction.READY for action in actions):
                return Stopped(
                    "publish",
                    "publication did not establish every exact tracked remote bookmark",
                )
            operation = replace(
                operation,
                slots=tuple(
                    replace(slot, phase=NewPRPhase.READY) for slot in operation.slots
                ),
                phase=FirstPublicationPhase.CREATING_PRS,
            )
            operation_oid = cas_write_first_publication(
                workspace, operation_oid, operation
            )

        for index, slot in enumerate(operation.slots):
            readiness_error = _publication_readiness_error(workspace, operation)
            if readiness_error is not None:
                return Stopped("readiness", readiness_error)
            if slot.phase is NewPRPhase.READY:
                slot = replace(slot, phase=NewPRPhase.POSSIBLY_SENT)
                operation = _replace_slot(operation, index, slot)
                operation_oid = cas_write_first_publication(
                    workspace, operation_oid, operation
                )
                returned: GitHubPullRequest | None = None
                try:
                    repository = github.resolve_repository(operation.repository)
                    created = github.create_pull_request(
                        repository,
                        head_branch=slot.branch,
                        base_branch=slot.base_branch,
                        title=slot.title,
                        body=slot.initial_body,
                        draft=True,
                    )
                    candidate = github.pull_requests((created,))[0]
                    if _slot_pr_matches(
                        operation.repository, slot, candidate, initial=True
                    ):
                        returned = candidate
                except Error:
                    # The request may have reached GitHub. Resolve only by marker.
                    pass
                if returned is not None:
                    matches = (returned,)
                else:
                    try:
                        matches = tuple(
                            pr
                            for pr in github.find_pull_requests(
                                operation.repository,
                                head_branches=(slot.branch,),
                            )
                            if slot.marker in pr.body
                        )
                    except Error as exc:
                        return Stopped(
                            "create-pr", f"possibly sent; lookup failed: {exc}"
                        )
            elif slot.phase is NewPRPhase.POSSIBLY_SENT:
                try:
                    matches = tuple(
                        pr
                        for pr in github.find_pull_requests(
                            operation.repository,
                            head_branches=(slot.branch,),
                        )
                        if slot.marker in pr.body
                    )
                except Error as exc:
                    return Stopped("create-pr", f"possibly sent; lookup failed: {exc}")
            else:
                matches = ()
            if slot.phase is NewPRPhase.POSSIBLY_SENT:
                valid = tuple(
                    pr
                    for pr in matches
                    if _slot_pr_matches(
                        operation.repository, slot, pr, initial=True
                    )
                )
                if len(valid) != 1:
                    return Stopped(
                        "create-pr",
                        "possibly sent; marker lookup did not find exactly one matching PR",
                    )
                slot = replace(
                    slot,
                    phase=NewPRPhase.BOUND,
                    pr_identity=valid[0].identity,
                )
                operation = _replace_slot(operation, index, slot)
                operation_oid = cas_write_first_publication(
                    workspace, operation_oid, operation
                )
            if slot.phase is NewPRPhase.BOUND:
                assert slot.pr_identity is not None
                readiness_error = _publication_readiness_error(workspace, operation)
                if readiness_error is not None:
                    return Stopped("readiness", readiness_error)
                try:
                    observed = github.find_pull_requests(
                        operation.repository, head_branches=(slot.branch,)
                    )
                    matches = tuple(
                        pr for pr in observed if pr.identity == slot.pr_identity
                    )
                    if len(matches) != 1:
                        return Stopped("metadata", "bound PR is not uniquely observable")
                    repository = github.resolve_repository(operation.repository)
                    github.update_pull_request(
                        repository,
                        slot.pr_identity,
                        title=slot.title,
                        body=slot.body,
                    )
                    observed = github.find_pull_requests(
                        operation.repository, head_branches=(slot.branch,)
                    )
                except Error as exc:
                    return Stopped("metadata", str(exc))
                matches = tuple(
                    pr for pr in observed if pr.identity == slot.pr_identity
                )
                if len(matches) != 1 or not _slot_pr_matches(
                    operation.repository, slot, matches[0], initial=False
                ):
                    return Stopped("metadata", "bound PR did not reach frozen metadata")
                slot = replace(slot, phase=NewPRPhase.VERIFIED)
                operation = _replace_slot(operation, index, slot)
                operation_oid = cas_write_first_publication(
                    workspace, operation_oid, operation
                )
        if any(slot.phase is not NewPRPhase.VERIFIED for slot in operation.slots):
            return Stopped("create-pr", "not every first-publication slot is verified")
        if operation.target is not FirstPublicationTarget.STANDALONE:
            readiness_error = _publication_readiness_error(workspace, operation)
            if readiness_error is not None:
                return Stopped("readiness", readiness_error)
            send_stack = False
            if operation.stack_phase is StackLinkPhase.NOT_ATTEMPTED:
                try:
                    if not append_source_is_unchanged(github, operation):
                        return Stopped(
                            "stack-link", "existing append stack changed before linking"
                        )
                except Error as exc:
                    return Stopped("stack-link", str(exc))
                operation = replace(
                    operation,
                    stack_phase=StackLinkPhase.POSSIBLY_SENT,
                    phase=FirstPublicationPhase.LINKING_STACK,
                )
                operation_oid = cas_write_first_publication(
                    workspace, operation_oid, operation
                )
                send_stack = True
            if operation.stack_phase is StackLinkPhase.POSSIBLY_SENT:
                try:
                    stack = observe_first_publication_stack(github, operation)
                except Error as exc:
                    return Stopped("stack-link", f"topology readback failed: {exc}")
                if stack is None and send_stack:
                    readiness_error = _publication_readiness_error(workspace, operation)
                    if readiness_error is not None:
                        return Stopped("readiness", readiness_error)
                    try:
                        if not append_source_is_unchanged(github, operation):
                            return Stopped(
                                "stack-link", "existing append stack changed before request"
                            )
                    except Error as exc:
                        return Stopped("stack-link", str(exc))
                    try:
                        run_first_publication_stack_link(github, operation)
                    except Error:
                        # The mutation may have reached GitHub. Only topology
                        # readback determines whether it completed.
                        pass
                    try:
                        stack = observe_first_publication_stack(github, operation)
                    except Error as exc:
                        return Stopped(
                            "stack-link", f"possibly sent; readback failed: {exc}"
                        )
                if stack is None:
                    return Stopped(
                        "stack-link",
                        "possibly sent; complete stack membership is not yet exact",
                    )
                operation = replace(
                    operation,
                    stack_phase=StackLinkPhase.VERIFIED,
                    resulting_stack=stack.identity,
                )
                operation_oid = cas_write_first_publication(
                    workspace, operation_oid, operation
                )
        return _finish_first_publication(operation_oid, operation, workspace, github)


def apply(
    plan: SyncPlan, workspace: str | Path, github: GitHubClient
) -> ApplyResult:
    """Execute one frozen existing-stack plan without replanning or lease refresh."""
    unsupported = _apply_shape(plan)
    if unsupported is not None:
        return unsupported
    assert isinstance(plan, NoOp | Apply)
    with repository_lock(workspace):
        try:
            fresh = reobserve_for_apply(workspace, github, plan)
        except Error as exc:
            return Stopped("reobserve", str(exc))
        drift = validate_frozen_dependencies(plan, fresh)
        if drift is not None:
            reason = drift.reasons[0]
            return Stopped("revalidate", f"{reason.code}: {reason.detail}")
        if isinstance(plan, NoOp):
            _fresh, stopped = verify_final_state(workspace, github, plan)
            return stopped or Verified(False, False, False)

        head_published = False
        metadata_updated = False
        metadata_error = False
        if plan.head_updates:
            response = push_exact_head_updates(
                workspace, plan.head_updates, plan.dependencies.push_url
            )
            if response.returncode == 0:
                head_published = True
            else:
                try:
                    observed = observe_live_refs(
                        plan.dependencies.push_url,
                        plan.desired.repository,
                        tuple(update.ref.full_name for update in plan.head_updates),
                        cwd=workspace,
                    )
                except Error as exc:
                    return Stopped("push", f"push result is unknown: {exc}")
                actual = tuple(item.commit_id for item in observed)
                wanted = tuple(item.new_commit_id for item in plan.head_updates)
                previous = tuple(
                    item.expected_old_commit_id for item in plan.head_updates
                )
                if actual == wanted:
                    head_published = True
                elif actual == previous:
                    return Stopped("push", "push was not observed at the destination")
                else:
                    return Stopped(
                        "push",
                        "destination heads have unexpected or partial values",
                        head_published=any(
                            actual_commit == update.new_commit_id
                            for actual_commit, update in zip(
                                actual, plan.head_updates, strict=True
                            )
                        ),
                    )
        if plan.head_updates and plan.metadata_updates:
            try:
                published = reobserve_for_apply(workspace, github, plan)
            except Error as exc:
                return Stopped(
                    "pre-metadata-verify",
                    f"heads were published but could not be verified before metadata: {exc}",
                    head_published=head_published,
                )
            expected_heads = {
                update.ref.full_name: update.new_commit_id
                for update in plan.head_updates
            }
            live_heads = {
                item.ref.full_name: item.commit_id for item in published.live_refs
            }
            pr_heads = {
                f"refs/heads/{pr.head_branch}": pr.head_oid
                for pr in published.pull_requests
            }
            if any(
                live_heads.get(name) != commit_id or pr_heads.get(name) != commit_id
                for name, commit_id in expected_heads.items()
            ):
                return Stopped(
                    "pre-metadata-verify",
                    "a published head was superseded before metadata update",
                    head_published=head_published,
                )
        for update in plan.metadata_updates:
            try:
                repository = github.resolve_repository(plan.desired.repository)
                github.update_pull_request(
                    repository,
                    update.pr_identity,
                    title=update.title,
                    body=update.body,
                )
                metadata_updated = True
            except Error:
                # A lost API response is resolved by the same authoritative final read.
                metadata_error = True

        final, stopped = verify_final_state(workspace, github, plan)
        if stopped is not None:
            if plan.metadata_updates and (metadata_updated or metadata_error):
                stopped = replace(
                    stopped,
                    detail=stopped.detail + "; metadata updates may be incomplete",
                )
            return replace(
                stopped,
                head_published=head_published,
                metadata_updated=metadata_updated,
            )
        assert final is not None
        if plan.metadata_updates:
            metadata_updated = True
        pr_by_ref = {
            RemoteBranchRef(plan.desired.repository, f"refs/heads/{pr.head_branch}"): pr
            for pr in plan.dependencies.prs
        }
        publications = tuple(
            LastPublishedHead(
                pr_by_ref[update.ref].identity,
                update.ref,
                update.new_commit_id,
            )
            for update in plan.head_updates
        )
        if not publications and plan.tracking_update is None:
            return Verified(False, metadata_updated, False)
        try:
            record_verified_state(
                workspace,
                plan.dependencies.state_blob_oid,
                final.tool_state.state,
                plan.tracking_update,
                publications,
            )
        except Error as exc:
            return Stopped(
                "receipt",
                f"effects verified but state persistence failed: {exc}",
                head_published=head_published,
                metadata_updated=metadata_updated,
                final_state_verified=True,
            )
        return Verified(
            head_published,
            metadata_updated,
            bool(publications),
            plan.tracking_update is not None,
        )


def run_stack_unstack(
    github: GitHubClient,
    repository: GitHubRepository,
    source: ServerStackMembership,
) -> GitHubStackSummary | None:
    return github.unstack(repository, source.stack.identity)


def prove_stack_dissolved(
    github: GitHubClient,
    repository: GitHubRepository,
    identity: GitHubStackId,
) -> StackDissolution:
    try:
        stack = github.stack(repository, identity)
    except Error:
        return StackDissolution.UNKNOWN
    return StackDissolution.ABSENT if stack is None else StackDissolution.PRESENT


def run_pr_base_edit(
    github: GitHubClient,
    repository: GitHubRepository,
    identity: PullRequestId,
    desired_base: str,
) -> None:
    github.update_pull_request(repository, identity, base_branch=desired_base)


def _topology_desired_bases(plan: TopologyPlan) -> dict[PullRequestId, str]:
    frozen = {pr.identity: pr for pr in plan.dependencies.prs}
    result: dict[PullRequestId, str] = {}
    previous = plan.desired.base_branch
    for wanted in plan.desired.active:
        result[wanted.pr_identity] = previous
        previous = frozen[wanted.pr_identity].head_branch
    return result


def run_topology_stack_link(
    github: GitHubClient, repository: GitHubRepository, plan: TopologyPlan
) -> GitHubStackSummary:
    identities = tuple(item.pr_identity for item in plan.desired.active)
    return github.create_stack(repository, pull_requests=identities)


def observe_topology_projection(
    github: GitHubClient, plan: TopologyPlan
) -> tuple[tuple[GitHubPullRequest, ...], GitHubStack | None]:
    repository = _frozen_repository(github, plan.repository, plan.repository_name)
    sources = github.pull_requests(
        tuple(pr.identity for pr in plan.dependencies.prs)
    )
    desired = tuple(item.pr_identity for item in plan.desired.active)
    stack_ids = {item.stack.identity for item in sources if item.stack is not None}
    stack = None
    if len(desired) >= 2 and len(stack_ids) == 1:
        stack = github.stack(repository, next(iter(stack_ids)))
    return sources, stack


def _topology_projection_error(
    plan: TopologyPlan,
    sources: Sequence[GitHubPullRequest],
    stack: GitHubStack | None,
) -> str | None:
    frozen = {pr.identity: pr for pr in plan.dependencies.prs}
    actual = {item.identity: item for item in sources}
    if set(actual) != set(frozen):
        return "source PR identity set changed"
    desired = tuple(item.pr_identity for item in plan.desired.active)
    desired_by_id = {item.pr_identity: item for item in plan.desired.active}
    bases = _topology_desired_bases(plan)
    for identity, expected in frozen.items():
        pr = actual[identity]
        if pr.state is not PullRequestState.OPEN or pr.number != expected.number:
            return f"pull request #{expected.number} is not the frozen open PR"
        if identity in desired_by_id:
            wanted = desired_by_id[identity]
            if (
                pr.head_oid,
                pr.base_branch,
                pr.title,
                pr.body,
                pr.draft,
            ) != (
                wanted.desired_commit_id,
                bases[identity],
                wanted.title,
                wanted.body,
                expected.draft,
            ):
                return f"pull request #{expected.number} does not match the desired projection"
        elif pr.stack is not None:
            return f"detached pull request #{expected.number} is not standalone"
    if len(desired) >= 2:
        if stack is None or stack.pull_requests != desired:
            return "resulting stack membership or order is not exact"
        if stack.base_branch != plan.desired.base_branch:
            return "resulting stack base is not exact"
    elif any(actual[identity].stack is not None for identity in desired):
        return "zero/one desired PR must be standalone"
    return None


def _build_topology_final_state(
    plan: TopologyPlan, state: TrackedState
) -> TrackedState:
    source_ids = tuple(pr.identity for pr in plan.dependencies.prs)
    source_base = (
        plan.source.tracked.base_branch
        if isinstance(plan.source, TrackedRestoreSource)
        else (
            plan.dependencies.prs[0].base_branch
            if isinstance(plan.source, StandalonePullRequest)
            else plan.source.base_branch
        )
    )
    matching = tuple(
        stack
        for stack in state.stacks
        if stack.repository == plan.repository
        and stack.base_branch == source_base
        and stack.ordered_prs == source_ids
        and (
            not isinstance(plan.source, TrackedRestoreSource)
            or stack == plan.source.tracked
        )
    )
    if len(matching) != 1:
        raise Error("private state no longer contains the exact source record")
    stacks = tuple(stack for stack in state.stacks if stack != matching[0])
    if plan.tracking_update is not None:
        stacks += (plan.tracking_update,)
    publications = state.last_published_heads
    adoptions = state.last_adopted_heads
    frozen = {pr.identity: pr for pr in plan.dependencies.prs}
    for update in plan.head_updates:
        identity = next(
            identity
            for identity, pr in frozen.items()
            if update.ref.full_name == f"refs/heads/{pr.head_branch}"
        )
        key = (identity, update.ref)
        publications = tuple(
            item for item in publications if (item.pr, item.ref) != key
        ) + (LastPublishedHead(identity, update.ref, update.new_commit_id),)
        adoptions = tuple(item for item in adoptions if (item.pr, item.ref) != key)
    return TrackedState(stacks, publications, adoptions)


def _delete_temporary_base_command(
    workspace: str | Path, plan: TopologyPlan, item: PlannedTemporaryBase
) -> list[str]:
    return [
        "git",
        f"--git-dir={git_common_dir(workspace)}",
        "push",
        "--atomic",
        "--no-follow-tags",
        "--recurse-submodules=no",
        f"--force-with-lease={item.ref.full_name}:{item.new_base_commit_id}",
        plan.dependencies.push_url,
        f":{item.ref.full_name}",
    ]


def _observe_topology_pr(
    github: GitHubClient,
    repository: GitHubRepository,
    expected: GitHubPullRequest,
    *,
    by_identity: bool = False,
    expected_head_commit_id: str | None = None,
    cwd: str | Path | None = None,
) -> GitHubPullRequest:
    actual = github.pull_requests((expected.identity,))[0]
    if (
        actual.identity != expected.identity
        or actual.number != expected.number
        or actual.state is not PullRequestState.OPEN
        or actual.draft != expected.draft
        or actual.head_repository != expected.head_repository
        or actual.head_branch != expected.head_branch
        or actual.head_oid != (expected_head_commit_id or expected.head_oid)
        or actual.title != expected.title
        or actual.body != expected.body
        or actual.auto_merge_enabled
        or actual.in_merge_queue
    ):
        raise SourceMismatch(f"pull request #{expected.number} changed")
    return actual


def resume_topology_repair(
    workspace: str | Path, github: GitHubClient
) -> TopologyRepairResult:
    """Resume the retained topology transaction from authoritative readback."""
    with repository_lock(workspace):
        try:
            operation_oid, raw = read_operation(workspace)
        except Error as exc:
            return Stopped("operation", str(exc))
        if not isinstance(raw, TopologyRepair):
            return Stopped("operation", "active operation is not a topology repair")
        operation = raw
        plan = operation.plan
        restore = isinstance(plan.source, TrackedRestoreSource)
        try:
            repository = _frozen_repository(github, plan.repository, plan.repository_name)
        except Error as exc:
            return Stopped("routing", str(exc))

        def save(**changes: object) -> None:
            nonlocal operation, operation_oid
            operation = replace(operation, **changes)
            operation_oid = cas_write_topology_repair(
                workspace, operation_oid, operation
            )

        try:
            if operation.phase is TopologyRepairPhase.CREATING_TEMPORARY_BASES:
                if plan.temporary_bases:
                    refs = tuple(item.ref for item in plan.temporary_bases)
                    values = tuple(
                        item.commit_id for item in _topology_refs(plan, workspace, refs)
                    )
                    absent = (None,) * len(refs)
                    exact = tuple(
                        item.old_base_commit_id for item in plan.temporary_bases
                    )
                    if values == absent:
                        create_temporary_bases(
                            workspace,
                            plan.dependencies.push_url,
                            plan.temporary_bases,
                        )
                        values = tuple(
                            item.commit_id
                            for item in _topology_refs(plan, workspace, refs)
                        )
                    if values != exact:
                        return Stopped(
                            "temporary-bases",
                            "temporary-base readback is absent, mixed, or foreign",
                        )
                save(phase=TopologyRepairPhase.UNSTACKING)

            if operation.phase is TopologyRepairPhase.UNSTACKING:
                membership = (
                    plan.source.current_stack
                    if isinstance(plan.source, TrackedRestoreSource)
                    else plan.source
                )
                _sources, stack = _observe_source_association(
                    github,
                    plan,
                    repository,
                    allow_dissolved=True,
                    cwd=workspace,
                )
                if isinstance(membership, ServerStackMembership):
                    if stack is not None and not operation.unstack_possibly_sent:
                        save(unstack_possibly_sent=True)
                        try:
                            run_stack_unstack(github, repository, membership)
                        except Error:
                            pass
                    if (
                        prove_stack_dissolved(
                            github,
                            repository,
                            membership.stack.identity,
                        )
                        is not StackDissolution.ABSENT
                    ):
                        return Stopped("unstack", "old stack dissolution is unproven")
                _sources, stack = _observe_source_association(
                    github,
                    plan,
                    repository,
                    allow_dissolved=True,
                    cwd=workspace,
                )
                if stack is not None:
                    return Stopped("unstack", "source stack still survives")
                save(phase=TopologyRepairPhase.TEMPORARY_BASES)

            if operation.phase is TopologyRepairPhase.TEMPORARY_BASES:
                frozen = {pr.identity: pr for pr in plan.dependencies.prs}
                for item in plan.temporary_bases:
                    source = _observe_topology_pr(
                        github,
                        repository,
                        frozen[item.pr_identity],
                        by_identity=restore,
                        cwd=workspace,
                    )
                    if source.stack is not None:
                        return Stopped(
                            "temporary-base", "a stack association reappeared"
                        )
                    name = item.ref.full_name.removeprefix("refs/heads/")
                    if source.base_branch == name:
                        if item.pr_identity in operation.temporary_bases_possibly_sent:
                            save(
                                temporary_bases_possibly_sent=tuple(
                                    identity
                                    for identity in operation.temporary_bases_possibly_sent
                                    if identity != item.pr_identity
                                )
                            )
                        continue
                    if source.base_branch != frozen[item.pr_identity].base_branch:
                        return Stopped(
                            "temporary-base",
                            "PR base is neither frozen nor the temporary base",
                        )
                    send_base = False
                    if item.pr_identity not in operation.temporary_bases_possibly_sent:
                        save(
                            temporary_bases_possibly_sent=(
                                *operation.temporary_bases_possibly_sent,
                                item.pr_identity,
                            )
                        )
                        send_base = True
                    if not send_base:
                        return Stopped(
                            "temporary-base",
                            "possibly sent base edit still reads as its old base",
                        )
                    try:
                        run_pr_base_edit(github, repository, item.pr_identity, name)
                    except Error:
                        pass
                    verify = _observe_topology_pr(
                        github,
                        repository,
                        frozen[item.pr_identity],
                        by_identity=restore,
                        cwd=workspace,
                    )
                    if verify.base_branch != name:
                        return Stopped("temporary-base", "base edit did not read back")
                    save(
                        temporary_bases_possibly_sent=tuple(
                            identity
                            for identity in operation.temporary_bases_possibly_sent
                            if identity != item.pr_identity
                        )
                    )
                save(phase=TopologyRepairPhase.PUBLISHING)

            if operation.phase is TopologyRepairPhase.PUBLISHING:
                sources = tuple(
                    _observe_topology_pr(
                        github,
                        repository,
                        pr,
                        by_identity=restore,
                        cwd=workspace,
                    )
                    for pr in plan.dependencies.prs
                )
                if any(item.stack is not None for item in sources):
                    return Stopped("publish", "a stack association reappeared")
                refs = tuple(item.ref for item in plan.head_updates) + tuple(
                    item.ref for item in plan.temporary_bases
                )
                old = tuple(
                    item.expected_old_commit_id for item in plan.head_updates
                ) + tuple(item.old_base_commit_id for item in plan.temporary_bases)
                desired = tuple(
                    item.new_commit_id for item in plan.head_updates
                ) + tuple(item.new_base_commit_id for item in plan.temporary_bases)
                values = tuple(
                    item.commit_id for item in _topology_refs(plan, workspace, refs)
                )
                if values == old and refs:
                    move_temporary_bases(
                        workspace,
                        plan.dependencies.push_url,
                        plan.head_updates,
                        plan.temporary_bases,
                    )
                    values = tuple(
                        item.commit_id for item in _topology_refs(plan, workspace, refs)
                    )
                if values != desired:
                    return Stopped(
                        "publish", "transition readback is old, mixed, or foreign"
                    )
                save(phase=TopologyRepairPhase.RELINKING)

            if operation.phase is TopologyRepairPhase.RELINKING:
                frozen = {pr.identity: pr for pr in plan.dependencies.prs}
                bases = _topology_desired_bases(plan)
                for wanted in plan.desired.active:
                    source = _observe_topology_pr(
                        github,
                        repository,
                        frozen[wanted.pr_identity],
                        by_identity=restore,
                        expected_head_commit_id=wanted.desired_commit_id,
                        cwd=workspace,
                    )
                    if source.stack is not None:
                        return Stopped("relink", "a stack association reappeared")
                    if (
                        source.base_branch == bases[wanted.pr_identity]
                        and wanted.pr_identity
                        in operation.temporary_bases_possibly_sent
                    ):
                        save(
                            temporary_bases_possibly_sent=tuple(
                                identity
                                for identity in operation.temporary_bases_possibly_sent
                                if identity != wanted.pr_identity
                            )
                        )
                    if source.base_branch != bases[wanted.pr_identity]:
                        send_base = False
                        if (
                            wanted.pr_identity
                            not in operation.temporary_bases_possibly_sent
                        ):
                            save(
                                temporary_bases_possibly_sent=(
                                    *operation.temporary_bases_possibly_sent,
                                    wanted.pr_identity,
                                )
                            )
                            send_base = True
                        if not send_base:
                            return Stopped(
                                "relink",
                                "possibly sent final base edit still reads as its old base",
                            )
                        try:
                            run_pr_base_edit(
                                github,
                                repository,
                                wanted.pr_identity,
                                bases[wanted.pr_identity],
                            )
                        except Error:
                            pass
                        source = _observe_topology_pr(
                            github,
                            repository,
                            frozen[wanted.pr_identity],
                            by_identity=restore,
                            expected_head_commit_id=wanted.desired_commit_id,
                            cwd=workspace,
                        )
                        if source.base_branch != bases[wanted.pr_identity]:
                            return Stopped(
                                "relink", "final base edit did not read back"
                            )
                        save(
                            temporary_bases_possibly_sent=tuple(
                                identity
                                for identity in operation.temporary_bases_possibly_sent
                                if identity != wanted.pr_identity
                            )
                        )
                if len(plan.desired.active) >= 2 and not operation.relink_possibly_sent:
                    save(relink_possibly_sent=True)
                    try:
                        run_topology_stack_link(github, repository, plan)
                    except Error:
                        pass
                sources, stack = observe_topology_projection(github, plan)
                error = _topology_projection_error(plan, sources, stack)
                if error is not None:
                    return Stopped("relink", error)
                save(phase=TopologyRepairPhase.VERIFYING)

            if operation.phase is TopologyRepairPhase.VERIFYING:
                for _ in range(2):
                    sources, stack = observe_topology_projection(github, plan)
                    error = _topology_projection_error(plan, sources, stack)
                    if error is not None:
                        return Stopped("verify", error)
                state_oid, state = read_state(workspace)
                if state_oid != plan.dependencies.state_blob_oid:
                    return Stopped("state", "private state changed before commit")
                final_json = state_to_json(_build_topology_final_state(plan, state))
                final_oid = _run(
                    [
                        "git",
                        f"--git-dir={git_common_dir(workspace)}",
                        "hash-object",
                        "-w",
                        "--stdin",
                    ],
                    stdin=final_json,
                ).strip()
                save(
                    phase=TopologyRepairPhase.COMMITTING,
                    final_state_json=final_json,
                    final_state_oid=final_oid,
                )

            if operation.phase is TopologyRepairPhase.COMMITTING:
                current = read_ref_oid(workspace, STATE_REF)
                if current == plan.dependencies.state_blob_oid:
                    sources, stack = observe_topology_projection(github, plan)
                    error = _topology_projection_error(plan, sources, stack)
                    if error is not None:
                        return Stopped("verify", error)
                    assert operation.final_state_json is not None
                    if (
                        cas_write_state(
                            workspace, current, parse_state(operation.final_state_json)
                        )
                        != operation.final_state_oid
                    ):
                        return Stopped(
                            "state", "committed state differs from frozen OID"
                        )
                elif current != operation.final_state_oid:
                    return Stopped(
                        "state", "private state is neither expected nor final"
                    )

                refs = tuple(item.ref for item in plan.temporary_bases)
                live = _topology_refs(plan, workspace, refs)
                for item, observed in zip(plan.temporary_bases, live, strict=True):
                    if observed.commit_id is None:
                        continue
                    if observed.commit_id != item.new_base_commit_id:
                        return Stopped(
                            "cleanup", "temporary base branch moved externally"
                        )
                    subprocess.run(
                        _delete_temporary_base_command(workspace, plan, item),
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    check = _topology_refs(plan, workspace, (item.ref,))
                    if check != (LiveRemoteRef(item.ref, None),):
                        return Stopped(
                            "cleanup", "temporary-base deletion was not verified"
                        )
                if _topology_refs(plan, workspace, refs) != tuple(
                    LiveRemoteRef(item.ref, None) for item in plan.temporary_bases
                ):
                    return Stopped("cleanup", "temporary-base cleanup is incomplete")
                cas_delete_operation(workspace, operation_oid)
                return TopologyRepairVerified(
                    tuple(item.pr_identity for item in plan.desired.active)
                )
        except Error as exc:
            return Stopped(operation.phase.value, str(exc))
        return Stopped("operation", "topology operation reached no terminal state")


def start_membership_repair(operation: MembershipRepair, workspace: str | Path) -> str:
    """Fence a frozen membership transaction before performing any effect."""
    _validate_membership_repair(operation)
    if operation.phase is not MembershipRepairPhase.PREPARING_BOOKMARKS:
        raise ValueError("a new membership repair must start before effects")
    with repository_lock(workspace):
        if read_ref_oid(workspace, STATE_REF) != operation.expected_state_oid:
            raise ConcurrentUpdate("private state changed before operation start")
        if read_ref_oid(workspace, OPERATION_REF) is not None:
            raise ConcurrentUpdate("another operation is already active")
        _local, readbacks = observe_publication_slots(workspace, operation)  # type: ignore[arg-type]
        for slot, readback in zip(operation.slots, readbacks, strict=True):
            if slot.bookmark_setup is BookmarkSetup.CREATE and readback.local_target is not None:
                raise ConcurrentUpdate(f"CREATE bookmark {slot.branch} is no longer absent")
            if slot.bookmark_setup is BookmarkSetup.KEEP and not _exact_target(readback.local_target, slot.commit_id):
                raise ConcurrentUpdate(f"KEEP bookmark {slot.branch} moved")
            if readback.live_commit_id is not None or not _absent_target(readback.remote_target):
                raise ConcurrentUpdate(f"publication destination {slot.branch} is no longer absent")
        refs = tuple(item.ref for item in operation.temporary_bases)
        if _topology_refs(_membership_carrier(operation).plan, workspace, refs) != tuple(
            LiveRemoteRef(ref, None) for ref in refs
        ):
            raise ConcurrentUpdate("a frozen temporary base branch is occupied")
        return cas_write_membership_repair(workspace, None, operation)


def _membership_plan(operation: MembershipRepair) -> TopologyPlan:
    """Make an in-memory adapter for topology leaf commands; never persist it."""
    slots = {slot.slot_id: slot for slot in operation.slots}
    source = {pr.identity: pr for pr in operation.source_prs}
    prs = list(operation.source_prs)
    active: list[DesiredExistingPR] = []
    metadata = {item.pr_identity: item for item in operation.metadata_updates}
    updates = {item.ref.full_name: item for item in operation.head_updates}
    for item in operation.desired:
        if isinstance(item, ExistingMembershipEntry):
            pr = source[item.pr_identity]
            update = updates.get(f"refs/heads/{pr.head_branch}")
            wanted = metadata.get(item.pr_identity)
            active.append(DesiredExistingPR(item.pr_identity, update.new_commit_id if update else pr.head_oid,
                                            wanted.title if wanted else pr.title, wanted.body if wanted else pr.body))
        else:
            slot = slots[item.slot_id]
            if slot.pr_identity is None:
                raise Error("new membership slot is not bound")
            pr = GitHubPullRequest(slot.pr_identity, "<unobserved>", PullRequestState.OPEN, True,
                                   operation.repository, slot.branch, slot.commit_id, slot.base_branch, "", False, False,
                                   slot.title, slot.body, None)
            prs.append(pr)
            active.append(DesiredExistingPR(slot.pr_identity, slot.commit_id, slot.title, slot.body))
    dependencies = TopologyDependencies(operation.effective_config, operation.push_url,
        operation.expected_state_oid, None, tuple(prs), operation.source_membership, (), ())
    detached = (
        ()
        if operation.source_tracked is None
        else tuple(identity for identity in operation.source_tracked.ordered_prs
                   if identity not in {item.pr_identity for item in active})
    )
    return TopologyPlan(operation.repository, operation.repository_name, operation.remote,
        DesiredStack(operation.repository, operation.base_branch, tuple(active)), detached,
        operation.head_updates, operation.temporary_bases, None, dependencies)


def _membership_projection(
    github: GitHubClient,
    operation: MembershipRepair,
    repository: GitHubRepository,
) -> tuple[tuple[GitHubPullRequest, ...], GitHubStack | None]:
    plan = _membership_plan(operation)
    sources = github.pull_requests(tuple(pr.identity for pr in plan.dependencies.prs))
    desired = tuple(item.pr_identity for item in plan.desired.active)
    ids = {
        item.stack.identity
        for item in sources
        if item.identity in desired and item.stack is not None
    }
    stack = (
        github.stack(repository, next(iter(ids)))
        if len(desired) >= 2 and len(ids) == 1
        else None
    )
    return sources, stack


def _membership_projection_error(operation: MembershipRepair,
                                 sources: Sequence[GitHubPullRequest],
                                 stack: GitHubStack | None) -> str | None:
    plan = _membership_plan(operation)
    error = _topology_projection_error(plan, sources, stack)
    if error is not None:
        return error
    omitted = (
        set()
        if operation.source_tracked is None
        else set(operation.source_tracked.ordered_prs)
        - {item.pr_identity for item in plan.desired.active}
    )
    frozen = {pr.identity: pr for pr in operation.source_prs}
    actual = {item.identity: item for item in sources}
    for identity in omitted:
        old, now = frozen[identity], actual[identity]
        if now.stack is not None or (now.head_oid, now.title, now.body, now.draft) != (
            old.head_oid, old.title, old.body, old.draft
        ):
            return f"omitted pull request #{old.number} changed or is not standalone"
    return None


def _membership_source_refs_error(operation: MembershipRepair, plan: TopologyPlan,
                                  workspace: str | Path) -> str | None:
    """Recheck the frozen old comparisons before dissolving their association."""
    refs: list[RemoteBranchRef] = []
    expected: list[str] = []
    for pr in operation.source_prs:
        refs.extend((RemoteBranchRef(operation.repository, f"refs/heads/{pr.head_branch}"),
                     RemoteBranchRef(operation.repository, f"refs/heads/{pr.base_branch}")))
        if pr.head_oid is None or pr.base_oid is None:
            return f"pull request #{pr.number} has incomplete commit observations"
        expected.extend((pr.head_oid, pr.base_oid))
    # Repeated predecessor refs are intentional; observe them once and compare by key.
    unique = tuple(dict.fromkeys(refs))
    observed = {item.ref: item.commit_id for item in _topology_refs(plan, workspace, unique)}
    for ref, oid in zip(refs, expected, strict=True):
        if observed.get(ref) != oid:
            return f"frozen live ref {ref.full_name} changed"
    return None


def _build_membership_final_state(operation: MembershipRepair, state: TrackedState) -> TrackedState:
    desired = tuple(item.pr_identity for item in _membership_plan(operation).desired.active)
    if operation.source_tracked is None:
        source_ids = {pr.identity for pr in operation.source_prs}
        if any(
            stack.repository == operation.repository
            and source_ids.intersection((*stack.ordered_prs, *stack.detached_prs))
            for stack in state.stacks
        ):
            raise Error("private state gained an overlapping standalone source record")
        detached: tuple[PullRequestId, ...] = ()
        stacks = state.stacks
    else:
        matches = tuple(item for item in state.stacks if item == operation.source_tracked)
        if len(matches) != 1:
            raise Error("private state no longer contains the exact source record")
        omitted = tuple(identity for identity in operation.source_tracked.ordered_prs if identity not in desired)
        detached = tuple(dict.fromkeys((*operation.source_tracked.detached_prs, *omitted)))
        stacks = tuple(item for item in state.stacks if item != operation.source_tracked)
    if desired:
        stacks += (TrackedStack(operation.repository, operation.base_branch, desired, detached),)
    publications, adoptions = state.last_published_heads, state.last_adopted_heads
    source = {pr.identity: pr for pr in operation.source_prs}
    receipts: list[LastPublishedHead] = []
    for update in operation.head_updates:
        identity = next(identity for identity, pr in source.items()
                        if update.ref.full_name == f"refs/heads/{pr.head_branch}")
        receipts.append(LastPublishedHead(identity, update.ref, update.new_commit_id))
    for slot in operation.slots:
        assert slot.pr_identity is not None
        receipts.append(LastPublishedHead(slot.pr_identity,
            RemoteBranchRef(operation.repository, f"refs/heads/{slot.branch}"), slot.commit_id))
    keys = {(item.pr, item.ref) for item in receipts}
    publications = tuple(item for item in publications if (item.pr, item.ref) not in keys) + tuple(receipts)
    adoptions = tuple(item for item in adoptions if (item.pr, item.ref) not in keys)
    return TrackedState(stacks, publications, adoptions)


def resume_membership_repair(
    workspace: str | Path, github: GitHubClient
) -> MembershipRepairResult:
    """Recover one mixed membership transaction entirely from its durable record."""
    with repository_lock(workspace):
        try:
            operation_oid, raw = read_operation(workspace)
            if not isinstance(raw, MembershipRepair):
                return Stopped("operation", "active operation is not a membership repair")
            operation = raw
            repository = _frozen_repository(
                github, operation.repository, operation.repository_name
            )
        except Error as exc:
            return Stopped("operation", str(exc))

        def save(**changes: object) -> None:
            nonlocal operation, operation_oid
            operation = replace(operation, **changes)
            operation_oid = cas_write_membership_repair(workspace, operation_oid, operation)

        try:
            if operation.phase is not MembershipRepairPhase.COMMITTING and read_ref_oid(workspace, STATE_REF) != operation.expected_state_oid:
                return Stopped("state", "private state changed while operation is active")
            if operation.phase is MembershipRepairPhase.PREPARING_BOOKMARKS:
                stopped = _prepare_publication_bookmarks(workspace, operation)  # type: ignore[arg-type]
                if stopped: return stopped
                save(phase=MembershipRepairPhase.PUBLISHING_NEW_REFS)
            if operation.phase is MembershipRepairPhase.PUBLISHING_NEW_REFS:
                stopped = _prepare_publication_tracking(workspace, operation)  # type: ignore[arg-type]
                if stopped: return stopped
                _local, readbacks = observe_publication_slots(workspace, operation)  # type: ignore[arg-type]
                actions = classify_publication_recovery(operation, readbacks)  # type: ignore[arg-type]
                if isinstance(actions, Stopped): return actions
                pending = tuple(slot for slot, action in zip(operation.slots, actions, strict=True)
                                if slot.phase is NewPRPhase.NOT_ATTEMPTED and action is PublicationRecoveryAction.PUSH)
                if pending:
                    save(slots=tuple(replace(slot, phase=NewPRPhase.PUBLICATION_POSSIBLY_SENT) if slot in pending else slot for slot in operation.slots))
                retries = tuple(slot for slot, action in zip(operation.slots, actions, strict=True) if action is PublicationRecoveryAction.PUSH)
                if retries: push_new_slot_refs(workspace, operation, retries)  # type: ignore[arg-type]
                _local, readbacks = observe_publication_slots(workspace, operation)  # type: ignore[arg-type]
                actions = classify_publication_recovery(operation, readbacks)  # type: ignore[arg-type]
                if isinstance(actions, Stopped): return actions
                for slot, action in zip(operation.slots, actions, strict=True):
                    if action is PublicationRecoveryAction.TRACK: track_publication_bookmark(workspace, operation, slot)  # type: ignore[arg-type]
                if PublicationRecoveryAction.TRACK in actions:
                    _local, readbacks = observe_publication_slots(workspace, operation)  # type: ignore[arg-type]
                    actions = classify_publication_recovery(operation, readbacks)  # type: ignore[arg-type]
                if isinstance(actions, Stopped) or any(item is not PublicationRecoveryAction.READY for item in actions):
                    return actions if isinstance(actions, Stopped) else Stopped("publish", "new refs are not exact and tracked")
                save(slots=tuple(replace(slot, phase=NewPRPhase.READY) for slot in operation.slots), phase=MembershipRepairPhase.CREATING_PRS)
            if operation.phase is MembershipRepairPhase.CREATING_PRS:
                for index, slot in enumerate(operation.slots):
                    if slot.phase is NewPRPhase.READY:
                        slot = replace(slot, phase=NewPRPhase.POSSIBLY_SENT); slots = list(operation.slots); slots[index] = slot; save(slots=tuple(slots))
                        returned: GitHubPullRequest | None = None
                        try:
                            created = github.create_pull_request(
                                repository,
                                head_branch=slot.branch,
                                base_branch=slot.base_branch,
                                title=slot.title,
                                body=slot.initial_body,
                                draft=True,
                            )
                            candidate = github.pull_requests((created,))[0]
                            if _slot_pr_matches(operation.repository, slot, candidate, initial=True):
                                returned = candidate
                        except Error:
                            pass
                        matches = (
                            (returned,)
                            if returned is not None
                            else tuple(
                                pr
                                for pr in github.find_pull_requests(
                                    operation.repository, head_branches=(slot.branch,)
                                )
                                if slot.marker in pr.body
                            )
                        )
                    elif slot.phase is NewPRPhase.POSSIBLY_SENT:
                        matches = tuple(
                            pr
                            for pr in github.find_pull_requests(
                                operation.repository, head_branches=(slot.branch,)
                            )
                            if slot.marker in pr.body
                        )
                    else: matches = ()
                    if slot.phase is NewPRPhase.POSSIBLY_SENT:
                        valid = tuple(pr for pr in matches if _slot_pr_matches(operation.repository, slot, pr, initial=True))
                        if len(valid) != 1: return Stopped("create-pr", "possibly sent; marker lookup did not find exactly one matching PR")
                        slot = replace(slot, phase=NewPRPhase.BOUND, pr_identity=valid[0].identity)
                        slots = list(operation.slots); slots[index] = slot; save(slots=tuple(slots))
                    if slot.phase is NewPRPhase.BOUND:
                        assert slot.pr_identity is not None
                        found = github.find_pull_requests(
                            operation.repository, head_branches=(slot.branch,)
                        )
                        bound = tuple(pr for pr in found if pr.identity == slot.pr_identity)
                        if len(bound) != 1:
                            return Stopped("metadata", "bound PR is not uniquely observable")
                        try:
                            github.update_pull_request(
                                repository,
                                slot.pr_identity,
                                title=slot.title,
                                body=slot.body,
                            )
                        except Error:
                            pass
                        found = github.find_pull_requests(
                            operation.repository, head_branches=(slot.branch,)
                        )
                        if len(tuple(pr for pr in found if pr.identity == slot.pr_identity and _slot_pr_matches(operation.repository, slot, pr, initial=False))) != 1:
                            return Stopped("metadata", "bound PR did not reach frozen metadata")
                        slots = list(operation.slots); slots[index] = replace(slot, phase=NewPRPhase.VERIFIED); save(slots=tuple(slots))
                save(phase=MembershipRepairPhase.UNSTACKING_SOURCE)
            plan = _membership_plan(operation)
            if operation.phase is MembershipRepairPhase.UNSTACKING_SOURCE:
                refs_error = _membership_source_refs_error(operation, plan, workspace)
                if refs_error: return Stopped("revalidate", refs_error)
                if isinstance(operation.source_membership, StandalonePullRequestSet):
                    sources = github.pull_requests(
                        tuple(pr.identity for pr in operation.source_prs)
                    )
                    stack = None
                else:
                    sources, stack = _observe_source_association(
                        github,
                        _membership_carrier(operation).plan,
                        repository,
                        allow_dissolved=True,
                        cwd=workspace,
                    )
                if isinstance(operation.source_membership, ServerStackMembership):
                    if stack is not None and not operation.source_unstack_possibly_sent:
                        save(source_unstack_possibly_sent=True)
                        try:
                            run_stack_unstack(github, repository, operation.source_membership)
                        except Error:
                            pass
                    if prove_stack_dissolved(github, repository, operation.source_membership.stack.identity) is not StackDissolution.ABSENT:
                        return Stopped("unstack", "old stack dissolution is unproven")
                    sources, _stack = _observe_source_association(
                        github,
                        _membership_carrier(operation).plan,
                        repository,
                        allow_dissolved=True,
                        cwd=workspace,
                    )
                if any(item.stack is not None for item in sources): return Stopped("unstack", "source stack still survives")
                save(phase=MembershipRepairPhase.TRANSITIONING_BASES)
            if operation.phase is MembershipRepairPhase.TRANSITIONING_BASES:
                all_sources, _stack = _membership_projection(github, operation, repository)
                if any(item.stack is not None for item in all_sources):
                    return Stopped("temporary-base", "a source or new PR is not standalone")
                refs = tuple(item.ref for item in operation.temporary_bases)
                values = tuple(item.commit_id for item in _topology_refs(plan, workspace, refs))
                old = tuple(item.old_base_commit_id for item in operation.temporary_bases)
                if values == (None,) * len(refs) and refs:
                    create_temporary_bases(workspace, operation.push_url, operation.temporary_bases)
                    values = tuple(item.commit_id for item in _topology_refs(plan, workspace, refs))
                if values != old: return Stopped("temporary-bases", "temporary-base readback is absent, mixed, or foreign")
                frozen = {pr.identity: pr for pr in operation.source_prs}
                for item in operation.temporary_bases:
                    source = _observe_topology_pr(github, repository, frozen[item.pr_identity], by_identity=True, cwd=workspace)
                    name = item.ref.full_name.removeprefix("refs/heads/")
                    if source.stack is not None: return Stopped("temporary-base", "a stack association reappeared")
                    if source.base_branch == name:
                        if item.pr_identity in operation.base_transitions_possibly_sent:
                            save(base_transitions_possibly_sent=tuple(identity for identity in operation.base_transitions_possibly_sent if identity != item.pr_identity))
                        continue
                    if source.base_branch != name:
                        if source.base_branch != frozen[item.pr_identity].base_branch: return Stopped("temporary-base", "PR base is foreign")
                        if item.pr_identity in operation.base_transitions_possibly_sent:
                            return Stopped("temporary-base", "possibly sent base edit still reads as its old base")
                        save(base_transitions_possibly_sent=(*operation.base_transitions_possibly_sent, item.pr_identity))
                        try:
                            run_pr_base_edit(github, repository, item.pr_identity, name)
                        except Error:
                            pass
                        if _observe_topology_pr(github, repository, frozen[item.pr_identity], by_identity=True, cwd=workspace).base_branch != name: return Stopped("temporary-base", "base edit did not read back")
                        save(base_transitions_possibly_sent=tuple(identity for identity in operation.base_transitions_possibly_sent if identity != item.pr_identity))
                save(phase=MembershipRepairPhase.PUBLISHING)
            if operation.phase is MembershipRepairPhase.PUBLISHING:
                refs = tuple(item.ref for item in operation.head_updates) + tuple(item.ref for item in operation.temporary_bases)
                old = tuple(item.expected_old_commit_id for item in operation.head_updates) + tuple(item.old_base_commit_id for item in operation.temporary_bases)
                final = tuple(item.new_commit_id for item in operation.head_updates) + tuple(item.new_base_commit_id for item in operation.temporary_bases)
                values = tuple(item.commit_id for item in _topology_refs(plan, workspace, refs))
                if values == old and refs:
                    save(publication_possibly_sent=tuple(item.ref for item in operation.head_updates))
                    move_temporary_bases(workspace, operation.push_url, operation.head_updates, operation.temporary_bases)
                    values = tuple(item.commit_id for item in _topology_refs(plan, workspace, refs))
                if values != final: return Stopped("publish", "transition readback is old, mixed, or foreign")
                save(phase=MembershipRepairPhase.RELINKING)
            if operation.phase is MembershipRepairPhase.RELINKING:
                bases = _topology_desired_bases(plan); frozen = {pr.identity: pr for pr in plan.dependencies.prs}
                metadata = {item.pr_identity: item for item in operation.metadata_updates}
                for wanted in plan.desired.active:
                    source = _observe_topology_pr(github, repository, frozen[wanted.pr_identity], by_identity=True, expected_head_commit_id=wanted.desired_commit_id, cwd=workspace)
                    if source.stack is not None: return Stopped("relink", "a stack association reappeared")
                    if source.base_branch == bases[wanted.pr_identity] and wanted.pr_identity in operation.base_transitions_possibly_sent:
                        save(base_transitions_possibly_sent=tuple(identity for identity in operation.base_transitions_possibly_sent if identity != wanted.pr_identity))
                    if source.base_branch != bases[wanted.pr_identity]:
                        if wanted.pr_identity in operation.base_transitions_possibly_sent:
                            return Stopped("relink", "possibly sent final base edit still reads as its old base")
                        save(base_transitions_possibly_sent=(*operation.base_transitions_possibly_sent, wanted.pr_identity))
                        try:
                            run_pr_base_edit(github, repository, wanted.pr_identity, bases[wanted.pr_identity])
                        except Error:
                            pass
                        verified = _observe_topology_pr(github, repository, frozen[wanted.pr_identity], by_identity=True, expected_head_commit_id=wanted.desired_commit_id, cwd=workspace)
                        if verified.base_branch != bases[wanted.pr_identity]: return Stopped("relink", "final base edit did not read back")
                        save(base_transitions_possibly_sent=tuple(identity for identity in operation.base_transitions_possibly_sent if identity != wanted.pr_identity))
                    update = metadata.get(wanted.pr_identity)
                    if update is not None and (source.title, source.body) != (update.title, update.body):
                        try:
                            github.update_pull_request(
                                repository,
                                update.pr_identity,
                                title=update.title,
                                body=update.body,
                            )
                        except Error:
                            pass
                if len(plan.desired.active) >= 2 and not operation.relink_possibly_sent:
                    save(relink_possibly_sent=True)
                    try:
                        run_topology_stack_link(github, repository, plan)
                    except Error:
                        pass
                sources, stack = _membership_projection(github, operation, repository)
                error = _membership_projection_error(operation, sources, stack)
                if error: return Stopped("relink", error)
                save(phase=MembershipRepairPhase.VERIFYING, resulting_stack=stack.identity if stack else None)
            if operation.phase is MembershipRepairPhase.VERIFYING:
                for _ in range(2):
                    sources, stack = _membership_projection(github, operation, repository)
                    error = _membership_projection_error(operation, sources, stack)
                    if error: return Stopped("verify", error)
                state_oid, state = read_state(workspace)
                if state_oid != operation.expected_state_oid: return Stopped("state", "private state changed before commit")
                final_json = state_to_json(_build_membership_final_state(operation, state))
                final_oid = _run(["git", f"--git-dir={git_common_dir(workspace)}", "hash-object", "-w", "--stdin"], stdin=final_json).strip()
                save(phase=MembershipRepairPhase.COMMITTING, final_state_json=final_json, final_state_oid=final_oid)
            if operation.phase is MembershipRepairPhase.COMMITTING:
                current = read_ref_oid(workspace, STATE_REF)
                if current == operation.expected_state_oid:
                    sources, stack = _membership_projection(github, operation, repository)
                    error = _membership_projection_error(operation, sources, stack)
                    if error: return Stopped("verify", error)
                    assert operation.final_state_json is not None
                    if cas_write_state(workspace, current, parse_state(operation.final_state_json)) != operation.final_state_oid: return Stopped("state", "committed state differs from frozen OID")
                elif current != operation.final_state_oid: return Stopped("state", "private state is neither expected nor final")
                # Refuse cleanup while any PR still names an operation-owned base.
                sources, _stack = _membership_projection(github, operation, repository)
                names = {item.ref.full_name.removeprefix("refs/heads/") for item in operation.temporary_bases}
                if any(item.base_branch in names for item in sources): return Stopped("cleanup", "a PR still uses a temporary base")
                for item, live in zip(operation.temporary_bases, _topology_refs(plan, workspace, tuple(x.ref for x in operation.temporary_bases)), strict=True):
                    if live.commit_id is None: continue
                    if live.commit_id != item.new_base_commit_id: return Stopped("cleanup", "temporary base branch moved externally")
                    subprocess.run(_delete_temporary_base_command(workspace, plan, item), text=True, capture_output=True, check=False)
                    if _topology_refs(plan, workspace, (item.ref,)) != (LiveRemoteRef(item.ref, None),): return Stopped("cleanup", "temporary-base deletion was not verified")
                cas_delete_operation(workspace, operation_oid)
                return MembershipRepairVerified(tuple(item.pr_identity for item in plan.desired.active))
        except Error as exc:
            return Stopped(operation.phase.value, str(exc))
        return Stopped("operation", "membership operation reached no terminal state")


def resume_operation(
    workspace: str | Path, github: GitHubClient
) -> FirstPublicationResult | TopologyRepairResult | MembershipRepairResult:
    try:
        _oid, operation = read_operation(workspace)
    except Error as exc:
        return Stopped("operation", str(exc))
    if isinstance(operation, TopologyRepair):
        return resume_topology_repair(workspace, github)
    if isinstance(operation, MembershipRepair):
        return resume_membership_repair(workspace, github)
    return resume_first_publication(workspace, github)


def observe_revision_boundaries(
    workspace: str | Path, revision: str, config_keys: Sequence[str]
) -> tuple[ObservedCommit, ...]:
    """Resolve an explicit revset in jj's ancestry order (oldest first)."""
    return observe_local(
        workspace,
        revision=revision,
        config_keys=config_keys,
        ancestry_order=True,
    ).commits


def _bounded_commit_revision(
    selected_revision: str,
    local: LocalObservation,
    state: TrackedState,
    comparisons: Sequence[tuple[str, str]] = (),
) -> str:
    """Cover selected work and exact persisted identities without scanning all commits."""
    exact_targets = (
        item.target.commit_id
        for item in (*local.local_bookmarks, *local.remote_bookmarks)
        if isinstance(item.target, CommitTarget)
    )
    receipt_targets = (
        item.verified_commit_id
        for item in (*state.last_published_heads, *state.last_adopted_heads)
    )
    terms = (
        f"({selected_revision})",
        *(f"{base}::{head}" for base, head in comparisons),
        *exact_targets,
        *receipt_targets,
    )
    return " | ".join(dict.fromkeys(terms))


def _managed_branch_names(state: TrackedState) -> tuple[str, ...]:
    names = [stack.base_branch for stack in state.stacks]
    names.extend(
        association.base_branch
        for stack in state.stacks
        for association in stack.detached_prs
    )
    names.extend(
        item.ref.full_name.removeprefix("refs/heads/")
        for item in (*state.last_published_heads, *state.last_adopted_heads)
    )
    return tuple(dict.fromkeys(names))


def observe_historical_pull_request_collisions(
    github: GitHubClient,
    repository: GitHubRepository,
    branch_names: Sequence[str],
    *,
    cwd: str | Path | None = None,
) -> tuple[GitHubPullRequest, ...]:
    """Read historical PRs using only the proposed head/base branch names."""
    branches = tuple(dict.fromkeys(branch_names))
    if not branches:
        return ()
    return github.find_pull_requests(
        repository.identity,
        head_branches=branches,
        base_branches=branches,
    )
    owner, separator, name = repository.name_with_owner.partition("/")
    if not separator or not owner or not name or "/" in name:
        raise ValueError("repository name must have owner/name form")
    definitions = ["$owner: String!", "$name: String!"]
    selections: list[str] = []
    variables: dict[str, object] = {"owner": owner, "name": name}
    fields = """nodes {
        id number state isDraft
        headRepository { id }
        headRefName headRefOid baseRefName baseRefOid
        autoMergeRequest { enabledAt }
        mergeQueueEntry { id }
        title body
        stack { id baseRefName }
      }
      pageInfo { hasNextPage }"""
    for index, branch in enumerate(branches):
        variable = f"branch{index}"
        definitions.append(f"${variable}: String!")
        variables[variable] = branch
        selections.extend(
            (
                f"head{index}: pullRequests(first: 100, states: [OPEN, CLOSED, MERGED], headRefName: ${variable}) {{ {fields} }}",
                f"base{index}: pullRequests(first: 100, states: [OPEN, CLOSED, MERGED], baseRefName: ${variable}) {{ {fields} }}",
            )
        )
    query = f"""query({', '.join(definitions)}) {{
  repository(owner: $owner, name: $name) {{
    id
    {chr(10).join(selections)}
  }}
}}"""
    response = _exact_record(
        _github_graphql(
            repository.identity.host,
            query,
            variables,
            cwd=cwd,
        ),
        {"data"},
        "GitHub historical collision response",
    )
    data = _exact_record(
        response["data"], {"repository"}, "historical collision data"
    )
    raw_repository = data["repository"]
    if raw_repository is None:
        raise IncompleteSource("GitHub repository is unavailable")
    aliases = {
        f"{kind}{index}"
        for index in range(len(branches))
        for kind in ("head", "base")
    }
    record = _exact_record(
        raw_repository, {"id", *aliases}, "historical collision repository"
    )
    if _text(record["id"], "historical collision repository ID") != repository.identity.node_id:
        raise SourceMismatch("push destination and historical PR repository differ")
    result: dict[PullRequestId, GitHubPullRequest] = {}
    for alias in sorted(aliases):
        connection = _exact_record(
            record[alias], {"nodes", "pageInfo"}, "historical collision connection"
        )
        page = _exact_record(
            connection["pageInfo"], {"hasNextPage"}, "historical collision page"
        )
        if page["hasNextPage"] is not False:
            raise IncompleteSource("filtered historical PR evidence exceeds one page")
        nodes = connection["nodes"]
        if not isinstance(nodes, list):
            raise MalformedSource("historical collision nodes must be an array")
        for node in nodes:
            if not isinstance(node, dict) or type(node.get("number")) is not int:
                raise MalformedSource("historical collision PR is malformed")
            pull_request = _parse_pull_request(node, repository, node["number"])
            previous = result.setdefault(pull_request.identity, pull_request)
            if previous != pull_request:
                raise SourceMismatch("historical collision observations disagree")
    return tuple(result.values())


def observe_open_pull_request_candidates(
    github: GitHubClient,
    repository: GitHubRepository,
    branch_names: Sequence[str],
    *,
    cwd: str | Path | None = None,
) -> tuple[GitHubPullRequest, ...]:
    """Read only open PRs whose repository-qualified heads are local candidates."""
    branches = tuple(dict.fromkeys(branch_names))
    if not branches:
        return ()
    return github.find_pull_requests(
        repository.identity,
        head_branches=branches,
        states=(PullRequestState.OPEN,),
    )
    owner, separator, name = repository.name_with_owner.partition("/")
    if not separator or not owner or not name or "/" in name:
        raise ValueError("repository name must have owner/name form")
    definitions = ["$owner: String!", "$name: String!"]
    selections: list[str] = []
    variables: dict[str, object] = {"owner": owner, "name": name}
    for index, branch in enumerate(branches):
        variable = f"head{index}"
        alias = f"candidate{index}"
        definitions.append(f"${variable}: String!")
        variables[variable] = branch
        selections.append(
            f"""{alias}: pullRequests(first: 2, states: [OPEN], headRefName: ${variable}) {{
      nodes {{
        id number state isDraft
        headRepository {{ id }}
        headRefName headRefOid baseRefName baseRefOid
        autoMergeRequest {{ enabledAt }}
        mergeQueueEntry {{ id }}
        title body
        stack {{ id baseRefName }}
      }}
      pageInfo {{ hasNextPage }}
    }}"""
        )
    query = f"""query({', '.join(definitions)}) {{
  repository(owner: $owner, name: $name) {{
    id
    {chr(10).join(selections)}
  }}
}}"""
    response = _exact_record(
        _github_graphql(
            repository.identity.host,
            query,
            variables,
            cwd=cwd,
        ),
        {"data"},
        "GitHub open PR candidate response",
    )
    data = _exact_record(response["data"], {"repository"}, "open PR candidate data")
    raw_repository = data["repository"]
    if raw_repository is None:
        raise IncompleteSource("GitHub repository is unavailable")
    aliases = {f"candidate{index}" for index in range(len(branches))}
    record = _exact_record(
        raw_repository, {"id", *aliases}, "open PR candidate repository"
    )
    if _text(record["id"], "candidate repository ID") != repository.identity.node_id:
        raise SourceMismatch("push destination and candidate PR repository differ")
    result: list[GitHubPullRequest] = []
    for index, branch in enumerate(branches):
        connection = _exact_record(
            record[f"candidate{index}"], {"nodes", "pageInfo"}, "candidate PR connection"
        )
        page = _exact_record(
            connection["pageInfo"], {"hasNextPage"}, "candidate PR page"
        )
        if page["hasNextPage"] is not False:
            raise SourceMismatch(f"multiple open pull requests use head branch {branch}")
        nodes = connection["nodes"]
        if not isinstance(nodes, list):
            raise MalformedSource("candidate pull request nodes must be an array")
        for node in nodes:
            if not isinstance(node, dict):
                raise MalformedSource("candidate pull request node must be an object")
            number = node.get("number")
            if type(number) is not int:
                raise MalformedSource("candidate pull request number must be an integer")
            source = _parse_pull_request(node, repository, number)
            if source.head_branch != branch:
                raise SourceMismatch("GitHub returned a different candidate head branch")
            result.append(source)
    if len({item.identity for item in result}) != len(result):
        raise SourceMismatch("candidate head branches resolve to overlapping pull requests")
    return tuple(result)


def resolve_first_publication_assignments(
    workspace: str | Path,
    github: GitHubClient,
    local: LocalObservation,
    repository: GitHubRepository,
    push_url: str,
    commit_ids: Sequence[str],
    protected_branch_names: Sequence[str],
) -> tuple[PublicationAssignment, ...] | Blocked:
    """Collect resolver-only naming evidence and return its exact frozen mapping."""
    try:
        template = _jj(
            workspace, None, "config", "get", "templates.git_push_bookmark"
        ).rstrip("\n")
        if not template:
            raise SourceMismatch("templates.git_push_bookmark is empty")
        template_results: list[PublicationAssignment] = []
        for commit_id in commit_ids:
            rendered = _jj(
                workspace,
                local.operation_id,
                "log",
                "--no-graph",
                "-r",
                commit_id,
                "-T",
                f'({template}) ++ "\\n"',
            ).splitlines()
            if len(rendered) != 1 or not rendered[0]:
                raise SourceMismatch(
                    "templates.git_push_bookmark must produce one nonempty line per revision"
                )
            template_results.append(PublicationAssignment(commit_id, rendered[0]))

        protected = set(protected_branch_names)
        candidate_names: list[str] = []
        for commit_id, generated in zip(commit_ids, template_results, strict=True):
            aliases = tuple(
                item.name
                for item in local.local_bookmarks
                if item.name not in protected
                and isinstance(item.target, CommitTarget)
                and item.target.commit_id == commit_id
            )
            candidate_names.extend(aliases or (generated.branch_name,))
        names = tuple(dict.fromkeys(candidate_names))
        destinations = observe_live_refs(
            push_url,
            repository.identity,
            tuple(f"refs/heads/{name}" for name in names),
            cwd=workspace,
        )
        historical = observe_historical_pull_request_collisions(
            github, repository, names, cwd=workspace
        )
    except Error as exc:
        return _block("publication-assignment-unavailable", "selection", str(exc))
    return resolve_publication_assignments(
        PublicationAssignmentInput(
            repository.identity,
            tuple(commit_ids),
            local,
            None,
            tuple(template_results),
            tuple(protected_branch_names),
            destinations,
            historical,
        )
    )


def _assign_pr_boundaries(
    local: LocalObservation,
    prs: Sequence[GitHubPullRequest],
    commits: Sequence[ObservedCommit],
) -> tuple[ExistingPRAssignment, ...] | Blocked:
    by_commit = {commit.commit_id: commit for commit in local.commits}
    assignments: list[ExistingPRAssignment] = []
    used: set[PullRequestId] = set()
    for commit in commits:
        matches: list[PullRequestId] = []
        for pr in prs:
            target = _one_bookmark_target(local, None, pr.head_branch)
            local_head = by_commit.get(target or "")
            if target == commit.commit_id or (
                local_head is not None and local_head.change_id == commit.change_id
            ):
                matches.append(pr.identity)
        if len(matches) != 1 or matches[0] in used:
            return _block(
                "mixed-or-unproven-selection",
                commit.commit_id,
                "every selected boundary must map uniquely to an existing PR by exact local bookmark/change identity",
            )
        used.add(matches[0])
        assignments.append(ExistingPRAssignment(matches[0], commit.commit_id))
    return tuple(assignments)


def classify_complete_membership_boundaries(
    local: LocalObservation,
    prs: Sequence[GitHubPullRequest],
    commits: Sequence[ObservedCommit],
) -> tuple[CompleteMembershipBoundary, ...] | Blocked:
    """Classify a complete oldest-first selection without positional inference.

    A PR identity is retained only when its local bookmark points at the exact
    selected commit or at a commit carrying the same jj change ID.  No-match is
    a genuinely new slot; ambiguity and reuse are errors rather than new PRs.
    """
    by_commit = {commit.commit_id: commit for commit in local.commits}
    if len({commit.commit_id for commit in commits}) != len(commits):
        return _block(
            "duplicate-selection", "stack", "selected boundaries must be unique"
        )
    for pr in prs:
        bookmarks = tuple(item for item in local.local_bookmarks if item.name == pr.head_branch)
        if len(bookmarks) != 1 or not isinstance(bookmarks[0].target, CommitTarget):
            return _block(
                "ambiguous-boundary",
                f"PR #{pr.number}",
                "every source PR head must have one exact local bookmark target",
            )
    result: list[CompleteMembershipBoundary] = []
    used: set[PullRequestId] = set()
    for commit in commits:
        matches: list[PullRequestId] = []
        for pr in prs:
            target = _one_bookmark_target(local, None, pr.head_branch)
            local_head = by_commit.get(target or "")
            if target == commit.commit_id or (
                local_head is not None and local_head.change_id == commit.change_id
            ):
                matches.append(pr.identity)
        if len(matches) > 1:
            return _block(
                "ambiguous-boundary",
                commit.commit_id,
                "selected boundary matches multiple existing PR identities",
            )
        if not matches:
            result.append(NewMembershipBoundary(commit.commit_id))
            continue
        identity = matches[0]
        if identity in used:
            return _block(
                "duplicate-membership",
                f"PR #{identity.number}",
                "an existing PR may occupy only one desired boundary",
            )
        used.add(identity)
        result.append(ExistingMembershipBoundary(identity, commit.commit_id))
    return tuple(result)


def selected_boundary_bookmark_names(
    local: LocalObservation, commits: Sequence[ObservedCommit]
) -> tuple[str, ...]:
    """Return local branch aliases that can identify existing selected PRs."""
    selected_ids = {commit.commit_id for commit in commits}
    selected_changes = {commit.change_id for commit in commits}
    by_commit = {commit.commit_id: commit for commit in local.commits}
    return tuple(
        dict.fromkeys(
            bookmark.name
            for bookmark in local.local_bookmarks
            if isinstance(bookmark.target, CommitTarget)
            and (
                bookmark.target.commit_id in selected_ids
                or (
                    bookmark.target.commit_id in by_commit
                    and by_commit[bookmark.target.commit_id].change_id in selected_changes
                )
            )
        )
    )


def resolve_standalone_membership_boundaries(
    local: LocalObservation,
    repository: GitHubRepositoryId,
    selected_pr: PullRequestId,
    sources: Sequence[GitHubPullRequest],
    commits: Sequence[ObservedCommit],
) -> tuple[
    tuple[CompleteMembershipBoundary, ...],
    tuple[GitHubPullRequest, ...],
    str,
] | Blocked:
    """Resolve desired boundaries and prove the old standalone source chain."""
    if len({commit.commit_id for commit in commits}) != len(commits):
        return _block("duplicate-selection", "stack", "selected boundaries must be unique")
    by_commit = {commit.commit_id: commit for commit in local.commits}
    used: set[PullRequestId] = set()
    resolved: list[CompleteMembershipBoundary] = []
    matched_sources: list[GitHubPullRequest] = []
    for commit in commits:
        matches: list[GitHubPullRequest] = []
        for source in sources:
            pr = source
            bookmarks = tuple(
                item for item in local.local_bookmarks if item.name == pr.head_branch
            )
            if len(bookmarks) != 1 or not isinstance(bookmarks[0].target, CommitTarget):
                continue
            target = bookmarks[0].target.commit_id
            local_head = by_commit.get(target)
            if target == commit.commit_id or (
                local_head is not None and local_head.change_id == commit.change_id
            ):
                matches.append(source)
        if len(matches) > 1:
            return _block(
                "ambiguous-boundary",
                commit.commit_id,
                "selected boundary matches multiple open PR identities",
            )
        if not matches:
            resolved.append(NewMembershipBoundary(commit.commit_id))
            continue
        source = matches[0]
        if source.identity in used:
            return _block(
                "duplicate-membership",
                f"PR #{source.number}",
                "an existing PR may occupy only one desired boundary",
            )
        used.add(source.identity)
        matched_sources.append(source)
        resolved.append(ExistingMembershipBoundary(source.identity, commit.commit_id))
    if selected_pr not in used:
        return _block(
            "incomplete-observation",
            f"PR #{selected_pr.number}",
            "the selected PR was not uniquely resolved by the selected revisions",
        )
    if any(
        source.identity.repository != repository
        or source.head_repository != repository
        for source in matched_sources
    ):
        return _block(
            "foreign-desired",
            "stack",
            "every resolved PR must have a repository-local head",
        )
    if any(
        source.stack is not None
        for source in matched_sources
    ):
        return _block(
            "mixed-stack-membership",
            "stack",
            "resolved PRs must all be standalone before they can be linked together",
        )
    by_head: dict[str, GitHubPullRequest] = {}
    for source in matched_sources:
        if source.head_branch in by_head:
            return _block(
                "ambiguous-source-graph",
                source.head_branch,
                "multiple source PRs claim one head branch",
            )
        by_head[source.head_branch] = source
    roots: list[GitHubPullRequest] = []
    children: dict[PullRequestId, list[GitHubPullRequest]] = {}
    for source in matched_sources:
        parent = by_head.get(source.base_branch)
        if parent is None:
            roots.append(source)
            continue
        if source.base_oid != parent.head_oid:
            return _block(
                "invalid-source-graph",
                f"PR #{source.number}",
                "an internal base name and commit do not identify the same predecessor",
            )
        children.setdefault(parent.identity, []).append(source)
    branched = next(
        (identity for identity, values in children.items() if len(values) != 1), None
    )
    if branched is not None:
        return _block(
            "branched-source-graph",
            f"PR #{branched.number}",
            "the standalone source graph is not a nonbranching chain",
        )
    if len(roots) != 1:
        return _block(
            "multiple-root-source-graph",
            "stack",
            "the standalone source graph must have exactly one external ultimate base",
        )
    ordered: list[GitHubPullRequest] = []
    current: GitHubPullRequest | None = roots[0]
    while current is not None and current not in ordered:
        ordered.append(current)
        descendants = children.get(current.identity, [])
        current = descendants[0] if descendants else None
    if len(ordered) != len(matched_sources):
        return _block(
            "disconnected-source-graph",
            "stack",
            "the standalone source graph is disconnected or cyclic",
        )
    return tuple(resolved), tuple(ordered), roots[0].base_branch


def prepare_membership_repair(
    workspace: str | Path,
    snapshot: Snapshot,
    repository: GitHubRepository,
    remote: str,
    boundaries: Sequence[CompleteMembershipBoundary],
    *,
    standalone_sources: Sequence[GitHubPullRequest] | None = None,
    standalone_base_branch: str | None = None,
) -> MembershipRepair | Blocked:
    """Observe and freeze a mixed complete-membership transaction; never mutate."""
    boundaries = tuple(boundaries)
    standalone_sources = (
        None if standalone_sources is None else tuple(standalone_sources)
    )
    source_prs = (
        snapshot.pull_requests
        if standalone_sources is None
        else tuple(standalone_sources)
    )
    source_ids = tuple(pr.identity for pr in source_prs)
    source_membership: MembershipRepairSource = (
        snapshot.membership
        if standalone_sources is None
        else StandalonePullRequestSet(source_ids)
    )
    membership_ids = (
        (snapshot.membership.pr,)
        if isinstance(snapshot.membership, StandalonePullRequest)
        else snapshot.membership.ordered_prs
    ) if standalone_sources is None else source_ids
    if snapshot.tool_state.operation_blob_oid is not None:
        return _block("operation-fenced", OPERATION_REF, "a pending operation fences planning")
    if (not boundaries or source_ids != membership_ids or len(set(source_ids)) != len(source_ids)
            or any(pr.state is not PullRequestState.OPEN for pr in source_prs)):
        return _block("incomplete-membership", "stack", "repair requires one exact fully-open source membership")
    if any(pr.head_repository != snapshot.repository or pr.identity.repository != snapshot.repository for pr in source_prs):
        return _block("nonlocal-pr", "stack", "repair requires repository-local PRs")
    if any(pr.auto_merge_enabled or pr.in_merge_queue for pr in source_prs):
        return _block("active-automation", "stack", "automation blocks membership repair")
    overlapping = tuple(stack for stack in snapshot.tool_state.state.stacks if stack.repository == snapshot.repository and set(source_ids).intersection((*stack.ordered_prs, *stack.detached_prs)))
    if standalone_sources is None:
        exact = tuple(stack for stack in overlapping if stack.base_branch == (source_prs[0].base_branch if isinstance(snapshot.membership, StandalonePullRequest) else snapshot.membership.base_branch) and stack.ordered_prs == source_ids)
        if len(exact) != 1 or len(overlapping) != 1:
            return _block("tracked-source-mismatch", "stack", "source must have one exact, nonoverlapping tracked record")
        tracked: TrackedStack | None = exact[0]
        base_branch = tracked.base_branch
    else:
        if overlapping:
            return _block(
                "tracked-source-overlap",
                "stack",
                "explicit standalone PRs overlap existing tracked state",
            )
        tracked = None
        if standalone_base_branch is None:
            return _block(
                "incomplete-source-graph",
                "stack",
                "standalone repair requires one frozen ultimate base",
            )
        base_branch = standalone_base_branch
    semantic = [[snapshot.repository.host, snapshot.repository.node_id], [item.number for item in source_ids], base_branch,
                [["existing", item.pr_identity.number, item.commit_id] if isinstance(item, ExistingMembershipBoundary) else ["new", item.commit_id] for item in boundaries]]
    digest = hashlib.sha256(json.dumps(semantic, separators=(",", ":")).encode()).hexdigest()[:24]
    new_boundaries = tuple(item for item in boundaries if isinstance(item, NewMembershipBoundary))
    protected = (base_branch, *(pr.head_branch for pr in source_prs), *_managed_branch_names(snapshot.tool_state.state))
    publication = (
        resolve_first_publication_assignments(
            workspace,
            snapshot.local,
            repository,
            snapshot.push_url,
            tuple(item.commit_id for item in new_boundaries),
            protected,
        )
        if new_boundaries
        else ()
    )
    if isinstance(publication, Blocked):
        return publication
    publication_by_commit = {item.commit_id: item.branch_name for item in publication}
    refs = {item.ref: item for item in snapshot.live_refs}
    base = refs.get(RemoteBranchRef(snapshot.repository, f"refs/heads/{base_branch}"))
    if base is None or base.commit_id is None:
        return _block("base-disagrees", base_branch, "target branch was not authoritatively observed")
    by_pr = {pr.identity: pr for pr in source_prs}
    by_commit = {commit.commit_id: commit for commit in snapshot.local.commits}
    # Preserve every old comparison before constructing the desired map.
    for pr in source_prs:
        head = refs.get(RemoteBranchRef(snapshot.repository, f"refs/heads/{pr.head_branch}"))
        literal = refs.get(RemoteBranchRef(snapshot.repository, f"refs/heads/{pr.base_branch}"))
        moving_external_base = (
            standalone_sources is not None and pr.identity == source_prs[0].identity
        )
        if (head is None or head.commit_id != pr.head_oid or literal is None
                or (not moving_external_base
                    and literal.commit_id != pr.base_oid)
                or pr.base_oid == pr.head_oid
                or (standalone_sources is None and not _comparison_is_nonempty_linear_and_conflict_free(
                    snapshot.local.commits,
                    pr.base_oid,
                    pr.head_oid,
                ))):
            return _block("invalid-old-comparison", f"PR #{pr.number}", "old comparison is not exact and nonempty")
    desired: list[DesiredMembershipEntry] = []
    slots: list[NewPRSlot] = []
    updates: list[PlannedHeadUpdate] = []
    metadata_updates: list[PRMetadataUpdate] = []
    temporary_specs: list[tuple[PullRequestId, str, str]] = []
    predecessor_branch, predecessor_commit = base_branch, base.commit_id
    new_ordinal = 0
    for boundary in boundaries:
        commit = by_commit.get(boundary.commit_id)
        if (commit is None or commit.has_conflicts or boundary.commit_id == predecessor_commit
                or ((standalone_sources is None or desired)
                    and not _comparison_is_nonempty_linear_and_conflict_free(
                        snapshot.local.commits, predecessor_commit, boundary.commit_id
                    ))):
            return _block("invalid-desired-comparison", boundary.commit_id, "desired segment is absent, nonlinear, empty, or conflicted")
        metadata = _metadata(commit.description)
        if metadata is None:
            return _block("title-missing", boundary.commit_id, "selected revision has no title")
        if isinstance(boundary, ExistingMembershipBoundary):
            pr = by_pr.get(boundary.pr_identity)
            if pr is None:
                return _block("foreign-desired", f"PR #{boundary.pr_identity.number}", "desired PR is not in source")
            desired.append(ExistingMembershipEntry(pr.identity))
            wanted = DesiredExistingPR(pr.identity, boundary.commit_id, *metadata)
            head = refs[RemoteBranchRef(snapshot.repository, f"refs/heads/{pr.head_branch}")]
            planned = _plan_head_update(
                snapshot.local.commits, snapshot.tool_state.state, wanted, head
            )
            if isinstance(planned, Blocked): return planned
            updates.extend(planned); metadata_updates.extend(_metadata_updates(pr, wanted))
            assert pr.base_oid is not None
            temporary_specs.append((pr.identity, pr.base_oid, predecessor_commit))
            predecessor_branch = pr.head_branch
        else:
            new_ordinal += 1
            slot_id = f"membership-{digest}-{new_ordinal}"
            branch = publication_by_commit[boundary.commit_id]
            matches = tuple(item for item in snapshot.local.local_bookmarks if item.name == branch)
            if not matches: setup = BookmarkSetup.CREATE
            elif len(matches) == 1 and isinstance(matches[0].target, CommitTarget) and matches[0].target.commit_id == boundary.commit_id: setup = BookmarkSetup.KEEP
            else: return _block("bookmark-setup-changed", branch, "publication bookmark is not absent or exact")
            desired.append(NewMembershipEntry(slot_id))
            slots.append(NewPRSlot(slot_id, branch, boundary.commit_id, setup, predecessor_branch, *metadata))
            predecessor_branch = branch
        predecessor_commit = boundary.commit_id
    temporary_refs = tuple(RemoteBranchRef(snapshot.repository, f"refs/heads/jj-stack/temporary-bases/{digest}/{index}") for index in range(1, len(temporary_specs) + 1))
    absences = (() if not temporary_refs else observe_live_refs(
        snapshot.push_url, snapshot.repository,
        tuple(item.full_name for item in temporary_refs), cwd=workspace
    ))
    if tuple(item.ref for item in absences) != temporary_refs or any(item.commit_id is not None for item in absences):
        return _block("temporary-base-occupied", "stack", "temporary base absence observations are incomplete or occupied")
    temporary = tuple(PlannedTemporaryBase(identity, absence.ref, old, new) for (identity, old, new), absence in zip(temporary_specs, absences, strict=True))
    operation = MembershipRepair(snapshot.repository, repository.name_with_owner, snapshot.push_url, remote,
        snapshot.local.effective_config, snapshot.local.workspace_targets, base_branch, snapshot.tool_state.state_blob_oid,
        source_membership, source_prs, tracked, tuple(desired), tuple(slots), tuple(updates), temporary, tuple(metadata_updates))
    try: _validate_membership_repair(operation)
    except ValueError as exc: return _block("invalid-membership-repair", "stack", str(exc))
    return operation


def _desired_from_assignments(
    repository: GitHubRepositoryId,
    base_branch: str,
    local: LocalObservation,
    prs: Sequence[GitHubPullRequest],
    assignments: Sequence[ExistingPRAssignment],
) -> DesiredStack | Blocked:
    by_pr = {pr.identity: pr for pr in prs}
    by_commit = {commit.commit_id: commit for commit in local.commits}
    desired: list[DesiredExistingPR] = []
    for assignment in assignments:
        pr = by_pr.get(assignment.pr_identity)
        commit = by_commit.get(assignment.commit_id)
        if pr is None or commit is None or commit.has_conflicts:
            return _block(
                "commit-unobserved",
                assignment.commit_id,
                "assigned tracked PR or local commit is unavailable",
            )
        metadata = _metadata(commit.description)
        if metadata is None:
            return _block("title-missing", assignment.commit_id, "selected revision has no title")
        desired.append(DesiredExistingPR(pr.identity, commit.commit_id, *metadata))
    return DesiredStack(repository, base_branch, tuple(desired))


def _snapshot_matches_tracked_topology(snapshot: Snapshot, tracked: TrackedStack) -> bool:
    if isinstance(snapshot.membership, StandalonePullRequest):
        membership = (snapshot.membership.pr,)
        base = snapshot.pull_requests[0].base_branch
    else:
        membership = snapshot.membership.ordered_prs
        base = snapshot.membership.base_branch
    if membership != tracked.ordered_prs or base != tracked.base_branch:
        return False
    previous = tracked.base_branch
    for pr in snapshot.pull_requests:
        if pr.base_branch != previous:
            return False
        previous = pr.head_branch
    return True


def plan_explicit_tracked_restore(
    workspace: str | Path,
    github: GitHubClient,
    intent: ExistingIntent,
    *,
    snapshot: Snapshot,
    remote: str,
    tracked: TrackedStack,
) -> TopologyPlanResult:
    repository = github.resolve_repository(snapshot.push_url)
    prs = github.pull_requests(tracked.ordered_prs)
    if intent.revision is None:
        by_commit = {commit.commit_id: commit for commit in snapshot.local.commits}
        boundaries: list[ObservedCommit] = []
        for pr in prs:
            target = _one_bookmark_target(snapshot.local, None, pr.head_branch)
            commit = by_commit.get(target or "")
            if commit is None:
                return _block(
                    "tracked-representation-missing",
                    f"PR #{pr.number}",
                    "every tracked PR requires one exact local boundary",
                )
            boundaries.append(commit)
    else:
        boundaries = list(
            observe_commits(
                workspace,
                snapshot.local.operation_id,
                intent.revision,
                ancestry_order=True,
            )
        )
    assignments = _assign_pr_boundaries(snapshot.local, prs, boundaries)
    if isinstance(assignments, Blocked):
        return assignments
    if tuple(item.pr_identity for item in assignments) != tracked.ordered_prs:
        return _block(
            "restore-order-mismatch",
            "stack",
            "explicit local boundaries must exactly preserve tracked membership and order",
        )
    desired = _desired_from_assignments(
        tracked.repository,
        tracked.base_branch,
        snapshot.local,
        prs,
        assignments,
    )
    if isinstance(desired, Blocked):
        return desired
    observation = observe_tracked_restore(
        workspace,
        github,
        local=snapshot.local,
        tool_state=snapshot.tool_state,
        repository=repository,
        push_url=snapshot.push_url,
        remote=remote,
        tracked=tracked,
        desired=desired,
        pull_requests=prs,
    )
    source = prepare_tracked_restore_planning(observation)
    if isinstance(source, Blocked):
        return source
    absences = observe_live_refs(
        snapshot.push_url,
        tracked.repository,
        tuple(ref.full_name for ref in source.temporary_base_refs),
        cwd=workspace,
    )
    return plan_tracked_restore(complete_tracked_restore_planning_input(source, absences))


def _implicit_existing_boundaries(snapshot: Snapshot) -> tuple[ObservedCommit, ...] | Blocked:
    by_commit = {commit.commit_id: commit for commit in snapshot.local.commits}
    result: list[ObservedCommit] = []
    for pr in snapshot.pull_requests:
        if pr.state is not PullRequestState.OPEN:
            continue
        target = _one_bookmark_target(snapshot.local, None, pr.head_branch)
        commit = by_commit.get(target or "")
        if commit is None:
            return _block(
                "tracked-representation-missing",
                f"PR #{pr.number}",
                "implicit intent requires every active PR to have one exact local boundary",
            )
        result.append(commit)
    return tuple(result)


def plan_explicit_existing(
    workspace: str | Path,
    github: GitHubClient,
    intent: ExistingIntent,
    *,
    requested_remote: str | None = None,
    config_keys: Sequence[str] = ("git.push",),
) -> ExplicitPlan:
    """Observe, resolve, and classify one explicitly selected existing stack."""
    if intent.pr_number <= 0:
        return _block("invalid-pr", str(intent.pr_number), "PR number must be positive")
    local = observe_local(
        workspace,
        revision=intent.revision or "all()",
        config_keys=config_keys,
        ancestry_order=intent.revision is not None,
    )
    remote = resolve_remote(local, requested_remote)
    snapshot = observe_snapshot(
        workspace,
        github,
        revision=intent.revision or "all()",
        config_keys=config_keys,
        remote=remote,
        selected_pr_number=intent.pr_number,
        local=local,
    )
    selected_identity = (
        snapshot.membership.pr
        if isinstance(snapshot.membership, StandalonePullRequest)
        else snapshot.membership.selected_pr
    )
    if intent.revision is not None and not isinstance(
        snapshot.membership, StandalonePullRequest
    ):
        focused_commits = observe_commits(
            workspace,
            local.operation_id,
            _bounded_commit_revision(
                intent.revision,
                local,
                snapshot.tool_state.state,
                tuple(
                    (pr.base_oid, pr.head_oid)
                    for pr in snapshot.pull_requests
                ),
            ),
        )
        snapshot = replace(snapshot, local=replace(local, commits=focused_commits))
    tracked_candidates = tuple(
        stack
        for stack in snapshot.tool_state.state.stacks
        if stack.repository == snapshot.repository and selected_identity in stack.ordered_prs
    )
    if len(tracked_candidates) > 1:
        return _block(
            "tracked-overlap",
            f"PR #{selected_identity.number}",
            "selected PR belongs to multiple tracked active stacks",
        )
    if tracked_candidates and not _snapshot_matches_tracked_topology(
        snapshot, tracked_candidates[0]
    ):
        return plan_explicit_tracked_restore(
            workspace,
            github,
            intent,
            snapshot=snapshot,
            remote=remote,
            tracked=tracked_candidates[0],
        )

    open_prs = tuple(pr for pr in snapshot.pull_requests if pr.state is PullRequestState.OPEN)
    if intent.revision is None:
        boundaries = _implicit_existing_boundaries(snapshot)
        if isinstance(boundaries, Blocked):
            return boundaries
        assignments = _assign_pr_boundaries(snapshot.local, snapshot.pull_requests, boundaries)
        if isinstance(assignments, Blocked):
            return assignments
        selection: StackSelection | Blocked = StackSelection(
            snapshot.pull_requests[0].base_branch
            if isinstance(snapshot.membership, StandalonePullRequest)
            else snapshot.membership.base_branch,
            assignments,
        )
    else:
        boundaries = local.commits
        standalone_sources: tuple[GitHubPullRequest, ...] | None = None
        standalone_repository: GitHubRepository | None = None
        if isinstance(snapshot.membership, StandalonePullRequest):
            standalone_repository = github.resolve_repository(snapshot.push_url)
            resolved = resolve_standalone_membership_boundaries(
                snapshot.local,
                snapshot.repository,
                selected_identity,
                observe_open_pull_request_candidates(
                    github,
                    standalone_repository,
                    selected_boundary_bookmark_names(snapshot.local, boundaries),
                    cwd=workspace,
                ),
                boundaries,
            )
            if isinstance(resolved, Blocked):
                return resolved
            classified, standalone_sources, standalone_base_branch = resolved
            focused_commits = observe_commits(
                workspace,
                local.operation_id,
                _bounded_commit_revision(
                    intent.revision,
                    local,
                    snapshot.tool_state.state,
                    (
                        (
                            standalone_sources[0].base_oid,
                            standalone_sources[-1].head_oid,
                        ),
                    ),
                ),
            )
            snapshot = replace(snapshot, local=replace(local, commits=focused_commits))
        else:
            classified = classify_complete_membership_boundaries(
                snapshot.local, snapshot.pull_requests, boundaries
            )
        if isinstance(classified, Blocked):
            return classified
        classified_existing = tuple(
            item.pr_identity
            for item in classified
            if isinstance(item, ExistingMembershipBoundary)
        )
        has_new = any(isinstance(item, NewMembershipBoundary) for item in classified)
        source_ids = tuple(pr.identity for pr in open_prs)
        if standalone_sources is not None and (
            len(standalone_sources) > 1 or has_new
        ):
            assert standalone_repository is not None
            source_prs = tuple(standalone_sources)
            ref_names = tuple(
                dict.fromkeys(
                    (
                        f"refs/heads/{standalone_base_branch}",
                        *(f"refs/heads/{pr.head_branch}" for pr in source_prs),
                        *(f"refs/heads/{pr.base_branch}" for pr in source_prs),
                    )
                )
            )
            standalone_snapshot = replace(
                snapshot,
                pull_requests=source_prs,
                live_refs=observe_live_refs(
                    snapshot.push_url,
                    snapshot.repository,
                    ref_names,
                    cwd=workspace,
                ),
            )
            return prepare_membership_repair(
                workspace,
                standalone_snapshot,
                standalone_repository,
                remote,
                classified,
                standalone_sources=standalone_sources,
                standalone_base_branch=standalone_base_branch,
            )
        simple_append = (
            isinstance(snapshot.membership, ServerStackMembership)
            and tuple(
                item.pr_identity
                for item in classified[: len(source_ids)]
                if isinstance(item, ExistingMembershipBoundary)
            ) == source_ids
            and all(
                isinstance(item, NewMembershipBoundary)
                for item in classified[len(source_ids) :]
            )
            and len(classified) > len(source_ids)
        )
        if (has_new and not simple_append) or not classified_existing:
            repository = github.resolve_repository(snapshot.push_url)
            return prepare_membership_repair(
                workspace, snapshot, repository, remote, classified
            )
        existing_count = 0
        for pr, commit in zip(open_prs, boundaries):
            if _one_bookmark_target(snapshot.local, None, pr.head_branch) != commit.commit_id:
                break
            existing_count += 1
        if existing_count == len(open_prs) and len(boundaries) > existing_count:
            if not isinstance(snapshot.membership, ServerStackMembership) or len(open_prs) != len(
                snapshot.pull_requests
            ):
                return _block(
                    "append-unsupported",
                    "stack",
                    "append requires unchanged membership in one fully-open stack",
                )
            repository = github.resolve_repository(snapshot.push_url)
            commits = tuple(item.commit_id for item in boundaries[existing_count:])
            protected = (
                snapshot.membership.base_branch,
                *(pr.head_branch for pr in snapshot.pull_requests),
                *_managed_branch_names(snapshot.tool_state.state),
            )
            publication = resolve_first_publication_assignments(
                workspace,
                github,
                snapshot.local,
                repository,
                snapshot.push_url,
                commits,
                protected,
            )
            if isinstance(publication, Blocked):
                return publication
            return prepare_multi_first_publication(
                workspace,
                snapshot.local,
                snapshot.tool_state,
                repository,
                snapshot.push_url,
                remote,
                snapshot.membership.base_branch,
                publication,
                existing_stack=GitHubStack(
                    snapshot.membership.stack.identity,
                    snapshot.membership.stack.node_id,
                    snapshot.membership.base_branch,
                    snapshot.membership.ordered_prs,
                ),
                existing_pull_requests=snapshot.pull_requests,
            )
        assignments = _assign_pr_boundaries(snapshot.local, snapshot.pull_requests, boundaries)
        if isinstance(assignments, Blocked):
            return assignments
        selection = StackSelection(
            snapshot.pull_requests[0].base_branch
            if isinstance(snapshot.membership, StandalonePullRequest)
            else snapshot.membership.base_branch,
            assignments,
        )

    desired = derive_desired(snapshot, selection)
    if isinstance(desired, Blocked):
        return desired
    source_open = tuple(pr.identity for pr in open_prs)
    selected = tuple(item.pr_identity for item in desired.active)
    if selected == source_open:
        local_boundaries = tuple(
            _one_bookmark_target(snapshot.local, None, pr.head_branch) for pr in open_prs
        )
        desired_boundaries = tuple(item.desired_commit_id for item in desired.active)
        if (
            desired_boundaries == local_boundaries
            and snapshot.tool_state.operation_blob_oid is None
            and all(not pr.auto_merge_enabled and not pr.in_merge_queue for pr in open_prs)
        ):
            adoption = inspect_remote_restack(snapshot, remote)
            if isinstance(adoption, RemoteRestackAdoption):
                return adoption
        return plan_sync(snapshot, desired)
    repository = github.resolve_repository(snapshot.push_url)
    source = prepare_topology_planning(snapshot, repository, remote, desired)
    if isinstance(source, Blocked):
        return source
    absences = observe_live_refs(
        snapshot.push_url,
        snapshot.repository,
        tuple(ref.full_name for ref in source.temporary_base_refs),
        cwd=workspace,
    )
    return plan_topology(complete_topology_planning_input(source, absences))


def plan_explicit_new(
    workspace: str | Path,
    github: GitHubClient,
    intent: NewIntent,
    *,
    requested_remote: str | None = None,
    config_keys: Sequence[str] = ("git.push",),
) -> FirstPublication | Blocked:
    """Observe and freeze first publication; this never consults PR selection."""
    local = observe_local(
        workspace,
        revision=intent.revision,
        config_keys=config_keys,
        ancestry_order=True,
    )
    selected = local.commits
    if not selected:
        return _block(
            "empty-publication", intent.revision, "new publication requires a nonempty revset"
        )
    remote = resolve_remote(local, requested_remote)
    push_url = resolve_push_url(local, remote)
    repository = github.resolve_repository(push_url)
    base = intent.base_branch or repository.default_branch
    commits = tuple(item.commit_id for item in selected)
    tool_state = observe_tool_state(workspace)
    local = replace(
        local,
        commits=observe_commits(
            workspace,
            local.operation_id,
            _bounded_commit_revision(intent.revision, local, tool_state.state),
        ),
    )
    protected = (base, *_managed_branch_names(tool_state.state))
    assignments = resolve_first_publication_assignments(
        workspace, github, local, repository, push_url, commits, protected
    )
    if isinstance(assignments, Blocked):
        return assignments
    if len(assignments) == 1:
        return prepare_standalone_first_publication(
            workspace,
            local,
            tool_state,
            repository,
            push_url,
            remote,
            base,
            assignments[0],
        )
    return prepare_multi_first_publication(
        workspace,
        local,
        tool_state,
        repository,
        push_url,
        remote,
        base,
        assignments,
    )


def plan_explicit(
    workspace: str | Path,
    github: GitHubClient,
    intent: ExplicitIntent,
    *,
    requested_remote: str | None = None,
) -> ExplicitPlan:
    if isinstance(intent, ExistingIntent):
        return plan_explicit_existing(
            workspace, github, intent, requested_remote=requested_remote
        )
    if isinstance(intent, NewIntent):
        return plan_explicit_new(
            workspace, github, intent, requested_remote=requested_remote
        )
    _oid, operation = read_operation(workspace)
    return PersistedOperation(operation)


def render_explicit(value: ExplicitPlan | ExplicitResult) -> str:
    if isinstance(value, (Blocked, NoOp, Apply)):
        return render(value)
    if isinstance(value, TopologyPlan):
        return render_topology(value)
    if isinstance(value, RemoteRestackAdoption):
        lines = ["remote restack adoption (local only; no push):"]
        lines.extend(
            f"  - {item.branch}: L={item.local_commit_id} T={item.tracked_commit_id} R={item.remote_commit_id}"
            for item in value.boundaries
        )
        lines.extend(
            f"  - affected workspace {name}" for name, _change in value.workspace_change_ids
        )
        return "\n".join(lines)
    if isinstance(value, FirstPublication):
        return "first publication:\n" + "\n".join(
            f"  - publish {slot.commit_id} as {slot.branch} onto {slot.base_branch}"
            for slot in value.slots
        )
    if isinstance(value, MembershipRepair):
        slots = {slot.slot_id: slot for slot in value.slots}
        source = {pr.identity: pr for pr in value.source_prs}
        retained = {
            item.pr_identity for item in value.desired
            if isinstance(item, ExistingMembershipEntry)
        }
        lines = [f"membership repair at {value.phase.value}:", "  ordered goal:"]
        for item in value.desired:
            if isinstance(item, ExistingMembershipEntry):
                lines.append(f"    - existing #{source[item.pr_identity].number}")
            else:
                slot = slots[item.slot_id]
                lines.append(f"    - new {slot.slot_id}: {slot.commit_id} as {slot.branch} onto {slot.base_branch}")
        detached = (
            ()
            if value.source_tracked is None
            else tuple(
                identity
                for identity in value.source_tracked.ordered_prs
                if identity not in retained
            )
        )
        lines.append(
            "  detached: "
            + (
                ", ".join(f"#{source[item].number}" for item in detached)
                if detached
                else "none"
            )
        )
        return "\n".join(lines)
    if isinstance(value, PersistedOperation):
        if isinstance(value.operation, TopologyRepair):
            return (
                f"resume topology repair at {value.operation.phase.value}:\n"
                + render_topology(value.operation.plan)
            )
        if isinstance(value.operation, MembershipRepair):
            return "resume " + render_explicit(value.operation)
        return (
            f"resume first publication at {value.operation.phase.value}:\n"
            + render_explicit(value.operation)
        )
    if isinstance(value, FirstPublicationVerified):
        return f"published {len(value.ordered_prs)} PR(s)"
    if isinstance(value, TopologyRepairVerified):
        return f"topology repaired for {len(value.ordered_prs)} PR(s)"
    if isinstance(value, MembershipRepairVerified):
        return f"membership repaired for {len(value.ordered_prs)} PR(s)"
    if isinstance(value, AdoptionVerified):
        return f"adopted {len(value.adopted_heads)} remote head(s) locally"
    if isinstance(value, Verified):
        return "sync verified"
    if isinstance(value, Stopped):
        return f"stopped [{value.stage}]: {value.detail}"
    raise TypeError(f"unsupported explicit value: {type(value).__name__}")


def render_detached_associations(values: Sequence[DetachedAssociation]) -> str:
    if not values:
        return "no detached PR associations"
    return "detached PR associations:\n" + "\n".join(
        f"  - PR #{item.pr.number} ({item.repository.host}, base {item.base_branch})"
        for item in values
    )


def render_detached_result(value: DetachedAssociationResult) -> str:
    return (
        f"removed detached association PR #{value.association.pr.number}; "
        f"state {value.state_blob_oid}"
    )


def apply_explicit(
    value: ExplicitPlan, workspace: str | Path, github: GitHubClient
) -> ExplicitResult:
    """Dispatch the exact immutable object that was rendered and approved."""
    if isinstance(value, Blocked):
        return value
    if isinstance(value, RemoteRestackAdoption):
        return adopt_remote_restack(value, workspace, github)
    if isinstance(value, FirstPublication):
        start_first_publication(value, workspace)
        return resume_first_publication(workspace, github)
    if isinstance(value, MembershipRepair):
        start_membership_repair(value, workspace)
        return resume_membership_repair(workspace, github)
    if isinstance(value, TopologyPlan):
        start_topology_repair(value, workspace)
        return resume_topology_repair(workspace, github)
    if isinstance(value, PersistedOperation):
        return resume_operation(workspace, github)
    return apply(value, workspace, github)
