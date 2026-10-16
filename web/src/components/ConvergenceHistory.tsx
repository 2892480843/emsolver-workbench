import { useState, type ReactNode } from "react";
import { number, type Entity } from "../api";
import { convergenceHistory, type ResidualPoint } from "./convergence";

const seriesStyles = [
  { dash: undefined, label: "实线 / 圆点" },
  { dash: "7 3", label: "长虚线 / 方点" },
  { dash: "2 3", label: "点线 / 菱形点" },
  { dash: "9 3 2 3", label: "点划线 / 三角点" },
  { dash: "9 3 2 3 2 3", label: "双点划线 / 叉点" },
  { dash: "3 3 9 3", label: "短长虚线 / 十字点" },
];
const markerLabels = {
  failure: "F",
  shift: "S",
  lock: "L",
  restart: "R",
  fallback: "B",
};
const markerNames = {
  failure: "失败 / 终止",
  shift: "工作位移",
  lock: "软锁定",
  restart: "重启",
  fallback: "回退",
};

function ResidualMarker({
  x,
  y,
  style,
  locked,
  children,
}: {
  x: number;
  y: number;
  style: number;
  locked: boolean | null;
  children: ReactNode;
}) {
  const radius = locked ? 3.5 : 2;
  const fill = locked ? "#111111" : "#FFFFFF";
  let marker;
  switch (style) {
    case 1:
      marker = (
        <rect
          x={-radius}
          y={-radius}
          width={radius * 2}
          height={radius * 2}
          fill={fill}
        />
      );
      break;
    case 2:
      marker = (
        <path
          d={`M0 ${-radius}L${radius} 0L0 ${radius}L${-radius} 0Z`}
          fill={fill}
        />
      );
      break;
    case 3:
      marker = (
        <path d={`M0 ${-radius}L${radius} ${radius}H${-radius}Z`} fill={fill} />
      );
      break;
    case 4:
      marker = (
        <path
          d={`M${-radius} ${-radius}L${radius} ${radius}M${radius} ${-radius}L${-radius} ${radius}`}
          fill="none"
        />
      );
      break;
    case 5:
      marker = (
        <path
          d={`M0 ${-radius}V${radius}M${-radius} 0H${radius}`}
          fill="none"
        />
      );
      break;
    default:
      marker = <circle r={radius} fill={fill} />;
  }
  return (
    <g
      transform={`translate(${x},${y})`}
      stroke="#111111"
      strokeWidth={locked ? 1.8 : 1}
    >
      <title>{children}</title>
      {marker}
    </g>
  );
}

