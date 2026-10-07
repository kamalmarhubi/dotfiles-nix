# /// script
# requires-python = ">=3.12"
# dependencies = ["markdown-it-py==4.2.0", "pytest"]
# ///
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "files" / "bin" / "jj-stack"
sys.path.insert(0, str(SCRIPT.parent))
import jj_stack_sync as sync  # noqa: E402


def load_script():
    loader = importlib.machinery.SourceFileLoader("jj_stack_cli", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "argv",
    (
        ["sync", "--new"],
        ["sync", "--new", "-r", "x", "--pr", "1"],
        ["sync", "--base", "main"],
        ["sync", "--replace-pr", "1"],
        ["sync", "--force-local-wins"],
        ["sync", "-r", "x", "--replace-pr", "1", "--force-local-wins"],
        ["sync", "--resume"],
    ),
)
def test_cli_rejects_invalid_or_obsolete_grammar(argv) -> None:
    cli = load_script()
    with pytest.raises(SystemExit, match="2"):
        cli.main(argv)


@pytest.mark.parametrize(
    ("argv", "intent"),
    (
        (["sync", "--dry-run"], sync.ExistingSyncIntent()),
        (["sync", "--pr", "7", "--dry-run"], sync.ExistingSyncIntent(7)),
        (
            ["sync", "--pr", "7", "-r", "x", "--force-local-wins", "--dry-run"],
            sync.ExistingSyncIntent(7, "x", (), True),
        ),
        (
            ["sync", "-r", "x", "--replace-pr", "3", "--dry-run"],
            sync.ExistingSyncIntent(None, "x", (3,), False),
        ),
        (
            ["sync", "--new", "-r", "x", "--base", "dev", "--dry-run"],
            sync.NewSyncIntent("x", "dev"),
        ),
    ),
)
def test_cli_constructs_current_intent_without_resume(
    monkeypatch, argv, intent
) -> None:
    cli = load_script()
    seen = []
    command = sync.SyncCommandPlan(sync.Blocked(()), "push")
    monkeypatch.setattr(
        cli.sync,
        "execute_sync_command",
        lambda _workspace, _github, actual, **kwargs: (
            seen.append((actual, kwargs)) or (command, sync.Stopped("plan", "blocked"))
        ),
    )
    monkeypatch.setattr(cli.sync, "GitHubAPI", lambda **_kwargs: object())

    assert cli.main(argv) == 2
    assert seen[0][0] == intent
    assert seen[0][1]["dry_run"] is True
    assert seen[0][1]["confirm"] is None


def test_cli_confirms_the_exact_recomputed_plan(monkeypatch) -> None:
    cli = load_script()
    command = sync.SyncCommandPlan(sync.NoOp(
        sync.DesiredStack(sync.GitHubRepositoryId("github.com", "R"), "main", ()),
        sync.Dependencies((), "push", "origin", None, None, (),
            sync.StandalonePullRequest(
                sync.PullRequestId(sync.GitHubRepositoryId("github.com", "R"), 1)
            ), (), ()),
    ), "push")
    confirmed = []

    def execute(_workspace, _github, _intent, **kwargs):
        confirmed.append(kwargs["confirm"](command))
        return command, sync.Verified()

    monkeypatch.setattr(cli.sync, "execute_sync_command", execute)
    monkeypatch.setattr(cli.sync, "GitHubAPI", lambda **_kwargs: object())
    monkeypatch.setattr(cli.sync, "render_sync_command", lambda value: "PLAN" if value is command else "DONE")

    assert cli.main(["sync", "--yes"]) == 0
    assert confirmed == [True]


def test_execute_recovers_then_recomputes_then_confirms_exact_plan(monkeypatch) -> None:
    events = []
    command = sync.SyncCommandPlan(object(), "push")
    result = sync.Verified()
    fact = sync.RecoveryEntry(
        "fact",
        sync.MetadataMutationAttempt(
            sync.GitHubRepositoryId("github.com", "R"),
            sync.PRMetadataUpdate(
                sync.PullRequestId(sync.GitHubRepositoryId("github.com", "R"), 1),
                "Title",
                "",
            ),
            "Old",
            "",
        ),
        True,
    )
    monkeypatch.setattr(sync, "repository_lock", lambda _workspace: nullcontext())
    monkeypatch.setattr(sync, "read_recovery", lambda _workspace: (None, None))
    monkeypatch.setattr(
        sync, "settle_recovery", lambda *_args, **_kwargs: events.append("recover") or (fact,)
    )
    monkeypatch.setattr(
        sync,
        "plan_sync_command",
        lambda *_args, **kwargs: (
            events.append(("plan", kwargs["recovery_facts"])) or command
        ),
    )
    monkeypatch.setattr(
        sync,
        "apply_sync_command",
        lambda actual, *_args, **_kwargs: events.append(("apply", actual)) or result,
    )

    current, actual = sync.execute_sync_command(
        "/repo",
        object(),
        sync.ExistingSyncIntent(1),
        confirm=lambda actual: events.append(("confirm", actual)) or True,
    )

    assert current is command and actual is result
    assert events == [
        "recover",
        ("plan", (fact,)),
        ("confirm", command),
        ("plan", (fact,)),
        ("apply", command),
    ]


def test_execute_dry_run_interprets_without_confirmation(monkeypatch) -> None:
    command = sync.SyncCommandPlan(sync.Blocked(()), "push")
    monkeypatch.setattr(sync, "repository_lock", lambda _workspace: nullcontext())
    monkeypatch.setattr(sync, "read_recovery", lambda _workspace: (None, None))
    monkeypatch.setattr(sync, "settle_recovery", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(sync, "plan_sync_command", lambda *_args, **_kwargs: command)
    monkeypatch.setattr(
        sync,
        "apply_sync_command",
        lambda actual, *_args, **kwargs: (
            sync.Stopped("plan", "blocked")
            if isinstance(actual.value, sync.Blocked) and kwargs["dry_run"]
            else pytest.fail("unexpected apply")
        ),
    )
    confirmed = []

    current, result = sync.execute_sync_command(
        "/repo",
        object(),
        sync.ExistingSyncIntent(),
        confirm=lambda _plan: confirmed.append(True) or True,
        dry_run=True,
    )

    assert current is command
    assert isinstance(result, sync.Stopped)
    assert confirmed == []


def test_plan_sync_command_routes_new_and_existing(monkeypatch) -> None:
    new = sync.SyncCommandPlan(sync.Blocked(()), "new")
    existing = sync.SyncCommandPlan(sync.Blocked(()), "existing")
    monkeypatch.setattr(sync, "_plan_new_sync", lambda *_args: new)
    monkeypatch.setattr(sync, "_plan_existing_sync", lambda *_args: existing)

    assert sync.plan_sync_command(
        "/repo", object(), sync.NewSyncIntent("x")
    ) is new
    assert sync.plan_sync_command(
        "/repo", object(), sync.ExistingSyncIntent()
    ) is existing


def test_installed_executable_smoke() -> None:
    assert os.access(SCRIPT, os.X_OK)
    result = subprocess.run(
        [SCRIPT, "--help"], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0
    assert "sync" in result.stdout


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
