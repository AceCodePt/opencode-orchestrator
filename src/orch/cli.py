"""`orch` command line.

Deliberately stdlib-only. This has to run on a box that is a pinned-binary
environment, so a dependency tree is a liability.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from pathlib import Path

from . import dispatch as dispatch_mod
from . import gate as gate_mod
from . import harness, init as init_mod, observe, server as server_mod
from .discovery import discover
from . import doctor as doctor_mod
from .store import Store

__version__ = "0.1.0"


def cmd_observe(args: argparse.Namespace) -> int:
    if args.watch:
        try:
            while True:
                facts = observe.look_all()
                sys.stdout.write("\x1b[2J\x1b[H")
                print(time.strftime("%H:%M:%S"))
                print(observe.render(facts, verbose=args.verbose))
                sys.stdout.flush()
                time.sleep(args.interval)
        except KeyboardInterrupt:
            return 0
    facts = observe.look_all()
    if args.json:
        print(json.dumps(
            [
                {
                    "pid": f.tui.pid,
                    "port": f.tui.port,
                    "worktree": f.worktree,
                    "state": f.state,
                    "version": f.version,
                    "tmux": f.tui.tmux,
                    "busy_sessions": f.busy_sessions,
                    "blocked": [vars(b) for b in f.blocked],
                    "notes": f.notes,
                }
                for f in facts
            ],
            indent=2,
        ))
    else:
        print(observe.render(facts, verbose=args.verbose))
    # Exit non-zero when something needs a human, so this composes in a shell.
    return 1 if any(f.needs_human or f.is_alarm for f in facts) else 0


def cmd_events(args: argparse.Namespace) -> int:
    """Tail the SSE stream. Polling cannot replace this: a request raised and
    answered between two samples is invisible forever."""
    tuis = [t for t in discover() if t.observable]
    if args.port:
        tuis = [t for t in tuis if t.port == args.port]
    if not tuis:
        print("no observable TUI found (need one started with --port)", file=sys.stderr)
        return 2
    tui = tuis[0]
    print(f"# tailing /event on port {tui.port} ({tui.cwd})", file=sys.stderr)
    seen: dict[str, int] = {}
    deadline = time.time() + args.duration if args.duration else None

    def _dump(*_a) -> None:
        # Print the histogram even when killed, otherwise `timeout orch events`
        # discards everything it collected.
        if seen:
            print("\n# event types observed:", file=sys.stderr)
            for k, v in sorted(seen.items(), key=lambda kv: -kv[1]):
                print(f"#   {v:5d}  {k}", file=sys.stderr)
        sys.stderr.flush()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _dump)
    signal.signal(signal.SIGINT, _dump)

    try:
        for ev in harness.events(tui.port):
            if deadline and time.time() > deadline:
                break
            etype = str(ev.get("type", "?"))
            seen[etype] = seen.get(etype, 0) + 1
            if args.types_only:
                continue
            props = ev.get("properties", {})
            if args.filter and args.filter not in etype:
                continue
            print(f"{time.strftime('%H:%M:%S')} {etype}")
            if args.verbose and isinstance(props, dict):
                for k, v in props.items():
                    s = json.dumps(v) if not isinstance(v, str) else v
                    print(f"    {k}={s[:160]}")
    except KeyboardInterrupt:
        pass
    except Exception as e:  # noqa: BLE001 - a dying server must not traceback
        print(f"# stream ended: {e}", file=sys.stderr)
    if seen:
        print("\n# event types observed:", file=sys.stderr)
        for k, v in sorted(seen.items(), key=lambda kv: -kv[1]):
            print(f"#   {v:5d}  {k}", file=sys.stderr)
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    start = Path(args.directory) if args.directory else Path.cwd()
    r = init_mod.run(start, force=args.force, version=__version__)
    print(init_mod.render(r))
    # No worktree resolved means nothing was written; that is a failure, not a no-op.
    return 0 if (r.created or r.skipped) else 2


def cmd_doctor(args: argparse.Namespace) -> int:
    checks, worst = doctor_mod.run(Path(args.directory) if args.directory else None)
    print(doctor_mod.render(checks))
    return worst


def cmd_server(args: argparse.Namespace) -> int:
    """Lifecycle of the one shared headless server."""
    if args.action == "start":
        try:
            info = server_mod.start()
        except (RuntimeError, TimeoutError, OSError) as e:
            print(f"could not start: {e}", file=sys.stderr)
            return 1
        print(f"shared server pid={info.pid} {info.url}")
        return 0

    if args.action == "stop":
        killed = server_mod.stop()
        print("stopped" if killed else "nothing running")
        return 0

    if args.action == "restart":
        try:
            info = server_mod.restart()
        except (RuntimeError, TimeoutError, OSError) as e:
            print(f"could not restart: {e}", file=sys.stderr)
            return 1
        print(f"shared server pid={info.pid} {info.url}")
        return 0

    st = server_mod.status()
    if args.json:
        print(json.dumps(st, indent=2))
    else:
        if not st.get("running"):
            print(f"not running ({st.get('reason')})")
        else:
            print(f"pid={st['pid']} {st['url']} version={st.get('version') or '?'} "
                  f"uptime={st.get('uptime_s')}s")
            if st.get("unresponsive"):
                # The dangerous middle state: the pid exists so naive checks call it
                # up, but it answers nothing, and every task on the machine depends
                # on this one process.
                print("  ! process is alive but does not answer /global/health")
    if not st.get("running"):
        return 2
    return 1 if st.get("unresponsive") else 0


def cmd_attach(args: argparse.Namespace) -> int:
    """Open a TUI onto the shared server.

    The whole reason headless is livable: the viewer is ephemeral and closing it
    does nothing to the agent.
    """
    info = server_mod.current()
    if info is None:
        print("no shared server running; `orch server start` first", file=sys.stderr)
        return 2
    cmd = ["opencode", "attach", info.url]
    if args.directory:
        cmd += ["--dir", args.directory]
    if args.print_only:
        print(" ".join(cmd))
        return 0
    import os as _os
    _os.execvp(cmd[0], cmd)  # replace this process; the TUI owns the terminal now
    return 0  # unreachable


def cmd_task_start(args: argparse.Namespace) -> int:
    r = dispatch_mod.start(
        Path(args.directory) if args.directory else Path.cwd(),
        args.name,
        prompt=args.prompt,
        model=args.model,
        agent=args.agent,
        branch=args.branch,
        worktree=Path(args.worktree) if args.worktree else None,
        dry_run=args.dry_run,
        allow_ungated=args.allow_ungated,
    )
    for s in r.steps:
        print(f"  {s}")
    # Flush before writing to stderr: otherwise a redirected run shows the failure
    # above the steps that led to it, which reads as if it failed before starting.
    sys.stdout.flush()
    for p in r.problems:
        print(f"  ! {p}", file=sys.stderr)
    sys.stderr.flush()
    return 0 if r.ok else 1


def cmd_task_list(args: argparse.Namespace) -> int:
    start = Path(args.directory) if args.directory else Path.cwd()
    _, project, err = dispatch_mod.project_of(start)
    if err or project is None:
        print(err or "not in a worktree", file=sys.stderr)
        return 2
    st = Store()
    try:
        rows = st.tasks(str(project))
        if args.json:
            print(json.dumps([vars(t) for t in rows], indent=2))
            return 0
        if not rows:
            print("no tasks recorded for this project")
            return 0
        for t in rows:
            flag = ""
            if t.lease_expired:
                # Expiry is not permission to reclaim: a busy agent whose renewal
                # broke looks identical here, which is why this only annotates.
                flag = "  (lease expired - verify before reclaiming)"
            print(f"  {t.status:8} {t.name:34} pid={t.pid or '-':<8} "
                  f"retries={t.retries}{flag}")
        return 0
    finally:
        st.close()


def cmd_task_reset(args: argparse.Namespace) -> int:
    """Return a task to available. Deliberately a human action.

    Nothing auto-resets a `failed` or `stuck` task: those states mean a worktree
    exists and may hold half-finished work, and silently handing it to the next
    dispatcher would hide that. `orch observe` shows what is there first.
    """
    start = Path(args.directory) if args.directory else Path.cwd()
    _, project, err = dispatch_mod.project_of(start)
    if err or project is None:
        print(err or "not in a worktree", file=sys.stderr)
        return 2
    st = Store()
    try:
        t = st.get(str(project), args.name)
        if t is None:
            print(f"no such task: {args.name}", file=sys.stderr)
            return 2
        if t.status == "running" and not args.force:
            print(
                f"{args.name} is still running (pid={t.pid or '?'}). Resetting a live "
                f"task orphans the agent; stop it first, or pass --force if you have "
                f"confirmed it is dead.",
                file=sys.stderr,
            )
            return 1
        st.reclaim(str(project), args.name, f"reset by hand (was {t.status})")
        print(f"{args.name}: {t.status} -> todo")
        return 0
    finally:
        st.close()


def cmd_gate(args: argparse.Namespace) -> int:
    """Report whether this project's gates can be trusted."""
    start = Path(args.directory) if args.directory else Path.cwd()
    top, _, err = dispatch_mod.project_of(start)
    if err or top is None:
        print(err or "not in a worktree", file=sys.stderr)
        return 2
    findings = gate_mod.inspect(top)
    if not findings:
        print("no gates configured - nothing verifies work in this project")
        return 2
    worst = 0
    for f in findings:
        mark = {"ok": "  ", "diverged": "~ ", "uncommitted": "! ",
                "missing": "! "}.get(f.state, "? ")
        print(f"{mark}{f.name}: {f.state}")
        print(f"    {f.detail}")
        if f.state == gate_mod.DIVERGED:
            worst = max(worst, 1)
        elif not f.trustworthy:
            worst = max(worst, 2)
    return worst


