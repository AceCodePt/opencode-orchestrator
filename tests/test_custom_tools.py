from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orch import custom_tools, install  # noqa: E402


def test_ensure_installs_tools_with_the_backend_path_baked_in() -> None:
    tmp = tempfile.TemporaryDirectory()
    saved_tools = custom_tools.TOOLS_DIR
    custom_tools.TOOLS_DIR = Path(tmp.name) / "tools"
    try:
        assert custom_tools.check(), "nothing installed yet must fail doctor"
        assert custom_tools.ensure() is True, "first install must report a change"
        for name in ("create_task", "update_task", "archive_task", "delete_task"):
            assert (custom_tools.TOOLS_DIR / f"{name}.ts").exists(), name
            body = (custom_tools.TOOLS_DIR / f"{name}.ts").read_text()
            assert custom_tools.TOOL_BIN_PLACEHOLDER not in body, \
                "the placeholder must be substituted"
            assert '"tasktool", op' in body, \
                "the bridge must spawn `bin/orch tasktool <op> ...`"
        assert "wait_human_start" in (custom_tools.TOOLS_DIR / "create_task.ts").read_text(), \
            "the drafting guidance lives in the tool description"
        assert not custom_tools.check(), "a current install must pass doctor"
        assert custom_tools.ensure() is False, "an unchanged install is a no-op"
    finally:
        custom_tools.TOOLS_DIR = saved_tools
        tmp.cleanup()
    print("ok: ensure installs the tools (path baked in), idempotently")


def test_render_substitutes_the_placeholder() -> None:
    rendered = custom_tools._render("const BIN = '{{ORCH_BIN}}'")
    assert b"{{ORCH_BIN}}" not in rendered
    assert rendered.endswith(b"'") and b"bin/orch" in rendered
    print("ok: the tool bridge gets the orch dispatcher path")


def test_tool_bin_resolves_to_the_canonical_install_when_one_exists() -> None:
    tmp = tempfile.TemporaryDirectory()
    saved = {k: os.environ.get(k) for k in ("HOME", "XDG_DATA_HOME")}
    os.environ["HOME"] = str(Path(tmp.name) / "home")
    os.environ["XDG_DATA_HOME"] = str(Path(tmp.name) / "data")
    try:
        assert install.canonical_bin() is None
        assert custom_tools._tool_bin() == Path(__file__).resolve().parents[1] / "bin" / "orch", \
            "without an install, the running tree's bin/orch is used"

        install.install()
        assert custom_tools._tool_bin() == install.install_dir() / "bin" / "orch", \
            "with an install present, every process bakes the same canonical path"
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        tmp.cleanup()
    print("ok: _tool_bin() prefers the canonical install so writers can't disagree")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n{len(tests)} custom-tools tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
