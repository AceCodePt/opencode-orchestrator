from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

from . import agent as agent_mod
from . import daemon as daemon_mod
from . import dispatch as dispatch_mod
from . import doctor as doctor_mod
from . import verify as verify_mod
from . import harness
from . import hooks as hooks_mod
from . import init as init_mod
from . import observe
from . import project as project_mod
from . import server as server_mod
from . import state
from . import tasks as tasks_mod

__version__ = "0.1.0"


def cmd_init(args: argparse.Namespace) -> int:
    start = Path(args.directory) if args.directory else Path.cwd()
    r = init_mod.run(start, force=args.force, version=__version__)
    print(init_mod.render(r))
    return 0 if (r.created or r.skipped) else 2


def cmd_doctor(args: argparse.Namespace) -> int:
    checks, worst = doctor_mod.run(Path(args.directory) if args.directory else None)
    print(doctor_mod.render(checks))
    return worst


def _fire_global(event: str, env: dict[str, str] | None = None,
                 cwd: Path | None = None) -> None:
    try:
        pid = hooks_mod.run(event, {**(env or {}), "ORCH_EVENT": event}, cwd)
    except OSError as e:
        print(f"  ! global hook {event} could not spawn: {e}", file=sys.stderr)
        return
    if pid is not None:
        print(f"  fired global hook {event} pid={pid}")


def _fire_task_global(event: str, proj: state.Project, task: str,
                      branch: str | None = None, worktree: Path | None = None,
                      extra: dict[str, str] | None = None) -> None:
    env: dict[str, str] = {
        "ORCH_ACTION": "cli",
        "PROJECT_DIR": str(proj.common),
        "PROJECT_NAME": daemon_mod.project_label(str(proj.common)),
        "BASE_BRANCH": proj.base,
        "TASK_NAME": task,
    }
    if branch:
        env["BRANCH_NAME"] = branch
    if worktree is not None:
        env["WORKTREE_PATH"] = str(worktree)
        env["WORKTREE_NAME"] = worktree.name
    if extra:
        env.update(extra)
    _fire_global(event, env, cwd=proj.base_worktree)


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
        _fire_global("restart-server")
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
        _fire_global("restart-server")
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
    )
    for s in r.steps:
        print(f"  {s}")
    sys.stdout.flush()
    for p in r.problems:
        print(f"  ! {p}", file=sys.stderr)
    sys.stderr.flush()
    if r.ok and r.worktree is not None and not args.dry_run:
        proj = state.Project.from_dir(r.worktree)
        if proj is not None:
            _fire_task_global("dispatched", proj, r.worktree.name,
                              worktree=r.worktree)
    return 0 if r.ok else 1


def _task_mtime(t: state.TaskState, base_wt: Path | None) -> float:
    """The newest folder mtime for a task: the worktree tree for a claimed
    task, else tasks/<name> or archive/<name> in the base worktree. 0 when no
    folder exists. Drives the display sort only - scan order (which the
    scheduler iterates in name order) is untouched."""
    paths = [Path(p) for p in (t.worktree, base_wt and base_wt / "tasks" / t.name,
                               base_wt and base_wt / "archive" / t.name)
             if p]
    newest = 0.0
    for root in paths:
        try:
            if root.is_dir():
                for dirpath, _dirs, files in os.walk(root):
                    for name in files + [""]:
                        p = Path(dirpath, name) if name else Path(dirpath)
                        try:
                            newest = max(newest, p.stat().st_mtime)
                        except OSError:
                            pass
            elif root.exists():
                newest = max(newest, root.stat().st_mtime)
        except OSError:
            pass
    return newest


def cmd_task_list(args: argparse.Namespace) -> int:
    if args.global_:
        return _task_list_global(args)
    start = Path(args.directory) if args.directory else Path.cwd()
    proj = state.Project.from_dir(start)
    if proj is None:
        print("not in a worktree", file=sys.stderr)
        return 2
    st = server_mod.status()
    port = st.get("port") if st.get("running") and not st.get("unresponsive") else None
    tasks = proj.scan(port)
    base_wt = proj.base_worktree
    tasks.sort(key=lambda t: (_task_mtime(t, base_wt), t.name))
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
        if t.wait_human_start:
            state_info += " start-wait"
        if t.wait_human_merge:
            state_info += " merge-wait"
        if t.dependencies:
            state_info += " deps=[" + ",".join(t.dependencies) + "]"
        if t.dirty:
            state_info += " dirty"
        elif t.diff:
            state_info += " diff"
        print(f"  {t.status:12} {t.name:34} {branch:<34} {where}{state_info}")
        for n in t.notes:
            print(f"    ! {n}")
        for b in t.blocked:
            print(f"    ↳ {b.kind}: {b.detail}  [{b.request_id}]")
        sec, action, detail = _classify_wait(t, base_wt)
        if sec == "needs-you" and t.claimed:
            print(f"    NEEDS-YOU: {detail}")
            if action:
                print(f"    -> {action}")
    return 0


def _roots_from(args: argparse.Namespace) -> list[str] | None:
    if not args.roots:
        return None
    return [r.strip() for r in args.roots.split(",") if r.strip()]


def _q(path: str | Path) -> str:
    return shlex.quote(str(path))


