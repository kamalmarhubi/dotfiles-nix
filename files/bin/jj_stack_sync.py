"""Observation, planning, and existing-stack synchronization for ``jj stack sync``.

This module intentionally has no command-line entry point or topology mutation.
"""

from __future__ import annotations

import fcntl
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
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from pathlib import Path
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


class GitHubTransportFailure(Error):
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


@dataclass(frozen=True)
class GitHubResponse:
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
    default_branch: str


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
class GitHubPullRequestSource:
    """One strictly validated PR projection plus its nullable stack fields."""

    pr: GitHubPullRequest
    stack_id: str | None
    stack_base_branch: str | None


@dataclass(frozen=True)
class GitHubStackSource:
    identity: ServerStackIdentity
    base_branch: str
    ordered_prs: tuple[GitHubPullRequest, ...]

    @property
    def stack_id(self) -> str:
        return self.identity.node_id

    @property
    def stack_number(self) -> int:
        return self.identity.number


@dataclass(frozen=True)
class StandalonePullRequest:
    pr: PullRequestId


@dataclass(frozen=True)
class ServerStackIdentity:
    repository: GitHubRepositoryId
    node_id: str
    number: int

    def __post_init__(self) -> None:
        if not self.repository.host or not self.repository.node_id or not self.node_id:
            raise ValueError("server stack identity strings must be nonempty")
        if type(self.number) is not int or self.number <= 0:
            raise ValueError("server stack number must be a positive integer")


@dataclass(frozen=True)
class ServerStackMembership:
    """Complete ordered membership for the stack containing selected_pr."""

    selected_pr: PullRequestId
    stack: ServerStackIdentity
    base_branch: str
    ordered_prs: tuple[PullRequestId, ...]

    def __post_init__(self) -> None:
        if not self.base_branch or not self.ordered_prs:
            raise ValueError("server stack identity and membership must be nonempty")
        if self.selected_pr not in self.ordered_prs:
            raise ValueError("selected PR must belong to the observed server stack")
        if len(set(self.ordered_prs)) != len(self.ordered_prs):
            raise ValueError("server stack membership must not contain duplicates")
        if any(pr.repository != self.selected_pr.repository for pr in self.ordered_prs):
            raise ValueError("all server stack members must belong to one repository")
        if self.stack.repository != self.selected_pr.repository:
            raise ValueError("server stack and members must belong to one repository")

    @property
    def server_stack_id(self) -> str:
        return self.stack.node_id

    @property
    def server_stack_number(self) -> int:
        return self.stack.number


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


@dataclass(frozen=True)
class TrackedState:
    stacks: tuple[TrackedStack, ...]
    last_published_heads: tuple[LastPublishedHead, ...]
    last_adopted_heads: tuple[LastAdoptedHead, ...] = ()


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
        if not self.base_branch or not self.ordered:
            raise ValueError("stack selection must have a base and assignments")
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
    COMMITTING = "committing"


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
    pr_number: int | None = None

    @property
    def marker(self) -> str:
        return f"<!-- jj-stack-slot:{self.slot_id} -->"

    @property
    def initial_body(self) -> str:
        return f"{self.body}\n\n{self.marker}" if self.body else self.marker


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
    slots: tuple[NewPRSlot, ...]
    phase: FirstPublicationPhase = FirstPublicationPhase.PREPARING_BOOKMARKS
    final_state_json: str | None = None
    final_state_oid: str | None = None


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
    pr_number: int
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
    pull_request_numbers: tuple[int, ...]
    expected_before: tuple[PullRequestId, ...]
    desired_after: tuple[PullRequestId, ...]
    stack: ServerStackIdentity | None = None
    phase: GitHubEffectPhase = GitHubEffectPhase.NOT_ATTEMPTED
    resulting_stack: ServerStackIdentity | None = None


GitHubEffect = PullRequestBaseEffect | StackEffect


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
    tracking_update: TrackedStack | None
    dependencies: Dependencies


SyncPlan = Blocked | NoOp | Apply


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


def _read_github_response(response: object, status: int) -> GitHubResponse:
    headers = _response_headers(response)
    try:
        body = response.read()  # type: ignore[attr-defined]
    except http.client.IncompleteRead as exc:
        raise GitHubTransportFailure(
            "truncated-body", status=status, headers=headers, body=exc.partial
        ) from exc
    except (OSError, TimeoutError, socket.timeout) as exc:
        raise GitHubTransportFailure("read", status=status, headers=headers) from exc
    lengths = tuple(
        int(value)
        for name, value in headers
        if name.lower() == "content-length" and value.isdecimal()
    )
    if lengths and (len(set(lengths)) != 1 or len(body) != lengths[0]):
        raise GitHubTransportFailure(
            "truncated-body", status=status, headers=headers, body=body
        )
    return GitHubResponse(status, headers, body)


