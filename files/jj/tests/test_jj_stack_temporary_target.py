# /// script
# requires-python = ">=3.12"
# dependencies = ["markdown-it-py==4.2.0", "pytest"]
# ///
from __future__ import annotations

import dataclasses
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "bin"))
import jj_stack_sync as sync  # noqa: E402
import jj_stack_temporary_target as target  # noqa: E402
from fake_github import (  # noqa: E402
    FakeGitHubClient,
    FakeGitHubServer,
    FakeGitTransport,
)


def temporary_case(tmp_path: Path):
    workspace = tmp_path / "repo"
    subprocess.run(("jj", "git", "init", "--colocate", workspace),
                   check=True, capture_output=True)
    subprocess.run(("git", "-C", workspace, "remote", "add", "origin",
                    "/fake/repo.git"), check=True)
    subprocess.run(("jj", "--repository", workspace, "config", "set", "--repo",
                    "git.push", "origin"), check=True, capture_output=True)
    repository = sync.GitHubRepository(
        sync.GitHubRepositoryId("github.com", "R"), "owner/repo",
        "https://github.com/owner/repo", "main",
    )
    push_url = "/fake/repo.git"
    server = FakeGitHubServer()
    server.seed_repository(repository, aliases=(push_url,))
    for branch, oid in (("main", "0" * 40), ("one", "1" * 40),
                        ("two", "2" * 40), ("boundary", "5" * 40),
                        ("checks", "7" * 40)):
        server.seed_branch(repository.identity, branch, oid)
    one = sync.PullRequestId(repository.identity, 1)
    two = sync.PullRequestId(repository.identity, 2)
    prs = (
        sync.GitHubPullRequest(one, "one", sync.PullRequestState.OPEN, False,
            repository.identity, "one", "1" * 40, "main", "0" * 40,
            False, False, "One", "", None),
        sync.GitHubPullRequest(two, "two", sync.PullRequestState.OPEN, False,
            repository.identity, "two", "2" * 40, "one", "1" * 40,
            False, False, "Two", "", None),
    )
    for pr in prs:
        server.seed_pull_request(pr)
    participants = (
        sync.TemporaryTargetParticipant(
            "pr:1", "one", "3" * 40, "main", "main", "One", "", one,
            "One", ""),
        sync.TemporaryTargetParticipant(
            "pr:2", "two", "4" * 40, "one", "one", "Two", "", two,
            "Two", ""),
    )
    refs = tuple(sync.RemoteBranchRef(
        repository.identity, f"refs/heads/{name}"
    ) for name in ("one", "two", "boundary"))
    boundary_owner = sync.PullRequestId(repository.identity, 99)
    boundary = sync.LastPublishedBoundary(boundary_owner, refs[2], "6" * 40)
    updates = (
        sync.PlannedHeadUpdate(refs[0], "1" * 40, "3" * 40, sync.FastForward()),
        sync.PlannedHeadUpdate(refs[1], "2" * 40, "4" * 40, sync.FastForward()),
        sync.PlannedHeadUpdate(refs[2], "5" * 40, "6" * 40, sync.FastForward()),
    )
    obligation = sync.TemporaryTargetObligation(
        "obligation", repository.identity, push_url, "checks", ("ci",),
        participants,
        sync.TemporaryTargetSourceGuard(None, None, (one, two), prs), (),
        sync.TemporaryTargetExistingBatch(updates, (boundary,)),
        sync.TemporaryTargetCoreGoal(
            sync.EMPTY_STATE, "main", (boundary_owner,), ("pr:1", "pr:2"),
            (("pr:1", refs[0].full_name, "3" * 40),
             ("pr:2", refs[1].full_name, "4" * 40)),
            (), (), (boundary,),
        ),
    )
    return (workspace, repository, server, FakeGitHubClient(server),
            FakeGitTransport(server), obligation, refs, prs)


