import { number, type Entity } from "../api";
import type { WorkspaceSummary } from "../models/jobs";
import { Badge } from "../ui";

export function WorkbenchActions({
  page,
  matrix,
  job,
  result,
  config,
  plan,
  summary,
  onNavigate,
}: {
  page: string;
  matrix?: Entity;
  job: Entity | null;
  result: Entity | null;
  config: Entity;
  plan: Entity | null;
  summary: WorkspaceSummary | null;
  onNavigate: (page: string) => void;
}) {
  const inputName =
    matrix?.name || (matrix?.id ? String(matrix.id).slice(0, 15) : null);
  const target =
    config.target?.mode === "interval_all"
      ? `区间 [${number(config.target.interval?.lower)}, ${number(config.target.interval?.upper)})`
      : `最近 ${number(config.target?.k)} 个模式`;
  const validation = result?.validation;
  const actions = [
    {
      page: "matrices",
      label: inputName ? "草稿输入" : "矩阵资产",
      value: inputName || `${number(summary?.matrices)} 个已登记矩阵`,
      detail: inputName
        ? `当前草稿 · ${number(matrix?.n)} 阶 / ${matrix?.dtype || "类型未记录"}`
        : "导入、来源与结构诊断",
    },
    {
      page: "configure",
      label: "配置草稿",
      value: target,
      detail: plan
        ? `草稿预检计划 ${String(plan.id).slice(0, 14)}`
        : `草稿 · ${number(config.resources?.memory_limit_gib)} GiB / ${number(config.resources?.threads)} 线程`,
    },
    {
      page: "jobs",
      label: job ? "选中任务" : "任务中心",
      value: job ? (
        <Badge status={job.status} />
      ) : (
        `${number(summary?.active_jobs)} 项活动任务`
      ),
      detail: job
        ? `${String(job.id).slice(0, 14)} · ${job.config?.solver?.backend || "后端未记录"}`
        : "查看队列、阶段与运行事件",
    },
    {
      page: "results",
      label: job ? "选中任务结果" : "结果审查",
      value: job ? (
        validation ? (
          <Badge status={validation.target_certification || "UNVERIFIED"} />
        ) : ["FAILED", "CANCELLED", "TIMED_OUT"].includes(job.status) ? (
          "无可用数值结果"
        ) : (
          "结果尚未加载"
        )
      ) : (
        `${number(summary?.certified_jobs)} 次验证通过`
      ),
      detail: job
        ? "目标认证状态 · 查看独立验证证据"
        : "当前范围的历史验证通过记录",
    },
  ];

  return (
    <nav className="workbench-actions" aria-label="工作区快捷入口">
      {actions.map((action, index) => (
        <button
          type="button"
          className="workbench-action"
          key={action.page}
          aria-current={page === action.page ? "page" : undefined}
          onClick={() => onNavigate(action.page)}
        >
          <span className="workbench-action-label">
            <span aria-hidden="true">{String(index + 1).padStart(2, "0")}</span>
            {action.label}
          </span>
          <strong
            className="workbench-action-value"
            title={typeof action.value === "string" ? action.value : undefined}
          >
            {action.value}
          </strong>
          <small>{action.detail}</small>
        </button>
      ))}
    </nav>
  );
}
