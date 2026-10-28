#!/usr/bin/env bash
# Starts StitchPilot on macOS or Linux. The first run sets up its Python environment (a few minutes).
#   ./run.sh [picture]          (or: bash run.sh)
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ "$(uname)" == "Darwin" ]]; then
    venv="$HOME/Library/Application Support/StitchPilot/venv"
else
    venv="${XDG_DATA_HOME:-$HOME/.local/share}/StitchPilot/venv"
fi

if [[ ! -x "$venv/bin/python" ]]; then
    py=""
    for cand in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
        if command -v "$cand" >/dev/null 2>&1 &&
           "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
            py="$cand"
            break
        fi
    done
    if [[ -z "$py" ]]; then
        echo "StitchPilot needs Python 3.10 or newer: https://www.python.org/downloads/macos/" >&2
        echo "(or with Homebrew: brew install python)" >&2
        exit 1
    fi
    echo "Setting up StitchPilot for the first time with $($py --version)..."
    mkdir -p "$(dirname "$venv")"
    "$py" -m venv "$venv"
    "$venv/bin/python" -m pip install --upgrade pip
    "$venv/bin/python" -m pip install -r "$here/requirements.txt"
fi

[[ "${1:-}" == "--setup-only" ]] && exit 0  # used by build/build_mac.sh
exec "$venv/bin/python" "$here/app.py" "$@"
