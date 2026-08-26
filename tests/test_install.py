from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orch import cli, install  # noqa: E402


def _set_env(tmp: Path) -> dict[str, str | None]:
    saved = {k: os.environ.get(k) for k in ("HOME", "XDG_DATA_HOME", "PATH")}
    os.environ["HOME"] = str(tmp / "home")
    os.environ["XDG_DATA_HOME"] = str(tmp / "data")
    os.environ["PATH"] = "/usr/bin:/bin"
    return saved


def _restore_env(saved: dict[str, str | None]) -> None:
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def test_install_creates_the_copy_and_launcher() -> None:
    tmp = tempfile.TemporaryDirectory()
    saved = _set_env(Path(tmp.name))
    try:
        status, lines = install.install()
        assert status == "created", (status, lines)
        dst = install.install_dir()
        assert dst.is_dir()
        assert (dst / "bin" / "orch").is_file()
        assert (dst / "src" / "orch" / "cli.py").is_file()
        assert not (dst / ".git").exists(), \
            "the copy must be .git-free so it stays a static snapshot"
        launcher = install.launcher_path()
        assert launcher.is_file(), "the launcher must be a real file"
        assert not launcher.is_symlink(), "the launcher must not be a symlink"
        assert launcher.read_text() == install.launcher_text()
        assert launcher.stat().st_mode & 0o111, "the launcher must be executable"
    finally:
        _restore_env(saved)
        tmp.cleanup()
    print("ok: install copies the repo (minus .git) and writes a real launcher")


def test_install_is_idempotent() -> None:
    tmp = tempfile.TemporaryDirectory()
    saved = _set_env(Path(tmp.name))
    try:
        status, _ = install.install()
        assert status == "created"
        status2, _ = install.install()
        assert status2 == "already current", status2
    finally:
        _restore_env(saved)
        tmp.cleanup()
    print("ok: a second install reports already current")


def test_install_reports_updated_when_the_copy_is_stale() -> None:
    tmp = tempfile.TemporaryDirectory()
    saved = _set_env(Path(tmp.name))
    try:
        install.install()
        dst = install.install_dir()
        (dst / "README.md").write_text("stale\n")
        status, _ = install.install()
        assert status == "updated", status
        assert (dst / "README.md").read_text() != "stale\n", \
            "the stale copy must be refreshed"
        status2, _ = install.install()
        assert status2 == "already current", status2
    finally:
        _restore_env(saved)
        tmp.cleanup()
    print("ok: a stale installed copy is updated back to the current source")


def test_install_refuses_a_foreign_file_at_the_launcher() -> None:
    tmp = tempfile.TemporaryDirectory()
    saved = _set_env(Path(tmp.name))
    try:
        launcher = install.launcher_path()
        launcher.parent.mkdir(parents=True, exist_ok=True)
        launcher.write_text("#!/bin/sh\necho mine\n")
        status, lines = install.install()
        assert status == "refused", (status, lines)
        assert launcher.read_text() == "#!/bin/sh\necho mine\n", \
            "the foreign file must survive an install refusal"
    finally:
        _restore_env(saved)
        tmp.cleanup()
    print("ok: install refuses to clobber a non-orch file at the launcher path")


def _run_launcher(tmp: Path, *argv: str) -> subprocess.CompletedProcess:
    env = {k: os.environ[k] for k in ("HOME", "XDG_DATA_HOME")}
    env["PATH"] = "/usr/bin:/bin"
    return subprocess.run(
        [sys.executable, str(install.launcher_path()), *argv],
        capture_output=True, text=True, env=env,
        cwd=str(tmp / "cwd"),
    )


def test_launcher_runs_the_installed_copy_without_a_checkout() -> None:
    tmp = tempfile.TemporaryDirectory()
    saved = _set_env(Path(tmp.name))
    try:
        (Path(tmp.name) / "cwd").mkdir()
        install.install()
        p = _run_launcher(Path(tmp.name), "version")
        assert p.returncode == 0, p.stderr
        assert p.stdout.strip() == f"orch {cli.__version__}", p.stdout
    finally:
        _restore_env(saved)
        tmp.cleanup()
    print("ok: the launcher runs `orch version` from the installed src, "
          "no checkout present")


def test_install_uninstall_round_trip() -> None:
    tmp = tempfile.TemporaryDirectory()
    saved = _set_env(Path(tmp.name))
    try:
        (Path(tmp.name) / "cwd").mkdir()
        install.install()
        p = _run_launcher(Path(tmp.name), "version")
        assert p.returncode == 0 and "orch" in p.stdout

        lines = install.uninstall()
        assert not install.launcher_path().exists(), \
            "uninstall must remove the launcher"
        assert not install.install_dir().exists(), \
            "uninstall must remove the canonical install dir"
        assert any(".orchestration" in l for l in lines), lines
    finally:
        _restore_env(saved)
        tmp.cleanup()
    print("ok: install/uninstall round-trips cleanly")


def test_uninstall_never_touches_a_projects_orchestration() -> None:
    tmp = tempfile.TemporaryDirectory()
    saved = _set_env(Path(tmp.name))
    try:
        project = Path(tmp.name) / "lab"
        orch = project / ".orchestration"
        orch.mkdir(parents=True)
        marker = orch / "config.json"
        marker.write_text("keep me\n")

        install.install()
        install.uninstall()

        assert marker.exists() and marker.read_text() == "keep me\n", \
            "a project's .orchestration must survive orch uninstall"
        assert not install.install_dir().exists()
        assert not install.launcher_path().exists()
    finally:
        _restore_env(saved)
        tmp.cleanup()
    print("ok: uninstall never touches any project's .orchestration/")


def test_path_hint_offers_the_export_line() -> None:
    tmp = tempfile.TemporaryDirectory()
    saved = _set_env(Path(tmp.name))
    try:
        hint = install.path_hint()
        assert any('export PATH="$HOME/.local/bin:$PATH"' in l for l in hint), hint

        os.environ["PATH"] = str(install.launcher_dir()) + ":/usr/bin:/bin"
        assert install.path_hint() == [], "on PATH, no hint is needed"
    finally:
        _restore_env(saved)
        tmp.cleanup()
    print("ok: the PATH hint appears only when the launcher dir is missing")


def test_canonical_bin_tracks_the_installed_copy() -> None:
    tmp = tempfile.TemporaryDirectory()
    saved = _set_env(Path(tmp.name))
    try:
        assert install.canonical_bin() is None, \
            "no install yet means no canonical bin"

        install.install()
        canonical = install.canonical_bin()
        assert canonical is not None
        assert canonical == install.install_dir() / "bin" / "orch"
        assert canonical.is_file(), "the canonical bin must be the installed binary"
    finally:
        _restore_env(saved)
        tmp.cleanup()
    print("ok: canonical_bin points at the installed binary once an install exists")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n{len(tests)} install tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())