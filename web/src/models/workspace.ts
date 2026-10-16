import type { Entity } from "../api";
import { parseNumberAxis } from "./experiments.js";

export function indexMatricesById(matrices: readonly Entity[]) {
  const byId = new Map<string, Entity>();
  for (const matrix of matrices) {
    // Match the original Array.find lookup if a response contains duplicate IDs.
    if (!byId.has(matrix.id)) byId.set(matrix.id, matrix);
  }
  return byId;
}

export function parseNullspaceIndices(value: string): number[] {
  return value.trim()
    ? parseNumberAxis(value, "零空间坐标", {
        integer: true,
        min: 0,
        maxLength: 512,
      })
    : [];
}

export function safeSourceUrl(value: unknown): string | undefined {
  if (typeof value !== "string" || !value.trim()) return undefined;
  try {
    const url = new URL(value.trim());
    return ["http:", "https:"].includes(url.protocol) ? url.href : undefined;
  } catch {
    return undefined;
  }
}

export function matrixMetadataPayload(draft: Entity) {
  const text = (key: string) => {
    const value =
      typeof draft[key] === "string"
        ? draft[key].trim()
        : key === "seed" &&
            typeof draft[key] === "number" &&
            Number.isFinite(draft[key])
          ? String(draft[key])
          : "";
    if (value.length > 2000) throw new Error("元数据字段不能超过 2000 字");
    return value || null;
  };
  const source = text("source_url");
  if (source && !safeSourceUrl(source))
    throw new Error("来源链接必须是完整的 HTTP 或 HTTPS 地址");
  const license = text("license");
  if (draft.public === true && !license)
    throw new Error("公开声明需要填写数据许可");
  const mapping = text("frequency_mapping");
  if (mapping && mapping !== "lambda=omega_squared")
    throw new Error("频率映射不受支持");
  return {
    source_url: source,
    license,
    units: text("units"),
    frequency_mapping: mapping,
    description: text("description"),
    purpose: text("purpose"),
    generator_script: text("generator_script"),
    seed: text("seed"),
    classification: text("classification"),
    public_scope: text("public_scope"),
    public: draft.public === true,
  };
}

export type ProjectDraft = {
  name: string;
  description: string;
  classification: string;
  members: { subject: string; role: string }[];
  storage: string;
  queued: string;
  priority: string;
  retention: string;
};
export function projectDraft(project: Entity): ProjectDraft {
  return {
    name: project.name || "",
    description: project.description || "",
    classification: project.classification || "internal",
    members: (project.members || []).map((member: Entity) => ({
      subject: member.subject,
      role: member.role,
    })),
    storage: String(project.policy?.storage_limit_gib ?? 4096),
    queued: String(project.policy?.max_queued_jobs ?? 100),
    priority: String(project.policy?.priority ?? 0),
    retention: String(project.policy?.retention_days ?? 30),
  };
}
export function projectPatch(draft: ProjectDraft) {
  const number = (
    value: string,
    label: string,
    integer = false,
    min = 0,
    max = Infinity,
  ) => {
    if (!/^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$/.test(value.trim()))
      throw new Error(`${label}必须是数值`);
    const parsed = Number(value);
    if (
      !Number.isFinite(parsed) ||
      parsed < min ||
      parsed > max ||
      (integer && !Number.isSafeInteger(parsed))
    )
      throw new Error(`${label}超出允许范围`);
    return parsed;
  };
  const name = draft.name.trim();
  if (!name || name.length > 200) throw new Error("项目名称需为 1–200 字");
  if (!["public", "internal", "confidential"].includes(draft.classification))
    throw new Error("数据分级无效");
  const members = draft.members.map((member) => ({
    subject: member.subject.trim(),
    role: member.role,
  }));
  if (
    members.some(
      (member) =>
        !member.subject ||
        !["viewer", "analyst", "admin"].includes(member.role),
    )
  )
    throw new Error("成员身份或角色无效");
  if (new Set(members.map((member) => member.subject)).size !== members.length)
    throw new Error("成员身份不能重复");
  return {
    name,
    description: draft.description.trim(),
    classification: draft.classification,
    members,
    policy: {
      storage_limit_gib: number(
        draft.storage,
        "存储配额",
        false,
        Number.MIN_VALUE,
        65536,
      ),
      max_queued_jobs: number(draft.queued, "排队配额", true, 1, 100),
      priority: number(draft.priority, "优先级", true, -10, 10),
      retention_days: number(draft.retention, "保留天数", true, 1, 36500),
    },
  };
}

export function projectSearchPath(query: string) {
  const q = query.trim();
  return `/projects${q ? `?${new URLSearchParams({ q })}` : ""}`;
}

