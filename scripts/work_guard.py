#!/usr/bin/env python3
"""Work guard: enforce "plan -> code-change" and prevent silent drift.

Usage:
  python3 scripts/work_guard.py --repo . --interval 600 --mode once
  python3 scripts/work_guard.py --repo . --interval 600 --mode watch

Behavior:
- Records baseline HEAD + timestamp.
- If no new commit AND no working tree diff for >= interval:
  - prints a WARNING and exits non-zero (once)
  - or prints WARNING periodically (watch)

This is intentionally local-only (no messaging side-effects).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time


def sh(cmd: list[str], cwd: str) -> str:
    return subprocess.check_output(cmd, cwd=cwd, text=True).strip()


def get_head(cwd: str) -> str:
    return sh(["git", "rev-parse", "HEAD"], cwd)


def has_diff(cwd: str) -> bool:
    out = subprocess.check_output(["git", "diff", "--stat"], cwd=cwd, text=True)
    return bool(out.strip())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=".")
    ap.add_argument("--interval", type=int, default=600)
    ap.add_argument("--mode", choices=["once", "watch"], default="once")
    args = ap.parse_args()

    repo = args.repo
    baseline_head = get_head(repo)
    baseline_ts = time.time()

    def check() -> int:
        cur_head = get_head(repo)
        diff = has_diff(repo)
        idle = (cur_head == baseline_head) and (not diff)
        age = int(time.time() - baseline_ts)
        if idle and age >= args.interval:
            print(
                f"WORK_GUARD: no code changes for {age}s (HEAD unchanged, no diff). "
                "Enter code-change mode or declare blocker.",
                file=sys.stderr,
            )
            return 2
        return 0

    if args.mode == "once":
        return check()

    # watch
    while True:
        rc = check()
        if rc:
            # keep printing periodically
            pass
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
