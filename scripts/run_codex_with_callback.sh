#!/usr/bin/env bash
set -euo pipefail

# Run Codex and ALWAYS wake OpenClaw on completion (success or failure).
# Usage:
#   ./scripts/run_codex_with_callback.sh --full-auto "<prompt>"
#   ./scripts/run_codex_with_callback.sh "<prompt>"

mode="--full-auto"
if [[ "${1:-}" == "--full-auto" || "${1:-}" == "--yolo" || "${1:-}" == "--sandbox" ]]; then
  mode="$1"
  shift
fi

prompt="${1:-}"
if [[ -z "$prompt" ]]; then
  echo "Usage: $0 [--full-auto|--yolo] \"<prompt>\"" >&2
  exit 2
fi

rc=0
set +e
codex exec "$mode" "$prompt"
rc=$?
set -e

# Wake the gateway so Agent 42 follows through with commit/proof/verification.
# Best-effort: do not fail the script if wake fails.
msg="Codex finished (rc=$rc): ${prompt:0:140}"
openclaw gateway wake --text "$msg" --mode now >/dev/null 2>&1 || true

exit $rc