def _classify_wait(t: state.TaskState, base_wt: Path | None) -> tuple[str | None, str | None, str]:
    if t.session == state.SESSION_BLOCKED:
        b = t.blocked[0] if t.blocked else None
        if b is not None and b.kind == "question":
            action = f"orch task answer -C {_q(base_wt or t.project)} {t.name} <your reply>"
        else:
            action = f"orch task answer -C {_q(base_wt or t.project)} {t.name} allow|deny"
        detail = f"blocked on {b.kind}: {b.detail}" if b else "blocked"
        return "needs-you", action, detail
    if t.session == state.SESSION_BUSY:
        return None, None, ""
    if base_wt is None:
        return "needs-you", f"orch doctor -C {_q(t.project)}", "no base worktree"
    if not t.worktree or not Path(t.worktree).is_dir():
        return "needs-you", f"orch task reset -C {_q(base_wt)} {t.name}", "worktree missing"
    if t.conflicted:
        return "needs-you", f"resolve the merge conflict in {_q(base_wt)}", \
            "merge conflict in progress"
    if t.interrupted and (t.dirty or t.diff):
        return "needs-you", f"orch task archive -C {_q(base_wt)} {t.name}", \
            "interrupted: the turn was aborted (a user stop); the work is held for you to verify"
    if t.dirty:
        return "needs-you", f"review the uncommitted changes in {_q(t.worktree)}", \
            "worktree is dirty"
    if t.diff and t.wait_human_merge:
        return "needs-you", f"orch task merge -C {_q(base_wt)} {t.name}", \
            "verified, awaiting your merge"
    if t.diff:
        return "looks-done", None, "unmerged (awaiting verify/merge)"
    return None, None, ""


def _task_list_global(args: argparse.Namespace) -> int:
    roots = _roots_from(args)
    st = server_mod.status()
    running = bool(st.get("running")) and not bool(st.get("unresponsive"))
    port = st.get("port") if running else None
    info = server_mod.current()
    du = daemon_mod.unit_status()
    daemon_active = bool(du.get("installed") and du.get("ActiveState") == "active")

    projects = state.discover_projects(roots)
    if not projects:
        print("no supervised projects discovered under "
              + ", ".join(state.default_roots()), file=sys.stderr)
        return 2

    items: list[dict] = []
    for common, orch in sorted(projects.items()):
        proj = state.Project(Path(common), orch, state.base_from_config(orch))
        base_wt = proj.base_worktree
        tasks = proj.scan(port)
        agent = "none"
        if info is not None and base_wt is not None:
            agent = (agent_mod.status(info, base_wt) or {}).get("state") or "none"
        for t in tasks:
            if t.archived or not t.claimed:
                continue
            sec, action, detail = _classify_wait(t, base_wt)
            if sec is None:
                continue
            if sec == "looks-done" and not daemon_active:
                detail = "unmerged and nothing is settling it (daemon down)"
            items.append({
                "project": daemon_mod.project_label(str(common)),
                "project_dir": str(base_wt) if base_wt else common,
                "task": t.name,
                "section": sec,
                "detail": detail,
                "action": action,
                "agent": agent,
            })

    if args.json:
        print(json.dumps({"server": "up" if running else "down",
                          "daemon": "active" if daemon_active else "down",
                          "items": items}, indent=2))
        return 0

    print(f"server   {'up' if running else 'down'}")
    if daemon_active:
        print("daemon   active")
    else:
        print("daemon   down - nothing is settling, verifying, or merging")
    if not running:
        print("note     server is down: blocked status is unknown; `orch server start`")
    if not items:
        print("\nnothing waiting - no blocked, stuck, or unmerged work")
        return 0

    last = None
    for it in items:
        if it["project"] != last:
            suffix = f"   primary-agent: {it['agent']}" if it["agent"] != "none" else ""
            print(f"\n{it['project']}{suffix}")
            last = it["project"]
        print(f"  {it['section'].upper():12} {it['task']}")
        print(f"      {it['detail']}")
        if it["action"]:
            print(f"      -> {it['action']}")

    needs_you = any(i["section"] == "needs-you" for i in items)
    stalled = not daemon_active and any(i["section"] == "looks-done" for i in items)
    return 1 if (needs_you or stalled) else 0


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
        "wait_human_start": t.wait_human_start,
        "wait_human_merge": t.wait_human_merge,
        "interrupted": t.interrupted,
        "dependencies": t.dependencies,
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
    if r.ok:
        _fire_task_global("reset", proj, t.name, branch=t.branch,
                          worktree=Path(t.worktree))
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
    if r.ok:
        _fire_task_global("archived", proj, t.name, branch=t.branch,
                          worktree=Path(t.worktree))
    return 0 if r.ok else 1


def cmd_task_add(args: argparse.Namespace) -> int:
    start = Path(args.directory) if args.directory else Path.cwd()
    r = tasks_mod.add_many(start, args.name, title=args.title,
                           depends=args.depends)
    print(tasks_mod.render(r))
    return 0 if r.ok else 2


def cmd_task_merge(args: argparse.Namespace) -> int:
    from . import merge as merge_mod
    start = Path(args.directory) if args.directory else Path.cwd()
    proj = state.Project.from_dir(start)
    if proj is None:
        print("not in a worktree", file=sys.stderr)
        return 2
    tasks = proj.scan()
    name = args.name
    if name is None:
        branch = dispatch_mod.current_branch(start)
        if branch is None or branch == proj.base:
            print(f"no task named: run `orch task merge` from the task's own "
                  f"worktree (you are on {branch or 'a detached HEAD'})",
                  file=sys.stderr)
            return 2
        name = branch
    t = next((x for x in tasks if x.name == name), None)
    if t is None or not t.claimed or not t.worktree or not t.branch:
        print(f"no claimed task: {name}", file=sys.stderr)
        return 2
    r = merge_mod.merge_and_cleanup(proj.common, name, t.branch,
                                    Path(t.worktree), proj.base)
    for s in r.steps:
        print(f"  {s}")
    for p in r.problems:
        print(f"  ! {p}", file=sys.stderr)
    if r.ok:
        _fire_task_global("merged", proj, t.name, branch=t.branch,
                          worktree=Path(t.worktree))
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
            harness.reply_permission(port, b.request_id,
                                     "once" if reply == "allow" else "reject",
                                     directory=t.worktree)
            print(f"{reply}ed permission {b.request_id} for {args.name}")
    except Exception as e:  # noqa: BLE001
        print(f"could not answer {b.request_id}: {e}", file=sys.stderr)
        return 1
    if len(t.blocked) > 1:
        print(f"{len(t.blocked) - 1} more pending request(s); pass --request <id> "
              "to answer a specific one", file=sys.stderr)
    return 0


