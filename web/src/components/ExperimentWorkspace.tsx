import { useCallback, useEffect, useRef, useState } from "react";
import {
  FileText,
  FlaskConical,
  Play,
  RefreshCw,
  ShieldCheck,
  Square,
} from "lucide-react";
import { api, download, number, post, type Entity } from "../api";
import {
  Badge,
  Button,
  Empty,
  Field,
  Json,
  Metric,
  Notice,
  Panel,
} from "../ui";
import {
  batchNeedsPolling,
  benchmarkPagePath,
  experimentSummary,
  parseNumberAxis,
  readBenchmarkDetail,
  type BenchmarkPage,
  type ExperimentBatch,
  type ExperimentDefinition,
  type ExperimentParameters,
  type ExperimentPlan,
} from "../models/experiments";
import { ConfigurationDiff } from "./ConfigurationDiff";

type Props = {
  projectId: string;
  matrices: Entity[];
  baseConfig: Entity;
  onOpenJob: (job: Entity) => void;
  onChanged: () => void | Promise<void>;
  notify: (message: string, error?: boolean) => void;
};
type Draft = {
  k: string;
  sigma: string;
  threads: string;
  memory: string;
  adaptive: "enabled" | "disabled" | "both";
  repeats: string;
  maxRuns: string;
  maxTime: string;
  maxDisk: string;
};
const short = (value?: string) => value?.slice(0, 12) || "—";
const errorMessage = (error: unknown) =>
  error instanceof Error ? error.message : String(error);
const date = (value: string) => new Date(value).toLocaleString("zh-CN");
const printable = (value: unknown) =>
  typeof value === "string" ? value : JSON.stringify(value);

function Parameters({ value }: { value: Partial<ExperimentParameters> }) {
  return (
    <span className="experiment-parameters">
      k={value.k ?? "—"} · σ={value.sigma ?? "—"} · {value.threads ?? "—"} 线程
      · {value.memory_limit_gib ?? "—"} GiB · 位移恢复
      {value.adaptive_shift === undefined
        ? "—"
        : value.adaptive_shift
          ? "开"
          : "关"}
    </span>
  );
}

function Summary({ summary = {} }: { summary?: Entity }) {
  const counts = experimentSummary(summary);
  return (
    <div className="benchmark-summary experiment-summary">
      <span>
        全部运行 <b>{counts.total}</b>
      </span>
      <span>
        认证成功{" "}
        <b>
          {counts.certified} / {counts.total}
        </b>
      </span>
      <span>
        部分结果{" "}
        <b>
          {counts.partial} / {counts.total}
        </b>
      </span>
      <span>
        失败 / 取消 / 超时{" "}
        <b>
          {counts.failed} / {counts.total}
        </b>
      </span>
      <span>
        未结束 <b>{counts.active}</b>
      </span>
      <span>
        全部记录耗时中位数 <b>{number(summary.time_median_seconds)} s</b>（分母{" "}
        {counts.recordedTimes}）
      </span>
      <span>
        认证成功耗时中位数{" "}
        <b>{number(summary.certified_time_median_seconds)} s</b>（分母{" "}
        {counts.certifiedTimes}）
      </span>
    </div>
  );
}

