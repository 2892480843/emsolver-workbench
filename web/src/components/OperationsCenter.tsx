import { useEffect, useRef, useState } from "react";
import { api, number, type Entity } from "../api";
import { operationsPolicyPatch, projectDraft } from "../models/workspace";
import { Button, Field, Metric, Notice, Panel } from "../ui";
import { ClusterWorkers } from "./ClusterWorkers";

export function OperationsCenter({
  onSaved,
  identity = null,
}: {
  onSaved: () => void;
  identity?: Entity | null;
}) {
  const [summary, setSummary] = useState<Entity | null>(null);
  const [rows, setRows] = useState<Entity[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [selected, setSelected] = useState("");
  const [draft, setDraft] = useState(projectDraft({}));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);
  const request = useRef<AbortController | null>(null);
  const load = async (next?: string) => {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setBusy(true);
    setError("");
    try {
      const params = new URLSearchParams({ limit: "50" });
      if (next) params.set("cursor", next);
      const [metrics, page] = await Promise.all([
        api<Entity>("/operations/summary", { signal: controller.signal }),
        api<Entity>(`/operations/project-policies?${params}`, {
          signal: controller.signal,
        }),
      ]);
      if (controller.signal.aborted) return;
      setSummary(metrics);
      if (!next) {
        setSelected("");
        setSaved(false);
      }
      setRows((current) => (next ? [...current, ...page.items] : page.items));
      setCursor(page.next_cursor);
    } catch (failure) {
      if (!controller.signal.aborted)
        setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  };
  useEffect(() => {
    void load();
    return () => request.current?.abort();
  }, []);
  const current = rows.find((row) => row.project_id === selected);
  const save = async () => {
    if (!current || busy) return;
    const controller = new AbortController();
    request.current?.abort();
    request.current = controller;
    setBusy(true);
    setSaved(false);
    setError("");
    try {
      const body = operationsPolicyPatch(selected, draft);
      const row = await api<Entity>("/operations/project-policies", {
        method: "PATCH",
        body: JSON.stringify(body),
        signal: controller.signal,
      });
      if (controller.signal.aborted) return;
      setRows((items) =>
        items.map((item) => (item.project_id === row.project_id ? row : item)),
      );
      setSaved(true);
      onSaved();
    } catch (failure) {
      if (!controller.signal.aborted)
        setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  };
  return (
    <>
      <Panel
        title="运维中心"
        eyebrow="OPERATIONS · NO DEFAULT BUSINESS ACCESS"
        action={
          <Button disabled={busy} onClick={() => void load()}>
            刷新运维汇总
          </Button>
        }
      >
        <p className="panel-copy">
          运维角色仅查看聚合指标、脱敏审计计数与项目编号对应的配额。项目名称、成员、输入和业务结果不会在此返回。业务读取或下载需另获项目成员授权；完整备份下载仅限显式工作台所有者。
        </p>
        {error ? <Notice kind="error">{error}</Notice> : null}
        {saved ? (
          <Notice>配额策略已保存并留存操作审计；项目成员权限未改变。</Notice>
        ) : null}
        {summary ? (
          <>
            <div className="governance-metrics">
              <Metric label="项目总数" value={number(summary.projects)} />
              <Metric label="活跃任务" value={number(summary.active_jobs)} />
              <Metric label="任务总数" value={number(summary.jobs)} />
              <Metric
                label="存储可用"
                value={number(summary.health?.storage_free_bytes / 1024 ** 3)}
                unit="GiB"
              />
            </div>
            <p className="subtle-copy">
              队列 {summary.health?.queue_available ? "可用" : "降级"} · 后端{" "}
              {summary.health?.backend_runnable ? "可运行" : "不可用"} · 最近{" "}
              {summary.audit?.records_counted ?? 0} 条审计中拒绝{" "}
              {summary.audit?.result_counts?.denied ?? 0}{" "}
              次（最多1000条的聚合窗口，不提供主体、业务对象或原文）。
            </p>
          </>
        ) : null}
        <Field
          label="项目编号"
          hint="仅展示不透明编号；此处调整配额不授予任何项目业务角色"
        >
          <select
            value={selected}
            disabled={busy}
            onChange={(event) => {
              const id = event.target.value;
              setSelected(id);
              setSaved(false);
              setDraft(
                projectDraft(rows.find((row) => row.project_id === id) || {}),
              );
            }}
          >
            <option value="">选择需管理的项目编号</option>
            {rows.map((row) => (
              <option key={row.project_id} value={row.project_id}>
                {row.project_id}
              </option>
            ))}
          </select>
        </Field>
        {cursor ? (
          <Button disabled={busy} onClick={() => void load(cursor)}>
            加载更多项目编号
          </Button>
        ) : null}
        {current ? (
          <>
            <p className="subtle-copy">
              输入 {number(current.usage.input_bytes)} 字节 · 输出{" "}
              {number(current.usage.output_bytes)} 字节 · 预约输出{" "}
              {number(current.usage.reserved_output_bytes)} 字节 · 排队{" "}
              {number(current.usage.queued_jobs)}{" "}
              项。按项目逻辑配额计量，不等于跨项目去重后的物理磁盘总量。
            </p>
            <div className="form-grid">
              {[
                ["storage", "存储配额 / GiB"],
                ["queued", "最大排队任务 / 项"],
                ["priority", "队列优先级 / -10～10"],
                ["retention", "结果保留周期 / 天"],
              ].map(([key, label]) => (
                <Field key={key} label={label}>
                  <input
                    type="number"
                    step={key === "storage" ? "any" : "1"}
                    disabled={busy}
                    value={draft[key as keyof typeof draft] as string}
                    onChange={(event) => {
                      setSaved(false);
                      setDraft((value) => ({
                        ...value,
                        [key]: event.target.value,
                      }));
                    }}
                  />
                </Field>
              ))}
            </div>
            <Button disabled={busy} busy={busy} onClick={() => void save()}>
              保存项目配额策略
            </Button>
          </>
        ) : null}
      </Panel>
      <ClusterWorkers
        key={identity?.subject || "unknown"}
        identity={identity}
      />
    </>
  );
}
