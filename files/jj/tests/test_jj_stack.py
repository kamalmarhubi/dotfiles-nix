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
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "files" / "bin" / "jj-stack"


def load_script():
    sys.path.insert(0, str(SCRIPT.parent))
    try:
        loader = importlib.machinery.SourceFileLoader("jj_stack_cli", str(SCRIPT))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(SCRIPT.parent))


@pytest.mark.parametrize(
    "argv",
    [
        ["sync"],
        ["sync", "--new"],
        ["sync", "--pr", "1", "--base", "main"],
        ["sync", "--resume", "-r", "x"],
        ["sync", "--resume", "--base", "main"],
        ["sync", "--resume", "--dry-run"],
        ["sync", "--resume", "--remote", "origin"],
        ["sync", "--pr", "1", "--new", "-r", "x"],
        ["sync", "--new", "-r", "x", "--bootstrap-local-wins"],
        ["sync", "--resume", "--bootstrap-local-wins"],
        ["sync", "--pr", "1", "--bootstrap-local-wins"],
    ],
)
def test_rejects_invalid_grammar(argv):
    cli = load_script()
    with pytest.raises(SystemExit, match="2"):
        cli.main(argv)


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["sync", "--pr", "17", "--dry-run"], ("ExistingIntent", 17, None, False)),
        (
            ["sync", "--pr", "17", "-r", "x", "--dry-run"],
            ("ExistingIntent", 17, "x", False),
        ),
        (
            [
                "sync",
                "--pr",
                "17",
                "-r",
                "x",
                "--bootstrap-local-wins",
                "--dry-run",
            ],
            ("ExistingIntent", 17, "x", True),
        ),
        (
            ["sync", "--new", "-r", "x", "--base", "dev", "--dry-run"],
            ("NewIntent", "x", "dev"),
        ),
        (["sync", "--resume", "--yes"], ("ResumeIntent",)),
    ],
)
def test_constructs_exact_intent_and_plans_once(monkeypatch, argv, expected):
    cli = load_script()
    calls = []
    plan = object()
    monkeypatch.setattr(
        cli.sync,
        "plan_explicit",
        lambda workspace, intent: calls.append(intent) or plan,
    )
    monkeypatch.setattr(cli.sync, "render_explicit", lambda value: "rendered")
    monkeypatch.setattr(
        cli.sync, "apply_explicit", lambda value, workspace: SimpleNamespace()
    )

    assert cli.main(argv) == 0
    intent = calls[0]
    assert len(calls) == 1
    assert (type(intent).__name__, *vars(intent).values()) == expected


def test_remote_override_is_forwarded_only_to_new_planning(monkeypatch) -> None:
    cli = load_script()
    calls = []
    monkeypatch.setattr(
        cli.sync,
        "plan_explicit",
        lambda workspace, intent, **kwargs: calls.append((intent, kwargs)) or object(),
    )
    monkeypatch.setattr(cli.sync, "render_explicit", lambda _value: "plan")

    assert cli.main(["sync", "--pr", "17", "--remote", "publish", "--dry-run"]) == 0
    assert calls == [
        (cli.sync.ExistingIntent(17, None), {"requested_remote": "publish"})
    ]


def test_dry_run_renders_exact_object_and_does_not_apply(monkeypatch, capsys):
    cli = load_script()
    plan = object()
    seen = []
    monkeypatch.setattr(cli.sync, "plan_explicit", lambda *_: plan)
    monkeypatch.setattr(
        cli.sync, "render_explicit", lambda value: seen.append(value) or "PLAN"
    )
    monkeypatch.setattr(cli.sync, "apply_explicit", lambda *_: pytest.fail("applied"))
    assert cli.main(["sync", "--pr", "4", "--dry-run"]) == 0
    assert seen == [plan]
    assert capsys.readouterr().out == "PLAN\n"


def test_yes_applies_identical_rendered_object(monkeypatch):
    cli = load_script()
    plan, result = object(), object()
    rendered = []
    applied = []
    monkeypatch.setattr(cli.sync, "plan_explicit", lambda *_: plan)
    monkeypatch.setattr(
        cli.sync, "render_explicit", lambda value: rendered.append(value) or "value"
    )
    monkeypatch.setattr(
        cli.sync,
        "apply_explicit",
        lambda value, workspace: applied.append(value) or result,
    )
    assert cli.main(["sync", "--new", "-r", "x", "--yes"]) == 0
    assert rendered == [plan, result]
    assert applied == [plan]


