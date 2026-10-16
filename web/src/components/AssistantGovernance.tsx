import { useEffect, useRef, useState } from "react";
import { BookOpen, RefreshCw, Search, Sparkles } from "lucide-react";
import { api, number, post, type Entity } from "../api";
import {
  safeSourceUrl,
  projectRole,
  recordAdviceEvaluation,
} from "../models/workspace";
import { ConfigurationDiff } from "./ConfigurationDiff";
import { Badge, Button, Empty, Field, Json, Notice, Panel } from "../ui";

type Props = {
  selectedJob: Entity | null;
  config: Entity;
  jobs: Entity[];
  projects: Entity[];
  identity: Entity | null;
  notify: (message: string, error?: boolean) => void;
};
const message = (error: unknown) =>
  error instanceof Error ? error.message : String(error);
const fallbackLabels: Record<string, string> = {
  not_configured: "未配置本地模型，已返回离线规则解释",
  license_not_approved: "模型许可尚未批准，已返回规则解释",
  timeout: "本地模型响应超时，已返回规则解释",
  invalid_response: "模型输出未通过结构检查，已返回规则解释",
  unavailable: "本地模型当前不可用，已返回规则解释",
};

export function KnowledgeEvidence({ entries }: { entries: Entity[] }) {
  return (
    <div className="knowledge-registry">
      {entries.map((entry, index) => (
        <details key={entry.id || index}>
          <summary>
            <BookOpen size={15} />
            <span>{entry.title}</span>
            <small>v{entry.version || "—"}</small>
          </summary>
          <p>{entry.summary}</p>
          {entry.prerequisites?.length ? (
            <Notice>
              <b>适用前提</b>
              <ul>
                {entry.prerequisites.map((value: string, i: number) => (
                  <li key={i}>{value}</li>
                ))}
              </ul>
            </Notice>
          ) : null}
          {entry.risks?.length ? (
            <Notice kind="warning">
              <b>风险与限制</b>
              <ul>
                {entry.risks.map((value: string, i: number) => (
                  <li key={i}>{value}</li>
                ))}
              </ul>
            </Notice>
          ) : null}
          {(
            entry.sources ||
            (entry.url ? [{ title: entry.title, url: entry.url }] : [])
          ).map((source: Entity, i: number) => (
            <div className="knowledge-source" key={i}>
              {safeSourceUrl(source.url) ? (
                <a
                  href={safeSourceUrl(source.url)}
                  target="_blank"
                  rel="noreferrer"
                >
                  {source.title || source.url}
                </a>
              ) : (
                <span>{source.title || "未提供可打开的来源"}</span>
              )}
              <small>
                来源版本 {source.version || "未登记"} · 核对日期{" "}
                {source.accessed_at || "未登记"}
              </small>
            </div>
          ))}
          <p className="subtle-copy">
            条目 {entry.id || "—"} · 知识库版本 {entry.registry_version || "—"}
          </p>
        </details>
      ))}
    </div>
  );
}

