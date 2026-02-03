#!/usr/bin/env bash
set -euo pipefail

# Minimal "kick" script to re-enter code-change mode.
# Prints repo state + a reminder.

repo="${1:-.}"
cd "$repo"

echo "=== resume_work: $(date) ==="
echo "PWD: $(pwd)"

git status -sb || true

echo "---"
echo "HEAD: $(git rev-parse --short HEAD)"
echo "Last commit: $(git log -1 --oneline)"

echo "---"
echo "Working tree diff (stat):"
git diff --stat || true

echo "---"
echo "Reminder: produce a diff/commit in the next 10 minutes or declare blocker."
