import { useId } from "react";
import type { Entity } from "../api";
import type { WorkspaceSummary } from "../models/jobs";
import {
  buildRecentActivity,
  buildStatusDistribution,
  statusFilterFor,
} from "../models/overview";
import { ArrowButton, Badge, Empty, Panel } from "../ui";

type Props = {
  summary: WorkspaceSummary | null;
  jobs: Entity[];
  total: number | undefined;
  hasMore: boolean;
  matrices: Entity[];
  onOpenJob: (job: Entity) => void;
  onSelectStatus: (status: string) => void;
  onShowMatrices: () => void;
};

const dateFormat = new Intl.DateTimeFormat("zh-CN", {
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});

export function OverviewInsights({
  summary,
  jobs,
  total,
  hasMore,
  matrices,
  onOpenJob,
  onSelectStatus,
  onShowMatrices,
}: Props) {
  const captionId = useId();
  const distribution = buildStatusDistribution(summary);
  const activity = buildRecentActivity(jobs, total, hasMore, 4);
  const maxCount = Math.max(1, ...distribution.rows.map(({ count }) => count));
  const matrixNames = new Map(
    matrices.map((matrix) => [matrix.id, matrix.name]),
  );
  return (
    <div className="overview-insights">
      <Panel
        title="任务状态分布"
        className="overview-distribution"
        action={
          <ArrowButton onClick={() => onSelectStatus("ALL")}>
            查看任务
          </ArrowButton>
        }
      >
        <p className="overview-section-intro">
          依据当前项目范围的服务端汇总，点击状态可筛选任务。
        </p>
        {distribution.kind === "unavailable" ? (
          <Empty
            title="等待状态汇总"
            description="汇总加载后显示各状态的实际任务数量。"
          />
        ) : distribution.kind === "empty" ? (
          <Empty
            title="当前范围暂无任务"
            description="导入矩阵并规划计算后，任务状态会在此汇总。"
          />
        ) : (
          <figure
            className="paper-figure overview-distribution-figure"
            aria-labelledby={captionId}
          >
            <div className="overview-status-bars">
              {distribution.rows
                .filter(({ count }) => count > 0)
                .map(({ status, count }, index) => (
                  <button
                    type="button"
                    key={status}
                    className="overview-status-row"
                    onClick={() => onSelectStatus(statusFilterFor(status))}
                  >
                    <span className="overview-status-name">
                      <Badge status={status} />
                    </span>
                    <span className="overview-status-track" aria-hidden="true">
                      <span
                        className="overview-status-fill"
                        style={{
                          width: `${(count / maxCount) * 100}%`,
                          backgroundColor: ["#111111", "#6B6B66", "#D4D4D0"][
                            index % 3
                          ],
                        }}
                      />
                    </span>
                    <span className="overview-status-count">
                      {count.toLocaleString("zh-CN")}
                      <small> 项</small>
                    </span>
                  </button>
                ))}
            </div>
            <figcaption id={captionId} className="paper-caption">
              当前范围的非零状态计数；条长按最大类别归一化。
            </figcaption>
          </figure>
        )}
        {distribution.kind === "incomplete" ? (
          <p className="overview-data-note" role="status">
            状态明细与汇总暂未对齐，当前仅展示有效计数，刷新后重试。
          </p>
        ) : null}
        <div className="overview-distribution-footer">
          <span>汇总任务</span>
          <strong>
            {distribution.expectedTotal?.toLocaleString("zh-CN") ?? "—"}
            <small> 项</small>
          </strong>
        </div>
      </Panel>
      <Panel
        title="实验动态"
        className="overview-activity"
        action={
          <span className="overview-scope-label">
            {activity.scope === "all" ? "当前范围" : "已加载任务"}
          </span>
        }
      >
        <p className="overview-section-intro">
          按创建时间排列
          {activity.scope === "loaded"
            ? `，基于已加载的 ${activity.loadedCount} 项任务`
            : ""}
          {activity.totalCount !== null && activity.scope === "loaded"
            ? `（共 ${activity.totalCount} 项）`
            : ""}
          。
        </p>
        {activity.items.length ? (
          <ol className="overview-activity-list">
            {activity.items.map(({ job, createdAt }, index) => (
              <li key={job.id} className="overview-activity-item">
                <span className="overview-activity-index" aria-hidden="true">
                  {String(index + 1).padStart(2, "0")}
                </span>
                <div className="overview-activity-content">
                  <div className="overview-activity-heading">
                    <button
                      type="button"
                      className="overview-activity-link"
                      onClick={() => onOpenJob(job)}
                    >
                      {matrixNames.get(job.matrix_id) || "稀疏特征值实验"}
                    </button>
                    <Badge status={job.status} />
                  </div>
                  <div className="overview-activity-meta">
                    <span className="mono" title={String(job.id)}>
                      {String(job.id).slice(0, 12)}
                    </span>
                    <span>{job.config?.solver?.backend || "后端未记录"}</span>
                    {createdAt !== null ? (
                      <time
                        dateTime={new Date(createdAt).toISOString()}
                        title={new Date(createdAt).toLocaleString("zh-CN")}
                      >
                        {dateFormat.format(createdAt)}
                      </time>
                    ) : (
                      <span>创建时间未记录</span>
                    )}
                  </div>
                </div>
              </li>
            ))}
          </ol>
        ) : (
          <Empty
            title="暂无任务活动"
            description="当前已加载范围没有计算任务。"
          />
        )}
        <div className="overview-activity-footer">
          <span>
            当前输入资产{" "}
            <strong>{matrices.length.toLocaleString("zh-CN")}</strong> 项矩阵
          </span>
          <ArrowButton onClick={onShowMatrices}>查看矩阵</ArrowButton>
        </div>
      </Panel>
    </div>
  );
}
