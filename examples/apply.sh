#!/usr/bin/env bash
# Apply a flat list of CLI commands from a file through the real CLI.
#   sudo examples/apply.sh examples/lab-access.cfg
set -euo pipefail

file="${1:?usage: apply.sh <config-file>}"
[[ -r "$file" ]] || { echo "cannot read $file" >&2; exit 1; }
command -v makemeswitch >/dev/null || { echo "makemeswitch not installed" >&2; exit 1; }

args=()
while IFS= read -r line; do
  line="${line%%#*}"
  [[ -z "${line//[[:space:]]/}" ]] && continue
  args+=(-c "$line")
done < "$file"

exec makemeswitch "${args[@]}"
