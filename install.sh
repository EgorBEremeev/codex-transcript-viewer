#!/usr/bin/env bash
set -euo pipefail

marketplace="codex-transcript"
plugin="codex-transcript@$marketplace"
package="git+https://github.com/sadanand1120/codex-transcript-viewer.git@main"
tool_args=(--force --refresh)

if [[ "${1:-}" == "--editable" ]]; then
  package="${2:?editable install requires a checkout path}"
  tool_args+=(--editable)
elif (( $# )); then
  echo "usage: install.sh [--editable CHECKOUT]" >&2
  exit 2
fi

uv tool install "${tool_args[@]}" "$package"
codex-transcript --json doctor

if codex plugin list | awk -v plugin="$plugin" '$1 == plugin && $2 == "installed," {found = 1} END {exit !found}'; then
  codex plugin remove "$plugin"
fi
if codex plugin marketplace list | awk 'NR > 1 {print $1}' | grep -qx "$marketplace"; then
  codex plugin marketplace remove "$marketplace"
fi

codex plugin marketplace add sadanand1120/codex-transcript-viewer --ref main
codex plugin add "$plugin"

codex plugin marketplace list | awk -v marketplace="$marketplace" '
  NR > 1 && $1 == marketplace {print; found = 1}
  END {exit !found}
'
codex plugin list | awk -v plugin="$plugin" '
  $1 == plugin {print; found = 1}
  END {exit !found}
'
