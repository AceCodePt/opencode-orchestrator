from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from . import harness

# OpenCode 2 manages one background service per machine (`opencode serve
# --service`), registering its URL and password in
# `~/.local/state/opencode/service.json`. orch no longer spawns its own
# server: it discovers that service, authenticates to it, and only starts one
# when none is running. The service is shared with the user's own TUI, so it is
# deliberately not torn down with orch.

PORT_RE = re.compile(r":(\d+)/?$")

START_TIMEOUT_S = 30.0
STOP_GRACE_S = 5.0


def state_dir() -> Path:
    """orch's own state (trusted projects, hooks, daemon log) - not opencode's."""
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    d = Path(base) / "orch"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        d.chmod(0o700)
    except OSError:
        pass
    return d


def _log_file() -> Path:
    return state_dir() / "server.log"


def _neutral_cwd() -> Path:
    d = state_dir() / "server-cwd"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def service_file() -> Path:
    """opencode's service registration. The service is one per machine on a
    persisted port, so this resolves from the real home rather than orch's
    XDG-isolated state; `ORCH_SERVICE_FILE` overrides it for tests and custom
    setups."""
    override = os.environ.get("ORCH_SERVICE_FILE")
    if override:
        return Path(override)
    return Path.home() / ".local" / "state" / "opencode" / "service.json"


def read_service_file() -> dict | None:
    try:
        raw = json.loads(service_file().read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


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


def proc_start_epoch(pid: int) -> int:
    """The wall-clock time `pid` started, from /proc/uptime and its starttime."""
    ticks = proc_start_ticks(pid)
    if ticks is None:
        return 0
    try:
        up = float(Path("/proc/uptime").read_text().split()[0])
        hz = os.sysconf("SC_CLK_TCK")
        return int(time.time() - (up - ticks / hz))
    except (OSError, ValueError, IndexError, ZeroDivisionError):
        return 0


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
    password: str = ""
    version: str | None = None
    started_at: int = 0
    start_ticks: int | None = None

    @property
    def alive(self) -> bool:
        return pid_alive(self.pid, self.start_ticks)

    def healthy(self) -> bool:
        harness.set_auth(self.port, self.password)
        return bool((harness.health(self.port) or {}).get("healthy"))


def _port_of(url: str) -> int:
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


def _info_from_service(raw: dict) -> ServerInfo | None:
    try:
        pid = int(raw["pid"])
        url = str(raw["url"])
    except (KeyError, TypeError, ValueError):
        return None
    return ServerInfo(
        pid=pid,
        port=_port_of(url),
        url=url,
        password=str(raw.get("password") or ""),
        version=str(raw.get("version") or "") or None,
        started_at=proc_start_epoch(pid),
        start_ticks=proc_start_ticks(pid),
    )


def current() -> ServerInfo | None:
    """The live opencode service, or None when none is registered/alive."""
    raw = read_service_file()
    if raw is None:
        return None
    info = _info_from_service(raw)
    if info is None or not info.alive:
        return None
    harness.set_auth(info.port, info.password)
    return info


def discover() -> ServerInfo | None:
    return current()


def status() -> dict:
    info = current()
    if info is None:
        return {"running": False, "reason": "no opencode service registered"}
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


def _spawn_service(opencode: str) -> subprocess.Popen:
    log = _log_file()
    log.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle = os.fdopen(
        os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600),
        "wb", buffering=0,
    )
    try:
        return subprocess.Popen(
            [opencode, "serve", "--service"],
            cwd=str(_neutral_cwd()),
            env={**os.environ, "ORCH_BIN": orch_bin()},
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    finally:
        handle.close()


def _await_service(previous_pid: int | None, proc: subprocess.Popen,
                   timeout: float) -> ServerInfo:
    """Wait for the spawned service to register itself and answer /api/info."""
    deadline = time.time() + timeout
    last_error = ""
    while time.time() < deadline:
        info = current()
        if info is not None and info.pid != previous_pid and info.healthy():
            return info
        if info is not None and info.healthy():
            return info
        code = proc.poll()
        if code is not None and info is None:
            text = ""
            try:
                text = _log_file().read_text(errors="replace")
            except OSError:
                pass
            last_error = f"opencode serve --service exited with code {code}: {text.strip()[-2000:]}"
            break
        time.sleep(0.2)
    raise TimeoutError(
        last_error
        or f"opencode service did not become healthy within {timeout:.0f}s; "
           f"see {_log_file()}"
    )


def start(reuse: bool = True, opencode: str = "opencode") -> ServerInfo:
    """Return the live shared service, starting one if none is running. opencode
    normally starts this itself when a client connects; orch only steps in when
    the daemon or a CLI call needs it and none exists.

    opencode only loads a newly added local plugin at service startup (not on a
    hot reload), so a changed plugin forces a restart - that is what makes
    `orch install`'s custom tools available to sessions."""
    from . import custom_tools

    tools_changed = custom_tools.ensure()

    existing = current()
    if reuse and existing is not None and existing.healthy() and not tools_changed:
        return existing

    previous_pid = existing.pid if existing else None
    if existing is not None:
        stop()
    proc = _spawn_service(opencode)
    return _await_service(previous_pid, proc, START_TIMEOUT_S)


def stop(timeout: float = STOP_GRACE_S) -> bool:
    """Stop the shared service by its recorded pid. This is an explicit act
    (`orch server stop`, uninstall is deliberate); the daemon never calls it,
    because the service is shared with the user's own opencode clients."""
    info = current()
    if info is None:
        return False

    try:
        os.kill(info.pid, signal.SIGTERM)
    except OSError:
        return False

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not info.alive:
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
    return True


def restart(opencode: str = "opencode") -> ServerInfo:
    stop()
    return start(reuse=False, opencode=opencode)
