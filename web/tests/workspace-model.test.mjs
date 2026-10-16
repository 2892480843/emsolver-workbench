import test from "node:test";
import assert from "node:assert/strict";
import {
  matrixMetadataPayload,
  safeSourceUrl,
  projectDraft,
  projectPatch,
  projectSearchPath,
  readImportTask,
  projectRole,
  parseNullspaceIndices,
  readModeComparison,
  withSolverMethod,
  withComputeDevice,
  withCorrectionPreconditioner,
  correctionPreconditionerError,
  canWarmStart,
  cleanupItemCount,
  recordAdviceEvaluation,
  operationsPolicyPatch,
  canOperate,
  indexMatricesById,
} from "../.test-build/models/workspace.js";

test("matrix lookup preserves the first duplicate, missing entries and input order", () => {
  const first = Object.freeze({ id: "m1", name: "original" });
  const second = Object.freeze({ id: "m2", name: "" });
  const duplicate = Object.freeze({ id: "m1", name: "later" });
  const matrices = Object.freeze([first, second, duplicate]);
  const byId = indexMatricesById(matrices);

  assert.equal(byId.get("m1"), first);
  assert.equal(byId.get("m2"), second);
  assert.equal(byId.get("missing"), undefined);
  assert.deepEqual([...byId.keys()], ["m1", "m2"]);
  assert.deepEqual(matrices, [first, second, duplicate]);
  assert.equal(indexMatricesById([]).size, 0);
});

test("matrix metadata trims editable fields and excludes unknown/private properties", () => {
  assert.deepEqual(
    matrixMetadataPayload({
      source_url: " https://example.org/input ",
      license: " CC0-1.0 ",
      description: " known source ",
      public: true,
      _path: "/secret",
    }),
    {
      source_url: "https://example.org/input",
      license: "CC0-1.0",
      units: null,
      frequency_mapping: null,
      description: "known source",
      public: true,
      purpose: null,
      generator_script: null,
      seed: null,
      classification: null,
      public_scope: null,
    },
  );
});

test("project patches validate finite quotas, preserve role distinctions and encode search text", () => {
  const draft = projectDraft({
    name: " test ",
    members: [{ subject: "reader", role: "viewer" }],
  });
  assert.equal(projectPatch(draft).name, "test");
  assert.deepEqual(projectPatch(draft).members, [
    { subject: "reader", role: "viewer" },
  ]);
  for (const values of [
    { storage: "Infinity" },
    { queued: "101" },
    { priority: "-11" },
    { retention: "0" },
    { queued: "1.5" },
  ]) {
    assert.throws(() => projectPatch({ ...draft, ...values }));
  }
  assert.throws(
    () =>
      projectPatch({
        ...draft,
        members: [
          { subject: "a", role: "admin" },
          { subject: " a ", role: "analyst" },
        ],
      }),
    /重复/,
  );
  const path = new URL(projectSearchPath(" a & 中文 "), "http://localhost");
  assert.equal(path.searchParams.get("q"), "a & 中文");
  assert.equal(projectSearchPath(" "), "/projects");
});

test("import reads poll only unfinished tasks and reject an asynchronously cancelled response", async () => {
  for (const status of [
    "QUEUED",
    "RUNNING",
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
  ]) {
    const controller = new AbortController();
    const next = await readImportTask(
      "id /&",
      async (path, init) => {
        assert.equal(path, "/imports/id%20%2F%26");
        assert.equal(init.signal, controller.signal);
        return { id: "id /&", status };
      },
      controller.signal,
    );
    assert.equal(
      next.delay,
      ["QUEUED", "RUNNING"].includes(status) ? 1000 : null,
    );
  }
  const controller = new AbortController();
  await assert.rejects(
    readImportTask(
      "old",
      async () => {
        await Promise.resolve();
        controller.abort();
        return { status: "SUCCEEDED" };
      },
      controller.signal,
    ),
    { name: "AbortError" },
  );
});

test("public matrix metadata requires a license and only permits supported frequency mappings", () => {
  assert.throws(
    () => matrixMetadataPayload({ public: true, license: " " }),
    /许可/,
  );
  assert.throws(
    () => matrixMetadataPayload({ frequency_mapping: "arbitrary()" }),
    /频率映射/,
  );
  assert.equal(
    matrixMetadataPayload({
      public: false,
      frequency_mapping: "lambda=omega_squared",
    }).frequency_mapping,
    "lambda=omega_squared",
  );
});

