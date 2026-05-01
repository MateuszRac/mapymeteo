#!/usr/bin/env bash
# Generuje config/palettes.json na podstawie palet kolorów z src/polrad.py
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON=".venv/Scripts/python.exe"
if [ ! -f "$PYTHON" ]; then
    PYTHON="python3"
fi
"$PYTHON" sh/export_palettes.py