def github_http_request(
    host: str,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    cwd: str | Path | None = None,
) -> GitHubResponse:
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
        raise GitHubTransportFailure(category) from exc
    else:
        with response:
            result = _read_github_response(response, response.status)
    if time.monotonic() - started > GITHUB_OVERALL_TIMEOUT:
        raise GitHubTransportFailure(
            "overall-timeout",
            status=result.status,
            headers=result.headers,
            body=result.body,
        )
    return result


def _github_json_response(response: GitHubResponse, context: str) -> object:
    if not 200 <= response.status < 300:
        raise SourceUnavailable(f"{context} returned HTTP {response.status}")
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


def _github_repository_path(repository_name: str) -> str:
    owner, separator, name = repository_name.partition("/")
    if not separator or not owner or not name or "/" in name:
        raise ValueError("repository name must have owner/name form")
    return f"repos/{quote(owner, safe='')}/{quote(name, safe='')}"


def send_github_effect(
    effect: GitHubEffect, *, cwd: str | Path | None = None
) -> GitHubResponse:
    """Send one already-journaled effect exactly once; callers own reconciliation."""
    if effect.phase is not GitHubEffectPhase.POSSIBLY_SENT:
        raise ValueError("GitHub effect must be journaled as possibly-sent before I/O")
    if effect.repository.host != "github.com":
        raise SourceUnavailable(f"unsupported GitHub host: {effect.repository.host}")
    base = _github_repository_path(effect.repository_name)
    if isinstance(effect, PullRequestBaseEffect):
        if effect.pr_identity.repository != effect.repository:
            raise ValueError("pull request effect belongs to another repository")
        method = "PATCH"
        path = f"/{base}/pulls/{effect.pr_number}"
        payload: object | None = {"base": effect.desired_base}
    elif effect.kind is StackEffectKind.CREATE:
        if effect.stack is not None:
            raise ValueError("stack creation cannot freeze an existing stack")
        method, path = "POST", f"/{base}/stacks"
        payload = {"pull_requests": list(effect.pull_request_numbers)}
    else:
        if effect.stack is None or effect.stack.repository != effect.repository:
            raise ValueError("stack mutation requires the frozen stack identity")
        suffix = "add" if effect.kind is StackEffectKind.ADD else "unstack"
        method, path = "POST", f"/{base}/stacks/{effect.stack.number}/{suffix}"
        payload = (
            {"pull_requests": list(effect.pull_request_numbers)}
            if effect.kind is StackEffectKind.ADD
            else None
        )
    body = (
        None
        if payload is None
        else json.dumps(payload, separators=(",", ":")).encode()
    )
    return github_http_request(
        effect.repository.host, method, path, body=body, cwd=cwd
    )


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


