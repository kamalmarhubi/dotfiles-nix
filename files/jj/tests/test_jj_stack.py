# /// script
# requires-python = ">=3.12"
# dependencies = ["markdown-it-py==4.2.0", "pytest"]
# ///
import importlib.machinery
import importlib.util
import shlex
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "files" / "jj" / "jj-stack"


def load_script():
    loader = importlib.machinery.SourceFileLoader("jj_stack", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_structured_jj_output_disables_forced_color(tmp_path, monkeypatch):
    script = load_script()
    repo = tmp_path / "repo"
    subprocess.run(
        ["jj", "git", "init", "--colocate", str(repo)],
        text=True,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        [
            "jj",
            "--repository",
            str(repo),
            "config",
            "set",
            "--repo",
            "ui.color",
            "always",
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["jj", "--repository", str(repo), "bookmark", "create", "-r", "@", "topic"],
        text=True,
        capture_output=True,
        check=True,
    )
    monkeypatch.setenv("JJ_WORKSPACE_ROOT", str(repo))
    script.JJ = script.jj_command()

    operation = script.begin_inspection_phase()
    workspaces = script.workspace_targets()
    bookmarks = script.local_bookmark_targets()
    target = bookmarks[0][1]

    assert script.JJ == ["jj", "--color=never", "--repository", str(repo)]
    assert "\x1b" not in operation
    assert operation and int(operation, 16) >= 0
    assert workspaces == (("default", target),)
    assert bookmarks == (("topic", target),)
    assert "\x1b" not in target


@pytest.mark.parametrize("response", ["null", "[]", '"text"', "1"])
def test_graphql_rejects_non_object_json(monkeypatch, response):
    script = load_script()
    monkeypatch.setattr(
        script,
        "command",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 1, response, "upstream failure"
        ),
    )

    with pytest.raises(script.Error, match="could not query GitHub: upstream failure"):
        script.graphql(
            "query { viewer { login } }", {}, {}, ".", "could not query GitHub"
        )


def revision(script, commit, local=(), remote=(), generated=None):
    return script.Revision(
        commit,
        f"change-{commit}",
        tuple(local),
        tuple(remote),
        f"Title {commit}",
        f"Body {commit}",
        generated or f"push-{commit}",
    )


def pull_request(
    script,
    head,
    number,
    *,
    state="OPEN",
    merged=False,
    draft=True,
    stack_id="STACK_7",
    stack_number=7,
    base="main",
    base_oid="base",
    head_oid=None,
    commits=1,
):
    return script.PullRequest(
        f"PR_{number}",
        number,
        f"https://github.com/owner/repo/pull/{number}",
        state,
        merged,
        draft,
        f"Title {head}",
        f"Body {head}",
        head,
        head_oid or head,
        base,
        base_oid,
        commits,
        stack_id,
        stack_number,
        repository="owner/repo",
        head_repository="owner/repo",
    )


def pull_request_node(
    head,
    number,
    *,
    state="MERGED",
    merged=True,
    stack_id="STACK_7",
    stack_number=7,
    historical_oid=None,
):
    return {
        "id": f"PR_{number}",
        "number": number,
        "url": f"https://github.com/owner/repo/pull/{number}",
        "state": state,
        "merged": merged,
        "isDraft": False,
        "title": f"Title {head}",
        "body": f"Body {head}",
        "headRefName": head,
        "headRefOid": head,
        "baseRefName": "main",
        "baseRefOid": "base",
        "commits": {"totalCount": 1},
        "historical": {"nodes": [{"commit": {"oid": historical_oid or head}}]},
        "repository": {"nameWithOwner": "owner/repo"},
        "headRepository": {"nameWithOwner": "owner/repo"},
        "stack": (
            {"id": stack_id, "number": stack_number} if stack_id is not None else None
        ),
        "autoMergeRequest": None,
        "mergeQueueEntry": None,
    }


def test_discovers_merged_selector_by_historical_oid_not_live_head(monkeypatch):
    script = load_script()
    node = pull_request_node("main", 92, historical_oid="old-head")
    monkeypatch.setattr(
        script,
        "graphql",
        lambda *_args: {
            "repository": {
                "object": {
                    "associatedPullRequests": {
                        "nodes": [node],
                        "pageInfo": {"hasNextPage": False},
                    }
                }
            }
        },
    )

    selectors = script.discover_merged_selectors("owner/repo", ["old-head"], {}, ".")

    assert selectors["old-head"][0].number == 92
    assert selectors["old-head"][0].head_oid == "main"


def test_rejects_oid_that_is_both_open_boundary_and_merged_selector(monkeypatch):
    script = load_script()
    merged = pull_request_node("merged", 1, historical_oid="same")
    opened = pull_request_node(
        "same", 2, state="OPEN", merged=False, historical_oid="same"
    )
    monkeypatch.setattr(
        script,
        "graphql",
        lambda *_args: {
            "repository": {
                "object": {
                    "associatedPullRequests": {
                        "nodes": [merged, opened],
                        "pageInfo": {"hasNextPage": False},
                    }
                }
            }
        },
    )

    with pytest.raises(script.Error, match="ambiguously both"):
        script.discover_merged_selectors("owner/repo", ["same"], {}, ".")


def test_base_transition_history_is_paginated(monkeypatch):
    script = load_script()
    pr = pull_request(script, "topic", 2, stack_id=None, stack_number=None)
    responses = iter(
        [
            {
                "node": {
                    "timelineItems": {
                        "nodes": [
                            {
                                "id": "EVENT_1",
                                "createdAt": "2026-01-01T00:00:00Z",
                                "previousRefName": "main",
                                "currentRefName": "merged-a",
                            }
                        ],
                        "pageInfo": {"hasNextPage": True, "endCursor": "NEXT"},
                    }
                }
            },
            {
                "node": {
                    "timelineItems": {
                        "nodes": [
                            {
                                "id": "EVENT_2",
                                "createdAt": "2026-01-02T00:00:00Z",
                                "previousRefName": "merged-a",
                                "currentRefName": "main",
                            }
                        ],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            },
        ]
    )
    cursors = []

    def graphql(_query, variables, *_args):
        cursors.append(variables["cursor"])
        return next(responses)

    monkeypatch.setattr(script, "graphql", graphql)

    transitions = script.pull_request_base_transitions(pr, {}, ".")

    assert [item.node_id for item in transitions] == ["EVENT_1", "EVENT_2"]
    assert cursors == [None, "NEXT"]


def test_recovers_unique_surviving_merged_stack(monkeypatch):
    script = load_script()
    first = pull_request(
        script,
        "b",
        2,
        stack_id=None,
        stack_number=None,
        base="merged-a",
        base_oid="base",
    )
    second = pull_request(
        script,
        "c",
        3,
        stack_id=None,
        stack_number=None,
        base="b",
        base_oid="b",
    )
    boundary = pull_request(
        script, "merged-a", 1, state="MERGED", merged=True, head_oid="old-a"
    )
    surviving = script.StackSnapshot("STACK_7", 7, [boundary])
    transition = script.BaseTransition(
        "EVENT_1", "2026-01-01T00:00:00Z", "main", "merged-a"
    )
    monkeypatch.setattr(
        script, "pull_request_base_transitions", lambda *_args: (transition,)
    )
    monkeypatch.setattr(script, "merged_pull_request_by_head", lambda *_args: boundary)
    monkeypatch.setattr(script, "fetch_stack", lambda *_args: surviving)
    repo = script.Repository(
        ".",
        "owner/repo",
        "owner/repo",
        "github.com",
        "main",
        "https://github.com/owner/repo",
    )

    recovered = script.recover_surviving_stack([first, second], repo, {})

    assert recovered is not None
    stack, evidence = recovered
    assert stack is surviving
    assert evidence.stack_number == 7
    assert evidence.boundary is boundary
    assert evidence.transition is transition


def test_recovery_refuses_ambiguous_base_history(monkeypatch):
    script = load_script()
    first = pull_request(
        script,
        "b",
        2,
        stack_id=None,
        stack_number=None,
        base="merged-a",
    )
    transitions = (
        script.BaseTransition("E1", "one", "main", "merged-a"),
        script.BaseTransition("E2", "two", "merged-a", "main"),
    )
    monkeypatch.setattr(
        script, "pull_request_base_transitions", lambda *_args: transitions
    )
    repo = script.Repository(
        ".",
        "owner/repo",
        "owner/repo",
        "github.com",
        "main",
        "https://github.com/owner/repo",
    )

    with pytest.raises(script.Error, match="multiple recorded base changes"):
        script.recover_surviving_stack([first], repo, {})


def test_selectors_retain_complete_prefix_and_enforce_comparable_order(monkeypatch):
    script = load_script()
    a = pull_request(script, "a", 1, state="MERGED", merged=True)
    omitted = pull_request(script, "omitted", 2, state="MERGED", merged=True)
    c = pull_request(script, "c", 3, state="MERGED", merged=True)
    complete = script.StackSnapshot("STACK_7", 7, [a, omitted, c])
    monkeypatch.setattr(script, "fetch_stack", lambda *_args: complete)
    monkeypatch.setattr(script, "revision_ids", lambda _revset: [])

    selected = script.selector_stack({"a": (a,), "c": (c,)}, {}, ".")

    assert selected is complete
    assert selected.merged == [a, omitted, c]

    reversed_stack = script.StackSnapshot("STACK_7", 7, [c, a])
    monkeypatch.setattr(script, "fetch_stack", lambda *_args: reversed_stack)
    monkeypatch.setattr(
        script,
        "revision_ids",
        lambda revset: ["a"] if revset == "a & ::c" else [],
    )
    with pytest.raises(script.Error, match="contradict GitHub stack order"):
        script.selector_stack({"a": (a,), "c": (c,)}, {}, ".")


def test_resolves_existing_pr_before_other_local_bookmarks():
    script = load_script()
    item = revision(
        script,
        "a",
        local=("new-alias", "published"),
        remote=("published",),
    )
    pr = pull_request(script, "published", 1, head_oid="a")

    heads, prs = script.resolve_heads([item], {"published": pr})

    assert heads == ["published"]
    assert prs == {"published": pr}


def test_resolve_heads_uses_local_then_generated_and_rejects_ambiguity():
    script = load_script()

    heads, _ = script.resolve_heads(
        [
            revision(script, "a", local=("feature/a",)),
            revision(script, "b", generated="push-b"),
        ],
        {},
    )
    assert heads == ["feature/a", "push-b"]

    with pytest.raises(script.Error, match="ambiguous local bookmarks"):
        script.resolve_heads(
            [revision(script, "a", local=("one", "two"))],
            {},
        )


def test_rejects_base_and_unrelated_remote_ref_heads():
    script = load_script()
    item = revision(script, "selected", local=("main",))
    with pytest.raises(script.Error, match="base branch"):
        script.validate_head_ref_collisions(
            [item], ["main"], {}, {"main": "base"}, "main"
        )

    item = revision(script, "selected", local=("colleague",))
    with pytest.raises(script.Error, match="unrelated remote branch"):
        script.validate_head_ref_collisions(
            [item], ["colleague"], {}, {"colleague": "other"}, "main"
        )

    script.validate_head_ref_collisions(
        [item], ["colleague"], {}, {"colleague": "selected"}, "main"
    )
    pr = pull_request(script, "colleague", 1, head_oid="other")
    script.validate_head_ref_collisions(
        [item], ["colleague"], {"colleague": pr}, {"colleague": "other"}, "main"
    )


@pytest.mark.parametrize(
    ("existing", "merged", "selected", "kind", "link_heads"),
    [
        ([], [], ["a", "b"], "NEW", ["a", "b"]),
        (["a", "b"], [], ["a", "b"], "METADATA_ONLY", []),
        (["a"], [], ["a", "b"], "APPEND", ["b"]),
        (["a", "b"], [], ["b", "a"], "FULLY_OPEN_REBUILD", ["b", "a"]),
        (
            ["a", "b"],
            ["merged"],
            ["b", "a"],
            "MERGED_PREFIX_REBUILD",
            ["b", "a"],
        ),
    ],
)
def test_reconciliation_plans(existing, merged, selected, kind, link_heads):
    script = load_script()
    merged_prs = [
        pull_request(script, head, index + 1, state="MERGED", merged=True)
        for index, head in enumerate(merged)
    ]
    open_prs = [
        pull_request(script, head, index + 10) for index, head in enumerate(existing)
    ]
    stack = script.StackSnapshot(
        "STACK_7" if existing or merged else None,
        7 if existing or merged else None,
        [*merged_prs, *open_prs],
        {pr.head: pr for pr in open_prs},
    )
    revisions = [revision(script, head, local=(head,)) for head in selected]

    plan = script.make_plan(revisions, selected, stack, "base")

    assert plan.kind is getattr(script.TopologyKind, kind)
    assert plan.link_heads == link_heads
    assert plan.boundary == (
        merged_prs[-1] if merged_prs and kind == "MERGED_PREFIX_REBUILD" else None
    )


def test_append_after_fully_merged_stack_uses_boundary_anchor():
    script = load_script()
    merged = pull_request(script, "merged", 1, state="MERGED", merged=True)
    stack = script.StackSnapshot("STACK_7", 7, [merged], {})
    revisions = [
        revision(script, "a", local=("a",)),
        revision(script, "b", local=("b",)),
    ]

    plan = script.make_plan(revisions, ["a", "b"], stack, "base")

    assert plan.kind is script.TopologyKind.APPEND
    assert plan.boundary == merged
    assert plan.link_heads == ["a", "b"]


def test_all_merged_selection_is_noop_or_detaches_open_suffix():
    script = load_script()
    merged = pull_request(script, "merged", 1, state="MERGED", merged=True)

    noop = script.make_plan(
        [], [], script.StackSnapshot("STACK_7", 7, [merged]), "base"
    )
    assert noop.kind is script.TopologyKind.METADATA_ONLY
    assert noop.omitted == []

    opened = pull_request(script, "open", 2)
    detach = script.make_plan(
        [],
        [],
        script.StackSnapshot("STACK_7", 7, [merged, opened]),
        "base",
    )
    assert detach.kind is script.TopologyKind.MERGED_PREFIX_REBUILD
    assert detach.omitted == [opened]
    assert detach.boundary is None


def test_metadata_update_preserves_existing_first_open_base():
    script = load_script()
    merged = pull_request(script, "publish-merge-x", 58, state="MERGED", merged=True)
    current = pull_request(script, "c", 53, base="main")
    stack = script.StackSnapshot("STACK_55", 55, [merged, current], {"c": current})
    plan = script.make_plan(
        [revision(script, "c", local=("c",))],
        ["c"],
        stack,
        "base",
    )

    assert plan.kind is script.TopologyKind.METADATA_ONLY
    assert script.expected_first_open_base(plan, stack, "main") == "main"


def test_stale_merged_boundary_rebuilds_before_refreshing_base():
    script = load_script()
    merged = pull_request(script, "merged", 58, state="MERGED", merged=True)
    current = pull_request(script, "c", 53, base="merged", base_oid="old-base")
    stack = script.StackSnapshot("STACK_55", 55, [merged, current], {"c": current})

    plan = script.make_plan(
        [revision(script, "c", local=("c",))],
        ["c"],
        stack,
        "new-base",
    )

    assert plan.kind is script.TopologyKind.MERGED_PREFIX_REBUILD
    assert plan.boundary == merged
    assert plan.refresh_base
    assert plan.link_heads == ["c"]
    assert script.link_command(
        plan,
        stack,
        script.Repository(".", "owner/repo", "owner/repo", "github.com", "main", "url"),
        "origin",
    ) == [
        "gh",
        "stack",
        "link",
        "--remote",
        "origin",
        "55",
        "--",
        current.url,
    ]


def test_plan_lists_omitted_open_prs():
    script = load_script()
    a = pull_request(script, "a", 1)
    omitted = pull_request(script, "omitted", 2)
    b = pull_request(script, "b", 3)
    stack = script.StackSnapshot("STACK_7", 7, [a, omitted, b], {"a": a, "b": b})

    plan = script.make_plan(
        [revision(script, "a", local=("a",)), revision(script, "b", local=("b",))],
        ["a", "b"],
        stack,
        "base",
    )

    assert plan.kind is script.TopologyKind.FULLY_OPEN_REBUILD
    assert plan.omitted == [omitted]


def test_unstack_guard_is_reapplied_to_fresh_auto_merge_and_queue_state():
    script = load_script()
    safe = pull_request(script, "safe", 1)
    plan = script.Plan(
        script.TopologyKind.FULLY_OPEN_REBUILD,
        ["safe"],
        [],
        ["safe"],
        7,
        None,
        False,
    )
    script.verify_unstackable(plan, script.StackSnapshot("STACK_7", 7, [safe]))

    auto = script.PullRequest(**{**safe.__dict__, "auto_merge": True})
    with pytest.raises(script.Error, match=r"auto-merge or merge queue.*#1"):
        script.verify_unstackable(plan, script.StackSnapshot("STACK_7", 7, [auto]))

    queued = script.PullRequest(**{**safe.__dict__, "merge_queue": True})
    with pytest.raises(script.Error, match=r"auto-merge or merge queue.*#1"):
        script.verify_unstackable(plan, script.StackSnapshot("STACK_7", 7, [queued]))


def test_topology_delta_tracks_new_omitted_and_reordered_prs():
    script = load_script()
    a = pull_request(script, "a", 1)
    b = pull_request(script, "b", 2)
    c = pull_request(script, "c", 3)
    complete = script.StackSnapshot("STACK_7", 7, [a, b, c], {"a": a, "b": b, "c": c})

    assert not script.has_topology_delta(["a", "b", "c"], complete)
    assert script.has_topology_delta(["a", "x", "c"], complete)
    assert script.has_topology_delta(["a", "c"], complete)
    assert script.has_topology_delta(["c", "b", "a"], complete)

    state = script.BoundaryState("c", "local-c", "tracked-c", "remote-c", c)
    assert (
        script.classify_reconciliation(
            (state,), has_new_boundaries=True, topology_changed=True
        )
        is script.ReconciliationKind.LOCAL_AUTHORITY
    )


def test_omission_plan_prints_validated_consequences(capsys):
    script = load_script()
    omitted = pull_request(script, "topic", 12, head_oid="live-oid")
    omitted = script.PullRequest(
        **{
            **omitted.__dict__,
            "title": "Title\nwith escape \x1b[31m",
        }
    )
    stack = script.StackSnapshot("STACK_7", 7, [omitted])
    plan = script.Plan(
        script.TopologyKind.FULLY_OPEN_REBUILD,
        [],
        [omitted],
        [],
        7,
        None,
        False,
    )
    evidence = (
        script.ExistingBoundary(
            omitted, "live-oid", "live-oid", script.RemoteCheck.EXACT
        ),
    )
    repo = script.Repository(
        ".",
        "owner/repo",
        "owner/repo",
        "github.com",
        "main",
        "https://github.com/owner/repo",
    )

    script.print_plan(
        plan,
        [],
        stack,
        repo,
        "origin",
        None,
        "base",
        {},
        verbosity=1,
        existing_evidence=evidence,
        close_numbers=frozenset({12}),
    )

    output = capsys.readouterr().err
    assert "#12 Title\\nwith escape \\u001b[31m" in output
    assert "action: detach from stack; close after successful publication" in output
    assert "branch: retain topic @ live-oid" in output
    assert "remote check: live head matches tracked baseline" in output
    assert "do not prove its changes are present" in output


def test_closed_unmerged_entry_forces_merged_prefix_rebuild():
    script = load_script()
    merged = pull_request(script, "merged", 1, state="MERGED", merged=True)
    closed = pull_request(script, "closed", 2, state="CLOSED", stack_id="STACK_7")
    a = pull_request(script, "a", 3)
    b = pull_request(script, "b", 4)
    stack = script.StackSnapshot(
        "STACK_7",
        7,
        [merged, closed, a, b],
        {"a": a, "b": b},
    )

    plan = script.make_plan(
        [revision(script, "a", local=("a",)), revision(script, "b", local=("b",))],
        ["a", "b"],
        stack,
        "base",
    )

    assert plan.kind is script.TopologyKind.MERGED_PREFIX_REBUILD
    assert plan.boundary == merged
    assert plan.link_heads == ["a", "b"]


@pytest.mark.parametrize(
    ("local", "tracked", "live", "new", "expected"),
    [
        ("same", "same", "same", False, "ALREADY_CONVERGED"),
        ("same", "same", "same", True, "NORMAL_PUBLISH"),
        ("old", "old", "restacked", False, "REMOTE_ADOPT"),
        ("amended", "old", "old", False, "LOCAL_AUTHORITY"),
        ("amended", "old", "restacked", False, "LOCAL_AUTHORITY"),
    ],
)
def test_classifies_complete_suffix_authority(local, tracked, live, new, expected):
    script = load_script()
    pr = pull_request(script, "topic", 1, head_oid=live)
    state = script.BoundaryState("topic", local, tracked, live, pr)

    kind = script.classify_reconciliation((state,), has_new_boundaries=new)

    assert kind is getattr(script.ReconciliationKind, expected)


def test_missing_tracking_baseline_only_allows_synchronized_clone():
    script = load_script()
    pr = pull_request(script, "topic", 1, head_oid="same")
    synchronized = script.BoundaryState("topic", "same", None, "same", pr)
    assert (
        script.classify_reconciliation((synchronized,), has_new_boundaries=False)
        is script.ReconciliationKind.ALREADY_CONVERGED
    )

    ambiguous = script.BoundaryState("topic", "local", None, "remote", pr)
    with pytest.raises(script.Error, match="no usable pre-fetch tracking baseline"):
        script.classify_reconciliation((ambiguous,), has_new_boundaries=False)


def test_rejects_concurrent_stack_membership_or_order_change():
    script = load_script()
    a = pull_request(script, "a", 1)
    b = pull_request(script, "b", 2, base="a", base_oid="a")
    inserted = pull_request(script, "inserted", 3)
    before = script.StackSnapshot("STACK_7", 7, [a, b])

    script.verify_stack_snapshot_unchanged(
        before, script.StackSnapshot("STACK_7", 7, [a, b])
    )
    with pytest.raises(script.Error, match="membership, order, state, or refs changed"):
        script.verify_stack_snapshot_unchanged(
            before, script.StackSnapshot("STACK_7", 7, [a, inserted, b])
        )
    with pytest.raises(script.Error, match="membership, order, state, or refs changed"):
        script.verify_stack_snapshot_unchanged(
            before, script.StackSnapshot("STACK_7", 7, [b, a])
        )
    auto = script.PullRequest(**{**a.__dict__, "auto_merge": True})
    with pytest.raises(script.Error, match="membership, order, state, or refs changed"):
        script.verify_stack_snapshot_unchanged(
            before, script.StackSnapshot("STACK_7", 7, [auto, b])
        )
    queued = script.PullRequest(**{**a.__dict__, "merge_queue": True})
    with pytest.raises(script.Error, match="membership, order, state, or refs changed"):
        script.verify_stack_snapshot_unchanged(
            before, script.StackSnapshot("STACK_7", 7, [queued, b])
        )

    rewritten_a = pull_request(script, "a", 1, head_oid="new-a", base_oid="new-main")
    rewritten_b = pull_request(
        script, "b", 2, head_oid="new-b", base="a", base_oid="new-a"
    )
    script.verify_stack_snapshot_unchanged(
        before,
        script.StackSnapshot("STACK_7", 7, [rewritten_a, rewritten_b]),
        {"main": "new-main", "a": "new-a", "b": "new-b"},
        {"main": "new-main", "a": "new-a", "b": "new-b"},
    )


def test_snapshot_normalizes_only_active_pr_refs_after_boundary_publication():
    script = load_script()
    merged = pull_request(
        script,
        "a",
        1,
        state="MERGED",
        merged=True,
        head_oid="historical-a",
        base="main",
        base_oid="historical-main",
    )
    omitted = pull_request(
        script, "b", 2, head_oid="live-b", base="a", base_oid="historical-a"
    )
    retained = pull_request(
        script, "c", 3, head_oid="old-c", base="b", base_oid="live-b"
    )
    before = script.StackSnapshot("STACK_7", 7, [merged, omitted, retained])
    after_omitted = script.PullRequest(**{**omitted.__dict__, "base_oid": "new-main"})
    after_retained = script.PullRequest(**{**retained.__dict__, "head_oid": "new-c"})
    after = script.StackSnapshot("STACK_7", 7, [merged, after_omitted, after_retained])
    expected_refs = {"main": "new-main", "a": "new-main", "c": "new-c"}

    script.verify_stack_snapshot_unchanged(before, after, expected_refs, expected_refs)

    changed_merged = script.PullRequest(**{**merged.__dict__, "head_oid": "new-main"})
    with pytest.raises(script.Error, match="membership, order, state, or refs changed"):
        script.verify_stack_snapshot_unchanged(
            before,
            script.StackSnapshot(
                "STACK_7", 7, [changed_merged, after_omitted, after_retained]
            ),
            expected_refs,
            expected_refs,
        )


def test_snapshot_preserves_pending_first_base_refresh():
    script = load_script()
    merged = pull_request(
        script,
        "a",
        1,
        state="MERGED",
        merged=True,
        head_oid="historical-a",
        base_oid="historical-main",
    )
    opened = pull_request(
        script,
        "b",
        2,
        head_oid="old-b",
        base="a",
        base_oid="historical-a",
    )
    before = script.StackSnapshot("STACK_7", 7, [merged, opened])
    published = script.PullRequest(**{**opened.__dict__, "head_oid": "new-b"})
    after = script.StackSnapshot("STACK_7", 7, [merged, published])
    head_refs = {"a": "new-main", "b": "new-b"}
    base_refs = {"b": "new-b"}

    script.verify_stack_snapshot_unchanged(before, after, head_refs, base_refs)

    prematurely_refreshed = script.PullRequest(
        **{**published.__dict__, "base_oid": "new-main"}
    )
    with pytest.raises(script.Error, match="membership, order, state, or refs changed"):
        script.verify_stack_snapshot_unchanged(
            before,
            script.StackSnapshot("STACK_7", 7, [merged, prematurely_refreshed]),
            head_refs,
            base_refs,
        )


def test_rejects_live_ref_movement_after_authority_classification():
    script = load_script()
    pr = pull_request(script, "topic", 1, head_oid="original-r")
    state = script.BoundaryState("topic", "local", "tracked", "original-r", pr)

    script.verify_live_boundary_refs_unchanged((state,), {"topic": "original-r"})
    with pytest.raises(script.Error, match="after authority classification"):
        script.verify_live_boundary_refs_unchanged((state,), {"topic": "concurrent-r"})


def test_open_suffix_root_uses_immutable_merged_head():
    script = load_script()
    merged = pull_request(
        script,
        "merged",
        1,
        state="MERGED",
        merged=True,
        head_oid="historical-merged-head",
    )

    assert (
        script.open_suffix_root(
            script.StackSnapshot("STACK_7", 7, [merged]), "current-main"
        )
        == "historical-merged-head"
    )
    assert (
        script.open_suffix_root(script.StackSnapshot(None, None, []), "current-main")
        == "current-main"
    )


def test_atomic_publication_uses_full_exact_leases_before_refspecs():
    script = load_script()
    updates = (
        script.RefUpdate("b", "r-b", "new-b"),
        script.RefUpdate("c", "r-c", "new-c"),
    )

    command = script.atomic_push_command("/git", "origin", updates)

    assert command == [
        "git",
        "-C",
        "/git",
        "push",
        "origin",
        "--atomic",
        "--force-with-lease=refs/heads/b:r-b",
        "--force-with-lease=refs/heads/c:r-c",
        "new-b:refs/heads/b",
        "new-c:refs/heads/c",
    ]

    preflight = script.atomic_push_command("/git", "origin", updates, dry_run=True)
    assert preflight[6:8] == ["--dry-run", "--no-verify"]


def test_selected_head_ref_must_still_match_authorized_snapshot():
    script = load_script()
    script.verify_planned_head_refs_unchanged(
        {"existing": "old", "new": None}, {"existing": "old"}
    )
    with pytest.raises(script.Error, match="selected remote refs changed"):
        script.verify_planned_head_refs_unchanged(
            {"existing": "old", "new": None},
            {"existing": "old", "new": "concurrent"},
        )


def test_atomic_preflight_does_not_run_pre_push_hook(tmp_path):
    script = load_script()
    remote = tmp_path / "remote.git"
    source = tmp_path / "source"
    sentinel = tmp_path / "hook-ran"

    def git(*args, cwd=source):
        return subprocess.run(
            ["git", *args], cwd=cwd, text=True, capture_output=True, check=True
        ).stdout.strip()

    git("init", "--bare", str(remote), cwd=tmp_path)
    git("init", str(source), cwd=tmp_path)
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.com")
    (source / "file").write_text("base\n")
    git("add", "file")
    git("commit", "-m", "base")
    old = git("rev-parse", "HEAD")
    git("remote", "add", "origin", str(remote))
    git("push", "origin", f"{old}:refs/heads/topic")
    (source / "file").write_text("base\nnew\n")
    git("commit", "-am", "new")
    new = git("rev-parse", "HEAD")
    hook = source / ".git" / "hooks" / "pre-push"
    hook.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(sentinel))}\n")
    hook.chmod(0o755)

    subprocess.run(
        script.atomic_push_command(
            str(source),
            "origin",
            (script.RefUpdate("topic", old, new),),
            dry_run=True,
        ),
        text=True,
        capture_output=True,
        check=True,
    )

    assert not sentinel.exists()
    assert git("ls-remote", "origin", "refs/heads/topic").split()[0] == old


def test_patch_id_disables_replace_refs_and_external_diff(monkeypatch):
    script = load_script()
    calls = []

    def command(args, **_kwargs):
        calls.append(args)
        output = "patch\n" if "show" in args else "patch-id oid\n"
        return subprocess.CompletedProcess(args, 0, output, "")

    monkeypatch.setattr(script, "command", command)

    assert script.git_patch_id("/git", "oid") == "patch-id"
    assert calls[0] == [
        "git",
        "--no-replace-objects",
        "-C",
        "/git",
        "show",
        "--pretty=format:",
        "--binary",
        "--no-ext-diff",
        "--no-textconv",
        "oid",
    ]


def test_git_change_id_ignores_message_body(monkeypatch):
    script = load_script()
    commit = (
        "tree tree\n"
        "parent parent\n"
        "change-id header-change\n"
        "author Test <test@example.com> 0 +0000\n"
        "committer Test <test@example.com> 0 +0000\n\n"
        "Title\n\nchange-id message-text\n"
    )
    monkeypatch.setattr(
        script,
        "command",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, commit, ""),
    )

    assert script.git_commit_change_id("/git", "oid") == "header-change"


