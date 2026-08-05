from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass

PROC_NAME = "opencode"

_SS_PROC = re.compile(r'\(\("(?P<name>[^"]+)",pid=(?P<pid>\d+)')
_SS_PORT = re.compile(r":(?P<port>\d+)$")


@dataclass
class Tui:
    pid: int
    port: int | None
    cwd: str | None
    cmdline: str
    tmux: str | None = None

    @property
    def observable(self) -> bool:
        return self.port is not None

    def __str__(self) -> str:
        return f"pid={self.pid} port={self.port or '-'} cwd={self.cwd}"


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=10, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _listening_ports() -> dict[int, int]:
    out: dict[int, int] = {}
    for line in _run(["ss", "-ltnp"]).splitlines():
        if PROC_NAME not in line or "Local Address" in line:
            continue
        m = _SS_PROC.search(line)
        if not m or m.group("name") != PROC_NAME:
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        mp = _SS_PORT.search(parts[3])
        if not mp:
            continue
        out[int(m.group("pid"))] = int(mp.group("port"))
    return out


def _pids() -> list[int]:
    out = _run(["pgrep", "-x", PROC_NAME])
    return [int(x) for x in out.split() if x.isdigit()]


def _cwd(pid: int) -> str | None:
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return None


def _cmdline(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return fh.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
    except OSError:
        return ""


def _ppid(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/status", "r") as fh:
            for line in fh:
                if line.startswith("PPid:"):
                    return int(line.split()[1])
    except (OSError, ValueError):
        return None
    return None


def _tmux_sessions() -> dict[int, str]:
    out = _run(["tmux", "list-panes", "-a", "-F", "#{pane_pid}\t#{session_name}"])
    mapping: dict[int, str] = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 2 and parts[0].isdigit():
            mapping[int(parts[0])] = parts[1]
    return mapping


def _tmux_for(pid: int, panes: dict[int, str]) -> str | None:
    seen = 0
    cur: int | None = pid
    while cur and cur > 1 and seen < 12:
        if cur in panes:
            return panes[cur]
        cur = _ppid(cur)
        seen += 1
    return None


def discover() -> list[Tui]:
    ports = _listening_ports()
    panes = _tmux_sessions()
    out: list[Tui] = []
    for pid in sorted(_pids()):
        out.append(
            Tui(
                pid=pid,
                port=ports.get(pid),
                cwd=_cwd(pid),
                cmdline=_cmdline(pid),
                tmux=_tmux_for(pid, panes),
            )
        )
    return out
