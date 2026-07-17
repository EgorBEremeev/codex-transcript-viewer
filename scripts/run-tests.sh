#!/usr/bin/env bash
set -euo pipefail

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
build_dir="$(mktemp -d)"
trap 'rm -rf "$build_dir"' EXIT

cd "$root"
PYTHONDONTWRITEBYTECODE=1 uv run --no-project --python 3.11 --with-editable . python -m unittest discover -s tests -v
uv build --out-dir "$build_dir"
python -m json.tool .agents/plugins/marketplace.json >/dev/null
python -m json.tool plugins/codex-transcript/.codex-plugin/plugin.json >/dev/null