def observe_github_repository(
    push_url: str, *, cwd: str | Path | None = None
) -> GitHubRepository:
    host, name_with_owner = _github_repository_locator(push_url, cwd=cwd)
    owner, separator, name = name_with_owner.partition("/")
    assert separator and owner and name
    raw = _exact_record(
        _github_graphql(
            host,
            """
            query($owner: String!, $name: String!) {
              repository(owner: $owner, name: $name) {
                id nameWithOwner url defaultBranchRef { name }
              }
            }
            """,
            {"owner": owner, "name": name},
            cwd=cwd,
        ),
        {"data"},
        "GitHub repository GraphQL response",
    )
    data = _exact_record(raw["data"], {"repository"}, "repository GraphQL data")
    if data["repository"] is None:
        raise IncompleteSource("GitHub repository is unavailable")
    raw = _exact_record(
        data["repository"],
        {"id", "nameWithOwner", "url", "defaultBranchRef"},
        "GitHub repository response",
    )
    default = raw["defaultBranchRef"]
    if default is None:
        raise IncompleteSource("GitHub repository has no default branch")
    default_record = _exact_record(default, {"name"}, "default branch")
    url = _text(raw["url"], "repository URL")
    response_host = urlparse(url).hostname
    if response_host is None:
        raise MalformedSource("repository URL has no host")
    if response_host.lower() != host or raw["nameWithOwner"] != name_with_owner:
        raise SourceMismatch("GitHub returned a different repository locator")
    return GitHubRepository(
        GitHubRepositoryId(host, _text(raw["id"], "repository ID")),
        _text(raw["nameWithOwner"], "repository name"),
        url,
        _text(default_record["name"], "default branch name"),
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

_PR_SOURCE_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    id
    pullRequest(number: $number) {
      id number state isDraft
      headRepository { id }
      headRefName headRefOid baseRefName baseRefOid
      autoMergeRequest { enabledAt }
      mergeQueueEntry { id }
      title body
      stack { id baseRefName }
    }
  }
}
"""


def _parse_pr_source(
    value: object, repository: GitHubRepository, expected_number: int
) -> GitHubPullRequestSource:
    raw = _exact_record(value, _PR_SOURCE_FIELDS, "pull request")
    number = raw["number"]
    if type(number) is not int or number <= 0 or number != expected_number:
        raise SourceMismatch("GitHub returned a different pull request number")
    try:
        state = PullRequestState(_text(raw["state"], "pull request state"))
    except ValueError as exc:
        raise MalformedSource("pull request state is unsupported") from exc
    draft = raw["isDraft"]
    if type(draft) is not bool:
        raise MalformedSource("pull request draft state must be boolean")
    head_repository = raw["headRepository"]
    if head_repository is None:
        raise IncompleteSource("pull request head repository is unavailable")
    head_repository_record = _exact_record(
        head_repository, {"id"}, "pull request head repository"
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
    stack_id = None
    stack_base = None
    if stack is not None:
        stack_record = _exact_record(stack, {"id", "baseRefName"}, "pull request stack")
        stack_id = _text(stack_record["id"], "stack ID")
        stack_base = _text(stack_record["baseRefName"], "stack base branch")
    identity = PullRequestId(repository.identity, _text(raw["id"], "pull request ID"))
    return GitHubPullRequestSource(
        GitHubPullRequest(
            identity,
            number,
            state,
            draft,
            GitHubRepositoryId(
                repository.identity.host,
                _text(head_repository_record["id"], "head repository ID"),
            ),
            _text(raw["headRefName"], "head branch"),
            _text(raw["headRefOid"], "reported head commit"),
            _text(raw["baseRefName"], "base branch"),
            _text(raw["baseRefOid"], "reported base commit"),
            auto_merge is not None,
            merge_queue is not None,
            _text(raw["title"], "pull request title"),
            body,
        ),
        stack_id,
        stack_base,
    )


def observe_github_pull_request(
    repository: GitHubRepository,
    number: int,
    *,
    cwd: str | Path | None = None,
) -> GitHubPullRequestSource:
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
    return _parse_pr_source(pull_request, repository, number)


_STACK_SOURCE_QUERY = """
query($id: ID!) {
  node(id: $id) {
    ... on PullRequestStack {
      id number baseRefName
      entries(first: 100) {
        nodes { position pullRequest {
          id number state isDraft
          headRepository { id }
          headRefName headRefOid baseRefName baseRefOid
          autoMergeRequest { enabledAt }
          mergeQueueEntry { id }
          title body
          stack { id baseRefName }
        } }
        pageInfo { hasNextPage }
      }
    }
  }
}
"""


def observe_github_stack(
    repository: GitHubRepository,
    stack_id: str,
    *,
    cwd: str | Path | None = None,
) -> GitHubStackSource:
    response = _exact_record(
        _github_graphql(
            repository.identity.host,
            _STACK_SOURCE_QUERY,
            {"id": stack_id},
            cwd=cwd,
        ),
        {"data"},
        "GitHub stack response",
    )
    data = _exact_record(response["data"], {"node"}, "stack GraphQL data")
    if data["node"] is None:
        raise IncompleteSource("pull request stack is unavailable")
    stack = _exact_record(
        data["node"],
        {"id", "number", "baseRefName", "entries"},
        "pull request stack",
    )
    if _text(stack["id"], "stack ID") != stack_id:
        raise SourceMismatch("GitHub returned a different stack")
    stack_number = stack["number"]
    if type(stack_number) is not int or stack_number <= 0:
        raise MalformedSource("stack number must be a positive integer")
    base_branch = _text(stack["baseRefName"], "stack base branch")
    entries = _exact_record(stack["entries"], {"nodes", "pageInfo"}, "stack entries")
    page_info = _exact_record(
        entries["pageInfo"], {"hasNextPage"}, "stack entries page info"
    )
    if page_info["hasNextPage"] is not False:
        if page_info["hasNextPage"] is True:
            raise IncompleteSource("pull request stack has more than 100 entries")
        raise MalformedSource("stack pagination state must be boolean")
    nodes = entries["nodes"]
    if not isinstance(nodes, list) or not nodes:
        raise IncompleteSource("pull request stack has no entries")
    positioned: list[tuple[int, GitHubPullRequest]] = []
    for node in nodes:
        entry = _exact_record(node, {"position", "pullRequest"}, "stack entry")
        position = entry["position"]
        if type(position) is not int or position <= 0:
            raise MalformedSource("stack position must be a positive integer")
        if entry["pullRequest"] is None:
            raise IncompleteSource("stack entry pull request is unavailable")
        raw_pr = _exact_record(
            entry["pullRequest"], _PR_SOURCE_FIELDS, "stack pull request"
        )
        number = raw_pr["number"]
        if type(number) is not int:
            raise MalformedSource("pull request number must be an integer")
        parsed = _parse_pr_source(raw_pr, repository, number)
        if parsed.stack_id != stack_id or parsed.stack_base_branch != base_branch:
            raise SourceMismatch("stack entry reports different membership or base")
        positioned.append((position, parsed.pr))
    positioned.sort(key=lambda item: item[0])
    if [position for position, _pr in positioned] != list(
        range(1, len(positioned) + 1)
    ):
        raise IncompleteSource("stack positions are incomplete or duplicated")
    prs = tuple(pr for _position, pr in positioned)
    if len({pr.identity for pr in prs}) != len(prs):
        raise SourceMismatch("stack contains duplicate pull request identities")
    return GitHubStackSource(
        ServerStackIdentity(repository.identity, stack_id, stack_number),
        base_branch,
        prs,
    )


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
    *,
    revision: str,
    config_keys: Sequence[str],
    remote: str,
    selected_pr_number: int,
) -> Snapshot:
    """Assemble complete source facts for one explicitly selected PR's stack."""
    local = observe_local(workspace, revision=revision, config_keys=config_keys)
    push_url = resolve_push_url(local, remote)
    fetch_url = resolve_remote_url(local, remote)
    repository = observe_github_repository(push_url, cwd=workspace)
    source = observe_github_pull_request(repository, selected_pr_number, cwd=workspace)
    selected_pr = source.pr
    if source.stack_id is None:
        if source.stack_base_branch is not None:
            raise SourceMismatch("standalone pull request reported a stack base")
        pull_requests = (selected_pr,)
        membership: PullRequestMembership = StandalonePullRequest(selected_pr.identity)
        base_branch = selected_pr.base_branch
    else:
        if source.stack_base_branch is None:
            raise IncompleteSource("stack base branch is unavailable")
        stack = observe_github_stack(repository, source.stack_id, cwd=workspace)
        if (
            stack.identity.repository != repository.identity
            or stack.identity.node_id != source.stack_id
            or stack.base_branch != source.stack_base_branch
            or selected_pr.identity not in {pr.identity for pr in stack.ordered_prs}
        ):
            raise SourceMismatch("selected pull request and complete stack disagree")
        pull_requests = stack.ordered_prs
        membership = ServerStackMembership(
            selected_pr.identity,
            stack.identity,
            stack.base_branch,
            tuple(pr.identity for pr in pull_requests),
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
    for authority in (*state.last_published_heads, *state.last_adopted_heads):
        require_text(
            authority.pr.repository.host,
            authority.pr.repository.node_id,
            authority.pr.node_id,
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


def _validate_first_publication(operation: FirstPublication) -> None:
    if (
        not operation.repository.host
        or not operation.repository.node_id
        or not operation.repository_name
        or not operation.push_url
        or not operation.remote
        or not operation.base_branch
        or len(operation.slots) != 1
    ):
        raise ValueError(
            "standalone first-publication identity and goal must be nonempty"
        )
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
        if bound != (slot.pr_identity is not None and slot.pr_number is not None):
            raise ValueError("slot binding does not match its phase")
        if (
            slot.pr_identity is not None
            and slot.pr_identity.repository != operation.repository
        ):
            raise ValueError("slot PR belongs to another repository")
        if slot.pr_number is not None and slot.pr_number <= 0:
            raise ValueError("slot PR number must be positive")
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
    if operation.phase is FirstPublicationPhase.COMMITTING and phases != {
        NewPRPhase.VERIFIED
    }:
        raise ValueError("committing requires every slot verified")
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
        raw = exact(value, {"repository", "node_id"}, context)
        return PullRequestId(
            repository(raw["repository"], f"{context}.repository"),
            text(raw["node_id"], f"{context}.node_id"),
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
                "slots",
                "phase",
                "final_state_json",
                "final_state_oid",
            },
            "operation",
        )
        if raw["operation_kind"] != "first-publication":
            raise ValueError("operation kind is invalid")
        raw_slots = raw["slots"]
        raw_config = raw["effective_config"]
        raw_workspaces = raw["workspace_targets"]
        if (
            not isinstance(raw_slots, list)
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
                    "pr_number",
                },
                context,
            )
            number = slot["pr_number"]
            if number is not None and type(number) is not int:
                raise ValueError(f"{context}.pr_number must be an integer or null")
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
                    number,
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
            tuple(slots),
            FirstPublicationPhase(text(raw["phase"], "operation.phase")),
            optional_text(raw["final_state_json"], "operation.final_state_json"),
            optional_text(raw["final_state_oid"], "operation.final_state_oid"),
        )
        _validate_first_publication(operation)
        return operation
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise Error(f"invalid refs/jj-stack/operation payload: {exc}") from exc


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
        if name in protected:
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
                pull_request.identity.node_id,
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
        local.effective_config,
        local.workspace_targets,
        base_branch,
        tool_state.state_blob_oid,
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
                assignment.pr_identity.node_id,
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
            desired.repository.node_id,
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
                pr.identity.node_id,
                "planner requires repository-local PRs",
            )
        if pr.state is PullRequestState.OPEN:
            seen_open = True
        elif pr.state is PullRequestState.CLOSED or seen_open:
            return _block(
                "unsupported-member-state",
                pr.identity.node_id,
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
        if len(heads) != 1 or heads[0].commit_id != pr.reported_head_commit_id:
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
                pr.identity.node_id,
                "PR literal base does not match unchanged stack order",
            )
        pr_base_name = f"refs/heads/{pr.base_branch}"
        pr_base_ref = RemoteBranchRef(snapshot.repository, pr_base_name)
        pr_bases = tuple(
            value for value in snapshot.live_refs if value.ref == pr_base_ref
        )
        if len(pr_bases) != 1 or pr_bases[0].commit_id != pr.reported_base_commit_id:
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
    comparison_base = (
        merged[-1].reported_head_commit_id if merged else bases[0].commit_id
    )
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
    if len(publications) != 1:
        adoptions = tuple(
            item
            for item in snapshot.tool_state.state.last_adopted_heads
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
    candidate = TrackedStack(snapshot.repository, base_branch, ordered_prs)
    if candidate in snapshot.tool_state.state.stacks:
        return None
    members = set(ordered_prs)
    if any(
        stack.repository == snapshot.repository
        and members.intersection(stack.ordered_prs)
        for stack in snapshot.tool_state.state.stacks
    ):
        return _block(
            "tracked-topology-changed",
            "stack",
            "observed membership overlaps a different tracked stack",
        )
    return candidate


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
                pr.identity.node_id,
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
    consequences = [
        *(
            f"publish {item.new_commit_id} to {item.ref.full_name}"
            for item in plan.head_updates
        ),
        *(
            f"update metadata for PR {item.pr_identity.node_id}"
            for item in plan.metadata_updates
        ),
        *(
            ("record verified stack membership",)
            if plan.tracking_update is not None
            else ()
        ),
    ]
    return "apply:\n" + "\n".join(f"  - {item}" for item in consequences)


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
    base = merged[-1].reported_head_commit_id if merged else None
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


def reobserve_for_apply(workspace: str | Path, plan: NoOp | Apply) -> Snapshot:
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
        revision=_apply_revision(plan),
        config_keys=config_keys,
        remote=plan.dependencies.remote,
        selected_pr_number=selected_prs[0].number,
    )


