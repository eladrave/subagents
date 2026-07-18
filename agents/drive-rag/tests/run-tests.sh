#!/usr/bin/env bash
set -euo pipefail
package_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
runtime_python=${DRIVE_RAG_PYTHON:-/opt/drive-rag/venv/bin/python}

if [[ ! -x "$runtime_python" ]]; then
  printf 'Drive RAG runtime Python is not executable: %s\n' "$runtime_python" >&2
  exit 1
fi

PYTHONPATH="$package_dir/skills/drive-rag/scripts${PYTHONPATH:+:$PYTHONPATH}" \
  "$runtime_python" -m pytest -q -p no:cacheprovider "$package_dir/tests"
