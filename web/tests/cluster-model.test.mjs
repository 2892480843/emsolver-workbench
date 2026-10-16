import test from "node:test";
import assert from "node:assert/strict";
import {
  workerEnrollment,
  workerConnection,
  workerTime,
} from "../.test-build/models/cluster.js";

const draft = {
  name: " node-a ",
  projectIds: ["p1"],
  threads: "4",
  memory: "8",
  disk: "10",
};

test("worker enrollment sends only explicit projects and reviewed capacity fields", () => {
  assert.deepEqual(
    workerEnrollment({ ...draft, token: "never-send" }, ["p1"]),
    {
      name: "node-a",
      project_ids: ["p1"],
      resources: { threads: 4, memory_gib: 8, disk_gib: 10 },
      backends: ["scipy"],
    },
  );
});

test("worker enrollment rejects empty, wildcard, duplicate and unavailable project scopes", () => {
  for (const projectIds of [[], ["*"], ["p1", "*"], ["p1", "p1"], ["p2"]]) {
    assert.throws(() => workerEnrollment({ ...draft, projectIds }, ["p1"]));
  }
});

test("worker enrollment rejects invalid names and nonfinite or nonpositive budgets", () => {
  for (const patch of [
    { name: " " },
    { name: "a\nb" },
    { threads: "0" },
    { threads: "1.5" },
    { threads: "257" },
    { memory: "Infinity" },
    { memory: "NaN" },
    { disk: "-2" },
    { memory: "" },
    { disk: "4097" },
  ]) {
    assert.throws(() => workerEnrollment({ ...draft, ...patch }, ["p1"]));
  }
});

test("revoked credentials remain revoked despite a recent heartbeat or active row", () => {
  assert.equal(
    workerConnection(
      {
        revoked_at: 900,
        last_seen_at: 1000,
        active_lease: { expires_at: 1100 },
      },
      1000,
    ).label,
    "已撤销",
  );
});

test("worker status distinguishes valid execution, expired lease and stale observation", () => {
  assert.equal(
    workerConnection({ active_lease: { expires_at: 1001 } }, 1000).label,
    "执行中",
  );
  assert.equal(
    workerConnection({ active_lease: { expires_at: 1000 } }, 1000).label,
    "租约已过期",
  );
  assert.equal(
    workerConnection({ last_seen_at: 990 }, 1000).label,
    "近期有响应",
  );
  assert.equal(
    workerConnection({ last_seen_at: 500 }, 1000).label,
    "近期无响应",
  );
  assert.equal(
    workerConnection({ last_seen_at: null }, 1000).label,
    "尚未连接",
  );
});

test("worker time does not invent dates for missing or invalid observations", () => {
  for (const value of [null, undefined, NaN, Infinity, -1]) {
    assert.equal(workerTime(value), "尚未报告");
  }
  assert.notEqual(workerTime(1000), "尚未报告");
});
