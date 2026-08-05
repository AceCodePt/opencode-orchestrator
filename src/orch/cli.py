from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from pathlib import Path

from . import daemon as daemon_mod
from . import dispatch as dispatch_mod
from . import gate as gate_mod
from . import harness, init as init_mod, observe, server as server_mod
from . import tasks as tasks_mod
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
    return 1 if any(f.needs_human or f.is_alarm for f in facts) else 0


def cmd_events(args: argparse.Namespace) -> int:
    tuis = [t for t in discover() if t.observable]
    if args.port:
        tuis = [t for t in tuis if t.port == args.port]
    if not tuis:
        print("no observable TUI found (need one started with --port)", file=sys.stderr)
        return 2
    if len(tuis) > 1:
        print(f"{len(tuis)} observable TUIs; pass --port to choose one:", file=sys.stderr)
        for t in sorted(tuis, key=lambda t: t.port or 0):
            where = f", cwd={t.cwd}" if t.cwd else ""
            print(f"  --port {t.port}  (pid={t.pid}{where})", file=sys.stderr)
        return 2
    tui = tuis[0]
    print(f"# tailing /event on port {tui.port} ({tui.cwd})", file=sys.stderr)
    seen: dict[str, int] = {}
    deadline = time.time() + args.duration if args.duration else None

    def _dump(*_a) -> None:
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
    except Exception as e:  # noqa: BLE001
        print(f"# stream ended: {e}", file=sys.stderr)
    if seen:
        print("\n# event types observed:", file=sys.stderr)
        for k, v in sorted(seen.items(), key=lambda kv: -kv[1]):
            print(f"#   {v:5d}  {k}", file=sys.stderr)
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    start = Path(args.directory) if args.directory else Path.cwd()
    r = init_mod.run(start, force=args.force, version=__version__,
                     force_hooks=args.force_hooks)
    print(init_mod.render(r))
    return 0 if (r.created or r.skipped) else 2


def cmd_doctor(args: argparse.Namespace) -> int:
    checks, worst = doctor_mod.run(Path(args.directory) if args.directory else None)
    print(doctor_mod.render(checks))
    return worst


def cmd_server(args: argparse.Namespace) -> int:
    if args.action is None:
        args._sparser.print_help()
        return 0
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
                print("  ! process is alive but does not answer /global/health")
    if not st.get("running"):
        return 2
    return 1 if st.get("unresponsive") else 0


def cmd_attach(args: argparse.Namespace) -> int:
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
    _os.execvp(cmd[0], cmd)
    return 0


def cmd_task_help(args: argparse.Namespace) -> int:
    args._tparser.print_help()
    return 0


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
    sys.stdout.flush()
    for p in r.problems:
        print(f"  ! {p}", file=sys.stderr)
    sys.stderr.flush()
    return 0 if r.ok else 1


def cmd_task_list(args: argparse.Namespace) -> int:
    start = Path(args.directory) if args.directory else Path.cwd()
    top, project, err = dispatch_mod.project_of(start)
    if err or project is None or top is None:
        print(err or "not in a worktree", file=sys.stderr)
        return 2
    st = Store()
    try:
        base = gate_mod.base_branch(top)
        st.sync_intent(str(project), dispatch_mod.declared_names(top, base))
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
                flag = "  (lease expired - verify before reclaiming)"
            v = st.last_verdict(str(project), t.name)
            said = f"  {v['source']}:{v['outcome']}" if v else ""
            print(f"  {t.status:12} {t.name:34} pid={t.pid or '-':<8} "
                  f"retries={t.retries}{said}{flag}")
        return 0
    finally:
        st.close()


def cmd_task_reset(args: argparse.Namespace) -> int:
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


def cmd_task_add(args: argparse.Namespace) -> int:
    start = Path(args.directory) if args.directory else Path.cwd()
    r = tasks_mod.add_many(start, args.name, title=args.title, desc=args.desc)
    print(tasks_mod.render(r))
    return 0 if r.ok else 2


