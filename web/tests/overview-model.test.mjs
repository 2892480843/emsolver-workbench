import test from "node:test";
import assert from "node:assert/strict";
import {
  buildStatusDistribution,
  buildRecentActivity,
  statusFilterFor,
} from "../.test-build/models/overview.js";

const summary = (
  counts,
  jobs = Object.values(counts).reduce((a, b) => a + b, 0),
) => ({
  projects: 1,
  matrices: 2,
  jobs,
  active_jobs: 0,
  certified_jobs: 0,
  job_status_counts: counts,
});

test("unrecognized chart categories cannot issue unsupported API filter values", () => {
  assert.equal(statusFilterFor("PARTIAL"), "PARTIAL");
  assert.equal(statusFilterFor("NEW_STATUS"), "ALL");
  assert.equal(statusFilterFor("CREATED"), "ALL");
});

test("missing summary is unavailable, while a confirmed zero-job summary is empty", () => {
  assert.equal(buildStatusDistribution(null).kind, "unavailable");
  const empty = buildStatusDistribution(summary({}));
  assert.equal(empty.kind, "empty");
  assert.equal(empty.countedTotal, 0);
  assert.equal(empty.expectedTotal, 0);
});

test("status counts come from the summary and retain unfamiliar server statuses", () => {
  const result = buildStatusDistribution(
    summary({ SUCCEEDED: 81, RUNNING: 3, NEW_STATUS: 7 }),
  );
  assert.equal(result.kind, "ready");
  assert.equal(result.countedTotal, 91);
  assert.deepEqual(
    result.rows.map(({ status, count }) => [status, count]),
    [
      ["RUNNING", 3],
      ["SUCCEEDED", 81],
      ["NEW_STATUS", 7],
    ],
  );
});

test("inconsistent totals and invalid counts remain explicit rather than producing a rate", () => {
  const result = buildStatusDistribution(
    summary({ RUNNING: 4, FAILED: -1, PARTIAL: 1.5 }, 12),
  );
  assert.equal(result.kind, "incomplete");
  assert.deepEqual(result.rows, [{ status: "RUNNING", count: 4 }]);
  assert.equal(result.countedTotal, 4);
  assert.equal(result.expectedTotal, 12);
  assert.equal(buildStatusDistribution(summary({}, 8)).kind, "incomplete");
});

test("activity sorts valid creation instants newest first without mutating the loaded page", () => {
  const jobs = [
    { id: "invalid", created_at: "not-a-date" },
    { id: "old", created_at: "2026-10-16T09:00:00+08:00" },
    { id: "new", created_at: "2026-10-16T02:00:00Z" },
    { id: "equal", created_at: "2026-10-16T10:00:00+08:00" },
    { id: "absent" },
  ];
  const originalOrder = jobs.map(({ id }) => id);
  const result = buildRecentActivity(jobs, 5, false, 5);
  assert.deepEqual(
    result.items.map(({ job }) => job.id),
    ["new", "equal", "old", "invalid", "absent"],
  );
  assert.deepEqual(
    jobs.map(({ id }) => id),
    originalOrder,
  );
  assert.equal(result.items.at(-1).createdAt, null);
  assert.equal(result.scope, "all");
});

test("activity reports loaded-page scope until the full result set is known", () => {
  const jobs = Array.from({ length: 50 }, (_, index) => ({
    id: `j${index}`,
    created_at: "2026-10-16T02:00:00Z",
  }));
  const paged = buildRecentActivity(jobs, 90, true, 4);
  assert.equal(paged.scope, "loaded");
  assert.equal(paged.loadedCount, 50);
  assert.equal(paged.totalCount, 90);
  assert.equal(paged.items.length, 4);
  assert.equal(buildRecentActivity(jobs, 50, true).scope, "loaded");
  assert.equal(buildRecentActivity(jobs, undefined, false).scope, "loaded");
  assert.equal(buildRecentActivity(jobs, 49, false).totalCount, null);
});
