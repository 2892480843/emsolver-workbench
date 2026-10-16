export function rowNnzBins(value: unknown) {
  if (!value || typeof value !== "object") return [];
  const { counts, bin_edges: edges } = value as Record<string, unknown>;
  if (
    !Array.isArray(counts) ||
    !Array.isArray(edges) ||
    !counts.length ||
    counts.length > 64 ||
    edges.length !== counts.length + 1
  )
    return [];
  if (
    !counts.every(
      (item) =>
        typeof item === "number" && Number.isSafeInteger(item) && item >= 0,
    ) ||
    !edges.every(
      (item, index) =>
        typeof item === "number" &&
        Number.isFinite(item) &&
        (!index || item > edges[index - 1]),
    )
  )
    return [];
  return counts.map((rows: number, index: number) => ({
    start: edges[index] as number,
    end: edges[index + 1] as number,
    rows,
    includesEnd: index === counts.length - 1,
  }));
}