def cmd_task_merge(args: argparse.Namespace) -> int:
    from . import merge as merge_mod
    start = Path(args.directory) if args.directory else Path.cwd()
    _, project, err = dispatch_mod.project_of(start)
    if err or project is None:
        print(err or "not in a worktree", file=sys.stderr)
        return 2
    st = Store()
    try:
        r = merge_mod.merge_and_cleanup(st, str(project), args.name)
        for s in r.steps:
            print(f"  {s}")
        sys.stdout.flush()
        for p in r.problems:
            print(f"  ! {p}", file=sys.stderr)
        return 0 if r.ok else 1
    finally:
        st.close()


def cmd_gate(args: argparse.Namespace) -> int:
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


def cmd_gate_run(args: argparse.Namespace) -> int:
    wt = Path(args.directory) if args.directory else Path.cwd()
    v = gate_mod.run(wt, args.event)
    print(f"{v.event}: {v.summary}")
    if v.diverged:
        print("  ! the worktree's gate differs from the committed one; the committed "
              "one ran")
    if v.detail:
        for line in v.detail.strip().splitlines()[-20:]:
            print(f"  {line}")

    st = Store()
    try:
        t = st.by_worktree(str(wt))
        if t is not None:
            st.record_verdict(t.project, t.name, v.event, v.outcome, v.detail,
                              diverged=v.diverged, seconds=v.seconds, source="cli")
            print(f"  recorded against task {t.name}")
    finally:
        st.close()
    return 0 if v.justifies_done else 1


def cmd_gate_state(args: argparse.Namespace) -> int:
    st = Store()
    try:
        t = st.by_worktree(args.worktree)
        if t is None:
            print(json.dumps({"known": False, "fingerprint": None, "attempts": 0}))
            return 0
        print(json.dumps({
            "known": True, "task": t.name, "project": t.project,
            "fingerprint": t.fingerprint, "attempts": t.retries,
        }))
        return 0
    finally:
        st.close()


def cmd_gate_report(args: argparse.Namespace) -> int:
    st = Store()
    try:
        t = st.by_worktree(args.worktree)
        if t is None:
            print(json.dumps({"known": False, "attempts": 0}))
            return 0
        attempts = t.retries
        if args.outcome == "running":
            st.record_verdict(t.project, t.name, args.event, "running",
                              args.detail or "", source="router")
            print(json.dumps({"known": True, "task": t.name, "attempts": attempts}))
            return 0
        if args.outcome == "passed":
            if args.fingerprint:
                st.set_fingerprint(t.project, t.name, args.fingerprint)
            st.reset_retry(t.project, t.name)
            attempts = 0
        elif args.outcome == "failed":
            attempts = st.bump_retry(t.project, t.name)
        elif args.outcome == "stuck" and args.fingerprint:
            st.set_fingerprint(t.project, t.name, args.fingerprint)
        st.record_verdict(t.project, t.name, args.event, args.outcome,
                          args.detail or "", diverged=args.diverged,
                          seconds=args.seconds, source="router")
        print(json.dumps({"known": True, "task": t.name, "attempts": attempts}))
        return 0
    finally:
        st.close()


def cmd_install(args: argparse.Namespace) -> int:
    for line in daemon_mod.install(interval=args.interval,
                                   enable=not args.no_enable):
        print(line)
    print("\nlogs: journalctl --user -u orch-daemon -f")
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    for line in daemon_mod.uninstall(wipe_state=not args.keep_state):
        print(line)
    return 0


