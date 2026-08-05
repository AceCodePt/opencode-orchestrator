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

export const ROUTER_CONTRACT = 2;

const ORCH_DIR = '.orchestration';

function hookName(eventType: string): string {
  return eventType.replace(/\./g, '-');
}

async function readConfig($: any, worktree: string): Promise<OrchConfig> {
  const rel = `${ORCH_DIR}/config.json`;
  let working: OrchConfig = {};
  try {
    working = JSON.parse(
      await fs.readFile(path.join(worktree, rel), 'utf8'),
    ) as OrchConfig;
  } catch {
  }
  const base = working.baseBranch ?? 'main';

  try {
    const committed = JSON.parse(
      await $`git -C ${worktree} show ${base}:${rel}`.text(),
    ) as OrchConfig;
    return { ...committed, baseBranch: base };
  } catch {
    return { baseBranch: base };
  }
}

interface Gate {
  exec: string;
  
  diverged: boolean;
  cleanup: () => Promise<void>;
}

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
    return null;
  }
  if (!committed.trim()) return null;

  let diverged = false;
  try {
    diverged = (await fs.readFile(path.join(worktree, rel), 'utf8')) !== committed;
  } catch {
    diverged = true; 
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

async function fingerprint(
  $: any,
  worktree: string,
  gate: GateConfig,
): Promise<string> {
  const include = gate.include?.length ? gate.include : ['**/*'];
  const exclude = gate.exclude ?? [];

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
      h.update('<missing>');
    }
    h.update('\0');
  }
  return h.digest('hex');
}

function globAny(file: string, patterns: string[]): boolean {
  return patterns.some((p) => globOne(file, p));
}

function globOne(file: string, pattern: string): boolean {
  const rx = pattern
    .replace(/[.+^${}()|[\]\\]/g, '\\$&')
    .replace(/\*\*\/?/g, '\u0000')
    .replace(/\*/g, '[^/]*')
    .replace(/\u0000/g, '(?:.*/)?');
  return new RegExp(`^${rx}$`).test(file);
}

const ORCH = process.env.ORCH_BIN || 'orch';

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
    return null;
  }
}

export const OrchRouter: Plugin = async ({ $, client, directory, worktree }) => {
  const root = worktree ?? directory;
  const cfg = await readConfig($, root);
  const gate = cfg.gate ?? {};
  const retryCap = gate.retryCap ?? 3;

  let running = false;

  const memAttempts = new Map<string, number>();

  async function report(
    outcome: Outcome,
    event: string,
    detail: string,
    extra: { fingerprint?: string; diverged?: boolean; seconds?: number } = {},
  ) {
    await client.app
      .log({
        body: {
          service: 'orch-router',
          level: outcome === 'passed' ? 'info' : 'warn',
          message: `${event} -> ${outcome}${detail ? `: ${detail.slice(0, 400)}` : ''}`,
        },
      })
      .catch(() => undefined);

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
      await report(
        'no-gate-configured',
        event,
        `no ${hookName(event)} committed on ${cfg.baseBranch ?? 'main'}`,
      );
      return;
    }

    if (gate.diverged) {
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

    if (shouldSkipGate(state, current)) return;

    running = true;
    try {
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

      const local = (memAttempts.get(key) ?? (state?.attempts ?? 0)) + 1;
      memAttempts.set(key, local);
      const reported = await report(
        'failed', event, `attempt ${local}/${retryCap} (${secs}s)\n${tail}`,
        { diverged: gate.diverged, seconds: Number(secs) },
      );
      const attempts = Math.max(reported ?? 0, local);

      if (attempts >= retryCap) {
        await report('stuck', event, `gave up after ${attempts} attempts: ${tail}`, {
          fingerprint: current,
          diverged: gate.diverged,
        });
        return;
      }

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
