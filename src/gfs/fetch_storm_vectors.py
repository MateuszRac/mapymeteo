"""
Downloads the latest GFS storm motion vectors (USTM, VSTM at 0-6000 m AGL)
and saves them as data/storm_vectors/storm_vectors_<YYYYMMDDHHZ>.npz.

Run via sh/fetch_storm_vectors.sh or directly:
    python src/gfs/fetch_storm_vectors.py [--n-files 25] [--keep 3]

Cron example (every 6 h, shortly after each GFS run):
    30 3,9,15,21 * * * /path/to/mapymeteo/sh/fetch_storm_vectors.sh
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

from log_setup import setup_logging
from gfs.fetcher import GfsFetcher
from gfs.reader import GribReader

from dotenv import load_dotenv
load_dotenv(ROOT / ".env", override=True)

PROJECT_PATH = Path(os.getenv("PROJECT_PATH", str(ROOT)))
SV_DIR       = PROJECT_PATH / "data" / "storm_vectors"
LOG_DIR      = PROJECT_PATH / "logs"

# Minimal NOMADS filter — only USTM/VSTM at 0-6000 m AGL, small area
_SV_PARAMS = {
    "var_USTM": "on",
    "var_VSTM": "on",
    "lev_6000-0_m_above_ground": "on",
    "subregion": "",
    "toplat":    62,
    "bottomlat": 38,
    "leftlon":   8,
    "rightlon":  32,
}

log = logging.getLogger("fetch_storm_vectors")


def _extract_grib(fp: Path) -> tuple | None:
    """Return (lats, lons, valid_ts, ustm_2d, vstm_2d) or None on failure."""
    try:
        reader = GribReader(fp)
        ustm = reader.get_parameter("ustm", "heightAboveGroundLayer", 6000, step_type="instant")
        vstm = reader.get_parameter("vstm", "heightAboveGroundLayer", 6000, step_type="instant")
    except Exception as exc:
        log.warning("Pominięto %s: %s", fp.name, exc)
        return None

    lats = ustm.coords["latitude"].values.astype(np.float32)
    lons = ustm.coords["longitude"].values.astype(np.float32)
    vt   = pd.Timestamp(ustm.coords["valid_time"].values).timestamp()
    return lats, lons, vt, ustm.values.astype(np.float32), vstm.values.astype(np.float32)


def fetch_and_save(n_files: int = 25, keep: int = 3) -> Path | None:
    SV_DIR.mkdir(parents=True, exist_ok=True)

    fetcher = GfsFetcher(SV_DIR / "_tmp")
    log.info("Szukam najnowszego przebiegu GFS na NOMADS…")

    try:
        run_df = fetcher.find_latest_run(min_files=n_files)
    except Exception as exc:
        log.error("Błąd listowania NOMADS: %s", exc)
        return None

    cycle_date = run_df.iloc[0]["cycle_date"]
    run_hour   = run_df.iloc[0]["run_hour"]
    init_dt    = datetime.strptime(f"{cycle_date}{run_hour}", "%Y%m%d%H")
    log.info("Przebieg GFS: %s %sZ (%d kroków)", cycle_date, run_hour, len(run_df))

    # Skip download if NPZ for this run already exists
    tag      = init_dt.strftime("%Y%m%d%H") + "z"
    out_path = SV_DIR / f"storm_vectors_{tag}.npz"
    if out_path.exists():
        log.info("NPZ dla tego przebiegu już istnieje: %s", out_path.name)
        _prune(keep)
        return out_path

    tmp_dir = fetcher.output_dir
    tmp_dir.mkdir(parents=True, exist_ok=True)
    downloaded: list[Path] = []

    for _, row in run_df.head(n_files).iterrows():
        url  = fetcher.build_url(row["cycle_date"], row["run_hour"], row["file_name"], params=_SV_PARAMS)
        dest = tmp_dir / row["file_name"]
        try:
            fetcher.download(url, filename=row["file_name"])
            downloaded.append(dest)
        except Exception as exc:
            log.warning("Błąd pobierania %s: %s", row["file_name"], exc)

    log.info("Pobrano %d plików GRIB", len(downloaded))

    valid_times: list[float] = []
    ustm_list:   list        = []
    vstm_list:   list        = []
    lats = lons = None

    for fp in sorted(downloaded):
        res = _extract_grib(fp)
        if res is None:
            continue
        lts, lns, vt, u, v = res
        if lats is None:
            lats, lons = lts, lns
        valid_times.append(vt)
        ustm_list.append(u)
        vstm_list.append(v)

    # Clean up temp GRIBs regardless of outcome
    for fp in downloaded:
        fp.unlink(missing_ok=True)
    try:
        tmp_dir.rmdir()
    except OSError:
        pass

    if not valid_times:
        log.error("Brak danych USTM/VSTM w pobranych plikach")
        return None

    order = np.argsort(valid_times)
    np.savez_compressed(
        out_path,
        lats        = lats,
        lons        = lons,
        valid_times = np.array(valid_times, dtype=np.float64)[order],
        ustm        = np.array(ustm_list,   dtype=np.float32)[order],
        vstm        = np.array(vstm_list,   dtype=np.float32)[order],
        init_time   = np.array([init_dt.timestamp()], dtype=np.float64),
    )
    log.info("Zapisano %s (%d kroków czasowych)", out_path.name, len(valid_times))

    _prune(keep)
    return out_path


def _prune(keep: int) -> None:
    files = sorted(SV_DIR.glob("storm_vectors_*.npz"))
    for old in files[:-keep]:
        old.unlink()
        log.info("Usunięto stary plik: %s", old.name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Pobierz wektory ruchu burz GFS")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--n-files",   type=int, default=25, help="Liczba kroków prognozy do pobrania")
    parser.add_argument("--keep",      type=int, default=3,  help="Liczba zachowywanych plików NPZ")
    args = parser.parse_args()

    setup_logging(LOG_DIR, level=args.log_level.upper())
    result = fetch_and_save(n_files=args.n_files, keep=args.keep)
    sys.exit(0 if result else 1)