def _pr_without_metadata(pr: GitHubPullRequest) -> tuple[object, ...]:
    return (
        pr.identity,
        pr.number,
        pr.state,
        pr.draft,
        pr.head_repository,
        pr.head_branch,
        pr.reported_head_commit_id,
        pr.base_branch,
        pr.reported_base_commit_id,
        pr.auto_merge_enabled,
        pr.in_merge_queue,
    )


def _dependency_live_refs(dependencies: Dependencies) -> tuple[LiveRemoteRef, ...]:
    return tuple(dict.fromkeys(dependencies.live_heads + dependencies.live_bases))


def validate_frozen_dependencies(plan: NoOp | Apply, fresh: Snapshot) -> Blocked | None:
    dependencies = plan.dependencies
    if fresh.repository != plan.desired.repository:
        return _block("repository-changed", "repository", "repository identity changed")
    if fresh.local.effective_config != dependencies.effective_config:
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
                "pr-changed", old_pr.identity.node_id, "pull request state changed"
            )
        if old_pr.identity not in metadata_planned and (
            new_pr.title,
            new_pr.body,
        ) != (old_pr.title, old_pr.body):
            return _block(
                "metadata-changed",
                old_pr.identity.node_id,
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


_UPDATE_PR_MUTATION = """
mutation($id: ID!, $title: String!, $body: String!) {
  updatePullRequest(input: {pullRequestId: $id, title: $title, body: $body}) {
    pullRequest { id }
  }
}
"""


def update_frozen_pr_metadata(workspace: str | Path, update: PRMetadataUpdate) -> None:
    response = _exact_record(
        _github_graphql(
            update.pr_identity.repository.host,
            _UPDATE_PR_MUTATION,
            {
                "id": update.pr_identity.node_id,
                "title": update.title,
                "body": update.body,
            },
            cwd=workspace,
        ),
        {"data"},
        "update pull request response",
    )
    data = _exact_record(
        response["data"], {"updatePullRequest"}, "update pull request data"
    )
    updated = _exact_record(
        data["updatePullRequest"], {"pullRequest"}, "update pull request payload"
    )
    pull_request = _exact_record(updated["pullRequest"], {"id"}, "updated pull request")
    if pull_request["id"] != update.pr_identity.node_id:
        raise SourceMismatch("GitHub updated a different pull request")


_SLOT_PR_FIELDS = {
    "id",
    "number",
    "state",
    "isDraft",
    "headRefName",
    "headRefOid",
    "baseRefName",
    "baseRefOid",
    "title",
    "body",
}


def _parse_slot_pr(raw: object, repository: GitHubRepositoryId) -> GitHubPullRequest:
    value = _exact_record(raw, _SLOT_PR_FIELDS, "first-publication pull request")
    number = value["number"]
    draft = value["isDraft"]
    if type(number) is not int or number <= 0 or type(draft) is not bool:
        raise MalformedSource("first-publication PR number or draft state is invalid")
    try:
        state = PullRequestState(_text(value["state"], "pull request state"))
    except ValueError as exc:
        raise MalformedSource("first-publication PR state is unsupported") from exc
    body = value["body"]
    if body is None:
        body = ""
    if not isinstance(body, str):
        raise MalformedSource("first-publication PR body must be text or null")
    return GitHubPullRequest(
        PullRequestId(repository, _text(value["id"], "pull request ID")),
        number,
        state,
        draft,
        repository,
        _text(value["headRefName"], "pull request head branch"),
        _text(value["headRefOid"], "pull request head OID"),
        _text(value["baseRefName"], "pull request base branch"),
        _text(value["baseRefOid"], "pull request base OID"),
        False,
        False,
        _text(value["title"], "pull request title"),
        body,
    )


_CREATE_PR_MUTATION = """
mutation($repository: ID!, $base: String!, $head: String!, $title: String!, $body: String!) {
  createPullRequest(input: {
    repositoryId: $repository, baseRefName: $base, headRefName: $head,
    title: $title, body: $body, draft: true
  }) {
    pullRequest {
      id number state isDraft headRefName headRefOid baseRefName baseRefOid title body
    }
  }
}
"""


def create_slot_pull_request(
    workspace: str | Path, operation: FirstPublication, slot: NewPRSlot
) -> GitHubPullRequest:
    response = _exact_record(
        _github_graphql(
            operation.repository.host,
            _CREATE_PR_MUTATION,
            {
                "repository": operation.repository.node_id,
                "base": slot.base_branch,
                "head": slot.branch,
                "title": slot.title,
                "body": slot.initial_body,
            },
            cwd=workspace,
        ),
        {"data"},
        "create pull request response",
    )
    data = _exact_record(response["data"], {"createPullRequest"}, "create PR data")
    payload = data["createPullRequest"]
    if payload is None:
        raise IncompleteSource("create pull request returned no payload")
    created = _exact_record(payload, {"pullRequest"}, "create PR payload")
    return _parse_slot_pr(created["pullRequest"], operation.repository)


_FIND_SLOT_PRS_QUERY = """
query($owner: String!, $name: String!, $head: String!) {
  repository(owner: $owner, name: $name) {
    id
    pullRequests(first: 100, states: [OPEN, CLOSED, MERGED], headRefName: $head) {
      nodes {
        id number state isDraft headRefName headRefOid baseRefName baseRefOid title body
      }
      pageInfo { hasNextPage }
    }
  }
}
"""


def find_slot_pull_requests(
    workspace: str | Path,
    operation: FirstPublication,
    slot: NewPRSlot,
    *,
    marker_only: bool = True,
) -> tuple[GitHubPullRequest, ...]:
    owner, _separator, name = operation.repository_name.partition("/")
    response = _exact_record(
        _github_graphql(
            operation.repository.host,
            _FIND_SLOT_PRS_QUERY,
            {"owner": owner, "name": name, "head": slot.branch},
            cwd=workspace,
        ),
        {"data"},
        "slot lookup response",
    )
    data = _exact_record(response["data"], {"repository"}, "slot lookup data")
    repository = data["repository"]
    if repository is None:
        raise IncompleteSource("repository is absent during slot lookup")
    repo = _exact_record(repository, {"id", "pullRequests"}, "slot lookup repository")
    if _text(repo["id"], "slot lookup repository ID") != operation.repository.node_id:
        raise SourceMismatch("slot lookup returned another repository")
    connection = _exact_record(
        repo["pullRequests"], {"nodes", "pageInfo"}, "slot PR connection"
    )
    page = _exact_record(connection["pageInfo"], {"hasNextPage"}, "slot page info")
    if page["hasNextPage"] is not False:
        raise IncompleteSource("slot PR lookup was truncated")
    nodes = connection["nodes"]
    if not isinstance(nodes, list):
        raise MalformedSource("slot PR nodes must be an array")
    parsed = tuple(_parse_slot_pr(node, operation.repository) for node in nodes)
    return (
        tuple(pr for pr in parsed if slot.marker in pr.body) if marker_only else parsed
    )


def _slot_pr_matches(slot: NewPRSlot, pr: GitHubPullRequest, *, initial: bool) -> bool:
    return (
        pr.head_branch == slot.branch
        and pr.reported_head_commit_id == slot.commit_id
        and pr.base_branch == slot.base_branch
        and pr.title == slot.title
        and pr.body == (slot.initial_body if initial else slot.body)
        and (initial or (pr.state is PullRequestState.OPEN and pr.draft))
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
                reported_head_commit_id=(
                    wanted.desired_commit_id
                    if wanted is not None
                    else pr.reported_head_commit_id
                ),
                reported_base_commit_id=final_refs[f"refs/heads/{pr.base_branch}"],
                title=wanted.title if wanted is not None else pr.title,
                body=wanted.body if wanted is not None else pr.body,
            )
        )
    return tuple(expected)


