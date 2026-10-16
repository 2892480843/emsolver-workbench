import { useEffect, useState } from "react";
import { FolderOpen, Plus, Search, ShieldCheck } from "lucide-react";
import { api, number, type Entity } from "../api";
import {
  projectDraft,
  projectPatch,
  projectRole,
  projectSearchPath,
  type ProjectDraft,
} from "../models/workspace";
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

type Props = {
  projects: Entity[];
  identity: Entity | null;
  selectedId: string;
  onSelect: (id: string) => void;
  onUpdated: (project: Entity) => void;
  onCreate: () => void;
  notify: (message: string, error?: boolean) => void;
};
const message = (error: unknown) =>
  error instanceof Error ? error.message : String(error);
const classificationLabels: Record<string, string> = {
  public: "公开",
  internal: "内部",
  confidential: "保密",
};

export function ProjectWorkspace({
  projects,
  identity,
  selectedId,
  onSelect,
  onUpdated,
  onCreate,
  notify,
}: Props) {
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState("");
  const [found, setFound] = useState<Entity[]>(projects);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    if (!search) {
      setFound(projects);
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    api<Entity[]>(projectSearchPath(search), { signal: controller.signal })
      .then((items) => {
        if (!controller.signal.aborted) {
          setFound(items);
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
  }, [search, projects]);
  const selected = projects.find((project) => project.id === selectedId);
  const canEdit = projectRole(selected, identity) === "admin";
  return (
    <Panel
      title="项目管理"
      eyebrow="PROJECTS & DATA POLICY"
      action={
        <Button
          disabled={Boolean(
            identity?.operations_admin && !identity.global_admin,
          )}
          onClick={onCreate}
        >
          <Plus size={14} />
          新建项目
        </Button>
      }
    >
      <form
        className="governance-search"
        onSubmit={(event) => {
          event.preventDefault();
          setSearch(query.trim());
        }}
      >
        <div className="search-field">
          <Search size={16} />
          <input
            aria-label="检索项目"
            maxLength={200}
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="检索项目名称、说明或编号"
          />
        </div>
        <Button type="submit" busy={loading}>
          检索
        </Button>
        {search ? (
          <Button
            type="button"
            onClick={() => {
              setQuery("");
              setSearch("");
            }}
          >
            显示全部
          </Button>
        ) : null}
      </form>
      {error ? <Notice kind="error">项目数据读取失败：{error}</Notice> : null}
      <div className="project-management-grid">
        <div className="project-directory">
          {found.length ? (
            found.map((project) => (
              <button
                key={project.id}
                className={`project-directory-item ${selectedId === project.id ? "selected" : ""}`}
                onClick={() => onSelect(project.id)}
              >
                <FolderOpen size={20} />
                <span>
                  <b>{project.name}</b>
                  <small>{project.description || "尚无项目说明"}</small>
                  <em>
                    {classificationLabels[project.classification] || "内部"} ·{" "}
                    {project.archived ? "已归档" : "可创建实验"}
                  </em>
                </span>
              </button>
            ))
          ) : (
            <Empty
              title="没有匹配项目"
              description="调整检索条件，或创建一个独立的实验项目。"
            />
          )}
        </div>
        {selected ? (
          <ProjectEditor
            key={selected.id}
            project={selected}
            identity={identity}
            canEdit={canEdit}
            onUpdated={onUpdated}
            notify={notify}
          />
        ) : (
          <Empty
            title="选择项目管理策略"
            description="可查看存储用量、数据分级、成员角色和任务保留周期。"
          />
        )}
      </div>
    </Panel>
  );
}

function ProjectEditor({
  project,
  identity,
  canEdit,
  onUpdated,
  notify,
}: {
  project: Entity;
  identity: Entity | null;
  canEdit: boolean;
  onUpdated: Props["onUpdated"];
  notify: Props["notify"];
}) {
  const [draft, setDraft] = useState<ProjectDraft>(() => projectDraft(project));
  const [usage, setUsage] = useState<Entity | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [revision, setRevision] = useState(0);
  const fingerprint = JSON.stringify(project);
  useEffect(() => {
    setDraft(projectDraft(project));
  }, [fingerprint]);
  useEffect(() => {
    const controller = new AbortController();
    api(`/projects/${encodeURIComponent(project.id)}/usage`, {
      signal: controller.signal,
    })
      .then((value) => {
        if (!controller.signal.aborted) {
          setUsage(value);
          setError("");
        }
      })
      .catch((failure) => {
        if (!controller.signal.aborted) setError(message(failure));
      });
    return () => controller.abort();
  }, [project.id, revision]);
  const change = (field: keyof ProjectDraft, value: string) =>
    setDraft((previous) => ({ ...previous, [field]: value }));
  const save = async (archived?: boolean) => {
    if (busy || !canEdit) return;
    try {
      const payload =
        archived === undefined ? projectPatch(draft) : { archived };
      setBusy(archived === undefined ? "save" : "archive");
      const updated = await api(`/projects/${encodeURIComponent(project.id)}`, {
        method: "PATCH",
        body: JSON.stringify({ ...payload, expected_version: project.version ?? 1 }),
      });
      onUpdated(updated);
      setRevision((value) => value + 1);
      notify(
        archived === undefined
          ? "项目属性、成员与资源策略已保存"
          : archived
            ? "项目已归档，历史记录保留可读"
            : "项目已恢复，可继续创建实验",
      );
    } catch (failure) {
      setError(message(failure));
      notify(message(failure), true);
    } finally {
      setBusy("");
    }
  };
  return (
    <div className="project-editor">
      <div className="section-title">
        <div>
          <span className="eyebrow">{project.id}</span>
          <h3>{project.name}</h3>
        </div>
        <Badge status={project.archived ? "ARCHIVED" : "READY"} />
      </div>
      {error ? <Notice kind="error">{error}</Notice> : null}
      {usage ? (
        <div className="governance-metrics">
          <Metric
            label="已用存储"
            value={number(usage.total_bytes / 1024 ** 3)}
            unit="GiB"
            detail={`配额 ${number(usage.storage_limit_bytes / 1024 ** 3)} GiB`}
          />
          <Metric
            label="任务输出预留"
            value={number(usage.reserved_output_bytes / 1024 ** 3)}
            unit="GiB"
            detail={`${usage.queued_jobs ?? "—"} 项排队 / 活动任务`}
          />
        </div>
      ) : null}
      {!canEdit ? (
        <Notice>
          当前身份 {identity?.subject || "尚未确认"}{" "}
          只读。项目管理员可修改成员及资源策略。
        </Notice>
      ) : null}
      {project.archived ? (
        <Notice>归档后禁止新增输入与任务；历史结果仍按原权限读取。</Notice>
      ) : null}
      <fieldset
        disabled={!canEdit || Boolean(busy)}
        className="governance-fields"
      >
        <div className="form-grid">
          <Field label="项目名称">
            <input
              maxLength={200}
              value={draft.name}
              onChange={(event) => change("name", event.target.value)}
            />
          </Field>
          <Field label="数据分级">
            <select
              value={draft.classification}
              onChange={(event) => change("classification", event.target.value)}
            >
              {Object.entries(classificationLabels).map(([key, label]) => (
                <option key={key} value={key}>
                  {label}
                </option>
              ))}
            </select>
          </Field>
        </div>
        <Field label="项目说明">
          <textarea
            rows={2}
            value={draft.description}
            onChange={(event) => change("description", event.target.value)}
          />
        </Field>
        <div className="form-grid">
          <Field label="存储配额 / GiB">
            <input
              inputMode="decimal"
              value={draft.storage}
              onChange={(event) => change("storage", event.target.value)}
            />
          </Field>
          <Field label="排队任务上限" hint="1–100">
            <input
              inputMode="numeric"
              value={draft.queued}
              onChange={(event) => change("queued", event.target.value)}
            />
          </Field>
          <Field label="调度优先级" hint="-10 至 10；同优先级按提交顺序">
            <input
              inputMode="numeric"
              value={draft.priority}
              onChange={(event) => change("priority", event.target.value)}
            />
          </Field>
          <Field label="任务保留天数" hint="清理仍保护被引用的输入和保留运行">
            <input
              inputMode="numeric"
              value={draft.retention}
              onChange={(event) => change("retention", event.target.value)}
            />
          </Field>
        </div>
        <div className="governance-member-heading">
          <b>项目成员</b>
          <Button
            type="button"
            onClick={() =>
              setDraft((previous) => ({
                ...previous,
                members: [...previous.members, { subject: "", role: "viewer" }],
              }))
            }
          >
            <Plus size={13} />
            添加成员
          </Button>
        </div>
        {draft.members.map((member, index) => (
          <div className="governance-member" key={index}>
            <Field label={`成员 ${index + 1} 身份`}>
              <input
                list="configured-subjects"
                value={member.subject}
                onChange={(event) =>
                  setDraft((previous) => ({
                    ...previous,
                    members: previous.members.map((item, i) =>
                      i === index
                        ? { ...item, subject: event.target.value }
                        : item,
                    ),
                  }))
                }
              />
            </Field>
            <Field label="角色">
              <select
                value={member.role}
                onChange={(event) =>
                  setDraft((previous) => ({
                    ...previous,
                    members: previous.members.map((item, i) =>
                      i === index
                        ? { ...item, role: event.target.value }
                        : item,
                    ),
                  }))
                }
              >
                <option value="viewer">查看者 · 读取历史</option>
                <option value="analyst">分析者 · 创建与运行</option>
                <option value="admin">管理员 · 管理项目</option>
              </select>
            </Field>
            <Button
              type="button"
              aria-label={`移除成员 ${index + 1}`}
              onClick={() =>
                setDraft((previous) => ({
                  ...previous,
                  members: previous.members.filter((_, i) => i !== index),
                }))
              }
            >
              移除
            </Button>
          </div>
        ))}
        <datalist id="configured-subjects">
          {(identity?.configured_subjects || []).map((subject: string) => (
            <option key={subject} value={subject} />
          ))}
        </datalist>
      </fieldset>
      <div className="experiment-actions">
        <Button
          primary
          disabled={!canEdit || Boolean(busy)}
          busy={busy === "save"}
          onClick={() => void save()}
        >
          <ShieldCheck size={14} />
          保存项目策略
        </Button>
        <Button
          disabled={!canEdit || Boolean(busy)}
          busy={busy === "archive"}
          onClick={() => void save(!project.archived)}
        >
          {project.archived ? "恢复项目" : "归档项目"}
        </Button>
        <Button
          disabled={Boolean(busy)}
          onClick={() => setRevision((value) => value + 1)}
        >
          刷新用量
        </Button>
      </div>
      <details>
        <summary>存储用量与策略原始记录</summary>
        <Json value={usage} />
      </details>
    </div>
  );
}
