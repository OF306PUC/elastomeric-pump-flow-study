#!/bin/bash
# Launch one acquisition run of this study on an acquisition host.
#
# The logger is the driver submodule (driver/, pinned to a commit). This wrapper
# only points it at the study's own manifests; everything else — the environment
# file, data/ and logs/ — lives in the directory this is run from (the study root).
#
#   cd elastomeric-pump-flow-study && ./run.sh
#
# First time on a host:  git clone --recurse-submodules <study repo>
# Keep it current:       git config submodule.recurse true   (then plain git pull)
set -euo pipefail

STUDY_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ ! -x "$STUDY_DIR/driver/run.sh" ]; then
    echo "ERROR: driver/ is empty — run: git submodule update --init" >&2
    exit 1
fi
cd "$STUDY_DIR"
# Relative to driver/, which is where the driver's run.sh resolves it from.
export MANIFESTS_DIR="${MANIFESTS_DIR:-../manifests}"
exec "$STUDY_DIR/driver/run.sh" "$@"