@dataclass
class _PermAsk:
    owner: str
    worktree: str
    kind: str
    detail: str
    request_id: str
    session_id: str


def _perm_asks(port: int, proj: state.Project) -> list[_PermAsk]:
    asks: list[_PermAsk] = []
    for t in proj.scan(port):
        if not t.claimed or not t.worktree:
            continue
        for b in t.blocked:
            if b.kind == "question":
                continue
            asks.append(_PermAsk(owner=t.name, worktree=t.worktree,
                                 kind=b.kind, detail=b.detail,
                                 request_id=b.request_id, session_id=b.session_id))
    base_wt = proj.base_worktree
    if base_wt is not None:
        sid = agent_mod.find(port, base_wt)
        if sid is not None:
            for p in harness.permissions(port, str(base_wt)):
                if str(p.get("sessionID") or "") != sid:
                    continue
                d = observe._describe_permission(p)
                asks.append(_PermAsk(owner=f"primary-agent@{proj.base}",
                                     worktree=str(base_wt), kind=d.kind,
                                     detail=d.detail, request_id=d.request_id,
                                     session_id=d.session_id))
    return asks


def _pick_project(args: argparse.Namespace, port: int,
                  start: Path) -> tuple[state.Project | None, int]:
    projects = state.discover_projects(_roots_from(args))
    if not projects:
        print("no supervised projects discovered under "
              + ", ".join(state.default_roots()), file=sys.stderr)
        return None, 2
    current = state.Project.from_dir(start)
    cur_common = str(current.common) if current else None
    items: list[tuple[state.Project, str, int]] = []
    for common, orch in sorted(projects.items()):
        p = state.Project(Path(common), orch, state.base_from_config(orch))
        n = len(_perm_asks(port, p))
        if n:
            items.append((p, daemon_mod.project_label(common), n))
    if not items:
        print("nothing pending - no permission asks in any supervised project")
        return None, 0
    if len(items) == 1:
        return items[0][0], 0
    print("pick a project:")
    for i, (_p, label, n) in enumerate(items, 1):
        mark = " *" if str(_p.common) == cur_common else ""
        print(f"  {i:>2}. {label}{mark}  {n} pending")
    print("  q. quit")
    while True:
        line = _prompt("pick a project: ")
        if line is None:
            print()
            return None, 0
        c = line.strip().lower()
        if c in ("q", "quit"):
            return None, 0
        try:
            idx = int(c)
        except ValueError:
            print("  a number or q", file=sys.stderr)
            continue
        if 1 <= idx <= len(items):
            return items[idx - 1][0], 0
        print(f"  pick 1..{len(items)} or q", file=sys.stderr)


def _pick_owner(proj: state.Project, port: int) -> str | None:
    asks = _perm_asks(port, proj)
    if not asks:
        print("nothing pending - no permission asks in this project")
        return None
    owners: list[str] = []
    for a in asks:
        if a.owner not in owners:
            owners.append(a.owner)
    if len(owners) == 1:
        return owners[0]
    print("pick a task:")
    for i, owner in enumerate(owners, 1):
        own = [a for a in asks if a.owner == owner]
        kinds = " · ".join(a.kind for a in own)
        print(f"  {i:>2}. {owner}  {len(own)} pending: {kinds}")
    print("  q. quit")
    while True:
        line = _prompt("pick a task: ")
        if line is None:
            print()
            return None
        c = line.strip().lower()
        if c in ("q", "quit"):
            return None
        try:
            idx = int(c)
        except ValueError:
            print("  a number or q", file=sys.stderr)
            continue
        if 1 <= idx <= len(owners):
            return owners[idx - 1]
        print(f"  pick 1..{len(owners)} or q", file=sys.stderr)


def _explain_denied(port: int, session_id: str, reason: str) -> None:
    """Tell the agent why its ask was denied and keep it working, so the
    session stays busy instead of settling idle and being swept up as a
    no-op by the daemon. Best effort - a session that vanished is fine."""
    text = (f"The permission request was denied. Reason: {reason}. "
            "Adjust your approach and continue the task; if you need to ask "
            "something, ask it.")
    try:
        harness.prompt_async(port, session_id, text)
    except harness.HarnessError:
        pass


_DENY_DEFAULT = "denied by the operator; adjust your approach and continue"


def _walk_permissions(proj: state.Project, port: int, owner: str) -> int:
    print(f"\npermissions for {owner}")
    asks = [a for a in _perm_asks(port, proj) if a.owner == owner]
    left = 0
    for ask in asks:
        print(f"\n  {ask.kind}: {ask.detail}")
        while True:
            line = _prompt("  [a]llow  [d]eny  [s]kip  [q]uit: ")
            if line is None:
                left += 1
                break
            c = line.strip().lower()
            if c in ("a", "allow"):
                try:
                    harness.reply_permission(port, ask.request_id, "once",
                                             directory=ask.worktree)
                except Exception as e:  # noqa: BLE001
                    print(f"  ! could not allow {ask.request_id}: {e}",
                          file=sys.stderr)
                    left += 1
                else:
                    print(f"  allowed {ask.request_id}")
                break
            if c in ("d", "deny"):
                reason = _prompt("  reason (sent to the agent): ") or ""
                reason = reason.strip() or _DENY_DEFAULT
                try:
                    harness.reply_permission(port, ask.request_id, "reject",
                                             directory=ask.worktree,
                                             message=reason)
                except Exception as e:  # noqa: BLE001
                    print(f"  ! could not deny {ask.request_id}: {e}",
                          file=sys.stderr)
                    left += 1
                else:
                    _explain_denied(port, ask.session_id, reason)
                    print(f"  denied {ask.request_id}: {reason}")
                break
            if c in ("s", "skip"):
                left += 1
                break
            if c in ("q", "quit"):
                left += 1
                return 1
            print("  allow, deny, skip or quit", file=sys.stderr)
    still = [a for a in _perm_asks(port, proj) if a.owner == owner]
    if still:
        print(f"\n{len(still)} still pending for {owner}:")
        for a in still:
            print(f"  {a.kind}: {a.detail}")
        return 1
    if left:
        print(f"\n{left} left unanswered for {owner}")
        return 1
    print(f"\nall permissions for {owner} answered")
    return 0


