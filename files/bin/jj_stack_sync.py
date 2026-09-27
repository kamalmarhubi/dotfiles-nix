"""Read-only observation and pure planning foundation for ``jj stack sync``.

This module intentionally has no command-line entry point and no remote mutation.
"""

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
from collections.abc import Iterator, Sequence
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
        if not self.base_branch or not self.ordered:
            raise ValueError("stack selection must have a base and assignments")
        identities = tuple(item.pr_identity for item in self.ordered)
        if len(set(identities)) != len(identities):
            raise ValueError("stack selection must not assign a PR more than once")


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


@dataclass(frozen=True)
class Verified:
    head_published: bool
    metadata_updated: bool
    authority_persisted: bool


@dataclass(frozen=True)
class Stopped:
    stage: str
    detail: str
    head_published: bool = False
    metadata_updated: bool = False
    final_state_verified: bool = False
    authority_persisted: bool = False


ApplyResult = Verified | Stopped


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
      stack { id number baseRefName }
    }
  }
}
"""


def _parse_github_pull_request(
    value: object, repository: GitHubRepository, expected_number: int
) -> GitHubPullRequest:
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
) -> Snapshot:
    """Assemble complete source facts for one explicitly selected PR's stack."""
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
            require_text(pr.repository.host, pr.repository.node_id)
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
            f"publish {item.new_commit_id} to {item.ref.full_name}"
            for item in plan.head_updates
        ),
        *(
            f"update metadata for PR #{pr_numbers[item.pr_identity]}"
            for item in plan.metadata_updates
        ),
    ]
    return "apply:\n" + "\n".join(f"  - {item}" for item in consequences)


def _standalone_apply_shape(plan: SyncPlan) -> Stopped | None:
    if isinstance(plan, Blocked):
        return Stopped("plan", "a blocked plan cannot be applied")
    if (
        len(plan.desired.active) != 1
        or len(plan.dependencies.prs) != 1
        or not isinstance(plan.dependencies.membership, StandalonePullRequest)
        or plan.dependencies.membership.pr != plan.desired.active[0].pr_identity
        or plan.dependencies.prs[0].identity != plan.desired.active[0].pr_identity
        or plan.dependencies.prs[0].state is not PullRequestState.OPEN
    ):
        return Stopped(
            "shape",
            "apply currently requires exactly one open standalone pull request",
        )
    if isinstance(plan, Apply):
        if (
            len(plan.head_updates) > 1
            or len(plan.metadata_updates) > 1
            or any(
                update.pr_identity != plan.desired.active[0].pr_identity
                for update in plan.metadata_updates
            )
        ):
            return Stopped("shape", "plan contains unsupported multi-PR effects")
        expected_ref = RemoteBranchRef(
            plan.desired.repository,
            f"refs/heads/{plan.dependencies.prs[0].head_branch}",
        )
        if any(update.ref != expected_ref for update in plan.head_updates):
            return Stopped("shape", "plan changes an unexpected head branch")
    return None


def _apply_revision(plan: NoOp | Apply) -> str:
    desired = plan.desired.active[0].desired_commit_id
    base = plan.dependencies.live_bases[0].commit_id
    if base is None:
        raise Error("planned base ref is absent")
    return f"{base}::{desired}"


