from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orch import init as init_mod  # noqa: E402
from orch import state  # noqa: E402


def _repo() -> tuple[Path, tempfile.TemporaryDirectory]:
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name) / "wt"
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@t"],
                   check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "t"],
                   check=True)
    (root / "seed").write_text("x")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "seed"],
                   check=True)
    return root, tmp


def _common(root: Path) -> Path:
    p = subprocess.run(["git", "-C", str(root), "rev-parse",
                        "--path-format=absolute", "--git-common-dir"],
                       capture_output=True, text=True, check=True)
    return Path(p.stdout.strip())


def test_init_auto_trusts_the_project() -> None:
    saved = os.environ.get("XDG_STATE_HOME")
    stmp = tempfile.TemporaryDirectory()
    os.environ["XDG_STATE_HOME"] = stmp.name
    try:
        root, tmp = _repo()
        assert not state.is_trusted(_common(root))
        r = init_mod.run(root)
        assert state.is_trusted(_common(root)), \
            "`orch init` is the owner setting a project up, so it must trust it"
        assert any("trusted this project" in n for n in r.notes), r.notes
        tmp.cleanup()
    finally:
        stmp.cleanup()
        if saved is None:
            os.environ.pop("XDG_STATE_HOME", None)
        else:
            os.environ["XDG_STATE_HOME"] = saved
    print("ok: orch init trusts the project it scaffolds")


def test_init_writes_global_hook_examples() -> None:
    saved = {k: os.environ.get(k) for k in ("XDG_CONFIG_HOME", "XDG_STATE_HOME")}
    tmp = tempfile.TemporaryDirectory()
    os.environ["XDG_CONFIG_HOME"] = tmp.name
    os.environ["XDG_STATE_HOME"] = str(Path(tmp.name) / "state")
    try:
        root, repo_tmp = _repo()
        r = init_mod.run(root)
        hooks_dir = Path(tmp.name) / "orch" / "hooks"
        from orch import hooks
        assert all((hooks_dir / f"{e}.example").exists()
                   for e in hooks.GLOBAL_HOOKS), r.created
        r2 = init_mod.run(root)
        assert not r2.created, "a second init skips the existing examples"
        repo_tmp.cleanup()
    finally:
        tmp.cleanup()
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print("ok: orch init writes the global hook .example templates")


def test_init_does_not_scaffold_project_samples() -> None:
    saved = {k: os.environ.get(k) for k in ("XDG_CONFIG_HOME", "XDG_STATE_HOME")}
    tmp = tempfile.TemporaryDirectory()
    os.environ["XDG_CONFIG_HOME"] = tmp.name
    os.environ["XDG_STATE_HOME"] = str(Path(tmp.name) / "state")
    try:
        root, repo_tmp = _repo()
        r = init_mod.run(root)
        hooks_dir = root / ".orchestration" / "hooks"
        assert list(hooks_dir.glob("*.sample")) == [], \
            "no hook templates may be scaffolded into the repo"
        gi = (root / ".orchestration" / ".gitignore").read_text()
        assert "*.sample" in gi and "*.example" in gi, \
            "the repo-side gitignore must exclude every inert template"
        cfg_hooks = Path(tmp.name) / "orch" / "hooks"
        assert (cfg_hooks / "session-idle.example").exists(), r.created
        verify = (cfg_hooks / "session-idle.example").read_text()
        assert "no verification configured" in verify and "commit" in verify, \
            "the machine-level session-idle template must be the verification one"
        r2 = init_mod.run(root)
        assert not r2.created, "a second init skips the existing examples"
        repo_tmp.cleanup()
    finally:
        tmp.cleanup()
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print("ok: orch init installs hook templates machine-level, never in the repo")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n{len(tests)} init tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