def test_atomic_publication_stale_lease_changes_no_branch(tmp_path):
    script = load_script()
    remote = tmp_path / "remote.git"
    source = tmp_path / "source"

    def git(*args, cwd=source, check=True):
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            text=True,
            capture_output=True,
            check=check,
        )

    git("init", "--bare", str(remote), cwd=tmp_path)
    git("init", str(source), cwd=tmp_path)
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.com")
    (source / "file").write_text("base\n")
    git("add", "file")
    git("commit", "-m", "base")
    old = git("rev-parse", "HEAD").stdout.strip()
    git("remote", "add", "origin", str(remote))
    git("push", "origin", f"{old}:refs/heads/b", f"{old}:refs/heads/c")

    (source / "file").write_text("base\nnew\n")
    git("commit", "-am", "new")
    new = git("rev-parse", "HEAD").stdout.strip()
    (source / "file").write_text("base\nconcurrent\n")
    git("commit", "-am", "concurrent")
    concurrent = git("rev-parse", "HEAD").stdout.strip()
    git("push", "origin", f"{concurrent}:refs/heads/b")

    updates = (
        script.RefUpdate("b", old, new),
        script.RefUpdate("c", old, new),
    )
    failed = subprocess.run(
        script.atomic_push_command(str(source), "origin", updates),
        text=True,
        capture_output=True,
        check=False,
    )

    assert failed.returncode != 0
    assert git("rev-parse", "refs/remotes/origin/b").stdout.strip() == concurrent
    assert git("ls-remote", "origin", "refs/heads/c").stdout.split()[0] == old


