#!/bin/bash
# watch_radar.sh — jednorazowe sprawdzenie IMGW, generowanie overlayów i sync FTP.
#
# Uruchamiany przez crona. Przykładowy wpis crontab (co 5 minut):
#   */5 * * * * /full/path/to/mapymeteo/sh/watch_radar.sh
#
# Aby edytować crontab:
#   crontab -e
#
# Użycie ręczne:
#   ./sh/watch_radar.sh
#   ./sh/watch_radar.sh --log-level DEBUG
#   ./sh/watch_radar.sh --radar pas --product 250.max

set -euo pipefail

# ── Argumenty ────────────────────────────────────────────────────────────────
EXTRA_ARGS=()
while [[ $# -gt 0 ]]; do
    EXTRA_ARGS+=("$1")
    shift
done

# ── Katalog projektu ──────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Szukamy .env w górę drzewa katalogów ─────────────────────────────────────
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

# ── Logi ─────────────────────────────────────────────────────────────────────
LOG_DIR="$PROJECT_PATH/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/watch_radar.log"

# Przekieruj stdout i stderr jednocześnie na konsolę i do pliku logu
exec > >(tee -a "$LOG_FILE") 2>&1

echo "────────────────────────────────────────────────"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] watch_radar start"
echo "[info] PROJECT_PATH=$PROJECT_PATH"
echo "[info] .env=$ENV_FILE"

# ── Wybór interpretera Pythona ────────────────────────────────────────────────
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

"$PYTHON" -c "import h5py, geopandas" 2>/dev/null \
    || echo "[ostrzeżenie] h5py/geopandas niedostępne — sprawdź środowisko Python"

# ── Uruchomienie ──────────────────────────────────────────────────────────────
"$PYTHON" "$PROJECT_PATH/src/fetch_new.py" "${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] watch_radar koniec"