def test_parse_policy() -> None:
    assert target.parse_policy(None, None) is None
    assert target.parse_policy("required", '["build", "review"]') == target.Policy(
        "required", ("build", "review")
    )
    with pytest.raises(target.Error, match="together"):
        target.parse_policy("required", None)
    with pytest.raises(target.Error, match="nonempty string list"):
        target.parse_policy("required", "[]")
    with pytest.raises(target.Error, match="duplicate"):
        target.parse_policy("required", '["Build", "build"]')


def test_first_candidate_keeps_final_bases_and_unbound_slots() -> None:
    repository = sync.GitHubRepositoryId("github.com", "R")
    policy = target.Policy("checks", ("ci",))
    goals = (
        sync.NewPullRequestGoal("1" * 40, "one", "main", "One", ""),
        sync.NewPullRequestGoal("2" * 40, "two", "one", "Two", ""),
    )
    local = sync.LocalObservation(".", "op", (), (), (), (), (), (), ".git")
    plan = sync.FirstPublicationPlan(
        sync.FirstPublicationGoal(repository, "main", "0" * 40, goals),
        local, (), ())

    candidate = sync.temporary_target_candidate(
        plan, "push", sync.EMPTY_STATE, policy
    )

    assert candidate is not None
    assert tuple(item.pull_request for item in candidate.participants) == (None, None)
    assert tuple(item.final_base for item in candidate.participants) == ("main", "one")
    assert candidate.absent_branches == (("one", "1" * 40), ("two", "2" * 40))


def test_ordinary_authorities_and_candidate_roundtrip_are_exact() -> None:
    repository = sync.GitHubRepositoryId("github.com", "R")
    pr = sync.PullRequestId(repository, 1)
    ref = sync.RemoteBranchRef(repository, "refs/heads/one")
    publication = sync.LastPublishedHead(pr, ref, "1" * 40)
    participant = sync.TemporaryTargetParticipant(
        "pr:1", "one", "2" * 40, "main", "main", "One", "", pr)
    source_pr = sync.GitHubPullRequest(
        pr, "PR", sync.PullRequestState.OPEN, False, repository, "one", "1" * 40,
        "main", "0" * 40, False, False, "One", "", None)
    expected = sync.TrackedState((), (publication,))
    authorities = (
        sync.FastForward(), sync.MatchesLastPublication(publication),
        sync.MatchesLastAdoption(sync.LastAdoptedHead(pr, ref, "1" * 40)),
        sync.ExplicitLocalWins(),
    )
    updates = tuple(sync.PlannedHeadUpdate(ref, "1" * 40, "2" * 40, value)
                    for value in authorities)
    candidate = sync.TemporaryTargetObligation(
        "id", repository, "push", "checks", ("ci",), (participant,),
        sync.TemporaryTargetSourceGuard(None, None, (pr,), (source_pr,)), (),
        sync.TemporaryTargetExistingBatch(updates),
        sync.TemporaryTargetCoreGoal(
            expected, "main", (), ("pr:1",),
            (("pr:1", ref.full_name, "2" * 40),), (), (), ()))

    journal = sync.RecoveryJournal((), (candidate,))

    assert sync.parse_recovery(sync.recovery_to_json(journal)) == journal


def test_mixed_candidate_includes_unchanged_open_members() -> None:
    # The complete adapter behavior is covered through its invariant: source
    # members are participants even when no head update is needed.
    repository = sync.GitHubRepositoryId("github.com", "R")
    pr = sync.PullRequestId(repository, 1)
    observed = sync.GitHubPullRequest(
        pr, "PR", sync.PullRequestState.OPEN, False, repository, "one", "1" * 40,
        "main", "0" * 40, False, False, "One", "", None)
    assignment = sync.ExistingPRAssignment(pr, "1" * 40)
    plan = sync.MixedMembershipPlan(
        sync.MixedMembershipGoal(repository, "main", (pr,), ()), None, (observed,), (),
        (assignment,), (("One", ""),), (), ())

    candidate = sync.temporary_target_candidate(
        plan, "push", sync.EMPTY_STATE, target.Policy("checks", ("ci",))
    )

    assert candidate is not None
    assert tuple(item.pull_request for item in candidate.participants) == (pr,)
    assert candidate.existing_batch is None