def cmd_daemon(args: argparse.Namespace) -> int:
    if args.action is None:
        args._dparser.print_help()
        return 0
    if args.action == "status":
        info = daemon_mod.unit_status()
        if args.json:
            print(json.dumps(info, indent=2))
            return 0
        if not info.get("installed"):
            print("not installed under systemd - nothing supervises tasks across a "
                  "reboot. `orch install` fixes that.")
            return 1
        print(f"unit    {info['unit']}")
        print(f"state   {info.get('ActiveState')}/{info.get('SubState')} "
              f"({info.get('UnitFileState')})  pid={info.get('ExecMainPID')}")
        if info.get("linger") is False:
            print("linger  NO - this daemon will not run unless you are logged in")
            return 1
        print("linger  yes - survives logout and reboot")
        return 0 if info.get("ActiveState") == "active" else 1

    project: str | None = None
    if args.directory or args.project_only:
        start = Path(args.directory) if args.directory else Path.cwd()
        _, p, err = dispatch_mod.project_of(start)
        if err or p is None:
            print(err or "not in a worktree", file=sys.stderr)
            return 2
        project = str(p)
    return daemon_mod.run(project, interval=args.interval,
                          once=args.once, dry_run=args.dry_run)


def cmd_version(_: argparse.Namespace) -> int:
    print(f"orch {__version__}")
    for t in discover():
        if t.observable and t.port:
            caps = harness.capabilities(t.port)
            v = ".".join(map(str, caps.version)) if caps.version else "?"
            print(f"  harness on port {t.port}: {v}")
    return 0


class _SubcommandHelpFormatter(argparse.HelpFormatter):
    def _format_action(self, action: argparse.Action) -> str:
        if action.nargs == argparse.PARSER:
            parts: list[str] = []
            for subaction in self._iter_indented_subactions(action):
                r = self._format_action(subaction) or ""
                parts.append("".join(
                    l[2:] if l.startswith("  ") else l
                    for l in r.splitlines(keepends=True)
                ))
            return "".join(parts)
        return super()._format_action(action)


class _OrchArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("formatter_class", _SubcommandHelpFormatter)
        super().__init__(*args, **kwargs)


