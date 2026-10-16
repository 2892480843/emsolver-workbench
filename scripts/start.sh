#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ ! -x .venv-emsolver/bin/python ]]; then
  echo '请先执行 uv venv --python 3.12 .venv-emsolver 和 uv pip install --python .venv-emsolver/bin/python -e ".[test]"' >&2
  exit 2
fi
if [[ ! -f web/dist/index.html ]]; then
  (cd web && npm ci && npm run build)
fi
exec .venv-emsolver/bin/emsolver serve --host 127.0.0.1 --port "${EMSOLVER_PORT:-8000}"
