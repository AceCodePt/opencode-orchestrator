/**
 * orch router - a generic event -> hook dispatcher.
 *
 * Installed into a project's `.opencode/plugins/` by `orch init`.
 *
 * This is deliberately NOT a feature. It is named after mechanism, not policy:
 * an event arrives, and if `.orchestration/hooks/<event-name>` exists it runs,
 * and the outcome is reported. What "verify" means for a project lives in that
 * hook, never here. Adding a new event should cost configuration, not code.
 *
 * It is also deliberately thin on harness knowledge. All HTTP vocabulary for
 * opencode lives on the Python side (see docs/architecture.md), so there is
 * nothing here to keep in sync when endpoints drift.
 */

import type { Plugin } from '@opencode-ai/plugin';
import { createHash } from 'crypto';
import fs from 'fs/promises';
import path from 'path';

type Outcome =
  | 'running'
  | 'passed'
  | 'failed'
  | 'stuck'
  | 'no-gate-configured'
  | 'gate-modified';

interface GateConfig {
  include?: string[];
  exclude?: string[];
  retryCap?: number;
}

interface OrchConfig {
  baseBranch?: string;
  gate?: GateConfig;
  daemon?: string;
}

/**
 * Contract version of this router. Bump it whenever the daemon starts relying on
 * something the router does.
 *
 * Projects own their copy of this file (the shadcn model), so a byte-level diff against
 * the shipped template cannot distinguish a deliberate customisation from a router that
 * predates a guarantee the supervisor now depends on. A declared number can, and
 * `doctor` fails - not warns - when it is too old.
 *
 *   1 - runs the committed base-branch gate; reports outcomes; bounded retries.
 *   2 - reports `running` *before* the gate executes. Without this the supervisor
 *       cannot tell a slow check from an agent nothing ever examined: the session is
 *       idle for the whole duration of a gate and the dispatch has usually exited.
 *       Measured with a 45s gate on a contract-1 router - the task was marked
 *       needs-review at 40s, mid-check, and then abandoned.
 */
export const ROUTER_CONTRACT = 2;

const ORCH_DIR = '.orchestration';

/** Map an opencode event type to a hook filename: `session.idle` -> `session-idle`. */
function hookName(eventType: string): string {
  return eventType.replace(/\./g, '-');
}

/**
 * Read config from the committed base branch, not the working tree.
 *
 * The config is as security-critical as the gate itself: `gate.include: []` makes
 * the fingerprint match everything and nothing, `exclude: ["**"]` means no change
 * ever looks worth checking, and a large `retryCap` turns a bounded loop into a
 * billable one. Trusting the working-tree copy would leave the agent able to
 * neuter its own gate without ever touching the hook.
 *
 * `baseBranch` is the one setting that must come from the working tree, because it
 * is the thing that tells us which committed ref to read.
 */
async function readConfig($: any, worktree: string): Promise<OrchConfig> {
  const rel = `${ORCH_DIR}/config.json`;
  let working: OrchConfig = {};
  try {
    working = JSON.parse(
      await fs.readFile(path.join(worktree, rel), 'utf8'),
    ) as OrchConfig;
  } catch {
    /* absent or malformed; defaults below */
  }
  const base = working.baseBranch ?? 'main';

  try {
    const committed = JSON.parse(
      await $`git -C ${worktree} show ${base}:${rel}`.text(),
    ) as OrchConfig;
    return { ...committed, baseBranch: base };
  } catch {
    // Not committed: fall back to defaults, NOT to the working copy. An
    // uncommitted config carries no authority, and `resolveGate` will report
    // `no-gate-configured` for the uncommitted hook anyway.
    return { baseBranch: base };
  }
}

interface Gate {
  /** Executable copy of the *committed* gate, staged outside the worktree. */
  exec: string;
  /** True when the working tree's copy differs from the committed one. */
  diverged: boolean;
  cleanup: () => Promise<void>;
}

