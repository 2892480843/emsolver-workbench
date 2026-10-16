import { useRef, useState } from "react";
import { api, post, type Entity } from "../api";
import { canWarmStart, canExactResume } from "../models/workspace";
import { Button, Notice } from "../ui";

type Props = {
  job: Entity;
  canRun: boolean;
  canManageRetention: boolean;
  onUpdated: (job: Entity) => void;
  onRestarted: (job: Entity, select: boolean) => void;
  notify: (message: string, error?: boolean) => void;
};
export function JobLifecycleControls({
  job,
  canRun,
  canManageRetention,
  onUpdated,
  onRestarted,
  notify,
}: Props) {
  const [busy, setBusy] = useState("");
  const current = useRef(job.id);
  current.current = job.id;
  const retained = job.retained !== false;
  const warm = async (exact = false) => {
    if (busy || !canRun || !canWarmStart(job)) return;
    const source = job.id;
    setBusy("warm");
    try {
      const created = await post(
        `/jobs/${encodeURIComponent(source)}/restart`,
        { use_checkpoint: true, resume_mode: exact ? "exact_block" : "subspace" },
      );
      onRestarted(created, current.current === source);
      notify(
        exact ? "完整状态已预检，创建关联的精确续算任务；工作进程将复核输入与运行环境" : "检查点已校验，创建了关联的新热启动任务；原运行保留且未作精确续算",
      );
    } catch (error) {
      notify(error instanceof Error ? error.message : String(error), true);
    } finally {
      setBusy("");
    }
  };
  const retain = async () => {
    if (busy || !canManageRetention) return;
    setBusy("retention");
    try {
      const updated = await api(
        `/jobs/${encodeURIComponent(job.id)}/retention`,
        { method: "PATCH", body: JSON.stringify({ retained: !retained }) },
      );
      onUpdated(updated);
      notify(
        retained
          ? "该任务允许按项目周期回收结果；活动与依赖保护仍然有效"
          : "该任务已标记保留，结果不会按保留周期回收",
      );
    } catch (error) {
      notify(error instanceof Error ? error.message : String(error), true);
    } finally {
      setBusy("");
    }
  };
  return (
    <div className="job-lifecycle-controls">
      <div className="experiment-actions">
        {canWarmStart(job) ? (
          <Button
            disabled={!canRun || Boolean(busy)}
            busy={busy === "warm"}
            onClick={() => void warm()}
          >
            校验检查点并创建热启动任务
          </Button>
        ) : null}
        {canExactResume(job) ? <Button disabled={!canRun || Boolean(busy)} busy={busy === "warm"} onClick={() => void warm(true)}>
          校验完整状态并精确续算
        </Button> : null}
        <span className="subtle-copy">
          结果保留：{retained ? "保留" : "按项目周期回收"}
          {job.artifacts_state ? ` · 产物 ${job.artifacts_state}` : ""}
        </span>
        {canManageRetention ? (
          <Button
            disabled={Boolean(busy)}
            busy={busy === "retention"}
            onClick={() => void retain()}
          >
            {retained ? "允许按周期回收" : "保留任务结果"}
          </Button>
        ) : null}
      </div>
      {canWarmStart(job) ? (
        <p className="subtle-copy">
          热启动使用服务端存在且兼容的子空间检查点，为相同冻结配置创建新任务；不是从原迭代位置精确续算。
        </p>
      ) : null}
      {["EXPIRED", "MISSING"].includes(job.artifacts_state) ? (
        <Notice kind="warning">
          该任务的数值产物已过期或缺失。历史状态和配置保留；不能从缺失检查点热启动。
        </Notice>
      ) : null}
      {job.resumption ? (
        <Notice>
          此任务从 {job.resumption.source_job_id}{" "}
          {job.resumption.exact_resume ? "的完整块状态恢复，原运行仍保留；精确续算：是（配置、输入及环境复核通过后执行）。" : "的子空间热启动，原运行仍保留；精确续算：否。"}
        </Notice>
      ) : null}
    </div>
  );
}
