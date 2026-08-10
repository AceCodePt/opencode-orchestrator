from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orch import hooks, init, opencode_events  # noqa: E402


def test_catalog_is_well_formed() -> None:
    for event, props in opencode_events.OPENCODE_EVENTS.items():
        assert re.fullmatch(r"[a-z][a-z0-9-]*(\.[a-z][a-z0-9-]*)*", event), event
        if props:
            assert re.fullmatch(r"[a-zA-Z][a-zA-Z0-9]*"
                                r"(,[a-zA-Z][a-zA-Z0-9]*)*", props), (event, props)
    print(f"ok: catalog holds {len(opencode_events.OPENCODE_EVENTS)} "
          f"well-formed events")


def test_hook_names_are_unique() -> None:
    names = [opencode_events.hook_name(e) for e in opencode_events.OPENCODE_EVENTS]
    assert len(names) == len(set(names)), \
        "two events must not map to one hook name"
    print("ok: dots-to-dashes hook names are unique")


def test_hook_names_do_not_clash_with_other_hooks() -> None:
    reserved = set(hooks.GLOBAL_HOOKS) | set(init.TASK_HOOKS) | {"session-idle"}
    names = {opencode_events.hook_name(e) for e in opencode_events.OPENCODE_EVENTS}
    assert names & reserved == {"session-idle"}, names & reserved
    print("ok: event hook names clash with no lifecycle or global hook; "
          "session-idle is reserved for the gate")


def test_hook_name_dots_to_dashes() -> None:
    assert opencode_events.hook_name("session.idle") == "session-idle"
    assert opencode_events.hook_name("permission.v2.asked") == "permission-v2-asked"
    assert opencode_events.hook_name("session.next.tool.success") == \
        "session-next-tool-success"
    print("ok: hook names map dots to dashes")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n{len(tests)} opencode-events tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