def _perm_one_shot(proj: state.Project, port: int, name: str,
                   reply: str, reason: str | None = None) -> int:
    asks = [a for a in _perm_asks(port, proj) if a.owner == name]
    if not asks:
        print(f"{name} has no pending permission asks", file=sys.stderr)
        return 1
    ask = asks[0]
    reason_text = None
    if reply == "deny":
        reason_text = (reason or "").strip() or _DENY_DEFAULT
    try:
        if reply == "deny":
            harness.reply_permission(port, ask.request_id, "reject",
                                     directory=ask.worktree,
                                     message=reason_text)
        else:
            harness.reply_permission(port, ask.request_id, "once",
                                     directory=ask.worktree)
    except Exception as e:  # noqa: BLE001
        print(f"could not answer {ask.request_id}: {e}", file=sys.stderr)
        return 1
    if reply == "deny":
        assert reason_text is not None
        _explain_denied(port, ask.session_id, reason_text)
        print(f"denied permission {ask.request_id} for {name}: {reason_text}")
    else:
        print(f"allowed permission {ask.request_id} for {name}")
    if len(asks) > 1:
        print(f"{len(asks) - 1} more pending request(s) for {name}",
              file=sys.stderr)
    return 0


def _perm_list(proj: state.Project, port: int) -> int:
    asks = _perm_asks(port, proj)
    if not asks:
        print("nothing pending - no permission asks in this project")
        return 0
    for a in asks:
        print(f"{a.owner}  {a.kind}: {a.detail}  [{a.request_id}]")
    return 0


def _perm_json(args: argparse.Namespace, port: int, proj: state.Project | None,
               global_mode: bool) -> int:
    items: list[dict] = []
    projects: list[state.Project] = []
    if global_mode:
        for common, orch in sorted(state.discover_projects(_roots_from(args)).items()):
            projects.append(state.Project(Path(common), orch,
                                          state.base_from_config(orch)))
    else:
        if proj is None:
            print("not in a project; pass -C <directory>", file=sys.stderr)
            return 2
        projects = [proj]
    for p in projects:
        label = daemon_mod.project_label(str(p.common))
        for a in _perm_asks(port, p):
            items.append({"project": label, "owner": a.owner, "kind": a.kind,
                          "detail": a.detail, "request_id": a.request_id})
    print(json.dumps(items, indent=2))
    return 0


def cmd_permissions(args: argparse.Namespace) -> int:
    start = Path(args.directory) if args.directory else Path.cwd()
    st = server_mod.status()
    port = (st.get("port") if st.get("running")
            and not st.get("unresponsive") else None)
    if not port:
        print("no shared server; `orch server start` first", file=sys.stderr)
        return 2

    proj = state.Project.from_dir(start)
    global_mode = args.global_ or proj is None

    if args.task:
        if proj is None:
            print("not in a project; pass -C <directory>", file=sys.stderr)
            return 2
        reply = (args.reply or "").strip().lower()
        if reply not in ("allow", "deny"):
            print("the --task one-shot takes allow|deny", file=sys.stderr)
            return 2
        return _perm_one_shot(proj, port, args.task, reply, reason=args.reason)
    if args.reply:
        print("a reply needs --task <name>", file=sys.stderr)
        return 2
    if args.json:
        return _perm_json(args, port, proj, global_mode)
    if args.directory is not None and not args.global_:
        if proj is None:
            print("not in a project; pass -C <directory>", file=sys.stderr)
            return 2
        return _perm_list(proj, port)
    if not _interactive():
        print("interactive approval needs a terminal; pass --task <name> "
              "allow|deny for a one-shot, or -C <dir> to list", file=sys.stderr)
        return 2

    if global_mode:
        proj, rc = _pick_project(args, port, start)
        if proj is None:
            return rc
    else:
        assert proj is not None
        print(f"project {daemon_mod.project_label(str(proj.common))}")

    owner = _pick_owner(proj, port)
    if owner is None:
        return 0
    return _walk_permissions(proj, port, owner)


def cmd_verify(args: argparse.Namespace) -> int:
    start = Path(args.directory) if args.directory else Path.cwd()
    top, _, err = dispatch_mod.project_of(start)
    if err or top is None:
        print(err or "not in a worktree", file=sys.stderr)
        return 2
    findings = verify_mod.inspect(top)
    worst = 0
    for f in findings:
        mark = {"ok": "  ", "diverged": "~ ", "uncommitted": "! ",
                "missing": "! "}.get(f.state, "? ")
        print(f"{mark}{f.name}: {f.state}")
        print(f"    {f.detail}")
        if f.state == verify_mod.DIVERGED:
            worst = max(worst, 1)
        elif not f.trustworthy:
            worst = max(worst, 2)
    if args.run:
        v = verify_mod.run(top, args.event)
        print(f"{v.event}: {v.summary}")
        if v.diverged:
            print("  ! the worktree's hook differs from the committed one; the "
                  "committed one ran")
        if v.detail:
            for line in v.detail.strip().splitlines()[-20:]:
                print(f"  {line}")
        if v.outcome == verify_mod.NO_VERIFY:
            return 0
        return 0 if v.justifies_done else 1
    if any(f.state in (verify_mod.OK, verify_mod.DIVERGED)
           and f.name in verify_mod.VERIFY_HOOKS for f in findings):
        print("    run it: orch verify --run")
    if not findings:
        print("no verification hook committed - tasks are accepted without verification")
        return 0
    return worst


