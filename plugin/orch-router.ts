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

type Outcome = 'passed' | 'failed' | 'stuck' | 'no-gate-configured';

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

const ORCH_DIR = '.orchestration';
const STATE_DIR = path.join(ORCH_DIR, '.state');

/** Map an opencode event type to a hook filename: `session.idle` -> `session-idle`. */
function hookName(eventType: string): string {
  return eventType.replace(/\./g, '-');
}

async function readConfig(worktree: string): Promise<OrchConfig> {
  try {
    const raw = await fs.readFile(path.join(worktree, ORCH_DIR, 'config.json'), 'utf8');
    return JSON.parse(raw) as OrchConfig;
  } catch {
    return {};
  }
}

async function hookPath(worktree: string, event: string): Promise<string | null> {
  const p = path.join(worktree, ORCH_DIR, 'hooks', hookName(event));
  try {
    await fs.access(p, (await import('fs')).constants.X_OK);
    return p;
  } catch {
    return null;
  }
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

async function readState(worktree: string, key: string): Promise<string | null> {
  try {
    return (await fs.readFile(path.join(worktree, STATE_DIR, key), 'utf8')).trim();
  } catch {
    return null;
  }
}

async function writeState(worktree: string, key: string, value: string): Promise<void> {
  const dir = path.join(worktree, STATE_DIR);
  await fs.mkdir(dir, { recursive: true });
  await fs.writeFile(path.join(dir, key), value);
}

export const OrchRouter: Plugin = async ({ $, client, directory, worktree }) => {
  const root = worktree ?? directory;
  const cfg = await readConfig(root);
  const gate = cfg.gate ?? {};
  const retryCap = gate.retryCap ?? 3;

  let running = false;

  async function report(outcome: Outcome, event: string, detail: string) {
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
  }

  async function runHook(event: string, sessionID: string) {
    if (running) return;

    const hook = await hookPath(root, event);
    if (!hook) {
      // Absence of a gate is NOT a pass. Reporting `passed` here would manufacture
      // confidence in work nothing checked - the same silent blindness as a TUI
      // launched without --port.
      await report('no-gate-configured', event, `no ${hookName(event)} hook`);
      return;
    }

    const fpKey = `fingerprint-${hookName(event)}`;
    const previous = await readState(root, fpKey);
    const current = await fingerprint($, root, gate);

    // Persisted, so a TUI restart cannot silently re-baseline and skip the first
    // check of a session.
    if (previous === current) return;

    running = true;
    try {
      const started = Date.now();
      const result = await $`${hook}`
        .env({
          ...process.env,
          WORKTREE_PATH: root,
          WORKTREE_NAME: path.basename(root),
          ORCH_EVENT: event,
          ORCH_SESSION_ID: sessionID,
        })
        .nothrow();
      const secs = ((Date.now() - started) / 1000).toFixed(1);

      if (result.exitCode === 0) {
        await writeState(root, fpKey, current);
        await writeState(root, `retries-${hookName(event)}`, '0');
        await report('passed', event, `${secs}s`);
        return;
      }

      const attempts = Number((await readState(root, `retries-${hookName(event)}`)) ?? '0') + 1;
      await writeState(root, `retries-${hookName(event)}`, String(attempts));

      const output = `${result.stdout ?? ''}${result.stderr ?? ''}`.toString();
      const tail = output.slice(-2000);

      if (attempts >= retryCap) {
        // The loop is self-inflicted, so it is bounded on purpose. An agent that
        // cannot fix a failure would otherwise retry forever and bill for it.
        await writeState(root, fpKey, current);
        await report('stuck', event, `gave up after ${attempts} attempts: ${tail}`);
        return;
      }

      await report('failed', event, `attempt ${attempts}/${retryCap} (${secs}s)`);

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
