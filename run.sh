#!/usr/bin/env bash
# Skyrim Alchemy Companion - Linux/macOS launcher.
# Starts the companion from this folder's own directory.
set -euo pipefail

cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
    echo "Could not find python3." >&2
    echo "Install Python 3 (3.8 or newer) with your package manager, e.g.:" >&2
    echo "  Ubuntu/Debian:  sudo apt install python3" >&2
    echo "  Fedora:         sudo dnf install python3" >&2
    echo "  macOS:          brew install python3" >&2
    exit 1
fi

exec python3 skyrim_alchemy.py "$@"
