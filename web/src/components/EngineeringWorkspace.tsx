import { useEffect, useMemo, useRef, useState } from "react";
import { api, number, type Entity } from "../api";
import { safeSourceUrl } from "../models/workspace";
import { Button, Field, Notice, Panel } from "../ui";
import { engineeringUpload } from "./field-mapping";
import "./engineering.css";

const units: Record<string, string> = {
  omega_squared: "s^-2",
  angular_frequency: "rad/s",
  frequency_hz: "Hz",
  wavenumber_squared: "m^-2",
};
const formulas: Record<string, string> = {
  omega_squared: "f = √λ / (2π)",
  angular_frequency: "f = λ / (2π)",
  frequency_hz: "f = λ",
  wavenumber_squared: "f = c √λ / (2π)",
};

function emptyManifest(matrix: Entity) {
  return JSON.stringify(
    {
      schema_version: "1.0",
      model_version: "",
      matrix_sha256: matrix.hash,
      authorized: false,
      public: false,
      source: { title: "", url: "" },
      license: "",
      coordinate_units: "m",
      field_units: "normalized",
      coordinates: [],
      dof_nodes: [],
      dof_components: [],
      cells: [],
      frequency_mapping: null,
      nullspace: null,
    },
    null,
    2,
  );
}

function EngineeringEditor({
  matrix,
  canManage,
  onChanged,
}: {
  matrix: Entity;
  canManage: boolean;
  onChanged: () => void;
}) {
  const [page, setPage] = useState<Entity | null>(null);
  const [text, setText] = useState(() => emptyManifest(matrix));
  const [authorized, setAuthorized] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const request = useRef<AbortController | null>(null);
  const alive = useRef(true);
  const fileRead = useRef(0);
  const draft = useMemo<Entity | null>(() => {
    try {
      const value = JSON.parse(text);
      return value && !Array.isArray(value) && typeof value === "object"
        ? value
        : null;
    } catch {
      return null;
    }
  }, [text]);
  const load = async () => {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    try {
      const value = await api<Entity>(
        `/matrices/${encodeURIComponent(matrix.id)}/engineering`,
        { signal: controller.signal },
      );
      if (!controller.signal.aborted) setPage(value);
    } catch (failure) {
      if (!controller.signal.aborted)
        setError(failure instanceof Error ? failure.message : String(failure));
    }
  };
  useEffect(() => {
    alive.current = true;
    void load();
    return () => {
      alive.current = false;
      request.current?.abort();
    };
  }, [matrix.id]);
  const edit = (key: string, value: unknown) => {
    if (draft) {
      setText(JSON.stringify({ ...draft, [key]: value }, null, 2));
      setAuthorized(false);
    }
  };
  const perform = async (path: string, payload: unknown, message: string) => {
    if (busy || !canManage) return;
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await api(
        `/matrices/${encodeURIComponent(matrix.id)}/engineering${path}`,
        {
          method: "POST",
          body: JSON.stringify(payload),
          signal: controller.signal,
        },
      );
      if (controller.signal.aborted) return;
      setNotice(message);
      setAuthorized(false);
      onChanged();
      await load();
    } catch (failure) {
      if (!controller.signal.aborted)
        setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      if (alive.current) setBusy(false);
    }
  };
  const upload = () => {
    try {
      void perform(
        "",
        engineeringUpload(text, matrix.hash, authorized),
        "新模型版本已写入只读侧车。请明确选择版本，再创建新计划和任务。",
      );
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    }
  };
  const readFile = async (file?: File) => {
    if (!file) return;
    setError("");
    setAuthorized(false);
    const read = ++fileRead.current;
    try {
      if (file.size > 16 * 1024 ** 2)
        throw new Error("清单文件不能超过 16 MiB");
      const value = new TextDecoder("utf-8", { fatal: true }).decode(
        await file.arrayBuffer(),
      );
      if (alive.current && read === fileRead.current) {
        setText(value);
        setNotice(`已读取 ${file.name}；尚未上传或授权。`);
      }
    } catch (failure) {
      if (alive.current)
        setError(failure instanceof Error ? failure.message : String(failure));
    }
  };
  const mapping = draft?.frequency_mapping;
  return (
    <Panel
      title="工程模型映射"
      eyebrow="IMMUTABLE SIDECAR · PROJECT AUTHORIZATION"
      action={
        <Button disabled={busy} onClick={() => void load()}>
          刷新模型版本
        </Button>
      }
    >
      <p className="panel-copy">
        为原始矩阵绑定有来源、许可和单位的坐标及自由度映射。每个版本内容按
        SHA-256
        固定；仅随后新建的计划采用所选版本，任务继承其计划的冻结模型。上传坐标本身不证明物理模型正确。
      </p>
      <p className="mono">原始矩阵 SHA-256：{matrix.hash}</p>
      {error ? <Notice kind="error">{error}</Notice> : null}
      {notice ? <Notice>{notice}</Notice> : null}
      <div className="engineering-versions">
        {page?.items?.map((item: Entity) => (
          <article className="engineering-version" key={item.id}>
            <strong>
              {item.model_version}{" "}
              {page.selected_id === item.id ? "· 当前选定" : ""}
            </strong>
            <p>
              {number(item.n_nodes, 0)} 节点 / {number(item.n_dofs, 0)} 自由度 ·
              坐标 {item.coordinate_units} · 分量 {item.field_units} ·{" "}
              {item.license}
              <br />
              {item.model_kind === "analytic_demo"
                ? "CC0 解析节点链演示；不是器件网格。"
                : "用户提供并由项目管理员声明有权使用。"}
              <br />
              {safeSourceUrl(item.source?.url) ? (
                <a
                  href={safeSourceUrl(item.source?.url)}
                  target="_blank"
                  rel="noreferrer noopener"
                >
                  {item.source?.title}
                </a>
              ) : (
                item.source?.title
              )}
              <br />
              <span className="mono">{item.sidecar_sha256}</span>
              <br />
              {item.nullspace_report
                ? `原输入零空间声明：${item.nullspace_report.status}，${item.nullspace_report.basis_count} 个基向量；不自动删除任何模式。`
                : "未声明零空间基。"}
              <br />
              {item.frequency_mapping
                ? `${formulas[item.frequency_mapping.kind]}；适用范围：${item.frequency_mapping.applicability}`
                : "未声明频率公式；仅显示原特征值。"}
            </p>
            {item.availability === "MISSING" ? (
              <Notice kind="error">侧车文件缺失，不能选择或显示场图。</Notice>
            ) : (
              <Button
                disabled={!canManage || busy || page.selected_id === item.id}
                onClick={() =>
                  void perform(
                    "/select",
                    { asset_id: item.id },
                    "模型版本已选定；历史运行保留原冻结模型，需重新预览并提交新任务。",
                  )
                }
              >
                用于后续新任务
              </Button>
            )}
          </article>
        ))}
        {page && !page.items?.length ? (
          <Notice>尚无工程映射；矩阵图和模式序号不会被解释为器件几何。</Notice>
        ) : null}
      </div>
      {page && page.total > page.items.length ? (
        <p>
          当前显示最近 {page.items.length} / {page.total} 个不可变版本。
        </p>
      ) : null}
      {!canManage ? (
        <Notice>
          上传、生成或选择工程模型需要项目管理员权限；当前角色可查看已获授权的模型。
        </Notice>
      ) : (
        <details>
          <summary>上传或编辑授权模型清单</summary>
          <div className="engineering-upload">
            <Field
              label="UTF-8 JSON 清单（最大 16 MiB）"
              hint="至多 100,000 个三维节点；自由度映射长度须等于矩阵阶数；可选线段、三角形或四面体单元。"
            >
              <input
                type="file"
                accept="application/json,.json"
                disabled={busy}
                onChange={(event) => void readFile(event.target.files?.[0])}
              />
            </Field>
            <Field
              label="完整模型清单"
              hint="coordinates 为 nNodes×3；dof_nodes 与 dof_components 使用原始自由度顺序。可选 nullspace.basis 使用稀疏 indices/real/imag；独立进程只验证原输入零空间残差，不移除物理模式。"
            >
              <textarea
                value={text}
                disabled={busy}
                spellCheck={false}
                onChange={(event) => {
                  setText(event.target.value);
                  setAuthorized(false);
                }}
              />
            </Field>
            {draft ? (
              <>
                <div className="engineering-controls">
                  <Field label="模型版本">
                    <input
                      value={draft.model_version || ""}
                      disabled={busy}
                      onChange={(event) =>
                        edit("model_version", event.target.value)
                      }
                    />
                  </Field>
                  <Field label="许可标识">
                    <input
                      value={draft.license || ""}
                      disabled={busy}
                      onChange={(event) => edit("license", event.target.value)}
                    />
                  </Field>
                  <Field label="来源名称">
                    <input
                      value={draft.source?.title || ""}
                      disabled={busy}
                      onChange={(event) =>
                        edit("source", {
                          ...draft?.source,
                          title: event.target.value,
                        })
                      }
                    />
                  </Field>
                  <Field label="来源链接">
                    <input
                      value={draft.source?.url || ""}
                      disabled={busy}
                      placeholder="https://…"
                      onChange={(event) =>
                        edit("source", {
                          ...draft?.source,
                          url: event.target.value,
                        })
                      }
                    />
                  </Field>
                  <Field label="坐标单位">
                    <select
                      value={draft.coordinate_units || "m"}
                      disabled={busy}
                      onChange={(event) =>
                        edit("coordinate_units", event.target.value)
                      }
                    >
                      {["m", "cm", "mm"].map((value) => (
                        <option key={value}>{value}</option>
                      ))}
                    </select>
                  </Field>
                  <Field label="场分量单位">
                    <input
                      value={draft.field_units || ""}
                      disabled={busy}
                      onChange={(event) =>
                        edit("field_units", event.target.value)
                      }
                    />
                  </Field>
                  <Field label="明确审阅的频率公式">
                    <select
                      value={mapping?.kind || ""}
                      disabled={busy}
                      onChange={(event) => {
                        const kind = event.target.value;
                        edit(
                          "frequency_mapping",
                          kind
                            ? {
                                kind,
                                eigenvalue_units: units[kind],
                                reviewed: false,
                                applicability: "",
                                ...(kind === "wavenumber_squared"
                                  ? { wave_speed_m_per_s: 299792458 }
                                  : {}),
                              }
                            : null,
                        );
                      }}
                    >
                      <option value="">无；保留特征值</option>
                      {Object.entries(formulas).map(([kind, formula]) => (
                        <option key={kind} value={kind}>
                          {formula}
                        </option>
                      ))}
                    </select>
                  </Field>
                  {mapping ? (
                    <>
                      <Field
                        label={`公式适用范围 · λ 单位 ${mapping.eigenvalue_units}`}
                      >
                        <input
                          value={mapping.applicability || ""}
                          disabled={busy}
                          onChange={(event) =>
                            edit("frequency_mapping", {
                              ...mapping,
                              applicability: event.target.value,
                            })
                          }
                        />
                      </Field>
                      {mapping.kind === "wavenumber_squared" ? (
                        <Field label="模型波速 c（m/s）">
                          <input
                            type="number"
                            min="0"
                            max="10000000000"
                            value={mapping.wave_speed_m_per_s ?? ""}
                            disabled={busy}
                            onChange={(event) =>
                              edit("frequency_mapping", {
                                ...mapping,
                                wave_speed_m_per_s: Number(event.target.value),
                              })
                            }
                          />
                        </Field>
                      ) : null}
                    </>
                  ) : null}
                </div>
                {mapping ? (
                  <label className="engineering-check">
                    <input
                      type="checkbox"
                      disabled={busy}
                      checked={mapping.reviewed === true}
                      onChange={(event) =>
                        edit("frequency_mapping", {
                          ...mapping,
                          reviewed: event.target.checked,
                        })
                      }
                    />
                    我已核对该模型方程、特征值单位及频率公式的适用条件。负特征值不会转换为实非负频率。
                  </label>
                ) : null}
              </>
            ) : null}
            <label className="engineering-check">
              <input
                type="checkbox"
                disabled={busy}
                checked={authorized}
                onChange={(event) => setAuthorized(event.target.checked)}
              />
              我有权在本项目使用并存储这些坐标、自由度映射、来源与许可信息。此声明不会自动公开数据。
            </label>
            <div className="button-row">
              <Button
                primary
                disabled={busy || !authorized}
                busy={busy}
                onClick={upload}
              >
                校验并保存新版本
              </Button>
              {matrix.source === "synthetic" && matrix.n <= 512 ? (
                <Button
                  disabled={busy || !authorized}
                  onClick={() =>
                    void perform(
                      "/demo",
                      { authorized: true },
                      "已创建 CC0 解析节点链映射演示，需选择后创建新任务；不代表真实器件几何。",
                    )
                  }
                >
                  创建 CC0 解析映射演示
                </Button>
              ) : null}
            </div>
          </div>
        </details>
      )}
    </Panel>
  );
}

export function EngineeringWorkspace(props: {
  matrix: Entity;
  canManage: boolean;
  onChanged: () => void;
}) {
  return <EngineeringEditor key={props.matrix.id} {...props} />;
}