def reobserve_for_apply(
    workspace: str | Path, github: GitHubClient, plan: NoOp | Apply
) -> Snapshot:
    """Reobserve only the frozen standalone plan's publication dependencies."""
    config_keys = tuple(key for key, _value in plan.dependencies.effective_config)
    local = observe_local(
        workspace,
        revision=_apply_revision(plan),
        config_keys=config_keys,
    )
    push_url = resolve_push_url(local, plan.dependencies.remote)
    repository = github.resolve_repository(push_url)
    planned_pr = plan.dependencies.prs[0]
    pull_request = github.pull_requests((planned_pr.identity,))[0]
    if pull_request.stack is None:
        membership: PullRequestMembership = StandalonePullRequest(
            pull_request.identity
        )
    else:
        summary = pull_request.stack
        stack = github.stack(repository, summary.identity)
        if stack is None:
            raise IncompleteSource("pull request stack is unavailable")
        if (
            stack.node_id != summary.node_id
            or stack.base_branch != summary.base_branch
            or pull_request.identity not in stack.pull_requests
        ):
            raise SourceMismatch("pull request and complete stack disagree")
        membership = ServerStackMembership(
            pull_request.identity,
            summary,
            stack.pull_requests,
        )
    ref_names = tuple(
        dict.fromkeys(
            (
                *(ref.ref.full_name for ref in plan.dependencies.live_heads),
                *(ref.ref.full_name for ref in plan.dependencies.live_bases),
            )
        )
    )
    return Snapshot(
        repository.identity,
        push_url,
        plan.dependencies.remote,
        local,
        observe_tool_state(workspace),
        (pull_request,),
        membership,
        observe_live_refs(push_url, repository.identity, ref_names, cwd=workspace),
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
    if fresh.live_refs != dependencies.live_heads + dependencies.live_bases:
        return _block("refs-changed", "destination", "live head or base refs changed")
    if len(fresh.pull_requests) != 1:
        return _block("pr-changed", "pull-request", "pull request observation changed")
    old_pr, new_pr = dependencies.prs[0], fresh.pull_requests[0]
    if _pr_without_metadata(new_pr) != _pr_without_metadata(old_pr):
        return _block(
            "pr-changed", f"PR #{old_pr.number}", "pull request state changed"
        )
    metadata_planned = isinstance(plan, Apply) and bool(plan.metadata_updates)
    if not metadata_planned and (new_pr.title, new_pr.body) != (
        old_pr.title,
        old_pr.body,
    ):
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


def push_exact_head_update(
    workspace: str | Path, update: PlannedHeadUpdate, push_url: str
) -> subprocess.CompletedProcess[str]:
    common = git_common_dir(workspace)
    return subprocess.run(
        [
            "git",
            f"--git-dir={common}",
            "push",
            "--atomic",
            "--no-follow-tags",
            "--recurse-submodules=no",
            f"--force-with-lease={update.ref.full_name}:{update.expected_old_commit_id}",
            push_url,
            f"{update.new_commit_id}:{update.ref.full_name}",
        ],
        text=True,
        capture_output=True,
        check=False,
    )


def _expected_final_pr(plan: NoOp | Apply) -> GitHubPullRequest:
    pr = plan.dependencies.prs[0]
    wanted = plan.desired.active[0]
    return replace(
        pr,
        head_oid=wanted.desired_commit_id,
        title=wanted.title,
        body=wanted.body,
    )


def verify_final_one_pr_state(
    workspace: str | Path, github: GitHubClient, plan: NoOp | Apply
) -> tuple[Snapshot | None, Stopped | None]:
    try:
        fresh = reobserve_for_apply(workspace, github, plan)
    except Error as exc:
        return None, Stopped("verify", str(exc))
    expected_pr = _expected_final_pr(plan)
    expected_head = LiveRemoteRef(
        RemoteBranchRef(
            plan.desired.repository, f"refs/heads/{expected_pr.head_branch}"
        ),
        plan.desired.active[0].desired_commit_id,
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
        or fresh.pull_requests != (expected_pr,)
        or fresh.live_refs != (expected_head,) + plan.dependencies.live_bases
    ):
        return fresh, Stopped(
            "verify", "authoritative final state does not match the frozen plan"
        )
    return fresh, None


def record_last_published_head(
    workspace: str | Path,
    expected_state_oid: str | None,
    state: TrackedState,
    publication: LastPublishedHead,
) -> str:
    retained = tuple(
        item
        for item in state.last_published_heads
        if not (item.pr == publication.pr and item.ref == publication.ref)
    )
    return cas_write_state(
        workspace,
        expected_state_oid,
        TrackedState(state.stacks, retained + (publication,)),
    )


def apply(plan: SyncPlan, workspace: str | Path, github: GitHubClient) -> ApplyResult:
    """Execute one frozen existing-head plan without replanning or lease refresh."""
    unsupported = _standalone_apply_shape(plan)
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
            _fresh, stopped = verify_final_one_pr_state(workspace, github, plan)
            return stopped or Verified(False, False, False)

        head_published = False
        metadata_updated = False
        if plan.head_updates:
            update = plan.head_updates[0]
            response = push_exact_head_update(
                workspace, update, plan.dependencies.push_url
            )
            if response.returncode == 0:
                head_published = True
            else:
                try:
                    observed = observe_live_refs(
                        plan.dependencies.push_url,
                        plan.desired.repository,
                        (update.ref.full_name,),
                        cwd=workspace,
                    )[0].commit_id
                except Error as exc:
                    return Stopped("push", f"push result is unknown: {exc}")
                if observed == update.new_commit_id:
                    head_published = True
                elif observed == update.expected_old_commit_id:
                    return Stopped("push", "push was not observed at the destination")
                else:
                    return Stopped("push", "destination head has an unexpected value")
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
        if plan.metadata_updates:
            try:
                update = plan.metadata_updates[0]
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
                pass

        final, stopped = verify_final_one_pr_state(workspace, github, plan)
        if stopped is not None:
            return replace(
                stopped,
                head_published=head_published,
                metadata_updated=metadata_updated,
            )
        assert final is not None
        if plan.metadata_updates:
            metadata_updated = True
        if not plan.head_updates:
            return Verified(False, metadata_updated, False)
        wanted = plan.desired.active[0]
        publication = LastPublishedHead(
            wanted.pr_identity,
            plan.head_updates[0].ref,
            wanted.desired_commit_id,
        )
        try:
            record_last_published_head(
                workspace,
                plan.dependencies.state_blob_oid,
                final.tool_state.state,
                publication,
            )
        except Error as exc:
            return Stopped(
                "receipt",
                f"publication verified but authority persistence failed: {exc}",
                head_published=True,
                metadata_updated=metadata_updated,
                final_state_verified=True,
            )
        return Verified(True, metadata_updated, True)
