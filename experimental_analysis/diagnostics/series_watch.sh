#!/bin/bash
# Live check of a series diagnostic WHILE it runs: copy the two CSV files from
# their hosts and compare the last N minutes. Read-only on the hosts.
#
#   ./series_watch.sh <test_host> <ref_host> <CONDITION> [minutes]
#   ./series_watch.sh pi9 pi10 S1 30         # SEN4 on pi9 vs SEN3 on pi10, last 30 min
#
# By the manifest's convention the sensor under test is rep_1 and the reference
# rep_2 (manifests/sensor-series-diag.toml). Use it right after the purge of
# condition S3: a ratio that jumps back to ~1 points to a trapped bubble.
set -euo pipefail
if [ $# -lt 3 ]; then
    sed -n '2,10p' "$0"; exit 2
fi
TEST_HOST=$1; REF_HOST=$2; COND=$3; MIN=${4:-30}
HERE="$(cd "$(dirname "$0")" && pwd)"
LIVE="$HERE/../Temp/_series_live"          # git-ignored, overwritten each call
mkdir -p "$LIVE"
REMOTE=elastomeric-pump-flow-study/data
scp -q "$TEST_HOST:$REMOTE/${COND}_rep_1.csv" "$LIVE/${COND}_test_${TEST_HOST}.csv"
scp -q "$REF_HOST:$REMOTE/${COND}_rep_2.csv"  "$LIVE/${COND}_ref_${REF_HOST}.csv"
# The minute blocks are short here so a change shows up quickly.
python3 "$HERE/series_compare.py" \
    "$LIVE/${COND}_test_${TEST_HOST}.csv" "$LIVE/${COND}_ref_${REF_HOST}.csv" \
    --last "$MIN" --block-min 5