export function ConvergenceHistory({
  events,
  method,
  threshold,
  truncated = false,
}: {
  events: Entity[];
  method?: string;
  threshold?: number;
  truncated?: boolean;
}) {
  const [page, setPage] = useState(0);
  const overview = convergenceHistory(events, 0, 1);
  const firstRank = Math.min(
    page * 6,
    Math.max(0, Math.floor((overview.modeCount - 1) / 6) * 6),
  );
  const history = convergenceHistory(events, firstRank);
  const values = history.series.flatMap((line) =>
    line.points.map((point) => point.residual),
  );
  const validThreshold =
    typeof threshold === "number" && Number.isFinite(threshold) && threshold > 0
      ? threshold
      : null;
  const log = (value: number) => Math.log10(Math.max(value, 1e-18));
  const levels = [
    ...values,
    ...history.attempts.map((point) => point.residual),
    ...(validThreshold ? [validThreshold] : []),
  ].map(log);
  const low = Math.min(...levels, -8) - 0.5;
  const high = Math.max(...levels, -8) + 0.5;
  const first = events[0]?.seq || 1;
  const last = events.at(-1)?.seq || first;
  const x = (seq: number) =>
    60 + ((seq - first) / Math.max(1, last - first)) * 590;
  const y = (value: number) => 20 + ((high - log(value)) / (high - low)) * 165;
  const path = (points: ResidualPoint[]) =>
    points
      .map(
        (point, index) =>
          `${index && points[index - 1].segment === point.segment ? "L" : "M"}${x(point.seq)},${y(point.residual)}`,
      )
      .join(" ");
  return (
    <div className="vector-chart">
      {history.modeCount > 6 ? (
        <label className="queue-actions">
          候选排序位置
          <select
            value={firstRank / 6}
            onChange={(event) => setPage(Number(event.target.value))}
          >
            {Array.from(
              { length: Math.ceil(history.modeCount / 6) },
              (_, index) => (
                <option key={index} value={index}>
                  {index * 6 + 1}–{Math.min(history.modeCount, index * 6 + 6)}
                </option>
              ),
            )}
          </select>
        </label>
      ) : null}
      {history.series.length || history.attempts.length ? (
        <figure className="paper-figure">
          <svg
            viewBox="0 0 710 240"
            role="img"
            aria-label="实际原问题残差历史，横轴为事件序号，纵轴为十进制对数；竖线标注真实策略事件"
          >
            {Array.from({ length: 5 }, (_, index) => {
              const exponent = high - ((high - low) * index) / 4;
              const position = 20 + (index / 4) * 165;
              return (
                <g key={index}>
                  <line
                    x1="60"
                    x2="650"
                    y1={position}
                    y2={position}
                    stroke="#D4D4D0"
                  />
                  <text x="5" y={position + 4}>
                    10^{exponent.toFixed(1)}
                  </text>
                </g>
              );
            })}
            {history.stalls.map((stall) => (
              <rect
                key={stall.start}
                x={x(stall.start)}
                y="20"
                width={Math.max(3, x(stall.end) - x(stall.start))}
                height="165"
                fill="#F5F5F0"
              >
                <title>
                  后台停滞观测 #{stall.start}–{stall.end}，累计 {stall.checks}{" "}
                  次
                  {stall.threshold !== null ? `，阈值 ${stall.threshold}` : ""}
                </title>
              </rect>
            ))}
            {validThreshold ? (
              <line
                x1="60"
                x2="650"
                y1={y(validThreshold)}
                y2={y(validThreshold)}
                stroke="#6B6B66"
                strokeDasharray="5 4"
              >
                <title>最终独立验证阈值 {validThreshold}</title>
              </line>
            ) : null}
            {history.series.map((line, index) => (
              <g key={line.rank}>
                <path
                  d={path(line.points)}
                  fill="none"
                  stroke="#111111"
                  strokeDasharray={seriesStyles[index].dash}
                  strokeWidth="1.6"
                />
                {line.points.map((point) => (
                  <ResidualMarker
                    key={point.seq}
                    x={x(point.seq)}
                    y={y(point.residual)}
                    style={index}
                    locked={point.locked}
                  >
                    排序位置 {line.rank + 1}；事件 #{point.seq}；外层{" "}
                    {point.iteration ?? "不可用"}；残差 {point.residual}；软锁定{" "}
                    {point.locked === null
                      ? "不可用"
                      : point.locked
                        ? "是"
                        : "否"}
                    ；工作位移 {point.sigmaWork ?? "不可用"}
                  </ResidualMarker>
                ))}
              </g>
            ))}
            {history.attempts.map((point) => (
              <rect
                key={point.seq}
                x={x(point.seq) - 3}
                y={y(point.residual) - 3}
                width="6"
                height="6"
                fill="#D4D4D0"
                stroke="#111111"
              >
                <title>
                  尝试末态最大残差 #{point.seq}：{point.residual}
                </title>
              </rect>
            ))}
            {history.markers.map((marker, index) => (
              <g key={`${marker.seq}-${index}`}>
                <title>
                  #{marker.seq} {marker.label}
                  {marker.count !== undefined ? `：${marker.count}` : ""}
                </title>
                <line
                  x1={x(marker.seq)}
                  x2={x(marker.seq)}
                  y1="190"
                  y2="199"
                  stroke="#111111"
                />
                <text x={x(marker.seq)} y="212" textAnchor="middle">
                  {markerLabels[marker.kind]}
                </text>
              </g>
            ))}
            <text x="60" y="228">
              事件 #{first}
            </text>
            <text x="550" y="228">
              事件 #{last}
            </text>
          </svg>
          <figcaption className="paper-caption">
            实际原问题残差历史。线型与点形区分当前排序位置；灰色虚线为独立验证阈值，灰底方点为尝试末态最大残差。
            轴下字母标记：F 失败或终止、S 工作位移、L 软锁定、R 重启、B 回退。
          </figcaption>
        </figure>
      ) : null}
      {!history.series.length ? (
        <p className="subtle-copy">
          {method === "arpack" ||
          events.some((event) => event.event === "arpack_started")
            ? "ARPACK 不提供逐外层、逐模式原问题残差历史。"
            : "当前事件尚未提供逐模式残差历史。"}
          尝试末态残差以方块显示，不能代替逐模式曲线。
        </p>
      ) : (
        <>
          <div className="benchmark-summary">
            {history.series.map((line, index) => (
              <span key={line.rank}>
                排序位置 {line.rank + 1}（{seriesStyles[index].label}）·{" "}
                {line.measuredCount} 次测量 · 末值{" "}
                {number(line.points.at(-1)?.residual)}
              </span>
            ))}
          </div>
          <p className="subtle-copy">
            曲线按当前 Ritz
            候选的目标距离排序位置对应，不代表跨迭代固定的物理模式身份。加粗大点仅表示后台明确报告的软锁定位置；缺少位置时只展示实际数量。小于
            10⁻¹⁸的值按10⁻¹⁸绘图，悬停显示原值。每条曲线最多240点，保留每个采样桶的极大/极小值及首末值。
          </p>
        </>
      )}
      <p className="subtle-copy">
        {history.stagnationAvailable
          ? `浅色区间为后台报告的停滞观测，共 ${history.stalls.length} 段。`
          : "当前事件未提供停滞计数，停滞区间不可用。"}
        {truncated
          ? "当前仅含最近1000条事件，较早曲线、峰值及关键事件请下载完整事件。"
          : "图和下列标记均来自当前已读取事件；关键标记不参与曲线采样。"}
      </p>
      {history.markers.length ? (
        <details>
          <summary>真实策略标记（{history.markers.length}，未减采样）</summary>
          <div className="event-log">
            {history.markers.map((marker, index) => (
              <div className="event-row" key={`${marker.seq}-${index}`}>
                <span className="event-seq">#{marker.seq}</span>
                <span>
                  {markerLabels[marker.kind]} · {markerNames[marker.kind]}
                </span>
                <span>
                  {marker.label}
                  {marker.count !== undefined ? `：${marker.count}` : ""}
                </span>
              </div>
            ))}
          </div>
        </details>
      ) : null}
    </div>
  );
}
