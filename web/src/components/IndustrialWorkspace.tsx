import { useEffect, useRef, useState } from "react";
import { api, download, type Entity } from "../api";
import { Badge, Button, Empty, Field, Json, Notice, Panel } from "../ui";
import "./industrial.css";

const statusText: Record<string, string> = {
  PASS: "通过",
  FAIL: "未通过",
  UNVERIFIED: "未认证",
};
const gateText: Record<string, string> = {
  declared_use: "用途声明",
  frozen_binding: "冻结记录一致性",
  artifacts: "输入与产物完整性",
  residual: "原问题残差",
  target_coverage: "目标模式完整性",
  physical_review: "物理模型审查",
  reference_provenance: "独立参考来源",
  reference_comparison: "参考误差与模式对齐",
  frequency: "频率解释",
  performance: "性能与资源",
  job_outcome: "任务完成状态",
};
const reasonText: Record<string, string> = {
  TEACHING_OR_RESEARCH_ONLY: "用途为教学或研究，不能取得工业通过结论。",
  INDUSTRIAL_DECLARATION: "已声明工业用途；其余证据仍逐项核验。",
  FROZEN_JOB_MATCHES: "当前任务与档案冻结版本一致。",
  FROZEN_JOB_AND_ENGINEERING_MATCH: "任务与所绑定工程模型均与冻结版本一致。",
  PUBLISHED_HASHES_MATCH: "输入与已发布产物的内容摘要一致。",
  FRESH_ORIGINAL_PROBLEM_VALIDATION: "已用原始问题重新计算误差。",
  ORIGINAL_TARGET_CERTIFICATION: "原始目标的模式覆盖结论。",
  PHYSICAL_MODEL_OR_REVIEW_INCOMPLETE_OR_TEACHING_ONLY:
    "缺少工业物理模型审查证据，或来源仅为教学模型。",
  NO_INDEPENDENT_REFERENCE: "尚未提供独立参考数据。",
  NO_TRUSTED_ATTESTATION: "尚未取得已配置信任机构的审查回执。",
  TRUST_AUTHORITY_NOT_CONFIGURED: "服务尚未配置参考审查机构。",
  NO_FREQUENCY_CLAIM_MATRIX_SCOPE:
    "本档案仅验收矩阵特征值，不形成器件频率结论。",
  NO_PERFORMANCE_CLAIM_REQUESTED: "未设置性能门槛，不形成性能通过结论。",
  CANDIDATE_DID_NOT_SUCCEED: "候选任务没有完成合格求解。",
  FRESH_INDEPENDENT_VALIDATION_UNAVAILABLE:
    "资源或数据条件不满足，无法完成本次独立复验。",
  FRESH_TARGET_VALIDATION_UNAVAILABLE: "尚无本次独立目标完整性证据。",
};
const useText: Record<string, string> = {
  teaching: "教学验证",
  research: "研究验证",
  industrial: "工业验收",
};
const criteriaText: Record<string, string> = {
  max_scaled_residual: "最大缩放残差",
  max_eigenvalue_absolute_error: "特征值绝对误差上限",
  max_eigenvalue_relative_error: "特征值相对误差上限",
  max_subspace_sine: "模式子空间误差上限",
  max_frequency_relative_error: "频率相对误差上限",
  max_walltime_seconds: "总耗时上限（秒，可空）",
  max_peak_memory_bytes: "峰值内存上限（字节，可空）",
  min_speedup: "相对基线最低加速比（可空）",
};