export function ExperimentWorkspace({
  projectId,
  matrices,
  baseConfig,
  onOpenJob,
  onChanged,
  notify,
}: Props) {
  const [draft, setDraft] = useState<Draft>(() => ({
    k: String(baseConfig.target?.k ?? 6),
    sigma: String(baseConfig.target?.sigma ?? 0),
    threads: String(baseConfig.resources?.threads ?? 1),
    memory: String(baseConfig.resources?.memory_limit_gib ?? 16),
    adaptive:
      baseConfig.solver?.adaptive_shift?.enabled === false
        ? "disabled"
        : "enabled",
    repeats: "1",
    maxRuns: "100",
    maxTime: "7200",
    maxDisk: "200",
  }));
  const [matrixIds, setMatrixIds] = useState<string[]>([]);
  const [preview, setPreview] = useState<{
    fingerprint: string;
    plan: ExperimentPlan;
  } | null>(null);
  const [busy, setBusy] = useState("");
  const [previewError, setPreviewError] = useState("");
  const previewRequest = useRef<AbortController | null>(null);
  const scopedMatrices = projectId
    ? matrices.filter((matrix) => matrix.project_id === projectId)
    : [];
  const matrixById = new Map(matrices.map((matrix) => [matrix.id, matrix]));
  // Include selected asset metadata as well as all draft fields; an input change must revoke confirmation immediately.
  const fingerprint = JSON.stringify({
    projectId,
    matrixIds,
    draft,
    baseConfig,
    inputs: matrixIds.map((id) => matrixById.get(id)),
  });
  const fingerprintRef = useRef(fingerprint);
  fingerprintRef.current = fingerprint;
  const scopeRef = useRef(projectId);
  scopeRef.current = projectId;
  const currentPreview =
    preview?.fingerprint === fingerprint ? preview.plan : null;

  let specification: ExperimentDefinition | null = null;
  let validationError = "";
  let cellCount = 0;
  let runCount = 0;
  try {
    if (!projectId)
      throw new Error("请先在页面顶部选择一个项目，再设计项目内实验。");
    if (!matrixIds.length) throw new Error("至少选择一个矩阵。");
    if (matrixIds.length > 30) throw new Error("一次实验最多选择 30 个矩阵。");
    if (matrixIds.some((id) => matrixById.get(id)?.project_id !== projectId))
      throw new Error("实验矩阵必须全部属于当前项目，请重新选择。");
    if (baseConfig.target?.mode !== "nearest_k")
      throw new Error(
        "参数实验当前只支持最近 k 个目标；请先在求解配置中选择该目标模式。",
      );
    const k = parseNumberAxis(draft.k, "k", {
      integer: true,
      min: 1,
      max: 10000,
    });
    const sigma = parseNumberAxis(draft.sigma, "σ");
    const threads = parseNumberAxis(draft.threads, "线程数", {
      integer: true,
      min: 1,
      max: 256,
    });
    const memory = parseNumberAxis(draft.memory, "内存", {
      min: Number.MIN_VALUE,
      max: 1024,
    });
    const repeats = parseNumberAxis(draft.repeats, "重复次数", {
      integer: true,
      min: 1,
      max: 10,
      maxLength: 1,
    })[0];
    const maxRuns = parseNumberAxis(draft.maxRuns, "总运行预算", {
      integer: true,
      min: 1,
      max: 100,
      maxLength: 1,
    })[0];
    const maxTime = parseNumberAxis(draft.maxTime, "累计时间预算", {
      min: Number.MIN_VALUE,
      maxLength: 1,
    })[0];
    const maxDisk = parseNumberAxis(draft.maxDisk, "累计磁盘预算", {
      min: Number.MIN_VALUE,
      maxLength: 1,
    })[0];
    const adaptive =
      draft.adaptive === "both"
        ? [true, false]
        : [draft.adaptive === "enabled"];
    cellCount =
      matrixIds.length *
      k.length *
      sigma.length *
      threads.length *
      memory.length *
      adaptive.length;
    runCount = cellCount * repeats;
    if (cellCount > 100 || runCount > 100)
      throw new Error(
        `当前为 ${cellCount} 个组合、${runCount} 次运行，组合和运行总数均不得超过 100。`,
      );
    if (runCount > maxRuns)
      throw new Error(
        `当前 ${runCount} 次运行超过填写的总运行预算 ${maxRuns}。`,
      );
    if (runCount * Number(baseConfig.resources?.walltime_seconds) > maxTime)
      throw new Error(
        "所有运行的时间硬上限之和超过累计时间预算，请减少组合或调整预算。",
      );
    if (runCount * Number(baseConfig.resources?.disk_limit_gib) > maxDisk)
      throw new Error(
        "所有运行的磁盘硬上限之和超过累计磁盘预算，请减少组合或调整预算。",
      );
    specification = {
      project_id: projectId,
      matrix_ids: matrixIds,
      base_config: baseConfig,
      k_values: k,
      sigma_values: sigma,
      threads_values: threads,
      memory_limit_gib_values: memory,
      adaptive_shift_values: adaptive,
      repeats,
      budget: {
        max_runs: maxRuns,
        max_total_walltime_seconds: maxTime,
        max_total_disk_gib: maxDisk,
      },
    };
  } catch (error) {
    validationError = errorMessage(error);
  }

  useEffect(() => {
    selectionVersionRef.current += 1;
    setMatrixIds([]);
    setSelectedId("");
  }, [projectId]);
  useEffect(() => {
    previewRequest.current?.abort();
    setPreview(null);
    setPreviewError("");
    setBusy((value) => (value === "preview" ? "" : value));
    return () => previewRequest.current?.abort();
  }, [fingerprint]);

  const [list, setList] = useState<{ projectId: string; page: BenchmarkPage }>({
    projectId,
    page: { items: [], next_cursor: null, total: 0 },
  });
  const [listLoading, setListLoading] = useState(false);
  const [listError, setListError] = useState("");
  const [listRevision, setListRevision] = useState(0);
  const listRequest = useRef<AbortController | null>(null);
  const fetchPage = useCallback(
    async (cursor?: string | null) => {
      listRequest.current?.abort();
      const controller = new AbortController();
      listRequest.current = controller;
      setListLoading(true);
      setListError("");
      try {
        const page = await api<BenchmarkPage>(
          benchmarkPagePath(projectId, cursor),
          { signal: controller.signal },
        );
        if (controller.signal.aborted || scopeRef.current !== projectId) return;
        setList((previous) => {
          if (!cursor || previous.projectId !== projectId)
            return { projectId, page };
          const byId = new Map(
            previous.page.items.map((batch) => [batch.id, batch]),
          );
          page.items.forEach((batch) => byId.set(batch.id, batch));
          return { projectId, page: { ...page, items: [...byId.values()] } };
        });
      } catch (error) {
        if (!controller.signal.aborted && scopeRef.current === projectId)
          setListError(errorMessage(error));
      } finally {
        if (!controller.signal.aborted && scopeRef.current === projectId)
          setListLoading(false);
      }
    },
    [projectId],
  );
  useEffect(() => {
    void fetchPage();
    return () => listRequest.current?.abort();
  }, [fetchPage, listRevision]);
  const refreshList = () => {
    listRequest.current?.abort();
    setListRevision((value) => value + 1);
  };
  const visiblePage =
    list.projectId === projectId
      ? list.page
      : { items: [], total: 0, next_cursor: null };

  const [selectedId, setSelectedId] = useState("");
  const selectionVersionRef = useRef(0);
  const selectedIdRef = useRef(selectedId);
  selectedIdRef.current = selectedId;
  const [detail, setDetail] = useState<ExperimentBatch | null>(null);
  const [detailError, setDetailError] = useState("");
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailRevision, setDetailRevision] = useState(0);
  const detailRequest = useRef<AbortController | null>(null);
  const visibleDetail =
    detail?.id === selectedId && (!projectId || detail.project_id === projectId)
      ? detail
      : null;
  useEffect(() => {
    const controller = new AbortController();
    detailRequest.current = controller;
    let timer: ReturnType<typeof setTimeout>;
    setDetailError("");
    if (!selectedId) {
      setDetailLoading(false);
      return () => controller.abort();
    }
    setDetailLoading(true);
    const read = async () => {
      let delay: number | null = 3500;
      try {
        const next = await readBenchmarkDetail(
          selectedId,
          api,
          controller.signal,
        );
        if (
          controller.signal.aborted ||
          selectedIdRef.current !== selectedId ||
          scopeRef.current !== projectId
        )
          return;
        setDetail(next.batch);
        setDetailError("");
        setList((previous) =>
          previous.projectId !== projectId
            ? previous
            : {
                ...previous,
                page: {
                  ...previous.page,
                  items: previous.page.items.map((batch) =>
                    batch.id === selectedId
                      ? { ...next.batch, runs: undefined, cells: undefined }
                      : batch,
                  ),
                },
              },
        );
        delay = next.delay;
      } catch (error) {
        if (!controller.signal.aborted && selectedIdRef.current === selectedId)
          setDetailError(errorMessage(error));
      } finally {
        if (!controller.signal.aborted && selectedIdRef.current === selectedId)
          setDetailLoading(false);
      }
      if (!controller.signal.aborted && delay !== null)
        timer = setTimeout(read, delay);
    };
    void read();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [selectedId, projectId, detailRevision]);

  const change = (field: keyof Draft, value: string) =>
    setDraft((previous) => ({ ...previous, [field]: value }));
  const freeze = async () => {
    if (!specification || busy) return;
    const expected = fingerprint;
    previewRequest.current?.abort();
    const controller = new AbortController();
    previewRequest.current = controller;
    setBusy("preview");
    setPreviewError("");
    try {
      const plan = await api<ExperimentPlan>("/experiments/plans", {
        method: "POST",
        body: JSON.stringify(specification),
        signal: controller.signal,
      });
      if (controller.signal.aborted || fingerprintRef.current !== expected)
        return;
      setPreview({ fingerprint: expected, plan });
      notify("实验计划已冻结，请核对组合与硬预算后确认提交");
    } catch (error) {
      if (!controller.signal.aborted && fingerprintRef.current === expected)
        setPreviewError(errorMessage(error));
    } finally {
      if (!controller.signal.aborted && fingerprintRef.current === expected)
        setBusy("");
    }
  };
  const submit = async () => {
    if (!currentPreview || busy) return;
    const expected = fingerprint;
    const expectedProject = projectId;
    const expectedSelectionVersion = selectionVersionRef.current;
    setBusy("submit");
    try {
      const batch = await post<ExperimentBatch>("/experiments/submit", {
        experiment_plan_id: currentPreview.id,
      });
      if (scopeRef.current === expectedProject) {
        refreshList();
        if (
          fingerprintRef.current === expected &&
          selectionVersionRef.current === expectedSelectionVersion
        ) {
          setDetail(batch);
          setSelectedId(batch.id);
          setDetailRevision((value) => value + 1);
        }
      }
      void onChanged();
      notify("冻结实验已提交；相同计划重复提交将返回原批次");
    } catch (error) {
      notify(errorMessage(error), true);
    } finally {
      setBusy("");
    }
  };
  const cancel = async () => {
    if (!visibleDetail || busy) return;
    const id = visibleDetail.id;
    setBusy("cancel");
    try {
      await post(`/benchmarks/${encodeURIComponent(id)}/cancel`);
      if (selectedIdRef.current === id) setDetailRevision((value) => value + 1);
      refreshList();
      void onChanged();
      notify("批次取消已请求，已结束的运行保留原结果");
    } catch (error) {
      notify(errorMessage(error), true);
    } finally {
      setBusy("");
    }
  };
  const exportReport = async () => {
    if (!visibleDetail || busy) return;
    const id = visibleDetail.id;
    setBusy("report");
    try {
      const report = await post("/reports", { benchmark_id: id });
      await download(report.url, `benchmark-${short(id)}.html`);
      notify("包含全部组合、失败和统计分母的实验报告已下载");
    } catch (error) {
      notify(errorMessage(error), true);
    } finally {
      setBusy("");
    }
  };

  return (
    <div className="experiment-workspace">
      <div className="two-col experiment-design">
        <Panel title="设计有界参数实验" eyebrow="01 / EXPERIMENT DESIGN">
          <p className="experiment-copy">
            从当前求解配置出发，组合各轴并分别重复运行。输入矩阵及存储声明由所选资产绑定，其他求解与验收条件沿用当前配置。
          </p>
          <fieldset className="experiment-fields" disabled={busy === "submit"}>
            <div className="field">
              <span id="experiment-matrix-label">
                项目内矩阵（已选 {matrixIds.length} / 30）
              </span>
              <div
                className="checkbox-list experiment-matrices"
                role="group"
                aria-labelledby="experiment-matrix-label"
              >
                {scopedMatrices.length ? (
                  scopedMatrices.map((matrix) => (
                    <label className="checkbox" key={matrix.id}>
                      <input
                        type="checkbox"
                        checked={matrixIds.includes(matrix.id)}
                        onChange={() =>
                          setMatrixIds((previous) =>
                            previous.includes(matrix.id)
                              ? previous.filter((id) => id !== matrix.id)
                              : [...previous, matrix.id],
                          )
                        }
                      />
                      <span>
                        {matrix.name}
                        <small>
                          n={matrix.n} · {matrix.source || "用户导入"}
                        </small>
                      </span>
                    </label>
                  ))
                ) : (
                  <p className="muted">
                    {projectId
                      ? "请先在当前项目登记矩阵。"
                      : "请在页面顶部选择项目。"}
                  </p>
                )}
              </div>
            </div>
            <div className="form-grid">
              <Field label="目标数量 k" hint="逗号分隔的正整数，例如 4, 6">
                <input
                  value={draft.k}
                  onChange={(event) => change("k", event.target.value)}
                  inputMode="text"
                />
              </Field>
              <Field label="目标位移 σ" hint="逗号分隔的有限数值，例如 0, 0.1">
                <input
                  value={draft.sigma}
                  onChange={(event) => change("sigma", event.target.value)}
                  inputMode="text"
                />
              </Field>
              <Field
                label="CPU 线程数"
                hint="每个值 1–256；不代表实际线程利用率"
              >
                <input
                  value={draft.threads}
                  onChange={(event) => change("threads", event.target.value)}
                  inputMode="text"
                />
              </Field>
              <Field
                label="每次内存上限 / GiB"
                hint="大于 0，最多 1024；逗号分隔"
              >
                <input
                  value={draft.memory}
                  onChange={(event) => change("memory", event.target.value)}
                  inputMode="text"
                />
              </Field>
              <Field
                label="有界工作位移恢复"
                hint="对照已实现策略的开关，不改变目标位移"
              >
                <select
                  value={draft.adaptive}
                  onChange={(event) => change("adaptive", event.target.value)}
                >
                  <option value="enabled">仅开启</option>
                  <option value="disabled">仅关闭</option>
                  <option value="both">开启与关闭对照</option>
                </select>
              </Field>
              <Field label="每组合重复次数" hint="1–10；每次运行保留独立编号">
                <input
                  value={draft.repeats}
                  onChange={(event) => change("repeats", event.target.value)}
                  inputMode="numeric"
                />
              </Field>
            </div>
            <div className="experiment-budget-title">整批预算上限</div>
            <div className="form-grid">
              <Field label="最多运行次数" hint="最多 100 次">
                <input
                  value={draft.maxRuns}
                  onChange={(event) => change("maxRuns", event.target.value)}
                  inputMode="numeric"
                />
              </Field>
              <Field label="累计时间硬预算 / s">
                <input
                  value={draft.maxTime}
                  onChange={(event) => change("maxTime", event.target.value)}
                  inputMode="decimal"
                />
              </Field>
              <Field label="累计磁盘硬预算 / GiB">
                <input
                  value={draft.maxDisk}
                  onChange={(event) => change("maxDisk", event.target.value)}
                  inputMode="decimal"
                />
              </Field>
              <div className="experiment-count">
                <b>
                  {cellCount} 组合 · {runCount} 次运行
                </b>
                <small>各数值轴最多 10 项，重复值保序去重</small>
              </div>
            </div>
          </fieldset>
          <Notice>
            累计时间与磁盘按每次硬上限求和；当前每次为{" "}
            {number(baseConfig.resources?.walltime_seconds)} s /{" "}
            {number(baseConfig.resources?.disk_limit_gib)}{" "}
            GiB。它们是最坏情况限额，不是预计耗时或预计占用。
          </Notice>
          {validationError ? (
            <p className="experiment-validation" role="status">
              {validationError}
            </p>
          ) : null}
          {previewError ? (
            <Notice kind="error">冻结失败：{previewError}</Notice>
          ) : null}
          <div className="experiment-actions">
            <Button
              primary
              disabled={!specification || Boolean(busy)}
              busy={busy === "preview"}
              onClick={() => void freeze()}
            >
              <ShieldCheck size={15} />
              冻结并预览实验
            </Button>
          </div>
          <details className="experiment-base">
            <summary>当前基础配置（修改后预览失效）</summary>
            <Json value={baseConfig} />
          </details>
        </Panel>

        <Panel title="核对冻结计划" eyebrow="02 / REVIEW BEFORE SUBMISSION">
          {currentPreview ? (
            <>
              <div className="plan-status">
                <ShieldCheck size={23} />
                <div>
                  <b>组合及配置已冻结</b>
                  <small>
                    计划 {short(currentPreview.id)} ·{" "}
                    {date(currentPreview.created_at)}
                  </small>
                </div>
              </div>
              <div className="experiment-budget-metrics">
                <Metric
                  label="冻结组合"
                  value={currentPreview.cells.length}
                  detail={`${currentPreview.budget.total_runs ?? runCount} 次独立运行`}
                />
                <Metric
                  label="累计时间硬上限"
                  value={number(currentPreview.budget.total_walltime_seconds)}
                  unit="s"
                  detail={`整批限额 ${number(currentPreview.budget.max_total_walltime_seconds)} s`}
                />
                <Metric
                  label="累计磁盘硬上限"
                  value={number(currentPreview.budget.total_disk_gib)}
                  unit="GiB"
                  detail={`整批限额 ${number(currentPreview.budget.max_total_disk_gib)} GiB`}
                />
                <Metric
                  label="单进程内存上限峰值"
                  value={number(currentPreview.budget.peak_memory_limit_gib)}
                  unit="GiB"
                  detail="顺序执行取最大值；不是累加内存"
                />
              </div>
              <p className="experiment-copy">
                实际耗时和稀疏分解的额外空间尚不能由硬上限准确预测。确认后服务器会重新检查输入、运行能力和队列容量。
              </p>
              {(currentPreview.warnings || []).map((warning, index) => (
                <Notice kind="warning" key={index}>
                  {printable(warning)}
                </Notice>
              ))}
              <div className="experiment-actions">
                <Button
                  primary
                  busy={busy === "submit"}
                  disabled={Boolean(busy)}
                  onClick={() => void submit()}
                >
                  <Play size={15} />
                  确认提交冻结计划
                </Button>
              </div>
              <p className="experiment-frozen-hash mono">
                计划哈希：{currentPreview.hash}
              </p>
              <div className="experiment-cells">
                {currentPreview.cells.map((cell, index) => (
                  <details key={cell.id}>
                    <summary>
                      组合 {index + 1} ·{" "}
                      {matrixById.get(cell.matrix_id)?.name ||
                        short(cell.matrix_id)}
                    </summary>
                    <Parameters value={cell.parameters} />
                    <p className="subtle-copy">
                      相对于同矩阵首个组合的逐字段变化
                    </p>
                    <ConfigurationDiff
                      changes={cell.configuration_diff || []}
                    />
                    <details>
                      <summary>冻结配置与输入哈希</summary>
                      <Json
                        value={{
                          matrix_hash: cell.matrix_hash,
                          plan_hash: cell.plan_hash,
                          config: cell.config,
                        }}
                      />
                    </details>
                  </details>
                ))}
              </div>
            </>
          ) : (
            <Empty
              title="先冻结，再确认"
              description="选择矩阵与参数，冻结后可逐组合检查配置差异与整批硬预算。任一输入、项目或基础配置变化都会使预览失效。"
            />
          )}
        </Panel>
      </div>

      <Panel
        title="实验与历史基准批次"
        eyebrow="EXPERIMENT HISTORY"
        action={
          <Button busy={listLoading} onClick={refreshList}>
            <RefreshCw size={14} />
            刷新摘要
          </Button>
        }
      >
        {listError ? (
          <Notice kind="error">摘要读取失败：{listError}</Notice>
        ) : null}
        {visiblePage.items.length ? (
          <div className="table-wrap">
            <table className="experiment-batches">
              <caption className="paper-table-caption">冻结实验批次与运行状态汇总</caption>
              <thead>
                <tr>
                  <th>批次 / 冻结时间</th>
                  <th>类型</th>
                  <th>认证 / 全部</th>
                  <th>失败 / 取消 / 超时</th>
                  <th>未结束</th>
                  <th>详情</th>
                </tr>
              </thead>
              <tbody>
                {visiblePage.items.map((batch) => {
                  const counts = experimentSummary(batch.summary);
                  return (
                    <tr
                      key={batch.id}
                      className={selectedId === batch.id ? "selected" : ""}
                    >
                      <td>
                        <b className="mono">{short(batch.id)}</b>
                        <small>{date(batch.created_at)}</small>
                      </td>
                      <td>
                        {batch.experiment_plan_id ? "参数实验" : "历史基准"}
                      </td>
                      <td>
                        {counts.certified} / {counts.total}
                      </td>
                      <td>
                        {counts.failed} / {counts.total}
                      </td>
                      <td>{counts.active}</td>
                      <td>
                        <Button
                          onClick={() => {
                            selectionVersionRef.current += 1;
                            detailRequest.current?.abort();
                            setSelectedId(batch.id);
                            setDetailRevision((value) => value + 1);
                          }}
                        >
                          查看详情
                        </Button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : listLoading ? (
          <p className="experiment-copy" role="status">
            正在读取批次摘要…
          </p>
        ) : (
          <Empty
            title="尚无实验批次"
            description="冻结并确认一个参数实验，运行结果和旧基准批次会显示在这里。"
          />
        )}
        <div className="job-pagination">
          <span className="subtle-copy">
            已加载 {visiblePage.items.length} / {visiblePage.total} 个批次 ·
            摘要不包含完整运行记录
          </span>
          {visiblePage.next_cursor ? (
            <Button
              busy={listLoading}
              disabled={listLoading}
              onClick={() => void fetchPage(visiblePage.next_cursor)}
            >
              加载更多批次
            </Button>
          ) : null}
        </div>
      </Panel>

      {selectedId ? (
        <Panel
          title={`批次详情 · ${short(selectedId)}`}
          eyebrow="ALL RUNS / ALL OUTCOMES"
          action={
            <div className="experiment-actions">
              <Button
                busy={detailLoading}
                onClick={() => {
                  detailRequest.current?.abort();
                  setDetailRevision((value) => value + 1);
                }}
              >
                <RefreshCw size={14} />
                刷新详情
              </Button>
              {visibleDetail && batchNeedsPolling(visibleDetail) ? (
                <Button
                  busy={busy === "cancel"}
                  disabled={Boolean(busy)}
                  onClick={() => void cancel()}
                >
                  <Square size={13} />
                  取消未结束运行
                </Button>
              ) : null}
            </div>
          }
        >
          {detailError ? (
            <Notice kind="error">详情读取失败：{detailError}</Notice>
          ) : null}
          {visibleDetail ? (
            <>
              <Summary summary={visibleDetail.summary} />
              <Notice>
                {batchNeedsPolling(visibleDetail)
                  ? "仅当前选中的活动批次自动更新；全部运行结束后停止轮询。"
                  : "当前批次已结束，自动轮询已停止。"}{" "}
                失败、取消、超时与部分结果始终保留在全部运行分母中。认证成功耗时单独统计；跨目标或资源组合不自动计算加速比。
              </Notice>
              {visibleDetail.groups?.length ? (
                <div className="experiment-groups">
                  {visibleDetail.groups.map((group, index) => (
                    <details
                      key={group.cell_id}
                      open={visibleDetail.groups!.length === 1}
                    >
                      <summary>
                        <FlaskConical size={15} />
                        组合 {index + 1} ·{" "}
                        {matrixById.get(group.matrix_id)?.name ||
                          short(group.matrix_id)}
                        <span className="mono">{short(group.cell_id)}</span>
                      </summary>
                      <Parameters value={group.parameters || {}} />
                      <Summary summary={group.summary} />
                      <ConfigurationDiff
                        changes={group.configuration_diff || []}
                      />
                      <p className="subtle-copy">
                        运行编号：{group.job_ids.map(short).join(" · ") || "—"}
                      </p>
                    </details>
                  ))}
                </div>
              ) : (
                <p className="experiment-copy">
                  历史基准记录保留原始任务及统计；该版本未登记参数组合。
                </p>
              )}
              <div className="section-title experiment-runs-title">
                <div>
                  <span className="eyebrow">COMPLETE RUN LEDGER</span>
                  <h3>全部运行（{visibleDetail.runs?.length ?? 0}）</h3>
                </div>
              </div>
              <div className="table-wrap">
                <table className="experiment-runs">
                  <caption className="paper-table-caption">实验批次的全部运行记录</caption>
                  <thead>
                    <tr>
                      <th>运行</th>
                      <th>组合 / 重复</th>
                      <th>矩阵</th>
                      <th>状态</th>
                      <th>记录耗时 / s</th>
                      <th>错误 / 结果说明</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(visibleDetail.runs || []).map((run) => (
                      <tr key={run.id}>
                        <td>
                          <button
                            className="link-button mono"
                            onClick={() => onOpenJob(run)}
                          >
                            {short(run.id)}
                          </button>
                        </td>
                        <td>
                          {run.experiment?.cell_id
                            ? short(run.experiment.cell_id)
                            : "历史基准"}{" "}
                          / {run.experiment?.repeat ?? "—"}
                        </td>
                        <td>
                          {matrixById.get(run.matrix_id)?.name ||
                            short(run.matrix_id)}
                        </td>
                        <td>
                          <Badge status={run.status} />
                        </td>
                        <td className="mono">
                          {number(
                            run.metrics?.walltime_seconds ??
                              run.result?.metrics?.walltime_seconds,
                          )}
                        </td>
                        <td>
                          {run.error?.code ||
                            run.result?.validation?.status ||
                            "—"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="benchmark-export">
                <Button
                  disabled={Boolean(busy)}
                  busy={busy === "report"}
                  onClick={() => void exportReport()}
                >
                  <FileText size={14} />
                  生成完整实验报告
                </Button>
              </div>
              <details className="experiment-base">
                <summary>冻结规格、预算与完整批次记录</summary>
                <Json value={visibleDetail} />
              </details>
            </>
          ) : (
            <p className="experiment-copy" role="status">
              正在读取所选批次的完整运行…
            </p>
          )}
        </Panel>
      ) : null}
    </div>
  );
}