def test_candidate_receipt_metadata_only_unowned_never_mints_from_equality() -> None:
    repository = sync.GitHubRepositoryId("github.com", "R")
    pr_id = sync.PullRequestId(repository, 1)
    pr = sync.GitHubPullRequest(
        pr_id, "node", sync.PullRequestState.OPEN, False, repository, "one", "1" * 40,
        "main", "0" * 40, False, False, "Old", "", None)
    desired = sync.DesiredStack(repository, "main", (
        sync.DesiredExistingPR(pr_id, "1" * 40, "New", ""),
    ))
    dependencies = sync.Dependencies(
        (), "push", "origin", None, None, (pr,), sync.StandalonePullRequest(pr_id), (), ())
    plan = sync.Apply(desired, (), (sync.PRMetadataUpdate(pr_id, "New", ""),), dependencies)

    candidate = sync.temporary_target_candidate(
        plan, "push", sync.EMPTY_STATE, target.Policy("checks", ("ci",)))

    assert candidate is not None
    assert candidate.core.publications == ()
    assert candidate.core.adoptions == ()


def test_candidate_receipt_one_changed_one_unchanged_filters_actual_written_slots() -> None:
    repository = sync.GitHubRepositoryId("github.com", "R")
    one, two = sync.PullRequestId(repository, 1), sync.PullRequestId(repository, 2)
    ref_one = sync.RemoteBranchRef(repository, "refs/heads/one")
    ref_two = sync.RemoteBranchRef(repository, "refs/heads/two")
    prs = (
        sync.GitHubPullRequest(one, "one", sync.PullRequestState.OPEN, False, repository,
            "one", "1" * 40, "main", "0" * 40, False, False, "One", "", None),
        sync.GitHubPullRequest(two, "two", sync.PullRequestState.OPEN, False, repository,
            "two", "2" * 40, "one", "1" * 40, False, False, "Two", "", None),
    )
    desired = sync.DesiredStack(repository, "main", (
        sync.DesiredExistingPR(one, "3" * 40, "One", ""),
        sync.DesiredExistingPR(two, "2" * 40, "Two", ""),
    ))
    dependencies = sync.Dependencies(
        (), "push", "origin", None, None, prs,
        sync.ServerStackMembership(one, sync.GitHubStackSummary(
            sync.GitHubStackId(repository, 9), "stack", "main"), (one, two)), (), ())
    update = sync.PlannedHeadUpdate(ref_one, "1" * 40, "3" * 40, sync.FastForward())
    old_publication = sync.LastPublishedHead(two, ref_two, "2" * 40)
    old_adoption = sync.LastAdoptedHead(two, ref_two, "2" * 40)
    state = sync.TrackedState((), (old_publication,), (old_adoption,))

    candidate = sync.temporary_target_candidate(
        sync.Apply(desired, (update,), (), dependencies), "push", state,
        target.Policy("checks", ("ci",)))

    assert candidate is not None
    assert candidate.core.publications == (
        ("pr:1", ref_one.full_name, "3" * 40),
        ("pr:2", ref_two.full_name, "2" * 40),
    )
    assert candidate.core.adoptions == (old_adoption,)


