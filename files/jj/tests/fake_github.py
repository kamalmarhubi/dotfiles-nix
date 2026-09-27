from __future__ import annotations

import dataclasses
from collections.abc import Sequence

import jj_stack_sync as sync


class FakeGitHubServer:
    """Authoritative GitHub state and semantics for workflow tests."""

    def __init__(self) -> None:
        self._repositories: dict[sync.GitHubRepositoryId, sync.GitHubRepository] = {}
        self._locators: dict[str, sync.GitHubRepositoryId] = {}
        self._pull_requests: dict[sync.PullRequestId, sync.GitHubPullRequest] = {}
        self._stacks: dict[sync.GitHubStackId, sync.GitHubStack] = {}

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

    def seed_pull_request(self, pull_request: sync.GitHubPullRequest) -> None:
        if pull_request.identity.repository not in self._repositories:
            raise ValueError("pull request repository is not seeded")
        if pull_request.identity in self._pull_requests:
            raise ValueError("pull request is already seeded")
        if pull_request.stack is not None:
            raise ValueError("seed stack membership through seed_stack")
        self._pull_requests[pull_request.identity] = pull_request

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


class FakeGitHubClient:
    """Instrumentable client boundary over a ``FakeGitHubServer``."""

    def __init__(self, server: FakeGitHubServer) -> None:
        self._server = server

    def resolve_repository(
        self, locator: str | sync.GitHubRepositoryId
    ) -> sync.GitHubRepository:
        return self._server.read_repository(locator)

    def pull_requests(
        self, identities: Sequence[sync.PullRequestId]
    ) -> tuple[sync.GitHubPullRequest, ...]:
        return self._server.read_pull_requests(tuple(identities))

    def stack(
        self,
        repository: sync.GitHubRepository,
        identity: sync.GitHubStackId,
    ) -> sync.GitHubStack | None:
        return self._server.read_stack(repository, identity)