def build_parser() -> argparse.ArgumentParser:
    p = _OrchArgumentParser(
        prog="orch",
        description="Orchestrate opencode agents across bare-repo worktrees.",
    )
    p.add_argument("--version", action="version", version=f"orch {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True,
                           metavar="<command>", title="commands")

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
    i.add_argument("--force", action="store_true",
                   help="overwrite existing files, except gates")
    i.add_argument("--force-hooks", action="store_true",
                   help="also overwrite hooks, replacing a real gate with "
                        "the fail-closed scaffold")
    i.set_defaults(func=cmd_init)

    d = sub.add_parser("doctor", help="verify the contracts that fail silently")
    d.add_argument("-C", "--directory")
    d.set_defaults(func=cmd_doctor)

    s = sub.add_parser("server", help="the one shared headless opencode server")
    s.add_argument("--json", action="store_true")
    sa = s.add_subparsers(dest="action", metavar="<action>", title="server actions")
    ss = sa.add_parser("status",
                       help="whether the shared server is up and healthy")
    ss.add_argument("--json", action="store_true")
    sa.add_parser("start", help="start the shared server (reuse if already up)")
    sa.add_parser("stop", help="stop the shared server by recorded pid")
    sa.add_parser("restart", help="stop and start the shared server again")
    s.set_defaults(func=cmd_server, _sparser=s)

    a = sub.add_parser("attach", help="open a TUI onto the shared server (ephemeral)")
    a.add_argument("-C", "--directory", help="worktree to view")
    a.add_argument("--print-only", action="store_true",
                   help="print the command instead of running it")
    a.set_defaults(func=cmd_attach)

    t = sub.add_parser("task", help="dispatch and inspect tasks")
    t.set_defaults(func=cmd_task_help, _tparser=t)
    tsub = t.add_subparsers(dest="task_cmd",
                            metavar="<command>", title="task commands")

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

    ta = tsub.add_parser("add",
                         help="scaffold task.md specs and list them in TASKS.md")
    ta.add_argument("name", nargs="+")
    ta.add_argument("-C", "--directory",
                    help="write into this worktree instead of the current one")
    ta.add_argument("--title", help="entry title (default: derived from the name)")
    ta.add_argument("--desc", help="one-line description for the TASKS.md entry")
    ta.set_defaults(func=cmd_task_add)

    tm = tsub.add_parser("merge",
                         help="merge a done task into base and clean up")
    tm.add_argument("name")
    tm.add_argument("-C", "--directory")
    tm.set_defaults(func=cmd_task_merge)

    g = sub.add_parser("gate", help="can this project's gates be trusted")
    g.add_argument("-C", "--directory")
    g.set_defaults(func=cmd_gate)

    gr = sub.add_parser("gate-run", help="run the committed gate here and report")
    gr.add_argument("-C", "--directory")
    gr.add_argument("--event", default="session-idle")
    gr.set_defaults(func=cmd_gate_run)

    gs = sub.add_parser("gate-state", help="(internal) gate fingerprint and attempts")
    gs.add_argument("--worktree", required=True)
    gs.add_argument("--event", default="session-idle")
    gs.set_defaults(func=cmd_gate_state)

    gp = sub.add_parser("gate-report", help="(internal) record a router gate result")
    gp.add_argument("--worktree", required=True)
    gp.add_argument("--event", default="session-idle")
    gp.add_argument("--outcome", required=True)
    gp.add_argument("--fingerprint")
    gp.add_argument("--detail")
    gp.add_argument("--seconds", type=float, default=0.0)
    gp.add_argument("--diverged", action="store_true")
    gp.set_defaults(func=cmd_gate_report)

    dm = sub.add_parser("daemon", help="supervise leases, verdicts and the server")
    da = dm.add_subparsers(dest="action", metavar="<action>", title="daemon actions")
    dr = da.add_parser("run",
                       help="run the supervision loop (foreground; Ctrl-C stops)")
    ds = da.add_parser("status",
                       help="report what systemd says is supervising")
    dm.add_argument("-C", "--directory",
                    help="supervise only this project (default: every project)")
    dm.add_argument("--project-only", action="store_true",
                    help="supervise only the project of the current directory")
    dm.add_argument("-n", "--interval", type=float, default=10.0)
    dm.add_argument("--once", action="store_true", help="one pass, then exit")
    dm.add_argument("--dry-run", action="store_true",
                    help="decide and print, change nothing")
    dm.add_argument("--json", action="store_true")
    # The run flags exist on both the parent and the `run` subparser so
    # `orch daemon <flag> run` and `orch daemon run <flag>` both work, matching
    # how the actions were called before they became subcommands.
    dr.add_argument("-C", "--directory",
                    help="supervise only this project (default: every project)")
    dr.add_argument("--project-only", action="store_true",
                    help="supervise only the project of the current directory")
    dr.add_argument("-n", "--interval", type=float, default=10.0)
    dr.add_argument("--once", action="store_true", help="one pass, then exit")
    dr.add_argument("--dry-run", action="store_true",
                    help="decide and print, change nothing")
    ds.add_argument("--json", action="store_true")
    dm.set_defaults(func=cmd_daemon, _dparser=dm)

    inst = sub.add_parser(
        "install",
        help="write and enable the systemd user unit supervising every project")
    inst.add_argument("-n", "--interval", type=float, default=10.0)
    inst.add_argument("--no-enable", action="store_true",
                      help="write the systemd unit but do not enable it")
    inst.set_defaults(func=cmd_install)

    un = sub.add_parser(
        "uninstall",
        help="remove every config-level trace of orch (never a project's "
             ".orchestration/ or router plugin)")
    un.add_argument("--keep-state", action="store_true",
                    help="stop the daemon and server but keep the database and "
                         "task history in ~/.local/state/orch")
    un.set_defaults(func=cmd_uninstall)

    v = sub.add_parser("version", help="show orch and harness versions")
    v.set_defaults(func=cmd_version)
    return p


def main(argv: list[str] | None = None) -> int:
    if argv is None and not sys.argv[1:]:
        build_parser().print_help()
        return 0
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
