#!/usr/bin/env bash
# Collect this device's usage and push it. Safe to run from cron.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

DEVICE="$(python3 claude_usage.py config device)"

python3 claude_usage.py collect

# Nothing changed means nothing to push; exit quietly so the cron log stays clean.
if git diff --quiet -- data && git diff --cached --quiet -- data \
   && [ -z "$(git ls-files --others --exclude-standard -- data)" ]; then
  exit 0
fi

git add data
git commit -q -m "data: $DEVICE usage through $(date +%F)"

# Each device writes only data/<device>/, so the only contention is the branch
# ref itself. Rebase and retry covers two devices pushing in the same minute.
for n in 1 2 3; do
  if git pull -q --rebase --autostash && git push -q; then
    exit 0
  fi
  sleep $((n * 5))
done

echo "claude-usage: push failed after 3 tries on $DEVICE" >&2
exit 1
