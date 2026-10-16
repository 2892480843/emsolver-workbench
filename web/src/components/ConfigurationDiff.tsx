import type { ConfigurationChange } from "../models/experiments";

const display = (value: unknown) => JSON.stringify(value) ?? "—";

export function ConfigurationDiff({
  changes,
  empty = "与当前矩阵的首个组合一致。",
}: {
  changes: ConfigurationChange[];
  empty?: string;
}) {
  if (!changes.length) return <p className="subtle-copy">{empty}</p>;
  return (
    <div className="table-wrap experiment-diff">
      <table>
        <caption className="paper-table-caption">冻结配置差异</caption>
        <thead>
          <tr>
            <th>字段</th>
            <th>基线值</th>
            <th>新值</th>
          </tr>
        </thead>
        <tbody>
          {changes.map((change) => (
            <tr key={change.path}>
              <td className="mono">{change.path}</td>
              <td>
                <code>{display(change.before)}</code>
              </td>
              <td>
                <code>{display(change.after)}</code>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
