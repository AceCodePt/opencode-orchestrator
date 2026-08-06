from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import daemon as daemon_mod
from . import dispatch as dispatch_mod
from . import doctor as doctor_mod
from . import gate as gate_mod
from . import harness
from . import init as init_mod
from . import server as server_mod
from . import state
from . import tasks as tasks_mod

__version__ = "0.1.0"


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
    proj = state.Project.from_dir(start)
    if proj is None:
        print("not in a worktree", file=sys.stderr)
        return 2
    st = server_mod.status()
    port = st.get("port") if st.get("running") and not st.get("unresponsive") else None
    tasks = proj.scan(port)
    if args.json:
        print(json.dumps([_task_json(t) for t in tasks], indent=2))
        return 0
    if not tasks:
        print("no tasks recorded for this project")
        return 0
    for t in tasks:
        branch = t.branch or "-"
        where = (t.worktree or "-").split("/")[-1]
        state_info = ""
        if t.session != state.SESSION_NONE:
            state_info = f" session={t.session}"
        if t.hitl:
            state_info += " hitl"
        if t.dirty:
            state_info += " dirty"
        elif t.diff:
            state_info += " diff"
        print(f"  {t.status:12} {t.name:34} {branch:<34} {where}{state_info}")
        for n in t.notes:
            print(f"    ! {n}")
        for b in t.blocked:
            print(f"    ↳ {b.kind}: {b.detail}  [{b.request_id}]")
    return 0


def _task_json(t: state.TaskState) -> dict:
    return {
        "name": t.name,
        "status": t.status,
        "project": t.project,
        "declared": t.declared,
        "archived": t.archived,
        "claimed": t.claimed,
        "branch": t.branch,
        "worktree": t.worktree,
        "dirty": t.dirty,
        "diff": t.diff,
        "session": t.session,
        "session_ids": t.session_ids,
        "dispatch_alive": t.dispatch_alive,
        "dispatch_orchestrated": t.dispatch_orchestrated,
        "hitl": t.hitl,
        "base": t.base,
    }


def cmd_task_reset(args: argparse.Namespace) -> int:
    start = Path(args.directory) if args.directory else Path.cwd()
    proj = state.Project.from_dir(start)
    if proj is None:
        print("not in a worktree", file=sys.stderr)
        return 2
    tasks = proj.scan()
    t = next((x for x in tasks if x.name == args.name), None)
    if t is None:
        print(f"no such task: {args.name}", file=sys.stderr)
        return 2
    if not t.claimed or not t.worktree or not t.branch:
        print(f"{args.name} is not claimed (status {t.status}); nothing to reset",
              file=sys.stderr)
        return 1
    if not args.force:
        live = t.session in (state.SESSION_BUSY, state.SESSION_BLOCKED) \
            or t.dispatch_alive
        if live:
            print(
                f"{args.name} still has a live session or dispatch process. "
                f"Reset deletes the branch and worktree; pass --force only if you "
                f"have confirmed it is dead.",
                file=sys.stderr,
            )
            return 1
    from . import merge as merge_mod
    r = merge_mod.reset_task(proj.common, args.name, t.branch,
                             Path(t.worktree), proj.base)
    for s in r.steps:
        print(f"  {s}")
    for p in r.problems:
        print(f"  ! {p}", file=sys.stderr)
    print(f"{args.name}: -> todo (branch and worktree deleted)")
    return 0 if r.ok else 1


def cmd_task_archive(args: argparse.Namespace) -> int:
    start = Path(args.directory) if args.directory else Path.cwd()
    proj = state.Project.from_dir(start)
    if proj is None:
        print("not in a worktree", file=sys.stderr)
        return 2
    tasks = proj.scan()
    t = next((x for x in tasks if x.name == args.name), None)
    if t is None:
        print(f"no such task: {args.name}", file=sys.stderr)
        return 2
    if not t.claimed or not t.worktree or not t.branch:
        print(f"{args.name} is not claimed (status {t.status}); nothing to archive",
              file=sys.stderr)
        return 1
    from . import merge as merge_mod
    r = merge_mod.remove_task(proj.common, args.name, t.branch,
                              Path(t.worktree), proj.base)
    for s in r.steps:
        print(f"  {s}")
    for p in r.problems:
        print(f"  ! {p}", file=sys.stderr)
    print(f"{args.name}: closed and archived")
    return 0 if r.ok else 1


