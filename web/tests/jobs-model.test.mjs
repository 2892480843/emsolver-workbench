import test from "node:test";
import assert from "node:assert/strict";
import {
  mergeEventWindow,
  applyJobPage,
  jobPagePath,
  readDetailCycle,
  emptyDetail,
  listPollDelay,
} from "../.test-build/models/jobs.js";

const job = (id, status = "RUNNING") => ({ id, status, project_id: "p1" });
const event = (seq) => ({ seq, event: `event-${seq}` });
const signal = () => new AbortController().signal;

test("event window sorts, deduplicates and retains the newest 1000 sequences", () => {
  const current = Array.from({ length: 900 }, (_, i) => event(i + 1));
  const incoming = Array.from({ length: 400 }, (_, i) =>
    event(i + 800),
  ).reverse();
  const merged = mergeEventWindow(current, incoming);
  assert.equal(merged.items.length, 1000);
  assert.equal(merged.items[0].seq, 200);
  assert.equal(merged.items.at(-1).seq, 1199);
  assert.equal(merged.truncated, true);
  assert.equal(new Set(merged.items.map((e) => e.seq)).size, 1000);
});

test("server filter path encodes project and cursor, while ALL omits status", () => {
  const path = jobPagePath("p & 1", "FAILED", "opaque+/=");
  const params = new URL(path, "http://localhost").searchParams;
  assert.equal(params.get("status"), "FAILED");
  assert.equal(params.get("project_id"), "p & 1");
  assert.equal(params.get("cursor"), "opaque+/=");
  assert.equal(params.get("limit"), "50");
  assert.equal(
    new URL(jobPagePath("", "ALL"), "http://localhost").searchParams.has(
      "status",
    ),
    false,
  );
});

test("refreshes first page, preserves loaded history and removes filter mismatches", () => {
  const initial = applyJobPage(
    null,
    { items: [job("a"), job("b")], next_cursor: "first", total: 5 },
    "refresh",
    "p1",
    "RUNNING",
  );
  const loaded = applyJobPage(
    initial,
    { items: [job("c"), job("d")], next_cursor: "second", total: 5 },
    "append",
    "p1",
    "RUNNING",
  );
  const refreshed = applyJobPage(
    loaded,
    { items: [job("b"), job("c")], next_cursor: "newfirst", total: 4 },
    "refresh",
    "p1",
    "RUNNING",
  );
  assert.deepEqual(
    refreshed.items.map((j) => j.id),
    ["b", "c", "d"],
  );
  assert.equal(refreshed.next_cursor, "second");
  assert.equal(refreshed.total, 4);
});

test("event catch-up reads at most five pages per cycle and schedules a short continuation", async () => {
  const paths = [];
  const request = async (path) => {
    paths.push(path);
    if (!path.includes("/events/")) return job("j1");
    const after = Number(
      new URL(path, "http://localhost").searchParams.get("after"),
    );
    return { items: [event(after + 1)], next_after: after + 1, has_more: true };
  };
  const next = await readDetailCycle(
    "j1",
    emptyDetail("j1"),
    request,
    signal(),
  );
  assert.equal(paths.filter((p) => p.includes("/events/")).length, 5);
  assert.equal(next.state.after, 5);
  assert.equal(next.state.events.length, 5);
  assert.equal(next.delay, 150);
});

test("terminal job drains all events and reads numerical result before stopping", async () => {
  const paths = [];
  const result = { eigenvalues: [1.5], residuals: [1e-12] };
  const request = async (path) => {
    paths.push(path);
    if (path.endsWith("/results")) return result;
    if (path.includes("/events/")) {
      const after = Number(
        new URL(path, "http://localhost").searchParams.get("after"),
      );
      return {
        items: [event(after + 1)],
        next_after: after + 1,
        has_more: after === 0,
      };
    }
    return job("j1", "SUCCEEDED");
  };
  const next = await readDetailCycle(
    "j1",
    emptyDetail("j1"),
    request,
    signal(),
  );
  assert.equal(next.delay, null);
  assert.deepEqual(next.state.result, result);
  assert.equal(next.state.after, 2);
  assert.equal(paths.at(-1), "/jobs/j1/results");
});

