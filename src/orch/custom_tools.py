from __future__ import annotations

import os
import shutil
from pathlib import Path

# Sources shipped with orch: the task-intake bridge. In OpenCode 2 custom tools
# are registered by a *plugin* (there is no `tools/` directory any more), so orch
# installs one plugin whose `setup` registers create_task/update_task/
# archive_task/delete_task through `ctx.tool.transform`. The drafting guidance
# lives in the tool descriptions themselves - a skill would only drift from them.
_TOOLS_SRC = Path(__file__).resolve().parent / "tools"

# The global plugin directory OpenCode 2 discovers for every project.
PLUGIN_DIR = Path("~/.config/opencode/plugins").expanduser()

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


def plugin_paths() -> list[Path]:
    return [PLUGIN_DIR / src.name for src in tool_sources()]


def ensure() -> bool:
    """Make the machine's global opencode plugin current. True when anything was
    written (a fresh plugin is picked up by the service as it reloads)."""
    changed = False
    for src in tool_sources():
        changed |= _write_if_different(PLUGIN_DIR / src.name, _render(src.read_text()))
    return changed


def check() -> list[str]:
    """Doctor-style problems: missing or stale plugin files. Empty when
    everything is current."""
    problems: list[str] = []
    for src in tool_sources():
        dest = PLUGIN_DIR / src.name
        if not dest.exists():
            problems.append(f"custom-tool plugin {src.name} is not installed at "
                            f"{dest}; run `orch install` or `orch server restart`")
        elif dest.read_bytes() != _render(src.read_text()):
            problems.append(f"custom-tool plugin {src.name} at {dest} is stale; "
                            f"run `orch install` or `orch server restart`")
    return problems
