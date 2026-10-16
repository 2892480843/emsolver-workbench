import { useId, type ReactNode } from "react";
const statusLabels: Record<string, string> = {
  SUCCEEDED: "验证通过",
  PARTIAL: "部分结果 · 未认证",
  FAILED: "运行失败",
  CANCELLED: "已取消",
  RUNNING: "运行中",
  VALIDATING: "独立验证中",
  VERIFYING: "独立验证中",
  CANCELLING: "取消中",
  QUEUED: "排队中",
  CREATED: "已创建",
  WORKER_LOST: "工作进程丢失",
  TIMEOUT: "超时",
  TIMED_OUT: "已超时",
  ARCHIVED: "已归档",
  RESOURCE_LIMIT: "资源超限",
  READY: "可用",
  PASSED: "通过",
  PASS: "通过",
  FAIL: "未通过",
  UNVERIFIED: "未认证",
  NOT_CERTIFIED: "未认证",
  CERTIFIED: "已认证",
};
export function Badge({ status }: { status?: string }) {
  const value = status || "UNKNOWN";
  return (
    <span className={`badge ${value.toLowerCase()}`}>
      {statusLabels[value] || value}
    </span>
  );
}
export function Button({
  children,
  primary = false,
  busy = false,
  className = "",
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & {
  children: ReactNode;
  primary?: boolean;
  busy?: boolean;
}) {
  return (
    <button
      {...props}
      disabled={props.disabled || busy}
      aria-busy={busy || undefined}
      className={`button font-serif rounded-none transition-colors duration-200 ${primary ? "primary" : ""} ${className}`}
    >
      {busy ? <span className="paper-busy">处理中…</span> : null}
      {children}
    </button>
  );
}
export function Panel({
  title,
  action,
  children,
  className = "",
}: {
  title?: ReactNode;
  eyebrow?: string;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  const titleId = useId();
  return (
    <section className={`panel ${className}`} aria-labelledby={title ? titleId : undefined}>
      {title ? (
        <div className="panel-heading">
          <div>
            <h2 id={titleId}>{title}</h2>
          </div>
          {action}
        </div>
      ) : null}
      {children}
    </section>
  );
}
export function Empty({
  title = "暂无数据",
  description = "先创建一个实验项目，开始可复现的计算流程。",
  action,
}: {
  title?: string;
  description?: string;
  action?: ReactNode;
}) {
  return (
    <div className="empty">
      <h3 className="paper-unnumbered">{title}</h3>
      <p>{description}</p>
      {action}
    </div>
  );
}
export function Notice({
  children,
  kind = "info",
}: {
  children: ReactNode;
  kind?: string;
}) {
  return (
    <div className={`notice ${kind}`} role="note">
      <strong className="paper-note-label">
        {kind === "error" ? "错误" : kind === "warning" ? "注意" : "说明"}
      </strong>
      <div>{children}</div>
    </div>
  );
}
export function Field({
  label,
  children,
  hint,
}: {
  label: string;
  children: ReactNode;
  hint?: string;
}) {
  return (
    <label className="field">
      <span>{label}</span>
      {children}
      {hint ? <small>{hint}</small> : null}
    </label>
  );
}
export function Metric({
  label,
  value,
  unit,
  detail,
}: {
  label: string;
  value: ReactNode;
  unit?: string;
  detail?: ReactNode;
}) {
  return (
    <div className="metric">
      <span>{label}</span>
      <div>
        {value}
        <small>{unit}</small>
      </div>
      {detail ? <p>{detail}</p> : null}
    </div>
  );
}
export function ArrowButton({
  children,
  onClick,
}: {
  children: ReactNode;
  onClick: () => void;
}) {
  return (
    <button className="link-button" onClick={onClick}>
      {children}
    </button>
  );
}
export function Toast({
  message,
  error,
  onClose,
}: {
  message: string;
  error: boolean;
  onClose: () => void;
}) {
  return (
    <div
      role={error ? "alert" : "status"}
      className={`toast ${error ? "error" : ""}`}
    >
      <strong>{error ? "错误." : "通知."}</strong>
      <span>{message}</span>
      <button aria-label="关闭通知" onClick={onClose}>
        关闭
      </button>
    </div>
  );
}
export function Json({ value }: { value: unknown }) {
  return <pre className="json">{JSON.stringify(value, null, 2)}</pre>;
}