export function canOperate(identity: Entity | null) {
  return identity?.global_admin === true || identity?.operations_admin === true;
}

export function operationsPolicyPatch(projectId: string, draft: Entity) {
  if (!/^[A-Za-z0-9_-]{1,200}$/.test(projectId))
    throw new Error("项目编号无效");
  // Reuse the same quota bounds as project administration, then send only the
  // four operational fields. Business metadata and membership never cross here.
  const policy = projectPatch({
    name: "policy",
    description: "",
    classification: "internal",
    members: [],
    storage: String(draft.storage ?? ""),
    queued: String(draft.queued ?? ""),
    priority: String(draft.priority ?? ""),
    retention: String(draft.retention ?? ""),
  }).policy;
  return { project_id: projectId, policy };
}
export function projectRole(
  project: Entity | undefined,
  identity: Entity | null,
): string | null {
  if (!identity) return null;
  if (identity.global_admin) return "admin";
  return (
    project?.members?.find(
      (member: Entity) => member.subject === identity.subject,
    )?.role || null
  );
}
export const importIsTerminal = (status?: string) =>
  ["SUCCEEDED", "FAILED", "CANCELLED"].includes(status || "");
type Request = <T>(path: string, init: RequestInit) => Promise<T>;
export async function readImportTask(
  id: string,
  request: Request,
  signal: AbortSignal,
) {
  signal.throwIfAborted();
  const task = await request<Entity>(`/imports/${encodeURIComponent(id)}`, {
    signal,
  });
  signal.throwIfAborted();
  return { task, delay: importIsTerminal(task.status) ? null : 1000 };
}
export async function readModeComparison(
  referenceId: string,
  candidateId: string,
  request: Request,
  signal: AbortSignal,
) {
  signal.throwIfAborted();
  const result = await request<Entity>("/comparisons/modes", {
    method: "POST",
    body: JSON.stringify({
      reference_job_id: referenceId,
      candidate_job_id: candidateId,
    }),
    signal,
  });
  signal.throwIfAborted();
  return result;
}
function isCpuDoubleJD(config: Entity): boolean {
  return (
    config.solver?.backend === "scipy" &&
    config.solver?.method === "jacobi_davidson" &&
    (config.resources?.device || "cpu") === "cpu" &&
    (config.solver?.linear?.precision || "float64") === "float64"
  );
}

export function isIluPreconditioner(value: unknown): boolean {
  return value === "ilu_budgeted_v1" || value === "ilu_equilibrated_compensated_v2";
}

export function correctionPreconditionerError(
  config: Entity,
  capabilities: Entity,
): string {
  const preconditioner = config.solver?.linear?.preconditioner;
  if (!isIluPreconditioner(preconditioner)) return "";
  if (!isCpuDoubleJD(config))
    return "ILU（不完全 LU 分解）仅支持 CPU 双精度 Jacobi–Davidson；请切换设备、精度或预条件选项。";
  if (!capabilities.jacobi_davidson?.preconditioners?.includes(preconditioner))
    return "当前后端未声明支持 ILU 固定策略；请选择已支持的预条件选项。";
  return "";
}

export function slepcFactorizationError(config: Entity, capabilities: Entity): string {
  if (config.solver?.linear?.preconditioner !== "mumps_ooc_symmetric_v1") return "";
  if (config.solver?.backend !== "slepc" || config.solver?.method !== "krylovschur" ||
      config.problem?.scalar === "complex128" || config.resources?.device !== "cpu" ||
      config.resources?.mpi_ranks !== 1 || config.resources?.threads !== 1 ||
      config.solver?.linear?.precision !== "float64")
    return "磁盘因子方案仅支持 SLEPc 实数双精度、CPU 单进程单线程；实际输入仍须检查。";
  if (!capabilities.slepc?.factor_profiles?.includes("mumps_ooc_symmetric_v1"))
    return "当前后端未验证磁盘因子方案，请选择内存分解或刷新能力。";
  return "";
}

export function withSlepcFactorization(config: Entity, profile: string, capabilities: Entity): Entity {
  if (!["auto_budgeted", "mumps_ooc_symmetric_v1"].includes(profile))
    throw new Error("不支持的因子存储方案。");
  const next = { ...config, solver: { ...config.solver,
    linear: { ...config.solver.linear, preconditioner: profile } } };
  const error = slepcFactorizationError(next, capabilities);
  if (error) throw new Error(error);
  return next;
}

export function withCorrectionPreconditioner(
  config: Entity,
  preconditioner: string,
  capabilities: Entity,
): Entity {
  const next = {
    ...config,
    solver: {
      ...config.solver,
      linear: {
        ...config.solver.linear,
        preconditioner,
        ...(isIluPreconditioner(preconditioner) ? { precision: "float64" } : {}),
      },
    },
  };
  const error = correctionPreconditionerError(next, capabilities);
  if (error) throw new Error(error);
  return next;
}