/**
 * Resolve the gate from the base branch, never from the working tree.
 *
 * Observed on 2026-08-05: a dispatched agent hit the fail-closed scaffold, was
 * handed the failure, and *rewrote the hook that judges it* - then reported
 * `passed`. The rewrite happened to be honest, but nothing structurally stopped
 * it from writing `exit 0`. An agent that can edit its own gate can always pass.
 *
 * So the committed version is the authority. Evolving a gate then costs a commit
 * to the base branch, which is reviewable; editing it mid-task buys nothing.
 *
 * Staged in a temp dir *outside* the worktree so it is neither fingerprinted nor
 * visible to `git status`.
 */
async function resolveGate(
  $: any,
  worktree: string,
  event: string,
  base: string,
): Promise<Gate | null> {
  const rel = `${ORCH_DIR}/hooks/${hookName(event)}`;

  let committed: string;
  try {
    committed = await $`git -C ${worktree} show ${base}:${rel}`.text();
  } catch {
    // Not committed on the base branch. An uncommitted gate is not a gate:
    // reporting a pass off the working tree is what we are preventing.
    return null;
  }
  if (!committed.trim()) return null;

  let diverged = false;
  try {
    diverged = (await fs.readFile(path.join(worktree, rel), 'utf8')) !== committed;
  } catch {
    diverged = true; // deleted or unreadable in the worktree is itself divergence
  }

  const { tmpdir } = await import('os');
  const dir = await fs.mkdtemp(path.join(tmpdir(), 'orch-gate-'));
  const exec = path.join(dir, hookName(event));
  await fs.writeFile(exec, committed, { mode: 0o755 });

  return {
    exec,
    diverged,
    cleanup: () => fs.rm(dir, { recursive: true, force: true }),
  };
}

/**
 * Content hash of the files a project declared as mattering.
 *
 * Contents, not mtimes. An mtime fingerprint false-fires on `git checkout`,
 * `touch`, and stash - all of which rewrite timestamps without changing code.
 */
async function fingerprint(
  $: any,
  worktree: string,
  gate: GateConfig,
): Promise<string> {
  const include = gate.include?.length ? gate.include : ['**/*'];
  const exclude = gate.exclude ?? [];

  // Ask git for tracked+modified files rather than walking the tree: it already
  // knows about .gitignore, and orchestration must never hash node_modules.
  const listed = await $`git -C ${worktree} ls-files -co --exclude-standard`.text();
  const all = listed.split('\n').filter(Boolean);

  const matches = all.filter(
    (f: string) => globAny(f, include) && !globAny(f, exclude),
  );
  matches.sort();

  const h = createHash('sha256');
  for (const rel of matches) {
    h.update(rel);
    h.update('\0');
    try {
      h.update(await fs.readFile(path.join(worktree, rel)));
    } catch {
      // Deleted between listing and reading: its absence is part of the state.
      h.update('<missing>');
    }
    h.update('\0');
  }
  return h.digest('hex');
}

function globAny(file: string, patterns: string[]): boolean {
  return patterns.some((p) => globOne(file, p));
}

/** Minimal glob: supports `**`, `*` and literal segments. No dependencies. */
function globOne(file: string, pattern: string): boolean {
  const rx = pattern
    .replace(/[.+^${}()|[\]\\]/g, '\\$&')
    .replace(/\*\*\/?/g, '\u0000')
    .replace(/\*/g, '[^/]*')
    .replace(/\u0000/g, '(?:.*/)?');
  return new RegExp(`^${rx}$`).test(file);
}

/**
 * Router state lives in the orchestrator's database, reached through its CLI.
 *
 * It used to live in `.orchestration/.state/` inside the worktree being judged, which
 * was wrong three ways: two authorities for one question, per-worktree files that
 * cannot express a machine-wide concurrency limit, and state `orch observe` could not
 * see because it reads the database.
 *
 * A subprocess rather than an HTTP call to the daemon, deliberately. SQLite in WAL
 * mode already serialises writers across processes, so a socket would buy nothing and
 * add a service that can be *down* - and "the daemon was unreachable" must never
 * become a route by which a gate passes.
 */
