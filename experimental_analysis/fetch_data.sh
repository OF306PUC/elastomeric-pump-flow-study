#!/bin/bash
# Bring every run from the acquisition hosts into Temp/, filed by campaign and
# condition as each file's header declares. Details: fetch_data.py.
#
#   ./fetch_data.sh                 all hosts (pi1, pi8, pi9, pi10 + ambient from pi12)
#   ./fetch_data.sh --dry-run       report where each file would go, change nothing
#   ./fetch_data.sh --hosts pi1     one host
set -euo pipefail
exec python3 "$(dirname "$0")/fetch_data.py" "$@"