export function IndustrialWorkspace({
  projectId,
  jobs,
  canManage,
  canCheck,
}: {
  projectId: string;
  jobs: Entity[];
  canManage: boolean;
  canCheck: boolean;
}) {
  const [page, setPage] = useState<Entity | null>(null);
  const [selected, setSelected] = useState<Entity | null>(null);
  const [dossier, setDossier] = useState<Entity | null>(null);
  const [check, setCheck] = useState<Entity | null>(null);
  const [history, setHistory] = useState<Entity[]>([]);
  const [candidate, setCandidate] = useState("");
  const [templateBinding, setTemplateBinding] = useState<Entity | null>(null);
  const [referenceAsset, setReferenceAsset] = useState<Entity | null>(null);
  const [draft, setDraft] = useState("");
  const [revisionOf, setRevisionOf] = useState("");
  const [cursor, setCursor] = useState<string | null>(null);
  const [previous, setPrevious] = useState<(string | null)[]>([]);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const requests = useRef(new Set<AbortController>());
  const alive = useRef(true);
  const operation = useRef(false);
  const fileVersion = useRef(0);
  let parsed: Entity | null = null;
  try {
    const value = JSON.parse(draft);
    if (value && !Array.isArray(value) && typeof value === "object")
      parsed = value;
  } catch {
    /* Show the editable source until it forms a JSON object. */
  }
  const reportError = (failure: unknown) => {
    if (
      alive.current &&
      !(failure instanceof DOMException && failure.name === "AbortError")
    )
      setError(failure instanceof Error ? failure.message : String(failure));
  };
  const request = async <T,>(
    path: string,
    init: RequestInit = {},
  ): Promise<T> => {
    const controller = new AbortController();
    requests.current.add(controller);
    try {
      return await api<T>(path, { ...init, signal: controller.signal });
    } finally {
      requests.current.delete(controller);
    }
  };
  const load = async (next: string | null = cursor) => {
    if (!projectId) return;
    try {
      const params = new URLSearchParams({ limit: "20" });
      if (next) params.set("cursor", next);
      const data = await request<Entity>(
        `/projects/${encodeURIComponent(projectId)}/industrial-cases?${params}`,
      );
      if (alive.current) setPage(data);
    } catch (failure) {
      reportError(failure);
    }
  };
  useEffect(() => {
    alive.current = true;
    void load(cursor);
    return () => {
      alive.current = false;
      for (const controller of requests.current) controller.abort();
      requests.current.clear();
    };
  }, [projectId, cursor]);
  const perform = async (name: string, action: () => Promise<void>) => {
    if (operation.current) return;
    operation.current = true;
    setBusy(name);
    setError("");
    setNotice("");
    try {
      await action();
    } catch (failure) {
      reportError(failure);
    } finally {
      operation.current = false;
      if (alive.current) setBusy("");
    }
  };
  const inspect = async (item: Entity) => {
    const [data, checks] = await Promise.all([
      request<Entity>(
        `/industrial-cases/${encodeURIComponent(item.id)}/dossier`,
      ),
      request<Entity>(
        `/industrial-cases/${encodeURIComponent(item.id)}/checks?limit=20`,
      ),
    ]);
    if (!alive.current) return;
    setSelected(item);
    setDossier(data);
    setCheck(checks.items?.[0] || item.latest_check || null);
    setHistory(checks.items || []);
  };
  const template = () =>
    void perform("template", async () => {
      const data = await request<Entity>(
        `/projects/${encodeURIComponent(projectId)}/industrial-template?job_id=${encodeURIComponent(candidate)}`,
      );
      if (!alive.current) return;
      setDraft(JSON.stringify(data.manifest, null, 2));
      setRevisionOf("");
      setTemplateBinding(data.binding);
      setReferenceAsset(null);
      setNotice("已从任务冻结快照创建草稿。请补充实际证据；未知项保留空值。");
    });
  const update = (key: string, value: unknown) => {
    if (parsed) setDraft(JSON.stringify({ ...parsed, [key]: value }, null, 2));
  };
  const save = () =>
    void perform("save", async () => {
      if (!parsed || new TextEncoder().encode(draft).length > 1024 ** 2)
        throw new Error("请输入不超过 1 MiB 的 JSON 对象。");
      const path = revisionOf
        ? `/industrial-cases/${encodeURIComponent(revisionOf)}/revisions`
        : `/projects/${encodeURIComponent(projectId)}/industrial-cases`;
      const created = await request<Entity>(path, {
        method: "POST",
        body: JSON.stringify({ manifest: parsed }),
      });
      if (!alive.current) return;
      setNotice("已冻结新档案；旧版本保留。运行检查后可查看各项验收状态。");
      await load();
      await inspect(created);
    });
  const runCheck = () =>
    void perform("check", async () => {
      if (!selected) return;
      const result = await request<Entity>(
        `/industrial-cases/${encodeURIComponent(selected.id)}/checks`,
        { method: "POST", body: "{}" },
      );
      if (alive.current) {
        setCheck(result);
        setHistory((current) => [result, ...current].slice(0, 20));
        setNotice("本次检查已保存；每项结论均附证据或缺失原因。");
      }
      await load();
    });
  const readFile = async (file?: File) => {
    if (!file) return;
    const version = ++fileVersion.current;
    try {
      if (file.size > 1024 ** 2) throw new Error("清单不能超过 1 MiB。");
      const raw = new TextDecoder("utf-8", { fatal: true }).decode(
        await file.arrayBuffer(),
      );
      if (alive.current && version === fileVersion.current) {
        setDraft(raw);
        setNotice("已读取清单，尚未提交。");
      }
    } catch (failure) {
      reportError(failure);
    }
  };
  const uploadReference = async (file?: File) => {
    if (!file || !canManage) return;
    await perform("reference", async () => {
      if (file.size > 128 * 1024 ** 2)
        throw new Error("参考文件不能超过 128 MiB。");
      const body = new FormData();
      body.append("file", file);
      const asset = await request<Entity>(
        `/projects/${encodeURIComponent(projectId)}/industrial-reference-assets`,
        { method: "POST", body },
      );
      if (alive.current) {
        setReferenceAsset(asset);
        setNotice(
          "独立参考资产已保存，请确认来源后绑定至草稿。候选求解不会使用该参考。",
        );
      }
    });
  };
  const bindReference = () => {
    if (!parsed || !referenceAsset) return;
    update("reference", {
      matrix_sha256:
        templateBinding?.matrix_sha256 || dossier?.binding?.matrix_sha256 || "",
      target_sha256:
        templateBinding?.target_sha256 || dossier?.binding?.target_sha256 || "",
      method: "independent_solver",
      producer: "",
      source: { title: "", url: "" },
      artifact_sha256: referenceAsset.sha256,
      artifact_id: referenceAsset.id,
      eigenvalues: null,
      vectors_real: null,
      vectors_imag: null,
      attestation: null,
    });
    setNotice(
      "参考资产已绑定草稿。请在清单填写独立来源、程序版本及实际审查凭据，再冻结新档案。",
    );
  };

  if (!projectId)
    return (
      <Empty
        title="先选择项目"
        description="工业档案、验收任务与报告均受项目权限控制。"
      />
    );
  return (
    <div
      className={`industrial-workspace ${selected && dossier ? "has-review" : ""}`}
    >
      <Panel
        className="industrial-registry"
        title="工业验收档案"
        eyebrow="MODEL · COMPUTATION · INDEPENDENT EVIDENCE"
        action={
          <Button disabled={!!busy} onClick={() => void load()}>
            刷新档案
          </Button>
        }
      >
        <p className="panel-copy">
          将原始矩阵、任务目标、模型说明、独立参考和性能门槛冻结为同一版本。数值精度、目标覆盖、物理审查与资源要求分别判定；缺少证据显示“未认证”。
        </p>
        {error ? <Notice kind="error">{error}</Notice> : null}
        {notice ? <Notice>{notice}</Notice> : null}
        <div className="industrial-cases" aria-label="验收档案版本">
          {page?.items?.length ? (
            page.items.map((item: Entity) => (
              <button
                type="button"
                key={item.id}
                className={`industrial-case ${selected?.id === item.id ? "selected" : ""}`}
                aria-pressed={selected?.id === item.id}
                disabled={!!busy}
                onClick={() => void perform("inspect", () => inspect(item))}
              >
                <strong>{item.title}</strong>
                <span>
                  {useText[item.declared_use] || item.declared_use} · 版本{" "}
                  {item.revision}
                </span>
                <span>
                  {statusText[item.latest_check?.status] || "尚未检查"}
                </span>
                <small className="mono">{item.id}</small>
              </button>
            ))
          ) : (
            <p className="panel-copy">
              当前页暂无档案。可从已完成的任务创建第一个版本。
            </p>
          )}
        </div>
        <div className="button-row">
          <Button
            disabled={!!busy || !previous.length}
            onClick={() => {
              const history = [...previous];
              setCursor(history.pop() ?? null);
              setPrevious(history);
            }}
          >
            上一页
          </Button>
          <Button
            disabled={!!busy || !page?.next_cursor}
            onClick={() => {
              setPrevious([...previous, cursor]);
              setCursor(page?.next_cursor || null);
            }}
          >
            下一页
          </Button>
        </div>
      </Panel>
      {selected && dossier ? (
        <Panel
          className="industrial-review"
          title={selected.title}
          eyebrow={`FROZEN DOSSIER · REVISION ${selected.revision}`}
          action={<Badge status={check?.status || "CREATED"} />}
        >
          <p className="mono industrial-hash">
            SHA-256：{selected.dossier_sha256}
          </p>
          <div className="button-row">
            <Button disabled={!!busy || !canCheck} onClick={runCheck}>
              {busy === "check" ? "正在复核…" : "运行验收检查"}
            </Button>
            <Button
              disabled={!!busy || !canManage}
              onClick={() => {
                setDraft(JSON.stringify(dossier.manifest, null, 2));
                setRevisionOf(selected.id);
                setTemplateBinding(dossier.binding);
                setReferenceAsset(null);
                setNotice("正在编辑新修订，原档案保持可追溯。");
              }}
            >
              基于此版本修订
            </Button>
            <Button
              disabled={!!busy}
              onClick={() =>
                void perform("export", async () => {
                  await download(
                    `/industrial-cases/${encodeURIComponent(selected.id)}/report`,
                    `${selected.id}-acceptance.json`,
                  );
                })
              }
            >
              下载验收报告
            </Button>
          </div>
          {check ? (
            <>
              <Notice>
                {check.industrial_accepted === true
                  ? "已满足当前配置的工业验收门槛。"
                  : "当前记录尚未取得工业验收通过结论。"}
              </Notice>
              <div className="table-wrap">
                <table>
                  <caption className="paper-table-caption">
                    工业验收项目与证据判定
                  </caption>
                  <thead>
                    <tr>
                      <th>验收项目</th>
                      <th>判定</th>
                      <th>证据与原因</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(check.gates || []).map((gate: Entity) => (
                      <tr key={gate.name}>
                        <td>{gateText[gate.name] || gate.name}</td>
                        <td>
                          {gate.reason === "NO_FREQUENCY_CLAIM_MATRIX_SCOPE" ||
                          gate.reason === "NO_PERFORMANCE_CLAIM_REQUESTED" ? (
                            <span>未要求</span>
                          ) : (
                            <Badge status={gate.status} />
                          )}
                        </td>
                        <td>
                          {reasonText[gate.reason] || gate.reason}
                          {gate.details ? (
                            <details>
                              <summary>查看依据</summary>
                              <Json value={gate.details} />
                            </details>
                          ) : null}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          ) : null}
          <p className="panel-copy">
            验收范围：
            {selected.acceptance_scope === "resonant_frequencies"
              ? "器件谐振频率"
              : "原矩阵特征值问题"}
            。通过结论仅适用于本档案冻结的输入、范围与门槛。
          </p>
          {history.length ? (
            <Field label="历史检查（最近 20 次）">
              <select
                value={check?.id || ""}
                onChange={(event) =>
                  setCheck(
                    history.find((item) => item.id === event.target.value) ||
                      null,
                  )
                }
              >
                {history.map((item) => (
                  <option key={item.id} value={item.id}>
                    {new Date(item.created_at).toLocaleString("zh-CN")} ·{" "}
                    {statusText[item.status] || item.status} · {item.id}
                  </option>
                ))}
              </select>
            </Field>
          ) : null}
          <details>
            <summary>冻结的模型、任务与参考绑定</summary>
            <Json value={dossier.bindings || dossier.binding} />
          </details>
          <details>
            <summary>查看完整档案</summary>
            <Json value={dossier} />
          </details>
        </Panel>
      ) : null}
      <Panel
        className="industrial-creation"
        title={revisionOf ? "修订验收档案" : "创建验收档案"}
        eyebrow="IMMUTABLE VERSION · PROJECT AUTHORIZATION"
      >
        <div className="industrial-fields">
          <Field label="候选任务">
            <select
              value={candidate}
              onChange={(event) => setCandidate(event.target.value)}
              disabled={!!busy || !canManage}
            >
              <option value="">选择当前项目的已完成任务</option>
              {jobs
                .filter(
                  (job) =>
                    job.project_id === projectId &&
                    ["SUCCEEDED", "PARTIAL", "FAILED", "TIMED_OUT"].includes(
                      job.status,
                    ),
                )
                .map((job) => (
                  <option key={job.id} value={job.id}>
                    {job.name || job.id} · {job.status}
                  </option>
                ))}
            </select>
          </Field>
          <Field label="任务编号（也可粘贴历史任务编号）">
            <input
              value={candidate}
              disabled={!!busy || !canManage}
              onChange={(event) => setCandidate(event.target.value)}
            />
          </Field>
          <div className="industrial-template">
            <Button
              disabled={!!busy || !candidate || !canManage}
              onClick={template}
            >
              从任务生成证据模板
            </Button>
          </div>
        </div>
        {parsed ? (
          <>
            <div className="industrial-fields">
              <Field label="档案标题">
                <input
                  value={parsed.title || ""}
                  disabled={!canManage || !!busy}
                  onChange={(event) => update("title", event.target.value)}
                />
              </Field>
              <Field label="验收用途">
                <select
                  value={parsed.declared_use || "research"}
                  disabled={!canManage || !!busy}
                  onChange={(event) =>
                    update("declared_use", event.target.value)
                  }
                >
                  {Object.entries(useText).map(([key, label]) => (
                    <option key={key} value={key}>
                      {label}
                    </option>
                  ))}
                </select>
              </Field>
            </div>
            <Field label="验收范围">
              <select
                value={parsed.acceptance_scope || "matrix_eigenproblem"}
                disabled={!canManage || !!busy}
                onChange={(event) =>
                  update("acceptance_scope", event.target.value)
                }
              >
                <option value="matrix_eigenproblem">原矩阵特征值问题</option>
                <option value="resonant_frequencies">
                  器件谐振频率（需要模型映射和频率证据）
                </option>
              </select>
            </Field>
            <div className="industrial-fields">
              {Object.entries(criteriaText).map(([key, label]) => (
                <Field key={key} label={label}>
                  <input
                    type="number"
                    min="0"
                    step="any"
                    value={parsed?.criteria?.[key] ?? ""}
                    disabled={!canManage || !!busy}
                    onChange={(event) => {
                      if (!parsed) return;
                      update("criteria", {
                        ...parsed.criteria,
                        [key]:
                          event.target.value === ""
                            ? null
                            : Number(event.target.value),
                      });
                    }}
                  />
                </Field>
              ))}
            </div>
          </>
        ) : null}
        <div className="industrial-reference-upload">
          <Field label="独立参考二进制资产（NPZ）">
            <input
              type="file"
              accept=".npz"
              disabled={!canManage || !!busy}
              onChange={(event) =>
                void uploadReference(event.target.files?.[0])
              }
            />
          </Field>
          <p className="panel-copy">
            文件内须恰好含 eigenvalues.npy 与
            eigenvectors.npy，分别为模式值及按列排列的向量。最大 128 MiB、32
            个模式、800
            万个向量项，覆盖大矩阵参考所需存储；来源与目标仍须独立核实。
          </p>
          {referenceAsset ? (
            <>
              <p className="mono industrial-hash">
                {referenceAsset.id} · {referenceAsset.n} 行 /{" "}
                {referenceAsset.modes} 模式 · {referenceAsset.sha256}
              </p>
              <Button disabled={!parsed || !!busy} onClick={bindReference}>
                绑定到当前草稿
              </Button>
            </>
          ) : null}
        </div>
        <Field label="模型与独立证据清单（JSON）">
          <textarea
            className="industrial-editor mono"
            value={draft}
            disabled={!canManage || !!busy}
            spellCheck={false}
            placeholder="先从任务生成模板，或读取已有清单"
            onChange={(event) => setDraft(event.target.value)}
          />
        </Field>
        <p className="panel-copy">
          填写材料参数、边界条件、网格来源和参考数据；没有资料时保留
          null。声明、数字摘要和配置的审查者凭据各有独立校验，两个运行结果一致不能单独证明物理正确。
        </p>
        <div className="button-row">
          <label className="industrial-upload">
            读取清单文件
            <input
              type="file"
              accept=".json,application/json"
              disabled={!canManage || !!busy}
              onChange={(event) => void readFile(event.target.files?.[0])}
            />
          </label>
          <Button disabled={!canManage || !!busy || !parsed} onClick={save}>
            冻结为新版本
          </Button>
        </div>
      </Panel>
    </div>
  );
}
