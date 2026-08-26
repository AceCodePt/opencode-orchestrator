from __future__ import annotations

import os
import shutil
from pathlib import Path

# Sources shipped with orch: the custom-tool bridges. The drafting guidance
# lives in the tool descriptions themselves - a skill would only drift from
# them, and it would only fire if a model decided to load it.
_TOOLS_SRC = Path(__file__).resolve().parent / "tools"

# Global destination, read by the shared server and every local opencode TUI.
TOOLS_DIR = Path("~/.config/opencode/tools").expanduser()

TOOL_BIN_PLACEHOLDER = "{{ORCH_BIN}}"


def _tool_bin() -> Path:
    """The orch dispatcher the tools shell to. Prefers the canonical install's
    `bin/orch` when one exists so every process bakes the same path; otherwise
    the repo-local bin wins so a checkout works uninstalled."""
    from . import install

    canonical = install.canonical_bin()
    if canonical is not None:
        return canonical
    local = Path(__file__).resolve().parents[2] / "bin" / "orch"
    if local.exists():
        return local
    return Path(shutil.which("orch") or "orch")


def _render(body: str) -> bytes:
    return body.replace(TOOL_BIN_PLACEHOLDER, str(_tool_bin())).encode()


def _write_if_different(path: Path, data: bytes) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.read_bytes() == data:
            return False
    except OSError:
        pass
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    try:
        os.chmod(path, 0o644)
    except OSError:
        pass
    return True


def tool_sources() -> list[Path]:
    if not _TOOLS_SRC.is_dir():
        return []
    return sorted(p for p in _TOOLS_SRC.iterdir() if p.suffix == ".ts")


def ensure() -> bool:
    """Make the machine's global custom tools current. True when anything was
    written (a fresh tool set needs a server restart)."""
    changed = False
    for src in tool_sources():
        changed |= _write_if_different(TOOLS_DIR / src.name, _render(src.read_text()))
    return changed


def check() -> list[str]:
    """Doctor-style problems: missing or stale tool files. Empty when everything
    is current."""
    problems: list[str] = []
    for src in tool_sources():
        dest = TOOLS_DIR / src.name
        if not dest.exists():
            problems.append(f"custom tool {src.name} is not installed at {dest}; "
                            f"run `orch install` or `orch server restart`")
        elif dest.read_bytes() != _render(src.read_text()):
            problems.append(f"custom tool {src.name} at {dest} is stale; "
                            f"run `orch install` or `orch server restart`")
    return problems
