#!/usr/bin/env bash
set -euo pipefail

# Deterministic patch routine for ha-clawdbot (integration + panel JS)
# - Lints/compiles
# - Commits with provided message
# - Pushes to origin
# - (Optional) deploys into local HA docker container if present

usage() {
  cat <<'EOF'
Usage:
  scripts/release_ha_patch.sh -m "commit message" [--deploy]

Options:
  -m, --message   Commit message (required)
  --deploy        Also docker-cp files into ha-clawdbot container + restart (requires sudo + docker)
EOF
}

MSG=""
DEPLOY="0"

while [[ $# -gt 0 ]]; do
  case "$1" in
    -m|--message)
      MSG="${2:-}"; shift 2;;
    --deploy)
      DEPLOY="1"; shift;;
    -h|--help)
      usage; exit 0;;
    *)
      echo "Unknown arg: $1" >&2
      usage
      exit 2
      ;;
  esac
done

if [[ -z "$MSG" ]]; then
  echo "Missing -m/--message" >&2
  usage
  exit 2
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "==> Preflight: python compile"
python3 -m py_compile custom_components/clawdbot/__init__.py

echo "==> Preflight: node syntax check"
node -c ha-config/www/clawdbot-panel.js

echo "==> Git: status"
git status --porcelain

if [[ -z "$(git status --porcelain)" ]]; then
  echo "No changes to commit." >&2
  exit 1
fi

echo "==> Git: commit"
git add -A

git commit -m "$MSG"

echo "==> Git: push"
git push origin HEAD

if [[ "$DEPLOY" == "1" ]]; then
  echo "==> Deploy: copy files into ha-clawdbot container + restart"
  sudo docker cp custom_components/clawdbot/__init__.py ha-clawdbot:/config/custom_components/clawdbot/__init__.py
  sudo docker cp custom_components/clawdbot/services.yaml ha-clawdbot:/config/custom_components/clawdbot/services.yaml
  sudo docker cp ha-config/www/clawdbot-panel.js ha-clawdbot:/config/www/clawdbot-panel.js
  sudo docker restart ha-clawdbot
  echo "==> Deploy done."
fi

echo "==> Done."
