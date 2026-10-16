export type Entity = Record<string, any>;
export { activeStatuses } from "./models/jobs.js";
export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}
const ROOT = "/api/v1";
let token = sessionStorage.getItem("emsolver.token.v1") || "";
export function setToken(value: string) {
  token = value;
  if (value) sessionStorage.setItem("emsolver.token.v1", value);
  else sessionStorage.removeItem("emsolver.token.v1");
}
export function getToken() {
  return token;
}
export async function api<T = Entity>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const headers = new Headers(init.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body && !(init.body instanceof FormData))
    headers.set("Content-Type", "application/json");
  const response = await fetch(`${ROOT}${path}`, { ...init, headers });
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const data = await response.json();
      message =
        typeof data.detail === "string"
          ? data.detail
          : JSON.stringify(data.detail || data.error || data);
    } catch {
      /* Keep HTTP status when response is not JSON. */
    }
    throw new ApiError(message, response.status);
  }
  return response.json();
}
export function post<T = Entity>(path: string, data: unknown = {}) {
  return api<T>(path, { method: "POST", body: JSON.stringify(data) });
}
export async function download(
  path: string,
  filename: string,
  init: RequestInit = {},
) {
  const headers = new Headers(init.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body) headers.set("Content-Type", "application/json");
  const response = await fetch(
    path.startsWith("/api/") ? path : `${ROOT}${path}`,
    { ...init, headers },
  );
  if (!response.ok) throw new Error(`下载失败：${response.status}`);
  const url = URL.createObjectURL(await response.blob());
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
export function defaultConfig(matrixId = ""): Entity {
  return {
    schema_version: "1.0",
    problem: {
      type: "standard_hermitian",
      matrix_id: matrixId,
      storage: "full",
      scalar: "auto",
      hermitian_rtol: 1e-12,
      reordering: "none",
      nullspace: {
        basis_indices: [],
        interpretation: "unspecified",
        source: "",
      },
    },
    target: {
      mode: "nearest_k",
      k: 12,
      sigma: 0,
      interval: null,
      sort: "distance_then_value",
      cluster_policy: "fixed_k",
    },
    solver: {
      backend: "scipy",
      method: "arpack",
      transform: "shift_invert",
      subspace_max: 60,
      max_outer_iterations: 2000,
      random_seed: 20261016,
      fallback_profiles: [],
      block: {
        oversampling: 4,
        residual_check_interval: 1,
        max_restarts: 2,
        stagnation_checks: 20,
        checkpoint_interval: 0,
        checkpoint_kind: "subspace",
      },
      adaptive_shift: {
        enabled: true,
        max_attempts: 4,
        policy: "observed_ritz_gap_v2",
      },
      linear: {
        precision: "float64",
        method: "sparse_lu",
        preconditioner: "auto_budgeted",
        rtol_initial: 1e-6,
        max_iterations: 1000,
        adaptive_accuracy: {
          enabled: false,
          rtol_min: 1e-10,
          rtol_max: 1e-2,
          forcing_factor: 0.1,
        },
      },
    },
    validation: {
      residual_definition: "scaled_inf_v1",
      residual_scaled_max: 1e-8,
      absolute_residual_max: null,
      orthogonality_max: 1e-6,
      completeness_required: true,
    },
    resources: {
      memory_limit_gib: 16,
      threads: 1,
      mpi_ranks: 1,
      device: "cpu",
      walltime_seconds: 300,
      disk_limit_gib: 2,
    },
    observability: { metrics_interval_ms: 1000, redact_sensitive_fields: true },
    output: { eigenvectors: true, format: "numpy", atomic_write: true },
  };
}
export function number(value: unknown, digits = 3) {
  return typeof value === "number" && Number.isFinite(value)
    ? Math.abs(value) > 0 && Math.abs(value) < 0.001
      ? value.toExponential(2)
      : value.toLocaleString("en-US", { maximumFractionDigits: digits })
    : "—";
}