export function withSolverMethod(
  config: Entity,
  method: string,
  backend = config.solver.backend,
): Entity {
  const iterative = ["block_shift_invert", "jacobi_davidson"].includes(method);
  const jd = method === "jacobi_davidson";
  const old = config.solver;
  return {
    ...config,
    resources: { ...config.resources,
      ...(config.resources?.device !== undefined ? { device: "cpu" } : {}),
      ...(config.resources?.mpi_ranks !== undefined ? { mpi_ranks: 1 } : {}),
      ...(backend === "slepc" ? { threads: 1 } : {}) },
    solver: {
      ...old,
      backend,
      method,
      transform: jd ? "precond" : "shift_invert",
      fallback_profiles:
        method === "block_shift_invert" ? old.fallback_profiles || [] : [],
      linear: {
        ...old.linear,
        method: jd ? "gmres" : "sparse_lu",
        preconditioner: jd
          ? old.method === "jacobi_davidson" &&
            (["none", "jacobi"].includes(old.linear?.preconditioner) ||
              (isIluPreconditioner(old.linear?.preconditioner) && backend === "scipy" && isCpuDoubleJD(config)))
            ? old.linear.preconditioner
            : "none"
          : backend === "slepc" && method === "krylovschur" && old.backend === "slepc" &&
              old.linear?.preconditioner === "mumps_ooc_symmetric_v1"
            ? "mumps_ooc_symmetric_v1" : "auto_budgeted",
        precision: jd ? old.linear?.precision || "float64" : "float64",
        adaptive_accuracy: {
          rtol_min: 1e-10,
          rtol_max: 1e-2,
          forcing_factor: 0.1,
          ...old.linear?.adaptive_accuracy,
          enabled: jd
            ? (old.linear?.adaptive_accuracy?.enabled ?? false)
            : false,
        },
      },
      block: {
        ...old.block,
        checkpoint_kind: method === "block_shift_invert" ? (old.block?.checkpoint_kind || "subspace") : "subspace",
        checkpoint_interval: iterative
          ? (old.block?.checkpoint_interval ?? 0)
          : 0,
      },
    },
  };
}
export function withComputeDevice(config: Entity, device: "cpu" | "gpu"): Entity {
  if (device === "cpu") {
    const resetIlu = isIluPreconditioner(config.solver.linear?.preconditioner) && !isCpuDoubleJD(config);
    return {
      ...config,
      resources: { ...config.resources, device },
      ...(resetIlu ? { solver: { ...config.solver,
        linear: { ...config.solver.linear, preconditioner: "none" } } } : {}),
    };
  }
  const next = withSolverMethod(config, "jacobi_davidson", "scipy");
  return {
    ...next,
    resources: { ...next.resources, device: "gpu", mpi_ranks: 1 },
    solver: {
      ...next.solver,
      linear: {
        ...next.solver.linear,
        precision: "mixed_float32",
        preconditioner: isIluPreconditioner(next.solver.linear.preconditioner) ? "none" : next.solver.linear.preconditioner,
        rtol_initial: Math.max(1e-3, next.solver.linear.rtol_initial || 0),
      },
    },
  };
}
export function canExactResume(job: Entity | null): boolean {
  return canWarmStart(job) && job?.config?.solver?.backend === "scipy" &&
    job.config.solver.method === "block_shift_invert" && job.config.solver.block?.checkpoint_kind === "exact_block" &&
    job.config.solver.block.checkpoint_interval > 0 && !job.config.solver.adaptive_shift?.enabled &&
    !job.config.solver.fallback_profiles?.length;
}
export function canWarmStart(job: Entity | null): boolean {
  return Boolean(
    job &&
    ["SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED", "TIMED_OUT"].includes(
      job.status,
    ) &&
    ["block_shift_invert", "jacobi_davidson"].includes(
      job.config?.solver?.method,
    ) &&
    !["EXPIRED", "MISSING"].includes(job.artifacts_state),
  );
}
export function cleanupItemCount(preview: Entity): number {
  return (
    (preview.candidates?.length || 0) + (preview.retention?.items?.length || 0)
  );
}
export async function recordAdviceEvaluation(
  id: string,
  request: Request,
  signal: AbortSignal,
) {
  signal.throwIfAborted();
  const path = `/advice/effects/${encodeURIComponent(id)}`;
  await request<Entity>(`${path}/evaluate`, { method: "POST", signal });
  signal.throwIfAborted();
  const detail = await request<Entity>(path, { signal });
  signal.throwIfAborted();
  return detail;
}
