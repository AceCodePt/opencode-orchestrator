# Take the Gate Out of the Agent's Writable Surface

## Context

Observed on 2026-08-05 against a live headless agent, and it is the sharpest thing
this design got wrong.

A task was dispatched into a scaffolded worktree. The agent produced the
deliverable and went idle. The router ran the fail-closed scaffold, which failed by
design, and injected the failure back into the session as instructions. The agent
read `.orchestration/`, **rewrote `hooks/session-idle`** into a gate tailored to the
task, and went idle again. The router ran it, it passed, and the retry counter
reset.

Every component behaved as specified. That is the problem: the agent authored the
criterion by which it was then judged, and the resulting `session.idle -> passed`
is indistinguishable from an independently verified pass. This rewrite happened to
be honest — it really did check the deliverable — but nothing structurally
prevented `exit 0`, and "it happened to behave" is not a security property.

The same hole exists through `config.json`, without touching the hook at all:
`gate.exclude: ["**"]` means no change ever looks worth checking, and a large
`retryCap` turns a deliberately bounded loop into a billable one.

## Requirements

Done in the router:

- Run `git show <baseBranch>:.orchestration/hooks/<event>`, staged to a temp dir
  outside the worktree so it is neither fingerprinted nor visible to `git status`.
- Read `config.json` from the base branch too. `baseBranch` itself is the one value
  that must come from the working tree, since it names the ref to read.
- Uncommitted hook → `no-gate-configured`. An uncommitted gate is not a gate.
- Working copy differs from committed → also report `gate-modified`, for review. It
  does not overturn the verdict, because the committed gate is what ran.

Remaining:

- `orch doctor`: report an uncommitted or diverging gate as a finding, since the
  failure is otherwise silent — the task simply reports `no-gate-configured` and a
  human has to know what that means.
- `orch task start`: commit the gate as part of dispatch, or refuse to dispatch,
  rather than letting a dirty gate silently degrade to no gate.
- Decide where `gate-modified` is routed. It is a human-review signal, not a task
  failure, and the daemon has no notion of that category yet.

## Verification

Done and reproducible: commit a gate that fails loudly, overwrite the working-tree
copy with `exit 0`, dispatch a task that touches the gated file set. Observed:

```
session.idle -> gate-modified: the worktree copy differs from the committed gate…
session.idle -> stuck: gave up after 1 attempts: COMMITTED GATE SAYS NO
```

Before the change, the same setup reports `passed`.

## Prohibited Patterns

- Executing the working-tree copy of a hook, for any reason.
- Reading gate configuration from the working tree.
- Treating a diverging gate as a hard failure. It is a review signal; the committed
  gate already produced a trustworthy verdict, and conflating the two trains people
  to ignore it.
- Any mechanism whose safety rests on the agent choosing not to edit a file it can
  write.