def test_obligation_publishes_observable_heads_then_durably_records_status_before_restore(
    tmp_path: Path,
) -> None:
    workspace, _repository, server, github, git, obligation, refs, _prs = (
        temporary_case(tmp_path)
    )
    sync.create_temporary_target_obligation(workspace, obligation)

    pending = sync.apply_temporary_target_obligation(
        workspace, github, obligation.identity, git_transport=git
    )

    assert pending == sync.Stopped(
        "temporary-target-statuses", "required statuses are pending"
    )
    assert tuple(item.commit_id for item in git.observe_live_refs(
        obligation.push_url, obligation.repository,
        tuple(ref.full_name for ref in refs),
    )) == ("3" * 40, "4" * 40, "6" * 40)
    assert tuple(pr.head_oid for pr in github.pull_requests(
        tuple(p.pull_request for p in obligation.participants
              if p.pull_request is not None)
    )) == ("3" * 40, "4" * 40)

    server.set_statuses(obligation.repository, "3" * 40, {"ci": "success"})
    server.set_statuses(obligation.repository, "4" * 40, {"ci": "success"})
    with github.hold_next(sync.GitHubClient.update_pull_request) as held_restore:
        stopped = sync.apply_temporary_target_obligation(
            workspace, github, obligation.identity, git_transport=git
        )
    assert isinstance(stopped, sync.Stopped)
    journal = sync.read_recovery(workspace)[1]
    assert journal is not None
    durable = next(item for item in journal.obligations
                   if item.identity == obligation.identity)
    assert durable.status_success == sync.StatusSuccess(
        ((obligation.participants[0].pull_request, "3" * 40),
         (obligation.participants[1].pull_request, "4" * 40)),
        ("ci",),
    )

    held_restore.release()
    result = sync.apply_temporary_target_obligation(
        workspace, github, obligation.identity, git_transport=git
    )
    assert isinstance(result, sync.Verified)
    actual = github.pull_requests(tuple(
        p.pull_request for p in obligation.participants
        if p.pull_request is not None
    ))
    assert tuple(pr.base_branch for pr in actual) == ("main", "one")
    state = sync.read_state(workspace)[1]
    assert state.stacks == (
        sync.TrackedStack(
            obligation.repository, "main",
            obligation.core.historical_members + tuple(
                p.pull_request for p in obligation.participants
                if p.pull_request is not None
            ),
        ),
    )
    assert {item.ref: item.verified_commit_id for item in state.last_published_heads} == {
        refs[0]: "3" * 40, refs[1]: "4" * 40,
    }
    assert state.last_published_boundaries == obligation.core.boundaries
    assert sync.read_recovery(workspace) == (None, None)


def test_lost_atomic_push_is_read_back_without_replay(tmp_path: Path) -> None:
    workspace, _repository, _server, github, git, obligation, refs, _prs = (
        temporary_case(tmp_path)
    )
    sync.create_temporary_target_obligation(workspace, obligation)

    with git.lose_response(sync.GitTransport.push_exact_head_updates):
        result = sync.apply_temporary_target_obligation(
            workspace, github, obligation.identity, git_transport=git
        )

    assert result.stage == "temporary-target-statuses"
    assert tuple(item.commit_id for item in git.observe_live_refs(
        obligation.push_url, obligation.repository,
        tuple(ref.full_name for ref in refs),
    )) == ("3" * 40, "4" * 40, "6" * 40)
    journal = sync.read_recovery(workspace)[1]
    assert journal is not None
    head = next(entry for entry in journal.entries
                if isinstance(entry.attempt, sync.HeadMutationAttempt))
    assert head.possibly_live and head.verified_applied


def test_unowned_integration_base_may_advance_before_temporary_targeting(
    tmp_path: Path,
) -> None:
    workspace, _repository, server, github, git, obligation, _refs, _prs = (
        temporary_case(tmp_path)
    )
    sync.create_temporary_target_obligation(workspace, obligation)
    server.move_branch(obligation.repository, "main", "9" * 40)

    result = sync.apply_temporary_target_obligation(
        workspace, github, obligation.identity, git_transport=git
    )

    assert result == sync.Stopped(
        "temporary-target-statuses", "required statuses are pending"
    )


