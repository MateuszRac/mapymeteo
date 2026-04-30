#!/bin/bash
# serve.sh — uruchamia lokalny serwer HTTP dla strony radarowej.
#
# Użycie:
#   ./sh/serve.sh            # port 8080
#   ./sh/serve.sh 9000       # port 9000

PORT="${1:-8080}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Znajdź Python z h5py lub dowolny dostępny
_find_python() {
    for candidate in \
        "$HOME/miniconda3/envs/gfs/python.exe" \
        "$HOME/miniforge3/envs/gfs/python.exe" \
        "$HOME/miniconda3/envs/gfs/bin/python" \
        "$HOME/miniforge3/envs/gfs/bin/python"
    do
        [[ -x "$candidate" ]] && echo "$candidate" && return
    done
    command -v python3 2>/dev/null || command -v python
}

PYTHON="$(_find_python)"

echo "Serwer HTTP na http://localhost:${PORT}/web/index.html"
echo "Katalog: $PROJECT_DIR"
echo "Zatrzymaj przez Ctrl+C"
echo ""

cd "$PROJECT_DIR"
"$PYTHON" -m http.server "$PORT"
