import { useEffect, useRef, useState } from "react";
import { api, number, type Entity } from "../api";
import { readModeComparison } from "../models/workspace";
import { Button, Json, Metric, Notice, Panel } from "../ui";

export function ModeComparison({
  ids,
  jobs,
}: {
  ids: string[];
  jobs: Entity[];
}) {
  const fingerprint = ids.join("\0");
  const current = useRef(fingerprint);
  current.current = fingerprint;
  const request = useRef<AbortController | null>(null);
  const [result, setResult] = useState<{
    fingerprint: string;
    value: Entity;
  } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    request.current?.abort();
    setBusy(false);
    setError("");
    return () => request.current?.abort();
  }, [fingerprint]);
  const reference = jobs.find((job) => job.id === ids[0]);
  const candidate = jobs.find((job) => job.id === ids[1]);
  const eligible =
    ids.length === 2 &&
    reference &&
    candidate &&
    reference.matrix_hash &&
    reference.matrix_hash === candidate.matrix_hash &&
    [reference, candidate].every((job) =>
      ["SUCCEEDED", "PARTIAL"].includes(job.status),
    );
  const compare = async () => {
    if (!eligible || busy) return;
    const expected = fingerprint;
    const controller = new AbortController();
    request.current?.abort();
    request.current = controller;
    setBusy(true);
    setError("");
    try {
      const value = await readModeComparison(
        ids[0],
        ids[1],
        api,
        controller.signal,
      );
      if (!controller.signal.aborted && current.current === expected)
        setResult({ fingerprint: expected, value });
    } catch (failure) {
      if (!controller.signal.aborted && current.current === expected)
        setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      if (!controller.signal.aborted && current.current === expected)
        setBusy(false);
    }
  };
  const visible = result?.fingerprint === fingerprint ? result.value : null;
  return (
    <Panel
      title="跨运行模式匹配"
      eyebrow="COMPUTED MODE ALIGNMENT"
      action={
        <Button disabled={!eligible} busy={busy} onClick={() => void compare()}>
          匹配所选模式
        </Button>
      }
    >
      <p className="panel-copy">
        先选中的运行作对照，后选中的运行作候选。按特征值与对应子空间比较两个计算结果，不把对照运行当作独立真值。
      </p>
      {!eligible ? (
        <Notice>请选择同一输入哈希且已有成功或部分结果的两个运行。</Notice>
      ) : (
        <p className="subtle-copy">
          对照 {ids[0].slice(0, 12)} · 候选 {ids[1].slice(0, 12)}
        </p>
      )}
      {error ? <Notice kind="error">模式比较失败：{error}</Notice> : null}
      {visible ? (
        <>
          <Notice kind={visible.passed ? "info" : "warning"}>
            {visible.passed
              ? "两个计算结果满足本次匹配误差检查。"
              : "两个计算结果未满足本次匹配误差检查。"}{" "}
            此结论不增加任何一次运行的独立参考认证。
          </Notice>
          <div className="governance-metrics">
            <Metric label="匹配模式数" value={number(visible.matched_count)} />
            <Metric
              label="最大特征值绝对差"
              value={number(visible.max_eigenvalue_absolute_error)}
            />
            <Metric
              label="最大子空间夹角正弦"
              value={number(visible.max_subspace_sine)}
              detail="接近 0 表示所比较子空间更接近"
            />
            <Metric
              label="正交性误差"
              value={number(visible.orthogonality_error)}
            />
          </div>
          <details>
            <summary>模式对应、模式簇与完整比较记录</summary>
            <Json value={visible} />
          </details>
        </>
      ) : null}
    </Panel>
  );
}
