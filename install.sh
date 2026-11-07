#!/usr/bin/env bash
# Write claude-usage.toml for this machine and put sync.sh on a daily cron.
# Usage: ./install.sh <device> [--tz ZONE] [--cron "MINUTE HOUR"]
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
REPO="$(pwd)"

usage() { echo 'usage: ./install.sh <device> [--tz ZONE] [--cron "MINUTE HOUR"]' >&2; exit 1; }
DEVICE="${1:-}"
ZONE="UTC"
CRON="17 12"
[ -n "$DEVICE" ] || usage
shift
while [ $# -gt 0 ]; do
  case "$1" in
    --tz) ZONE="${2:-}"; [ -n "$ZONE" ] || usage; shift 2 ;;
    --cron) CRON="${2:-}"; [ -n "$CRON" ] || usage; shift 2 ;;
    *) usage ;;
  esac
done

for dep in python3 npx git crontab; do
  command -v "$dep" >/dev/null || { echo "missing dependency: $dep" >&2; exit 1; }
done
# 3.11 ships tomllib and parses the trailing Z in transcript timestamps.
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' \
  || { echo "python3 3.11 or newer required, found $(python3 -V)" >&2; exit 1; }

cat > claude-usage.toml <<EOF
device = "$DEVICE"
timezone = "$ZONE"
cron = "$CRON"
# Optional keys and their defaults: stale_days = 5, drift_warn = 0.02,
# transcripts = "<CLAUDE_CONFIG_DIR or HOME/.claude>/projects", ccusage = "npx -y ccusage@20"
EOF
# Reading the file back validates it: a bad zone or cron value fails here, before cron is touched.
MINUTE_HOUR="$(python3 claude_usage.py config cron)"
echo "device $(python3 claude_usage.py config device), timezone $(python3 claude_usage.py config timezone)"

# cron runs with a minimal PATH, so the job carries the PATH that works right now.
JOB="$MINUTE_HOUR * * * cd '$REPO' && PATH='$PATH' ./sync.sh >> '$REPO/sync.log' 2>&1"
JOB="${JOB//%/\\%}"
# Rerunning replaces an earlier entry for this checkout and leaves other entries alone.
if OUT="$(crontab -l 2>&1)"; then
  CURRENT="$OUT"
elif printf '%s' "$OUT" | grep -qi 'no crontab'; then
  CURRENT=""
else
  echo "crontab -l failed: $OUT" >&2; exit 1
fi
CURRENT="$(printf '%s\n' "$CURRENT" | grep -vF "cd '$REPO' && " || true)"
printf '%s\n%s\n' "$CURRENT" "$JOB" | grep -v '^$' | crontab -
set -- $MINUTE_HOUR
printf 'cron entry installed: daily at %02d:%02d\n' "$((10#$2))" "$((10#$1))"

echo "first collection, reads every transcript on disk"
./sync.sh
echo "done. see the week with: $REPO/claude_usage.py report"