@pytest.mark.parametrize("drift", ("base-name", "owned-base-oid"))
def test_source_guard_rejects_base_identity_and_owned_base_tip_drift(
    tmp_path: Path, drift: str,
) -> None:
    workspace, _repository, server, github, git, obligation, refs, prs = (
        temporary_case(tmp_path)
    )
    sync.create_temporary_target_obligation(workspace, obligation)
    # Even permissible integration drift must not mask the unsafe difference.
    server.move_branch(obligation.repository, "main", "9" * 40)
    if drift == "base-name":
        server.change_base(prs[0].identity, "checks", oid="7" * 40)
    else:
        # Change only the successor's observation, not its predecessor's head,
        # so rejection cannot be attributed to the existing foreign-head guard.
        server.change_base(prs[1].identity, "one", oid="8" * 40)

    before = sync.read_recovery(workspace)
    result = sync.apply_temporary_target_obligation(
        workspace, github, obligation.identity, git_transport=git
    )

    assert result == sync.Stopped("temporary-target", "source pull requests changed")
    assert sync.read_recovery(workspace) == before
    assert tuple(item.commit_id for item in git.observe_live_refs(
        obligation.push_url, obligation.repository,
        tuple(ref.full_name for ref in refs),
    )) == ("1" * 40, "2" * 40, "5" * 40)


@pytest.mark.parametrize("moved", ("one", "boundary"))
def test_stale_member_or_boundary_prevents_entire_atomic_batch(
    tmp_path: Path, moved: str,
) -> None:
    workspace, _repository, server, github, git, obligation, refs, _prs = (
        temporary_case(tmp_path)
    )
    sync.create_temporary_target_obligation(workspace, obligation)
    server.move_branch(obligation.repository, moved, "9" * 40)

    result = sync.apply_temporary_target_obligation(
        workspace, github, obligation.identity, git_transport=git
    )

    assert isinstance(result, sync.Stopped)
    expected = (("9" * 40, "2" * 40, "5" * 40) if moved == "one"
                else ("1" * 40, "2" * 40, "9" * 40))
    assert tuple(item.commit_id for item in git.observe_live_refs(
        obligation.push_url, obligation.repository,
        tuple(ref.full_name for ref in refs),
    )) == expected
    assert sync.read_state(workspace)[1] == sync.EMPTY_STATE
    journal = sync.read_recovery(workspace)[1]
    assert journal is not None
    assert not any(isinstance(entry.attempt, sync.HeadMutationAttempt)
                   and entry.verified_applied for entry in journal.entries)


def test_held_batch_rejects_late_member_movement_without_partial_publication(
    tmp_path: Path,
) -> None:
    workspace, _repository, server, github, git, obligation, refs, _prs = (
        temporary_case(tmp_path)
    )
    sync.create_temporary_target_obligation(workspace, obligation)
    with git.hold_next(sync.GitTransport.push_exact_head_updates) as pending:
        stopped = sync.apply_temporary_target_obligation(
            workspace, github, obligation.identity, git_transport=git
        )
    assert isinstance(stopped, sync.Stopped)
    server.move_branch(obligation.repository, "two", "9" * 40)
    with pytest.raises(sync.GitPushError, match="stale lease"):
        pending.release()

    assert tuple(item.commit_id for item in git.observe_live_refs(
        obligation.push_url, obligation.repository,
        tuple(ref.full_name for ref in refs),
    )) == ("1" * 40, "9" * 40, "5" * 40)
    retry = sync.apply_temporary_target_obligation(
        workspace, github, obligation.identity, git_transport=git
    )
    assert isinstance(retry, sync.Stopped)
    assert tuple(item.commit_id for item in git.observe_live_refs(
        obligation.push_url, obligation.repository,
        tuple(ref.full_name for ref in refs),
    )) == ("1" * 40, "9" * 40, "5" * 40)
    assert sync.read_state(workspace)[1] == sync.EMPTY_STATE


