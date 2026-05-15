#!/bin/bash
# gen_forecast_pysteps.sh — prognoza CMAX z użyciem PySTEPS:
#   1. Pobiera/odświeża wektory GFS (storm_env NPZ)
#   2. Uruchamia cmax_forecast_pysteps.py (LK OF + GFS regresja + S-PROG)
#
# Użycie:
#   ./sh/gen_forecast_pysteps.sh
#   ./sh/gen_forecast_pysteps.sh --method extrap
#   ./sh/gen_forecast_pysteps.sh --dry-run
#   ./sh/gen_forecast_pysteps.sh --log-level DEBUG

set -uo pipefail

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
LOG_FILE="$LOG_DIR/gen_forecast_pysteps.log"

exec > >(tee -a "$LOG_FILE") 2>&1

echo "════════════════════════════════════════════════"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] gen_forecast_pysteps start"
echo "[info] PROJECT_PATH=$PROJECT_PATH"

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

CONDA_ENV_DIR="$(dirname "$PYTHON")"
if [[ -d "$CONDA_ENV_DIR/Library/bin" ]]; then
    export PATH="$CONDA_ENV_DIR/Library/bin:$PATH"
fi

# ── Sprawdź czy PySTEPS jest dostępny ────────────────────────────────────────
if ! "$PYTHON" -c "import pysteps" 2>/dev/null; then
    echo "[błąd] PySTEPS nie jest zainstalowany w środowisku Python: $PYTHON"
    echo "[info]  Zainstaluj: pip install pysteps"
    exit 1
fi

PYSTEPS_VER=$("$PYTHON" -c "import pysteps; print(pysteps.__version__)" 2>/dev/null)
echo "[info] PySTEPS: $PYSTEPS_VER"

# ── Krok 1: wektory GFS ──────────────────────────────────────────────────────
echo ""
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Krok 1: pobieranie wektorów GFS..."
"$PYTHON" "$PROJECT_PATH/src/gfs/fetch_storm_environment.py" \
    && echo "[ok] fetch_storm_environment zakończony" \
    || echo "[ostrzeżenie] fetch_storm_environment błąd — kontynuuję"

# ── Krok 2: prognoza PySTEPS ─────────────────────────────────────────────────
echo ""
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Krok 2: prognoza PySTEPS..."
"$PYTHON" "$PROJECT_PATH/src/cmax_forecast_pysteps.py" "${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}"

echo ""
echo "[$(date '+%Y-%m-%d %H:%M:%S')] gen_forecast_pysteps koniec"
echo "════════════════════════════════════════════════"