def test_tracks_published_branches_and_allows_missing_local_bookmarks(monkeypatch):
    script = load_script()
    calls = []
    monkeypatch.setattr(
        script,
        "local_bookmark_targets",
        lambda: (("existing", "existing-oid"),),
    )
    monkeypatch.setattr(
        script,
        "jj",
        lambda *args, **kwargs: (
            calls.append((args, kwargs)) or subprocess.CompletedProcess(args, 0, "", "")
        ),
    )

    script.track_published_branches(
        "origin", {"existing": "existing-oid", "generated": "generated-oid"}
    )

    assert calls == [
        (
            (
                "bookmark",
                "track",
                "existing@origin",
                "generated@origin",
            ),
            {"check": False},
        )
    ]


def test_refuses_to_track_published_branch_over_divergent_local_bookmark(
    monkeypatch,
):
    script = load_script()
    monkeypatch.setattr(
        script,
        "local_bookmark_targets",
        lambda: (("branch", "different"),),
    )

    with pytest.raises(script.Error, match="local bookmarks diverged"):
        script.track_published_branches("origin", {"branch": "published"})


def test_verifies_published_branches_are_tracked_and_local(monkeypatch):
    script = load_script()
    monkeypatch.setattr(
        script,
        "jj_output",
        lambda *args: "existing\ngenerated\n",
    )
    monkeypatch.setattr(
        script,
        "local_bookmark_targets",
        lambda: (("existing", "a"), ("generated", "b"), ("other", "c")),
    )

    script.verify_published_branch_tracking(
        "origin", {"existing": "a", "generated": "b"}
    )


