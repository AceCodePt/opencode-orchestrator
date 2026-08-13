from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orch import custom_tools  # noqa: E402


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


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n{len(tests)} custom-tools tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