def test_yes_does_not_grant_bootstrap_replacement_authority(monkeypatch):
    cli = load_script()
    intents = []
    monkeypatch.setattr(
        cli.sync,
        "plan_explicit",
        lambda _workspace, intent: intents.append(intent) or cli.sync.Blocked(()),
    )
    monkeypatch.setattr(cli.sync, "render_explicit", lambda _value: "blocked")

    assert cli.main(["sync", "--pr", "17", "-r", "x", "--yes"]) == 2
    assert intents == [cli.sync.ExistingIntent(17, "x", bootstrap_local_wins=False)]


def test_noop_and_blocked_never_prompt_or_apply(monkeypatch):
    cli = load_script()
    noop = cli.sync.NoOp(SimpleNamespace(), SimpleNamespace())
    blocked = cli.sync.Blocked(())
    monkeypatch.setattr(cli.sync, "render_explicit", lambda _: "plan")
    monkeypatch.setattr(cli.sync, "apply_explicit", lambda *_: pytest.fail("applied"))
    monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("prompted"))
    monkeypatch.setattr(cli.sync, "plan_explicit", lambda *_: noop)
    assert cli.main(["sync", "--pr", "1"]) == 0
    monkeypatch.setattr(cli.sync, "plan_explicit", lambda *_: blocked)
    assert cli.main(["sync", "--pr", "1"]) == 2


def test_noninteractive_mutation_requires_yes(monkeypatch):
    cli = load_script()
    monkeypatch.setattr(cli.sync, "plan_explicit", lambda *_: object())
    monkeypatch.setattr(cli.sync, "render_explicit", lambda _: "plan")
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    with pytest.raises(SystemExit, match="2"):
        cli.main(["sync", "--pr", "1"])


@pytest.mark.parametrize(
    ("answer", "applies"), [("yes", True), ("n", False), ("", False)]
)
def test_interactive_prompt_defaults_no(monkeypatch, answer, applies):
    cli = load_script()
    calls = []
    monkeypatch.setattr(cli.sync, "plan_explicit", lambda *_: object())
    monkeypatch.setattr(cli.sync, "render_explicit", lambda _: "value")
    monkeypatch.setattr(
        cli.sync, "apply_explicit", lambda *args: calls.append(args) or object()
    )
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: answer)
    assert cli.main(["sync", "--pr", "1"]) == 0
    assert bool(calls) is applies


def test_resume_does_not_do_cli_observation(monkeypatch):
    cli = load_script()
    observed = []
    monkeypatch.setattr(
        cli.sync,
        "plan_explicit",
        lambda _, intent: observed.append(intent) or object(),
    )
    monkeypatch.setattr(cli.sync, "render_explicit", lambda _: "plan")
    monkeypatch.setattr(cli.sync, "apply_explicit", lambda *_: object())
    assert cli.main(["sync", "--resume", "--yes"]) == 0
    assert observed == [cli.sync.ResumeIntent()]


def test_detached_commands_dispatch_by_resolved_number(monkeypatch, capsys):
    cli = load_script()
    association, result = object(), object()
    monkeypatch.setattr(
        cli.sync,
        "resolve_detached_association",
        lambda _, number: association if number == 42 else None,
    )
    monkeypatch.setattr(cli.sync, "render_detached_result", lambda value: "done")
    closed, forgotten = [], []
    monkeypatch.setattr(
        cli.sync,
        "close_detached_association",
        lambda _, value: closed.append(value) or result,
    )
    monkeypatch.setattr(
        cli.sync,
        "forget_detached_association",
        lambda _, value: forgotten.append(value) or result,
    )
    assert cli.main(["detached", "close", "--pr", "42"]) == 0
    assert cli.main(["detached", "forget", "--pr", "42"]) == 0
    assert closed == [association] and forgotten == [association]
    assert capsys.readouterr().out == "done\ndone\n"


def test_detached_list_only_renders(monkeypatch, capsys):
    cli = load_script()
    values = (object(),)
    monkeypatch.setattr(cli.sync, "list_detached_associations", lambda _: values)
    monkeypatch.setattr(
        cli.sync,
        "render_detached_associations",
        lambda got: "LIST" if got is values else "bad",
    )
    assert cli.main(["detached", "list"]) == 0
    assert capsys.readouterr().out == "LIST\n"


def test_installed_executable_smoke():
    assert os.access(SCRIPT, os.X_OK)
    result = subprocess.run(
        [SCRIPT, "--help"], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0
    assert "sync" in result.stdout and "detached" in result.stdout


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
