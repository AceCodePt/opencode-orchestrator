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
import { shouldSkipGate } from '../plugin/orch-router.ts';

const FP = 'a'.repeat(64);

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
