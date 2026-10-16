import { useEffect, useId, useState } from "react";
import { api, number, type Entity } from "../api";
import { Badge, Field, Notice, Panel } from "../ui";
import {
  fieldSelection,
  projectField,
  type Plane,
  type Quantity,
} from "./field-mapping";
import "./engineering.css";

function FieldPlot({
  preview,
  plane,
  quantity,
}: {
  preview: Entity;
  plane: Plane;
  quantity: Quantity;
}) {
  const title = useId();
  let projected;
  try {
    projected = projectField(preview.points, plane, quantity);
  } catch (error) {
    return <Notice kind="error">{String(error)}</Notice>;
  }
  const byNode = new Map(
    projected.points.map((point) => [point.node_id, point]),
  );
  const shades = ["#FFFFFF", "#D4D4D0", "#6B6B66", "#111111"];
  const shade = (value: number) => {
    const fraction =
      projected.maximum === projected.minimum
        ? 1
        : (value - projected.minimum) / (projected.maximum - projected.minimum);
    return shades[
      Math.min(shades.length - 1, Math.floor(fraction * shades.length))
    ];
  };
  const quantityLabel =
    quantity === "amplitude" ? "幅值" : quantity === "real" ? "实部" : "虚部";
  const legend =
    projected.minimum === projected.maximum
      ? [{ fill: shades[3], label: number(projected.minimum, 6) }]
      : shades.map((fill, index) => ({
          fill,
          label: `${number(projected.minimum + ((projected.maximum - projected.minimum) * index) / shades.length, 6)}–${number(projected.minimum + ((projected.maximum - projected.minimum) * (index + 1)) / shades.length, 6)}${index === shades.length - 1 ? "（含上界）" : "（不含上界）"}`,
        }));
  return (
    <>
      <figure className="paper-figure">
        <svg
          className="engineering-plot"
          viewBox="0 0 640 360"
          role="img"
          aria-labelledby={title}
        >
          <title id={title}>
            {plane} 真实节点投影；灰度为{quantityLabel}
            ，坐标单位 {preview.coordinate_units}
          </title>
          <rect x="0" y="0" width="640" height="360" fill="#FFFFFF" />
          {(preview.cells || []).map((cell: Entity, index: number) => {
            const points = cell.nodes.map((id: number) => byNode.get(id));
            if (points.some((point: unknown) => !point)) return null;
            const pairs =
              cell.kind === "line"
                ? [[0, 1]]
                : cell.kind === "triangle"
                  ? [
                      [0, 1],
                      [1, 2],
                      [2, 0],
                    ]
                  : [
                      [0, 1],
                      [0, 2],
                      [0, 3],
                      [1, 2],
                      [1, 3],
                      [2, 3],
                    ];
            return (
              <g key={index} stroke="#D4D4D0" strokeWidth=".8">
                {pairs.map(([a, b]) => (
                  <line
                    key={`${a}-${b}`}
                    x1={points[a].x}
                    y1={points[a].y}
                    x2={points[b].x}
                    y2={points[b].y}
                  />
                ))}
              </g>
            );
          })}
          {projected.points.map((point) => (
            <circle
              key={point.node_id}
              cx={point.x}
              cy={point.y}
              r="4"
              fill={shade(point.value)}
              stroke="#111111"
              strokeWidth=".5"
            >
              <title>
                节点 {point.node_id} · [{point.coordinate.join(", ")}] ·{" "}
                {point.value}
              </title>
            </circle>
          ))}
          <text x="12" y="20" fill="currentColor" fontSize="12">
            {plane[1]} [{number(projected.vertical[0])},{" "}
            {number(projected.vertical[1])}] {preview.coordinate_units}
          </text>
          <text
            x="628"
            y="344"
            textAnchor="end"
            fill="currentColor"
            fontSize="12"
          >
            {plane[0]} [{number(projected.horizontal[0])},{" "}
            {number(projected.horizontal[1])}] {preview.coordinate_units}
          </text>
        </svg>
        <figcaption className="paper-caption">
          真实节点在 {plane} 平面的投影。{quantityLabel}以离散灰度表示，单位{" "}
          {preview.field_units}；悬停或展开下表查看原始数值。
          <span className="engineering-legend">
            {legend.map((entry) => (
              <span key={entry.fill}>
                <span
                  aria-hidden="true"
                  style={{
                    display: "inline-block",
                    width: "0.75em",
                    height: "0.75em",
                    marginRight: "0.35em",
                    backgroundColor: entry.fill,
                    border: "1px solid #111111",
                  }}
                />
                {entry.label}
              </span>
            ))}
          </span>
        </figcaption>
      </figure>
      <details>
        <summary>查看本图实际采样值（{preview.points.length} 个节点）</summary>
        <div className="table-scroll">
          <table>
            <caption className="paper-table-caption">原始自由度与工程坐标的模式分量</caption>
            <thead>
              <tr>
                <th>节点</th>
                <th>原始坐标</th>
                <th>实部</th>
                <th>虚部</th>
                <th>幅值</th>
              </tr>
            </thead>
            <tbody>
              {preview.points.map((point: Entity) => (
                <tr key={point.node_id}>
                  <td>{point.node_id}</td>
                  <td>{point.coordinate.join(", ")}</td>
                  <td>{number(point.value.real, 8)}</td>
                  <td>{number(point.value.imag, 8)}</td>
                  <td>{number(point.value.amplitude, 8)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </>
  );
}

function JobFieldMapping({ job }: { job: Entity | null }) {
  const [mode, setMode] = useState(0);
  const [component, setComponent] = useState("magnitude");
  const [plane, setPlane] = useState<Plane>("XY");
  const [quantity, setQuantity] = useState<Quantity>("amplitude");
  const [payload, setPayload] = useState<{ key: string; value: Entity } | null>(
    null,
  );
  const [failure, setFailure] = useState<{ key: string; text: string } | null>(
    null,
  );
  const key = `${job?.id}:${job?.status}:${job?.artifacts_state}:${job?.engineering_model?.sidecar_sha256}:${mode}:${component}`;
  const ready = fieldSelection(job);
  useEffect(() => {
    if (!ready || !job) return;
    const controller = new AbortController();
    api<Entity>(
      `/jobs/${encodeURIComponent(job.id)}/field-preview?${new URLSearchParams({ mode: String(mode), component, limit: "512" })}`,
      { signal: controller.signal },
    )
      .then((value) => {
        if (!controller.signal.aborted) {
          setFailure(null);
          setPayload({ key, value });
        }
      })
      .catch((error) => {
        if (!controller.signal.aborted)
          setFailure({
            key,
            text: error instanceof Error ? error.message : String(error),
          });
      });
    return () => controller.abort();
  }, [key, ready, job?.id, mode, component]);
  const preview = payload?.key === key ? payload.value : null;
  const error = failure?.key === key ? failure.text : "";
  const model = job?.engineering_model;
  const count = job?.result?.n_accepted;
  return (
    <Panel title="工程模型与模式分量" eyebrow="FROZEN MODEL · MEASURED FIELD">
      {!ready ? (
        <Notice>
          {!model
            ? "本次任务未冻结工程映射。请先在矩阵页上传并选择有权使用的模型，再创建新任务；历史结果不会补绑几何。"
            : "需任务完成并保留可用的向量文件，才能读取实际模式分量。"}
        </Notice>
      ) : (
        <>
          <p className="panel-copy">
            模型 {model.model_version} · {model.n_nodes} 节点 / {model.n_dofs}{" "}
            自由度 · {model.license}。模型哈希{" "}
            <span className="mono">{model.sidecar_sha256}</span>
          </p>
          {model.model_kind === "analytic_demo" ? (
            <Notice>
              此为 CC0
              解析节点链演示，仅验证映射流程，不是器件网格或工业电磁场。
            </Notice>
          ) : null}
          <div className="engineering-controls">
            <Field label="模式编号（从 1 开始）">
              <input
                type="number"
                min="1"
                max={typeof count === "number" && count > 0 ? count : undefined}
                value={mode + 1}
                onChange={(event) => {
                  const value = Number(event.target.value);
                  if (Number.isSafeInteger(value) && value >= 1)
                    setMode(value - 1);
                }}
              />
            </Field>
            <Field label="已映射分量">
              <select
                value={component}
                onChange={(event) => {
                  setComponent(event.target.value);
                  setQuantity("amplitude");
                }}
              >
                <option value="magnitude">已提供分量的模长</option>
                {(model.components || ["scalar", "x", "y", "z"]).map(
                  (value: string) => (
                    <option key={value} value={value}>
                      {value}
                    </option>
                  ),
                )}
              </select>
            </Field>
            <Field label="真实坐标投影">
              <select
                value={plane}
                onChange={(event) => setPlane(event.target.value as Plane)}
              >
                {["XY", "XZ", "YZ"].map((value) => (
                  <option key={value}>{value}</option>
                ))}
              </select>
            </Field>
            <Field label="灰度测量值">
              <select
                value={quantity}
                onChange={(event) =>
                  setQuantity(event.target.value as Quantity)
                }
              >
                <option value="amplitude">幅值</option>
                {component !== "magnitude" ? (
                  <>
                    <option value="real">实部</option>
                    <option value="imag">虚部</option>
                  </>
                ) : null}
              </select>
            </Field>
          </div>
          {error ? (
            <Notice kind="error">{error}</Notice>
          ) : !preview ? (
            <p role="status">
              正在校验冻结模型与结果哈希，读取至多 512 个节点…
            </p>
          ) : (
            <>
              <div className="engineering-legend">
                <span>
                  数值验证 <Badge status={preview.numerical_status} />
                </span>
                <span>
                  目标认证 <Badge status={preview.target_certification} />
                </span>
              </div>
              <FieldPlot preview={preview} plane={plane} quantity={quantity} />
              <p className="panel-copy">
                实际显示 {preview.points.length} / {preview.total_mapped_nodes}{" "}
                个有映射的节点。仅绘制采样内完整单元（{preview.cells.length} /{" "}
                {preview.cells_total}
                ）；不插值、不补造缺失分量。三轴不齐全时模长仅包含已提供分量；投影重叠不代表相同空间位置。场图不增加数值或物理认证。
              </p>
            </>
          )}
        </>
      )}
    </Panel>
  );
}

export function FieldMapping({ job }: { job: Entity | null }) {
  return <JobFieldMapping key={job?.id || "no-job"} job={job} />;
}
