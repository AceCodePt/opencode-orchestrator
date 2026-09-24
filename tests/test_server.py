from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orch import harness, server  # noqa: E402


def _isolate() -> tempfile.TemporaryDirectory:
    tmp = tempfile.TemporaryDirectory()
    os.environ["XDG_STATE_HOME"] = tmp.name
    os.environ["ORCH_SERVICE_FILE"] = str(Path(tmp.name) / "service.json")
    return tmp


def test_start_ticks_reads_own_process() -> None:
    ticks = server.proc_start_ticks(os.getpid())
    assert isinstance(ticks, int) and ticks > 0
    assert server.proc_start_ticks(os.getpid()) == ticks, "must be stable"
    print("ok: process start time is readable and stable")


def test_start_ticks_handles_a_comm_with_spaces() -> None:
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


def test_no_service_file_means_no_server() -> None:
    tmp = _isolate()
    try:
        assert server.read_service_file() is None
        assert server.current() is None
        assert server.status()["running"] is False
        assert server.stop() is False, "stopping nothing is a no-op"
    finally:
        tmp.cleanup()
    print("ok: no registered service reads as no server")


def test_current_reads_the_service_registration_and_registers_auth() -> None:
    tmp = _isolate()
    try:
        port = 49876
        server.service_file().write_text(json.dumps({
            "id": "svc", "version": "2.0.15",
            "url": f"http://127.0.0.1:{port}", "pid": os.getpid(),
            "password": "sekret",
        }))
        info = server.current()
        assert info is not None, "a live pid in service.json is the service"
        assert info.port == port and info.password == "sekret"
        assert info.version == "2.0.15"
        assert harness._AUTH.get(port) == "sekret", \
            "discovery must register the password so calls authenticate"
    finally:
        harness.clear_auth(port)
        tmp.cleanup()
    print("ok: current() reads the service registration and registers auth")


def test_a_stale_service_pid_reads_as_no_server() -> None:
    tmp = _isolate()
    try:
        server.service_file().write_text(json.dumps({
            "id": "svc", "version": "2.0.15",
            "url": "http://127.0.0.1:1", "pid": 999999,
            "password": "x",
        }))
        assert server.current() is None, "a dead recorded pid is not the service"
    finally:
        tmp.cleanup()
    print("ok: a stale service pid does not read as a server")


def test_port_parsing_matches_the_service_url() -> None:
    assert server._port_of("http://127.0.0.1:39153") == 39153
    assert server._port_of("http://127.0.0.1:4096/") == 4096
    assert server._port_of("http://localhost:8080/api") == 8080
    assert server._port_of(" http://127.0.0.1:1234 ") == 1234
    print("ok: the service url yields its port")


def test_state_dir_is_owner_only() -> None:
    import stat

    tmp = _isolate()
    try:
        assert stat.S_IMODE(server.state_dir().stat().st_mode) == 0o700, \
            "the state dir must not be traversable by other users"
    finally:
        tmp.cleanup()
    print("ok: the state directory is created 0700")


def main() -> int:
    saved = {k: os.environ.get(k)
             for k in ("XDG_STATE_HOME", "ORCH_SERVICE_FILE")}
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    try:
        for t in tests:
            t()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print(f"\n{len(tests)} server tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