def cmd_task_add(args: argparse.Namespace) -> int:
    start = Path(args.directory) if args.directory else Path.cwd()
    if args.assist and len(args.name) != 1:
        print("--assist drafts one spec, so it needs exactly one task name",
              file=sys.stderr)
        return 2
    r = tasks_mod.add_many(start, args.name, title=args.title, auto=args.auto)
    print(tasks_mod.render(r))
    if args.assist and r.created:
        task_name = dispatch_mod.slug(args.name[0])
        rel = tasks_mod.task_rel(task_name)
        a = tasks_mod.assist_spec(start, task_name, args.assist)
        print(f"  {rel}:")
        for n in a.notes:
            print(f"    {'! ' if not a.ok else '  '}{n}")
        if not a.ok:
            return 1
    return 0 if r.ok else 2


def cmd_task_merge(args: argparse.Namespace) -> int:
    from . import merge as merge_mod
    start = Path(args.directory) if args.directory else Path.cwd()
    proj = state.Project.from_dir(start)
    if proj is None:
        print("not in a worktree", file=sys.stderr)
        return 2
    tasks = proj.scan()
    t = next((x for x in tasks if x.name == args.name), None)
    if t is None or not t.claimed or not t.worktree or not t.branch:
        print(f"no claimed task: {args.name}", file=sys.stderr)
        return 2
    r = merge_mod.merge_and_cleanup(proj.common, args.name, t.branch,
                                    Path(t.worktree), proj.base)
    for s in r.steps:
        print(f"  {s}")
    for p in r.problems:
        print(f"  ! {p}", file=sys.stderr)
    return 0 if r.ok else 1


def cmd_task_answer(args: argparse.Namespace) -> int:
    start = Path(args.directory) if args.directory else Path.cwd()
    proj = state.Project.from_dir(start)
    if proj is None:
        print("not in a worktree", file=sys.stderr)
        return 2
    st = server_mod.status()
    port = (st.get("port") if st.get("running")
            and not st.get("unresponsive") else None)
    if not port:
        print("no shared server; `orch server start` first", file=sys.stderr)
        return 2
    tasks = proj.scan(port)
    t = next((x for x in tasks if x.name == args.name), None)
    if t is None:
        print(f"no such task: {args.name}", file=sys.stderr)
        return 2
    if not t.blocked:
        print(f"{args.name} is not blocked (status {t.status})", file=sys.stderr)
        return 1
    b = (next((x for x in t.blocked if x.request_id == args.request), None)
         if args.request else t.blocked[0])
    if b is None:
        print(f"no pending request {args.request}", file=sys.stderr)
        return 2
    try:
        if b.kind == "question":
            harness.reply_question(port, b.request_id, [{"value": args.reply}])
            print(f"answered question {b.request_id} for {args.name}")
        else:
            reply = args.reply.lower()
            if reply not in ("allow", "deny"):
                print("a permission takes `allow` or `deny`", file=sys.stderr)
                return 2
            harness.reply_permission(port, b.session_id, b.request_id, reply,
                                     remember=args.remember)
            print(f"{reply}ed permission {b.request_id} for {args.name}")
    except Exception as e:  # noqa: BLE001
        print(f"could not answer {b.request_id}: {e}", file=sys.stderr)
        return 1
    if len(t.blocked) > 1:
        print(f"{len(t.blocked) - 1} more pending request(s); pass --request <id> "
              "to answer a specific one", file=sys.stderr)
    return 0