def verify_final_state(
    workspace: str | Path, plan: NoOp | Apply
) -> tuple[Snapshot | None, Stopped | None]:
    try:
        fresh = reobserve_for_apply(workspace, plan)
    except Error as exc:
        return None, Stopped("verify", str(exc))
    expected_prs = _expected_final_prs(plan)
    desired_heads = {
        RemoteBranchRef(plan.desired.repository, f"refs/heads/{pr.head_branch}"): (
            pr.reported_head_commit_id
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
    if (
        fresh.repository != plan.desired.repository
        or fresh.local.effective_config != plan.dependencies.effective_config
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
    snapshot: Snapshot, remote: str
) -> RemoteRestackAdoption | Blocked:
    """Match every old/new change ID and patch ID, then freeze adoption."""
    if remote != snapshot.remote:
        return _block("remote-changed", remote, "selected remote differs from observation")
    if not snapshot.fetch_url:
        return _block("fetch-url-missing", remote, "fetch transport was not observed")
    try:
        fetch_repository = observe_github_repository(
            snapshot.fetch_url, cwd=snapshot.local.workspace
        )
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
        if live != pr.reported_head_commit_id:
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
        tuple(
            item for item in snapshot.local.effective_config if item[0] != "git.push"
        ),
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
    workspace: str | Path, plan: RemoteRestackAdoption, revision: str
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
        revision=revision,
        config_keys=tuple(key for key, _value in plan.effective_config),
        remote=plan.remote,
        selected_pr_number=selected_pr.number,
    )


def reobserve_for_adoption(
    workspace: str | Path, plan: RemoteRestackAdoption
) -> Snapshot:
    """Revalidate pre-fetch state without resolving remote-only commit IDs."""
    return _reobserve_adoption(workspace, plan, _adoption_pre_fetch_revision(plan))


def reobserve_after_adoption(
    workspace: str | Path, plan: RemoteRestackAdoption
) -> Snapshot:
    """Observe old and newly fetched commit closures for final verification."""
    return _reobserve_adoption(workspace, plan, _adoption_post_fetch_revision(plan))


def _validate_adoption_prestate(
    plan: RemoteRestackAdoption, fresh: Snapshot
) -> str | None:
    if (
        fresh.repository != plan.repository
        or fresh.fetch_url != plan.fetch_url
        or fresh.local.effective_config != plan.effective_config
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
        or fresh.local.effective_config != plan.effective_config
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
    plan: RemoteRestackAdoption, workspace: str | Path
) -> AdoptionResult:
    """Adopt one frozen restack whose change IDs and patch IDs matched."""
    with repository_lock(workspace):
        try:
            before = reobserve_for_adoption(workspace, plan)
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
            after = reobserve_after_adoption(workspace, plan)
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
    operation: FirstPublication, workspace: str | Path
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
            prs = find_slot_pull_requests(workspace, operation, slot, marker_only=False)
            matches = tuple(pr for pr in prs if pr.identity == slot.pr_identity)
            if len(matches) != 1 or not _slot_pr_matches(
                slot, matches[0], initial=False
            ):
                return f"pull request for slot {slot.slot_id} does not match the goal"
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
    ordered = new_prs
    if len(set(ordered)) != len(ordered):
        raise Error("first-publication final membership contains duplicate PRs")
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
) -> FirstPublicationResult:
    state_oid, state = read_state(workspace)
    if operation.phase is not FirstPublicationPhase.COMMITTING:
        if state_oid != operation.expected_state_oid:
            return Stopped("state", "private state changed before final commit")
        external_error = _verify_first_publication_external(operation, workspace)
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
        external_error = _verify_first_publication_external(operation, workspace)
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
        tuple(
            slot.pr_identity for slot in operation.slots if slot.pr_identity is not None
        )
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