test("source URLs reject active protocols and malformed addresses", () => {
  for (const url of [
    "javascript:alert(1)",
    "file:///tmp/private",
    "https://",
    "data:text/plain,test",
  ]) {
    assert.equal(safeSourceUrl(url), undefined);
    assert.throws(() => matrixMetadataPayload({ source_url: url }), /来源链接/);
  }
  assert.equal(
    safeSourceUrl("https://example.org/a?q=1"),
    "https://example.org/a?q=1",
  );
  assert.equal(safeSourceUrl(""), undefined);
});

test("project roles fail closed until identity is known and distinguish analysts from administrators", () => {
  const project = {
    members: [
      { subject: "reader", role: "viewer" },
      { subject: "analyst", role: "analyst" },
    ],
  };
  assert.equal(projectRole(project, null), null);
  assert.equal(projectRole(project, { subject: "other" }), null);
  assert.equal(projectRole(project, { subject: "reader" }), "viewer");
  assert.equal(projectRole(project, { subject: "analyst" }), "analyst");
  assert.equal(
    projectRole(undefined, { subject: "owner", global_admin: true }),
    "admin",
  );
  assert.equal(matrixMetadataPayload({ seed: 123 }).seed, "123");
});

test("declared nullspace coordinates are bounded original nonnegative integer indices", () => {
  assert.deepEqual(parseNullspaceIndices(""), []);
  assert.deepEqual(parseNullspaceIndices(" 0, 2, 2, 17 "), [0, 2, 17]);
  for (const value of [
    "-1",
    "1.5",
    "1,",
    Array.from({ length: 513 }, (_, i) => i).join(","),
  ])
    assert.throws(() => parseNullspaceIndices(value));
});

test("mode matching sends explicitly chosen computed-run IDs and rejects an obsolete response", async () => {
  const signal = new AbortController().signal;
  const result = await readModeComparison(
    "baseline",
    "candidate",
    async (path, init) => {
      assert.equal(path, "/comparisons/modes");
      assert.equal(init.method, "POST");
      assert.equal(init.signal, signal);
      assert.deepEqual(JSON.parse(init.body), {
        reference_job_id: "baseline",
        candidate_job_id: "candidate",
      });
      return { matched_count: 2, passed: true };
    },
    signal,
  );
  assert.equal(result.matched_count, 2);
  const controller = new AbortController();
  await assert.rejects(
    readModeComparison(
      "old",
      "old2",
      async () => {
        controller.abort();
        return {};
      },
      controller.signal,
    ),
    { name: "AbortError" },
  );
});

test("method changes make the linear solver coherent without changing target or hard limits", () => {
  const before = {
    target: { k: 4, sigma: 2 },
    validation: { residual_scaled_max: 1e-9 },
    resources: { memory_limit_gib: 2, walltime_seconds: 10 },
    solver: {
      backend: "scipy",
      method: "block_shift_invert",
      transform: "shift_invert",
      fallback_profiles: ["block_to_arpack_v1"],
      linear: {
        method: "sparse_lu",
        preconditioner: "auto_budgeted",
        adaptive_accuracy: {
          enabled: true,
          rtol_min: 1e-8,
          rtol_max: 1e-3,
          forcing_factor: 0.2,
        },
      },
      block: { checkpoint_interval: 5 },
    },
  };
  const snapshot = structuredClone(before);
  const jd = withSolverMethod(before, "jacobi_davidson");
  assert.equal(jd.solver.transform, "precond");
  assert.equal(jd.solver.linear.method, "gmres");
  assert.equal(jd.solver.linear.preconditioner, "none");
  assert.deepEqual(jd.solver.fallback_profiles, []);
  const arpack = withSolverMethod(jd, "arpack");
  assert.equal(arpack.solver.transform, "shift_invert");
  assert.equal(arpack.solver.linear.method, "sparse_lu");
  assert.equal(arpack.solver.linear.adaptive_accuracy.enabled, false);
  assert.equal(arpack.solver.block.checkpoint_interval, 0);
  for (const key of ["target", "validation", "resources"])
    assert.deepEqual(arpack[key], before[key]);
  assert.deepEqual(before, snapshot);
});