def cmd_task_import(args: argparse.Namespace) -> int:
    start = Path(args.directory) if args.directory else Path.cwd()
    if args.file == "-":
        text = sys.stdin.read()
    else:
        try:
            text = Path(args.file).read_text()
        except OSError as e:
            print(f"could not read {args.file}: {e}", file=sys.stderr)
            return 2
    items = tasks_mod.parse_items(text)
    if not items:
        print("no task lines found", file=sys.stderr)
        return 2
    r = tasks_mod.import_specs(start, items, auto=args.auto)
    print(tasks_mod.render(r))
    return 0 if r.ok else 2


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
    return 0 if v.justifies_done else 1


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

    i = sub.add_parser("init", help="scaffold .orchestration/ with the gate template")
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

    tr = tsub.add_parser("reset", help="return a claimed task to todo (delete branch + worktree)")
    tr.add_argument("name")
    tr.add_argument("-C", "--directory")
    tr.add_argument("--force", action="store_true",
                    help="reset even a task with a live session or dispatch")
    tr.set_defaults(func=cmd_task_reset)

    tar = tsub.add_parser("archive",
                          help="close a reviewed needs-review task: delete branch + worktree, archive the spec")
    tar.add_argument("name")
    tar.add_argument("-C", "--directory")
    tar.set_defaults(func=cmd_task_archive)

    ta = tsub.add_parser("add",
                         help="scaffold tasks/<name>/task.md specs (no TASKS.md)")
    ta.add_argument("name", nargs="+")
    ta.add_argument("-C", "--directory",
                    help="write into this worktree instead of the current one")
    ta.add_argument("--title", help="entry title (default: derived from the name)")
    ta.add_argument("--auto", action="store_true",
                    help="let the scheduler pick this task; by default a task "
                         "needs a human to start it")
    ta.add_argument("--assist", metavar="DESCRIPTION",
                    help="draft the spec with an opencode agent from this "
                         "intent; you review and commit it")
    ta.set_defaults(func=cmd_task_add)

    tm = tsub.add_parser("merge",
                         help="merge a done task into base and clean up")
    tm.add_argument("name")
    tm.add_argument("-C", "--directory")
    tm.set_defaults(func=cmd_task_merge)

    ta2 = tsub.add_parser("answer",
                          help="answer a blocked permission or question for a task")
    ta2.add_argument("name")
    ta2.add_argument("reply",
                     help="allow|deny for a permission; the answer text for a question")
    ta2.add_argument("-C", "--directory")
    ta2.add_argument("--request", help="answer a specific pending request id")
    ta2.add_argument("--remember", action="store_true",
                     help="remember a permission decision, not just this once")
    ta2.set_defaults(func=cmd_task_answer)

    ti = tsub.add_parser("import",
                         help="create task specs from a list file (or - for stdin)")
    ti.add_argument("file", help="path to a list file, or - to read stdin")
    ti.add_argument("-C", "--directory",
                    help="write into this worktree instead of the current one")
    ti.add_argument("--auto", action="store_true",
                    help="mark every imported spec scheduler-eligible (hitl: false)")
    ti.set_defaults(func=cmd_task_import)

    g = sub.add_parser("gate", help="can this project's gates be trusted")
    g.add_argument("-C", "--directory")
    g.set_defaults(func=cmd_gate)

    gr = sub.add_parser("gate-run", help="run the committed gate here and report")
    gr.add_argument("-C", "--directory")
    gr.add_argument("--event", default="session-idle")
    gr.set_defaults(func=cmd_gate_run)

    dm = sub.add_parser("daemon", help="supervise tasks, gates and the server")
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
             ".orchestration/)")
    un.add_argument("--keep-state", action="store_true",
                    help="stop the daemon and server but keep the database and "
                         "task history in ~/.local/state/orch")
    un.set_defaults(func=cmd_uninstall)

    v = sub.add_parser("version", help="show the orch version")
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
