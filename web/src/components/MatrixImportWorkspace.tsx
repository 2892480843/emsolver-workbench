import { useEffect, useRef, useState } from "react";
import { RefreshCw, Square, Upload } from "lucide-react";
import { api, number, post, type Entity } from "../api";
import { importIsTerminal, readImportTask } from "../models/workspace";
import { Badge, Button, Field, Notice, Panel } from "../ui";

type Props = {
  projectId: string;
  file: File | null;
  onFileChange: (file: File | null) => void;
  storage: string;
  onStorageChange: (storage: string) => void;
  onImported: (matrix: Entity) => void;
  notify: (message: string, error?: boolean) => void;
};
const message = (error: unknown) =>
  error instanceof Error ? error.message : String(error);
const stageLabels: Record<string, string> = {
  queued: "等待导入",
  receiving: "接收文件",
  validating: "安全校验",
  loading: "读取矩阵",
  parsing: "解析输入",
  normalizing: "规范化稀疏存储",
  storing: "保存只读资产",
  completed: "登记完成",
  cancelled: "已取消",
  failed: "导入失败",
};

export function MatrixImportWorkspace({
  projectId,
  file,
  onFileChange,
  storage,
  onStorageChange,
  onImported,
  notify,
}: Props) {
  const [list, setList] = useState<{ projectId: string; tasks: Entity[] }>({
    projectId,
    tasks: [],
  });
  const [selectedId, setSelectedId] = useState("");
  const [revision, setRevision] = useState(0);
  const [uploading, setUploading] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [error, setError] = useState("");
  const uploadRequest = useRef<AbortController | null>(null);
  const scope = useRef(projectId);
  scope.current = projectId;
  const previousProject = useRef(projectId);
  const imported = useRef(new Set<string>());
  const importedCallback = useRef(onImported);
  importedCallback.current = onImported;
  const fileCallback = useRef(onFileChange);
  fileCallback.current = onFileChange;
  const tasks = list.projectId === projectId ? list.tasks : [];
  const task = tasks.find((item) => item.id === selectedId);
  const updateTask = (value: Entity, expectedProject: string) =>
    setList((previous) => ({
      projectId: expectedProject,
      tasks: [
        value,
        ...(previous.projectId === expectedProject
          ? previous.tasks.filter((item) => item.id !== value.id)
          : []),
      ],
    }));

  useEffect(() => {
    uploadRequest.current?.abort();
    setUploading(false);
    setSelectedId("");
    setError("");
    if (previousProject.current !== projectId) fileCallback.current(null);
    previousProject.current = projectId;
    return () => uploadRequest.current?.abort();
  }, [projectId]);
  useEffect(() => {
    const controller = new AbortController();
    if (!projectId) return () => controller.abort();
    api<Entity[]>(
      `/imports?${new URLSearchParams({ project_id: projectId })}`,
      { signal: controller.signal },
    )
      .then((items) => {
        if (controller.signal.aborted || scope.current !== projectId) return;
        setList({ projectId, tasks: items });
        setSelectedId(
          (previous) =>
            previous ||
            items.find((item) => !importIsTerminal(item.status))?.id ||
            "",
        );
      })
      .catch((failure) => {
        if (!controller.signal.aborted) setError(message(failure));
      });
    return () => controller.abort();
  }, [projectId, revision]);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    if (!projectId || !selectedId) return () => controller.abort();
    const read = async () => {
      let delay: number | null = 2000;
      try {
        const next = await readImportTask(selectedId, api, controller.signal);
        if (controller.signal.aborted || scope.current !== projectId) return;
        updateTask(next.task, projectId);
        setError("");
        delay = next.delay;
        if (
          next.task.status === "SUCCEEDED" &&
          next.task.matrix_id &&
          !imported.current.has(next.task.id)
        ) {
          const matrix = await api(
            `/matrices/${encodeURIComponent(next.task.matrix_id)}`,
            { signal: controller.signal },
          );
          if (controller.signal.aborted || scope.current !== projectId) return;
          imported.current.add(next.task.id);
          importedCallback.current(matrix);
        }
      } catch (failure) {
        if (!controller.signal.aborted) {
          setError(message(failure));
          delay = 2000;
        }
      }
      if (!controller.signal.aborted && delay !== null)
        timer = setTimeout(read, delay);
    };
    void read();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [selectedId, projectId, revision]);
  const start = async () => {
    if (!file || !projectId || uploading) return;
    const expectedProject = projectId;
    const controller = new AbortController();
    uploadRequest.current?.abort();
    uploadRequest.current = controller;
    setUploading(true);
    setError("");
    try {
      const data = new FormData();
      data.append("file", file);
      data.append("project_id", projectId);
      data.append("name", file.name);
      data.append("storage", storage);
      const created = await api("/imports", {
        method: "POST",
        body: data,
        signal: controller.signal,
      });
      if (controller.signal.aborted || scope.current !== expectedProject)
        return;
      updateTask(created, projectId);
      setSelectedId(created.id);
      onFileChange(null);
      notify("文件已接收，正在跟踪服务端导入任务");
    } catch (failure) {
      if (!controller.signal.aborted) setError(message(failure));
    } finally {
      if (
        uploadRequest.current === controller &&
        scope.current === expectedProject
      )
        setUploading(false);
    }
  };
  const cancel = async () => {
    if (uploading) {
      uploadRequest.current?.abort();
      setUploading(false);
      notify("上传已取消，未登记的临时文件由服务端回收");
      return;
    }
    if (!task || importIsTerminal(task.status) || cancelling) return;
    const expectedProject = projectId;
    setCancelling(true);
    try {
      const cancelled = await post(
        `/imports/${encodeURIComponent(task.id)}/cancel`,
      );
      if (scope.current === expectedProject) {
        updateTask(cancelled, expectedProject);
        setRevision((value) => value + 1);
      }
      notify("导入取消已请求；取消任务不会产生可用矩阵记录");
    } catch (failure) {
      notify(message(failure), true);
    } finally {
      setCancelling(false);
    }
  };
  return (
    <Panel
      title="可取消的矩阵导入"
      eyebrow="TRACKED IMPORT"
      action={
        <Button onClick={() => setRevision((value) => value + 1)}>
          <RefreshCw size={14} />
          刷新记录
        </Button>
      }
    >
      {!projectId ? <Notice>请在顶部选择一个项目，再导入矩阵。</Notice> : null}
      <div className="form-grid">
        <Field
          label="矩阵文件"
          hint="SciPy 稀疏 .npz 或 NumPy .npy；原始稀疏输入不会整体稠密化"
        >
          <input
            type="file"
            accept=".npz,.npy"
            disabled={uploading || !projectId}
            onChange={(event) => {
              onFileChange(event.target.files?.[0] || null);
              event.target.value = "";
            }}
          />
        </Field>
        <Field label="存储声明" hint="必须显式声明，三角输入按共轭转置恢复">
          <select
            value={storage}
            disabled={uploading}
            onChange={(event) => onStorageChange(event.target.value)}
          >
            <option value="full">完整矩阵</option>
            <option value="upper">上三角</option>
            <option value="lower">下三角</option>
          </select>
        </Field>
      </div>
      {file ? (
        <p className="panel-copy">
          待上传：<b>{file.name}</b> · {number(file.size / 1024 ** 2)} MiB
        </p>
      ) : null}
      <div className="experiment-actions">
        <Button
          primary
          disabled={!file || !projectId || uploading}
          busy={uploading}
          onClick={() => void start()}
        >
          <Upload size={14} />
          开始导入
        </Button>
        {uploading || (task && !importIsTerminal(task.status)) ? (
          <Button busy={cancelling} onClick={() => void cancel()}>
            <Square size={13} />
            {uploading ? "取消上传" : "取消导入"}
          </Button>
        ) : null}
      </div>
      {uploading ? (
        <p role="status" className="panel-copy">
          正在传输文件；接收完成后将展示服务端解析阶段。
        </p>
      ) : null}
      {error ? <Notice kind="error">导入读取失败：{error}</Notice> : null}
      {tasks.length ? (
        <div className="table-wrap">
          <table>
            <caption className="paper-table-caption">矩阵导入任务与处理阶段</caption>
            <thead>
              <tr>
                <th>导入任务</th>
                <th>状态</th>
                <th>真实阶段</th>
                <th>已接收字节</th>
                <th>结果 / 错误</th>
              </tr>
            </thead>
            <tbody>
              {tasks.map((item) => (
                <tr
                  key={item.id}
                  className={item.id === selectedId ? "selected" : ""}
                >
                  <td>
                    <button
                      className="link-button mono"
                      onClick={() => {
                        setSelectedId(item.id);
                        setRevision((value) => value + 1);
                      }}
                    >
                      {item.id.slice(0, 14)}
                    </button>
                    <small>{item.name || item.filename || "矩阵导入"}</small>
                  </td>
                  <td>
                    <Badge status={item.status} />
                  </td>
                  <td>{stageLabels[item.stage] || item.stage || "—"}</td>
                  <td>
                    {number(item.bytes_received)} / {number(item.total_bytes)}
                  </td>
                  <td>
                    {item.status === "SUCCEEDED"
                      ? `矩阵 ${item.matrix_id?.slice(0, 12) || "已登记"}`
                      : item.error?.message ||
                        item.error?.code ||
                        (typeof item.error === "string" ? item.error : "—")}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      <p className="subtle-copy">
        取消、校验失败和中断上传保留状态说明。相同内容是否复用磁盘由服务端按哈希和项目权限判定。
      </p>
    </Panel>
  );
}
