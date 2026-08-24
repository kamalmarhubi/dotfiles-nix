#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.12"
# ///
"""Deterministic evidence harness for jj-stack live GitHub acceptance tests."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pty
import re
import select
import shlex
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

OID_RE = re.compile(r"[0-9a-f]{40}")
LABEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def now() -> str:
    return datetime.now(UTC).isoformat()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def session(root: Path) -> dict[str, object]:
    path = root / "session.json"
    if not path.is_file():
        raise SystemExit(f"acceptance session does not exist: {root}")
    return json.loads(path.read_text())


def verify_session(root: Path) -> dict[str, object]:
    metadata = session(root)
    expected = str(metadata["source_sha256"])
    copied = root / "jj-stack-source"
    if sha256(copied) != expected:
        raise SystemExit("retained jj-stack source no longer matches session hash")
    return metadata


def append_event(root: Path, event: dict[str, object]) -> None:
    verify_session(root)
    event = {"at": now(), **event}
    with (root / "events.jsonl").open("a") as output:
        output.write(json.dumps(event, sort_keys=True) + "\n")


def create_session(args: argparse.Namespace) -> None:
    root = args.root.resolve()
    source = args.source.resolve()
    actual = sha256(source)
    if actual != args.expected_sha256:
        raise SystemExit(
            f"source SHA-256 mismatch: expected {args.expected_sha256}, got {actual}"
        )
    try:
        root.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise SystemExit(
            f"refusing to reuse acceptance artifact root: {root}"
        ) from None
    copied = root / "jj-stack-source"
    shutil.copy2(source, copied)
    metadata = {
        "created_at": now(),
        "root": str(root),
        "source": str(source),
        "source_sha256": actual,
        "github_repo": args.github_repo,
    }
    write_json(root / "session.json", metadata)
    (root / "events.jsonl").touch()
    append_event(root, {"event": "session-created"})
    print(root)


def exact_oid(args: argparse.Namespace) -> None:
    result = subprocess.run(
        [
            "jj",
            "--repository",
            str(args.repo),
            "log",
            "--no-graph",
            "--ignore-working-copy",
            "--revision",
            args.revision,
            "--template",
            'commit_id ++ "\\n"',
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    values = result.stdout.splitlines()
    if result.returncode or len(values) != 1 or not OID_RE.fullmatch(values[0]):
        sys.stderr.write(result.stderr)
        raise SystemExit(
            f"revision must resolve to exactly one full commit OID: {args.revision!r}"
        )
    print(values[0])


def run_text(command: list[str], *, cwd: Path) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        sys.stderr.write(result.stderr)
        raise SystemExit(f"command failed ({result.returncode}): {shlex.join(command)}")
    return result.stdout


def commit_parts(repo: Path, oid: str) -> tuple[list[str], str]:
    commit = run_text(
        ["git", "--no-replace-objects", "cat-file", "commit", oid], cwd=repo
    )
    headers, separator, message = commit.partition("\n\n")
    if not separator:
        raise SystemExit(f"invalid Git commit object: {oid}")
    return headers.splitlines(), message


def commit_fingerprint(repo: Path, oid: str) -> dict[str, object]:
    headers, message = commit_parts(repo, oid)
    stable_headers = [
        line for line in headers if line.startswith(("tree ", "author ", "encoding "))
    ]
    return {"headers": stable_headers, "message": message}


def resolve_bookmark(repo: Path, bookmark: str) -> str:
    revset = f"bookmarks(exact:{json.dumps(bookmark)})"
    values = run_text(
        [
            "jj",
            "log",
            "--ignore-working-copy",
            "--no-graph",
            "--revision",
            revset,
            "--template",
            'commit_id ++ "\\n"',
        ],
        cwd=repo,
    ).splitlines()
    if len(values) != 1 or not OID_RE.fullmatch(values[0]):
        raise SystemExit(f"bookmark must resolve to one exact OID: {bookmark!r}")
    return values[0]


def linear_commits(repo: Path, base: str, tip: str) -> list[str]:
    commits = run_text(
        [
            "git",
            "--no-replace-objects",
            "rev-list",
            "--reverse",
            "--ancestry-path",
            f"{base}..{tip}",
        ],
        cwd=repo,
    ).splitlines()
    if not commits:
        raise SystemExit("fixture stack range is empty")
    previous = base
    for oid in commits:
        parents = (
            run_text(
                ["git", "--no-replace-objects", "show", "-s", "--format=%P", oid],
                cwd=repo,
            )
            .strip()
            .split()
        )
        if parents != [previous]:
            raise SystemExit(
                f"fixture stack is not a direct linear chain at {oid[:12]}"
            )
        previous = oid
    if commits[-1] != tip:
        raise SystemExit("fixture tip is not the end of the selected linear chain")
    return commits


def remote_head_oids(repo: Path) -> set[str]:
    remotes = run_text(["git", "remote"], cwd=repo).splitlines()
    if "origin" not in remotes:
        return set()
    lines = run_text(["git", "ls-remote", "--heads", "origin"], cwd=repo).splitlines()
    return {line.split()[0] for line in lines}


def prepare_linear(args: argparse.Namespace) -> None:
    root = args.root.resolve()
    verify_session(root)
    repo = args.repo.resolve()
    if not OID_RE.fullmatch(args.base):
        raise SystemExit("--base must be one exact 40-character Git OID")
    old_tip = resolve_bookmark(repo, args.tip_bookmark)
    old_commits = linear_commits(repo, args.base, old_tip)
    published = remote_head_oids(repo)
    already_published = [oid for oid in old_commits if oid in published]
    if already_published:
        raise SystemExit(
            "refusing to repair commits already published as remote branch heads: "
            + ", ".join(oid[:12] for oid in already_published)
        )

    fingerprints = [commit_fingerprint(repo, oid) for oid in old_commits]
    missing = []
    for oid in old_commits:
        headers, _message = commit_parts(repo, oid)
        count = sum(line.startswith("change-id ") for line in headers)
        if count > 1:
            raise SystemExit(f"commit {oid[:12]} has multiple change-id headers")
        if count == 0:
            missing.append(oid)

    before_operation = run_text(
        ["jj", "op", "log", "--no-graph", "--limit", "1", "-T", "id"], cwd=repo
    ).strip()
    command = None
    if missing:
        command = [
            "jj",
            "--ignore-immutable",
            "metaedit",
            "--update-change-id",
            *missing,
        ]
        run_text(command, cwd=repo)

    new_tip = resolve_bookmark(repo, args.tip_bookmark)
    new_commits = linear_commits(repo, args.base, new_tip)
    if len(new_commits) != len(old_commits):
        raise SystemExit("repair changed the number of commits in the fixture stack")
    for old, new, fingerprint in zip(
        old_commits, new_commits, fingerprints, strict=True
    ):
        if commit_fingerprint(repo, new) != fingerprint:
            raise SystemExit(f"repair changed fixture semantics for {old[:12]}")
        headers, _message = commit_parts(repo, new)
        count = sum(line.startswith("change-id ") for line in headers)
        if count != 1:
            raise SystemExit(
                f"repaired commit {new[:12]} does not have exactly one change-id header"
            )

    after_operation = run_text(
        ["jj", "op", "log", "--no-graph", "--limit", "1", "-T", "id"], cwd=repo
    ).strip()
    result = {
        "base": args.base,
        "tip_bookmark": args.tip_bookmark,
        "old_tip": old_tip,
        "new_tip": new_tip,
        "missing_change_ids": missing,
        "metaedit_argv": command,
        "before_operation": before_operation,
        "after_operation": after_operation,
        "commits": [
            {"old_oid": old, "new_oid": new}
            for old, new in zip(old_commits, new_commits, strict=True)
        ],
    }
    output = root / "fixture-preparation.json"
    if output.exists():
        raise SystemExit(f"refusing to overwrite fixture preparation: {output}")
    write_json(output, result)
    append_event(
        root,
        {
            "event": "fixture-prepared",
            "repaired": len(missing),
            "old_tip": old_tip,
            "new_tip": new_tip,
        },
    )
    print(json.dumps(result, indent=2, sort_keys=True))


def capture(
    output: Path,
    name: str,
    command: list[str],
    *,
    cwd: Path,
    required: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        check=False,
    )
    (output / f"{name}.stdout").write_bytes(result.stdout)
    (output / f"{name}.stderr").write_bytes(result.stderr)
    (output / f"{name}.exit").write_text(f"{result.returncode}\n")
    if required and result.returncode:
        raise SystemExit(
            f"snapshot command failed ({result.returncode}): {shlex.join(command)}"
        )
    return result


def snapshot(args: argparse.Namespace) -> None:
    root = args.root.resolve()
    metadata = verify_session(root)
    if not LABEL_RE.fullmatch(args.label):
        raise SystemExit(f"invalid snapshot label: {args.label!r}")
    output = root / "snapshots" / args.label
    try:
        output.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise SystemExit(f"refusing to overwrite snapshot: {output}") from None

    repo = args.repo.resolve()
    (output / "captured-at.txt").write_text(now() + "\n")
    commands = [
        ("git-show-ref", ["git", "show-ref"], False),
        (
            "git-heads",
            ["git", "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads"],
            True,
        ),
        ("remote-heads", ["git", "ls-remote", "--heads", "origin"], True),
        (
            "jj-op",
            [
                "jj",
                "op",
                "log",
                "--no-graph",
                "-T",
                'id ++ " " ++ description ++ "\\n"',
            ],
            True,
        ),
        (
            "jj-bookmarks",
            ["jj", "bookmark", "list", "--all-remotes", "--color=never"],
            True,
        ),
        ("jj-status", ["jj", "status", "--color=never"], True),
        ("jj-workspaces", ["jj", "workspace", "list"], True),
        (
            "jj-log",
            [
                "jj",
                "log",
                "--ignore-working-copy",
                "--color=never",
                "--revision",
                "all()",
                "--template",
                'commit_id ++ " " ++ change_id ++ " " ++ bookmarks ++ " " ++ description.first_line() ++ "\\n"',
            ],
            True,
        ),
    ]
    for name, command, required in commands:
        capture(output, name, command, cwd=repo, required=required)

    fetch_head = repo / ".git" / "FETCH_HEAD"
    (output / "FETCH_HEAD").write_bytes(
        fetch_head.read_bytes() if fetch_head.is_file() else b""
    )
    github_repo = args.github_repo or metadata.get("github_repo")
    if github_repo:
        pr_result = capture(
            output,
            "github-prs",
            [
                "gh",
                "pr",
                "list",
                "--repo",
                str(github_repo),
                "--state",
                "all",
                "--limit",
                "100",
                "--json",
                "number,id,url,state,isDraft,title,body,headRefName,headRefOid,baseRefName,mergeCommit,createdAt,updatedAt,closedAt,mergedAt,autoMergeRequest,mergeStateStatus",
            ],
            cwd=repo,
        )
        for pr in json.loads(pr_result.stdout):
            number = int(pr["number"])
            capture(
                output,
                f"github-pr-{number}-timeline",
                [
                    "gh",
                    "api",
                    "-H",
                    "Accept: application/vnd.github+json",
                    f"repos/{github_repo}/issues/{number}/timeline?per_page=100",
                ],
                cwd=repo,
            )
            capture(
                output,
                f"github-pr-{number}-rest",
                ["gh", "api", f"repos/{github_repo}/pulls/{number}"],
                cwd=repo,
            )

    manifest_lines = []
    for path in sorted(output.iterdir()):
        if path.is_file() and path.name != "MANIFEST.sha256":
            manifest_lines.append(f"{sha256(path)}  {path.name}")
    (output / "MANIFEST.sha256").write_text("\n".join(manifest_lines) + "\n")
    append_event(root, {"event": "snapshot", "label": args.label})
    print(output)


def command_result(
    root: Path,
    name: str,
    command: list[str],
    cwd: Path,
    result: subprocess.CompletedProcess[bytes],
) -> None:
    output = root / "commands" / name
    output.mkdir(parents=True, exist_ok=False)
    (output / "argv.json").write_text(json.dumps(command, indent=2) + "\n")
    (output / "cwd.txt").write_text(str(cwd) + "\n")
    (output / "stdout").write_bytes(result.stdout)
    (output / "stderr").write_bytes(result.stderr)
    (output / "exit").write_text(f"{result.returncode}\n")
    append_event(
        root,
        {
            "event": "command",
            "name": name,
            "argv": command,
            "cwd": str(cwd),
            "exit": result.returncode,
        },
    )


def run_command(args: argparse.Namespace) -> None:
    root = args.root.resolve()
    verify_session(root)
    if not LABEL_RE.fullmatch(args.name):
        raise SystemExit(f"invalid command name: {args.name!r}")
    if not args.command:
        raise SystemExit("missing command after --")
    result = subprocess.run(
        args.command,
        cwd=args.cwd,
        capture_output=True,
        check=False,
    )
    command_result(root, args.name, args.command, args.cwd.resolve(), result)
    sys.stdout.buffer.write(result.stdout)
    sys.stderr.buffer.write(result.stderr)
    raise SystemExit(result.returncode)


def run_pty(args: argparse.Namespace) -> None:
    root = args.root.resolve()
    verify_session(root)
    if not LABEL_RE.fullmatch(args.name):
        raise SystemExit(f"invalid PTY command name: {args.name!r}")
    if not args.command:
        raise SystemExit("missing command after --")
    output = root / "commands" / args.name
    output.mkdir(parents=True, exist_ok=False)
    (output / "argv.json").write_text(json.dumps(args.command, indent=2) + "\n")
    (output / "cwd.txt").write_text(str(args.cwd.resolve()) + "\n")

    master, slave = pty.openpty()
    process = subprocess.Popen(
        args.command,
        cwd=args.cwd,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        close_fds=True,
    )
    os.close(slave)
    raw = bytearray()
    prompt = args.prompt.encode()
    prompt_seen = False
    deadline = time.monotonic() + args.timeout
    hook_result: subprocess.CompletedProcess[bytes] | None = None
    try:
        while process.poll() is None or select.select([master], [], [], 0)[0]:
            if time.monotonic() > deadline:
                process.kill()
                raise SystemExit(f"PTY command timed out after {args.timeout} seconds")
            readable, _, _ = select.select([master], [], [], 0.1)
            if readable:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                raw.extend(chunk)
            if not prompt_seen and prompt in raw:
                prompt_seen = True
                if args.hook:
                    hook_result = subprocess.run(
                        args.hook,
                        cwd=args.cwd,
                        shell=True,
                        capture_output=True,
                        check=False,
                    )
                    (output / "hook.stdout").write_bytes(hook_result.stdout)
                    (output / "hook.stderr").write_bytes(hook_result.stderr)
                    (output / "hook.exit").write_text(f"{hook_result.returncode}\n")
                    if hook_result.returncode:
                        process.kill()
                        raise SystemExit("PTY prompt hook failed")
                os.write(master, args.response.encode() + b"\n")
        returncode = process.wait()
    finally:
        os.close(master)
        (output / "transcript.raw").write_bytes(raw)
        (output / "transcript.txt").write_text(raw.decode(errors="backslashreplace"))
    prompt_count = raw.count(prompt)
    (output / "exit").write_text(f"{returncode}\n")
    write_json(
        output / "pty.json",
        {
            "prompt": args.prompt,
            "prompt_count": prompt_count,
            "response": args.response,
            "hook": args.hook,
            "hook_exit": hook_result.returncode if hook_result else None,
        },
    )
    append_event(
        root,
        {
            "event": "pty-command",
            "name": args.name,
            "argv": args.command,
            "exit": returncode,
            "prompt_count": prompt_count,
        },
    )
    sys.stdout.buffer.write(raw)
    if prompt_count != 1:
        raise SystemExit(f"expected prompt exactly once, observed {prompt_count}")
    raise SystemExit(returncode)


GH_WRAPPER = r"""#!/usr/bin/env python3
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