def resume_first_publication(workspace: str | Path) -> FirstPublicationResult:
    """Continue only the exact frozen goal stored in the operation ref."""
    with repository_lock(workspace):
        try:
            operation_oid, operation = read_first_publication(workspace)
        except Error as exc:
            return Stopped("operation", str(exc))
        if operation.phase is FirstPublicationPhase.COMMITTING:
            return _finish_first_publication(operation_oid, operation, workspace)
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
                    candidate = create_slot_pull_request(workspace, operation, slot)
                    if _slot_pr_matches(slot, candidate, initial=True):
                        returned = candidate
                except Error:
                    # The request may have reached GitHub. Resolve only by marker.
                    pass
                if returned is not None:
                    matches = (returned,)
                else:
                    try:
                        matches = find_slot_pull_requests(workspace, operation, slot)
                    except Error as exc:
                        return Stopped(
                            "create-pr", f"possibly sent; lookup failed: {exc}"
                        )
            elif slot.phase is NewPRPhase.POSSIBLY_SENT:
                try:
                    matches = find_slot_pull_requests(workspace, operation, slot)
                except Error as exc:
                    return Stopped("create-pr", f"possibly sent; lookup failed: {exc}")
            else:
                matches = ()
            if slot.phase is NewPRPhase.POSSIBLY_SENT:
                valid = tuple(
                    pr for pr in matches if _slot_pr_matches(slot, pr, initial=True)
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
                    pr_number=valid[0].number,
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
                    update_frozen_pr_metadata(
                        workspace,
                        PRMetadataUpdate(slot.pr_identity, slot.title, slot.body),
                    )
                    observed = find_slot_pull_requests(
                        workspace, operation, slot, marker_only=False
                    )
                except Error as exc:
                    return Stopped("metadata", str(exc))
                matches = tuple(
                    pr for pr in observed if pr.identity == slot.pr_identity
                )
                if len(matches) != 1 or not _slot_pr_matches(
                    slot, matches[0], initial=False
                ):
                    return Stopped("metadata", "bound PR did not reach frozen metadata")
                slot = replace(slot, phase=NewPRPhase.VERIFIED)
                operation = _replace_slot(operation, index, slot)
                operation_oid = cas_write_first_publication(
                    workspace, operation_oid, operation
                )
        if any(slot.phase is not NewPRPhase.VERIFIED for slot in operation.slots):
            return Stopped("create-pr", "not every first-publication slot is verified")
        return _finish_first_publication(operation_oid, operation, workspace)


def apply(plan: SyncPlan, workspace: str | Path) -> ApplyResult:
    """Execute one frozen existing-stack plan without replanning or lease refresh."""
    unsupported = _apply_shape(plan)
    if unsupported is not None:
        return unsupported
    assert isinstance(plan, NoOp | Apply)
    with repository_lock(workspace):
        try:
            fresh = reobserve_for_apply(workspace, plan)
        except Error as exc:
            return Stopped("reobserve", str(exc))
        drift = validate_frozen_dependencies(plan, fresh)
        if drift is not None:
            reason = drift.reasons[0]
            return Stopped("revalidate", f"{reason.code}: {reason.detail}")
        if isinstance(plan, NoOp):
            _fresh, stopped = verify_final_state(workspace, plan)
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
                published = reobserve_for_apply(workspace, plan)
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
                f"refs/heads/{pr.head_branch}": pr.reported_head_commit_id
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
                update_frozen_pr_metadata(workspace, update)
                metadata_updated = True
            except Error:
                # A lost API response is resolved by the same authoritative final read.
                metadata_error = True

        final, stopped = verify_final_state(workspace, plan)
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
