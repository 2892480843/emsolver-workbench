import test from "node:test";
import assert from "node:assert/strict";
import {
  convergenceHistory,
  sampleExtrema,
} from "../.test-build/components/convergence.js";

test("rank histories use measured residuals, original event sequence and explicit lock indices", () => {
  const history = convergenceHistory([
    {
      seq: 1,
      event: "shift_attempt",
      sigma_work: 0.25,
      sigma_target: 0.2,
      attempt: 1,
    },
    {
      seq: 2,
      event: "block_residual_checked",
      iteration: 1,
      residuals: [1e-2, 4e-2],
      locked_indices: [],
      metrics: { soft_locked_modes: 0 },
    },
    {
      seq: 3,
      event: "block_residual_checked",
      iteration: 2,
      residuals: [0, 1e-4],
      locked_indices: [0],
      metrics: { soft_locked_modes: 1 },
    },
    { seq: 4, event: "attempt_validated", residual_max: 2e-8 },
  ]);
  assert.equal(history.modeCount, 2);
  assert.deepEqual(
    history.series[0].points.map((p) => [
      p.seq,
      p.iteration,
      p.residual,
      p.locked,
      p.sigmaWork,
    ]),
    [
      [2, 1, 1e-2, false, 0.25],
      [3, 2, 0, true, 0.25],
    ],
  );
  assert.equal(history.series[1].points.at(-1).locked, false);
  assert.equal(history.attempts[0].residual, 2e-8);
  assert.equal(
    history.markers.filter((m) => m.kind === "lock").at(-1).count,
    1,
  );
});

test("bounded curves retain narrow per-bucket residual spikes and troughs", () => {
  const points = Array.from({ length: 1000 }, (_, i) => ({
    seq: i + 1,
    residual: 1e-4,
  }));
  points[101].residual = 0.9;
  points[845].residual = 1e-16;
  const sampled = sampleExtrema(points, 40);
  assert.ok(sampled.length <= 40);
  assert.equal(sampled[0], points[0]);
  assert.equal(sampled.at(-1), points.at(-1));
  assert.ok(sampled.includes(points[101]));
  assert.ok(sampled.includes(points[845]));
  assert.deepEqual(
    sampled.map((p) => p.seq),
    [...sampled.map((p) => p.seq)].sort((a, b) => a - b),
  );
});

test("failures, shifts, lock changes, restarts and fallback markers are never decimated", () => {
  const events = Array.from({ length: 100 }, (_, i) => ({
    seq: i + 1,
    event: "shift_failed",
    error_code: "SHIFT_SINGULAR",
  }));
  events.push(
    { seq: 101, event: "shift_attempt", sigma_work: 0.3, sigma_target: 0.2 },
    { seq: 102, event: "modes_soft_locked", metrics: { soft_locked_modes: 3 } },
    { seq: 103, event: "block_restarted", metrics: { restarts: 2 } },
    {
      seq: 104,
      event: "fallback_started",
      metrics: { profile: "block_to_arpack_v1", reason: "NO_CONVERGENCE" },
    },
    { seq: 105, event: "fallback_completed", n_accepted: 2 },
  );
  const history = convergenceHistory(events, 0, 1, 8);
  assert.equal(history.markers.filter((m) => m.kind === "failure").length, 100);
  assert.deepEqual(
    history.markers.slice(-5).map((m) => m.kind),
    ["shift", "lock", "restart", "fallback", "fallback"],
  );
  assert.equal(history.markers.at(-3).count, 2);
});

test("ARPACK attempt maxima do not invent per-mode histories or lock status", () => {
  const history = convergenceHistory([
    { seq: 1, event: "arpack_started" },
    { seq: 2, event: "attempt_validated", residual_max: 1e-9 },
    { seq: 3, event: "linear_progress", residual: 1e-12 },
  ]);
  assert.equal(history.modeCount, 0);
  assert.deepEqual(history.series, []);
  assert.deepEqual(history.stalls, []);
  assert.equal(history.attempts.length, 1);
});

test("only explicit stagnation observations produce intervals; absent values remain unavailable", () => {
  const residual = (seq, stalled_checks) => ({
    seq,
    event: "block_residual_checked",
    iteration: seq,
    residuals: [1e-3],
    ...(stalled_checks === undefined
      ? {}
      : { stalled_checks, stagnation_threshold: 3 }),
  });
  const history = convergenceHistory([
    residual(1),
    residual(2, 1),
    residual(3, 2),
    residual(4, 0),
    residual(5),
  ]);
  assert.deepEqual(history.stalls, [
    { start: 2, end: 3, checks: 2, threshold: 3 },
  ]);
  assert.equal(history.stagnationAvailable, true);
  assert.equal(history.series[0].points[0].locked, null);
  assert.equal(convergenceHistory([residual(1)]).stagnationAvailable, false);
});

test("rank paging includes later ranks and ignores malformed numeric fields without fabricating zeros", () => {
  const history = convergenceHistory(
    [
      {
        seq: 1,
        event: "block_residual_checked",
        iteration: 1,
        residuals: [null, -1, NaN, Infinity, 0.2, 0.3, 0.4],
      },
      {
        seq: 2,
        event: "block_residual_checked",
        iteration: 2,
        residuals: [1e-3, 2e-3, 3e-3, 4e-3, 0.02, 0.03, 0.04],
      },
    ],
    4,
    2,
  );
  assert.equal(history.modeCount, 7);
  assert.deepEqual(
    history.series.map((s) => s.rank),
    [4, 5],
  );
  assert.deepEqual(
    history.series[0].points.map((p) => p.residual),
    [0.2, 0.02],
  );
  assert.equal(
    convergenceHistory([{ seq: 1, residuals: [null] }]).series.length,
    0,
  );
});

test("restart closes the observed stagnation interval and preserves its actual reason", () => {
  const history = convergenceHistory([
    {
      seq: 1,
      event: "block_residual_checked",
      residuals: [1e-3],
      stalled_checks: 2,
      stagnation_threshold: 3,
    },
    {
      seq: 2,
      event: "block_restarted",
      restart_reason: "residual_stagnation",
      metrics: { restarts: 1 },
    },
    {
      seq: 3,
      event: "block_residual_checked",
      residuals: [2e-3],
      stalled_checks: 1,
      stagnation_threshold: 3,
    },
  ]);
  assert.equal(history.stalls.length, 2);
  assert.equal(history.stalls[0].end, 1);
  assert.match(
    history.markers.find((m) => m.kind === "restart").label,
    /residual_stagnation/,
  );
});

test("a changed lock set is visible even when the aggregate count stays unchanged", () => {
  const history = convergenceHistory([
    {
      seq: 1,
      event: "block_residual_checked",
      residuals: [1e-10, 1e-2],
      locked_indices: [0],
      metrics: { soft_locked_modes: 1 },
    },
    {
      seq: 2,
      event: "block_residual_checked",
      residuals: [1e-2, 1e-10],
      locked_indices: [1],
      metrics: { soft_locked_modes: 1 },
    },
  ]);
  const locks = history.markers.filter((marker) => marker.kind === "lock");
  assert.equal(locks.length, 2);
  assert.notEqual(locks[0].label, locks[1].label);
});
