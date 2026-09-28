from __future__ import annotations

import dataclasses
from collections.abc import Callable, Collection, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TypeVar

import jj_stack_sync as sync


class FakeGitHubServer:
    """Authoritative GitHub state and semantics for workflow tests."""

    def __init__(self) -> None:
        self._repositories: dict[sync.GitHubRepositoryId, sync.GitHubRepository] = {}
        self._locators: dict[str, sync.GitHubRepositoryId] = {}
        self._pull_requests: dict[sync.PullRequestId, sync.GitHubPullRequest] = {}
        self._stacks: dict[sync.GitHubStackId, sync.GitHubStack] = {}
        self._branches: dict[tuple[sync.GitHubRepositoryId, str], str] = {}
        self._next_pull_request: dict[sync.GitHubRepositoryId, int] = {}

    def seed_repository(
        self, repository: sync.GitHubRepository, *, aliases: Sequence[str] = ()
    ) -> None:
        if repository.identity in self._repositories:
            raise ValueError("repository is already seeded")
        locators = (repository.name_with_owner, repository.url, *aliases)
        if any(locator in self._locators for locator in locators):
            raise ValueError("repository locator is already seeded")
        self._repositories[repository.identity] = repository
        for locator in locators:
            self._locators[locator] = repository.identity
        self._next_pull_request[repository.identity] = 1

    def seed_pull_request(self, pull_request: sync.GitHubPullRequest) -> None:
        if pull_request.identity.repository not in self._repositories:
            raise ValueError("pull request repository is not seeded")
        if pull_request.identity in self._pull_requests:
            raise ValueError("pull request is already seeded")
        if pull_request.stack is not None:
            raise ValueError("seed stack membership through seed_stack")
        self._pull_requests[pull_request.identity] = pull_request
        self._next_pull_request[pull_request.identity.repository] = max(
            self._next_pull_request[pull_request.identity.repository],
            pull_request.identity.number + 1,
        )

    def seed_branch(
        self, repository: sync.GitHubRepositoryId, branch: str, oid: str
    ) -> None:
        if repository not in self._repositories:
            raise ValueError("branch repository is not seeded")
        key = (repository, branch)
        if not branch or key in self._branches:
            raise ValueError("branch must be nonempty and unseeded")
        self._branches[key] = oid

    def seed_stack(self, stack: sync.GitHubStack) -> None:
        if stack.identity.repository not in self._repositories:
            raise ValueError("stack repository is not seeded")
        if stack.identity in self._stacks:
            raise ValueError("stack is already seeded")
        if not stack.pull_requests or len(set(stack.pull_requests)) != len(
            stack.pull_requests
        ):
            raise ValueError("stack membership must be nonempty and unique")
        if any(
            identity.repository != stack.identity.repository
            or identity not in self._pull_requests
            for identity in stack.pull_requests
        ):
            raise ValueError("stack members must be seeded in the same repository")
        existing = {
            identity
            for candidate in self._stacks.values()
            for identity in candidate.pull_requests
        }
        if existing.intersection(stack.pull_requests):
            raise ValueError("pull request already belongs to another stack")
        self._stacks[stack.identity] = stack

    def snapshot(self) -> FakeGitHubServer:
        frozen = FakeGitHubServer()
        frozen._repositories = self._repositories.copy()
        frozen._locators = self._locators.copy()
        frozen._pull_requests = self._pull_requests.copy()
        frozen._stacks = self._stacks.copy()
        frozen._branches = self._branches.copy()
        frozen._next_pull_request = self._next_pull_request.copy()
        return frozen

    def read_repository(
        self, locator: str | sync.GitHubRepositoryId
    ) -> sync.GitHubRepository:
        identity = self._locators.get(locator) if isinstance(locator, str) else locator
        if identity is None or identity not in self._repositories:
            raise sync.IncompleteSource("GitHub repository is unavailable")
        return dataclasses.replace(self._repositories[identity])

    def read_pull_requests(
        self, identities: Sequence[sync.PullRequestId]
    ) -> tuple[sync.GitHubPullRequest, ...]:
        result = []
        for identity in identities:
            pull_request = self._pull_requests.get(identity)
            if pull_request is None:
                raise sync.IncompleteSource(
                    f"pull request #{identity.number} is unavailable"
                )
            summaries = tuple(
                sync.GitHubStackSummary(
                    stack.identity, stack.node_id, stack.base_branch
                )
                for stack in self._stacks.values()
                if identity in stack.pull_requests
            )
            if len(summaries) > 1:
                raise AssertionError("fake contains overlapping stack membership")
            result.append(
                dataclasses.replace(
                    pull_request, stack=summaries[0] if summaries else None
                )
            )
        return tuple(result)

    def read_stack(
        self,
        repository: sync.GitHubRepository,
        identity: sync.GitHubStackId,
    ) -> sync.GitHubStack | None:
        if identity.repository != repository.identity:
            raise ValueError("stack identity belongs to another repository")
        stack = self._stacks.get(identity)
        return None if stack is None else dataclasses.replace(stack)

    def read_find_pull_requests(
        self,
        repository: sync.GitHubRepositoryId,
        *,
        head_branches: Sequence[str] = (),
        base_branches: Sequence[str] = (),
        states: Collection[sync.PullRequestState] | None = None,
    ) -> tuple[sync.GitHubPullRequest, ...]:
        heads = set(head_branches)
        bases = set(base_branches)
        if not heads and not bases:
            raise ValueError("pull request search requires a head or base branch")
        if "" in heads or "" in bases:
            raise ValueError("pull request search branches must be nonempty")
        selected_states = set(sync.PullRequestState) if states is None else set(states)
        if not selected_states:
            raise ValueError("pull request search states must be nonempty")
        return self.read_pull_requests(
            tuple(
                pull_request.identity
                for pull_request in self._pull_requests.values()
                if pull_request.identity.repository == repository
                and pull_request.state in selected_states
                and (
                    pull_request.head_branch in heads
                    or pull_request.base_branch in bases
                )
            )
        )

    def apply_create_pull_request(
        self,
        repository: sync.GitHubRepository,
        *,
        head_branch: str,
        base_branch: str,
        title: str,
        body: str,
        draft: bool,
    ) -> sync.PullRequestId:
        if not head_branch or not base_branch or not title:
            raise ValueError("pull request branches and title must be nonempty")
        head_oid = self._branches.get((repository.identity, head_branch))
        base_oid = self._branches.get((repository.identity, base_branch))
        if head_oid is None or base_oid is None:
            raise sync.IncompleteSource("pull request branch is unavailable")
        number = self._next_pull_request[repository.identity]
        self._next_pull_request[repository.identity] += 1
        identity = sync.PullRequestId(repository.identity, number)
        self._pull_requests[identity] = sync.GitHubPullRequest(
            identity,
            f"PR_{repository.identity.node_id}_{number}",
            sync.PullRequestState.OPEN,
            draft,
            repository.identity,
            head_branch,
            head_oid,
            base_branch,
            base_oid,
            False,
            False,
            title,
            body,
            None,
        )
        return identity

    def apply_update_pull_request(
        self,
        repository: sync.GitHubRepository,
        identity: sync.PullRequestId,
        *,
        title: str | None = None,
        body: str | None = None,
        base_branch: str | None = None,
        state: sync.PullRequestUpdateState | None = None,
    ) -> None:
        if identity.repository != repository.identity:
            raise ValueError("pull request identity belongs to another repository")
        if title is None and body is None and base_branch is None and state is None:
            raise ValueError("pull request update must change at least one field")
        if base_branch is not None and not base_branch:
            raise ValueError("pull request base branch must be nonempty")
        pull_request = self._pull_requests.get(identity)
        if pull_request is None:
            raise sync.IncompleteSource(f"pull request #{identity.number} is unavailable")
        changes: dict[str, object] = {}
        if title is not None:
            changes["title"] = title
        if body is not None:
            changes["body"] = body
        if base_branch is not None:
            changes["base_branch"] = base_branch
        if state is not None:
            changes["state"] = sync.PullRequestState[state.name]
        self._pull_requests[identity] = dataclasses.replace(pull_request, **changes)

    def move_head(self, identity: sync.PullRequestId, oid: str) -> None:
        pull_request = self._pull_request(identity)
        if not oid:
            raise ValueError("pull request head OID must be nonempty")
        self._pull_requests[identity] = dataclasses.replace(pull_request, head_oid=oid)

    def change_base(
        self,
        identity: sync.PullRequestId,
        branch: str,
        *,
        oid: str | None = None,
    ) -> None:
        pull_request = self._pull_request(identity)
        if not branch:
            raise ValueError("pull request base branch must be nonempty")
        changes: dict[str, object] = {"base_branch": branch}
        if oid is not None:
            if not oid:
                raise ValueError("pull request base OID must be nonempty")
            changes["base_oid"] = oid
        self._pull_requests[identity] = dataclasses.replace(pull_request, **changes)

    def close_pull_request(self, identity: sync.PullRequestId) -> None:
        self._set_state(identity, sync.PullRequestState.CLOSED)

    def merge_pull_request(self, identity: sync.PullRequestId) -> None:
        self._set_state(identity, sync.PullRequestState.MERGED)

    def _pull_request(
        self, identity: sync.PullRequestId
    ) -> sync.GitHubPullRequest:
        try:
            return self._pull_requests[identity]
        except KeyError as exc:
            raise sync.IncompleteSource(
                f"pull request #{identity.number} is unavailable"
            ) from exc

    def _set_state(
        self, identity: sync.PullRequestId, state: sync.PullRequestState
    ) -> None:
        pull_request = self._pull_request(identity)
        self._pull_requests[identity] = dataclasses.replace(pull_request, state=state)


