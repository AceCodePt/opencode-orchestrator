"""Tests for shared-server bookkeeping.

No opencode process is started here. What is tested is the part that decides
whether to *kill* something, because that is where a mistake is unrecoverable:
`pkill -f 'opencode serve'` already killed an unrelated shell during development,
and a stale pid file eventually points at whatever process inherited that number.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orch import server  # noqa: E402


def _isolate() -> tempfile.TemporaryDirectory:
    tmp = tempfile.TemporaryDirectory()
    os.environ["XDG_STATE_HOME"] = tmp.name
    return tmp


def test_start_ticks_reads_own_process() -> None:
    ticks = server.proc_start_ticks(os.getpid())
    assert isinstance(ticks, int) and ticks > 0
    assert server.proc_start_ticks(os.getpid()) == ticks, "must be stable"
    print("ok: process start time is readable and stable")


def test_start_ticks_handles_a_comm_with_spaces() -> None:
    """/proc/<pid>/stat's comm field can contain spaces and parens.

    Splitting on whitespace from the left mis-indexes every later field, which would
    silently produce the wrong start time - and a wrong start time means either
    refusing to kill our own server or agreeing to kill someone else's process.
    """
    p = subprocess.Popen(["bash", "-c", "exec -a 'we ird (name)' sleep 5"])
    try:
        time.sleep(0.3)
        ticks = server.proc_start_ticks(p.pid)
        assert isinstance(ticks, int) and ticks > 0, "parsing must survive odd comm"
        assert server.pid_alive(p.pid, ticks)
    finally:
        p.kill()
        p.wait()
    print("ok: stat parsing survives a comm containing spaces and parentheses")


def test_pid_reuse_is_detected() -> None:
    p = subprocess.Popen(["sleep", "5"])
    try:
        time.sleep(0.2)
        real = server.proc_start_ticks(p.pid)
        assert server.pid_alive(p.pid, real)
        # Same pid, wrong start time: this is what a recycled pid looks like, and
        # treating it as ours is how a supervisor kills a stranger's process.
        assert not server.pid_alive(p.pid, (real or 0) + 999), \
            "a mismatched start time must not count as alive"
    finally:
        p.kill()
        p.wait()
    print("ok: a recycled pid is not mistaken for our server")


def test_dead_pid_is_not_alive() -> None:
    p = subprocess.Popen(["true"])
    p.wait()
    time.sleep(0.1)
    assert not server.pid_alive(p.pid, 12345)
    assert not server.pid_alive(-1)
    print("ok: a dead pid is dead")


def test_state_round_trip_and_stale_clear() -> None:
    tmp = _isolate()
    try:
        assert server.read_state() is None
        info = server.ServerInfo(pid=os.getpid(), port=4096, url="http://127.0.0.1:4096",
                                 started_at=int(time.time()),
                                 start_ticks=server.proc_start_ticks(os.getpid()))
        server.write_state(info)
        back = server.read_state()
        assert back and back.pid == info.pid and back.port == 4096
        assert back.alive, "this process is obviously alive"

        # A record whose process is gone must be cleared rather than reported as a
        # server, otherwise every later call tries to talk to nothing.
        server.write_state(server.ServerInfo(
            pid=999999, port=1, url="http://127.0.0.1:1", started_at=1, start_ticks=1))
        assert server.current() is None
        assert server.read_state() is None, "current() must clear a stale record"
    finally:
        tmp.cleanup()
    print("ok: state round-trips and stale records are cleared")


def test_status_distinguishes_gone_from_unresponsive() -> None:
    tmp = _isolate()
    try:
        assert server.status()["running"] is False
        # Alive but not an opencode server: the pid answers, the port does not. This
        # is the dangerous middle state a naive `kill -0` check calls "up".
        server.write_state(server.ServerInfo(
            pid=os.getpid(), port=1, url="http://127.0.0.1:1",
            started_at=int(time.time()),
            start_ticks=server.proc_start_ticks(os.getpid())))
        st = server.status()
        assert st["running"] is True
        assert st["unresponsive"] is True, "a live pid that answers nothing is not up"
    finally:
        tmp.cleanup()
    print("ok: status separates gone from alive-but-unresponsive")


def test_port_parsing_matches_the_real_announcement() -> None:
    line = "opencode server listening on http://127.0.0.1:39153"
    m = server.LISTEN_RE.search(line)
    assert m, "the announcement format is how the port is discovered at all"
    url = m.group(1)
    assert url == "http://127.0.0.1:39153"
    assert server._port_of(url) == 39153
    # The announcement format is not ours to control, so tolerate the variations:
    # a trailing slash broke the original trailing-digits regex.
    assert server._port_of("http://127.0.0.1:4096/") == 4096
    assert server._port_of("http://localhost:8080/api") == 8080
    assert server._port_of(" http://127.0.0.1:1234 ") == 1234
    print("ok: the announced port is parsed out of the real log line")


def test_stop_without_a_server_is_not_an_error() -> None:
    tmp = _isolate()
    try:
        assert server.stop() is False, "stopping nothing must be a no-op, not a throw"
    finally:
        tmp.cleanup()
    print("ok: stopping nothing is a no-op")


def main() -> int:
    saved = os.environ.get("XDG_STATE_HOME")
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    try:
        for t in tests:
            t()
    finally:
        if saved is None:
            os.environ.pop("XDG_STATE_HOME", None)
        else:
            os.environ["XDG_STATE_HOME"] = saved
    print(f"\n{len(tests)} server tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