export function AssistantGovernance({
  selectedJob,
  config,
  jobs,
  projects,
  identity,
  notify,
}: Props) {
  const [explanation, setExplanation] = useState<{
    fingerprint: string;
    value: Entity;
  } | null>(null);
  const [explaining, setExplaining] = useState(false);
  const [explainError, setExplainError] = useState("");
  const fingerprint = JSON.stringify(
    selectedJob
      ? {
          id: selectedJob.id,
          status: selectedJob.status,
          metrics: selectedJob.metrics,
          error: selectedJob.error,
        }
      : { config },
  );
  const current = useRef(fingerprint);
  current.current = fingerprint;
  const request = useRef<AbortController | null>(null);
  useEffect(() => {
    request.current?.abort();
    setExplaining(false);
    setExplainError("");
    return () => request.current?.abort();
  }, [fingerprint]);
  const explain = async () => {
    if (explaining) return;
    const expected = fingerprint;
    const controller = new AbortController();
    request.current?.abort();
    request.current = controller;
    setExplaining(true);
    setExplainError("");
    try {
      const value = await api("/diagnostics/explain", {
        method: "POST",
        body: JSON.stringify(
          selectedJob ? { job_id: selectedJob.id } : { config },
        ),
        signal: controller.signal,
      });
      if (!controller.signal.aborted && current.current === expected)
        setExplanation({ fingerprint: expected, value });
    } catch (failure) {
      if (!controller.signal.aborted && current.current === expected)
        setExplainError(message(failure));
    } finally {
      if (!controller.signal.aborted && current.current === expected)
        setExplaining(false);
    }
  };
  const visible =
    explanation?.fingerprint === fingerprint ? explanation.value : null;
  const detail = visible?.explanation;
  return (
    <>
      <Panel
        title="本地 AI 解释与规则降级"
        eyebrow="BOUNDED EXPLANATION"
        action={
          <Button busy={explaining} onClick={() => void explain()}>
            <Sparkles size={15} />
            生成解释
          </Button>
        }
      >
        <p className="panel-copy">
          AI（人工智能）只解释脱敏的诊断摘要。数值认证、错误状态和建议白名单由计算与规则服务决定；模型不可用时继续提供规则解释。
        </p>
        {selectedJob ? (
          <div className="experiment-actions">
            <span className="mono">上下文 {selectedJob.id.slice(0, 14)}</span>
            <Badge status={selectedJob.status} />
          </div>
        ) : (
          <p className="subtle-copy">当前上下文：尚未运行的求解配置。</p>
        )}
        {explainError ? (
          <Notice kind="error">解释请求失败：{explainError}</Notice>
        ) : null}
        {detail ? (
          <>
            <div className="explanation-provenance">
              <b>
                {detail.source === "local_model"
                  ? "本地模型解释"
                  : "离线规则解释"}
              </b>
              <span>模板 {detail.template_version || "—"}</span>
              <span>规则 {visible?.rule_version || "—"}</span>
            </div>
            {detail.fallback_reason ? (
              <Notice>
                {fallbackLabels[detail.fallback_reason] ||
                  `规则降级原因：${detail.fallback_reason}`}
              </Notice>
            ) : null}
            {detail.model ? (
              <p className="subtle-copy">
                模型 {detail.model.id} · 版本 {detail.model.version} · 许可{" "}
                {detail.model.license_id}
              </p>
            ) : null}
            <p className="explanation-summary">{detail.summary}</p>
            {(detail.items || []).map((item: Entity, index: number) => (
              <div className="explanation-item" key={index}>
                <b>{item.rule_id}</b>
                <p>{item.text}</p>
              </div>
            ))}
            {visible?.knowledge?.length ? (
              <KnowledgeEvidence entries={visible.knowledge} />
            ) : null}
          </>
        ) : (
          <p className="subtle-copy">
            点击生成后显示解释来源、模型与提示模板版本，以及降级原因。
          </p>
        )}
      </Panel>
      <AdviceEffects
        jobs={jobs}
        sourceJobId={selectedJob?.id || ""}
        notify={notify}
        canWriteProject={(id) =>
          ["analyst", "admin"].includes(
            projectRole(
              projects.find((project) => project.id === id),
              identity,
            ) || "",
          )
        }
      />
      <KnowledgeRegistry />
    </>
  );
}