def test_rejects_missing_or_inexact_published_branch_tracking(monkeypatch):
    script = load_script()
    monkeypatch.setattr(script, "jj_output", lambda *args: "existing\n")
    monkeypatch.setattr(
        script,
        "local_bookmark_targets",
        lambda: (("existing", "wrong"), ("generated", "b")),
    )

    with pytest.raises(script.Error, match="existing, generated"):
        script.verify_published_branch_tracking(
            "origin", {"existing": "a", "generated": "b"}
        )


def test_topology_plan_is_separate_from_ref_publication():
    script = load_script()
    revisions = [revision(script, "a", local=("a",))]
    topology = script.make_plan(
        revisions, ["a"], script.StackSnapshot(None, None, []), "base"
    )
    refs = script.make_ref_publication_plan(
        script.ReconciliationKind.NORMAL_PUBLISH,
        revisions,
        ["a"],
        {"a": "old"},
    )

    assert not hasattr(topology, "push_args")
    assert refs.updates == (script.RefUpdate("a", "old", "a"),)


def test_plan_reports_multi_to_single_metadata_authority_transition(capsys):
    script = load_script()
    pr = pull_request(script, "a", 1, head_oid="a", commits=2)
    stack = script.StackSnapshot("STACK_7", 7, [pr], {"a": pr})
    revisions = [revision(script, "a", local=("a",))]
    plan = script.make_plan(revisions, ["a"], stack, "base", (1,))
    repo = script.Repository(
        ".",
        "owner/repo",
        "owner/repo",
        "github.com",
        "main",
        "https://github.com/owner/repo",
    )

    script.print_plan(
        plan,
        revisions,
        stack,
        repo,
        "origin",
        script.Selection(("a",), ("a",), "base", "a"),
        "base",
        {},
        verbosity=1,
    )

    assert (
        "segment changed 2 → 1 commits, so local title/body becomes authoritative"
        in capsys.readouterr().err
    )


def test_selected_pr_expands_to_complete_open_suffix(monkeypatch):
    script = load_script()
    a = pull_request(script, "a", 1)
    b = pull_request(script, "b", 2)
    c = pull_request(script, "c", 3)
    stack = script.StackSnapshot("STACK_7", 7, [a, b, c], {"b": b})
    all_revisions = [revision(script, f"oid-{name}") for name in ("a", "b", "c")]
    discovery = script.Discovery(None, all_revisions, [], ["b"], stack, [])
    monkeypatch.setattr(script, "local_bookmark_oid", lambda name: f"oid-{name}")
    monkeypatch.setattr(
        script,
        "revision_ids",
        lambda revset: (
            ["oid-a", "oid-b", "oid-c"] if revset == "(oid-b | oid-a | oid-c)" else []
        ),
    )

    expanded = script.expand_open_suffix(["oid-b"], discovery)

    assert expanded == ["oid-a", "oid-b", "oid-c"]


