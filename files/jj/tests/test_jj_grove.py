# /// script
# requires-python = ">=3.12"
# dependencies = ["pytest"]
# ///
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
JJ_CONFIG = REPO_ROOT / "files" / "jj" / "config.toml"
JJ_GROVE = REPO_ROOT / "files" / "jj" / "jj-grove"
Cwd = Optional[Union[Path, str]]


@dataclass
class GroveRepo:
    root: Path
    repo: Path
    origin: Path
    env: dict[str, str]

    def run(
        self,
        *args: str,
        cwd: Cwd = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            list(args),
            cwd=cwd or self.repo,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if check and result.returncode != 0:
            pytest.fail(
                f"`{' '.join(args)}` failed with exit {result.returncode}\n"
                f"cwd: {cwd or self.repo}\n"
                f"stdout:\n{result.stdout}\n"
                f"stderr:\n{result.stderr}"
            )
        return result

    def jj(
        self, *args: str, cwd: Cwd = None, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        return self.run("jj", *args, cwd=cwd, check=check)

    def grove(
        self, *args: str, cwd: Cwd = None, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        return self.run(sys.executable, str(JJ_GROVE), *args, cwd=cwd, check=check)

    def jj_stdout(self, *args: str, cwd: Cwd = None) -> str:
        return self.jj(*args, cwd=cwd).stdout.strip()

    def revset_exists(self, revset: str, cwd: Cwd = None) -> bool:
        out = self.jj_stdout("log", "--no-graph", "-r", revset, "-T", '"x\n"', cwd=cwd)
        return bool(out)

    def revset_count(self, revset: str, cwd: Cwd = None) -> int:
        out = self.jj_stdout("log", "--no-graph", "-r", revset, "-T", '"x\n"', cwd=cwd)
        return len(out.splitlines()) if out else 0

    def op_id(self, cwd: Cwd = None) -> str:
        return self.jj_stdout(
            "op", "log", "-n1", "--no-graph", "-T", "self.id()", cwd=cwd
        )

    def workspace_names(self, cwd: Cwd = None) -> list[str]:
        out = self.jj_stdout("workspace", "list", "-T", 'name ++ "\n"', cwd=cwd)
        return out.splitlines() if out else []

    def change_ids(self, revset: str, cwd: Cwd = None) -> list[str]:
        out = self.jj_stdout(
            "log",
            "--no-graph",
            "-r",
            revset,
            "-T",
            'change_id.shortest(8) ++ "\n"',
            cwd=cwd,
        )
        return out.splitlines() if out else []

    def bookmark_revset(self, name: str) -> str:
        return f'bookmarks(exact:"{name}")'

    def config_value(self, key: str, cwd: Cwd = None) -> str:
        return self.jj_stdout("config", "get", key, cwd=cwd)

    def create_grove(self, slug: str = "alpha") -> Path:
        result = self.grove("new", slug)
        return Path(result.stdout.strip())

    def create_commit(
        self, cwd: Path, filename: str, contents: str, message: str
    ) -> None:
        (cwd / filename).write_text(contents)
        self.jj("describe", "-m", message, cwd=cwd)

    def remote_heads(self) -> set[str]:
        result = self.run(
            "git",
            "--git-dir",
            str(self.origin),
            "for-each-ref",
            "--format=%(refname)",
            "refs/heads",
            cwd=self.root,
        )
        return set(result.stdout.splitlines())


@pytest.fixture
def grove_repo(tmp_path: Path) -> GroveRepo:
    home = tmp_path / "home"
    xdg_config_home = tmp_path / "xdg"
    repo = tmp_path / "repo"
    origin = tmp_path / "origin.git"

    (xdg_config_home / "jj").mkdir(parents=True)
    shutil.copy2(JJ_CONFIG, xdg_config_home / "jj" / "config.toml")
    home.mkdir()
    repo.mkdir()

    env = os.environ.copy()
    env.pop("PYENV_VERSION", None)
    env.update(
        {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(xdg_config_home),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "NO_COLOR": "1",
            "PAGER": "cat",
        }
    )

    harness = GroveRepo(
        root=tmp_path,
        repo=repo,
        origin=origin,
        env=env,
    )
    harness.run("git", "init", "--bare", str(origin), cwd=tmp_path)
    harness.jj("git", "init", "--colocate", ".", cwd=repo)
    (repo / "README.md").write_text("initial\n")
    harness.jj("describe", "-m", "initial")
    harness.jj("bookmark", "create", "main", "-r", "@")
    harness.jj("git", "remote", "add", "origin", str(origin))
    harness.jj("git", "push", "--remote", "origin", "-b", "main")
    return harness


def test_new_bare_creates_empty_base_bookmark(grove_repo: GroveRepo) -> None:
    workspaces_before = grove_repo.workspace_names()

    grove_repo.grove("new", "alpha", "--bare")

    assert grove_repo.revset_exists("kamal/alpha/base & empty()")
    assert grove_repo.workspace_names() == workspaces_before
    assert (
        grove_repo.jj_stdout(
            "log",
            "--no-graph",
            "-r",
            "kamal/alpha/base",
            "-T",
            "description.first_line()",
        )
        == "alpha base"
    )


def test_new_creates_bound_workspace_on_base(grove_repo: GroveRepo) -> None:
    workspace = grove_repo.create_grove("alpha")

    assert workspace.is_dir()
    assert (
        grove_repo.config_value('revset-aliases."grove_base()"', cwd=workspace)
        == "kamal/alpha/base"
    )
    assert (
        grove_repo.config_value('revset-aliases."grove_bookmarks()"', cwd=workspace)
        == 'grove_bookmarks("kamal/alpha/*")'
    )
    assert grove_repo.revset_exists("@ & descendants(kamal/alpha/base)", cwd=workspace)
    assert not grove_repo.revset_exists("@ & kamal/alpha/base", cwd=workspace)


def test_workspace_add_binds_second_workspace_to_existing_base(
    grove_repo: GroveRepo,
) -> None:
    grove_repo.grove("new", "alpha", "--bare")

    result = grove_repo.grove("workspace", "add", "alpha", "side")
    workspace = Path(result.stdout.strip())

    assert workspace.is_dir()
    assert (
        grove_repo.config_value('revset-aliases."grove_base()"', cwd=workspace)
        == "kamal/alpha/base"
    )
    assert (
        grove_repo.config_value('revset-aliases."grove_bookmarks()"', cwd=workspace)
        == 'grove_bookmarks("kamal/alpha/*")'
    )
    assert grove_repo.revset_exists("@ & descendants(kamal/alpha/base)", cwd=workspace)
    assert not grove_repo.revset_exists("@ & kamal/alpha/base", cwd=workspace)


def test_graft_head_publishes_bookmark_and_removes_private_descendants(
    grove_repo: GroveRepo,
) -> None:
    workspace = grove_repo.create_grove("alpha")
    grove_repo.create_commit(workspace, "a.txt", "A\n", "A")
    grove_repo.jj("new", cwd=workspace)
    grove_repo.create_commit(workspace, "b.txt", "B\n", "B")
    b_change = grove_repo.change_ids("@", cwd=workspace)[0]

    result = grove_repo.grove("graft", "@", cwd=workspace)
    names = result.stdout.splitlines()

    assert len(names) == 1
    assert re.fullmatch(r"kamal/alpha/\d{4}-\d{2}-\d{2}-.+", names[0])
    assert grove_repo.revset_exists(
        f"{grove_repo.bookmark_revset(names[0])} & {b_change}", cwd=workspace
    )
    assert grove_repo.revset_exists(f"{b_change} & descendants(trunk())", cwd=workspace)
    assert (
        grove_repo.revset_count(
            "descendants(kamal/alpha/base) ~ kamal/alpha/base", cwd=workspace
        )
        == 0
    )


def test_graft_prefix_keeps_later_work_private(grove_repo: GroveRepo) -> None:
    workspace = grove_repo.create_grove("alpha")
    grove_repo.create_commit(workspace, "a.txt", "A\n", "A")
    a_change = grove_repo.change_ids("@", cwd=workspace)[0]
    grove_repo.jj("new", cwd=workspace)
    grove_repo.create_commit(workspace, "b.txt", "B\n", "B")
    b_change = grove_repo.change_ids("@", cwd=workspace)[0]

    result = grove_repo.grove("graft", a_change, cwd=workspace)
    names = result.stdout.splitlines()

    assert len(names) == 1
    assert grove_repo.revset_exists(
        f"{grove_repo.bookmark_revset(names[0])} & {a_change}", cwd=workspace
    )
    assert grove_repo.revset_exists(f"{a_change} & descendants(trunk())", cwd=workspace)
    assert not grove_repo.revset_exists(
        f"{a_change} & descendants(kamal/alpha/base)", cwd=workspace
    )
    assert grove_repo.revset_exists(
        f"{b_change} & descendants(kamal/alpha/base)", cwd=workspace
    )


def test_sync_no_fetch_rebases_base_onto_current_trunk(grove_repo: GroveRepo) -> None:
    workspace = grove_repo.create_grove("alpha")
    grove_repo.create_commit(workspace, "work.txt", "work\n", "work")
    work_change = grove_repo.change_ids("@", cwd=workspace)[0]

    grove_repo.create_commit(grove_repo.repo, "upstream.txt", "upstream\n", "upstream")
    grove_repo.jj("bookmark", "move", "main", "--to", "@")
    grove_repo.jj("git", "push", "--remote", "origin", "-b", "main")

    grove_repo.grove("sync", "--no-fetch", cwd=workspace)

    assert grove_repo.revset_exists(
        "parents(kamal/alpha/base) & trunk()", cwd=workspace
    )
    assert grove_repo.revset_exists(
        f"{work_change} & descendants(kamal/alpha/base)", cwd=workspace
    )


def test_sync_prunes_merged_grove_bookmarks_but_keeps_base(
    grove_repo: GroveRepo,
) -> None:
    workspace = grove_repo.create_grove("alpha")
    grove_repo.jj(
        "bookmark", "create", "kamal/alpha/merged", "-r", "trunk()", cwd=workspace
    )

    grove_repo.grove("sync", "--no-fetch", cwd=workspace)

    assert not grove_repo.revset_exists("present(kamal/alpha/merged)", cwd=workspace)
    assert grove_repo.revset_exists("present(kamal/alpha/base)", cwd=workspace)


def test_sync_refuses_grove_bookmark_on_base_descendant(grove_repo: GroveRepo) -> None:
    workspace = grove_repo.create_grove("alpha")
    grove_repo.create_commit(workspace, "work.txt", "work\n", "work")
    grove_repo.jj("bookmark", "create", "kamal/alpha/live", "-r", "@", cwd=workspace)
    op_before = grove_repo.op_id(cwd=workspace)

    result = grove_repo.grove("sync", "--no-fetch", cwd=workspace, check=False)

    assert result.returncode != 0
    assert "sync would rebase them" in result.stderr
    assert grove_repo.op_id(cwd=workspace) == op_before
    assert grove_repo.revset_exists(
        "kamal/alpha/live & descendants(kamal/alpha/base)", cwd=workspace
    )


def test_push_excludes_base_bookmark(grove_repo: GroveRepo) -> None:
    workspace = grove_repo.create_grove("alpha")
    grove_repo.jj(
        "bookmark", "create", "kamal/alpha/published", "-r", "trunk()", cwd=workspace
    )

    grove_repo.grove("push", cwd=workspace)

    heads = grove_repo.remote_heads()
    assert "refs/heads/kamal/alpha/published" in heads
    assert "refs/heads/kamal/alpha/base" not in heads


def test_push_flushes_deleted_remote_bookmarks(grove_repo: GroveRepo) -> None:
    workspace = grove_repo.create_grove("alpha")
    bookmark = "kamal/alpha/published"
    remote_ref = f"refs/heads/{bookmark}"
    grove_repo.jj("bookmark", "create", bookmark, "-r", "trunk()", cwd=workspace)
    grove_repo.grove("push", cwd=workspace)
    assert remote_ref in grove_repo.remote_heads()

    grove_repo.jj("bookmark", "delete", bookmark, cwd=workspace)
    grove_repo.grove("push", cwd=workspace)

    assert remote_ref not in grove_repo.remote_heads()


def test_rm_force_deletes_bound_workspace_and_base_bookmark(
    grove_repo: GroveRepo,
) -> None:
    workspace = grove_repo.create_grove("alpha")

    grove_repo.grove("rm", "alpha", "--force", cwd=grove_repo.repo)

    assert not workspace.exists()
    assert not grove_repo.revset_exists(
        "present(kamal/alpha/base)", cwd=grove_repo.repo
    )


def test_rm_refuses_non_empty_grove_work_without_force(grove_repo: GroveRepo) -> None:
    workspace = grove_repo.create_grove("alpha")
    grove_repo.create_commit(workspace, "work.txt", "work\n", "work")
    op_before = grove_repo.op_id()

    result = grove_repo.grove("rm", "alpha", cwd=grove_repo.repo, check=False)

    assert result.returncode != 0
    assert "non-empty local commits" in result.stderr
    assert grove_repo.op_id() == op_before
    assert workspace.exists()
    assert grove_repo.revset_exists("present(kamal/alpha/base)")


def test_graft_refuses_non_empty_base(grove_repo: GroveRepo) -> None:
    workspace = grove_repo.create_grove("alpha")
    (workspace / "base.txt").write_text("base\n")
    grove_repo.jj("squash", "--into", "kamal/alpha/base", "-u", cwd=workspace)
    op_before = grove_repo.op_id(cwd=workspace)

    result = grove_repo.grove("graft", "@", cwd=workspace, check=False)

    assert result.returncode != 0
    assert "NONEMPTY-BASE" in result.stderr
    assert grove_repo.op_id(cwd=workspace) == op_before
    assert grove_repo.revset_exists("kamal/alpha/base & ~empty()", cwd=workspace)


def test_use_rebase_binds_unbound_workspace_to_grove(grove_repo: GroveRepo) -> None:
    grove_repo.grove("new", "alpha", "--bare")

    grove_repo.grove("use", "alpha", "--rebase")

    assert (
        grove_repo.config_value('revset-aliases."grove_base()"') == "kamal/alpha/base"
    )
    assert (
        grove_repo.config_value('revset-aliases."grove_bookmarks()"')
        == 'grove_bookmarks("kamal/alpha/*")'
    )
    assert grove_repo.revset_exists("@ & descendants(kamal/alpha/base)")
    assert not grove_repo.revset_exists("@ & kamal/alpha/base")


def test_use_refuses_to_rebase_across_bound_groves(grove_repo: GroveRepo) -> None:
    workspace = grove_repo.create_grove("alpha")
    grove_repo.grove("new", "beta", "--bare", cwd=grove_repo.repo)
    op_before = grove_repo.op_id(cwd=workspace)

    result = grove_repo.grove("use", "beta", "--rebase", cwd=workspace, check=False)

    assert result.returncode != 0
    assert "use won't rebase across groves" in result.stderr
    assert grove_repo.op_id(cwd=workspace) == op_before
    assert (
        grove_repo.config_value('revset-aliases."grove_base()"', cwd=workspace)
        == "kamal/alpha/base"
    )


def test_new_bare_dry_run_prints_plan_without_mutating(grove_repo: GroveRepo) -> None:
    op_before = grove_repo.op_id()
    workspaces_before = grove_repo.workspace_names()

    result = grove_repo.grove("new", "-n", "alpha", "--bare")

    assert "jj new --no-edit trunk()" in result.stderr
    assert "jj bookmark create kamal/alpha/base" in result.stderr
    assert grove_repo.op_id() == op_before
    assert grove_repo.workspace_names() == workspaces_before
    assert not grove_repo.revset_exists("present(kamal/alpha/base)")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
