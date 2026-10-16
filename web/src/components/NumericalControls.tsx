import { useEffect, useState } from "react";
import type { Entity } from "../api";
import {
  correctionPreconditionerError,
  isIluPreconditioner,
  parseNullspaceIndices,
  withCorrectionPreconditioner,
  slepcFactorizationError,
  withSlepcFactorization,
} from "../models/workspace";
import { Field, Notice, Panel } from "../ui";

const blockDefaults = {
  oversampling: 4,
  residual_check_interval: 1,
  max_restarts: 2,
  stagnation_checks: 20,
  checkpoint_interval: 0,
  checkpoint_kind: "subspace",
};
type Props = {
  config: Entity;
  capabilities?: Entity;
  onModify: (section: string, key: string, value: unknown) => void;
  onValidityChange: (error: string) => void;
};

export function NumericalControls({
  config,
  capabilities = {},
  onModify,
  onValidityChange,
}: Props) {
  const declaration = config.problem.nullspace || {
    basis_indices: [],
    interpretation: "unspecified",
    source: "",
  };
  const declaredIndices = (declaration.basis_indices || []).join(", ");
  const [indices, setIndices] = useState(declaredIndices);
  const [error, setError] = useState("");
  useEffect(() => {
    setIndices(declaredIndices);
    setError("");
  }, [declaredIndices, config.problem.matrix_id, onValidityChange]);
  const changeDeclaration = (key: string, value: unknown) =>
    onModify("problem", "nullspace", { ...declaration, [key]: value });
  const block = { ...blockDefaults, ...config.solver.block };
  const jd = config.solver.method === "jacobi_davidson";
  const iterative = jd || config.solver.method === "block_shift_invert";
  const linear = config.solver.linear || {};
  const iluSelected = isIluPreconditioner(linear.preconditioner);
  const iluSupported = capabilities.jacobi_davidson?.preconditioners?.includes("ilu_budgeted_v1") === true;
  const robustIluSupported = capabilities.jacobi_davidson?.preconditioners?.includes("ilu_equilibrated_compensated_v2") === true;
  const iluProfileSelectable = config.solver.backend === "scipy" && (config.resources?.device || "cpu") === "cpu";
  const preconditionerError = correctionPreconditionerError(config, capabilities) || slepcFactorizationError(config, capabilities);
  const accuracy = {
    enabled: false,
    rtol_min: 1e-10,
    rtol_max: 1e-2,
    forcing_factor: 0.1,
    ...linear.adaptive_accuracy,
  };
  const accuracyError =
    jd &&
    (!Number.isFinite(accuracy.rtol_min) ||
      !Number.isFinite(accuracy.rtol_max) ||
      accuracy.rtol_min <= 0 ||
      accuracy.rtol_max > 0.01 ||
      accuracy.rtol_min > accuracy.rtol_max ||
      !Number.isFinite(accuracy.forcing_factor) ||
      accuracy.forcing_factor <= 0 ||
      accuracy.forcing_factor > 1)
      ? "内层精度需满足 0 < 最小阈值 ≤ 最大阈值 ≤ 0.01，反馈比例为 (0, 1]。"
      : block.checkpoint_kind === "exact_block" &&
        (jd || !block.checkpoint_interval || config.solver.adaptive_shift.enabled || config.solver.fallback_profiles?.length)
        ? "精确续算需要块位移反演、正数检查点间隔、关闭位移恢复及算法回退。" : "";
  const checkpointError =
    iterative &&
    (!Number.isSafeInteger(block.checkpoint_interval) ||
      block.checkpoint_interval < 0 ||
      block.checkpoint_interval > 1000000)
      ? "检查点间隔需为 0–1000000 的整数。"
      : "";
  useEffect(() => {
    onValidityChange(error || preconditionerError || accuracyError || checkpointError);
  }, [error, preconditionerError, accuracyError, checkpointError, onValidityChange]);
  const changeAccuracy = (key: string, value: number | boolean) =>
    onModify("solver", "linear", {
      ...linear,
      adaptive_accuracy: { ...accuracy, [key]: value },
    });
  return (
    <Panel title="稀疏重排与算法控制" eyebrow="EXPLICIT NUMERICAL CONTROLS">
      {config.solver.backend === "slepc" ? (
        <Field label="分解因子存储" hint="磁盘方案把部分分解数据写入任务临时目录，以减少工作内存；仍受内存、磁盘和时间预算保护。仅支持实数单进程单线程。">
          <select value={linear.preconditioner === "mumps_ooc_symmetric_v1" ? linear.preconditioner : "auto_budgeted"}
            onChange={event => {
              try {
                const next = withSlepcFactorization(config, event.target.value, capabilities);
                setError(""); onModify("solver", "linear", next.solver.linear);
              } catch (failure) { setError(failure instanceof Error ? failure.message : "无法选择分解存储方案"); }
            }}>
            <option value="auto_budgeted">内存分解 · 原有 LU 策略</option>
            <option value="mumps_ooc_symmetric_v1" disabled={!capabilities.slepc?.factor_profiles?.includes("mumps_ooc_symmetric_v1")}>磁盘因子 · 实对称不定分解</option>
          </select>
        </Field>
      ) : null}
      <Field
        label="工作位移策略版本"
        hint="只影响数值工作位移恢复，保留原始目标及硬预算；旧快照保留其冻结版本"
      >
        <select
          value={config.solver.adaptive_shift.policy || "feedback_bounded_v1"}
          disabled={jd}
          onChange={(event) =>
            onModify("solver", "adaptive_shift", {
              ...config.solver.adaptive_shift,
              policy: event.target.value,
            })
          }
        >
          <option value="observed_ritz_gap_v2">
            v2 · 已观察 Ritz 谱间隙及线性反馈
          </option>
          <option value="feedback_bounded_v1">
            v1 · 有界矩阵尺度扰动（兼容旧快照）
          </option>
        </select>
      </Field>
      {jd ? (
        <p className="subtle-copy">
          JD 使用适配的投影修正方程，不通过切换位移恢复策略改写原目标。
        </p>
      ) : null}
      <Field
        label="对称坐标重排"
        hint="重排只改变计算顺序；输出恢复原始自由度坐标，并保存置换文件"
      >
        <select
          value={config.problem.reordering || "none"}
          onChange={(event) =>
            onModify("problem", "reordering", event.target.value)
          }
        >
          <option value="none">关闭 · 保持原始顺序</option>
          <option value="reverse_cuthill_mckee">
            反向 Cuthill–McKee · 减小稀疏带宽
          </option>
        </select>
      </Field>
      {iterative ? (
        <>
          {config.target.mode !== "nearest_k" ? (
            <Notice kind="error">
              当前迭代算法只支持最近 k 个目标；区间任务请切换到已支持的算法。
            </Notice>
          ) : null}
          <Notice>
            {jd ? (
              "Jacobi–Davidson 通过投影修正方程改善候选方向。GMRES（广义最小残差法）支持 CPU；选择 Metal 混合精度后，低精度稀疏乘积在 GPU 执行，原问题验收仍为 CPU 双精度。"
            ) : (
              <>
                块位移反演同时推进多个候选向量，并在原问题上检查残差。当前支持
                CPU 标准实数 / 复数厄密问题的最近 k 个目标。
              </>
            )}
          </Notice>
          <div className="form-grid">
            {[
              ["oversampling", "额外块向量数", 1, 128],
              ["residual_check_interval", "残差检查间隔", 1, 100],
              ["max_restarts", "最多重启次数", 0, 16],
              ["stagnation_checks", "停滞检查次数", 2, 1000],
              ["checkpoint_interval", "子空间检查点间隔", 0, 1000000],
            ].map(([key, label, min, max]) => (
              <Field
                key={String(key)}
                label={String(label)}
                hint={`允许范围 ${min}–${max}`}
              >
                <input
                  type="number"
                  min={Number(min)}
                  max={Number(max)}
                  value={block[String(key)]}
                  onChange={(event) =>
                    onModify("solver", "block", {
                      ...block,
                      [String(key)]: Number(event.target.value),
                    })
                  }
                />
              </Field>
            ))}
          </div>
          <p className="subtle-copy">
            检查点间隔为 0 时关闭周期快照。子空间快照用于热启动；精确状态保留下一次迭代所需的控制器和随机数状态，要求输入、配置、算法源码及运行环境兼容。
          </p>
          <Field label="检查点内容">
            <select value={block.checkpoint_kind} onChange={event => onModify("solver", "block", { ...block, checkpoint_kind: event.target.value })}>
              <option value="subspace">子空间 · 新任务热启动</option>
              <option value="exact_block" disabled={jd}>完整块迭代状态 · 精确续算</option>
            </select>
          </Field>
          {!jd ? (
            <label className="checkbox">
              <input
                type="checkbox"
                checked={
                  config.solver.fallback_profiles?.includes(
                    "block_to_arpack_v1",
                  ) || false
                }
                onChange={(event) =>
                  onModify(
                    "solver",
                    "fallback_profiles",
                    event.target.checked ? ["block_to_arpack_v1"] : [],
                  )
                }
              />
              <span>
                允许一次有界 ARPACK 回退
                <small>
                  冻结完整回退配置；沿用目标、精度及硬预算，不将回退成功视为主算法成功。
                </small>
              </span>
            </label>
          ) : (
            <>
              <label className="checkbox">
                <input
                  type="checkbox"
                  checked={accuracy.enabled}
                  onChange={(event) =>
                    changeAccuracy("enabled", event.target.checked)
                  }
                />
                <span>
                  启用内层自适应精度
                  <small>
                    根据数值残差在已填写阈值内调整修正方程精度，独立验收阈值保持原值。
                  </small>
                </span>
              </label>
              <div className="form-grid">
                <Field
                  label="修正方程预条件"
                  hint="Jacobi 用对角缩放改善迭代；ILU 用稀疏近似分解改善迭代，仅支持 CPU 双精度"
                >
                  <select
                    value={linear.preconditioner || "none"}
                    onChange={(event) =>
                      onModify("solver", "linear",
                        withCorrectionPreconditioner(config, event.target.value, capabilities).solver.linear,
                      )
                    }
                  >
                    <option value="none">无预条件</option>
                    {capabilities.jacobi_davidson?.preconditioners?.includes(
                      "jacobi",
                    ) || linear.preconditioner === "jacobi" ? (
                      <option
                        value="jacobi"
                        disabled={
                          !capabilities.jacobi_davidson?.preconditioners?.includes(
                            "jacobi",
                          )
                        }
                      >
                        Jacobi 对角缩放
                        {!capabilities.jacobi_davidson?.preconditioners?.includes(
                          "jacobi",
                        )
                          ? "（当前未验证）"
                          : ""}
                      </option>
                    ) : null}
                    {iluSupported || linear.preconditioner === "ilu_budgeted_v1" ? (
                      <option
                        value="ilu_budgeted_v1"
                        disabled={!iluSupported || !iluProfileSelectable}
                      >
                        ILU · 不完全 LU 分解（固定策略 v1）
                        {!iluSupported ? "（当前未支持）" : !iluProfileSelectable ? "（仅 SciPy CPU）" : ""}
                      </option>
                    ) : null}
                    {robustIluSupported || linear.preconditioner === "ilu_equilibrated_compensated_v2" ? (
                      <option value="ilu_equilibrated_compensated_v2" disabled={!robustIluSupported || !iluProfileSelectable}>
                        ILU v2 · 均衡、对角补偿与投影约束
                        {!robustIluSupported ? "（当前未支持）" : !iluProfileSelectable ? "（仅 SciPy CPU）" : ""}
                      </option>
                    ) : null}
                  </select>
                </Field>
                <Field
                  label="修正方程计算精度"
                  hint="低精度仅用于近似修正；原问题独立验证始终使用双精度，不放宽阈值"
                >
                  <select
                    value={linear.precision || "float64"}
                    onChange={(event) =>
                      onModify("solver", "linear", {
                        ...linear,
                        precision: event.target.value,
                      })
                    }
                  >
                    <option value="float64">
                      双精度 · float64 / complex128
                    </option>
                    {capabilities.jacobi_davidson?.mixed_precision ||
                    linear.precision === "mixed_float32" ? (
                      <option
                        value="mixed_float32"
                        disabled={
                          !capabilities.jacobi_davidson?.mixed_precision || iluSelected
                        }
                      >
                        混合精度 · 32位近似修正，必要时提升64位
                        {!capabilities.jacobi_davidson?.mixed_precision
                          ? "（当前未验证）"
                          : iluSelected ? "（ILU 仅支持双精度）" : ""}
                      </option>
                    ) : null}
                  </select>
                </Field>
                <Field label="固定内层残差阈值" hint="关闭自适应精度时使用">
                  <input
                    type="number"
                    step="any"
                    min="1e-16"
                    max=".01"
                    value={linear.rtol_initial}
                    onChange={(event) =>
                      onModify("solver", "linear", {
                        ...linear,
                        rtol_initial: Number(event.target.value),
                      })
                    }
                  />
                </Field>
                <Field label="最多内层迭代">
                  <input
                    type="number"
                    min="1"
                    value={linear.max_iterations}
                    onChange={(event) =>
                      onModify("solver", "linear", {
                        ...linear,
                        max_iterations: Number(event.target.value),
                      })
                    }
                  />
                </Field>
                {[
                  ["rtol_min", "最小内层阈值"],
                  ["rtol_max", "最大内层阈值"],
                  ["forcing_factor", "残差反馈比例"],
                ].map(([key, label]) => (
                  <Field key={key} label={label}>
                    <input
                      type="number"
                      step="any"
                      value={accuracy[key]}
                      onChange={(event) =>
                        changeAccuracy(key, Number(event.target.value))
                      }
                    />
                  </Field>
                ))}
              </div>
              {iluSelected ? (
                <Notice>
                  {linear.preconditioner === "ilu_equilibrated_compensated_v2"
                    ? "ILU v2：固定均衡与对角补偿，每次求解分解一次；每轮复用投影约束，以改善不定问题的修正方向。"
                    : "ILU 固定策略 v1：丢弃阈值 0.001、填充系数 2，每次求解构建一次并复用。"}
                  选择后使用 CPU 双精度；分解前检查内存预算，实际内存和时间仍受硬预算约束。
                  填充系数不保证内存峰值，资源不足或分解失败会明确终止；原目标及独立验收阈值保持原值。
                </Notice>
              ) : null}
            </>
          )}
        </>
      ) : null}
      <details className="nullspace-declaration">
        <summary>零空间工程声明（只检查，不删除模式）</summary>
        <p className="subtle-copy">
          零空间表示算子映射为零的方向。仅在掌握模型依据时声明原始坐标；接近零的特征值仍保留，声明不会自动移除模式。
        </p>
        <Field
          label="原始坐标索引"
          hint="零基非负整数、逗号分隔，最多 512 项；重复值保序去重"
        >
          <input
            value={indices}
            onChange={(event) => {
              const value = event.target.value;
              setIndices(value);
              try {
                const parsed = parseNullspaceIndices(value);
                setError("");
                changeDeclaration("basis_indices", parsed);
              } catch (failure) {
                const detail =
                  failure instanceof Error ? failure.message : String(failure);
                setError(detail);
              }
            }}
          />
        </Field>
        <div className="form-grid">
          <Field label="声明含义">
            <select
              value={declaration.interpretation}
              onChange={(event) =>
                changeDeclaration("interpretation", event.target.value)
              }
            >
              <option value="unspecified">未确认物理含义</option>
              <option value="expected_nullspace">模型预期的零空间</option>
            </select>
          </Field>
          <Field label="工程依据" hint="最多 256 字，记录模型来源或约束说明">
            <input
              value={declaration.source}
              maxLength={256}
              onChange={(event) =>
                changeDeclaration("source", event.target.value)
              }
            />
          </Field>
        </div>
        {error ? <Notice kind="error">{error}</Notice> : null}
      </details>
      <label className="checkbox">
        <input
          type="checkbox"
          checked={config.observability?.enabled !== false}
          onChange={(event) =>
            onModify("observability", "enabled", event.target.checked)
          }
        />
        <span>
          采集次要迭代事件
          <small>
            关闭后仍保留核心状态、独立验证和资源保护；数值配置与验收条件不变。
          </small>
        </span>
      </label>
      {preconditionerError || accuracyError || checkpointError ? (
        <Notice kind="error">{preconditionerError || accuracyError || checkpointError}</Notice>
      ) : null}
    </Panel>
  );
}