def cmd_install(args: argparse.Namespace) -> int:
    from . import custom_tools
    from . import install as install_mod

    status, lines = install_mod.install()
    for line in lines:
        print(line)
    if status == "refused":
        return 1
    for line in install_mod.path_hint():
        print(line)

    if custom_tools.ensure():
        try:
            server_mod.restart()
            print("custom tools installed to "
                  f"{custom_tools.TOOLS_DIR}; shared server restarted")
        except (OSError, RuntimeError, TimeoutError) as e:
            print(f"custom tools installed to {custom_tools.TOOLS_DIR}, but "
                  f"could not restart the shared server: {e}")
    else:
        print(f"custom tools current in {custom_tools.TOOLS_DIR}")
    for line in daemon_mod.install(interval=args.interval,
                                   enable=not args.no_enable):
        print(line)
    print("\nlogs: journalctl --user -u orch-daemon -f")
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    from . import install as install_mod

    for line in install_mod.uninstall():
        print(line)
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

    if args.action == "restart":
        if not daemon_mod.unit_path().exists():
            print("no unit installed - nothing to restart. `orch install`",
                  file=sys.stderr)
            return 1
        for line in daemon_mod.restart():
            print(line)
        info = daemon_mod.unit_status()
        return 0 if info.get("ActiveState") == "active" else 1

    if args.action == "budget":
        if args.max is None:
            b = state.machine_budget()
            if b is None:
                print("machine budget: unlimited - only per-project "
                      "scheduler.maxConcurrent caps auto-dispatch")
            else:
                print(f"machine budget: {b} concurrent task agents "
                      "machine-wide")
            return 0
        if args.max == "off":
            state.set_machine_budget(None)
            print("machine budget: unlimited")
            return 0
        try:
            n = int(args.max)
            if n < 0:
                raise ValueError
        except ValueError:
            print(f"{args.max!r} is not a non-negative maxConcurrent; pass a "
                  "number, `off`, or nothing to show it", file=sys.stderr)
            return 2
        state.set_machine_budget(n)
        print(f"machine budget: {n} concurrent task agents machine-wide "
              "(`orch daemon budget` to re-check)")
        return 0

    project: str | None = None
    if args.directory or args.project_only:
        start = Path(args.directory) if args.directory else Path.cwd()
        _, p, err = dispatch_mod.project_of(start)
        if err or p is None:
            print(err or "not in a worktree", file=sys.stderr)
            return 2
        project = str(p)
    return daemon_mod.run(project, interval=args.interval,
                          once=args.once, dry_run=args.dry_run,
                          sse=not args.no_sse)


def cmd_attach(args: argparse.Namespace) -> int:
    start = Path(args.directory) if args.directory else Path.cwd()
    proj, err = agent_mod.project_of(start)
    if err or proj is None or proj.base_worktree is None:
        print(err or "no base worktree for the primary agent", file=sys.stderr)
        return 2
    base = proj.base_worktree
    try:
        info = server_mod.start()
    except (RuntimeError, TimeoutError, OSError) as e:
        print(f"could not start the shared server: {e}", file=sys.stderr)
        return 1
    try:
        t = agent_mod.task_at(proj, info.port, start)
        if t is not None and t.worktree:
            dir_ = Path(t.worktree)
            sid = agent_mod.ensure_task(info.port, dir_, t.name)
        else:
            dir_ = base
            sid = agent_mod.ensure(info.port, base)
    except harness.HarnessError as e:
        print(e, file=sys.stderr)
        return 1
    try:
        os.execvp("opencode", ["opencode", "attach", info.url,
                               "--session", sid, "--dir", str(dir_)])
    except OSError as e:
        print(f"could not exec opencode attach: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_project_help(args: argparse.Namespace) -> int:
    args._pparser.print_help()
    return 0


def _interactive() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, OSError):
        return False


def _prompt(message: str) -> str | None:
    try:
        return input(message).strip()
    except EOFError:
        return None


def _yesno(message: str, default: bool) -> bool:
    suffix = "Y/n" if default else "y/N"
    answer = _prompt(f"{message} [{suffix}] ")
    if answer is None or answer == "":
        return default
    return answer.lower() in ("y", "yes")


def cmd_project_create(args: argparse.Namespace) -> int:
    interactive = _interactive()

    name = args.flag_name or args.name
    if not name:
        if not interactive:
            print("  ! a name is required: pass `--name <name>` "
                  "(stdin is not a terminal)", file=sys.stderr)
            return 1
        name = _prompt("project name: ")
        if not name:
            print("  ! no project name given", file=sys.stderr)
            return 1

    path = args.path
    if path is None and interactive:
        default = str(Path.cwd())
        p = _prompt(f"parent directory [{default}]: ")
        if p:
            path = str(Path(p).expanduser())
    if path is None:
        path = str(Path.cwd())

    gh = args.no_gh is not True
    if args.no_gh is None and interactive:
        gh = _yesno("create and push a GitHub repo", default=True)

    public = bool(args.public)
    if args.public is None and interactive and gh:
        public = _yesno("make the GitHub repo public", default=False)

    description = args.description
    if args.description is None and interactive and gh:
        d = _prompt("repo description (optional): ")
        description = d or None

    allow = bool(args.allow_unsupervised)
    if args.allow_unsupervised is None and interactive:
        parent = Path(path).expanduser()
        if project_mod._unsupervised(parent, state.default_roots()):
            allow = _yesno("the parent is outside the supervised roots; "
                           "create it anyway", default=False)

    r = project_mod.create(
        name,
        path=path,
        gh=gh,
        public=public,
        description=description,
        version=__version__,
        allow_unsupervised=allow,
    )
    for s in r.steps:
        print(f"  {s}")
    for p in r.problems:
        print(f"  ! {p}", file=sys.stderr)
    if not r.ok:
        return 1
    return 0


