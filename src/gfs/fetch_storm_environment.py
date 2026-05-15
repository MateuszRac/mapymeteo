"""
Downloads the latest GFS run and extracts convective environment fields:
  - USTM, VSTM at 0-6000 m AGL   (storm motion vectors)
  - CAPE at surface               (J/kg)
  - Shear 0-6 km                  (m/s, interpolated like convection.py _wmaxshear)
  - WmaxShear                     (m²/s², = sqrt(2*CAPE) * shear)

Saves data/storm_vectors/storm_env_<YYYYMMDDHHZ>.npz.

Run via sh/fetch_storm_environment.sh or directly:
    python src/gfs/fetch_storm_environment.py [--n-files 25] [--keep 3]

Cron example (every 6 h, shortly after each GFS run):
    30 3,9,15,21 * * * /path/to/mapymeteo/sh/fetch_storm_environment.sh
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
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

# NOMADS filter: storm motion + shear ingredients + CAPE
_ENV_PARAMS = {
    "var_CAPE": "on",
    "var_UGRD": "on",
    "var_VGRD": "on",
    "var_HGT":  "on",
    "var_USTM": "on",
    "var_VSTM": "on",
    "lev_surface":               "on",
    "lev_800_mb":                "on",
    "lev_700_mb":                "on",
    "lev_600_mb":                "on",
    "lev_500_mb":                "on",
    "lev_450_mb":                "on",
    "lev_10_m_above_ground":     "on",
    "lev_6000-0_m_above_ground": "on",
    "subregion": "",
    "toplat":    62,
    "bottomlat": 38,
    "leftlon":   8,
    "rightlon":  32,
}

log = logging.getLogger("fetch_storm_environment")


def _extract_grib(fp: Path, orog_cache: list) -> tuple | None:
    """Extract env fields from one GRIB file.

    orog_cache is a 1-element list used to carry the orography between calls
    (orography is static — only present in f000, reused for all steps).

    Returns (lats, lons, valid_ts, ustm, vstm, cape, shear, wmaxshear) or None.
    """
    try:
        reader = GribReader(fp)

        ustm = reader.get_parameter("ustm", "heightAboveGroundLayer", 6000, step_type="instant")
        vstm = reader.get_parameter("vstm", "heightAboveGroundLayer", 6000, step_type="instant")
        cape = reader.get_parameter("cape", "surface", 0)

        p800_u   = reader.get_parameter("u",  "isobaricInhPa", 800)
        p800_v   = reader.get_parameter("v",  "isobaricInhPa", 800)
        p700_u   = reader.get_parameter("u",  "isobaricInhPa", 700)
        p700_v   = reader.get_parameter("v",  "isobaricInhPa", 700)
        p600_u   = reader.get_parameter("u",  "isobaricInhPa", 600)
        p600_v   = reader.get_parameter("v",  "isobaricInhPa", 600)
        p500_u   = reader.get_parameter("u",  "isobaricInhPa", 500)
        p500_v   = reader.get_parameter("v",  "isobaricInhPa", 500)
        p500_hgt = reader.get_parameter("gh", "isobaricInhPa", 500)
        p450_u   = reader.get_parameter("u",  "isobaricInhPa", 450)
        p450_v   = reader.get_parameter("v",  "isobaricInhPa", 450)
        p450_hgt = reader.get_parameter("gh", "isobaricInhPa", 450)

        u10 = reader.get_parameter("u10", "heightAboveGround", 10)
        v10 = reader.get_parameter("v10", "heightAboveGround", 10)

    except Exception as exc:
        log.warning("Pominięto %s: %s", fp.name, exc)
        return None

    # Orography is static — read once from the first file that has it
    if not orog_cache:
        try:
            orog_da = reader.get_parameter("orog", "surface", 0)
            orog_cache.append(orog_da.values.astype(np.float32))
            log.debug("Orografia wczytana z %s", fp.name)
        except Exception:
            orog_cache.append(None)   # mark as attempted

    sfc_hgt = (orog_cache[0] if orog_cache[0] is not None
               else np.zeros(cape.values.shape, dtype=np.float32))

    # Shear 0-6 km — identical formula to convection.py _wmaxshear
    h6km  = sfc_hgt + 10 + 6000
    denom = p450_hgt.values - p500_hgt.values
    denom = np.where(np.abs(denom) < 1e-3, 1e-3, denom)
    frac  = (h6km - p500_hgt.values) / denom
    u6km  = p500_u.values + frac * (p450_u.values - p500_u.values)
    v6km  = p500_v.values + frac * (p450_v.values - p500_v.values)
    shear = np.sqrt((u6km - u10.values) ** 2 + (v6km - v10.values) ** 2)

    cape_v = np.maximum(cape.values, 0).astype(np.float32)
    wms    = (np.sqrt(2.0 * cape_v) * shear).astype(np.float32)

    lats = ustm.coords["latitude"].values.astype(np.float32)
    lons = ustm.coords["longitude"].values.astype(np.float32)
    vt   = float(pd.Timestamp(ustm.coords["valid_time"].values).value) / 1e9

    return (lats, lons, vt,
            ustm.values.astype(np.float32),
            vstm.values.astype(np.float32),
            cape_v,
            shear.astype(np.float32),
            wms,
            u10.values.astype(np.float32),    v10.values.astype(np.float32),
            p800_u.values.astype(np.float32),  p800_v.values.astype(np.float32),
            p700_u.values.astype(np.float32),  p700_v.values.astype(np.float32),
            p600_u.values.astype(np.float32),  p600_v.values.astype(np.float32),
            p500_u.values.astype(np.float32),  p500_v.values.astype(np.float32),
            p450_u.values.astype(np.float32),  p450_v.values.astype(np.float32))


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
    init_dt    = datetime.strptime(f"{cycle_date}{run_hour}", "%Y%m%d%H").replace(tzinfo=timezone.utc)
    log.info("Przebieg GFS: %s %sZ (%d kroków)", cycle_date, run_hour, len(run_df))

    tag      = init_dt.strftime("%Y%m%d%H") + "z"
    out_path = SV_DIR / f"storm_env_{tag}.npz"
    if out_path.exists():
        log.info("NPZ dla tego przebiegu już istnieje: %s", out_path.name)
        _prune(keep)
        return out_path

    tmp_dir = fetcher.output_dir
    tmp_dir.mkdir(parents=True, exist_ok=True)
    downloaded: list[Path] = []

    for _, row in run_df.head(n_files).iterrows():
        url  = fetcher.build_url(row["cycle_date"], row["run_hour"], row["file_name"], params=_ENV_PARAMS)
        dest = tmp_dir / row["file_name"]
        try:
            fetcher.download(url, filename=row["file_name"])
            downloaded.append(dest)
        except Exception as exc:
            log.warning("Błąd pobierania %s: %s", row["file_name"], exc)

    log.info("Pobrano %d plików GRIB", len(downloaded))

    orog_cache: list  = []
    valid_times: list = []
    ustm_list:   list = []
    vstm_list:   list = []
    cape_list:   list = []
    shear_list:  list = []
    wms_list:    list = []
    u10m_list:   list = []
    v10m_list:   list = []
    u800_list:   list = []
    v800_list:   list = []
    u700_list:   list = []
    v700_list:   list = []
    u600_list:   list = []
    v600_list:   list = []
    u500_list:   list = []
    v500_list:   list = []
    u450_list:   list = []
    v450_list:   list = []
    lats = lons = None

    for fp in sorted(downloaded):
        res = _extract_grib(fp, orog_cache)
        if res is None:
            continue
        (lts, lns, vt, ustm, vstm, cape, shear, wms,
         u10m, v10m, u800, v800, u700, v700, u600, v600, u500, v500, u450, v450) = res
        if lats is None:
            lats, lons = lts, lns
        valid_times.append(vt)
        ustm_list.append(ustm)
        vstm_list.append(vstm)
        cape_list.append(cape)
        shear_list.append(shear)
        wms_list.append(wms)
        u10m_list.append(u10m);  v10m_list.append(v10m)
        u800_list.append(u800);  v800_list.append(v800)
        u700_list.append(u700);  v700_list.append(v700)
        u600_list.append(u600);  v600_list.append(v600)
        u500_list.append(u500);  v500_list.append(v500)
        u450_list.append(u450);  v450_list.append(v450)

    for fp in downloaded:
        fp.unlink(missing_ok=True)
    try:
        tmp_dir.rmdir()
    except OSError:
        pass

    if not valid_times:
        log.error("Brak danych środowiskowych w pobranych plikach")
        return None

    order = np.argsort(valid_times)
    np.savez_compressed(
        out_path,
        lats        = lats,
        lons        = lons,
        valid_times = np.array(valid_times, dtype=np.float64)[order],
        ustm        = np.array(ustm_list,  dtype=np.float32)[order],
        vstm        = np.array(vstm_list,  dtype=np.float32)[order],
        cape        = np.array(cape_list,  dtype=np.float32)[order],
        shear       = np.array(shear_list, dtype=np.float32)[order],
        wmaxshear   = np.array(wms_list,   dtype=np.float32)[order],
        init_time   = np.array([init_dt.timestamp()], dtype=np.float64),
        u_10m       = np.array(u10m_list,  dtype=np.float32)[order],
        v_10m       = np.array(v10m_list,  dtype=np.float32)[order],
        u_800       = np.array(u800_list,  dtype=np.float32)[order],
        v_800       = np.array(v800_list,  dtype=np.float32)[order],
        u_700       = np.array(u700_list,  dtype=np.float32)[order],
        v_700       = np.array(v700_list,  dtype=np.float32)[order],
        u_600       = np.array(u600_list,  dtype=np.float32)[order],
        v_600       = np.array(v600_list,  dtype=np.float32)[order],
        u_500       = np.array(u500_list,  dtype=np.float32)[order],
        v_500       = np.array(v500_list,  dtype=np.float32)[order],
        u_450       = np.array(u450_list,  dtype=np.float32)[order],
        v_450       = np.array(v450_list,  dtype=np.float32)[order],
    )
    log.info("Zapisano %s (%d kroków, orografia=%s)",
             out_path.name, len(valid_times),
             "tak" if orog_cache and orog_cache[0] is not None else "nie (zerowa)")

    _prune(keep)
    return out_path


def _prune(keep: int) -> None:
    for pattern in ("storm_env_*.npz", "storm_vectors_*.npz"):
        files = sorted(SV_DIR.glob(pattern))
        for old in files[:-keep]:
            old.unlink()
            log.info("Usunięto stary plik: %s", old.name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Pobierz środowisko konwekcyjne GFS")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--n-files",   type=int, default=25, help="Liczba kroków prognozy do pobrania")
    parser.add_argument("--keep",      type=int, default=3,  help="Liczba zachowywanych plików NPZ")
    args = parser.parse_args()

    setup_logging(LOG_DIR, level=args.log_level.upper())
    result = fetch_and_save(n_files=args.n_files, keep=args.keep)
    sys.exit(0 if result else 1)
