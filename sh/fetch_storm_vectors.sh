#!/bin/bash
# fetch_storm_vectors.sh — pobiera wektory ruchu burz GFS i zapisuje NPZ
# do data/storm_vectors/. Uruchamiać co 6 godzin, np.:
#
#   30 3,9,15,21 * * * /full/path/to/mapymeteo/sh/fetch_storm_vectors.sh
#
# Użycie ręczne:
#   ./sh/fetch_storm_vectors.sh
#   ./sh/fetch_storm_vectors.sh --log-level DEBUG
#   ./sh/fetch_storm_vectors.sh --n-files 49

set -euo pipefail

EXTRA_ARGS=()
while [[ $# -gt 0 ]]; do
    EXTRA_ARGS+=("$1")
    shift
done

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

ENV_FILE=""
_dir="$PROJECT_DIR"
while [[ "$_dir" != "/" && "$_dir" != "$HOME" ]]; do
    if [[ -f "$_dir/.env" ]]; then
        ENV_FILE="$_dir/.env"
        break
    fi
    _dir="$(dirname "$_dir")"
done

if [[ -z "$ENV_FILE" ]]; then
    echo "[błąd] Nie znaleziono pliku .env" >&2
    exit 1
fi

set -a; source "$ENV_FILE"; set +a

PROJECT_PATH="${PROJECT_PATH:-$PROJECT_DIR}"
PROJECT_PATH="${PROJECT_PATH//\'/}"
PROJECT_PATH="${PROJECT_PATH//\"/}"

LOG_DIR="$PROJECT_PATH/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/fetch_storm_vectors.log"

exec > >(tee -a "$LOG_FILE") 2>&1

echo "────────────────────────────────────────────────"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] fetch_storm_vectors start"
echo "[info] PROJECT_PATH=$PROJECT_PATH"
echo "[info] .env=$ENV_FILE"

_find_python() {
    if [[ -n "${PYTHON_PATH:-}" ]]; then
        echo "$PYTHON_PATH"; return
    fi
    for candidate in \
        "$HOME/miniconda3/envs/gfs/python.exe" \
        "$HOME/miniforge3/envs/gfs/python.exe" \
        "$HOME/anaconda3/envs/gfs/python.exe" \
        "$HOME/miniconda3/envs/gfs/bin/python" \
        "$HOME/miniforge3/envs/gfs/bin/python" \
        "$PROJECT_PATH/.venv/Scripts/python.exe" \
        "$PROJECT_PATH/.venv/bin/python"
    do
        [[ -x "$candidate" ]] && echo "$candidate" && return
    done
    command -v python3 2>/dev/null || command -v python
}

PYTHON="$(_find_python)"
echo "[info] Python: $PYTHON"

# On Windows, conda envs need Library/bin on PATH for ecCodes DLL.
# Without `conda activate`, this directory is missing, so cfgrib can't load.
CONDA_ENV_DIR="$(dirname "$PYTHON")"
if [[ -d "$CONDA_ENV_DIR/Library/bin" ]]; then
    export PATH="$CONDA_ENV_DIR/Library/bin:$PATH"
    echo "[info] Dodano do PATH: $CONDA_ENV_DIR/Library/bin"
fi

"$PYTHON" "$PROJECT_PATH/src/gfs/fetch_storm_vectors.py" "${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] fetch_storm_vectors koniec"
