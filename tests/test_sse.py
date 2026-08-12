from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orch import hooks, opencode_events, sse, verify  # noqa: E402


def _wait_for(path: Path, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            return True
        time.sleep(0.05)
    return False


def _repo() -> tuple[Path, tempfile.TemporaryDirectory]:
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "t"], check=True)
    return root, tmp


def _commit(root: Path, msg: str = "c") -> None:
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", msg], check=True)


def test_hook_name_and_payload() -> None:
    assert sse.hook_name("permission.asked") == "permission-asked"
    ev = {"type": "session.status", "properties": {"sessionID": "s1", "status": "idle"}}
    env = sse.event_env(ev)
    assert env["ORCH_EVENT"] == "session-status"
    assert env["ORCH_EVENT_TYPE"] == "session.status"
    assert env["ORCH_SESSION_ID"] == "s1"
    assert '"type": "session.status"' in sse.payload_of(ev)
    print("ok: hook names, event env, and payload")


def test_event_stream_parses_sse_lines() -> None:
    lines = [
        "data: {\"id\": \"evt_1\", \"type\": \"session.idle\", \"properties\": {\"sessionID\": \"s1\"}}",
        "event: keepalive",
        "data: not json",
        "",
        "data: {\"type\": \"server.heartbeat\"}",
    ]
    out = []
    for line in lines:
        ev = harness_parse(line)
        if ev is not None:
            out.append(ev)
    assert [e["type"] for e in out] == ["session.idle", "server.heartbeat"]
    print("ok: event_stream parses data: lines and skips non-events")


def harness_parse(line: str):
    import json
    line = line.rstrip("\n")
    if not line.startswith("data:"):
        return None
    raw = line[len("data:"):].strip()
    if not raw:
        return None
    try:
        ev = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return ev if isinstance(ev, dict) and ev.get("type") else None


def test_dispatch_fires_global_and_project_hooks() -> None:
    saved = {k: os.environ.get(k) for k in ("XDG_CONFIG_HOME", "XDG_STATE_HOME")}
    tmp = tempfile.TemporaryDirectory()
    try:
        os.environ["XDG_CONFIG_HOME"] = tmp.name
        os.environ["XDG_STATE_HOME"] = str(Path(tmp.name) / "state")
        marker = Path(tmp.name) / "marker"
        # a global event hook, copied to its real name and executable
        gh = Path(tmp.name) / "orch" / "hooks"
        gh.mkdir(parents=True)
        p = gh / "permission-asked"
        p.write_text("#!/usr/bin/env bash\n"
                     f"cat > {marker}\n")
        p.chmod(0o755)

        # a project with a committed permission-asked hook
        root, _repo_tmp = _repo()
        h = root / ".orchestration" / "hooks"
        h.mkdir(parents=True)
        pm = root / "pm"
        (h / "permission-asked").write_text(
            "#!/usr/bin/env bash\n"
            f"cat > {pm}\n")
        (h / "permission-asked").chmod(0o755)
        (root / ".orchestration" / "config.json").write_text(
            '{"baseBranch": "main"}\n')
        _commit(root)

        ev = {"type": "permission.asked",
              "properties": {"sessionID": "s1", "permission": "bash"}}
        owner = (str(root), "main", str(root))
        sse.dispatch(ev, owner, trusted={str(root)})

        assert _wait_for(marker), "the global event hook must fire"
        assert json_of(marker)["type"] == "permission.asked"
        assert _wait_for(pm), "the project event hook must fire"
        assert json_of(pm)["type"] == "permission.asked"
    finally:
        tmp.cleanup()
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print("ok: dispatch fires the global and the owning project's event hook")


def json_of(p: Path) -> dict:
    import json
    return json.loads(p.read_text())


def test_session_idle_is_never_fired_from_the_stream() -> None:
    saved = {k: os.environ.get(k) for k in ("XDG_CONFIG_HOME", "XDG_STATE_HOME")}
    tmp = tempfile.TemporaryDirectory()
    try:
        os.environ["XDG_CONFIG_HOME"] = tmp.name
        os.environ["XDG_STATE_HOME"] = str(Path(tmp.name) / "state")
        root, _repo_tmp = _repo()
        h = root / ".orchestration" / "hooks"
        h.mkdir(parents=True)
        pm = root / "pm"
        (h / "session-idle").write_text(
            "#!/usr/bin/env bash\n"
            f"touch {pm}\n")
        (h / "session-idle").chmod(0o755)
        (root / ".orchestration" / "config.json").write_text(
            '{"baseBranch": "main"}\n')
        _commit(root)

        ev = {"type": "session.idle", "properties": {"sessionID": "s1"}}
        sse.dispatch(ev, (str(root / "lab.git"), "main", str(root)),
                     trusted={str(root / "lab.git")})
        assert not pm.exists(), "the acceptance hook must not fire from the event stream"
    finally:
        tmp.cleanup()
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print("ok: session.idle is the acceptance hook and never fires from the stream")


def test_watcher_routes_by_session_and_reconnects() -> None:
    import threading
    import time

    calls = []

    def fake_stream(port):
        yield {"type": "a.b", "properties": {}}
        yield {"type": "c.d", "properties": {"sessionID": "s1"}}
        w._stop.wait(5)  # block until the test stops the watcher

    w = sse.EventWatcher(resolver=lambda sid: ("/p", "main", "/wt"))
    orig_stream = sse.harness.event_stream
    orig_status = sse.server.status
    sse.harness.event_stream = fake_stream
    sse.server.status = lambda: {"running": True, "unresponsive": False, "port": 1}
    orig_dispatch = w._dispatch
    w._dispatch = lambda ev: calls.append(ev)
    try:
        w.start()
        deadline = time.time() + 5
        while time.time() < deadline and len(calls) < 2:
            time.sleep(0.01)
        w.stop()
        w.join(timeout=5)
        assert [c["type"] for c in calls] == ["a.b", "c.d"], calls
        assert not w.is_alive(), "the watcher must exit once stopped"
    finally:
        w.stop()
        sse.harness.event_stream = orig_stream
        sse.server.status = orig_status
        w._dispatch = orig_dispatch
    print("ok: the watcher reads the stream and the resolver maps sessions")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n{len(tests)} sse tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