const ORCH = process.env.ORCH_BIN || 'orch';

/**
 * Whether the gate can be skipped because nothing has changed since it last passed.
 *
 * Named and exported so the fail-closed rule is testable rather than incidental. The
 * only unsafe direction here is skipping: a needless run costs time, a wrongly skipped
 * run means work went out unchecked. So anything short of a known baseline that
 * matches exactly - state unavailable, no baseline recorded, empty string - must run
 * the gate.
 */
export function shouldSkipGate(
  state: { fingerprint?: string | null } | null,
  current: string,
): boolean {
  if (!state?.fingerprint || !current) return false;
  return state.fingerprint === current;
}

async function orchJSON($: any, args: string[]): Promise<any | null> {
  try {
    const r = await $`${ORCH} ${args}`.nothrow().quiet();
    if (r.exitCode !== 0) return null;
    return JSON.parse(r.stdout.toString());
  } catch {
    // Unreachable CLI is not fatal here, and must not be: every caller below treats
    // null as "no baseline known", which makes the gate *run* rather than skip.
    // Failing open on the skip decision would be the one unsafe direction.
    return null;
  }
}

export const OrchRouter: Plugin = async ({ $, client, directory, worktree }) => {
  const root = worktree ?? directory;
  const cfg = await readConfig($, root);
  const gate = cfg.gate ?? {};
  const retryCap = gate.retryCap ?? 3;

  let running = false;

  /**
   * Retry ceiling that survives the database being unreachable.
   *
   * The store holds the real counter, but if `orch` cannot be reached the cap would
   * otherwise be unenforceable and a failing gate would retry forever - the exact
   * unbounded billable loop the cap exists to prevent. In-memory only lasts as long as
   * this server does, which is weaker than the store and strictly better than nothing.
   */
  const memAttempts = new Map<string, number>();

  async function report(
    outcome: Outcome,
    event: string,
    detail: string,
    extra: { fingerprint?: string; diverged?: boolean; seconds?: number } = {},
  ) {
    // The plugin decides nothing about who is told. It produces a result; routing
    // is the daemon's job, which is why there is no report_back message here and
    // no notion of whether a human is in the loop.
    await client.app
      .log({
        body: {
          service: 'orch-router',
          level: outcome === 'passed' ? 'info' : 'warn',
          message: `${event} -> ${outcome}${detail ? `: ${detail.slice(0, 400)}` : ''}`,
        },
      })
      .catch(() => undefined);

    // Also to the store, so the outcome is visible to `orch task list` rather than
    // only to whoever greps the server log. Recorded as `source=router`: this runs
    // inside the agent's own server, so it is a self-report and cannot promote
    // anything to `done` - the daemon runs the gate again out-of-loop for that.
    const args = ['gate-report', '--worktree', root, '--event', event,
                  '--outcome', outcome];
    if (extra.fingerprint) args.push('--fingerprint', extra.fingerprint);
    if (extra.diverged) args.push('--diverged');
    if (extra.seconds) args.push('--seconds', String(extra.seconds));
    if (detail) args.push('--detail', detail.slice(0, 2000));
    const res = await orchJSON($, args);
    return res?.attempts as number | undefined;
  }

  async function runHook(event: string, sessionID: string) {
    if (running) return;

    const gate = await resolveGate($, root, event, cfg.baseBranch ?? 'main');
    if (!gate) {
      // Absence of a gate is NOT a pass. Reporting `passed` here would manufacture
      // confidence in work that nothing checked.
      await report(
        'no-gate-configured',
        event,
        `no ${hookName(event)} committed on ${cfg.baseBranch ?? 'main'}`,
      );
      return;
    }

    if (gate.diverged) {
      // Surfaced, not silenced, and it does not change the verdict: the committed
      // gate is what ran, so its result still means something. But an agent
      // editing its own gate is exactly what a human should look at.
      await report(
        'gate-modified',
        event,
        'the worktree copy differs from the committed gate; the committed one was ' +
          'used. Review the diff before trusting this task.',
      );
    }

    const state = await orchJSON($, [
      'gate-state', '--worktree', root, '--event', event,
    ]);
    const current = await fingerprint($, root, gate);

    // Persisted, so a TUI restart cannot silently re-baseline and skip the first
    // check of a session. A null state means the baseline is unknown, which runs the
    // gate rather than skipping it.
    if (shouldSkipGate(state, current)) return;

    running = true;
    try {
      // Announce before running, not after.
      //
      // The session is idle for the whole duration of the gate, and the dispatch
      // process has usually exited by now, so from outside this looks exactly like an
      // agent that stopped without anything ever checking it. With a fast gate the
      // window is invisible; with a real suite it is minutes long. Observed: a 45s gate
      // had its task marked `needs-review` at 40s, while the gate was still running.
      await report('running', event, 'gate started');

      const started = Date.now();
      const result = await $`${gate.exec}`
        .cwd(root)
        .env({
          ...process.env,
          WORKTREE_PATH: root,
          WORKTREE_NAME: path.basename(root),
          ORCH_EVENT: event,
          ORCH_SESSION_ID: sessionID,
        })
        .nothrow();
      const secs = ((Date.now() - started) / 1000).toFixed(1);

      const key = hookName(event);

      if (result.exitCode === 0) {
        memAttempts.set(key, 0);
        await report('passed', event, `${secs}s`, {
          fingerprint: current,
          diverged: gate.diverged,
          seconds: Number(secs),
        });
        return;
      }

      const output = `${result.stdout ?? ''}${result.stderr ?? ''}`.toString();
      const tail = output.slice(-2000);

      // The store owns the counter, but take whichever is higher: a CLI that could not
      // be reached returns undefined, and trusting that would reset the cap.
      const local = (memAttempts.get(key) ?? (state?.attempts ?? 0)) + 1;
      memAttempts.set(key, local);
      const reported = await report(
        'failed', event, `attempt ${local}/${retryCap} (${secs}s)\n${tail}`,
        { diverged: gate.diverged, seconds: Number(secs) },
      );
      const attempts = Math.max(reported ?? 0, local);

      if (attempts >= retryCap) {
        // The loop is self-inflicted, so it is bounded on purpose. An agent that
        // cannot fix a failure would otherwise retry forever and bill for it.
        await report('stuck', event, `gave up after ${attempts} attempts: ${tail}`, {
          fingerprint: current,
          diverged: gate.diverged,
        });
        return;
      }

      // Inject via the session API, never `tui.appendPrompt` + `tui.submitPrompt`.
      // Driving the TUI text box erases whatever the human was typing, requires a
      // TUI to exist at all, and races with the user.
      await client.session
        .prompt({
          path: { id: sessionID },
          body: {
            parts: [
              {
                type: 'text',
                text:
                  `The \`${hookName(event)}\` gate failed ` +
                  `(attempt ${attempts} of ${retryCap}). Fix these, then continue:\n\n` +
                  '```\n' + tail + '\n```',
              },
            ],
          },
        })
        .catch(async () => {
          await report('failed', event, 'could not inject prompt into session');
        });
    } finally {
      await gate.cleanup().catch(() => undefined);
      running = false;
    }
  }

  return {
    event: async ({ event }: { event: any }) => {
      const type: string = event?.type ?? '';
      const sessionID: string = event?.properties?.sessionID ?? '';
      if (!type || !sessionID) return;

      // Only gate primary sessions. A subagent going idle is not a task finishing,
      // and running a full check suite per subagent would thrash the machine.
      try {
        const s = await client.session.get({ path: { id: sessionID } });
        if ((s as any)?.data?.parentID) return;
        if ((s as any)?.error) return;
      } catch {
        return;
      }

      if (type === 'session.idle' || type === 'session.error') {
        await runHook(type, sessionID);
      }
    },
  };
};

export default OrchRouter;