def test_missing_local_pr_boundary_is_left_for_omission(monkeypatch):
    script = load_script()
    a = pull_request(script, "a", 1)
    hidden = pull_request(script, "hidden", 2)
    c = pull_request(script, "c", 3)
    stack = script.StackSnapshot("STACK_7", 7, [a, hidden, c], {"a": a, "c": c})
    discovery = script.Discovery(None, [], [], ["a", "c"], stack, [])
    monkeypatch.setattr(script, "local_bookmark_oid", lambda _name: None)

    assert script.expand_open_suffix(["oid-a", "oid-c"], discovery) == [
        "oid-a",
        "oid-c",
    ]


def test_off_line_local_pr_bookmark_is_left_for_omission(monkeypatch):
    script = load_script()
    a = pull_request(script, "a", 1)
    omitted = pull_request(script, "omitted", 2)
    c = pull_request(script, "c", 3)
    stack = script.StackSnapshot("STACK_7", 7, [a, omitted, c], {"a": a, "c": c})
    selected_range = [revision(script, "oid-a"), revision(script, "oid-c")]
    discovery = script.Discovery(None, selected_range, [], ["a", "c"], stack, [])
    monkeypatch.setattr(script, "local_bookmark_oid", lambda _name: "off-line")

    assert script.expand_open_suffix(["oid-a", "oid-c"], discovery) == [
        "oid-a",
        "oid-c",
    ]


def test_merged_selector_only_stack_still_expands_in_range_active_pr(monkeypatch):
    script = load_script()
    active = pull_request(script, "active", 2)
    stack = script.StackSnapshot("STACK_7", 7, [active], {})
    selected_range = [revision(script, "oid-active"), revision(script, "oid-new")]
    discovery = script.Discovery(None, selected_range, [], ["new"], stack, [])
    monkeypatch.setattr(script, "local_bookmark_oid", lambda _name: "oid-active")
    monkeypatch.setattr(
        script,
        "revision_ids",
        lambda revset: (
            ["oid-active", "oid-new"] if revset == "(oid-new | oid-active)" else []
        ),
    )

    assert script.expand_open_suffix(["oid-new"], discovery) == [
        "oid-active",
        "oid-new",
    ]


def test_execution_rediscovery_must_preserve_authorized_boundaries_and_prs():
    script = load_script()
    a = revision(script, "a")
    b = revision(script, "b")
    pr_a = pull_request(script, "a", 1)
    initial = script.Discovery(
        None,
        [a, b],
        [a],
        ["a"],
        script.StackSnapshot("STACK_7", 7, [pr_a], {"a": pr_a}),
        [],
    )
    authorized = script.publication_identity(initial)

    assert authorized == (("change-a", "a", "PR_1"),)
    inserted = pull_request(script, "b", 2, stack_id=None, stack_number=None)
    rediscovered = script.Discovery(
        None,
        [a, b],
        [a, b],
        ["a", "b"],
        script.StackSnapshot("STACK_7", 7, [pr_a], {"a": pr_a, "b": inserted}),
        [],
    )
    assert script.publication_identity(rediscovered) != authorized

    replacement = pull_request(script, "a", 99, stack_id=None, stack_number=None)
    replaced = script.Discovery(
        None,
        [a],
        [a],
        ["a"],
        script.StackSnapshot("STACK_7", 7, [pr_a], {"a": replacement}),
        [],
    )
    assert script.publication_identity(replaced) != authorized


def test_validates_complete_exact_old_suffix(monkeypatch):
    script = load_script()
    a = pull_request(script, "a", 1, base="main", base_oid="base", head_oid="a")
    b = pull_request(script, "b", 2, base="a", base_oid="a", head_oid="b")
    stack = script.StackSnapshot("STACK_7", 7, [a, b])
    monkeypatch.setattr(script, "ensure_git_commit", lambda *_args: None)
    monkeypatch.setattr(
        script,
        "git_last_linear_commits",
        lambda _root, tip, _count: (tip,),
    )
    monkeypatch.setattr(
        script,
        "git_linear_segment",
        lambda _root, base, tip: {("base", "a"): ("a",), ("a", "b"): ("b",)}[
            (base, tip)
        ],
    )

    evidence = script.validate_existing_suffix(
        stack,
        "owner/repo",
        {"main": "base", "a": "a", "b": "b"},
        {"main": "base", "a": "a", "b": "b"},
        "/git",
        "origin",
    )

    assert [item.pr.number for item in evidence] == [1, 2]
    assert all(item.check is script.RemoteCheck.EXACT for item in evidence)


def test_merged_prefix_is_the_open_suffix_root(monkeypatch):
    script = load_script()
    merged = pull_request(
        script, "merged", 1, state="MERGED", merged=True, head_oid="merged-root"
    )
    opened = pull_request(
        script,
        "opened",
        2,
        base="main",
        base_oid="unrelated-moving-base",
        head_oid="opened",
    )
    stack = script.StackSnapshot("STACK_7", 7, [merged, opened])
    monkeypatch.setattr(script, "ensure_git_commit", lambda *_args: None)
    monkeypatch.setattr(
        script,
        "git_last_linear_commits",
        lambda _root, tip, _count: (tip,),
    )
    monkeypatch.setattr(
        script,
        "git_linear_segment",
        lambda _root, base, tip: (
            ("opened",) if (base, tip) == ("merged-root", "opened") else ()
        ),
    )

    evidence = script.validate_existing_suffix(
        stack,
        "owner/repo",
        {"opened": "opened"},
        {"main": "current-main", "opened": "opened"},
        "/git",
        "origin",
    )

    assert evidence[0].check is script.RemoteCheck.EXACT


def test_live_first_segment_can_restack_onto_main_merge_commit(monkeypatch):
    script = load_script()
    merged = pull_request(
        script, "merged", 1, state="MERGED", merged=True, head_oid="historical-a"
    )
    opened = pull_request(
        script,
        "b",
        2,
        base="main",
        base_oid="main-merge",
        head_oid="live-b",
    )
    stack = script.StackSnapshot("STACK_7", 7, [merged, opened])
    linear_calls = []
    last_calls = []
    monkeypatch.setattr(script, "ensure_git_commit", lambda *_args: None)
    monkeypatch.setattr(
        script,
        "git_linear_segment",
        lambda _root, base, tip: linear_calls.append((base, tip)) or ("tracked-b",),
    )
    monkeypatch.setattr(
        script,
        "git_last_linear_commits",
        lambda _root, tip, count: last_calls.append((tip, count)) or ("live-b",),
    )
    monkeypatch.setattr(script, "git_commit_change_id", lambda *_args: "change-b")
    monkeypatch.setattr(script, "git_patch_id", lambda *_args: "patch-b")

    evidence = script.validate_existing_suffix(
        stack,
        "owner/repo",
        {"b": "tracked-b"},
        {"main": "main-merge", "b": "live-b"},
        "/git",
        "origin",
    )

    assert evidence[0].check is script.RemoteCheck.RESTACKED
    assert linear_calls == []
    assert last_calls == [("tracked-b", 1), ("live-b", 1)]


def test_tracked_first_open_may_already_be_restacked_onto_merge_result(monkeypatch):
    script = load_script()
    merged = pull_request(
        script, "merged", 1, state="MERGED", merged=True, head_oid="historical-a"
    )
    opened = pull_request(
        script,
        "b",
        2,
        base="merged-branch",
        base_oid="main-merge",
        head_oid="restacked-b",
    )
    stack = script.StackSnapshot("STACK_7", 7, [merged, opened])
    linear_calls = []
    last_calls = []
    monkeypatch.setattr(script, "ensure_git_commit", lambda *_args: None)
    monkeypatch.setattr(
        script,
        "git_linear_segment",
        lambda _root, base, tip: (
            linear_calls.append((base, tip))
            or (_ for _ in ()).throw(script.Error("crossed merge commit"))
        ),
    )
    monkeypatch.setattr(
        script,
        "git_last_linear_commits",
        lambda _root, tip, count: last_calls.append((tip, count)) or (tip,),
    )

    evidence = script.validate_existing_suffix(
        stack,
        "owner/repo",
        {"b": "restacked-b"},
        {"merged-branch": "main-merge", "b": "restacked-b"},
        "/git",
        "origin",
    )

    assert evidence[0].check is script.RemoteCheck.EXACT
    assert linear_calls == []
    assert last_calls == [("restacked-b", 1), ("restacked-b", 1)]