T = TypeVar("T")
_Mutation = Callable[..., object]


class PendingRequest:
    def __init__(self) -> None:
        self._invoke: Callable[[], object] | None = None
        self._terminal = False

    def _capture(self, invoke: Callable[[], object]) -> None:
        if self._invoke is not None or self._terminal:
            raise AssertionError("pending GitHub request was captured more than once")
        self._invoke = invoke

    def release(self) -> object:
        if self._invoke is None:
            raise ValueError("pending GitHub request has not been captured")
        if self._terminal:
            raise ValueError("pending GitHub request is already settled")
        self._terminal = True
        return self._invoke()

    def discard(self) -> None:
        if self._invoke is None:
            raise ValueError("pending GitHub request has not been captured")
        if self._terminal:
            raise ValueError("pending GitHub request is already settled")
        self._terminal = True


@dataclass
class _WriteFault:
    kind: str
    method: _Mutation
    pr: sync.PullRequestId | None
    pending: PendingRequest | None = None
    consumed: bool = False


class FakeGitHubClient:
    """GitHub client fake with transport and scheduling fault injection."""

    _MUTATIONS = {
        sync.GitHubClient.create_pull_request,
        sync.GitHubClient.update_pull_request,
    }

    def __init__(self, server: FakeGitHubServer) -> None:
        self._server = server
        self._write_fault: _WriteFault | None = None
        self._read_override: FakeGitHubServer | str | None = None

    @contextmanager
    def fail_before(
        self, method: _Mutation, *, pr: sync.PullRequestId | None = None
    ) -> Iterator[None]:
        with self._fault(_WriteFault("before", method, pr)):
            yield

    @contextmanager
    def lose_response(
        self, method: _Mutation, *, pr: sync.PullRequestId | None = None
    ) -> Iterator[None]:
        with self._fault(_WriteFault("after", method, pr)):
            yield

    @contextmanager
    def hold_next(
        self, method: _Mutation, *, pr: sync.PullRequestId | None = None
    ) -> Iterator[PendingRequest]:
        pending = PendingRequest()
        with self._fault(_WriteFault("hold", method, pr, pending)):
            yield pending

    @contextmanager
    def stale_reads(
        self, snapshot: FakeGitHubServer | None = None
    ) -> Iterator[None]:
        frozen = self._server.snapshot() if snapshot is None else snapshot.snapshot()
        with self._reads_from(frozen):
            yield

    @contextmanager
    def unavailable_reads(self) -> Iterator[None]:
        with self._reads_from("unavailable"):
            yield

    @contextmanager
    def _fault(self, fault: _WriteFault) -> Iterator[None]:
        if fault.method not in self._MUTATIONS:
            raise ValueError(f"unknown GitHub mutation {fault.method.__name__}")
        if (
            fault.pr is not None
            and fault.method is not sync.GitHubClient.update_pull_request
        ):
            raise ValueError("a pull request selector only applies to PR updates")
        if self._write_fault is not None:
            raise ValueError("a GitHub write fault is already active")
        self._write_fault = fault
        try:
            yield
            if not fault.consumed:
                raise AssertionError("configured GitHub write fault was not exercised")
        finally:
            if self._write_fault is fault:
                self._write_fault = None

    @contextmanager
    def _reads_from(self, source: FakeGitHubServer | str) -> Iterator[None]:
        if self._read_override is not None:
            raise ValueError("a GitHub read fault is already active")
        self._read_override = source
        try:
            yield
        finally:
            self._read_override = None

    def _reader(self) -> FakeGitHubServer:
        if self._read_override == "unavailable":
            raise sync.SourceUnavailable("injected GitHub read failure")
        return self._server if self._read_override is None else self._read_override

    def _mutation(
        self,
        method: _Mutation,
        pr: sync.PullRequestId | None,
        invoke: Callable[[], T],
    ) -> T:
        fault = self._write_fault
        if (
            fault is None
            or fault.consumed
            or fault.method != method
            or (fault.pr is not None and fault.pr != pr)
        ):
            return invoke()
        fault.consumed = True
        if fault.kind == "before":
            raise sync.GitHubTransportError("injected")
        if fault.kind == "hold":
            assert fault.pending is not None
            fault.pending._capture(invoke)
            raise sync.GitHubTransportError("injected-held")
        result = invoke()
        raise sync.GitHubTransportError("injected")

    def resolve_repository(
        self, locator: str | sync.GitHubRepositoryId
    ) -> sync.GitHubRepository:
        return self._reader().read_repository(locator)

    def pull_requests(
        self, identities: Sequence[sync.PullRequestId]
    ) -> tuple[sync.GitHubPullRequest, ...]:
        return self._reader().read_pull_requests(tuple(identities))

    def stack(
        self,
        repository: sync.GitHubRepository,
        identity: sync.GitHubStackId,
    ) -> sync.GitHubStack | None:
        return self._reader().read_stack(repository, identity)

    def find_pull_requests(
        self,
        repository: sync.GitHubRepositoryId,
        *,
        head_branches: Sequence[str] = (),
        base_branches: Sequence[str] = (),
        states: Collection[sync.PullRequestState] | None = None,
    ) -> tuple[sync.GitHubPullRequest, ...]:
        return self._reader().read_find_pull_requests(
            repository,
            head_branches=tuple(head_branches),
            base_branches=tuple(base_branches),
            states=None if states is None else tuple(states),
        )

    def create_pull_request(
        self,
        repository: sync.GitHubRepository,
        *,
        head_branch: str,
        base_branch: str,
        title: str,
        body: str,
        draft: bool,
    ) -> sync.PullRequestId:
        return self._mutation(
            sync.GitHubClient.create_pull_request,
            None,
            lambda: self._server.apply_create_pull_request(
                repository,
                head_branch=head_branch,
                base_branch=base_branch,
                title=title,
                body=body,
                draft=draft,
            ),
        )

    def update_pull_request(
        self,
        repository: sync.GitHubRepository,
        identity: sync.PullRequestId,
        *,
        title: str | None = None,
        body: str | None = None,
        base_branch: str | None = None,
        state: sync.PullRequestUpdateState | None = None,
    ) -> None:
        return self._mutation(
            sync.GitHubClient.update_pull_request,
            identity,
            lambda: self._server.apply_update_pull_request(
                repository,
                identity,
                title=title,
                body=body,
                base_branch=base_branch,
                state=state,
            ),
        )