test("failed/cancelled/timeout 404 or 409 is a settled result, successful job errors retry", async () => {
  for (const status of [
    "FAILED",
    "CANCELLED",
    "TIMED_OUT",
    "PARTIAL",
    "SUCCEEDED",
  ]) {
    for (const httpStatus of [404, 409]) {
      const request = async (path) => {
        if (path.endsWith("/results"))
          throw Object.assign(new Error("not ready"), { status: httpStatus });
        return path.includes("/events/")
          ? { items: [], next_after: 0, has_more: false }
          : job("j1", status);
      };
      if (["SUCCEEDED", "PARTIAL"].includes(status)) {
        await assert.rejects(
          readDetailCycle("j1", emptyDetail("j1"), request, signal()),
          /not ready/,
        );
      } else {
        assert.equal(
          (await readDetailCycle("j1", emptyDetail("j1"), request, signal()))
            .delay,
          null,
        );
      }
    }
  }
});

test("switch cancellation reaches every fetch and cannot publish a completed old cycle", async () => {
  const controller = new AbortController();
  const request = async (_path, init) => {
    assert.equal(init.signal, controller.signal);
    controller.abort();
    return job("j1");
  };
  await assert.rejects(
    readDetailCycle("j1", emptyDetail("j1"), request, controller.signal),
    { name: "AbortError" },
  );
});

test("idle list polling slows down when workspace has no active jobs", () => {
  assert.equal(listPollDelay(0), 15000);
  assert.equal(listPollDelay(1), 3500);
});

test("first-page refresh wins over stale overlapping history and append updates without duplication", () => {
  const first = applyJobPage(
    null,
    { items: [job("a")], next_cursor: "one", total: 3 },
    "refresh",
    "p1",
    "ALL",
  );
  const loaded = applyJobPage(
    first,
    { items: [job("b"), job("c")], next_cursor: null, total: 3 },
    "append",
    "p1",
    "ALL",
  );
  const updated = applyJobPage(
    loaded,
    { items: [job("b", "SUCCEEDED")], next_cursor: "new", total: 3 },
    "refresh",
    "p1",
    "ALL",
  );
  assert.equal(updated.items.find((j) => j.id === "b").status, "SUCCEEDED");
  const appended = applyJobPage(
    updated,
    { items: [job("c", "FAILED")], next_cursor: null, total: 3 },
    "append",
    "p1",
    "ALL",
  );
  assert.equal(appended.items.length, 2);
  assert.equal(appended.items.find((j) => j.id === "c").status, "FAILED");
});

test("incremental cycles resume after the last sequence and a replay never creates duplicate rows", async () => {
  const prior = {
    ...emptyDetail("j1"),
    after: 1000,
    events: Array.from({ length: 1000 }, (_, i) => event(i + 1)),
  };
  const paths = [];
  const request = async (path) => {
    paths.push(path);
    return path.includes("/events/")
      ? { items: [event(1000), event(1001)], next_after: 1001, has_more: false }
      : job("j1", "VERIFYING");
  };
  const next = await readDetailCycle("j1", prior, request, signal());
  assert.equal(
    paths.find((p) => p.includes("/events/")),
    "/jobs/j1/events/page?after=1000&limit=200",
  );
  assert.equal(next.state.events.length, 1000);
  assert.equal(next.state.events[0].seq, 2);
  assert.equal(next.state.after, 1001);
  assert.equal(next.delay, 2500);
  assert.equal(next.state.truncated, true);
});

test("a stalled event cursor and unexpected failed-result errors surface instead of silently stopping", async () => {
  const stalled = async (path) =>
    path.includes("/events/")
      ? { items: [], next_after: 0, has_more: true }
      : job("j1");
  await assert.rejects(
    readDetailCycle("j1", emptyDetail("j1"), stalled, signal()),
    /游标未前进/,
  );
  const failed = async (path) => {
    if (path.endsWith("/results"))
      throw Object.assign(new Error("server error"), { status: 500 });
    return path.includes("/events/")
      ? { items: [], next_after: 0, has_more: false }
      : job("j1", "FAILED");
  };
  await assert.rejects(
    readDetailCycle("j1", emptyDetail("j1"), failed, signal()),
    /server error/,
  );
});

test("terminal event drain observes events published with the terminal job state before stopping", async () => {
  let terminalRead = false;
  const request = async (path) => {
    if (path.endsWith("/results")) return { eigenvalues: [1] };
    if (path.includes("/events/"))
      return {
        items: terminalRead ? [event(1)] : [],
        next_after: terminalRead ? 1 : 0,
        has_more: false,
      };
    await Promise.resolve();
    terminalRead = true;
    return job("j1", "SUCCEEDED");
  };
  const next = await readDetailCycle(
    "j1",
    emptyDetail("j1"),
    request,
    signal(),
  );
  assert.equal(next.delay, null);
  assert.deepEqual(
    next.state.events.map((e) => e.seq),
    [1],
  );
});