def test_omitted_restack_requires_matching_change_and_patch_ids(monkeypatch):
    script = load_script()
    pr = pull_request(
        script, "topic", 12, base="main", base_oid="base", head_oid="live"
    )
    stack = script.StackSnapshot("STACK_7", 7, [pr])
    monkeypatch.setattr(script, "ensure_git_commit", lambda *_args: None)
    monkeypatch.setattr(
        script,
        "git_last_linear_commits",
        lambda _root, tip, _count: (tip,),
    )
    monkeypatch.setattr(script, "git_commit_change_id", lambda *_args: "change")
    monkeypatch.setattr(script, "git_patch_id", lambda *_args: "patch")

    evidence = script.validate_existing_suffix(
        stack,
        "owner/repo",
        {"topic": "tracked"},
        {"main": "base", "topic": "live"},
        "/git",
        "origin",
    )

    assert evidence[0].check is script.RemoteCheck.RESTACKED

    monkeypatch.setattr(
        script,
        "git_patch_id",
        lambda _root, oid: "remote-only" if oid == "live" else "patch",
    )
    with pytest.raises(script.Error, match="remote-only edits"):
        script.validate_existing_suffix(
            stack,
            "owner/repo",
            {"topic": "tracked"},
            {"main": "base", "topic": "live"},
            "/git",
            "origin",
        )


def test_omitted_suffix_requires_tracked_baseline(monkeypatch):
    script = load_script()
    pr = pull_request(
        script, "hidden", 12, base="main", base_oid="base", head_oid="live"
    )
    stack = script.StackSnapshot("STACK_7", 7, [pr])
    monkeypatch.setattr(script, "ensure_git_commit", lambda *_args: None)

    with pytest.raises(script.Error, match="no tracked restack baseline"):
        script.validate_existing_suffix(
            stack,
            "owner/repo",
            {"main": "base"},
            {"main": "base", "hidden": "live"},
            "/git",
            "origin",
        )


def test_verifies_complete_linear_remote_restack(monkeypatch):
    script = load_script()
    first = pull_request(script, "b", 1, head_oid="remote-b", base_oid="main")
    second = pull_request(script, "c", 2, head_oid="remote-c", base_oid="remote-b")
    states = (
        script.BoundaryState("b", "local-b", "local-b", "remote-b", first),
        script.BoundaryState("c", "local-c", "local-c", "remote-c", second),
    )
    fetched = []
    monkeypatch.setattr(
        script,
        "ensure_git_commit",
        lambda git_root, remote, oid: fetched.append((git_root, remote, oid)),
    )
    monkeypatch.setattr(
        script,
        "git_linear_segment",
        lambda _root, base, tip: {
            ("main", "remote-b"): ("one", "remote-b"),
            ("remote-b", "remote-c"): ("remote-c",),
        }[(base, tip)],
    )
    monkeypatch.setattr(script, "git_commit_change_id", lambda _root, oid: oid[-1])
    monkeypatch.setattr(
        script,
        "git_last_linear_commits",
        lambda _root, tip, count: {
            ("local-b", 2): ("old-one", "local-b"),
            ("local-c", 1): ("local-c",),
        }[(tip, count)],
    )
    monkeypatch.setattr(script, "git_patch_id", lambda _root, oid: oid[-1])

    script.verify_mechanical_restack(states, "main", (2, 1), "/git", "origin")

    assert fetched == [
        ("/git", "origin", "remote-b"),
        ("/git", "origin", "remote-c"),
    ]

    monkeypatch.setattr(
        script,
        "git_patch_id",
        lambda _root, oid: "remote-only" if oid == "remote-c" else oid[-1],
    )
    with pytest.raises(script.Error, match="remote-only edits"):
        script.verify_mechanical_restack(states, "main", (2, 1), "/git", "origin")


def test_first_live_segment_can_restack_onto_reported_pr_base(monkeypatch):
    script = load_script()
    pr = pull_request(
        script,
        "b",
        1,
        head_oid="live-b",
        base_oid="current-main",
    )
    state = script.BoundaryState("b", "tracked-b", "tracked-b", "live-b", pr)
    monkeypatch.setattr(script, "ensure_git_commit", lambda *_args: None)
    monkeypatch.setattr(
        script,
        "git_linear_segment",
        lambda *_args: (_ for _ in ()).throw(script.Error("not historical child")),
    )
    monkeypatch.setattr(
        script, "git_last_linear_commits", lambda _root, tip, _count: (tip,)
    )
    monkeypatch.setattr(script, "git_commit_parent", lambda *_args: "current-main")
    monkeypatch.setattr(script, "git_commit_change_id", lambda *_args: "change-b")
    monkeypatch.setattr(script, "git_patch_id", lambda *_args: "patch-b")

    script.verify_mechanical_restack(
        (state,),
        "historical-a",
        (1,),
        "/git",
        "origin",
        allow_restacked_first=True,
    )

    monkeypatch.setattr(script, "git_commit_parent", lambda *_args: "unrelated")
    with pytest.raises(script.Error, match="unexpected live base"):
        script.verify_mechanical_restack(
            (state,),
            "historical-a",
            (1,),
            "/git",
            "origin",
            allow_restacked_first=True,
        )


def test_local_authority_validates_existing_live_segment_counts(monkeypatch):
    script = load_script()
    pr = pull_request(script, "topic", 1, head_oid="tracked", commits=2)
    state = script.BoundaryState("topic", "squashed", "tracked", "tracked", pr)
    calls = []
    monkeypatch.setattr(
        script,
        "verify_live_suffix",
        lambda states, base, counts, root, remote, **kwargs: calls.append(
            ("live", states, base, counts, root, remote, kwargs)
        ),
    )
    monkeypatch.setattr(
        script,
        "verify_mechanical_restack",
        lambda states, base, counts, root, remote, **kwargs: calls.append(
            ("restack", states, base, counts, root, remote, kwargs)
        ),
    )

    script.verify_reconciliation_suffix(
        script.ReconciliationKind.LOCAL_AUTHORITY,
        (state,),
        "main",
        (1,),
        "/git",
        "origin",
    )

    assert calls == [
        (
            "live",
            (state,),
            "main",
            (2,),
            "/git",
            "origin",
            {"allow_restacked_first": False},
        )
    ]

    restacked = script.BoundaryState("topic", "squashed", "tracked", "restacked", pr)
    calls.clear()
    script.verify_reconciliation_suffix(
        script.ReconciliationKind.LOCAL_AUTHORITY,
        (restacked,),
        "main",
        (1,),
        "/git",
        "origin",
    )

    assert calls == [
        (
            "restack",
            (restacked,),
            "main",
            (2,),
            "/git",
            "origin",
            {"allow_restacked_first": False},
        )
    ]


def test_fetches_exact_live_base_without_updating_git_refs(tmp_path):
    script = load_script()
    remote = tmp_path / "remote.git"
    source = tmp_path / "source"
    local = tmp_path / "local"

    def git(*args, cwd=None):
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()

    git("init", "--bare", str(remote))
    git("init", str(source))
    git("config", "user.name", "Test", cwd=source)
    git("config", "user.email", "test@example.com", cwd=source)
    (source / "file").write_text("base\n")
    git("add", "file", cwd=source)
    git("commit", "-m", "base", cwd=source)
    git("branch", "-M", "main", cwd=source)
    git("remote", "add", "origin", str(remote), cwd=source)
    git("push", "-u", "origin", "main", cwd=source)
    git("symbolic-ref", "HEAD", "refs/heads/main", cwd=remote)
    git("clone", str(remote), str(local))
    git("config", "user.name", "Test", cwd=local)
    git("config", "user.email", "test@example.com", cwd=local)
    old_base = git("rev-parse", "HEAD", cwd=local)

    (source / "file").write_text("base\nadvanced\n")
    git("commit", "-am", "advance base", cwd=source)
    git("push", cwd=source)
    live_base = git("rev-parse", "HEAD", cwd=source)
    (local / "feature").write_text("feature\n")
    git("add", "feature", cwd=local)
    git("commit", "-m", "feature", cwd=local)
    tip = git("rev-parse", "HEAD", cwd=local)

    subprocess.run(
        ["jj", "git", "init", "--colocate", str(local)],
        text=True,
        capture_output=True,
        check=True,
    )
    script.JJ = ["jj", "--repository", str(local)]
    script.begin_inspection_phase()
    (local / "feature").write_text("dirty working copy\n")
    operation_before = script.operation_id()
    refs_before = git("show-ref", cwd=local)
    fetch_head = local / ".git" / "FETCH_HEAD"
    fetch_head_before = fetch_head.read_bytes() if fetch_head.exists() else None
    assert not script.git_commit_exists(str(local), live_base)

    script.ensure_git_commit(str(local), "origin", live_base)

    assert script.git_commit_exists(str(local), live_base)
    assert git("show-ref", cwd=local) == refs_before
    assert (
        fetch_head.read_bytes() if fetch_head.exists() else None
    ) == fetch_head_before
    assert script.operation_id() == operation_before
    assert (local / "feature").read_text() == "dirty working copy\n"
    assert script.git_fork(str(local), tip, live_base) == old_base
    assert script.fetched_remote_refs(str(local), "origin")["main"] == old_base

    selection = script.validate_boundaries([tip], live_base, str(local), "origin")

    assert selection.fork == old_base
    assert selection.commits == (tip,)


def test_live_base_validation_rejects_shallow_repositories(monkeypatch):
    script = load_script()
    monkeypatch.setattr(
        script,
        "command",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "true\n", ""),
    )

    with pytest.raises(script.Error, match="shallow Git repositories"):
        script.ensure_git_commit("/git", "origin", "base")


