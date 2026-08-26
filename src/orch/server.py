from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, asdict
from pathlib import Path

from . import harness

LISTEN_RE = re.compile(r"listening on\s+(https?://\S+)")
PORT_RE = re.compile(r":(\d+)\s*$")

START_TIMEOUT_S = 30.0
STOP_GRACE_S = 5.0


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    d = Path(base) / "orch"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        d.chmod(0o700)
    except OSError:
        pass
    return d


def _state_file() -> Path:
    return state_dir() / "server.json"


def _log_file() -> Path:
    return state_dir() / "server.log"


def _neutral_cwd() -> Path:
    d = state_dir() / "server-cwd"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def proc_start_ticks(pid: int) -> int | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    close = raw.rfind(")")
    if close == -1:
        return None
    fields = raw[close + 2:].split()
    try:
        return int(fields[19])
    except (IndexError, ValueError):
        return None


def pid_alive(pid: int, start_ticks: int | None = None) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    if start_ticks is None:
        return True
    return proc_start_ticks(pid) == start_ticks


@dataclass
class ServerInfo:
    pid: int
    port: int
    url: str
    started_at: int
    start_ticks: int | None = None

    @property
    def alive(self) -> bool:
        return pid_alive(self.pid, self.start_ticks)

    def healthy(self) -> bool:
        return bool((harness.health(self.port) or {}).get("healthy"))


def read_state() -> ServerInfo | None:
    try:
        raw = json.loads(_state_file().read_text())
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return ServerInfo(
            pid=int(raw["pid"]),
            port=int(raw["port"]),
            url=str(raw["url"]),
            started_at=int(raw.get("started_at", 0)),
            start_ticks=raw.get("start_ticks"),
        )
    except (KeyError, TypeError, ValueError):
        return None


def write_state(info: ServerInfo) -> None:
    p = _state_file()
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(info), indent=2))
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(p)


def clear_state() -> None:
    try:
        _state_file().unlink()
    except OSError:
        pass


def current() -> ServerInfo | None:
    info = read_state()
    if info is None:
        return None
    if not info.alive:
        clear_state()
        return None
    return info


def status() -> dict:
    info = read_state()
    if info is None:
        return {"running": False, "reason": "no recorded server"}
    if not info.alive:
        return {"running": False, "reason": f"recorded pid {info.pid} is gone",
                "stale": True, "pid": info.pid, "port": info.port}
    caps = harness.capabilities(info.port)
    return {
        "running": True,
        "pid": info.pid,
        "port": info.port,
        "url": info.url,
        "healthy": caps.healthy,
        "version": ".".join(map(str, caps.version)) if caps.version else None,
        "uptime_s": int(time.time()) - info.started_at if info.started_at else None,
        "started_at": info.started_at,
        "unresponsive": not caps.healthy,
    }


def orch_bin() -> str:
    """The orch dispatcher (server ORCH_BIN env, daemon unit ExecStart). Prefers
    the canonical install's `bin/orch` when one exists so every process bakes
    the same path; otherwise the repo-local bin wins so a checkout works
    uninstalled."""
    from . import install

    canonical = install.canonical_bin()
    if canonical is not None:
        return str(canonical)
    local = Path(__file__).resolve().parents[2] / "bin" / "orch"
    if local.exists():
        return str(local)
    return shutil.which("orch") or "orch"


def start(reuse: bool = True, opencode: str = "opencode") -> ServerInfo:
    from . import custom_tools

    if custom_tools.ensure():
        existing = current()
        if existing:
            stop()
        reuse = False

    if reuse:
        existing = current()
        if existing and existing.healthy():
            return existing
        if existing:
            stop()

    log = _log_file()
    log.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle = os.fdopen(
        os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600),
        "wb", buffering=0,
    )
    try:
        proc = subprocess.Popen(
            [opencode, "serve", "--port", "0", "--print-logs", "--log-level", "INFO"],
            cwd=str(_neutral_cwd()),
            env={**os.environ, "ORCH_BIN": orch_bin()},
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    finally:
        handle.close()

    url = _await_url(log, proc, START_TIMEOUT_S)
    port = _port_of(url)
    info = ServerInfo(
        pid=proc.pid,
        port=port,
        url=url,
        started_at=int(time.time()),
        start_ticks=proc_start_ticks(proc.pid),
    )
    write_state(info)
    return info


def _await_url(log: Path, proc: subprocess.Popen, timeout: float) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        code = proc.poll()
        text = ""
        try:
            text = log.read_text(errors="replace")
        except OSError:
            pass
        m = LISTEN_RE.search(text)
        if m:
            return m.group(1).rstrip("/")
        if code is not None:
            raise RuntimeError(
                f"opencode serve exited with code {code} before announcing a port.\n"
                f"--- {log} ---\n{text.strip()[-2000:]}"
            )
        time.sleep(0.2)
    proc.kill()
    raise TimeoutError(
        f"opencode serve did not announce a port within {timeout:.0f}s; see {log}"
    )


def _port_of(url: str) -> int:
    from urllib.parse import urlsplit

    try:
        port = urlsplit(url.strip()).port
    except ValueError:
        port = None
    if port:
        return int(port)
    m = PORT_RE.search(url.strip().rstrip("/"))
    if not m:
        raise RuntimeError(f"cannot read a port out of {url!r}")
    return int(m.group(1))


def stop(timeout: float = STOP_GRACE_S) -> bool:
    info = read_state()
    if info is None:
        return False
    if not info.alive:
        clear_state()
        return False

    try:
        os.kill(info.pid, signal.SIGTERM)
    except OSError:
        clear_state()
        return False

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not info.alive:
            clear_state()
            return True
        time.sleep(0.2)

    try:
        os.kill(info.pid, signal.SIGKILL)
    except OSError:
        pass
    for _ in range(25):
        if not info.alive:
            break
        time.sleep(0.2)
    clear_state()
    return True


def restart(opencode: str = "opencode") -> ServerInfo:
    stop()
    return start(reuse=False, opencode=opencode)
