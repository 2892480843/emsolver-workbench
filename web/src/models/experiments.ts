import type { Entity } from "../api";
import { activeStatuses } from "./jobs.js";

export type ConfigurationChange = {
  path: string;
  before: unknown;
  after: unknown;
};
export type ExperimentParameters = {
  k: number;
  sigma: number;
  threads: number;
  memory_limit_gib: number;
  adaptive_shift: boolean;
};
export type ExperimentBudget = {
  max_runs: number;
  max_total_walltime_seconds: number;
  max_total_disk_gib: number;
  total_runs?: number;
  total_walltime_seconds?: number;
  total_disk_gib?: number;
  peak_memory_limit_gib?: number;
};
export type ExperimentDefinition = {
  project_id: string;
  matrix_ids: string[];
  base_config: Entity;
  k_values: number[];
  sigma_values: number[];
  threads_values: number[];
  memory_limit_gib_values: number[];
  adaptive_shift_values: boolean[];
  repeats: number;
  budget: ExperimentBudget;
};
export type ExperimentCell = {
  id: string;
  matrix_id: string;
  matrix_hash: string;
  plan_id: string;
  plan_hash: string;
  config: Entity;
  parameters: ExperimentParameters;
  configuration_diff: ConfigurationChange[];
};
export type ExperimentPlan = {
  id: string;
  schema_version: string;
  project_id: string;
  definition: ExperimentDefinition;
  hash: string;
  cells: ExperimentCell[];
  budget: ExperimentBudget;
  warnings: unknown[];
  created_at: string;
};
export type ExperimentGroup = {
  cell_id: string;
  matrix_id: string;
  parameters: Partial<ExperimentParameters>;
  job_ids: string[];
  summary: Entity;
  configuration_diff: ConfigurationChange[];
};
export type ExperimentBatch = {
  id: string;
  project_id: string;
  created_at: string;
  status?: string;
  experiment_plan_id?: string;
  experiment_hash?: string;
  definition?: ExperimentDefinition;
  cells?: ExperimentCell[];
  budget?: ExperimentBudget;
  jobs?: string[];
  matrix_ids?: string[];
  repeats?: number;
  summary?: Entity;
  groups?: ExperimentGroup[];
  runs?: Entity[];
};
export type BenchmarkPage = {
  items: ExperimentBatch[];
  next_cursor: string | null;
  total: number;
};

const decimal = /^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$/;
export function parseNumberAxis(
  value: string,
  label: string,
  options: {
    integer?: boolean;
    min?: number;
    max?: number;
    maxLength?: number;
  } = {},
): number[] {
  const tokens = value.split(",");
  if (tokens.length > (options.maxLength ?? 10))
    throw new Error(`${label} 最多填写 ${options.maxLength ?? 10} 个值`);
  const result: number[] = [];
  const seen = new Set<number>();
  for (const token of tokens) {
    const trimmed = token.trim();
    const number = Number(trimmed);
    if (!decimal.test(trimmed) || !Number.isFinite(number))
      throw new Error(`${label} 必须是逗号分隔的有限数值，不能留空`);
    if (options.integer && !Number.isSafeInteger(number))
      throw new Error(`${label} 必须填写整数`);
    if (options.min !== undefined && number < options.min)
      throw new Error(`${label} 不得小于 ${options.min}`);
    if (options.max !== undefined && number > options.max)
      throw new Error(`${label} 不得大于 ${options.max}`);
    if (!seen.has(number)) {
      seen.add(number);
      result.push(number === 0 ? 0 : number);
    }
  }
  return result;
}

const isObject = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === "object" && !Array.isArray(value);

export function configurationDiff(
  before: Record<string, unknown>,
  after: Record<string, unknown>,
): ConfigurationChange[] {
  const changes: ConfigurationChange[] = [];
  const visit = (a: unknown, b: unknown, path: string) => {
    if (isObject(a) || isObject(b)) {
      const left = isObject(a) ? a : {};
      const right = isObject(b) ? b : {};
      const keys = [
        ...new Set([...Object.keys(left), ...Object.keys(right)]),
      ].sort();
      for (const key of keys)
        visit(left[key], right[key], path ? `${path}.${key}` : key);
      return;
    }
    if (JSON.stringify(a) !== JSON.stringify(b))
      changes.push({ path, before: a ?? null, after: b ?? null });
  };
  visit(before, after, "");
  return changes;
}

export function benchmarkPagePath(projectId: string, cursor?: string | null) {
  const params = new URLSearchParams({ limit: "20" });
  if (projectId) params.set("project_id", projectId);
  if (cursor) params.set("cursor", cursor);
  return `/benchmarks/page?${params}`;
}

const count = (value: unknown) =>
  typeof value === "number" && Number.isFinite(value) ? Math.max(0, value) : 0;
export function experimentSummary(summary: Entity = {}) {
  const total = count(summary.total);
  return {
    total,
    certified: count(summary.certified),
    partial: count(summary.partial),
    failed: count(summary.failed),
    active:
      summary.active === undefined
        ? Math.max(0, total - count(summary.terminal))
        : count(summary.active),
    recordedTimes: count(summary.timing_denominator),
    certifiedTimes: count(summary.certified_timing_denominator),
  };
}

export function batchNeedsPolling(batch: ExperimentBatch) {
  return (
    activeStatuses.has(batch.status || "") ||
    Boolean(batch.runs?.some((run) => activeStatuses.has(run.status))) ||
    experimentSummary(batch.summary).active > 0
  );
}

type Request = <T>(path: string, init: RequestInit) => Promise<T>;
export async function readBenchmarkDetail(
  id: string,
  request: Request,
  signal: AbortSignal,
) {
  signal.throwIfAborted();
  const batch = await request<ExperimentBatch>(
    `/benchmarks/${encodeURIComponent(id)}`,
    { signal },
  );
  signal.throwIfAborted();
  return { batch, delay: batchNeedsPolling(batch) ? 2500 : null };
}
