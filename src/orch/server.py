"""Lifecycle of the single shared `opencode serve`.

One headless server per machine hosts every worktree. That is safe for project
hooks because plugin loading is per-directory and lazy: a server started in one
directory, handed a session in another, imports the second directory's plugins and
passes them a `worktree` pointing there. Verified directly - see
docs/architecture.md.

Why headless rather than a TUI per task:

- A server always has a port, so "no unobservable agent" stops being a contract
  that a wrapper script has to enforce and becomes a property of the thing itself.
- 362 MB against 947 MB for a TUI, so memory stops being the concurrency ceiling.
- `serve` announces its port on stdout, so nothing has to scan for it.

The TUI is not gone, just demoted to a viewer: `opencode attach <url>` whenever a
human wants to watch, closed again without touching the agent.

Everything here identifies the process by **recorded pid plus start time**, never
by command-line pattern. `pkill -f 'opencode serve'` matches the very shell that
invokes it - it killed one of these commands mid-flight during development, and
would have taken an unrelated live agent with it had the pattern matched.
"""

from __future__ import annotations

import json
import os
import re
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
    return Path(base) / "orch"


def _state_file() -> Path:
    return state_dir() / "server.json"


def _log_file() -> Path:
    return state_dir() / "server.log"


def _neutral_cwd() -> Path:
    """A directory that is deliberately not a project.

    The server's own working directory becomes its default project. Starting it
    inside a real repo would make that repo's config and plugins the ambient
    default for anything dispatched without an explicit `--dir`, which is a
    surprising coupling for a process meant to be shared by every project.
    """
    d = state_dir() / "server-cwd"
    d.mkdir(parents=True, exist_ok=True)
    return d


# --------------------------------------------------------------- process identity


def proc_start_ticks(pid: int) -> int | None:
    """Field 22 of /proc/<pid>/stat: process start time in clock ticks.

    Recorded alongside the pid so a recycled pid cannot be mistaken for our
    server. Without this, `kill` on a stale pid file eventually kills whatever
    unrelated process inherited that number - which is precisely the class of
    accident that pattern-killing already caused here once.
    """
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    # The comm field can contain spaces and parentheses, so split after the last ')'.
    close = raw.rfind(")")
    if close == -1:
        return None
    fields = raw[close + 2:].split()
    # After comm and state, field 22 overall is index 19 in this remainder.
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
        return True  # exists, owned by someone else
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
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(info), indent=2))
    tmp.replace(p)  # atomic, so a crash mid-write cannot leave a truncated pid file


def clear_state() -> None:
    try:
        _state_file().unlink()
    except OSError:
        pass


# ------------------------------------------------------------------------ current


def current() -> ServerInfo | None:
    """The shared server, if one is genuinely running.

    Three degrees of wrongness are distinguished on purpose: no record at all, a
    record whose process is gone, and a live process that no longer answers. Only
    the last is worth alarming about - the others are just cleanup.
    """
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
        # A live process that does not answer is the interesting failure: the
        # supervisor must not read it as "up" just because the pid exists.
        "unresponsive": not caps.healthy,
    }


# -------------------------------------------------------------------------- start


def start(reuse: bool = True, opencode: str = "opencode") -> ServerInfo:
    """Start the shared server, or return the existing one.

    The port is read from the line `serve` prints on stdout rather than guessed or
    scanned for. `--port 0` is passed explicitly: for the TUI, omitting it starts no
    server at all, and while `serve` obviously always listens, being explicit keeps
    one launch idiom for both.
    """
    if reuse:
        existing = current()
        if existing and existing.healthy():
            return existing
        if existing:
            # Alive but not answering. Replace it rather than dispatch into a
            # server that cannot be observed; a half-dead shared server would
            # silently break every task on the machine.
            stop()

    log = _log_file()
    log.parent.mkdir(parents=True, exist_ok=True)
    handle = open(log, "wb", buffering=0)
    try:
        proc = subprocess.Popen(
            [opencode, "serve", "--port", "0", "--print-logs", "--log-level", "INFO"],
            cwd=str(_neutral_cwd()),
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            # New session, so the server outlives the shell that started it and is
            # not killed by a Ctrl-C aimed at the CLI.
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
    """Port from an announced URL.

    Parsed properly rather than by trailing-digits regex, which broke on a trailing
    slash. The announcement format is not something this code controls, so it should
    tolerate a path, a trailing slash, or a host that is not an IP literal.
    """
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


# --------------------------------------------------------------------------- stop


def stop(timeout: float = STOP_GRACE_S) -> bool:
    """Stop the shared server by recorded pid. Returns True if anything was killed.

    SIGTERM, then SIGKILL if it outstays the grace period. No pattern matching, and
    no `tmux kill-session`: killing the process ends its tmux session by itself,
    verified directly, so tmux is a host for a process and never a handle on its
    lifetime.
    """
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
    # Confirm rather than assume: reporting a kill that did not happen is how a
    # supervisor ends up with two servers.
    for _ in range(25):
        if not info.alive:
            break
        time.sleep(0.2)
    clear_state()
    return True


def restart(opencode: str = "opencode") -> ServerInfo:
    stop()
    return start(reuse=False, opencode=opencode)
