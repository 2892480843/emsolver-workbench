export type ClusterStatus = {
  enabled: boolean;
  execution_mode: "local" | "cluster";
  persistence_backend: "sqlite" | "postgresql";
};

export type ClusterWorker = {
  id: string;
  name: string;
  project_ids: string[];
  resources: { threads: number; memory_gib: number; disk_gib: number };
  backends: string[];
  revoked_at: number | null;
  last_seen_at: number | null;
  active_lease?: { job_id: string; attempt: number; expires_at: number } | null;
};

export type WorkerDraft = {
  name: string;
  projectIds: string[];
  threads: string;
  memory: string;
  disk: string;
};

export function workerEnrollment(draft: WorkerDraft, availableIds: string[]) {
  const name = draft.name.trim();
  if (!name || name.length > 128 || /[\u0000-\u001f\u007f-\u009f]/.test(name))
    throw new Error("请填写不超过128个字符的节点名称，不包含控制字符。");
  const available = new Set(availableIds);
  if (
    !draft.projectIds.length ||
    new Set(draft.projectIds).size !== draft.projectIds.length ||
    draft.projectIds.some((id) => id === "*" || !available.has(id))
  )
    throw new Error("请明确选择至少一个可用项目；此界面不授予全部项目权限。");
  const threads = Number(draft.threads);
  const memory = Number(draft.memory);
  const disk = Number(draft.disk);
  if (!Number.isInteger(threads) || threads < 1 || threads > 256)
    throw new Error("节点线程预算必须是1至256之间的整数。");
  if (!Number.isFinite(memory) || memory <= 0 || memory > 1024)
    throw new Error("节点内存预算必须大于0且不超过1024 GiB。");
  if (!Number.isFinite(disk) || disk <= 0 || disk > 4096)
    throw new Error("节点磁盘预算必须大于0且不超过4096 GiB。");
  return {
    name,
    project_ids: [...draft.projectIds],
    resources: { threads, memory_gib: memory, disk_gib: disk },
    backends: ["scipy"],
  };
}

type ConnectionRecord = {
  revoked_at?: number | null;
  last_seen_at?: number | null;
  active_lease?: { expires_at: number } | null;
};

export function workerConnection(
  worker: ConnectionRecord,
  now = Date.now() / 1000,
) {
  if (worker.revoked_at != null) return { label: "已撤销", kind: "failed" };
  if (worker.active_lease)
    return worker.active_lease.expires_at > now
      ? { label: "执行中", kind: "running" }
      : { label: "租约已过期", kind: "partial" };
  if (worker.last_seen_at == null) return { label: "尚未连接", kind: "queued" };
  return worker.last_seen_at <= now && now - worker.last_seen_at <= 30
    ? { label: "近期有响应", kind: "ready" }
    : { label: "近期无响应", kind: "partial" };
}

export function workerTime(value: number | null | undefined) {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0)
    return "尚未报告";
  const date = new Date(value * 1000);
  return Number.isFinite(date.getTime())
    ? date.toLocaleString("zh-CN", { hour12: false })
    : "尚未报告";
}
