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

from . import harness, init as init_mod, observe
from .discovery import discover
from . import doctor as doctor_mod

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

    v = sub.add_parser("version", help="show orch and harness versions")
    v.set_defaults(func=cmd_version)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