def cmd_project_clone(args: argparse.Namespace) -> int:
    interactive = _interactive()

    url = args.url
    if not url:
        print("  ! a repository url is required: `orch project clone <url>`",
              file=sys.stderr)
        return 1

    name = args.flag_name
    if not name:
        name = project_mod.name_from_url(url)
    if not name:
        if not interactive:
            print("  ! cannot derive a project name from the url; pass "
                  "`--name <name>` (stdin is not a terminal)", file=sys.stderr)
            return 1
        name = _prompt("project name: ")
        if not name:
            print("  ! no project name given", file=sys.stderr)
            return 1

    path = args.path
    if path is None and interactive:
        default = str(Path.cwd())
        p = _prompt(f"parent directory [{default}]: ")
        if p:
            path = str(Path(p).expanduser())
    if path is None:
        path = str(Path.cwd())

    allow = bool(args.allow_unsupervised)
    if args.allow_unsupervised is None and interactive:
        parent = Path(path).expanduser()
        if project_mod._unsupervised(parent, state.default_roots()):
            allow = _yesno("the parent is outside the supervised roots; "
                           "clone it anyway", default=False)

    def pick_branch(branches: list[str]) -> str | None:
        print("the clone has no 'main' branch - pick the base branch:")
        for i, b in enumerate(branches, 1):
            print(f"  {i}. {b}")
        ans = _prompt(f"base branch [{branches[0]}]: ")
        if ans is None or ans == "":
            return branches[0]
        try:
            idx = int(ans)
            if 1 <= idx <= len(branches):
                return branches[idx - 1]
        except ValueError:
            pass
        if ans in branches:
            return ans
        print(f"  ! {ans!r} is not one of: " + ", ".join(branches),
              file=sys.stderr)
        return None

    def ask_trust() -> bool:
        return _yesno("the clone already ships .orchestration; trust it so the "
                      "daemon verifies/merges here", default=False)

    r = project_mod.clone(
        url,
        name=name,
        path=path,
        base=args.base,
        trust=bool(args.trust),
        version=__version__,
        allow_unsupervised=allow,
        pick_branch=pick_branch if interactive else None,
        ask_trust=ask_trust if interactive else None,
    )
    for s in r.steps:
        print(f"  {s}")
    for p in r.problems:
        print(f"  ! {p}", file=sys.stderr)
    if not r.ok:
        return 1
    return 0


def cmd_project_trust(args: argparse.Namespace) -> int:
    start = Path(args.directory) if args.directory else Path.cwd()
    ok, msg = project_mod.trust(start)
    print(msg)
    return 0 if ok else 1


def cmd_project_untrust(args: argparse.Namespace) -> int:
    start = Path(args.directory) if args.directory else Path.cwd()
    ok, msg = project_mod.untrust(start)
    print(msg)
    return 0 if ok else 1


def cmd_project_list(args: argparse.Namespace) -> int:
    projects = project_mod.list_projects()
    if args.json:
        print(json.dumps(projects, indent=2))
        return 0
    if not projects:
        print("no supervised projects discovered under "
              + ", ".join(state.default_roots()))
        return 0
    for p in projects:
        mark = "trusted" if p["trusted"] else "untrusted"
        print(f"  {mark:9} {p['common']}")
    print("\nroots: " + ", ".join(state.default_roots()))
    print("trusted projects are settled by the daemon; untrusted ones only "
          "alert until `orch project trust` runs")
    return 0


def cmd_project_scheduler(args: argparse.Namespace) -> int:
    start = Path(args.directory) if args.directory else Path.cwd()
    proj = state.Project.from_dir(start)
    if proj is None:
        print("not in a worktree", file=sys.stderr)
        return 2

    if args.max is None:
        pol = state.scheduler_from_config(
            state.committed_config(proj.common, proj.base))
        if not pol.enabled:
            print("scheduler: off (only `orch task start` dispatches)")
            return 0
        print(f"scheduler: on, maxConcurrent={pol.max_concurrent}")
        return 0

    if args.max == "off":
        ok, msg = state.write_scheduler_policy(
            proj.common, proj.base, enabled=False, max_concurrent=None)
    else:
        try:
            n = int(args.max)
            if n < 1:
                raise ValueError
        except ValueError:
            print(f"{args.max!r} is not a positive maxConcurrent; pass a number "
                  "or `off`", file=sys.stderr)
            return 2
        ok, msg = state.write_scheduler_policy(
            proj.common, proj.base, enabled=True, max_concurrent=n)
    print(msg)
    return 0 if ok else 1


def cmd_project_base(args: argparse.Namespace) -> int:
    interactive = _interactive()
    start = Path(args.directory) if args.directory else Path.cwd()
    proj = state.Project.from_dir(start)
    if proj is None:
        print("not in a worktree", file=sys.stderr)
        return 2

    branch = args.branch
    if not branch:
        if not interactive:
            print("  ! a branch is required: `orch project base <branch>` "
                  "(stdin is not a terminal)", file=sys.stderr)
            return 1
        branches = project_mod._branches(proj.common)
        print("pick the new base branch:")
        for i, b in enumerate(branches, 1):
            print(f"  {i}. {b}")
        ans = _prompt(f"base branch [{branches[0]}]: ")
        if ans is None or ans == "":
            branch = branches[0]
        else:
            if ans in branches:
                branch = ans
            else:
                try:
                    idx = int(ans)
                    if 1 <= idx <= len(branches):
                        branch = branches[idx - 1]
                except ValueError:
                    pass
        if branch not in branches:
            print(f"  ! {branch!r} is not one of: " + ", ".join(branches),
                  file=sys.stderr)
            return 1

    r = project_mod.base(proj.common, branch)
    for s in r.steps:
        print(f"  {s}")
    for p in r.problems:
        print(f"  ! {p}", file=sys.stderr)
    if not r.ok:
        return 1
    return 0


