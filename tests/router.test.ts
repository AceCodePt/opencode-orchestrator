/**
 * Tests for the router's fail-closed rules.
 *
 * The router lives inside the agent's own server, so its verdicts are advisory - the
 * daemon runs the gate again out-of-loop before anything is called done. But one
 * decision here is not advisory at all: *whether to run the gate*. A wrongly skipped
 * run means nothing ever looked at the work, and no later component can recover the
 * check that did not happen.
 *
 * Which is why the store being unreachable has to mean "run it anyway".
 */

import { describe, expect, test } from 'bun:test';
import * as router from '../plugin/orch-router.ts';
import { shouldSkipGate } from '../plugin/orch-router.ts';

const FP = 'a'.repeat(64);

describe('plugin module shape', () => {
  test('every export is a function', () => {
    // opencode iterates a plugin's exports and requires each to be a function; a single
    // non-function export makes it reject the whole file with "Plugin export is not a
    // function". That disables the router silently - no gate fires, and the only trace
    // is one line in the server log. Cost an entire test cycle to find, so it is now a
    // test rather than a habit.
    for (const [name, value] of Object.entries(router)) {
      expect(typeof value, `export \`${name}\` must be a function`).toBe('function');
    }
  });

  test('the contract version is declared and readable without being exported', async () => {
    // `orch` reads it by pattern, which is why it must not be an export. The rule is
    // about the exported *value*, not the syntax: `export const OrchRouter = async ...`
    // is fine because it holds a function; `export const ROUTER_CONTRACT = 2` is not.
    const text = await Bun.file(
      new URL('../plugin/orch-router.ts', import.meta.url),
    ).text();
    const m = text.match(/ROUTER_CONTRACT\s*=\s*(\d+)/);
    expect(m).not.toBeNull();
    expect(Number(m![1])).toBeGreaterThanOrEqual(2);
    expect(router).not.toHaveProperty('ROUTER_CONTRACT');
  });
});

describe('shouldSkipGate', () => {
  test('skips only when a recorded baseline matches exactly', () => {
    expect(shouldSkipGate({ fingerprint: FP }, FP)).toBe(true);
    expect(shouldSkipGate({ fingerprint: FP }, 'b'.repeat(64))).toBe(false);
  });

  test('an unreachable store runs the gate rather than skipping it', () => {
    // `orchJSON` returns null when the CLI cannot be reached. Reading that as "no
    // change, therefore fine" would let an unavailable service approve unchecked
    // work, which is the failure this project exists to prevent.
    expect(shouldSkipGate(null, FP)).toBe(false);
  });

  test('a task with no baseline yet is checked', () => {
    // First run of a fresh worktree, and any worktree no dispatch created.
    expect(shouldSkipGate({}, FP)).toBe(false);
    expect(shouldSkipGate({ fingerprint: null }, FP)).toBe(false);
  });

  test('an empty fingerprint never matches', () => {
    // Both sides empty is the dangerous coincidence: a fingerprint of nothing must
    // not equal a baseline of nothing and silently pass.
    expect(shouldSkipGate({ fingerprint: '' }, '')).toBe(false);
    expect(shouldSkipGate({ fingerprint: FP }, '')).toBe(false);
  });
});
