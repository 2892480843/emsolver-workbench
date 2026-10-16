import { useState } from "react";
import { api, post, type Entity } from "../api";
import { matrixMetadataPayload, safeSourceUrl } from "../models/workspace";
import { Button, Field, Json, Notice } from "../ui";

type Props = {
  matrix: Entity;
  canEdit: boolean;
  canShare: boolean;
  onSaved: (matrix: Entity) => void;
  onDerived: (matrix: Entity) => void;
  notify: (message: string, error?: boolean) => void;
};

export function MatrixProvenance({
  matrix,
  canEdit,
  canShare,
  onSaved,
  onDerived,
  notify,
}: Props) {
  const [draft, setDraft] = useState<Entity>(() => ({
    classification: "internal",
    ...matrix.metadata,
  }));
  const [busy, setBusy] = useState(false);
  const [triangle, setTriangle] = useState("upper");
  const change = (key: string, value: unknown) =>
    setDraft((previous) => ({ ...previous, [key]: value }));
  const save = async () => {
    if (busy || !canEdit) return;
    try {
      const metadata: Entity = matrixMetadataPayload(draft);
      if (!canShare) delete metadata.public;
      setBusy(true);
      const updated = await api(`/matrices/${encodeURIComponent(matrix.id)}`, {
        method: "PATCH",
        body: JSON.stringify({ metadata, expected_version: matrix.version ?? 1 }),
      });
      onSaved(updated);
      notify("来源与许可已保存为新版本，历史运行保留原冻结记录");
    } catch (error) {
      notify(error instanceof Error ? error.message : String(error), true);
    } finally {
      setBusy(false);
    }
  };
  const derive = async () => {
    if (busy || matrix.archived || !canEdit) return;
    setBusy(true);
    try {
      const derived = await post(
        `/matrices/${encodeURIComponent(matrix.id)}/derive`,
        { triangle },
      );
      onDerived(derived);
      notify("已创建三角派生资产，原始矩阵与变换来源均保留");
    } catch (error) {
      notify(error instanceof Error ? error.message : String(error), true);
    } finally {
      setBusy(false);
    }
  };
  return (
    <details className="matrix-provenance">
      <summary>
        来源、许可与工程说明 · 版本 {matrix.metadata_version ?? 1}
      </summary>
      {!canEdit ? (
        <Notice>
          当前身份只读；分析者及项目管理员可编辑来源和创建派生资产。
        </Notice>
      ) : null}
      <div className="form-grid">
        <Field label="数据来源链接" hint="填写可追溯的 HTTP / HTTPS 来源地址">
          <input
            value={draft.source_url || ""}
            onChange={(event) => change("source_url", event.target.value)}
          />
        </Field>
        <Field label="数据许可" hint="公开数值结果前必须确认许可范围">
          <input
            value={draft.license || ""}
            onChange={(event) => change("license", event.target.value)}
          />
        </Field>
        <Field label="物理单位说明">
          <input
            value={draft.units || ""}
            onChange={(event) => change("units", event.target.value)}
            placeholder="留空代表尚未确认"
          />
        </Field>
        <Field
          label="特征值到频率的映射"
          hint="仅在模型确认 λ = ω² 时启用；σ 不等同于 Hz"
        >
          <select
            value={draft.frequency_mapping || ""}
            onChange={(event) =>
              change("frequency_mapping", event.target.value)
            }
          >
            <option value="">未确认 · 仅显示特征值</option>
            <option value="lambda=omega_squared">已确认 λ = ω²</option>
          </select>
        </Field>
      </div>
      <div className="form-grid">
        <Field label="数据用途">
          <input
            value={draft.purpose || ""}
            onChange={(event) => change("purpose", event.target.value)}
          />
        </Field>
        <Field label="数据分级">
          <select
            value={draft.classification || "internal"}
            onChange={(event) => change("classification", event.target.value)}
          >
            <option value="public">公开</option>
            <option value="internal">内部</option>
            <option value="confidential">保密</option>
          </select>
        </Field>
        <Field label="生成脚本或方法">
          <input
            value={draft.generator_script || ""}
            onChange={(event) => change("generator_script", event.target.value)}
          />
        </Field>
        <Field label="随机种子">
          <input
            value={draft.seed ?? ""}
            onChange={(event) => change("seed", event.target.value)}
          />
        </Field>
      </div>
      <Field label="允许公开的范围">
        <input
          value={draft.public_scope || ""}
          onChange={(event) => change("public_scope", event.target.value)}
          placeholder="例如仅脱敏统计、可公开输入及结果；留空代表尚未确认"
        />
      </Field>
      <Field
        label="用途与来源说明"
        hint="记录适用范围、生成方法及所需的工程前提"
      >
        <textarea
          rows={3}
          maxLength={2000}
          value={draft.description || ""}
          onChange={(event) => change("description", event.target.value)}
        />
      </Field>
      <label className="checkbox">
        <input
          type="checkbox"
          checked={draft.public === true}
          disabled={!canShare}
          onChange={(event) => change("public", event.target.checked)}
        />
        <span>
          许可允许公开此输入的数值结果
          <small>声明只作用于此后冻结的新运行；未确认权限时保持关闭。</small>
        </span>
      </label>
      {safeSourceUrl(draft.source_url) ? (
        <a
          className="knowledge-link"
          href={safeSourceUrl(draft.source_url)}
          target="_blank"
          rel="noreferrer"
        >
          查看登记来源
        </a>
      ) : null}
      {matrix.source === "synthetic" ? (
        <Notice>
          此输入为合成数据，不能作为真实器件或正式工程数据的证据。
        </Notice>
      ) : null}
      <Button
        busy={busy}
        disabled={matrix.archived || !canEdit}
        onClick={() => void save()}
      >
        保存元数据新版本
      </Button>
      <details>
        <summary>已有生成记录与来源字段</summary>
        <Json value={matrix.metadata || {}} />
      </details>
      <details>
        <summary>显式创建三角派生资产</summary>
        <p className="subtle-copy">
          仅在确认需要从完整矩阵提取三角时使用。操作创建新资产并记录来源，不覆盖当前矩阵。
        </p>
        <Field label="提取三角">
          <select
            value={triangle}
            onChange={(event) => setTriangle(event.target.value)}
          >
            <option value="upper">上三角</option>
            <option value="lower">下三角</option>
          </select>
        </Field>
        <Button
          disabled={
            matrix.archived || matrix.storage !== "full" || busy || !canEdit
          }
          busy={busy}
          onClick={() => void derive()}
        >
          创建派生资产
        </Button>
        {matrix.storage !== "full" ? (
          <p className="subtle-copy">仅完整存储的矩阵可提取三角。</p>
        ) : null}
      </details>
    </details>
  );
}