def cmd_version(_: argparse.Namespace) -> int:
    print(f"orch {__version__}")
    for t in discover():
        if t.observable and t.port:
            caps = harness.capabilities(t.port)
            v = ".".join(map(str, caps.version)) if caps.version else "?"
            print(f"  harness on port {t.port}: {v}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="orch",
        description="Orchestrate opencode agents across bare-repo worktrees.",
    )
    p.add_argument("--version", action="version", version=f"orch {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    o = sub.add_parser("observe", help="show the live state of every TUI (read-only)")
    o.add_argument("-v", "--verbose", action="store_true")
    o.add_argument("--json", action="store_true")
    o.add_argument("-w", "--watch", action="store_true", help="redraw continuously")
    o.add_argument("-n", "--interval", type=float, default=2.0)
    o.set_defaults(func=cmd_observe)

    e = sub.add_parser("events", help="tail the SSE event stream of one TUI")
    e.add_argument("--port", type=int)
    e.add_argument("-v", "--verbose", action="store_true")
    e.add_argument("--filter", help="only print event types containing this string")
    e.add_argument("--types-only", action="store_true",
                   help="print nothing until exit, then a histogram of event types")
    e.add_argument("-d", "--duration", type=float, default=0.0,
                   help="stop after N seconds and print the histogram")
    e.set_defaults(func=cmd_events)

    i = sub.add_parser("init", help="scaffold .orchestration/ and install the router plugin")
    i.add_argument("-C", "--directory")
    i.add_argument("--force", action="store_true", help="overwrite existing files")
    i.set_defaults(func=cmd_init)

    d = sub.add_parser("doctor", help="verify the contracts that fail silently")
    d.add_argument("-C", "--directory")
    d.set_defaults(func=cmd_doctor)

    s = sub.add_parser("server", help="the one shared headless opencode server")
    s.add_argument("action", nargs="?", default="status",
                   choices=["status", "start", "stop", "restart"])
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_server)

    a = sub.add_parser("attach", help="open a TUI onto the shared server (ephemeral)")
    a.add_argument("-C", "--directory", help="worktree to view")
    a.add_argument("--print-only", action="store_true",
                   help="print the command instead of running it")
    a.set_defaults(func=cmd_attach)

    t = sub.add_parser("task", help="dispatch and inspect tasks")
    tsub = t.add_subparsers(dest="task_cmd", required=True)

    ts = tsub.add_parser("start", help="claim a task and dispatch an agent for it")
    ts.add_argument("name")
    ts.add_argument("-C", "--directory")
    ts.add_argument("--prompt", help="override the prompt (defaults to the task file)")
    ts.add_argument("-m", "--model")
    ts.add_argument("--agent")
    ts.add_argument("--branch")
    ts.add_argument("--worktree")
    ts.add_argument("--dry-run", action="store_true",
                    help="show the plan and the exact dispatch command")
    ts.add_argument("--allow-ungated", action="store_true",
                    help="dispatch even though nothing will verify the result")
    ts.set_defaults(func=cmd_task_start)

    tl = tsub.add_parser("list", help="tasks recorded for this project")
    tl.add_argument("-C", "--directory")
    tl.add_argument("--json", action="store_true")
    tl.set_defaults(func=cmd_task_list)

    tr = tsub.add_parser("reset", help="return a task to available (a human action)")
    tr.add_argument("name")
    tr.add_argument("-C", "--directory")
    tr.add_argument("--force", action="store_true",
                    help="reset even a task recorded as running")
    tr.set_defaults(func=cmd_task_reset)

    g = sub.add_parser("gate", help="can this project's gates be trusted")
    g.add_argument("-C", "--directory")
    g.set_defaults(func=cmd_gate)

    v = sub.add_parser("version", help="show orch and harness versions")
    v.set_defaults(func=cmd_version)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