test("warm starts require a finished supported method and intact artifact state", () => {
  const job = {
    status: "CANCELLED",
    config: { solver: { method: "block_shift_invert" } },
  };
  assert.equal(canWarmStart(job), true);
  assert.equal(
    canWarmStart({ ...job, config: { solver: { method: "jacobi_davidson" } } }),
    true,
  );
  for (const state of ["EXPIRED", "MISSING"])
    assert.equal(canWarmStart({ ...job, artifacts_state: state }), false);
  assert.equal(canWarmStart({ ...job, status: "RUNNING" }), false);
  assert.equal(
    canWarmStart({ ...job, config: { solver: { method: "arpack" } } }),
    false,
  );
  assert.equal(canWarmStart(null), false);
});

test("leaving JD disables approximate correction precision while preserving the original contract", () => {
  const before = {
    target: { k: 4, sigma: 2 },
    validation: { residual_scaled_max: 1e-9 },
    resources: { memory_limit_gib: 2, walltime_seconds: 10 },
    solver: {
      backend: "scipy",
      method: "jacobi_davidson",
      linear: { preconditioner: "jacobi", precision: "mixed_float32" },
    },
  };
  const unchanged = withSolverMethod(before, "jacobi_davidson");
  assert.equal(unchanged.solver.linear.preconditioner, "jacobi");
  assert.equal(unchanged.solver.linear.precision, "mixed_float32");
  for (const method of ["arpack", "block_shift_invert"]) {
    const after = withSolverMethod(before, method);
    assert.equal(after.solver.linear.precision, "float64");
    assert.deepEqual(after.target, before.target);
    assert.deepEqual(after.validation, before.validation);
    assert.deepEqual(after.resources, before.resources);
  }
  assert.equal(before.solver.linear.precision, "mixed_float32");
});

function cpuIluConfig() {
  return {
    target: { k: 2, sigma: .15 },
    validation: { residual_scaled_max: 1e-9 },
    resources: { device: "cpu", mpi_ranks: 1, memory_limit_gib: 4, walltime_seconds: 120 },
    solver: {
      backend: "scipy", method: "jacobi_davidson", transform: "precond",
      linear: { method: "gmres", preconditioner: "ilu_budgeted_v1", precision: "float64", rtol_initial: 1e-3 },
    },
  };
}

test("valid CPU double JD preserves ILU and method/device changes reset incompatible choices", () => {
  const before = cpuIluConfig();
  const snapshot = structuredClone(before);
  assert.equal(withSolverMethod(before, "jacobi_davidson").solver.linear.preconditioner, "ilu_budgeted_v1");
  assert.equal(withComputeDevice(before, "cpu").solver.linear.preconditioner, "ilu_budgeted_v1");
  for (const method of ["arpack", "block_shift_invert"]) {
    const after = withSolverMethod(before, method);
    assert.equal(after.solver.linear.preconditioner, "auto_budgeted");
    assert.equal(after.solver.linear.precision, "float64");
    assert.deepEqual(after.target, before.target);
    assert.deepEqual(after.validation, before.validation);
    assert.deepEqual(after.resources, before.resources);
  }
  const gpu = withComputeDevice(before, "gpu");
  assert.equal(gpu.resources.device, "gpu");
  assert.equal(gpu.solver.linear.precision, "mixed_float32");
  assert.equal(gpu.solver.linear.preconditioner, "none");
  const cpu = withComputeDevice(gpu, "cpu");
  assert.equal(cpu.solver.linear.preconditioner, "none");
  assert.deepEqual(cpu.target, before.target);
  assert.deepEqual(cpu.validation, before.validation);
  assert.equal(cpu.resources.memory_limit_gib, before.resources.memory_limit_gib);
  assert.equal(cpu.resources.walltime_seconds, before.resources.walltime_seconds);
  assert.deepEqual(before, snapshot);
});

test("explicit capability-gated ILU selection promotes CPU correction precision without changing hard limits", () => {
  const before = cpuIluConfig();
  before.solver.linear.preconditioner = "jacobi";
  before.solver.linear.precision = "mixed_float32";
  const capabilities = { jacobi_davidson: { preconditioners: ["none", "jacobi", "ilu_budgeted_v1"] } };
  const snapshot = structuredClone(before);
  const next = withCorrectionPreconditioner(before, "ilu_budgeted_v1", capabilities);
  assert.equal(next.solver.linear.preconditioner, "ilu_budgeted_v1");
  assert.equal(next.solver.linear.precision, "float64");
  assert.equal(correctionPreconditionerError(next, capabilities), "");
  assert.deepEqual(next.target, before.target);
  assert.deepEqual(next.validation, before.validation);
  assert.deepEqual(next.resources, before.resources);
  assert.deepEqual(before, snapshot);
  assert.throws(() => withCorrectionPreconditioner(before, "ilu_budgeted_v1", {}), /后端未声明支持/);
  const jacobi = withCorrectionPreconditioner(before, "jacobi", {});
  assert.equal(jacobi.solver.linear.precision, "mixed_float32");
});