function KnowledgeRegistry() {
  const [draft, setDraft] = useState("");
  const [query, setQuery] = useState("");
  const [entries, setEntries] = useState<Entity[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    api<Entity[]>(
      `/knowledge${query ? `?${new URLSearchParams({ q: query })}` : ""}`,
      { signal: controller.signal },
    )
      .then((items) => {
        if (!controller.signal.aborted) {
          setEntries(items);
          setError("");
        }
      })
      .catch((failure) => {
        if (!controller.signal.aborted) setError(message(failure));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [query]);
  return (
    <Panel title="算法知识与故障处置" eyebrow="VERSIONED KNOWLEDGE">
      <form
        className="governance-search"
        onSubmit={(event) => {
          event.preventDefault();
          setQuery(draft.trim());
        }}
      >
        <div className="search-field">
          <Search size={16} />
          <input
            aria-label="检索知识条目"
            maxLength={200}
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            placeholder="按算法、错误或适用条件检索"
          />
        </div>
        <Button type="submit" busy={loading}>
          检索知识
        </Button>
      </form>
      {error ? <Notice kind="error">知识读取失败：{error}</Notice> : null}
      {entries.length ? (
        <KnowledgeEvidence entries={entries} />
      ) : (
        <Empty
          title={loading ? "正在读取知识条目" : "没有匹配条目"}
          description="知识条目保留适用前提、风险、官方出处及版本。"
        />
      )}
    </Panel>
  );
}

function AdviceEffects({
  jobs,
  sourceJobId,
  notify,
  canWriteProject,
}: {
  jobs: Entity[];
  sourceJobId: string;
  notify: Props["notify"];
  canWriteProject: (id: string) => boolean;
}) {
  const [records, setRecords] = useState<Entity[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [selection, setSelection] = useState<{
    id: string;
    detail: Entity;
  } | null>(null);
  const [jobId, setJobId] = useState("");
  const [onlyCurrent, setOnlyCurrent] = useState(false);
  const [revision, setRevision] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    const params =
      onlyCurrent && sourceJobId
        ? `?${new URLSearchParams({ source_job_id: sourceJobId })}`
        : "";
    api<Entity[]>(`/advice/effects${params}`, { signal: controller.signal })
      .then((items) => {
        if (!controller.signal.aborted) {
          setRecords(items);
          setError("");
        }
      })
      .catch((failure) => {
        if (!controller.signal.aborted) setError(message(failure));
      });
    return () => controller.abort();
  }, [onlyCurrent, sourceJobId, revision]);
  useEffect(() => {
    const controller = new AbortController();
    if (!selectedId) return () => controller.abort();
    api(`/advice/effects/${encodeURIComponent(selectedId)}`, {
      signal: controller.signal,
    })
      .then((detail) => {
        if (!controller.signal.aborted) {
          setSelection({ id: selectedId, detail });
          setError("");
        }
      })
      .catch((failure) => {
        if (!controller.signal.aborted) setError(message(failure));
      });
    return () => controller.abort();
  }, [selectedId, revision]);
  const selected = selection?.id === selectedId ? selection.detail : null;
  const derivedRecords = [
    ...new Map(
      records
        .filter(
          (record) => !record.record_type || record.record_type === "derived",
        )
        .map((record) => [record.effect_id || record.id, record]),
    ).values(),
  ];
  const evaluation = selected?.evaluation;
  const writable = Boolean(selected && canWriteProject(selected.project_id));
  const selectionRef = useRef(selectedId);
  selectionRef.current = selectedId;
  const record = async (id: string) => {
    const detail = await recordAdviceEvaluation(
      id,
      api,
      new AbortController().signal,
    );
    if (selectionRef.current === id) setSelection({ id, detail });
    setRevision((value) => value + 1);
  };
  const evaluate = async () => {
    if (!selectedId || !writable || busy) return;
    setBusy(true);
    try {
      await record(selectedId);
      notify("当前建议效果已记录；未变化的观测不会重复追加");
    } catch (failure) {
      setError(message(failure));
      notify(message(failure), true);
    } finally {
      setBusy(false);
    }
  };
  const link = async () => {
    if (!selectedId || !jobId.trim() || busy || !writable) return;
    setBusy(true);
    try {
      await post(`/advice/effects/${encodeURIComponent(selectedId)}/link`, {
        job_id: jobId.trim(),
      });
      await record(selectedId);
      notify("派生运行已关联，服务端已记录建议效果证据");
    } catch (failure) {
      setError(message(failure));
      notify(message(failure), true);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Panel
      title="建议采用与效果记录"
      eyebrow="ADVICE EFFECTS"
      action={
        <Button onClick={() => setRevision((value) => value + 1)}>
          <RefreshCw size={14} />
          刷新效果
        </Button>
      }
    >
      <p className="panel-copy">
        建议采用会创建新配置并保留原运行。关联由新配置产生的真实运行后，再检查认证状态、目标与资源条件，判断是否可以比较耗时。
      </p>
      {sourceJobId ? (
        <label className="checkbox">
          <input
            type="checkbox"
            checked={onlyCurrent}
            onChange={(event) => {
              setOnlyCurrent(event.target.checked);
              setSelectedId("");
            }}
          />
          <span>仅查看当前源运行的建议采用记录</span>
        </label>
      ) : null}
      {error ? <Notice kind="error">效果记录读取失败：{error}</Notice> : null}
      {derivedRecords.length ? (
        <div className="advice-effect-list">
          {derivedRecords.map((record) => {
            const id = record.effect_id || record.id;
            return (
              <button
                key={record.id || id}
                className={`project-directory-item ${selectedId === id ? "selected" : ""}`}
                onClick={() => {
                  setSelectedId(id);
                  setJobId("");
                }}
              >
                <span>
                  <b>建议 {String(id).slice(0, 14)}</b>
                  <small>
                    源运行 {record.source_job_id || "—"} · 派生配置{" "}
                    {record.derived_config_id || "—"}
                  </small>
                </span>
              </button>
            );
          })}
        </div>
      ) : (
        <p className="subtle-copy">
          尚无建议采用记录。先对已有运行采用一条规则建议，再运行新配置。
        </p>
      )}
      {selected ? (
        <div className="advice-effect-detail">
          <ConfigurationDiff
            changes={
              selected.configuration_diff ||
              selected.adoption?.configuration_diff ||
              []
            }
          />
          {evaluation ? (
            <div className="advice-evaluation">
              <h3>
                {evaluation.comparable
                  ? "服务端已确认本次比较条件"
                  : "当前运行不支持直接性能结论"}
              </h3>
              <p>{evaluation.reason}</p>
              <div className="table-wrap">
                <table>
                  <caption className="paper-table-caption">规则建议采用前后的运行指标</caption>
                  <thead>
                    <tr>
                      <th>指标</th>
                      <th>源运行</th>
                      <th>派生运行</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr>
                      <th>执行状态</th>
                      <td>
                        <Badge status={evaluation.before?.status} />
                      </td>
                      <td>
                        <Badge status={evaluation.after?.status} />
                      </td>
                    </tr>
                    <tr>
                      <th>数值验证</th>
                      <td>{evaluation.before?.numerical_status || "未取得"}</td>
                      <td>{evaluation.after?.numerical_status || "未取得"}</td>
                    </tr>
                    <tr>
                      <th>目标认证</th>
                      <td>
                        {evaluation.before?.target_certification || "未取得"}
                      </td>
                      <td>
                        {evaluation.after?.target_certification || "未取得"}
                      </td>
                    </tr>
                    <tr>
                      <th>记录耗时 / s</th>
                      <td>{number(evaluation.before?.walltime_seconds)}</td>
                      <td>{number(evaluation.after?.walltime_seconds)}</td>
                    </tr>
                    <tr>
                      <th>峰值内存 / MiB</th>
                      <td>
                        {number(
                          typeof evaluation.before?.peak_memory_bytes ===
                            "number"
                            ? evaluation.before.peak_memory_bytes / 1024 ** 2
                            : null,
                        )}
                      </td>
                      <td>
                        {number(
                          typeof evaluation.after?.peak_memory_bytes ===
                            "number"
                            ? evaluation.after.peak_memory_bytes / 1024 ** 2
                            : null,
                        )}
                      </td>
                    </tr>
                    <tr>
                      <th>最大残差</th>
                      <td>{number(evaluation.before?.residual_max)}</td>
                      <td>{number(evaluation.after?.residual_max)}</td>
                    </tr>
                  </tbody>
                </table>
              </div>
              {evaluation.comparable &&
              typeof evaluation.speedup === "number" ? (
                <p className="explanation-summary">
                  源运行耗时 / 派生运行耗时 ={" "}
                  <b>{number(evaluation.speedup)}×</b>
                </p>
              ) : null}
            </div>
          ) : (
            <p className="subtle-copy">尚无已关联运行的评估结果。</p>
          )}
          <div className="governance-search">
            <Field
              label="关联派生运行编号"
              hint="只能关联实际使用派生配置的运行；服务端会核验引用关系"
            >
              <input
                list="effect-run-options"
                value={jobId}
                onChange={(event) => setJobId(event.target.value)}
                placeholder="选择或粘贴完整运行编号"
              />
            </Field>
            <Button
              disabled={!jobId.trim() || busy || !writable}
              busy={busy}
              onClick={() => void link()}
            >
              关联并评估
            </Button>
            <Button
              disabled={busy || !writable}
              busy={busy}
              onClick={() => void evaluate()}
            >
              记录当前效果
            </Button>
          </div>
          <datalist id="effect-run-options">
            {jobs.map((job) => (
              <option key={job.id} value={job.id}>
                {job.status}
              </option>
            ))}
          </datalist>
          {!writable ? (
            <Notice>
              当前身份可查看即时效果预览。关联运行及追加效果记录需要项目分析者或管理员权限。
            </Notice>
          ) : null}
          <Notice>
            仅在服务端确认条件可比且认证通过时，耗时变化才能作为效果证据。失败或未认证运行仍保留在记录中。
          </Notice>
          <details>
            <summary>完整采用链与效果评估记录</summary>
            <Json value={selected} />
          </details>
        </div>
      ) : null}
    </Panel>
  );
}