def test_member_already_at_desired_oid_does_not_manufacture_batch_authority(
    tmp_path: Path,
) -> None:
    workspace, _repository, server, github, git, obligation, refs, _prs = (
        temporary_case(tmp_path)
    )
    sync.create_temporary_target_obligation(workspace, obligation)
    server.move_branch(obligation.repository, "one", "3" * 40)

    result = sync.apply_temporary_target_obligation(
        workspace, github, obligation.identity, git_transport=git
    )

    assert isinstance(result, sync.Stopped)
    assert tuple(item.commit_id for item in git.observe_live_refs(
        obligation.push_url, obligation.repository,
        tuple(ref.full_name for ref in refs),
    )) == ("3" * 40, "2" * 40, "5" * 40)
    assert sync.read_state(workspace)[1] == sync.EMPTY_STATE


def test_pristine_obligation_is_retired_before_fresh_replanning(
    tmp_path: Path, monkeypatch,
) -> None:
    workspace, _repository, _server, github, git, obligation, _refs, _prs = (
        temporary_case(tmp_path)
    )
    sync.create_temporary_target_obligation(workspace, obligation)
    changed_selection = sync.LocalObservation(
        str(workspace),
        "operation",
        (("git.push", "origin"),),
        (),
        (),
        (),
        (),
        (
            sync.ObservedCommit("a" * 40, (), "change-a", "A", False, False),
            sync.ObservedCommit(
                "b" * 40, ("a" * 40,), "change-b", "B", False, False
            ),
        ),
        str(workspace / ".git"),
        ("origin",),
    )
    calls = []

    def replan(*_args, **_kwargs):
        calls.append(True)
        return sync.SyncCommandPlan(
            sync.Blocked((sync.Blocker("fresh", "candidate", "recomputed"),)),
            obligation.push_url,
        )

    monkeypatch.setattr(sync, "observe_local", lambda *_args, **_kwargs: changed_selection)
    monkeypatch.setattr(sync, "plan_sync_command", replan)
    command, result = sync.execute_sync_command(
        workspace, github, sync.NewSyncIntent("changed"),
        git_transport=git,
    )

    assert calls == [True]
    assert command is not None and isinstance(command.value, sync.Blocked)
    assert result == sync.Stopped("plan", "current intent is blocked")
    assert sync.read_recovery(workspace) == (None, None)


@pytest.mark.parametrize("dry_run", (False, True))
def test_pristine_retirement_preserves_live_verified_and_other_repository_work(
    tmp_path: Path, monkeypatch, dry_run: bool,
) -> None:
    workspace, repository, _server, github, git, _obligation, _refs, _prs = (
        temporary_case(tmp_path)
    )
    other_repository = sync.GitHubRepositoryId("github.com", "OTHER")
    obligations = []
    entries = []
    for name, owner, oid, possibly_live, verified in (
        ("pristine", repository.identity, "a" * 40, False, False),
        ("live", repository.identity, "b" * 40, True, False),
        ("verified", repository.identity, "c" * 40, False, True),
        ("other", other_repository, "d" * 40, False, False),
    ):
        participant = sync.TemporaryTargetParticipant(
            name, name, oid, "main", "main", name, ""
        )
        obligations.append(sync.TemporaryTargetObligation(
            name, owner, "/fake/other.git" if owner == other_repository
            else "/fake/repo.git", "checks", ("ci",), (participant,),
            sync.TemporaryTargetSourceGuard(None, None, ()), ((name, oid),),
            None, sync.TemporaryTargetCoreGoal(
                sync.EMPTY_STATE, "main", (), (name,), (), (), (), ()
            ),
        ))
        entries.append(sync.RecoveryEntry(
            f"entry-{name}", sync.BranchCreationAttempt(
                owner, obligations[-1].push_url,
                (sync.NewPullRequestGoal(oid, name, "checks", name, ""),),
            ), possibly_live=possibly_live, obligation=name,
            verified_applied=verified,
        ))
    original = sync.RecoveryJournal(tuple(entries), tuple(obligations))
    oid = sync.cas_write_recovery(workspace, None, original)
    expected = original if dry_run else sync.RecoveryJournal(
        tuple(entries[1:]), tuple(obligations[1:])
    )
    planned = []

    def replan(*_args, **_kwargs):
        # Check retirement before planning, not just the eventual final state.
        assert sync.read_recovery(workspace)[1] == expected
        planned.append(True)
        return sync.SyncCommandPlan(
            sync.Blocked((sync.Blocker("fresh", "candidate", "recomputed"),)),
            "/fake/repo.git",
        )

    monkeypatch.setattr(sync, "plan_sync_command", replan)
    command, result = sync.execute_sync_command(
        workspace, github, sync.NewSyncIntent("@"),
        git_transport=git, dry_run=dry_run,
    )

    assert planned == [True]
    assert command is not None and isinstance(command.value, sync.Blocked)
    assert result == sync.Stopped("plan", "current intent is blocked")
    assert sync.read_recovery(workspace)[1] == expected
    if dry_run:
        assert sync.read_recovery(workspace) == (oid, original)


