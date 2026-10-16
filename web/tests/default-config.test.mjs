import test from "node:test";
import assert from "node:assert/strict";

// The configuration factory can be exercised without a browser or credentials.
globalThis.sessionStorage = { getItem: () => null };
const { defaultConfig } = await import("../.test-build/api.js");

test("a new UI draft fits a single CPU and does not mutate another draft", () => {
  const first = defaultConfig("m1");
  assert.equal(first.resources.threads, 1);
  first.resources.threads = 8;
  const second = defaultConfig("m2");
  assert.equal(second.resources.threads, 1);
  assert.equal(first.problem.matrix_id, "m1");
  assert.equal(second.problem.matrix_id, "m2");
});