test("imported unsupported ILU combinations are visible errors and switching profiles removes them", () => {
  const capabilities = { jacobi_davidson: { preconditioners: ["ilu_budgeted_v1"] } };
  const cpu = cpuIluConfig();
  assert.match(correctionPreconditionerError(cpu, {}), /后端未声明支持/);
  assert.equal(correctionPreconditionerError({ ...cpu, solver: { ...cpu.solver, linear: { preconditioner: "none" } } }, {}), "");
  for (const change of [
    { resources: { ...cpu.resources, device: "gpu" } },
    { solver: { ...cpu.solver, linear: { ...cpu.solver.linear, precision: "mixed_float32" } } },
    { solver: { ...cpu.solver, method: "arpack" } },
    { solver: { ...cpu.solver, backend: "slepc" } },
  ]) {
    const invalid = { ...cpu, ...change };
    assert.match(correctionPreconditionerError(invalid, capabilities), /仅支持 CPU 双精度/);
    assert.equal(withSolverMethod(invalid, "jacobi_davidson").solver.linear.preconditioner, "none");
    assert.equal(withComputeDevice(invalid, "cpu").solver.linear.preconditioner, "none");
  }
  const gpu = { ...cpu, resources: { ...cpu.resources, device: "gpu" } };
  assert.throws(() => withCorrectionPreconditioner(gpu, "ilu_budgeted_v1", capabilities), /仅支持 CPU 双精度/);
});

test("cleanup enables execution for expired job outputs even without temporary file candidates", () => {
  assert.equal(
    cleanupItemCount({
      candidates: [],
      retention: { items: [{ job_id: "j1" }] },
    }),
    1,
  );
  assert.equal(
    cleanupItemCount({
      candidates: ["inbox/old"],
      retention: { items: [{}, {}] },
    }),
    3,
  );
  assert.equal(cleanupItemCount({}), 0);
});

test("recording advice effects uses explicit POST then reads the current detail", async () => {
  const calls = [];
  const signal = new AbortController().signal;
  const value = await recordAdviceEvaluation(
    "effect /1",
    async (path, init) => {
      calls.push([path, init.method || "GET"]);
      assert.equal(init.signal, signal);
      return { evaluation: { comparable: false } };
    },
    signal,
  );
  assert.deepEqual(calls, [
    ["/advice/effects/effect%20%2F1/evaluate", "POST"],
    ["/advice/effects/effect%20%2F1", "GET"],
  ]);
  assert.equal(value.evaluation.comparable, false);
});

test("operations identity grants no project role and policy edits contain only opaque id and quotas", () => {
  const identity = { subject: "operator", operations_admin: true };
  assert.equal(canOperate(identity), true);
  assert.equal(canOperate({ subject: "legacy" }), false);
  assert.equal(canOperate({ operations_admin: "true" }), false);
  assert.equal(projectRole({ members: [] }, identity), null);
  assert.equal(
    projectRole(
      { members: [{ subject: "operator", role: "viewer" }] },
      identity,
    ),
    "viewer",
  );
  assert.deepEqual(
    operationsPolicyPatch("p_opaque", {
      storage: "2",
      queued: "10",
      priority: "3",
      retention: "8",
      name: "must-not-send",
      members: [{ subject: "operator", role: "admin" }],
    }),
    {
      project_id: "p_opaque",
      policy: {
        storage_limit_gib: 2,
        max_queued_jobs: 10,
        priority: 3,
        retention_days: 8,
      },
    },
  );
  assert.throws(() =>
    operationsPolicyPatch("p_opaque", {
      storage: "2",
      queued: "1",
      priority: "11",
      retention: "8",
    }),
  );
});

