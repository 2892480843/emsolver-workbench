import { Entity, number } from "./api";
import { rowNnzBins } from "./components/histogram";

export function RowNnzHistogram({ histogram }: { histogram: unknown }) {
  const bins = rowNnzBins(histogram);
  if (!bins.length)
    return (
      <p className="telemetry-note">
        当前矩阵未提供有效的每行非零元分布；旧资产可重新检查后获取。
      </p>
    );
  const max = Math.max(...bins.map((bin) => bin.rows), 1);
  const start = bins[0].start;
  const end = bins.at(-1)!.end;
  const x = (value: number) => 65 + ((value - start) / (end - start)) * 565;
  const rows = bins.reduce((total, bin) => total + bin.rows, 0);
  return (
    <figure className="vector-chart paper-figure">
      <svg
        viewBox="0 0 690 250"
        role="img"
        aria-label={`每行非零元分布，${bins.length}个桶，共统计${rows}行；横轴每行非零元数，纵轴行数`}
      >
        {[0, 0.5, 1].map((ratio) => (
          <g key={ratio}>
            <line
              x1="65"
              x2="630"
              y1={190 - ratio * 155}
              y2={190 - ratio * 155}
              stroke="#D4D4D0"
            />
            <text x="8" y={194 - ratio * 155}>
              {number(max * ratio)}
            </text>
          </g>
        ))}
        {bins.map((bin, index) => (
          <rect
            key={index}
            x={x(bin.start)}
            y={190 - (bin.rows / max) * 155}
            width={Math.max(0.5, x(bin.end) - x(bin.start) - 1)}
            height={(bin.rows / max) * 155}
            fill="#111111"
          >
            <title>
              每行非零元 [{bin.start}, {bin.end}
              {bin.includesEnd ? "]" : ")"}：{bin.rows} 行
            </title>
          </rect>
        ))}
        <text x="8" y="18">
          行数
        </text>
        <text x="65" y="211">
          {number(start, 4)}
        </text>
        <text x="590" y="211">
          {number(end, 4)}
        </text>
        <text x="270" y="236">
          每行非零元数（个）
        </text>
      </svg>
      <figcaption className="paper-caption">
        行非零元分布。{bins.length} 个实际统计桶 / 最多64桶，共 {number(rows)}{" "}
        行。悬停查看区间与行数；最后一个桶包含右端点。
      </figcaption>
    </figure>
  );
}
export function SparsityPlot({ matrix }: { matrix: Entity }) {
  const info = matrix.diagnostics || matrix;
  const points = info.preview || [];
  const n = matrix.n || info.n || 1;
  return (
    <figure className="sparsity paper-figure">
      <svg
        viewBox="0 0 340 300"
        role="img"
        aria-label={`矩阵非零元分布，维度 ${n}`}
      >
        <rect
          x="34"
          y="8"
          width="270"
          height="270"
          fill="#FFFFFF"
          stroke="#D4D4D0"
        />
        {points.map((p: number[], i: number) => (
          <rect
            key={i}
            x={34 + (p[1] / n) * 268}
            y={8 + (p[0] / n) * 268}
            width={Math.max(1.2, 270 / n)}
            height={Math.max(1.2, 270 / n)}
            fill="#111111"
          />
        ))}
        <text x="34" y="295">
          0
        </text>
        <text x="277" y="295">
          {n}
        </text>
        <text x="10" y="17">
          0
        </text>
        <text x="5" y="277">
          {n}
        </text>
      </svg>
      <figcaption className="paper-caption">
        矩阵非零结构。横轴为列号，纵轴为行号；最多展示 1,500 个实际采样点。
      </figcaption>
    </figure>
  );
}
export function ResidualChart({
  residuals,
  threshold = 1e-8,
}: {
  residuals: number[];
  threshold?: number;
}) {
  if (!residuals.length)
    return (
      <div className="chart-empty">
        暂无独立验证残差；任务产出后显示真实测量值。
      </div>
    );
  const logs = residuals.map((v) => Math.log10(Math.max(v, 1e-18)));
  const min = Math.min(...logs, Math.log10(threshold)) - 1,
    max = Math.max(...logs, Math.log10(threshold)) + 1;
  const y = (v: number) => 28 + ((max - v) / (max - min)) * 180;
  const x = (i: number) => 58 + (i / Math.max(1, residuals.length - 1)) * 555;
  return (
    <figure className="residual-chart paper-figure">
      <svg
        viewBox="0 0 660 250"
        role="img"
        aria-label="独立验证残差，纵轴为以十为底的对数"
      >
        <g>
          {Array.from({ length: 5 }, (_, i) => {
            const v = max - (i / 4) * (max - min);
            return (
              <g key={i}>
                <line x1="58" x2="620" y1={y(v)} y2={y(v)} stroke="#D4D4D0" />
                <text x="7" y={y(v) + 4}>{`10^${Math.round(v)}`}</text>
              </g>
            );
          })}
        </g>
        <line
          x1="58"
          x2="620"
          y1={y(Math.log10(threshold))}
          y2={y(Math.log10(threshold))}
          stroke="#111111"
          strokeDasharray="5 5"
        />
        <path
          d={logs.map((v, i) => `${i ? "L" : "M"}${x(i)},${y(v)}`).join(" ")}
          fill="none"
          stroke="#111111"
          strokeWidth="2"
        />
        {logs.map((v, i) => (
          <circle key={i} cx={x(i)} cy={y(v)} r="3.5" fill="#111111">
            <title>{`模式 ${i + 1}: ${number(residuals[i])}`}</title>
          </circle>
        ))}
        <text x="58" y="237">
          模式 01
        </text>
        <text
          x="550"
          y="237"
        >{`模式 ${residuals.length.toString().padStart(2, "0")}`}</text>
      </svg>
      <figcaption className="paper-caption">
        独立验证残差。实线与圆点为实际重算值，虚线为验收阈值{" "}
        {threshold.toExponential(0)}；纵轴采用十进制对数。
      </figcaption>
    </figure>
  );
}
export function Spectrum({
  values,
  sigma,
}: {
  values: number[];
  sigma: number;
}) {
  if (!values.length)
    return <div className="chart-empty">完成求解后在此显示实际谱分布。</div>;
  const low = Math.min(...values, sigma),
    high = Math.max(...values, sigma),
    span = high - low || 1;
  const x = (v: number) => 35 + ((v - low) / span) * 570;
  return (
    <figure className="paper-figure">
      <svg
        className="spectrum"
        viewBox="0 0 640 105"
        role="img"
        aria-label="相对目标位移的特征值分布"
      >
        <line x1="35" y1="52" x2="605" y2="52" stroke="#D4D4D0" />
        {values.map((v, i) => (
          <line
            key={i}
            x1={x(v)}
            x2={x(v)}
            y1={32 - (i % 3) * 6}
            y2={68 + (i % 3) * 6}
            stroke="#111111"
            strokeWidth="2"
          >
            <title>{number(v, 8)}</title>
          </line>
        ))}
        <line
          x1={x(sigma)}
          x2={x(sigma)}
          y1="13"
          y2="88"
          stroke="#111111"
          strokeDasharray="3 3"
        />
        <text x="35" y="99">
          {number(low, 5)}
        </text>
        <text x="550" y="99">
          {number(high, 5)}
        </text>
        <text x={Math.min(x(sigma) + 6, 530)} y="15">
          σ = {number(sigma)}
        </text>
      </svg>
      <figcaption className="paper-caption">
        相对目标位移的实际谱分布。实线为特征值，虚线为目标位移 σ。
      </figcaption>
    </figure>
  );
}
