import test from "node:test";
import assert from "node:assert/strict";
import {
  parseNumberAxis,
  configurationDiff,
  batchNeedsPolling,
  benchmarkPagePath,
  experimentSummary,
  readBenchmarkDetail,
} from "../.test-build/models/experiments.js";

test("number axes trim tokens and deduplicate numeric values in input order", () => {
  assert.deepEqual(
    parseNumberAxis(" 4, 2, 4.0, 1e-3, 0.001 ", "axis"),
    [4, 2, 0.001],
  );
});

test("number axes reject empty tokens, partial numbers and non-finite values", () => {
  for (const value of [
    "",
    "   ",
    ",1",
    "1,",
    "1,,2",
    "1, ,2",
    "2ms",
    "1 2",
    "0x10",
    "NaN",
    "Infinity",
    "-Infinity",
    "1e309",
  ]) {
    assert.throws(() => parseNumberAxis(value, "axis"), Error, value);
  }
});

test("integer axes accept inclusive bounds and reject fractional or out-of-range values", () => {
  const options = { integer: true, min: 1, max: 64 };
  assert.deepEqual(parseNumberAxis("1,64,2e0", "axis", options), [1, 64, 2]);
  for (const value of ["0", "65", "1.5"]) {
    assert.throws(() => parseNumberAxis(value, "axis", options), Error, value);
  }
});

test("fractional axes apply inclusive numeric limits", () => {
  const options = { min: 0.001, max: 0.5 };
  assert.deepEqual(
    parseNumberAxis("0.001,0.5,0.25", "axis", options),
    [0.001, 0.5, 0.25],
  );
  for (const value of ["0.0009", "0.5001"]) {
    assert.throws(() => parseNumberAxis(value, "axis", options), Error, value);
  }
});

test("number axes enforce the default and custom token limits before deduplication", () => {
  assert.equal(parseNumberAxis("1,2,3,4,5,6,7,8,9,10", "axis").length, 10);
  assert.throws(() => parseNumberAxis("1,1,1,1,1,1,1,1,1,1,1", "axis"), Error);
  assert.deepEqual(parseNumberAxis("2,2", "axis", { maxLength: 2 }), [2]);
  assert.throws(
    () => parseNumberAxis("2,2,2", "axis", { maxLength: 2 }),
    Error,
  );
});

test("configuration differences recurse into objects and sort full dot paths", () => {
  const before = { z: 1, solver: { tolerance: 0.01, block_size: 16 }, a: true };
  const after = {
    a: false,
    solver: { block_size: 32, tolerance: 0.001 },
    z: 2,
  };
  assert.deepEqual(configurationDiff(before, after), [
    { path: "a", before: true, after: false },
    { path: "solver.block_size", before: 16, after: 32 },
    { path: "solver.tolerance", before: 0.01, after: 0.001 },
    { path: "z", before: 1, after: 2 },
  ]);
});

test("configuration additions and deletions preserve leaf values and use null for absence", () => {
  const before = { removed: { enabled: false, count: 0 }, kept: 1 };
  const after = { added: { title: "", enabled: true }, kept: 1 };
  assert.deepEqual(configurationDiff(before, after), [
    { path: "added.enabled", before: null, after: true },
    { path: "added.title", before: null, after: "" },
    { path: "removed.count", before: 0, after: null },
    { path: "removed.enabled", before: false, after: null },
  ]);
});

test("configuration arrays are compared as leaves while equal content produces no difference", () => {
  const before = {
    solver: { modes: ["a", "b"], flags: [1, 2] },
    nullable: null,
  };
  const after = {
    nullable: null,
    solver: { flags: [1, 2], modes: ["b", "a"] },
  };
  assert.deepEqual(configurationDiff(before, after), [
    { path: "solver.modes", before: ["a", "b"], after: ["b", "a"] },
  ]);
  assert.deepEqual(configurationDiff(before, structuredClone(before)), []);
});

test("batch polling follows every active job status even when the batch reports terminal", () => {
  for (const status of [
    "CREATED",
    "QUEUED",
    "PENDING",
    "RUNNING",
    "VALIDATING",
    "VERIFYING",
    "PLANNING",
    "CANCELLING",
  ]) {
    assert.equal(
      batchNeedsPolling({ status: "FAILED", runs: [{ status }] }),
      true,
      status,
    );
  }
});

