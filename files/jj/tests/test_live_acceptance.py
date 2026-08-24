import hashlib
import json
import os
import subprocess
import sys

import pytest
from live_acceptance import (
    GH_WRAPPER,
    create_session,
    exact_oid,
    prepare_linear,
    run_pty,
)


def args(**values):
    return type("Args", (), values)()


def test_create_session_rejects_hash_mismatch_and_root_reuse(tmp_path):
    source = tmp_path / "source"
    source.write_text("source\n")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    root = tmp_path / "artifacts"

    with pytest.raises(SystemExit, match="source SHA-256 mismatch"):
        create_session(
            args(
                root=root,
                source=source,
                expected_sha256="0" * 64,
                github_repo=None,
            )
        )

    create_session(
        args(
            root=root,
            source=source,
            expected_sha256=digest,
            github_repo="owner/repo",
        )
    )
    assert (root / "jj-stack-source").read_bytes() == source.read_bytes()
    assert json.loads((root / "session.json").read_text())["source_sha256"] == digest

    with pytest.raises(SystemExit, match="refusing to reuse"):
        create_session(
            args(
                root=root,
                source=source,
                expected_sha256=digest,
                github_repo=None,
            )
        )


def test_exact_oid_rejects_ambiguous_revision(tmp_path, monkeypatch, capsys):
    def run(*_args, **_kwargs):
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(SystemExit, match="exactly one full commit OID"):
        exact_oid(args(repo=tmp_path, revision="A"))
    assert capsys.readouterr().out == ""


def test_gh_wrapper_records_exact_refs_and_injects_link_failure(tmp_path):
    repository = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", repository], check=True)
    subprocess.run(["git", "-C", repository, "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", repository, "config", "user.email", "test@example.com"],
        check=True,
    )
    (repository / "file").write_text("content\n")
    subprocess.run(["git", "-C", repository, "add", "file"], check=True)
    subprocess.run(["git", "-C", repository, "commit", "-qm", "initial"], check=True)
    expected = subprocess.run(
        ["git", "-C", repository, "rev-parse", "HEAD"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()

    wrapper = tmp_path / "gh"
    wrapper.write_text(GH_WRAPPER)
    wrapper.chmod(0o755)
    real = tmp_path / "real-gh"
    real.write_text("#!/bin/sh\nexit 0\n")
    real.chmod(0o755)
    log = tmp_path / "gh.jsonl"
    environment = {
        **os.environ,
        "JJ_STACK_ACCEPTANCE_REAL_GH": str(real),
        "JJ_STACK_ACCEPTANCE_GH_LOG": str(log),
        "JJ_STACK_ACCEPTANCE_FAIL_LINK": "1",
        "JJ_STACK_ACCEPTANCE_FAIL_EXIT": "73",
    }
    result = subprocess.run(
        [sys.executable, wrapper, "stack", "link", "--", "new-head"],
        cwd=repository,
        env=environment,
        check=False,
    )

    assert result.returncode == 73
    event = json.loads(log.read_text())
    assert event["argv"] == ["stack", "link", "--", "new-head"]
    assert event["is_stack_link"] is True
    assert (
        f"refs/heads/{subprocess.run(['git', '-C', repository, 'branch', '--show-current'], check=True, text=True, stdout=subprocess.PIPE).stdout.strip()} {expected}"
        in event["refs_before"]["stdout"]
    )


def test_prepare_linear_repairs_git_commits_in_one_metaedit_call(tmp_path):
    source = tmp_path / "source"
    source.write_text("source\n")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    root = tmp_path / "evidence"
    create_session(
        args(
            root=root,
            source=source,
            expected_sha256=digest,
            github_repo=None,
        )
    )

    repository = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", repository], check=True)
    subprocess.run(["git", "-C", repository, "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", repository, "config", "user.email", "test@example.com"],
        check=True,
    )
    for name in ("base", "A", "B"):
        (repository / name).write_text(name + "\n")
        subprocess.run(["git", "-C", repository, "add", name], check=True)
        subprocess.run(["git", "-C", repository, "commit", "-qm", name], check=True)
        if name == "base":
            base = subprocess.run(
                ["git", "-C", repository, "rev-parse", "HEAD"],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()
    subprocess.run(["git", "-C", repository, "branch", "-m", "fixture-tip"], check=True)
    subprocess.run(["jj", "git", "init", "--colocate", repository], check=True)

    prepare_linear(
        args(
            root=root,
            repo=repository,
            base=base,
            tip_bookmark="fixture-tip",
        )
    )

    result = json.loads((root / "fixture-preparation.json").read_text())
    assert len(result["missing_change_ids"]) == 2
    assert result["metaedit_argv"][:4] == [
        "jj",
        "--ignore-immutable",
        "metaedit",
        "--update-change-id",
    ]
    assert result["old_tip"] != result["new_tip"]
    assert result["before_operation"] != result["after_operation"]
    for commit in result["commits"]:
        headers = subprocess.run(
            ["git", "-C", repository, "cat-file", "commit", commit["new_oid"]],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        ).stdout.partition("\n\n")[0]
        assert sum(line.startswith("change-id ") for line in headers.splitlines()) == 1


def test_run_pty_stops_at_eof_after_child_exits(tmp_path, capsys):
    source = tmp_path / "source"
    source.write_text("source\n")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    root = tmp_path / "evidence"
    create_session(
        args(
            root=root,
            source=source,
            expected_sha256=digest,
            github_repo=None,
        )
    )

    with pytest.raises(SystemExit) as exited:
        run_pty(
            args(
                root=root,
                name="prompt",
                command=[
                    "sh",
                    "-c",
                    'printf "Continue? [y/N] "; read answer; printf "done: %s\\n" "$answer"',
                ],
                cwd=tmp_path,
                prompt="Continue? [y/N] ",
                response="y",
                hook=None,
                timeout=2,
            )
        )

    assert exited.value.code == 0
    assert "done: y" in capsys.readouterr().out
    evidence = root / "commands" / "prompt"
    assert (evidence / "exit").read_text() == "0\n"
    assert json.loads((evidence / "pty.json").read_text())["prompt_count"] == 1
