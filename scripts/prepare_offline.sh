#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python_bin="${EMSOLVER_PYTHON:-.venv-emsolver/bin/python}"
offline_dir="${1:-artifacts/offline}"
if [[ -e "$offline_dir" ]]; then
  echo '输出目录已存在，请选择新目录，避免覆盖既有交付包。' >&2
  exit 2
fi
mkdir -p "$offline_dir/wheels"
if ! "$python_bin" -m pip --version >/dev/null 2>&1; then
  "$python_bin" -m ensurepip
fi
# Run this on the same OS, architecture and Python version as the offline host.
"$python_bin" -m pip download --only-binary=:all: -r requirements.lock -d "$offline_dir/wheels"
"$python_bin" -m pip wheel --no-deps . -w "$offline_dir/wheels"
if [[ ! -f web/dist/index.html ]]; then
  (cd web && npm ci && npm run build)
fi
cp -R web/dist "$offline_dir/web-dist"
cp requirements.lock pyproject.toml LICENSE NOTICE.md README.md "$offline_dir/"
cp -R docs examples benchmarks "$offline_dir/"
mkdir -p "$offline_dir/scripts"
cp scripts/verify_offline_instance.py "$offline_dir/scripts/"
"$python_bin" -c 'import sys; print("%d.%d" % sys.version_info[:2])' > "$offline_dir/python-version.txt"
cat > "$offline_dir/install.sh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
python_bin="${EMSOLVER_PYTHON:-python$(cat python-version.txt)}"
"$python_bin" -c 'import sys; expected=open("python-version.txt").read().strip(); assert "%d.%d" % sys.version_info[:2] == expected, "Use the Python version recorded in python-version.txt"'
"$python_bin" -m venv .venv
.venv/bin/python -m pip install --no-index --find-links wheels -r requirements.lock
.venv/bin/python -m pip install --no-index --find-links wheels emsolver-workbench
mkdir -p web
cp -R web-dist web/dist
export EMSOLVER_WEB_DIST="$PWD/web/dist"
export EMSOLVER_RESOURCE_DIR="$PWD"
exec .venv/bin/emsolver serve --host 127.0.0.1 --port "${EMSOLVER_PORT:-8000}"
SH
chmod +x "$offline_dir/install.sh"
"$python_bin" -c 'import platform,sys; print(platform.platform()); print(sys.version)' > "$offline_dir/platform.txt"
echo "离线目录已生成：${offline_dir}；仅适用于platform.txt所记录的兼容平台。"