test("batch polling honors active summary counts, derived counts and active batch status", () => {
  assert.equal(
    batchNeedsPolling({ status: "FAILED", runs: [], summary: { active: 1 } }),
    true,
  );
  assert.equal(
    batchNeedsPolling({
      status: "SUCCEEDED",
      runs: [],
      summary: { total: 4, terminal: 3 },
    }),
    true,
  );
  assert.equal(
    batchNeedsPolling({ status: "QUEUED", runs: [], summary: { active: 0 } }),
    true,
  );
});

test("failed and fully terminal batches stop polling", () => {
  for (const status of [
    "SUCCEEDED",
    "PARTIAL",
    "FAILED",
    "CANCELLED",
    "TIMED_OUT",
  ]) {
    assert.equal(
      batchNeedsPolling({
        status,
        runs: [
          { status: "SUCCEEDED" },
          { status: "PARTIAL" },
          { status: "FAILED" },
          { status: "CANCELLED" },
          { status: "TIMED_OUT" },
        ],
        summary: { total: 5, terminal: 5 },
      }),
      false,
      status,
    );
  }
});

test("benchmark detail reads forward the abort signal and encode the batch id", async () => {
  const controller = new AbortController();
  const batch = { id: "batch /?&", status: "RUNNING", runs: [] };
  const request = async (path, init) => {
    assert.equal(path, "/benchmarks/batch%20%2F%3F%26");
    assert.equal(init.signal, controller.signal);
    return batch;
  };
  assert.deepEqual(
    await readBenchmarkDetail(batch.id, request, controller.signal),
    { batch, delay: 2500 },
  );
});

test("benchmark detail reads stop scheduling after a terminal response", async () => {
  const batch = {
    id: "batch-1",
    status: "FAILED",
    runs: [{ status: "FAILED" }],
    summary: { total: 1, terminal: 1 },
  };
  assert.deepEqual(
    await readBenchmarkDetail(
      batch.id,
      async () => batch,
      new AbortController().signal,
    ),
    { batch, delay: null },
  );
});

test("benchmark detail reads cannot publish a response after selection cancellation", async () => {
  const controller = new AbortController();
  const request = async () => {
    await Promise.resolve();
    controller.abort();
    return { id: "old-batch", status: "RUNNING", runs: [] };
  };
  await assert.rejects(
    readBenchmarkDetail("old-batch", request, controller.signal),
    { name: "AbortError" },
  );
});

test("benchmark page paths encode project and cursor with a fixed page limit", () => {
  const path = benchmarkPagePath("project & 中文/1", "opaque+/=?&#");
  const url = new URL(path, "http://localhost");
  assert.equal(url.pathname, "/benchmarks/page");
  assert.equal(url.searchParams.get("limit"), "20");
  assert.equal(url.searchParams.get("project_id"), "project & 中文/1");
  assert.equal(url.searchParams.get("cursor"), "opaque+/=?&#");
  assert.equal(
    new URL(benchmarkPagePath("p1"), url).searchParams.has("cursor"),
    false,
  );
});

test("experiment summaries preserve explicit timing denominators including failed measurements", () => {
  assert.deepEqual(
    experimentSummary({
      total: 8,
      certified: 2,
      partial: 1,
      failed: 5,
      active: 0,
      terminal: 8,
      timing_denominator: 6,
      certified_timing_denominator: 1,
    }),
    {
      total: 8,
      certified: 2,
      partial: 1,
      failed: 5,
      active: 0,
      recordedTimes: 6,
      certifiedTimes: 1,
    },
  );
});

test("experiment summaries default missing counters to zero without guessing timing denominators", () => {
  assert.deepEqual(experimentSummary({ total: 3, certified: 3, terminal: 3 }), {
    total: 3,
    certified: 3,
    partial: 0,
    failed: 0,
    active: 0,
    recordedTimes: 0,
    certifiedTimes: 0,
  });
  assert.deepEqual(experimentSummary({}), {
    total: 0,
    certified: 0,
    partial: 0,
    failed: 0,
    active: 0,
    recordedTimes: 0,
    certifiedTimes: 0,
  });
});

test("experiment summaries derive nonnegative active counts only when active is absent", () => {
  assert.equal(experimentSummary({ total: 7, terminal: 5 }).active, 2);
  assert.equal(experimentSummary({ total: 2, terminal: 3 }).active, 0);
  assert.equal(
    experimentSummary({ total: 7, terminal: 5, active: 0 }).active,
    0,
  );
});
