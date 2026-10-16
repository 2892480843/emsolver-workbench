import { useEffect, useRef, useState } from "react";
import { api, number, type Entity } from "../api";
import {
  workerConnection,
  workerEnrollment,
  workerTime,
  type ClusterStatus,
  type ClusterWorker,
  type WorkerDraft,
} from "../models/cluster";
import { Button, Empty, Field, Metric, Notice, Panel } from "../ui";

type Snapshot = {
  subject: string;
  status: ClusterStatus;
  workers: ClusterWorker[];
  projects: Entity[];
};
type Credential = {
  subject: string;
  workerId: string;
  name: string;
  token: string;
};
const initialDraft: WorkerDraft = {
  name: "",
  projectIds: [],
  threads: "2",
  memory: "16",
  disk: "20",
};
const message = (failure: unknown) =>
  failure instanceof Error ? failure.message : String(failure);

export function ClusterWorkers({ identity }: { identity: Entity | null }) {
  const canManage = identity?.global_admin === true;
  const subject = identity?.subject || "";
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [actionError, setActionError] = useState("");
  const [action, setAction] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [draft, setDraft] = useState<WorkerDraft>(initialDraft);
  const [credential, setCredential] = useState<Credential | null>(null);
  const [confirmRevoke, setConfirmRevoke] = useState("");
  const actionRequest = useRef<AbortController | null>(null);

  useEffect(() => {
    let stopped = false;
    let controller: AbortController | null = null;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const load = async () => {
      controller = new AbortController();
      const current = controller;
      const deadline = setTimeout(() => current.abort(), 8000);
      setLoading(true);
      try {
        const status = await api<ClusterStatus>("/cluster/status", {
          signal: current.signal,
        });
        const [workers, projects] =
          status.enabled && canManage
            ? await Promise.all([
                api<ClusterWorker[]>("/cluster/workers", {
                  signal: current.signal,
                }),
                api<Entity[]>("/projects", { signal: current.signal }),
              ])
            : [[], []];
        if (stopped || current.signal.aborted) return;
        setSnapshot({ subject, status, workers, projects });
        setLoadError("");
      } catch (failure) {
        if (!stopped)
          setLoadError(
            current.signal.aborted
              ? "集群状态请求超时，请刷新重试。"
              : message(failure),
          );
      } finally {
        clearTimeout(deadline);
        if (!stopped) {
          setLoading(false);
          // Schedule the next request after this one settles; slow responses
          // never create overlapping polling requests.
          timer = setTimeout(() => void load(), 5000);
        }
      }
    };
    void load();
    return () => {
      stopped = true;
      controller?.abort();
      clearTimeout(timer);
    };
  }, [canManage, subject, refresh]);

  useEffect(() => {
    setCredential(null);
    setConfirmRevoke("");
    setDraft(initialDraft);
    setAction("");
    setActionError("");
    return () => {
      const current = actionRequest.current;
      actionRequest.current = null;
      current?.abort();
    };
  }, [subject, canManage]);

  const visible = snapshot?.subject === subject ? snapshot : null;
  const status = visible?.status;
  const workers = canManage ? visible?.workers || [] : [];
  const projects = canManage
    ? visible?.projects.filter((project) => !project.archived) || []
    : [];
  const names = new Map(
    (visible?.projects || []).map((project) => [
      project.id as string,
      project.name as string,
    ]),
  );
  const secret =
    canManage && credential?.subject === subject ? credential : null;

  const register = async () => {
    if (!canManage || action || secret || !status?.enabled) return;
    setActionError("");
    let body;
    try {
      body = workerEnrollment(
        draft,
        projects.map((project) => project.id),
      );
    } catch (failure) {
      setActionError(message(failure));
      return;
    }
    const controller = new AbortController();
    actionRequest.current = controller;
    const deadline = setTimeout(() => controller.abort(), 10000);
    setAction("register");
    try {
      const enrolled = await api<{ worker: ClusterWorker; token: string }>(
        "/cluster/workers",
        {
          method: "POST",
          body: JSON.stringify(body),
          signal: controller.signal,
        },
      );
      if (controller.signal.aborted) return;
      setCredential({
        subject,
        workerId: enrolled.worker.id,
        name: enrolled.worker.name,
        token: enrolled.token,
      });
      setDraft(initialDraft);
      setRefresh((value) => value + 1);
    } catch {
      if (actionRequest.current !== controller) return;
      if (!controller.signal.aborted)
        // Enrollment responses contain a secret. Never echo arbitrary response
        // bodies into an error, toast, URL, audit view or persisted state.
        setActionError(
          "节点登记未完成。请刷新列表确认；若节点已创建但未取得凭证，请撤销后重新登记。",
        );
      else
        setActionError(
          "节点登记请求已中断。请刷新列表确认创建状态，避免重复登记。",
        );
    } finally {
      clearTimeout(deadline);
      if (actionRequest.current === controller) {
        actionRequest.current = null;
        setAction("");
      }
    }
  };

  const revoke = async (worker: ClusterWorker) => {
    if (!canManage || action || !status?.enabled) return;
    const controller = new AbortController();
    actionRequest.current = controller;
    const deadline = setTimeout(() => controller.abort(), 10000);
    setAction(worker.id);
    setActionError("");
    try {
      await api(`/cluster/workers/${encodeURIComponent(worker.id)}/revoke`, {
        method: "POST",
        signal: controller.signal,
      });
      if (controller.signal.aborted) return;
      if (credential?.workerId === worker.id) setCredential(null);
      setConfirmRevoke("");
      setRefresh((value) => value + 1);
    } catch (failure) {
      if (actionRequest.current !== controller) return;
      if (!controller.signal.aborted) setActionError(message(failure));
      else setActionError("撤销请求已中断，请刷新确认节点状态。");
    } finally {
      clearTimeout(deadline);
      if (actionRequest.current === controller) {
        actionRequest.current = null;
        setAction("");
      }
    }
  };

  return (
    <Panel
      title="集群计算节点"
      action={
        <Button
          disabled={loading || Boolean(action)}
          onClick={() => setRefresh((value) => value + 1)}
        >
          刷新节点状态
        </Button>
      }
    >
      {loadError ? <Notice kind="error">{loadError}</Notice> : null}
      {actionError ? <Notice kind="error">{actionError}</Notice> : null}
      {!status && loading ? (
        <p role="status" className="subtle-copy">
          正在读取执行模式…
        </p>
      ) : null}
      {status ? (
        <>
          <div className="governance-metrics">
            <Metric
              label="执行模式"
              value={
                status.execution_mode === "cluster" ? "多机集群" : "本机队列"
              }
            />
            <Metric
              label="任务记录存储"
              value={
                status.persistence_backend === "postgresql"
                  ? "PostgreSQL"
                  : "SQLite"
              }
            />
            {status.enabled && canManage ? (
              <Metric
                label="已登记节点"
                value={
                  workers.filter((worker) => worker.revoked_at == null).length
                }
                unit="个"
              />
            ) : null}
          </div>
          {!status.enabled ? (
            <Notice>
              当前部署尚未启用远程计算节点，任务继续使用服务所在机器的本机队列。启用集群执行后，工作台所有者可在此登记节点并选择允许计算的项目。
            </Notice>
          ) : !canManage ? (
            <Notice>
              集群执行已启用。节点列表、登记凭证和撤销操作仅向工作台所有者开放。
            </Notice>
          ) : (
            <>
              <p className="panel-copy">
                每个节点同时领取一个任务；项目范围和资源预算由所有者指定，任务仅在预算与节点实报能力均满足时分配。状态每5秒刷新，最近响应时间不等于数值结果已通过验证。
              </p>
              {workers.length ? (
                <div className="table-wrap">
                  <table>
                    <caption className="paper-table-caption">
                      已登记计算节点与当前租约（限时执行凭证）
                    </caption>
                    <thead>
                      <tr>
                        <th scope="col">节点 / 状态</th>
                        <th scope="col">项目范围</th>
                        <th scope="col">资源预算</th>
                        <th scope="col">最近响应 / 任务</th>
                        <th scope="col">操作</th>
                      </tr>
                    </thead>
                    <tbody>
                      {workers.map((worker) => {
                        const connection = workerConnection(worker);
                        return (
                          <tr key={worker.id}>
                            <td>
                              <strong>{worker.name}</strong>
                              <br />
                              <span className={`badge ${connection.kind}`}>
                                {connection.label}
                              </span>
                              <br />
                              <small className="mono">{worker.id}</small>
                            </td>
                            <td>
                              {worker.project_ids
                                .map((id) =>
                                  id === "*"
                                    ? "所有项目（已显式授权）"
                                    : names.get(id) || id,
                                )
                                .join("、")}
                            </td>
                            <td>
                              {number(worker.resources.threads, 0)} 线程
                              <br />
                              内存 {number(worker.resources.memory_gib)} GiB
                              <br />
                              磁盘 {number(worker.resources.disk_gib)} GiB
                              <br />
                              {worker.backends.join("、")}
                            </td>
                            <td>
                              {workerTime(worker.last_seen_at)}
                              {worker.active_lease ? (
                                <>
                                  <br />
                                  <span className="mono">
                                    {worker.active_lease.job_id}
                                  </span>
                                  <br />第 {worker.active_lease.attempt} 次尝试
                                  · 到期{" "}
                                  {workerTime(worker.active_lease.expires_at)}
                                </>
                              ) : (
                                <>
                                  <br />
                                  暂无执行租约
                                </>
                              )}
                            </td>
                            <td>
                              {worker.revoked_at != null ? (
                                "凭证已撤销"
                              ) : confirmRevoke === worker.id ? (
                                <div className="experiment-actions">
                                  <span>撤销后将停止接收该节点的新提交。</span>
                                  <Button
                                    disabled={Boolean(action)}
                                    busy={action === worker.id}
                                    onClick={() => void revoke(worker)}
                                  >
                                    确认撤销
                                  </Button>
                                  <Button
                                    disabled={Boolean(action)}
                                    onClick={() => setConfirmRevoke("")}
                                  >
                                    保留节点
                                  </Button>
                                </div>
                              ) : (
                                <Button
                                  disabled={Boolean(action)}
                                  onClick={() => setConfirmRevoke(worker.id)}
                                >
                                  撤销凭证
                                </Button>
                              )}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              ) : (
                <Empty
                  title="尚未登记计算节点"
                  description="先选择项目范围和资源预算登记节点，再将一次性凭证安全保存至计算机器。"
                />
              )}
              {secret ? (
                <Notice>
                  <strong>{secret.name} 的节点凭证仅在本次登记后展示</strong>
                  <p>
                    将凭证保存到计算节点上的独立文件，并限制为仅文件所有者可读写（权限600）。启动节点时通过{" "}
                    <code>--token-file</code>{" "}
                    指定文件。关闭、切换身份或离开此页后无法再次查看；遗失后应撤销并重新登记。
                  </p>
                  <Field
                    label="一次性节点凭证"
                    hint="请手动复制并安全保存；不要放入网址、日志或命令行参数。"
                  >
                    <textarea
                      readOnly
                      rows={3}
                      value={secret.token}
                      autoComplete="off"
                      spellCheck={false}
                    />
                  </Field>
                  <Button onClick={() => setCredential(null)}>
                    已安全保存，隐藏凭证
                  </Button>
                </Notice>
              ) : null}
              <details>
                <summary>登记新的计算节点</summary>
                <form
                  onSubmit={(event) => {
                    event.preventDefault();
                    void register();
                  }}
                >
                  <fieldset disabled={Boolean(action) || Boolean(secret)}>
                    <legend>节点身份与计算范围</legend>
                    <Field
                      label="节点名称"
                      hint="使用便于区分机器的名称，不填写凭证或业务信息。"
                    >
                      <input
                        required
                        maxLength={128}
                        value={draft.name}
                        onChange={(event) =>
                          setDraft((value) => ({
                            ...value,
                            name: event.target.value,
                          }))
                        }
                      />
                    </Field>
                    <fieldset className="checkbox-list">
                      <legend>允许计算的项目（请明确勾选）</legend>
                      {projects.map((project) => (
                        <label className="checkbox" key={project.id}>
                          <input
                            type="checkbox"
                            checked={draft.projectIds.includes(project.id)}
                            onChange={(event) => {
                              const checked = event.target.checked;
                              setDraft((value) => ({
                                ...value,
                                projectIds: checked
                                  ? [...value.projectIds, project.id]
                                  : value.projectIds.filter(
                                      (id) => id !== project.id,
                                    ),
                              }));
                            }}
                          />
                          <span>
                            {project.name}
                            <small className="mono"> {project.id}</small>
                          </span>
                        </label>
                      ))}
                      {!projects.length ? (
                        <p className="subtle-copy">
                          暂无可分配项目，请先在项目管理中创建项目。
                        </p>
                      ) : null}
                    </fieldset>
                    <div className="form-grid">
                      {(
                        [
                          ["threads", "线程预算", "1", "256", "1"],
                          ["memory", "内存预算 / GiB", "0.001", "1024", "any"],
                          ["disk", "磁盘预算 / GiB", "0.001", "4096", "any"],
                        ] as const
                      ).map(([key, label, min, max, step]) => (
                        <Field key={key} label={label}>
                          <input
                            required
                            type="number"
                            min={min}
                            max={max}
                            step={step}
                            value={draft[key]}
                            onChange={(event) =>
                              setDraft((value) => ({
                                ...value,
                                [key]: event.target.value,
                              }))
                            }
                          />
                        </Field>
                      ))}
                    </div>
                    <p className="subtle-copy">
                      GiB 为内存与磁盘容量单位。此处登记基于处理器的 SciPy
                      数值计算节点；资源预算请按节点实际可用于任务的容量填写。
                    </p>
                    <Button
                      type="submit"
                      primary
                      busy={action === "register"}
                      disabled={!projects.length}
                    >
                      登记节点并生成凭证
                    </Button>
                  </fieldset>
                </form>
              </details>
            </>
          )}
        </>
      ) : null}
    </Panel>
  );
}