def test_git_fork_rejects_missing_and_multiple_merge_bases(monkeypatch):
    script = load_script()

    monkeypatch.setattr(
        script,
        "command",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, "", "missing"),
    )
    with pytest.raises(script.Error, match="no common ancestor"):
        script.git_fork(".", "tip", "base")

    monkeypatch.setattr(
        script,
        "command",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "one\ntwo\n", ""),
    )
    with pytest.raises(script.Error, match="multiple merge bases"):
        script.git_fork(".", "tip", "base")


def test_boundary_validation_uses_git_fork_then_jj_history(monkeypatch):
    script = load_script()
    calls = []
    monkeypatch.setattr(script, "exactly_one", lambda _revset, _what: "tip")
    monkeypatch.setattr(
        script,
        "ensure_git_commit",
        lambda git_root, remote, oid: calls.append((git_root, remote, oid)),
    )
    monkeypatch.setattr(script, "git_fork", lambda *_args: "fork")

    def revisions(revset):
        return {
            "fork": ["fork"],
            "fork & root()": [],
            "fork..tip": ["a", "tip"],
            "(fork..tip) & divergent()": [],
            "(a | tip) & hidden()": [],
            "parents(a)": ["fork"],
            "parents(tip)": ["a"],
        }[revset]

    monkeypatch.setattr(script, "revision_ids", revisions)

    selection = script.validate_boundaries(["a", "tip"], "live-base", "/git", "origin")

    assert calls == [("/git", "origin", "live-base")]
    assert selection == script.Selection(("a", "tip"), ("a", "tip"), "fork", "tip")

    def divergent_revisions(revset):
        if revset == "(fork..tip) & divergent()":
            return ["tip"]
        return revisions(revset)

    monkeypatch.setattr(script, "revision_ids", divergent_revisions)
    with pytest.raises(script.Error, match="divergent changes"):
        script.validate_boundaries(["a", "tip"], "live-base", "/git", "origin")
    assert (
        script.validate_boundaries(
            ["a", "tip"],
            "live-base",
            "/git",
            "origin",
            reject_divergence=False,
        ).tip
        == "tip"
    )


def test_imports_base_branch_at_execution(monkeypatch):
    script = load_script()
    calls = []
    inspection_phases = []
    exists = iter([False, True])
    monkeypatch.setattr(script, "jj_commit_exists", lambda _oid: next(exists))
    monkeypatch.setattr(
        script,
        "jj",
        lambda *args: calls.append(args),
    )
    monkeypatch.setattr(
        script,
        "fetched_remote_refs",
        lambda *_args: {"dev": "planned"},
    )
    monkeypatch.setattr(
        script, "begin_inspection_phase", lambda: inspection_phases.append("fresh")
    )

    script.import_live_base("/git", "origin", "dev", "planned")

    assert calls == [("git", "fetch", "--remote", "origin", "--branch", "dev")]
    assert inspection_phases == ["fresh"]

    monkeypatch.setattr(script, "jj_commit_exists", lambda _oid: False)
    monkeypatch.setattr(
        script,
        "fetched_remote_refs",
        lambda *_args: {"dev": "moved"},
    )
    with pytest.raises(script.Error, match="changed while importing"):
        script.import_live_base("/git", "origin", "dev", "planned")


def test_boundary_push_precedes_local_move_and_quotes_lookup(monkeypatch):
    script = load_script()
    inspections = []
    monkeypatch.setattr(
        script,
        "jj_inspect",
        lambda *args, **_kwargs: (
            inspections.append(args)
            or subprocess.CompletedProcess(args, 0, "feature/name\n", "")
        ),
    )

    commands = script.boundary_commands(
        "feature/name", "new-base", "origin", "/git", "old-boundary"
    )

    assert inspections[0][2] == 'exact:"feature/name"'
    assert commands[0] == script.atomic_push_command(
        "/git",
        "origin",
        (script.RefUpdate("feature/name", "old-boundary", "new-base"),),
    )
    assert commands[1] == [
        *script.JJ,
        "bookmark",
        "set",
        "--allow-backwards",
        "-r",
        "new-base",
        "feature/name",
    ]


def test_consequential_plan_requires_confirmation_noninteractively(monkeypatch):
    script = load_script()
    pr = pull_request(script, "a", 1, stack_id=None, stack_number=None)
    stack = script.StackSnapshot(None, None, [], {"a": pr})
    plan = script.Plan(
        script.TopologyKind.NEW,
        ["a"],
        [],
        ["a"],
        None,
        None,
        False,
    )
    monkeypatch.setattr(
        script.sys, "stdin", type("Stdin", (), {"isatty": lambda _self: False})()
    )

    assert script.requires_confirmation(
        plan, stack, script.ReconciliationKind.NORMAL_PUBLISH, None
    )
    with pytest.raises(script.Error, match="rerun with --yes"):
        script.authorize_plan(required=True, plan_only=False, yes=False)
    script.authorize_plan(required=True, plan_only=True, yes=False)
    script.authorize_plan(required=True, plan_only=False, yes=True)


def test_consequential_plan_can_be_confirmed(monkeypatch, capsys):
    script = load_script()
    monkeypatch.setattr(
        script.sys, "stdin", type("Stdin", (), {"isatty": lambda _self: True})()
    )
    monkeypatch.setattr("builtins.input", lambda: "yes")

    script.authorize_plan(required=True, plan_only=False, yes=False)

    assert "Continue? [y/N]" in capsys.readouterr().err


def test_omitted_prs_are_covered_by_unified_confirmation():
    script = load_script()
    first = pull_request(script, "first", 11)
    second = pull_request(script, "second", 12)
    plan = script.Plan(
        script.TopologyKind.FULLY_OPEN_REBUILD,
        [],
        [first, second],
        [],
        7,
        None,
        False,
    )
    stack = script.StackSnapshot("STACK_7", 7, [first, second])
    assert script.requires_confirmation(
        plan, stack, script.ReconciliationKind.NORMAL_PUBLISH, None
    )
    script.validate_close_targets(plan, frozenset({12}))

    with pytest.raises(script.Error, match="not an omitted open PR"):
        script.validate_close_targets(plan, frozenset({99}))


def test_noninteractive_omission_accepts_yes_not_close_authorization(monkeypatch):
    script = load_script()
    omitted = pull_request(script, "topic", 12)
    plan = script.Plan(
        script.TopologyKind.FULLY_OPEN_REBUILD,
        [],
        [omitted],
        [],
        7,
        None,
        False,
    )
    monkeypatch.setattr(
        script.sys, "stdin", type("Stdin", (), {"isatty": lambda _self: False})()
    )

    with pytest.raises(script.Error, match="rerun with --yes"):
        script.authorize_plan(required=True, plan_only=False, yes=False)
    script.authorize_plan(required=True, plan_only=False, yes=True)
    script.validate_close_targets(plan, frozenset({12}))


def test_fully_open_unstack_accepts_missing_rest_stack(monkeypatch):
    script = load_script()
    old = script.StackSnapshot(
        "DISSOLVED_STACK",
        63,
        [pull_request(script, "a", 60)],
    )
    calls = []

    def command(args, **kwargs):
        calls.append((args, kwargs))
        return script.subprocess.CompletedProcess(
            args, 1, "HTTP/2.0 404 Not Found\n\n", ""
        )

    monkeypatch.setattr(script, "command", command)
    monkeypatch.setattr(
        script,
        "fetch_stack",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("dissolved stacks must not be queried by GraphQL node ID")
        ),
    )

    script.verify_unstacked(old, "owner/repo", {}, ".")

    assert calls[0][0][-1] == "repos/owner/repo/stacks/63"
    assert calls[0][1]["check"] is False


def test_final_verification_preserves_metadata_update_base_name(monkeypatch):
    script = load_script()
    merged = pull_request(script, "publish-merge-x", 58, state="MERGED", merged=True)
    current = pull_request(
        script,
        "c",
        53,
        draft=True,
        head_oid="oid-c",
        base="main",
        base_oid="base",
    )
    final = script.StackSnapshot("STACK_55", 55, [merged, current])
    monkeypatch.setattr(
        script,
        "discover_pull_requests",
        lambda *_args: {"c": current},
    )
    monkeypatch.setattr(script, "fetch_stack", lambda *_args: final)
    repo = script.Repository(
        ".",
        "owner/repo",
        "owner/repo",
        "github.com",
        "main",
        "https://github.com/owner/repo",
    )

    verified = script.verify_final(
        repo,
        [script.Revision("oid-c", "change-c", (), (), "Title c", "Body c", "push-c")],
        ["c"],
        {"c": ("Title c", "Body c")},
        {"c": True},
        "main",
        [merged],
        "STACK_55",
        "base",
        (1,),
        {},
    )

    assert verified == final


