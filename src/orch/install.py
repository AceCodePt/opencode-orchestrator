from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

LAUNCHER_MARKER = "# orch launcher - managed by `orch install`; do not edit"


def data_home() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base)


def install_dir() -> Path:
    """The one canonical install location: $XDG_DATA_HOME/orch."""
    return data_home() / "orch"


def launcher_dir() -> Path:
    return Path.home() / ".local" / "bin"


def launcher_path() -> Path:
    return launcher_dir() / "orch"


def repo_root() -> Path:
    """The source tree of the orch that is running: the checkout when
    `bin/orch` runs it, or the installed copy when the launcher does."""
    return Path(__file__).resolve().parents[2]


def launcher_text() -> str:
    return (
        "#!/usr/bin/env python3\n"
        + LAUNCHER_MARKER + "\n"
        "import os\n"
        "import sys\n\n"
        '_share = os.environ.get("XDG_DATA_HOME") or os.path.join(\n'
        '    os.path.expanduser("~"), ".local", "share")\n'
        'sys.path.insert(0, os.path.join(_share, "orch", "src"))\n\n'
        "from orch.cli import main\n\n"
        'if __name__ == "__main__":\n'
        "    raise SystemExit(main())\n"
    )


def _is_launcher(path: Path) -> bool:
    try:
        return LAUNCHER_MARKER in path.read_text(errors="replace")
    except OSError:
        return False


def _write_launcher() -> None:
    path = launcher_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(launcher_text())
    os.chmod(tmp, 0o755)
    tmp.replace(path)
    os.chmod(path, 0o755)


def _manifest(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in filenames:
            if name == ".git":
                continue
            p = Path(dirpath) / name
            out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _copy_tree(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".git"))


def install() -> tuple[str, list[str]]:
    """Copy the running repo (minus .git) to the canonical location and write
    the launcher. Returns (status, lines) with status in created/updated/
    already current; 'refused' means a non-orch file sits at the launcher."""
    lines: list[str] = []
    src = repo_root()
    dst = install_dir()
    launcher = launcher_path()

    if launcher.exists() and not _is_launcher(launcher):
        return "refused", [
            f"refusing to overwrite {launcher}: it exists and is not an "
            "orch-managed launcher",
        ]

    if src.resolve() == dst.resolve():
        tree_status = "already current"
        lines.append(f"the running source is already the canonical install at {dst}")
    elif dst.is_dir() and _manifest(src) == _manifest(dst):
        tree_status = "already current"
        lines.append(f"installed copy at {dst} is already current")
    else:
        existed = dst.exists()
        if existed:
            shutil.rmtree(dst)
        _copy_tree(src, dst)
        tree_status = "updated" if existed else "created"
        lines.append(f"copied orch to {dst} ({tree_status})")

    if launcher.exists() and launcher.read_text() == launcher_text():
        launcher_status = "already current"
        lines.append(f"launcher current at {launcher}")
    else:
        _write_launcher()
        launcher_status = "updated"
        lines.append(f"wrote launcher {launcher}")

    if tree_status == "created":
        status = "created"
    elif tree_status == "updated" or launcher_status == "updated":
        status = "updated"
    else:
        status = "already current"
    lines.append(f"orch install: {status}")
    return status, lines


def path_hint() -> list[str]:
    """The export line a shell needs when the launcher dir is not on PATH."""
    bin_dir = os.path.realpath(str(launcher_dir()))
    entries = [os.path.realpath(p)
               for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    if bin_dir in entries:
        return []
    return [
        'export PATH="$HOME/.local/bin:$PATH"',
        "note: start a new shell (or re-source) for the new PATH to take effect",
    ]


def uninstall() -> list[str]:
    """Remove the launcher and the canonical install directory. Never touches
    any project's `.orchestration/`."""
    lines: list[str] = []
    launcher = launcher_path()
    if launcher.is_symlink() or (launcher.exists() and _is_launcher(launcher)):
        launcher.unlink()
        lines.append(f"removed launcher {launcher}")
    elif launcher.exists():
        lines.append(f"left {launcher} alone: it is not an orch-managed launcher")
    else:
        lines.append(f"no launcher at {launcher}")

    dst = install_dir()
    if dst.exists():
        shutil.rmtree(dst)
        lines.append(f"removed install {dst}")
    else:
        lines.append(f"no install at {dst}")
    lines.append("left every project's .orchestration/ alone")
    return lines