test('Metal profile selects compatible JD while method changes reset device and MPI', async () => {
  const { withComputeDevice, canExactResume } = await import('../.test-build/models/workspace.js');
  const cfg={target:{k:2,sigma:0},resources:{device:'cpu',mpi_ranks:1,threads:2},solver:{backend:'scipy',method:'arpack',linear:{precision:'float64'},block:{},adaptive_shift:{enabled:true}}};
  const metal=withComputeDevice(cfg,'gpu');
  assert.equal(metal.solver.method,'jacobi_davidson');
  assert.equal(metal.solver.linear.precision,'mixed_float32');
  assert.equal(metal.solver.linear.rtol_initial,1e-3);
  assert.equal(metal.resources.device,'gpu');
  assert.equal(metal.resources.mpi_ranks,1);
  const mpi=withSolverMethod(metal,'krylovschur','slepc');
  assert.equal(mpi.resources.device,'cpu');
  assert.equal(mpi.resources.threads,1);
  assert.equal(mpi.solver.block.checkpoint_kind,'subspace');
  const exact=withSolverMethod(cfg,'block_shift_invert');
  exact.solver.adaptive_shift.enabled=false;
  exact.solver.block={...exact.solver.block,checkpoint_kind:'exact_block',checkpoint_interval:2};
  const job={status:'CANCELLED',config:exact};
  assert.equal(canExactResume(job),true);
  assert.equal(canExactResume({...job,artifacts_state:'MISSING'}),false);
  assert.equal(canExactResume({...job,config:{...exact,solver:{...exact.solver,adaptive_shift:{enabled:true}}}}),false);
  assert.equal(canExactResume({...job,status:'RUNNING'}),false);
});

test('robust ILU v2 retains its explicit identity and enforces CPU float64 across switches', () => {
  const id='ilu_equilibrated_compensated_v2';
  const caps={jacobi_davidson:{preconditioners:['none','jacobi','ilu_budgeted_v1',id]}};
  const before=cpuIluConfig();before.solver.linear.precision='mixed_float32';
  const next=withCorrectionPreconditioner(before,id,caps);
  assert.equal(next.solver.linear.precision,'float64');
  assert.equal(before.solver.linear.precision,'mixed_float32');
  assert.equal(withSolverMethod(next,'jacobi_davidson').solver.linear.preconditioner,id);
  assert.equal(withComputeDevice(next,'gpu').solver.linear.preconditioner,'none');
  assert.equal(withSolverMethod(next,'arpack').solver.linear.preconditioner,'auto_budgeted');
  assert.match(correctionPreconditionerError(next,{}),/后端未声明支持/);
  assert.match(correctionPreconditionerError({...next,resources:{...next.resources,device:'gpu'}},caps),/仅支持 CPU 双精度/);
});

test("SLEPc disk factor profile requires verified capability and preserves target semantics", async () => {
  const { withSlepcFactorization, slepcFactorizationError } = await import('../.test-build/models/workspace.js');
  assert.equal(typeof withSlepcFactorization, 'function');
  const config = {
    problem: { scalar: 'auto' }, target: { k: 2, sigma: 0 }, validation: { residual_scaled_max: 1e-8 },
    resources: { device: 'cpu', mpi_ranks: 1, threads: 1 },
    solver: { backend: 'slepc', method: 'krylovschur', linear: { precision: 'float64', preconditioner: 'auto_budgeted' }, adaptive_shift: {}, block: {} },
  };
  const before = structuredClone(config);
  const capabilities = { slepc: { factor_profiles: ['in_core_lu_v1', 'mumps_ooc_symmetric_v1'] } };
  const next = withSlepcFactorization(config, 'mumps_ooc_symmetric_v1', capabilities);
  assert.equal(next.solver.linear.preconditioner, 'mumps_ooc_symmetric_v1');
  assert.equal(slepcFactorizationError(next, capabilities), '');
  for (const key of ['problem', 'target', 'validation', 'resources']) assert.deepEqual(next[key], config[key]);
  assert.deepEqual(config, before);
  assert.throws(() => withSlepcFactorization(config, 'mumps_ooc_symmetric_v1', {}), /未验证/);
  for (const patch of [ { problem: { scalar: 'complex128' } }, { resources: { ...config.resources, mpi_ranks: 2 } } ]) {
    assert.notEqual(slepcFactorizationError({ ...next, ...patch }, capabilities), '');
  }
  assert.equal(withSolverMethod(next, 'krylovschur', 'slepc').solver.linear.preconditioner, 'mumps_ooc_symmetric_v1');
  assert.equal(withSolverMethod(next, 'arpack', 'scipy').solver.linear.preconditioner, 'auto_budgeted');
  assert.notEqual(withComputeDevice(next, 'gpu').solver.linear.preconditioner, 'mumps_ooc_symmetric_v1');
});
