import test from "node:test";
import assert from "node:assert/strict";
import { rowNnzBins } from "../.test-build/components/histogram.js";

test("row histogram preserves actual edge ranges, row counts and inclusive final endpoint", () => {
  assert.deepEqual(
    rowNnzBins({ counts: [3, 0, 7], bin_edges: [0, 2, 4, 6], max_bins: 64 }),
    [
      { start: 0, end: 2, rows: 3, includesEnd: false },
      { start: 2, end: 4, rows: 0, includesEnd: false },
      { start: 4, end: 6, rows: 7, includesEnd: true },
    ],
  );
});

test("histogram refuses malformed or oversized bins without drawing invented data", () => {
  for (const invalid of [
    null,
    {},
    { counts: [1], bin_edges: [0] },
    { counts: [-1], bin_edges: [0, 1] },
    { counts: [0.5], bin_edges: [0, 1] },
    { counts: [1], bin_edges: [1, 0] },
    { counts: [1], bin_edges: [0, Infinity] },
    {
      counts: Array(65).fill(1),
      bin_edges: Array.from({ length: 66 }, (_, i) => i),
    },
  ]) {
    assert.deepEqual(rowNnzBins(invalid), []);
  }
  assert.equal(
    rowNnzBins({
      counts: Array(64).fill(1),
      bin_edges: Array.from({ length: 65 }, (_, i) => i),
    }).length,
    64,
  );
});