def cmd_version(_: argparse.Namespace) -> int:
    print(f"orch {__version__}")
    return 0


def cmd_tasktool(args: argparse.Namespace) -> int:
    """Internal backend for the machine's create/update/archive/delete custom
    tools. Hidden from help; called as `orch tasktool <op> ...`."""
    from . import tasktool as tasktool_mod

    return tasktool_mod.main(list(args.rest))


class _SubcommandHelpFormatter(argparse.HelpFormatter):
    def _format_action(self, action: argparse.Action) -> str:
        if action.nargs == argparse.PARSER:
            parts: list[str] = []
            for subaction in self._iter_indented_subactions(action):
                if getattr(subaction, "help", None) == argparse.SUPPRESS:
                    continue
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

    i = sub.add_parser("init", help="scaffold .orchestration/ with the verification hook template")
    i.add_argument("-C", "--directory")
    i.add_argument("--force", action="store_true",
                   help="overwrite existing files (samples included)")
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
    ts.set_defaults(func=cmd_task_start)

    tl = tsub.add_parser("list", help="tasks recorded for this project")
    tl.add_argument("-C", "--directory")
    tl.add_argument("--json", action="store_true")
    tl.add_argument("--global", dest="global_", action="store_true",
                    help="scan every supervised project and show what is waiting "
                         "on you (blocked, stuck, and unmerged work)")
    tl.add_argument("--roots", help="comma-separated roots to scan (--global only)")
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
    ta.add_argument("--depends", nargs="+", default=[],
                    help="task slugs this task blocks on (dependencies front "
                         "matter); the scheduler waits until they are archived")
    ta.set_defaults(func=cmd_task_add)

    tm = tsub.add_parser("merge",
                         help="merge a done task into base and clean up")
    tm.add_argument("name", nargs="?",
                    help="task to merge; defaults to the task of the worktree "
                         "you run this from")
    tm.add_argument("-C", "--directory")
    tm.set_defaults(func=cmd_task_merge)

    ta2 = tsub.add_parser("answer",
                          help="answer a blocked permission or question for a task")
    ta2.add_argument("name")
    ta2.add_argument("reply",
                     help="allow|deny for a permission; the answer text for a question")
    ta2.add_argument("-C", "--directory")
    ta2.add_argument("--request", help="answer a specific pending request id")
    ta2.set_defaults(func=cmd_task_answer)

    pj = sub.add_parser("project", help="create and inspect projects")
    pj.set_defaults(func=cmd_project_help, _pparser=pj)
    pjsub = pj.add_subparsers(dest="project_cmd", metavar="<command>",
                              title="project commands")
    pc = pjsub.add_parser(
        "create",
        help="scaffold a new git project (gh repo + orch init by default)",
    )
    pc.add_argument("name", nargs="?", default=None,
                    help="project name (prompts for it if omitted)")
    pc.add_argument("--name", dest="flag_name",
                    help="project name (same as the positional)")
    pc.add_argument("--dir", "--path", dest="path", default=None,
                    help="parent directory (default: current directory; "
                         "prompts for it if omitted)")
    pc.add_argument("--no-gh", action="store_true", default=None,
                    help="skip creating the GitHub repo (prompts if omitted)")
    pc.add_argument("--public", action="store_true", default=None,
                    help="make the GitHub repo public (default: private; "
                         "prompts if omitted)")
    pc.add_argument("--description", help="GitHub repo description "
                                          "(prompts if omitted)")
    pc.add_argument("--allow-unsupervised", action="store_true", default=None,
                    help="create even outside the supervised roots (the daemon "
                         "will not discover it; prompts if the parent is "
                         "outside the roots)")
    pc.set_defaults(func=cmd_project_create)

    px = pjsub.add_parser(
        "clone",
        help="clone an existing repo into orch's layout "
             "(bare store + base worktree)",
    )
    px.add_argument("url", help="the repository url to clone (any git url)")
    px.add_argument("--name", dest="flag_name", default=None,
                    help="project name (default: derived from the url)")
    px.add_argument("--dir", "--path", dest="path", default=None,
                    help="parent directory (default: current directory; "
                         "prompts for it if omitted)")
    px.add_argument("--base", default=None,
                    help="base branch when the clone has no 'main' "
                         "(prompts for a pick if omitted and interactive)")
    px.add_argument("--trust", action="store_true",
                    help="trust a clone that already ships .orchestration "
                         "(not trusted by default - it is third-party code)")
    px.add_argument("--allow-unsupervised", action="store_true", default=None,
                    help="clone even outside the supervised roots (the daemon "
                         "will not discover it; prompts if the parent is "
                         "outside the roots)")
    px.set_defaults(func=cmd_project_clone)

    pt = pjsub.add_parser(
        "trust",
        help="let the daemon verify, merge and dispatch this project",
    )
    pt.add_argument("-C", "--directory")
    pt.set_defaults(func=cmd_project_trust)

    pu = pjsub.add_parser(
        "untrust",
        help="stop the daemon from acting on this project (does not touch worktrees)",
    )
    pu.add_argument("-C", "--directory")
    pu.set_defaults(func=cmd_project_untrust)

    pl = pjsub.add_parser(
        "list",
        help="every discovered project and its trust status",
    )
    pl.add_argument("--json", action="store_true")
    pl.set_defaults(func=cmd_project_list)

    ps = pjsub.add_parser(
        "scheduler",
        help="show or set this project's auto-dispatch concurrency "
             "(scheduler.maxConcurrent)",
    )
    ps.add_argument("max", nargs="?", default=None,
                    help="maxConcurrent for the scheduler, or `off`; omit to show "
                         "the committed policy")
    ps.add_argument("-C", "--directory")
    ps.set_defaults(func=cmd_project_scheduler)

    pb = pjsub.add_parser(
        "base",
        help="repoint this project's base worktree onto an existing branch",
    )
    pb.add_argument("branch", nargs="?", default=None,
                    help="the branch to make the base (prompts for a pick "
                         "if omitted and interactive)")
    pb.add_argument("-C", "--directory")
    pb.set_defaults(func=cmd_project_base)

    g = sub.add_parser("verify", help="can this project's verification hook be trusted")
    g.add_argument("-C", "--directory")
    g.add_argument("--run", action="store_true",
                   help="also run the committed verification hook here and report")
    g.add_argument("--event", default="session-idle",
                   help="which committed hook to run (default: session-idle)")
    g.set_defaults(func=cmd_verify)

    at = sub.add_parser(
        "attach",
        help="attach the interactive TUI to the project's primary agent "
             "(created on first use), or to a task's session when run from "
             "inside a claimed task worktree",
    )
    at.add_argument("-C", "--directory",
                    help="the project to attach to (default: the current "
                         "worktree's project)")
    at.set_defaults(func=cmd_attach)

    pm = sub.add_parser(
        "permissions",
        help="approve or deny pending permission asks interactively",
    )
    pm.add_argument("--task",
                    help="answer one task by name (one-shot; takes allow|deny)")
    pm.add_argument("reply", nargs="?", default=None,
                    help="allow|deny for the --task one-shot")
    pm.add_argument("--reason",
                    help="why the ask is denied; sent to the agent so it can "
                         "adjust and keep working (default: a generic denial)")
    pm.add_argument("--global", dest="global_", action="store_true",
                    help="pick a project across every supervised project first")
    pm.add_argument("--roots",
                    help="comma-separated roots to scan (--global only)")
    pm.add_argument("--json", action="store_true",
                    help="list pending permission asks without interacting")
    pm.add_argument("-C", "--directory",
                    help="the project to use; passing it makes the command "
                         "non-interactive (lists the project's pending asks, "
                         "or answers --task <name> allow|deny)")
    pm.set_defaults(func=cmd_permissions)

    dm = sub.add_parser("daemon", help="supervise tasks, verification and the server")
    da = dm.add_subparsers(dest="action", metavar="<action>", title="daemon actions")
    dr = da.add_parser("run",
                       help="run the supervision loop (foreground; Ctrl-C stops)")
    ds = da.add_parser("status",
                       help="report what systemd says is supervising")
    da.add_parser("restart",
                  help="restart the supervised daemon so it loads the installed code")
    dm.add_argument("-C", "--directory",
                    help="supervise only this project (default: every project)")
    dm.add_argument("--project-only", action="store_true",
                    help="supervise only the project of the current directory")
    dm.add_argument("-n", "--interval", type=float, default=1.0)
    dm.add_argument("--once", action="store_true", help="one pass, then exit")
    dm.add_argument("--dry-run", action="store_true",
                    help="decide and print, change nothing")
    dm.add_argument("--no-sse", action="store_true",
                    help="do not consume the server's /event stream (per-event "
                         "hooks stay silent)")
    dm.add_argument("--json", action="store_true")
    # The run flags exist on both the parent and the `run` subparser so
    # `orch daemon <flag> run` and `orch daemon run <flag>` both work, matching
    # how the actions were called before they became subcommands.
    dr.add_argument("-C", "--directory",
                    help="supervise only this project (default: every project)")
    dr.add_argument("--project-only", action="store_true",
                    help="supervise only the project of the current directory")
    dr.add_argument("-n", "--interval", type=float, default=1.0)
    dr.add_argument("--once", action="store_true", help="one pass, then exit")
    dr.add_argument("--dry-run", action="store_true",
                    help="decide and print, change nothing")
    dr.add_argument("--no-sse", action="store_true",
                    help="do not consume the server's /event stream (per-event "
                         "hooks stay silent)")
    ds.add_argument("--json", action="store_true")
    db = da.add_parser(
        "budget",
        help="show or set the machine-wide scheduler budget in "
             "~/.config/orch/daemon.json",
    )
    db.add_argument("max", nargs="?", default=None,
                    help="max concurrent task agents machine-wide, `off` to make "
                         "it unlimited, or omit to show the current budget")
    dm.set_defaults(func=cmd_daemon, _dparser=dm)

    inst = sub.add_parser(
        "install",
        help="copy orch to the canonical location and write a launcher on PATH, "
             "then enable the systemd user unit supervising every project")
    inst.add_argument("-n", "--interval", type=float, default=1.0)
    inst.add_argument("--no-enable", action="store_true",
                      help="write the systemd unit but do not enable it")
    inst.set_defaults(func=cmd_install)

    un = sub.add_parser(
        "uninstall",
        help="remove the launcher and the canonical install dir (never a "
             "project's .orchestration/), plus every config-level trace of orch")
    un.add_argument("--keep-state", action="store_true",
                    help="stop the daemon and server but keep the database and "
                         "task history in ~/.local/state/orch")
    un.set_defaults(func=cmd_uninstall)

    v = sub.add_parser("version", help="show the orch version")
    v.set_defaults(func=cmd_version)

    tt = sub.add_parser("tasktool", help=argparse.SUPPRESS)
    tt.add_argument("rest", nargs=argparse.REMAINDER,
                    help=argparse.SUPPRESS)
    tt.set_defaults(func=cmd_tasktool)
    return p


def main(argv: list[str] | None = None) -> int:
    if argv is None and not sys.argv[1:]:
        build_parser().print_help()
        return 0
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