def test_synchronizes_metadata_without_changing_readiness(monkeypatch):
    script = load_script()
    calls = []
    revisions = [revision(script, "a"), revision(script, "b")]
    prs = {
        "a": pull_request(script, "old-a", 1, draft=True),
        "b": pull_request(script, "old-b", 2, draft=False),
    }

    monkeypatch.setattr(
        script,
        "graphql",
        lambda query, variables, *_args: calls.append((query, variables)) or {},
    )
    script.synchronize_pull_requests(
        ["a", "b"],
        prs,
        {
            head: (item.title, item.body)
            for head, item in zip(["a", "b"], revisions, strict=True)
        },
        {},
        ".",
    )

    assert len(calls) == 1
    query, variables = calls[0]
    assert "updatePullRequest" in query
    assert "markPullRequestReadyForReview" not in query
    assert "convertPullRequestToDraft" not in query
    assert set(variables) == {"edit0", "edit1"}


def test_closes_omitted_pr_by_node_id_and_verifies_it(monkeypatch):
    script = load_script()
    opened = pull_request(script, "topic", 12, stack_id=None, stack_number=None)
    closed = script.PullRequest(**{**opened.__dict__, "state": "CLOSED"})
    calls = []
    monkeypatch.setattr(
        script,
        "graphql",
        lambda query, variables, *_args: calls.append((query, variables)) or {},
    )
    monkeypatch.setattr(script, "fetch_pull_request", lambda *_args: closed)

    script.close_omitted_pull_request(opened, {}, ".")

    assert "closePullRequest" in calls[0][0]
    assert calls[0][1] == {"input": {"pullRequestId": opened.node_id}}


def test_preserves_existing_multi_commit_metadata(monkeypatch):
    script = load_script()
    calls = []
    pr = pull_request(script, "a", 1, commits=2)
    monkeypatch.setattr(
        script,
        "graphql",
        lambda query, variables, *_args: calls.append((query, variables)) or {},
    )

    script.synchronize_pull_requests(
        ["a"],
        {"a": pr},
        {"a": (pr.title, pr.body)},
        {},
        ".",
    )

    assert calls == []


def test_refresh_closure_preserves_local_bookmarks_and_imports_base(monkeypatch):
    script = load_script()
    before = script.ClosureSnapshot(
        "stack",
        ("stack", "descendant"),
        ("change-stack", "change-descendant"),
        frozenset({("change-stack", "stack"), ("change-descendant", "descendant")}),
        (("default", "descendant"),),
        (("descendant", "descendant"), ("main", "old-main")),
    )
    monkeypatch.setattr(
        script,
        "revision_ids",
        lambda revset: ["new-main"] if revset == "new-main" else ["visible"],
    )
    monkeypatch.setattr(script, "workspace_targets", lambda: before.workspaces)
    monkeypatch.setattr(
        script,
        "local_bookmark_targets",
        lambda: before.bookmarks,
    )
    monkeypatch.setattr(
        script,
        "visible_identities",
        lambda: before.visible | frozenset({("change-main", "new-main")}),
    )

    refreshed = script.refresh_closure_baseline(before, "new-main")

    assert refreshed.bookmarks == before.bookmarks

    monkeypatch.setattr(
        script,
        "local_bookmark_targets",
        lambda: (("descendant", "moved"), ("main", "old-main")),
    )
    with pytest.raises(script.Error, match="changed a local bookmark"):
        script.refresh_closure_baseline(before, "new-main")

    monkeypatch.setattr(script, "local_bookmark_targets", lambda: before.bookmarks)
    monkeypatch.setattr(
        script,
        "revision_ids",
        lambda revset: [] if revset == "new-main" else ["visible"],
    )
    with pytest.raises(script.Error, match="did not import the live base commit"):
        script.refresh_closure_baseline(before, "new-main")


def test_link_commands_distinguish_new_append_and_merged_rebuild():
    script = load_script()
    repo = script.Repository(
        ".",
        "owner/repo",
        "owner/repo",
        "github.com",
        "main",
        "https://github.com/owner/repo",
    )

    new = script.Plan(
        script.TopologyKind.NEW,
        ["a", "b"],
        [],
        ["a", "b"],
        None,
        None,
        False,
    )
    append = script.Plan(
        script.TopologyKind.APPEND,
        ["a", "b"],
        [],
        ["b"],
        7,
        None,
        False,
    )
    boundary = pull_request(script, "merged", 1, state="MERGED", merged=True)
    rebuild = script.Plan(
        script.TopologyKind.MERGED_PREFIX_REBUILD,
        ["a", "b"],
        [],
        ["a", "b"],
        7,
        boundary,
        False,
    )
    existing = pull_request(script, "a", 2)
    empty_stack = script.StackSnapshot(None, None, [])
    partial_stack = script.StackSnapshot("STACK_7", 7, [existing], {"a": existing})

    assert script.link_command(new, empty_stack, repo, "origin") == [
        "gh",
        "stack",
        "link",
        "--remote",
        "origin",
        "--base",
        "main",
        "--",
        "a",
        "b",
    ]
    assert script.link_command(append, partial_stack, repo, "origin") == [
        "gh",
        "stack",
        "link",
        "--remote",
        "origin",
        "7",
        "--",
        "b",
    ]
    assert script.link_command(rebuild, partial_stack, repo, "origin") == [
        "gh",
        "stack",
        "link",
        "--remote",
        "origin",
        "7",
        "--",
        existing.url,
        "b",
    ]


def test_stack_link_temporarily_exposes_only_new_git_heads(tmp_path):
    script = load_script()
    repo = tmp_path / "repo"
    observed = tmp_path / "observed"

    def git(*args):
        return subprocess.run(
            ["git", *args],
            cwd=repo,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()

    repo.mkdir()
    git("init")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.com")
    (repo / "file").write_text("old\n")
    git("add", "file")
    git("commit", "-m", "old")
    old = git("rev-parse", "HEAD")
    git("branch", "existing")
    (repo / "file").write_text("new\n")
    git("commit", "-am", "new")
    new = git("rev-parse", "HEAD")
    command = [
        "sh",
        "-c",
        (
            f"git -C {shlex.quote(str(repo))} rev-parse refs/heads/existing "
            f"refs/heads/generated > {shlex.quote(str(observed))}; exit 1"
        ),
    ]

    result = script.run_stack_link(
        command,
        str(repo),
        {},
        {"existing": new, "generated": new},
    )

    assert result.returncode == 1
    assert observed.read_text().splitlines() == [new, new]
    assert git("rev-parse", "refs/heads/existing") == old
    assert (
        subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "refs/heads/generated"],
            text=True,
            capture_output=True,
            check=False,
        ).returncode
        != 0
    )


def test_reflows_only_root_paragraph_soft_breaks():
    script = load_script()
    body = (
        "Wrapped prose\ncontinues.\n\n- Wrapped\n  list item\n\nHard break  \nstays.\n"
    )

    assert script.reflow_markdown(body) == (
        "Wrapped prose continues.\n\n- Wrapped\n  list item\n\nHard break  \nstays.\n"
    )


def test_parser_requires_push_and_revset_and_rejects_abbreviations():
    script = load_script()

    with pytest.raises(SystemExit):
        script.parse_args([])
    with pytest.raises(SystemExit):
        script.parse_args(["push"])
    with pytest.raises(SystemExit):
        script.parse_args(["push", "--det", "a"])

    assert script.parse_args(["push", "--", "--option-like"]).revset == "--option-like"
    assert script.parse_args(
        ["push", "--close-omitted", "12", "--close-omitted", "13", "a"]
    ).close_omitted == [12, 13]
    assert script.parse_args(["push", "--plan", "-vv", "a"]).verbose == 2
    with pytest.raises(SystemExit):
        script.parse_args(["push", "--plan", "--yes", "a"])
    with pytest.raises(SystemExit):
        script.parse_args(["push", "-vvv", "a"])


def test_sparse_boundaries_count_complete_segments():
    script = load_script()
    selection = script.Selection(
        ("b", "d", "e"),
        ("a", "b", "c", "d", "e"),
        "main",
        "e",
    )

    assert script.segment_counts(selection) == (2, 2, 1)


def test_intermediate_existing_pr_must_be_selected(monkeypatch):
    script = load_script()
    revisions = [
        revision(script, "a"),
        revision(script, "b"),
        revision(script, "c"),
    ]
    pr = pull_request(script, "push-b", 123, head_oid="b")
    monkeypatch.setattr(
        script,
        "discover_pull_requests",
        lambda *_args: {"push-b": pr},
    )

    with pytest.raises(script.Error) as raised:
        script.inspect_existing_stack(
            "owner/repo",
            ["push-a", "push-b", "push-c"],
            revisions,
            ("a", "c"),
            {},
            ".",
        )

    message = str(raised.value)
    assert "open PR #123 is on change change-b" in message
    assert '"Title b"' in message
    assert pr.url in message
    assert "between selected boundaries change-a and change-c" in message
    assert "Include change-b in the revset" in message


def test_existing_pr_cannot_move_to_a_different_change(monkeypatch):
    script = load_script()
    item = revision(script, "new", local=("topic",))
    pr = pull_request(script, "topic", 12, head_oid="old")
    monkeypatch.setattr(script, "change_id_at", lambda _oid: "change-old")

    with pytest.raises(script.Error, match="different jj change"):
        script.verify_selected_pr_change_identity(
            [item], ["topic"], {"topic": pr}, {"topic": "old"}
        )

    unchanged = revision(script, "old", local=("topic",))
    script.verify_selected_pr_change_identity(
        [unchanged], ["topic"], {"topic": pr}, {"topic": "old"}
    )
