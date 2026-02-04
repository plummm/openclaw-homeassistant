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
# Ensure openclaw is reachable even if ~/.npm-global/bin isn't on PATH in the Codex shell.
OPENCLAW_BIN="${OPENCLAW_BIN:-openclaw}"
if ! command -v "$OPENCLAW_BIN" >/dev/null 2>&1; then
  if [[ -x "$HOME/.npm-global/bin/openclaw" ]]; then
    OPENCLAW_BIN="$HOME/.npm-global/bin/openclaw"
  fi
fi
"$OPENCLAW_BIN" gateway wake --text "$msg" --mode now >/dev/null 2>&1 || true

exit $rc
