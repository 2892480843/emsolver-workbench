/** Supplied coordinates and measured samples only; projection never invents geometry. */
export type Plane = "XY" | "XZ" | "YZ";
export type Quantity = "amplitude" | "real" | "imag";
export type FieldPoint = {
  node_id: number;
  coordinate: number[];
  value: { amplitude: number; real: number | null; imag: number | null };
};

export function projectField(
  points: FieldPoint[],
  plane: Plane,
  quantity: Quantity,
) {
  if (!Array.isArray(points) || !points.length || points.length > 512)
    throw new Error("场图只接受 1–512 个实际采样节点");
  const axes: Record<Plane, [number, number]> = {
    XY: [0, 1],
    XZ: [0, 2],
    YZ: [1, 2],
  };
  if (!axes[plane] || !["amplitude", "real", "imag"].includes(quantity))
    throw new Error("投影平面或分量无效");
  const [horizontal, vertical] = axes[plane];
  const ids = new Set<number>();
  for (const point of points) {
    if (
      !Number.isSafeInteger(point.node_id) ||
      point.node_id < 0 ||
      ids.has(point.node_id) ||
      point.coordinate.length !== 3 ||
      !point.coordinate.every(Number.isFinite) ||
      typeof point.value[quantity] !== "number" ||
      !Number.isFinite(point.value[quantity])
    )
      throw new Error("节点坐标或选定测量值不可用");
    ids.add(point.node_id);
  }
  const xs = points.map((point) => point.coordinate[horizontal]);
  const ys = points.map((point) => point.coordinate[vertical]);
  const xMin = Math.min(...xs),
    xMax = Math.max(...xs),
    yMin = Math.min(...ys),
    yMax = Math.max(...ys);
  const width = 640,
    height = 360,
    padding = 42;
  const xSpan = xMax - xMin,
    ySpan = yMax - yMin;
  const scale = Math.min(
    xSpan ? (width - padding * 2) / xSpan : Infinity,
    ySpan ? (height - padding * 2) / ySpan : Infinity,
  );
  const actualScale = Number.isFinite(scale) ? scale : 1;
  const centerX = (xMin + xMax) / 2,
    centerY = (yMin + yMax) / 2;
  const values = points.map((point) => point.value[quantity] as number);
  return {
    width,
    height,
    horizontal: [xMin, xMax],
    vertical: [yMin, yMax],
    minimum: Math.min(...values),
    maximum: Math.max(...values),
    points: points.map((point, index) => ({
      ...point,
      value: values[index],
      x: width / 2 + (point.coordinate[horizontal] - centerX) * actualScale,
      y: height / 2 - (point.coordinate[vertical] - centerY) * actualScale,
    })),
  };
}

export function fieldSelection(
  job: {
    status?: string;
    artifacts_state?: string;
    engineering_model?: { id?: string };
  } | null,
) {
  return Boolean(
    job &&
    ["SUCCEEDED", "PARTIAL"].includes(job.status || "") &&
    !["EXPIRED", "MISSING"].includes(job.artifacts_state || "") &&
    job.engineering_model?.id,
  );
}

export function engineeringUpload(
  text: string,
  matrixHash: string,
  authorized: boolean,
) {
  if (!authorized) throw new Error("请确认有权使用该模型、坐标与映射");
  if (new TextEncoder().encode(text).byteLength > 16 * 1024 ** 2)
    throw new Error("模型清单不能超过 16 MiB");
  const manifest = JSON.parse(text);
  if (
    !manifest ||
    typeof manifest !== "object" ||
    Array.isArray(manifest) ||
    manifest.matrix_sha256 !== matrixHash
  )
    throw new Error("清单必须绑定当前原始矩阵的 SHA-256，不能替换矩阵哈希");
  return { manifest: { ...manifest, authorized: true } };
}
