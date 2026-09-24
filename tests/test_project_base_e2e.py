from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orch import cli, daemon, dispatch, project, server, state  # noqa: E402

PASSING_HOOK = """#!/usr/bin/env bash
set -euo pipefail
cd "${WORKTREE_PATH:-$(git rev-parse --show-toplevel)}" || exit 1
grep -q "^answer: 42$" answer.txt
if [ -n "$(git status --porcelain)" ]; then
  git add -A
  git -c user.name=orch -c user.email=orch@localhost \\
      commit -q -m "orch: session-idle verification passed"
fi
exit 0
"""


def _have_opencode() -> bool:
    return shutil.which("opencode") is not None


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True)


def _project(tmp: Path, name: str, task: str) -> Path:
    root = tmp / name
    root.mkdir(parents=True)
    _git("init", "-q", "-b", "main", str(root))
    _git("-C", str(root), "config", "user.email", "t@t")
    _git("-C", str(root), "config", "user.name", "t")
    h = root / ".orchestration" / "hooks"
    h.mkdir(parents=True)
    (root / ".orchestration" / "config.json").write_text(
        '{"baseBranch": "main", "verify": {"retryCap": 3}}\n')
    (root / "seed").write_text("x")
    _git("-C", str(root), "add", "-A")
    _git("-C", str(root), "commit", "-q", "-m", "seed")
    d = root / "tasks" / task
    d.mkdir(parents=True)
    (d / "task.md").write_text(
        "# Done by commit\n\n## Verification\n\n`answer.txt` contains `answer: 42`.\n")
    _git("-C", str(root), "add", "-A")
    _git("-C", str(root), "commit", "-q", "-m", "task")
    return root


def _repoint(root: Path, via_cli: bool) -> Path:
    """Repoint the project's base from `main` onto a second existing branch
    (`next`) - via the backend `project.base()` or the `project base`
    subcommand - and return the store."""
    _top, common, _err = dispatch.project_of(root)
    assert common is not None, _err
    state.trust_project(common)
    assert _git("-C", str(root), "checkout", "-q", "-b", "next").returncode == 0
    assert _git("-C", str(root), "checkout", "-q", "main").returncode == 0
    if via_cli:
        rc = cli.main(["project", "base", "next", "-C", str(root)])
        assert rc == 0, rc
    else:
        r = project.base(common, "next")
        assert r.ok, r.problems
    return Path(common)


def _assert_repointed(store: Path, root: Path) -> Path:
    """The shared final state a repoint must leave, whichever path drove it:
    the base worktree is checked out on `next`, baseBranch=next is committed
    there, and the old `main` base branch still exists as a normal branch."""
    base_wt = root / "next"
    assert base_wt.is_dir(), "the new base worktree is checked out"
    branch = _git("-C", str(base_wt), "branch", "--show-current")
    assert branch.returncode == 0 and branch.stdout.strip() == "next", branch
    cfg = _git("-C", str(store), "show", "next:.orchestration/config.json")
    assert cfg.returncode == 0 and json.loads(cfg.stdout)["baseBranch"] == "next", cfg
    assert _git("-C", str(store), "show-ref", "--verify", "-q",
                "refs/heads/main").returncode == 0, "main still exists"
    assert (root / ".orchestration" / "config.json").is_file(), \
        "the old base worktree remains as an ordinary checkout"
    assert state.is_trusted(store), "repoint must not change trust"
    return base_wt


def _install_hook(base_wt: Path, body: str) -> None:
    p = base_wt / ".orchestration" / "hooks" / "session-idle"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)
    p.chmod(0o755)
    _git("-C", str(base_wt), "add", "-A")
    _git("-C", str(base_wt), "commit", "-q", "-m", "hook")