def test_obligation_conflicts_are_resource_scoped_within_repository(
    tmp_path: Path,
) -> None:
    workspace, repository, _server, _github, _git, obligation, _refs, _prs = (
        temporary_case(tmp_path)
    )
    sync.create_temporary_target_obligation(workspace, obligation)
    unrelated = sync.PullRequestId(repository.identity, 50)
    sync._append_recovery(workspace, sync.MetadataMutationAttempt(
        repository.identity, sync.PRMetadataUpdate(unrelated, "Other", ""),
        "Old", "",
    ))
    with pytest.raises(sync.ConcurrentUpdate, match="overlapping"):
        sync._append_recovery(workspace, sync.MetadataMutationAttempt(
            repository.identity,
            sync.PRMetadataUpdate(obligation.source.members[0], "One", ""),
            "Old", "",
        ))


def test_delayed_grouping_remains_recoverable_and_fences_member_base_edits(
    tmp_path: Path,
) -> None:
    workspace, repository, server, github, git, obligation, _refs, _prs = (
        temporary_case(tmp_path)
    )
    sync.create_temporary_target_obligation(workspace, obligation)
    for oid in ("3" * 40, "4" * 40):
        server.set_statuses(obligation.repository, oid, {"ci": "success"})

    with github.hold_next(sync.GitHubClient.create_stack) as pending:
        result = sync.apply_temporary_target_obligation(
            workspace, github, obligation.identity, git_transport=git
        )

    assert isinstance(result, sync.Verified)
    journal = sync.read_recovery(workspace)[1]
    assert journal is not None and journal.obligations == ()
    grouping = next(entry for entry in journal.entries
                    if isinstance(entry.attempt, sync.MixedGroupingAttempt))
    assert grouping.possibly_live
    first = obligation.participants[0]
    with pytest.raises(sync.ConcurrentUpdate, match="conflicting legacy"):
        sync.create_temporary_target_obligation(
            workspace,
            dataclasses.replace(
                obligation, identity="later",
                source=sync.TemporaryTargetSourceGuard(
                    None, None, (first.pull_request,),
                    github.pull_requests((first.pull_request,)),
                ),
                participants=(first,), existing_batch=None,
                core=sync.TemporaryTargetCoreGoal(
                    sync._temporary_target_projection(
                        sync.read_state(workspace)[1], repository.identity,
                        (first.pull_request,),
                    ),
                    "main", (), (first.slot,), (), (), (), (),
                ),
            ),
        )
    pending.release()
    assert sync.settle_recovery(workspace, github, git_transport=git) == ()
    assert sync.read_recovery(workspace) == (None, None)