real = os.environ["JJ_STACK_ACCEPTANCE_REAL_GH"]
log = Path(os.environ["JJ_STACK_ACCEPTANCE_GH_LOG"])
args = sys.argv[1:]
is_link = args[:2] == ["stack", "link"]

def refs():
    result = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return {"exit": result.returncode, "stdout": result.stdout, "stderr": result.stderr}

entry = {
    "at": datetime.now(timezone.utc).isoformat(),
    "argv": args,
    "cwd": os.getcwd(),
    "is_stack_link": is_link,
    "refs_before": refs(),
}
if is_link and os.environ.get("JJ_STACK_ACCEPTANCE_FAIL_LINK") == "1":
    entry["injected_exit"] = int(os.environ.get("JJ_STACK_ACCEPTANCE_FAIL_EXIT", "97"))
    with log.open("a") as output:
        output.write(json.dumps(entry, sort_keys=True) + "\n")
    raise SystemExit(entry["injected_exit"])

result = subprocess.run([real, *args])
entry["real_exit"] = result.returncode
entry["refs_after"] = refs()
with log.open("a") as output:
    output.write(json.dumps(entry, sort_keys=True) + "\n")
raise SystemExit(result.returncode)
"""


def make_gh_wrapper(args: argparse.Namespace) -> None:
    root = args.root.resolve()
    verify_session(root)
    wrapper_dir = root / "gh-wrapper"
    wrapper_dir.mkdir(exist_ok=False)
    wrapper = wrapper_dir / "gh"
    wrapper.write_text(GH_WRAPPER)
    wrapper.chmod(0o755)
    real_gh = shutil.which("gh")
    if real_gh is None:
        raise SystemExit("gh is not installed")
    environment = {
        "PATH": f"{wrapper_dir}:{os.environ['PATH']}",
        "JJ_STACK_ACCEPTANCE_REAL_GH": real_gh,
        "JJ_STACK_ACCEPTANCE_GH_LOG": str(root / "gh-wrapper.jsonl"),
        "JJ_STACK_ACCEPTANCE_FAIL_LINK": "1" if args.fail_link else "0",
        "JJ_STACK_ACCEPTANCE_FAIL_EXIT": str(args.fail_exit),
    }
    write_json(root / "gh-wrapper-env.json", environment)
    append_event(
        root,
        {"event": "gh-wrapper-created", "fail_link": args.fail_link},
    )
    for key, value in environment.items():
        print(f"export {key}={shlex.quote(value)}")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    subparsers = result.add_subparsers(dest="subcommand", required=True)

    create = subparsers.add_parser("create")
    create.add_argument("--root", type=Path, required=True)
    create.add_argument("--source", type=Path, required=True)
    create.add_argument("--expected-sha256", required=True)
    create.add_argument("--github-repo")
    create.set_defaults(function=create_session)

    oid = subparsers.add_parser("exact-oid")
    oid.add_argument("--repo", type=Path, required=True)
    oid.add_argument("revision")
    oid.set_defaults(function=exact_oid)

    prepare = subparsers.add_parser("prepare-linear")
    prepare.add_argument("--root", type=Path, required=True)
    prepare.add_argument("--repo", type=Path, required=True)
    prepare.add_argument("--base", required=True)
    prepare.add_argument("--tip-bookmark", required=True)
    prepare.set_defaults(function=prepare_linear)

    snap = subparsers.add_parser("snapshot")
    snap.add_argument("--root", type=Path, required=True)
    snap.add_argument("--repo", type=Path, required=True)
    snap.add_argument("--github-repo")
    snap.add_argument("label")
    snap.set_defaults(function=snapshot)

    run = subparsers.add_parser("run")
    run.add_argument("--root", type=Path, required=True)
    run.add_argument("--cwd", type=Path, required=True)
    run.add_argument("--name", required=True)
    run.add_argument("command", nargs=argparse.REMAINDER)
    run.set_defaults(function=run_command)

    terminal = subparsers.add_parser("pty")
    terminal.add_argument("--root", type=Path, required=True)
    terminal.add_argument("--cwd", type=Path, required=True)
    terminal.add_argument("--name", required=True)
    terminal.add_argument("--prompt", required=True)
    terminal.add_argument("--response", required=True)
    terminal.add_argument("--hook")
    terminal.add_argument("--timeout", type=float, default=300)
    terminal.add_argument("command", nargs=argparse.REMAINDER)
    terminal.set_defaults(function=run_pty)

    wrapper = subparsers.add_parser("make-gh-wrapper")
    wrapper.add_argument("--root", type=Path, required=True)
    wrapper.add_argument("--fail-link", action="store_true")
    wrapper.add_argument("--fail-exit", type=int, default=97)
    wrapper.set_defaults(function=make_gh_wrapper)
    return result


def main() -> None:
    args = parser().parse_args()
    if getattr(args, "command", [None])[:1] == ["--"]:
        args.command = args.command[1:]
    args.function(args)


if __name__ == "__main__":
    main()
