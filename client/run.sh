#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if [[ ! -x .venv/bin/python ]]; then
  echo 'VoxChat is not installed yet. In this folder, run: uv venv --python 3.12 && uv pip install --require-hashes -r requirements.lock'
  exit 1
fi
exec .venv/bin/python desktop.py "$@"
