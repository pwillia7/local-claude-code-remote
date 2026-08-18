#!/usr/bin/env bash
# Installed by `agent2telegram remote-control install` — edit the installer, not this copy.
set -euo pipefail

SID="${1:-}"

if [[ -z "$SID" ]]; then
    echo "ERROR: missing Claude session id"
    exit 1
fi

exec {{PYTHON}} -m agent2telegram remote-control toggle "$SID" \
    --label {{LABEL_SHELL}}{{CONFIG_ARG}}
