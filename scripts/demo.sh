#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
demo_dir="${1:-runs/demo-$(date +%Y%m%d-%H%M%S)}"
if [[ -e "$demo_dir" ]]; then
  echo '演示目录已存在，请选择新目录。' >&2
  exit 2
fi
mkdir -p "$demo_dir"
bin=.venv-emsolver/bin/emsolver
"$bin" doctor --json > "$demo_dir/environment.json"
"$bin" sample --kind near_singular --n 100000 --output "$demo_dir/matrix.npz" --json
"$bin" inspect --matrix "$demo_dir/matrix.npz" --json > "$demo_dir/diagnostics.json"
"$bin" solve --matrix "$demo_dir/matrix.npz" --config examples/default.yaml --output "$demo_dir/result" --json > "$demo_dir/result.json"
"$bin" validate --matrix "$demo_dir/matrix.npz" --run "$demo_dir/result" --json > "$demo_dir/verification.json"
"$bin" report --run "$demo_dir/result" --output "$demo_dir/report.html" --json
echo "演示与独立校验完成：$demo_dir"