def _claim_and_commit(store: Path, base: str, name: str,
                      files: dict[str, str]) -> Path:
    wt = store.parent.parent / name
    assert _git("-C", str(store), "worktree", "add", "-b", name,
                str(wt), base).returncode == 0, \
        f"branch {name} from {base} failed"
    for rel, content in files.items():
        (wt / rel).write_text(content)
    _git("-C", str(wt), "add", "-A")
    _git("-C", str(wt), "commit", "-q", "-m", f"done: {name}")
    return wt


def _run_step(root: Path, name: str) -> list[str]:
    _top, common, _err = dispatch.project_of(root)
    assert common is not None, _err
    sup = daemon.Supervisor(project=str(common), roots=[str(root.parent)])
    sup.mem.settled_at[(str(common), name)] = int(time.time()) - 31
    return sup.step()


def _restore(saved: dict[str, str | None]) -> None:
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def test_repoint_backend_and_cli_full_loop_from_the_new_base() -> None:
    if not _have_opencode():
        print("skip: opencode not on PATH (no live server)")
        return
    tmp = tempfile.TemporaryDirectory()
    saved = {k: os.environ.get(k) for k in ("XDG_STATE_HOME",)}
    os.environ["XDG_STATE_HOME"] = str(Path(tmp.name) / "state")
    try:
        t = Path(tmp.name)
        backend = _project(t, "backend-proj", "done-backend")
        cli_proj = _project(t, "cli-proj", "done-cli")
        backend_store = _repoint(backend, via_cli=False)
        cli_store = _repoint(cli_proj, via_cli=True)
        backend_base = _assert_repointed(backend_store, backend)
        cli_base = _assert_repointed(cli_store, cli_proj)
        _install_hook(backend_base, PASSING_HOOK)
        _install_hook(cli_base, PASSING_HOOK)

        server.start()  # the shared service (discovered/started, never stopped)
        backend_wt = _claim_and_commit(backend_store, "next", "done-backend",
                                       {"answer.txt": "answer: 42"})
        cli_wt = _claim_and_commit(cli_store, "next", "done-cli",
                                   {"answer.txt": "answer: 42"})
        log_backend = "\n".join(_run_step(backend, "done-backend"))
        log_cli = "\n".join(_run_step(cli_proj, "done-cli"))

        for joined in (log_backend, log_cli):
            assert "verify passed" in joined, joined
            assert "merged" in joined, joined
            assert "archived" in joined, joined
        assert (backend_base / "answer.txt").read_text() == "answer: 42"
        assert (cli_base / "answer.txt").read_text() == "answer: 42"
        for store, task in ((backend_store, "done-backend"),
                            (cli_store, "done-cli")):
            assert _git("-C", str(store), "show-ref", "--verify", "-q",
                        f"refs/heads/{task}").returncode != 0, \
                "the task branch must be deleted after merge"
            archived = _git("-C", str(store), "ls-tree", "-r", "--name-only",
                            "next", "--", "archive").stdout
            assert f"archive/{task}/task.md" in archived, archived
            assert _git("-C", str(store), "show-ref", "--verify", "-q",
                        "refs/heads/main").returncode == 0, \
                "the old base branch still exists"
        assert not backend_wt.exists(), "the backend task worktree is torn down"
        assert not cli_wt.exists(), "the cli task worktree is torn down"
    finally:
        _restore(saved)
        tmp.cleanup()
    print("ok: repoint via the backend and the CLI both complete the full loop "
          "from the new base, with the old base branch still present")


def main() -> int:
    from orch import custom_tools

    plugin_tmp = tempfile.TemporaryDirectory()
    saved_plugin = custom_tools.PLUGIN_DIR
    custom_tools.PLUGIN_DIR = Path(plugin_tmp.name)
    custom_tools.ensure()  # pre-install so server.start() never bounces the real service
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    try:
        for t in tests:
            t()
    finally:
        custom_tools.PLUGIN_DIR = saved_plugin
        plugin_tmp.cleanup()
    print(f"\n{len(tests)} e2e tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())