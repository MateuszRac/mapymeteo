"""
Pobiera dane wyładowań atmosferycznych z MTG-LI (EUMETSAT)
i zapisuje lightning.json dostępny dla frontendu.

Użycie:
  python fetch_lightning.py
  python fetch_lightning.py --log-level DEBUG
  python fetch_lightning.py --history-hours 2
"""

import argparse
import json
import logging
import math
import os
import re
import shutil
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import eumdac
import numpy as np
import pandas as pd
import xarray as xr
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=True)

try:
    from log_setup import setup_logging
    from transfer.ftp import FtpUploader
except ImportError:
    from src.log_setup import setup_logging
    from src.transfer.ftp import FtpUploader

PROJECT_PATH      = Path(os.getenv("PROJECT_PATH", str(ROOT)))
MTG_DATA_DIR      = PROJECT_PATH / "data" / "mtg"
OUTPUT_JSON       = PROJECT_PATH / "img" / "polrad" / "lightning.json"
LOG_DIR           = PROJECT_PATH / "logs"
WATCH_POINTS_FILE = ROOT / "config" / "watch_points.json"
ALERT_STATE_FILE  = PROJECT_PATH / "data" / "alert_state.json"
STORM_VECTORS_DIR = PROJECT_PATH / "data" / "storm_vectors"
_CMAX_CACHE_PATH  = PROJECT_PATH / "data" / "cmax" / "cmax_latest.npz"
_CMAX_MAX_AGE_S   = 20 * 60   # ignoruj cache starszy niż 20 min

# Optical flow
_FLOW_TARGET_KM   = 1.0    # docelowa rozdzielczość po próbkowaniu [km/px]
_FLOW_SIGMA_KM    = 30.0   # Gaussowski promień wypełnienia pola [km]
_FLOW_ECHO_THR    = 10.0   # min dBZ uznawany za echo [dBZ]
_FLOW_MAX_ECHO_KM = 50.0   # maks. odległość klastra od echa, powyżej której OF jest odrzucany [km]
ALERT_COOLDOWN_S  = 3600   # min. przerwa między alertami dla tej samej lokalizacji [s]

COLLECTION_ID   = "EO:EUM:DAT:0782"
LON_MIN, LON_MAX =  9.77, 27.03
LAT_MIN, LAT_MAX = 47.56, 57.48
SLOT_MINUTES    = 10
FTP_REMOTE_DIR  = os.getenv("FTP_REMOTE_IMG_DIR", "img")

# Przeliczniki stopień ↔ km (dla centrum obszaru ~52°N)
_CENTER_LAT   = 52.0
_KM_PER_LAT   = 111.32
_KM_PER_LON   = 111.32 * math.cos(math.radians(_CENTER_LAT))  # ≈ 68.5 km/°

# Stałe prognozy — wszystkie w km lub km/h
FORECAST_RECENT_MIN      = 20    # okno aktualnego obszaru [min]
FORECAST_MOTION_H        = 2     # okno estymacji prędkości [h]
FORECAST_AHEAD_H         = 1     # horyzont prognozy [h]
FORECAST_EPS_KM          = 25    # promień klastrowania DBSCAN [km]
FORECAST_SEARCH_KM       = 60    # promień szukania historii dla klastra [km]
FORECAST_BUFFER_KM       = 5     # bazowy bufor wokół bieżącego polygonu [km]
FORECAST_SPREAD_KM_PER_H = 1.0   # przyrost buforu końcowego z czasem [km/h]
FORECAST_SPREAD_FRACTION = 0.05  # przyrost buforu końcowego z odległością [udział dystansu]
FORECAST_MAX_KMH         = 100   # maks. prędkość komórki [km/h]
FORECAST_MIN_SAMPLES     = 2     # min punktów w klastrze DBSCAN
FORECAST_MIN_CLUSTER     = 5     # min punktów klastra do rysowania polygonu
FORECAST_INTENSE_DENS    = 10.0  # próg gęstości dla klastra intensywnego [/km²/10min]

# Progi wiarygodności historii centroidu (jeśli spełnione → historia > GFS)
HIST_MIN_BINS    = 3    # minimalna liczba binów w łańcuchu
HIST_MIN_SPAN_MIN = 15.0 # minimalna rozpiętość czasu [min]
HIST_MIN_R2      = 0.70  # minimalny R² ważony regresji liniowej

log = logging.getLogger("fetch_lightning")


# ── Slot helpers ──────────────────────────────────────────────────────────────

def _to_naive_utc(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _slot_key(dt: datetime) -> str:
    """Slot END time → 'YYYY-MM-DDTHH:MM:SS' (UTC, rounded to 10 min)."""
    dt = _to_naive_utc(dt).replace(second=0, microsecond=0)
    dt = dt.replace(minute=(dt.minute // SLOT_MINUTES) * SLOT_MINUTES)
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _needed_slots(now: datetime, history_hours: int) -> set:
    """Set of slot-end keys for the last `history_hours` hours."""
    now = _to_naive_utc(now).replace(second=0, microsecond=0)
    cutoff = now - timedelta(hours=history_hours)
    t = now.replace(minute=(now.minute // SLOT_MINUTES) * SLOT_MINUTES)
    slots = set()
    while t > cutoff:
        slots.add(t.strftime("%Y-%m-%dT%H:%M:%S"))
        t -= timedelta(minutes=SLOT_MINUTES)
    return slots


# ── EUMETSAT ──────────────────────────────────────────────────────────────────

def _get_collection():
    key    = os.getenv("MTG_CONSUMER_KEY", "")
    secret = os.getenv("MTG_CONSUMER_SECRET", "")
    if not key or not secret:
        raise ValueError("Brak MTG_CONSUMER_KEY / MTG_CONSUMER_SECRET w .env")
    token = eumdac.AccessToken((key, secret))
    return eumdac.DataStore(token).get_collection(COLLECTION_ID)


def _sensing_slot_key(product) -> str | None:
    """Returns slot key based on product.sensing_end, or None if unavailable."""
    try:
        se = _to_naive_utc(product.sensing_end).replace(second=0, microsecond=0)
        return se.strftime("%Y-%m-%dT%H:%M:%S")
    except Exception:
        return None


# ── NC parsing ────────────────────────────────────────────────────────────────

def _parse_nc(nc_path: Path) -> list:
    """Reads *0001.nc and returns list of group dicts filtered to the area."""
    ds = xr.open_dataset(str(nc_path))
    try:
        lat        = ds["latitude"].values
        lon        = ds["longitude"].values
        group_time = ds["group_time"].values
        n_events   = ds["number_of_events"].values
    finally:
        ds.close()

    mask = (
        (lat >= LAT_MIN) & (lat <= LAT_MAX) &
        (lon >= LON_MIN) & (lon <= LON_MAX)
    )

    groups = []
    for i in np.where(mask)[0]:
        try:
            t_str = pd.Timestamp(group_time[i]).strftime("%Y-%m-%dT%H:%M:%S")
        except Exception:
            continue
        groups.append({
            "lat": round(float(lat[i]), 4),
            "lon": round(float(lon[i]), 4),
            "n":   int(n_events[i]),
            "t":   t_str,
        })
    return groups


def _process_zip(zip_path: Path) -> list:
    """Extracts ZIP, parses *0001.nc, returns list of groups. Cleans up after."""
    extract_dir = zip_path.with_suffix("")
    try:
        extract_dir.mkdir(exist_ok=True)
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(extract_dir)

        nc_files = list(extract_dir.rglob("*0001.nc"))
        if not nc_files:
            raise FileNotFoundError("Brak pliku *0001.nc w ZIP")

        return _parse_nc(nc_files[0])
    finally:
        shutil.rmtree(extract_dir, ignore_errors=True)
        zip_path.unlink(missing_ok=True)


# ── Main ──────────────────────────────────────────────────────────────────────

def main(history_hours: int = 6) -> int:
    MTG_DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    needed = _needed_slots(now, history_hours)
    log.info("Sloty do pokrycia (%d godz.): %d", history_hours, len(needed))

    # Load existing JSON
    slots: dict = {}
    if OUTPUT_JSON.exists():
        try:
            existing = json.loads(OUTPUT_JSON.read_text(encoding="utf-8"))
            slots = existing.get("slots", {})
        except Exception as exc:
            log.warning("Nie można wczytać istniejącego lightning.json: %s", exc)

    # Prune slots outside window
    slots = {k: v for k, v in slots.items() if k in needed}

    # Find which slots still need downloading
    to_download = needed - {k for k, v in slots.items() if v.get("ok")}

    if not to_download:
        log.info("Wszystkie sloty już pobrane — tylko aktualizacja znacznika czasu.")
        _save(slots, now)
        return 0

    log.info("Brakujące sloty: %d", len(to_download))

    # Search EUMETSAT
    cutoff = now - timedelta(hours=history_hours)
    try:
        collection = _get_collection()
        products = list(collection.search(dtstart=cutoff, dtend=now))
        log.info("Znaleziono %d produktów na serwerze EUMETSAT", len(products))
    except Exception as exc:
        log.error("Błąd połączenia z EUMETSAT: %s", exc)
        _save(slots, now)
        return 1

    # Map slot key → product
    slot_to_product = {}
    for p in products:
        sk = _sensing_slot_key(p)
        if sk and sk in to_download:
            slot_to_product[sk] = p

    log.info("Dopasowano %d/%d slotów do produktów", len(slot_to_product), len(to_download))

    # Download and process each slot
    for sk in sorted(to_download):
        product = slot_to_product.get(sk)
        if product is None:
            log.warning("[%s] Brak produktu na serwerze", sk)
            slots[sk] = {"ok": False}
            continue

        zip_path = None
        try:
            with product.open() as src:
                zip_path = MTG_DATA_DIR / src.name
                with open(zip_path, "wb") as dst:
                    shutil.copyfileobj(src, dst)
            log.info("[%s] Pobrano ZIP: %s", sk, zip_path.name)
        except Exception as exc:
            log.error("[%s] Błąd pobierania: %s", sk, exc)
            if zip_path and zip_path.exists():
                zip_path.unlink(missing_ok=True)
            slots[sk] = {"ok": False}
            continue

        try:
            groups = _process_zip(zip_path)
            slots[sk] = {"ok": True, "groups": groups}
            log.info("[%s] Przetworzono: %d grup w obszarze", sk, len(groups))
        except Exception as exc:
            log.error("[%s] Błąd przetwarzania ZIP: %s", sk, exc)
            slots[sk] = {"ok": False}

    _save(slots, now)

    # FTP upload
    uploader = FtpUploader()
    if uploader.is_configured():
        try:
            remote_path = f"{FTP_REMOTE_DIR}/polrad/lightning.json"
            with uploader.session() as sess:
                sess.upload(OUTPUT_JSON, remote_path)
        except Exception as exc:
            log.warning("FTP upload lightning.json: %s", exc)

    # Cleanup old MTG files
    for f in MTG_DATA_DIR.iterdir():
        if f.suffix in (".zip", ".nc") and f.is_file():
            f.unlink(missing_ok=True)

    return 0


_SCALE = np.array([_KM_PER_LAT, _KM_PER_LON])   # mnożnik deg → km


def _compass(deg: float) -> str:
    """Kierunek ruchu w notacji różyczkowej (16 punktów)."""
    dirs = ["N","NNE","NE","ENE","E","ESE","SE","SSE",
            "S","SSW","SW","WSW","W","WNW","NW","NNW"]
    return dirs[round(deg / 22.5) % 16]


def _polygon_area_km2(hull_km: np.ndarray) -> float:
    """Pole polygonu ze wzoru Gaussa (shoelace), wynik w km²."""
    if len(hull_km) < 3:
        return 0.0
    y = hull_km[:, 0]   # lat_km
    x = hull_km[:, 1]   # lon_km
    i = np.arange(len(hull_km))
    j = (i + 1) % len(hull_km)
    return float(abs((x[i] * y[j] - x[j] * y[i]).sum()) / 2.0)


def _load_cmax() -> dict | None:
    """Ładuje stack klatek CMAX z NPZ-cache.

    Zwraca None gdy brak pliku lub najnowsza klatka jest za stara.
    """
    if not _CMAX_CACHE_PATH.exists():
        log.debug("CMAX cache: brak pliku %s", _CMAX_CACHE_PATH)
        return None
    try:
        import time
        c = np.load(_CMAX_CACHE_PATH)
        # Normalizuj klucz timestamps (stary format: 'timestamp' bez 's')
        if "timestamps" in c.files:
            ts_arr = c["timestamps"]
        elif "timestamp" in c.files:
            ts_arr = c["timestamp"]
        else:
            log.warning("CMAX cache: brak klucza timestamps/timestamp, pliki: %s", c.files)
            return None
        age_s = time.time() - float(ts_arr[-1])
        if age_s > _CMAX_MAX_AGE_S:
            log.debug("CMAX cache: za stary (%.0f s > %d s)", age_s, _CMAX_MAX_AGE_S)
            return None
        # Konwertuj do zwykłego dict i normalizuj klucz timestamps
        result = {k: c[k] for k in c.files}
        result["timestamps"] = ts_arr   # gwarantuje klucz 'timestamps' niezależnie od formatu
        n_frames = result["dbz"].shape[0] if result["dbz"].ndim == 3 else 1
        log.debug("CMAX cache: załadowano %d klatek, wiek %.0f s", n_frames, age_s)
        return result
    except Exception as exc:
        log.warning("CMAX cache: błąd ładowania: %s", exc)
        return None


def _pip_batch(lats: np.ndarray, lons: np.ndarray, poly: np.ndarray) -> np.ndarray:
    """Ray-casting PIP (wektorowo). poly: (N,2) [lat, lon], zwraca bool array."""
    inside = np.zeros(len(lats), dtype=bool)
    n = len(poly)
    j = n - 1
    for i in range(n):
        yi, xi = float(poly[i, 0]), float(poly[i, 1])
        yj, xj = float(poly[j, 0]), float(poly[j, 1])
        dy = yj - yi or 1e-15
        cond = ((yi > lats) != (yj > lats)) & (
            lons < xi + (xj - xi) * (lats - yi) / dy
        )
        inside ^= cond
        j = i
    return inside


def _max_dbz_in_polygon(cmax: dict, poly_deg: np.ndarray) -> float | None:
    """Zwraca max dBZ CMAX wewnątrz polygonu [lat, lon] lub None gdy brak danych."""
    lats_g = cmax["lats"]   # (H, W) float32 — środki pikseli
    lons_g = cmax["lons"]   # (H, W) float32
    dbz_raw = cmax["dbz"]
    # Stack (N,H,W) → bierzemy najnowszą klatkę; stary format to (H,W)
    dbz_g = dbz_raw[-1] if dbz_raw.ndim == 3 else dbz_raw

    lat_min = float(poly_deg[:, 0].min()) - 0.05
    lat_max = float(poly_deg[:, 0].max()) + 0.05
    lon_min = float(poly_deg[:, 1].min()) - 0.05
    lon_max = float(poly_deg[:, 1].max()) + 0.05

    bb = (
        (lats_g >= lat_min) & (lats_g <= lat_max) &
        (lons_g >= lon_min) & (lons_g <= lon_max)
    )
    rows, cols = np.where(bb)
    if rows.size == 0:
        return None

    flat_lats = lats_g[rows, cols]
    flat_lons = lons_g[rows, cols]
    flat_dbz  = dbz_g[rows, cols]

    inside = _pip_batch(flat_lats, flat_lons, poly_deg)
    vals = flat_dbz[inside]
    vals = vals[~np.isnan(vals)]
    if vals.size == 0:
        return None
    return round(float(vals.max()), 1)


def _fill_flow(field: np.ndarray, sigma_px: float) -> np.ndarray:
    """Wypełnia obszary bez echa (NaN) Gaussowskim ważonym uśrednieniem sąsiedztwa."""
    from scipy.ndimage import gaussian_filter
    valid = np.isfinite(field)
    f = np.where(valid, field, 0.0).astype(np.float32)
    w = valid.astype(np.float32)
    fs = gaussian_filter(f, sigma_px)
    ws = gaussian_filter(w, sigma_px)
    return np.where(ws > 0.01, fs / ws, 0.0).astype(np.float32)


def _radar_motion(
    cmax: dict | None, center_km: np.ndarray
) -> tuple[float, float, float] | None:
    """Estymuje ruch komórki burzowej metodą Farneback optical flow na danych CMAX.

    Wymaga co najmniej 2 klatek w stacku i pakietu cv2 (opencv-python).
    Zwraca (dlat, dlon, speed_kmh) lub None gdy dane niewystarczające.
    """
    if cmax is None:
        log.debug("OF: brak danych CMAX")
        return None
    try:
        import cv2
    except ImportError:
        log.debug("OF: cv2 niedostępne — pomijam radar optical flow")
        return None

    dbz_raw    = cmax.get("dbz")
    timestamps = cmax.get("timestamps")
    lats       = cmax.get("lats")
    lons       = cmax.get("lons")

    if dbz_raw is None or timestamps is None or lats is None or lons is None:
        log.warning("OF: brak kluczy w CMAX cache (dbz=%s, timestamps=%s, lats=%s, lons=%s)",
                    dbz_raw is not None, timestamps is not None,
                    lats is not None, lons is not None)
        return None
    dbz_stack = dbz_raw if dbz_raw.ndim == 3 else dbz_raw[np.newaxis]
    if len(dbz_stack) < 2:
        log.debug("OF: za mało klatek w stacku (%d < 2)", len(dbz_stack))
        return None

    # Próbkowanie do ~_FLOW_TARGET_KM rozdzielczości
    dpix_km = abs(float(lats[0, 0] - lats[1, 0])) * _KM_PER_LAT
    if dpix_km < 1e-6:
        log.warning("OF: nieprawidłowy dpix_km=%.6f (lats[0,0]=%.4f, lats[1,0]=%.4f)",
                    dpix_km, lats[0, 0], lats[1, 0])
        return None
    step    = max(1, round(_FLOW_TARGET_KM / dpix_km))

    lats_ds     = lats[::step, ::step]
    lons_ds     = lons[::step, ::step]
    dpix_lat_km = abs(float(lats_ds[0, 0] - lats_ds[1, 0])) * _KM_PER_LAT
    dpix_lon_km = abs(float(lons_ds[0, 1] - lons_ds[0, 0])) * _KM_PER_LON
    sigma_px    = _FLOW_SIGMA_KM / ((dpix_lat_km + dpix_lon_km) / 2.0)

    log.debug("OF: siatka %dx%d → %dx%d (step=%d, dpix=%.2f km, σ=%.0f px)",
              lats.shape[0], lats.shape[1], lats_ds.shape[0], lats_ds.shape[1],
              step, dpix_km, sigma_px)

    H, W = lats_ds.shape
    sum_u = np.zeros((H, W), dtype=np.float32)
    sum_v = np.zeros((H, W), dtype=np.float32)
    sum_w = np.zeros((H, W), dtype=np.float32)
    n_pairs = 0

    def _to_u8(arr: np.ndarray) -> np.ndarray:
        return np.clip(np.nan_to_num(arr, nan=0.0) / 65.0 * 255, 0, 255).astype(np.uint8)

    for i in range(len(dbz_stack) - 1):
        dt_s = float(timestamps[i + 1] - timestamps[i])
        if not (0 < dt_s <= 900):
            log.debug("OF: para %d-%d odrzucona (dt_s=%.0f)", i, i + 1, dt_s)
            continue

        f1 = _to_u8(dbz_stack[i    ][::step, ::step])
        f2 = _to_u8(dbz_stack[i + 1][::step, ::step])

        has_echo_f1 = np.nan_to_num(dbz_stack[i    ][::step, ::step], nan=0.0) >= _FLOW_ECHO_THR
        has_echo_f2 = np.nan_to_num(dbz_stack[i + 1][::step, ::step], nan=0.0) >= _FLOW_ECHO_THR
        has_echo    = has_echo_f1 | has_echo_f2
        echo_px     = int(has_echo.sum())
        log.debug("OF: para %d-%d: dt=%.0fs, echo_px=%d/%d",
                  i, i + 1, dt_s, echo_px, H * W)

        flow = cv2.calcOpticalFlowFarneback(
            f1, f2, None,
            pyr_scale=0.5, levels=5, winsize=25,
            iterations=3, poly_n=7, poly_sigma=1.5, flags=0,
        )
        # flow[...,0]=Δcol(wschód+), flow[...,1]=Δrow(południe+) → negujemy V
        dt_h   = dt_s / 3600.0
        u_kmh  =  flow[..., 0] * dpix_lon_km / dt_h
        v_kmh  = -flow[..., 1] * dpix_lat_km / dt_h

        w = has_echo.astype(np.float32) * float(i + 1)  # nowsze pary → wyższa waga
        sum_u  += u_kmh * w
        sum_v  += v_kmh * w
        sum_w  += w
        n_pairs += 1

    if n_pairs == 0:
        log.debug("OF: brak prawidłowych par klatek")
        return None

    raw_u = np.where(sum_w > 0, sum_u / sum_w, np.nan)
    raw_v = np.where(sum_w > 0, sum_v / sum_w, np.nan)

    valid_px = int(np.isfinite(raw_u).sum())
    log.debug("OF: ważone piksele z echem: %d/%d", valid_px, H * W)

    u_field = _fill_flow(raw_u, sigma_px)
    v_field = _fill_flow(raw_v, sigma_px)

    # Znajdź piksel siatki najbliższy centroidowi klastra
    center_lat = float(center_km[0]) / _KM_PER_LAT
    center_lon = float(center_km[1]) / _KM_PER_LON
    dist2_km2  = ((lats_ds - center_lat) * _KM_PER_LAT) ** 2 + \
                 ((lons_ds - center_lon) * _KM_PER_LON) ** 2
    i_row, i_col = np.unravel_index(int(np.argmin(dist2_km2)), dist2_km2.shape)
    nearest_km   = float(np.sqrt(dist2_km2[i_row, i_col]))

    if nearest_km > _FLOW_MAX_ECHO_KM:
        log.debug("OF: klaster poza siatką (%.1f km > %.1f km)", nearest_km, _FLOW_MAX_ECHO_KM)
        return None

    # Sprawdź czy klaster leży blisko rzeczywistego echa (nie tylko wypełnionego pola)
    echo_last  = np.nan_to_num(dbz_stack[-1][::step, ::step], nan=0.0)
    echo_dist2 = np.where(echo_last >= _FLOW_ECHO_THR, dist2_km2, np.inf)
    echo_nearest_km = float(np.sqrt(float(echo_dist2.min())))
    echo_thr_km     = _FLOW_SIGMA_KM * 2
    if echo_nearest_km > echo_thr_km:
        log.debug("OF: brak echa w pobliżu klastra (%.1f km > %.1f km)",
                  echo_nearest_km, echo_thr_km)
        return None

    u_kmh_c = float(u_field[i_row, i_col])
    v_kmh_c = float(v_field[i_row, i_col])
    speed_kmh = math.sqrt(u_kmh_c ** 2 + v_kmh_c ** 2)

    log.debug("OF: u=%.1f km/h, v=%.1f km/h → speed=%.1f km/h (klaster @ %.1f km od siatki, %.1f km od echa)",
              u_kmh_c, v_kmh_c, speed_kmh, nearest_km, echo_nearest_km)

    if speed_kmh < 1.0:
        log.debug("OF: prędkość %.1f km/h < 1 km/h (szum)", speed_kmh)
        return None
    if speed_kmh > FORECAST_MAX_KMH:
        scale      = FORECAST_MAX_KMH / speed_kmh
        u_kmh_c   *= scale
        v_kmh_c   *= scale
        speed_kmh  = FORECAST_MAX_KMH

    dlat = v_kmh_c * FORECAST_AHEAD_H / _KM_PER_LAT
    dlon = u_kmh_c * FORECAST_AHEAD_H / _KM_PER_LON
    log.debug("OF: wynik dlat=%.4f, dlon=%.4f, speed=%d km/h", dlat, dlon, round(speed_kmh))
    return dlat, dlon, round(speed_kmh)


def _max_density_km2(cl_km: np.ndarray, cl_n: np.ndarray,
                     cell_km: float = 10.0, hull_area_km2: float = 0.0) -> float:
    """Maksymalna gęstość wyładowań w siatce cell_km × cell_km.

    cl_n może być float (przeskalowane do 10-min tempa).
    Wyrównanie siatki może rozbić skupisko — podłoga ze średniej gwarantuje max >= avg.
    """
    if len(cl_km) == 0:
        return 0.0
    total = float(np.sum(cl_n))
    if total <= 0:
        return 0.0

    cell_area = cell_km ** 2
    lat_min = float(cl_km[:, 0].min())
    lon_min = float(cl_km[:, 1].min())
    grid: dict[tuple, float] = {}
    for pt, n in zip(cl_km, cl_n):
        key = (int((pt[0] - lat_min) / cell_km), int((pt[1] - lon_min) / cell_km))
        grid[key] = grid.get(key, 0.0) + float(n)

    max_dens = max(grid.values()) / cell_area if grid else 0.0

    # Podłoga: max musi być >= średniej (najgęstszy obszar >= średniej z definicji)
    if hull_area_km2 > 0:
        max_dens = max(max_dens, total / hull_area_km2)

    return round(max_dens, 4)


def _to_km(pts_deg: np.ndarray) -> np.ndarray:
    return pts_deg * _SCALE


def _to_deg(pts_km: np.ndarray) -> np.ndarray:
    return pts_km / _SCALE


def _dbscan(pts_km: np.ndarray, eps_km: float, min_samples: int) -> np.ndarray:
    """DBSCAN z KD-tree (O(n log n) pamięć). Zwraca etykiety (-1 = szum)."""
    try:
        from sklearn.cluster import DBSCAN as _DBSCAN
        return _DBSCAN(eps=eps_km, min_samples=min_samples,
                       algorithm="ball_tree", metric="euclidean",
                       n_jobs=1).fit(pts_km).labels_
    except ImportError:
        pass

    # Fallback bez sklearn — KD-tree z scipy
    from scipy.spatial import cKDTree
    tree   = cKDTree(pts_km)
    n      = len(pts_km)
    labels = np.full(n, -1, dtype=int)
    cid    = 0
    visited = np.zeros(n, dtype=bool)

    for i in range(n):
        if visited[i]:
            continue
        visited[i] = True
        nbrs = tree.query_ball_point(pts_km[i], eps_km)
        if len(nbrs) < min_samples:
            continue
        labels[i] = cid
        seeds = set(nbrs)
        seeds.discard(i)
        while seeds:
            q = seeds.pop()
            if not visited[q]:
                visited[q] = True
                q_nbrs = tree.query_ball_point(pts_km[q], eps_km)
                if len(q_nbrs) >= min_samples:
                    seeds.update(q_nbrs)
            if labels[q] == -1:
                labels[q] = cid
        cid += 1

    return labels


def _convex_hull_km(pts_km: np.ndarray) -> np.ndarray:
    """Andrew's monotone chain O(n log n), wejście i wyjście w km."""
    if len(pts_km) < 3:
        return pts_km
    # sortuj po x (lon_km), potem y (lat_km)
    s = pts_km[np.lexsort((pts_km[:, 0], pts_km[:, 1]))]

    def cross(O, A, B):
        return (A[1] - O[1]) * (B[0] - O[0]) - (A[0] - O[0]) * (B[1] - O[1])

    lo: list = []
    for p in s:
        while len(lo) >= 2 and cross(lo[-2], lo[-1], p) <= 0:
            lo.pop()
        lo.append(p)
    hi: list = []
    for p in s[::-1]:
        while len(hi) >= 2 and cross(hi[-2], hi[-1], p) <= 0:
            hi.pop()
        hi.append(p)
    lo.pop()
    hi.pop()
    return np.array(lo + hi)


def _buffer_hull_km(hull_km: np.ndarray, buffer_km: float) -> np.ndarray:
    """Przesuwa wierzchołki otoczki radialnie od centroidu o buffer_km [km]."""
    c     = hull_km.mean(axis=0)
    diff  = hull_km - c
    norms = np.linalg.norm(diff, axis=1, keepdims=True)
    norms = np.where(norms < 1e-6, buffer_km, norms)
    return hull_km + buffer_km * diff / norms


def _estimate_velocity(
    bins_by_t: dict, center_km: np.ndarray
) -> tuple[float, float, float, bool]:
    """Regresja centroidów historycznych punktów śledząc klaster wstecz slot po slot.

    Zwraca (dlat_deg, dlon_deg, speed_kmh, is_reliable).
    is_reliable=True gdy łańcuch jest wystarczająco długi, rozległy czasowo
    i liniowy (R² ważony ≥ HIST_MIN_R2).
    """
    if not bins_by_t:
        return 0.0, 0.0, 0, False

    sorted_bins = sorted(bins_by_t.items(), key=lambda x: x[0], reverse=True)

    chain: list[tuple[float, np.ndarray]] = []
    search_pos = center_km.copy()

    for bk, pts_list in sorted_bins:
        pts_km = np.array([[lat * _KM_PER_LAT, lon * _KM_PER_LON]
                           for lat, lon, n in pts_list])
        n_vals = np.array([n for lat, lon, n in pts_list], dtype=float)
        dists  = np.linalg.norm(pts_km - search_pos, axis=1)
        near   = dists <= FORECAST_SEARCH_KM
        if near.sum() < 2:
            continue
        w        = n_vals[near]
        centroid = (pts_km[near] * w[:, None]).sum(axis=0) / w.sum()
        chain.append((bk, centroid))
        search_pos = centroid

    if len(chain) < 2:
        return 0.0, 0.0, 0, False

    chain.sort(key=lambda c: c[0])
    nc      = len(chain)
    weights = np.arange(1, nc + 1, dtype=float)
    times   = np.array([c[0]    for c in chain])
    ys      = np.array([c[1][0] for c in chain])   # lat [km]
    xs      = np.array([c[1][1] for c in chain])   # lon [km]

    t0 = times[0]
    tn = times - t0   # sekundy od najstarszego binu

    T  = np.column_stack([tn, np.ones(nc)])
    Tw = T * weights[:, None]
    try:
        cy, *_ = np.linalg.lstsq(Tw, ys * weights, rcond=None)
        cx, *_ = np.linalg.lstsq(Tw, xs * weights, rcond=None)
    except np.linalg.LinAlgError:
        return 0.0, 0.0, 0, False

    # Ważony R² — jak dobrze liniowy model opisuje ruch centroidu
    y_pred = T @ cy
    x_pred = T @ cx
    w_sum  = weights.sum()
    ys_wm  = np.dot(weights, ys) / w_sum
    xs_wm  = np.dot(weights, xs) / w_sum
    ss_y   = np.dot(weights, (ys - ys_wm) ** 2)
    ss_x   = np.dot(weights, (xs - xs_wm) ** 2)
    r2_lat = 1.0 - np.dot(weights, (ys - y_pred)**2) / ss_y if ss_y > 1e-6 else 1.0
    r2_lon = 1.0 - np.dot(weights, (xs - x_pred)**2) / ss_x if ss_x > 1e-6 else 1.0
    r2     = min(r2_lat, r2_lon)

    span_min   = float(tn[-1]) / 60.0
    is_reliable = (nc >= HIST_MIN_BINS
                   and span_min >= HIST_MIN_SPAN_MIN
                   and r2 >= HIST_MIN_R2)

    vel_y_kmh = float(cy[0]) * 3600.0
    vel_x_kmh = float(cx[0]) * 3600.0
    speed_kmh = math.sqrt(vel_y_kmh ** 2 + vel_x_kmh ** 2)

    if speed_kmh > FORECAST_MAX_KMH:
        scale      = FORECAST_MAX_KMH / speed_kmh
        vel_y_kmh *= scale
        vel_x_kmh *= scale
        speed_kmh  = FORECAST_MAX_KMH

    dlat = vel_y_kmh * FORECAST_AHEAD_H / _KM_PER_LAT
    dlon = vel_x_kmh * FORECAST_AHEAD_H / _KM_PER_LON
    return dlat, dlon, round(speed_kmh), is_reliable


_SV_MAX_AGE_H = 12   # odrzuć dane GFS starsze niż tyle godzin


def _load_storm_vectors() -> dict | None:
    """Wczytuje najnowszy plik NPZ środowiska burzowego, jeśli jest wystarczająco świeży.

    Szuka najpierw storm_env_*.npz (pełne dane: wektory + CAPE + shear),
    a następnie storm_vectors_*.npz (tylko wektory — starszy format).
    """
    if not STORM_VECTORS_DIR.exists():
        return None
    files: list = []
    for pattern in ("storm_env_*.npz", "storm_vectors_*.npz"):
        files = sorted(STORM_VECTORS_DIR.glob(pattern))
        if files:
            break
    if not files:
        return None
    latest = files[-1]
    try:
        sv      = np.load(str(latest))
        init_ts = float(sv["init_time"][0])
        age_h   = (datetime.now(timezone.utc).timestamp() - init_ts) / 3600
        if age_h > _SV_MAX_AGE_H:
            log.info("Storm vectors za stare (%.1f h), używam centroidów", age_h)
            return None
        log.debug("Storm vectors: %s (wiek %.1f h)", latest.name, age_h)
        return dict(sv)
    except Exception as exc:
        log.warning("Błąd wczytywania storm vectors: %s", exc)
        return None


def _gfs_motion(
    sv: dict | None, center_km: np.ndarray, now_dt: datetime
) -> tuple[float, float, float] | None:
    """Zwraca (dlat, dlon, speed_kmh) z GFS USTM/VSTM lub None gdy brak danych.

    Wybiera najbliższy punkt siatki GFS i najbliższy krok czasowy do now_dt.
    Odrzuca dane, jeśli centroid leży poza obszarem GFS lub czas jest >90 min
    od najbliższego kroku prognozy.
    """
    if sv is None:
        return None

    lat = float(center_km[0]) / _KM_PER_LAT
    lon = float(center_km[1]) / _KM_PER_LON

    lats        = sv["lats"]
    lons        = sv["lons"]
    valid_times = sv["valid_times"]

    if lat < float(lats.min()) or lat > float(lats.max()):
        return None
    if lon < float(lons.min()) or lon > float(lons.max()):
        return None

    i_lat  = int(np.argmin(np.abs(lats - lat)))
    i_lon  = int(np.argmin(np.abs(lons - lon)))
    now_ts = now_dt.replace(tzinfo=timezone.utc).timestamp()
    i_time = int(np.argmin(np.abs(valid_times - now_ts)))

    if abs(valid_times[i_time] - now_ts) > 5400:   # >90 min → brak dopasowania
        return None

    u_ms = float(sv["ustm"][i_time, i_lat, i_lon])
    v_ms = float(sv["vstm"][i_time, i_lat, i_lon])

    if not (math.isfinite(u_ms) and math.isfinite(v_ms)):
        return None

    vel_x_kmh = u_ms * 3.6
    vel_y_kmh = v_ms * 3.6
    speed_kmh = math.sqrt(vel_x_kmh ** 2 + vel_y_kmh ** 2)

    if speed_kmh > FORECAST_MAX_KMH:
        scale      = FORECAST_MAX_KMH / speed_kmh
        vel_x_kmh *= scale
        vel_y_kmh *= scale
        speed_kmh  = FORECAST_MAX_KMH

    dlat = vel_y_kmh * FORECAST_AHEAD_H / _KM_PER_LAT
    dlon = vel_x_kmh * FORECAST_AHEAD_H / _KM_PER_LON
    return dlat, dlon, round(speed_kmh)


def _gfs_environment(
    sv: dict | None, center_km: np.ndarray, now_dt: datetime
) -> tuple[float, float, float, str] | None:
    """Zwraca (cape_jkg, shear06_ms, wmaxshear_m2s2, valid_time_str) z GFS.

    Wymaga pliku storm_env_*.npz (nowy format z pełnym środowiskiem).
    Zwraca None jeśli dane niedostępne lub poza obszarem/oknem czasowym.
    """
    if sv is None or "cape" not in sv:
        return None

    lat = float(center_km[0]) / _KM_PER_LAT
    lon = float(center_km[1]) / _KM_PER_LON

    lats        = sv["lats"]
    lons        = sv["lons"]
    valid_times = sv["valid_times"]

    if lat < float(lats.min()) or lat > float(lats.max()):
        return None
    if lon < float(lons.min()) or lon > float(lons.max()):
        return None

    i_lat  = int(np.argmin(np.abs(lats - lat)))
    i_lon  = int(np.argmin(np.abs(lons - lon)))
    now_ts = now_dt.replace(tzinfo=timezone.utc).timestamp()
    i_time = int(np.argmin(np.abs(valid_times - now_ts)))

    if abs(valid_times[i_time] - now_ts) > 5400:
        return None

    cape_v  = float(sv["cape"][i_time, i_lat, i_lon])
    shear_v = float(sv["shear"][i_time, i_lat, i_lon])
    wms_v   = float(sv["wmaxshear"][i_time, i_lat, i_lon])

    if not all(math.isfinite(x) for x in (cape_v, shear_v, wms_v)):
        return None

    vt_str = datetime.fromtimestamp(float(valid_times[i_time]), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M")
    return max(cape_v, 0.0), max(shear_v, 0.0), max(wms_v, 0.0), vt_str


def _compute_forecast(slots: dict, now: datetime) -> dict | None:
    """Klasteryzuje wyładowania (DBSCAN) i dla każdego klastra liczy prognozę.

    Wszystkie odległości wewnętrznie w km; JSON zawiera stopnie + speed_kmh.
    Zwraca {"clusters": [...], "valid_until": "..."} lub None.
    """
    now_naive     = _to_naive_utc(now)
    recent_cutoff = now_naive - timedelta(minutes=FORECAST_RECENT_MIN)
    motion_cutoff = now_naive - timedelta(hours=FORECAST_MOTION_H)

    recent_deg: list[list[float]] = []
    recent_n:   list[int]         = []
    recent_t:   list[float]       = []
    bins_by_t:  dict[float, list] = {}

    for slot in slots.values():
        if not slot.get("ok") or not slot.get("groups"):
            continue
        for g in slot["groups"]:
            try:
                t = datetime.strptime(g["t"], "%Y-%m-%dT%H:%M:%S")
            except (ValueError, KeyError):
                continue
            lat, lon, n = g["lat"], g["lon"], int(g.get("n", 1))

            if t >= recent_cutoff:
                recent_deg.append([lat, lon])
                recent_n.append(n)
                recent_t.append(t.timestamp())
            if t >= motion_cutoff:
                b  = t.replace(second=0, microsecond=0)
                b  = b.replace(minute=(b.minute // SLOT_MINUTES) * SLOT_MINUTES)
                bins_by_t.setdefault(b.timestamp(), []).append((lat, lon, n))

    if len(recent_deg) < FORECAST_MIN_CLUSTER:
        return None

    sv   = _load_storm_vectors()
    cmax = _load_cmax()
    pts_deg  = np.array(recent_deg)
    pts_km   = _to_km(pts_deg)
    pts_n    = np.array(recent_n,  dtype=int)
    pts_t    = np.array(recent_t,  dtype=float)
    labels   = _dbscan(pts_km, FORECAST_EPS_KM, FORECAST_MIN_SAMPLES)
    t10_cut  = (now_naive - timedelta(minutes=10)).timestamp()

    clusters_out = []
    for cid in set(labels):
        if cid == -1:
            continue
        mask   = labels == cid
        cl_km  = pts_km[mask]
        cl_deg = pts_deg[mask]
        cl_n   = pts_n[mask]
        cl_t   = pts_t[mask]
        if len(cl_km) < FORECAST_MIN_CLUSTER:
            continue

        center_km  = cl_km.mean(axis=0)
        center_deg = cl_deg.mean(axis=0)

        # Priorytet źródła ruchu: radar OF → GFS
        # (historia wyładowań tymczasowo wyłączona)
        radar = _radar_motion(cmax, center_km)
        dlat = dlon = speed_kmh = 0

        if radar is not None:
            dlat, dlon, speed_kmh = radar
            motion_source = "radar"
            motion_label  = "radar CMAX"
            log.debug("Klaster %d: radar OF %.1f km/h", cid, speed_kmh)
        else:
            gfs = _gfs_motion(sv, center_km, now_naive)
            if gfs is not None:
                dlat, dlon, speed_kmh = gfs
                motion_source = "gfs"
                gfs_run = datetime.fromtimestamp(float(sv["init_time"][0]), tz=timezone.utc)
                motion_label = f"GFS {gfs_run.strftime('%Y%m%d %HZ')}"
                log.debug("Klaster %d: GFS %.1f km/h", cid, speed_kmh)
            else:
                motion_source = "brak"
                motion_label  = "brak danych ruchu"
                log.debug("Klaster %d: brak OF i GFS", cid)

        # Kierunek ruchu (0° = N, zgodnie z ruchem wskazówek zegara)
        dy_km       = dlat * _KM_PER_LAT
        dx_km       = dlon * _KM_PER_LON
        dir_deg     = round(math.degrees(math.atan2(dx_km, dy_km)) % 360)
        dir_compass = _compass(dir_deg)

        # Otoczka pełnego klastra (okno FORECAST_RECENT_MIN) — do polygonu prognozy
        hull_cur_km = _convex_hull_km(cl_km)

        # Statystyki: ten sam zakres czasu dla obszaru i liczby uderzeń,
        # wynik przeliczany na tempo /10 min.
        m10      = cl_t >= t10_cut
        cl_km_10 = cl_km[m10]
        cl_n_10  = cl_n[m10]

        if len(cl_km_10) >= 3:
            # Wystarczająco dużo punktów w ostatnich 10 min
            hull_stat_km = _convex_hull_km(cl_km_10)
            area_km2     = round(_polygon_area_km2(hull_stat_km), 1)
            cl_km_stat   = cl_km_10
            cl_n_stat    = cl_n_10.astype(float)
            scale        = 1.0
        else:
            # Za mało punktów 10-min — bierz pełne okno i skaluj do 10 min
            area_km2   = round(_polygon_area_km2(hull_cur_km), 1)
            cl_km_stat = cl_km
            span_s     = float(cl_t.max() - cl_t.min()) if len(cl_t) > 1 else 600.0
            scale      = 10.0 / max(span_s / 60.0, 1.0)
            cl_n_stat  = cl_n.astype(float) * scale

        count_10min  = round(float(np.sum(cl_n_stat)))
        density_km2  = round(count_10min / area_km2, 4) if area_km2 > 0 else 0.0
        max_dens_km2 = _max_density_km2(cl_km_stat, cl_n_stat, hull_area_km2=area_km2)

        # Środowisko konwekcyjne z GFS (CAPE, shear, WmaxShear) dla centroidu
        env = _gfs_environment(sv, center_km, now_naive)

        # Maks. odbiciowość CMAX dla obszaru bieżącego klastra
        hull_cur_deg = _to_deg(hull_cur_km)
        max_dbz = _max_dbz_in_polygon(cmax, hull_cur_deg) if cmax is not None else None

        # Polygon prognozy: stożek niepewności.
        # Bufor bieżący (mały) i przesunięty (rosnący z czasem i dystansem),
        # następnie wypukła otoczka obu — naturalny kształt stożka.
        displacement_km = np.array([dlat * _KM_PER_LAT, dlon * _KM_PER_LON])
        dist_km         = float(np.linalg.norm(displacement_km))
        end_buffer_km   = (FORECAST_BUFFER_KM
                           + FORECAST_SPREAD_KM_PER_H * FORECAST_AHEAD_H
                           + dist_km * FORECAST_SPREAD_FRACTION)

        proj_km      = cl_km + displacement_km
        hull_proj_km = _convex_hull_km(proj_km)
        if len(hull_cur_km) < 3 or len(hull_proj_km) < 3:
            continue

        buf_cur_km  = _buffer_hull_km(hull_cur_km,  FORECAST_BUFFER_KM)
        buf_proj_km = _buffer_hull_km(hull_proj_km, end_buffer_km)
        swept_km    = _convex_hull_km(np.vstack([buf_cur_km, buf_proj_km]))
        if len(swept_km) < 3:
            continue
        poly_deg = _to_deg(swept_km)

        cluster_type = "intense" if max_dens_km2 >= FORECAST_INTENSE_DENS else "normal"

        clusters_out.append({
            "polygon":           [[round(float(p[0]), 4), round(float(p[1]), 4)] for p in poly_deg],
            "cluster_type":      cluster_type,
            "motion_source":     motion_source,
            "motion_label":      motion_label,
            "speed_kmh":         speed_kmh,
            "direction_deg":     dir_deg,
            "direction_compass": dir_compass,
            "stats": {
                "count_10min":     count_10min,
                "area_km2":        area_km2,
                "density_km2":     density_km2,
                "max_density_km2": max_dens_km2,
                "max_dbz":         max_dbz,
                "cape_jkg":        round(env[0]) if env else None,
                "shear06_ms":      round(env[1], 1) if env else None,
                "wmaxshear":       round(env[2]) if env else None,
                "env_valid_time":  env[3] if env else None,
            },
        })

    if not clusters_out:
        return None

    valid_until = (now_naive + timedelta(hours=FORECAST_AHEAD_H)).strftime("%Y-%m-%dT%H:%M:%S")
    return {"clusters": clusters_out, "valid_until": valid_until}


def _point_in_polygon(lat: float, lon: float, polygon: list) -> bool:
    """Ray-casting — polygon jako lista [lat, lon]."""
    n = len(polygon)
    inside = False
    j = n - 1
    for i in range(n):
        yi, xi = polygon[i]
        yj, xj = polygon[j]
        if ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def _send_telegram(token: str, chat_id: str, text: str) -> None:
    import urllib.request
    payload = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=10)


def _check_alerts(forecast: dict | None, now: datetime) -> None:
    """Sprawdza czy monitorowane punkty leżą w prognozowanych poligonach
    i wysyła alert Telegram (z cooldownem ALERT_COOLDOWN_S)."""
    token   = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    if not forecast or not forecast.get("clusters"):
        return
    if not WATCH_POINTS_FILE.exists():
        return

    try:
        watch_points = json.loads(WATCH_POINTS_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("Nie można wczytać watch_points.json: %s", exc)
        return

    state: dict = {}
    if ALERT_STATE_FILE.exists():
        try:
            state = json.loads(ALERT_STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass

    now_ts   = _to_naive_utc(now).timestamp()
    modified = False

    for point in watch_points:
        name = point.get("name", "?")
        lat  = float(point["lat"])
        lon  = float(point["lon"])

        for cl in forecast["clusters"]:
            polygon = cl.get("polygon", [])
            if not polygon or not _point_in_polygon(lat, lon, polygon):
                continue

            last_alert = state.get(name, 0)
            if now_ts - last_alert < ALERT_COOLDOWN_S:
                log.debug("Alert dla %s w cooldownie (%.0f min)", name,
                          (ALERT_COOLDOWN_S - (now_ts - last_alert)) / 60)
                break

            s            = cl.get("stats", {})
            cluster_type = cl.get("cluster_type", "normal")
            speed        = cl.get("speed_kmh", 0)
            direction    = cl.get("direction_compass", "—")
            valid_until  = forecast.get("valid_until", "—")
            emoji        = "🔴" if cluster_type == "intense" else "🟠"
            intensity    = "intensywna" if cluster_type == "intense" else "ogólna"

            msg = (
                f"{emoji} <b>Ostrzeżenie burzowe</b>\n"
                f"Lokalizacja <b>{name}</b> może znaleźć się w obszarze burzy!\n"
                f"\n"
                f"Typ komórki: {intensity}\n"
                f"Prędkość: {speed} km/h, kierunek: {direction}\n"
                f"Gęstość wyładowań: {s.get('density_km2', '—')} /km²/10min\n"
                f"Maks. gęstość: {s.get('max_density_km2', '—')} /km²/10min\n"
                f"Prognoza ważna do: {valid_until}"
            )
            try:
                _send_telegram(token, chat_id, msg)
                state[name] = now_ts
                modified = True
                log.info("Alert Telegram → %s (%s)", name, intensity)
            except Exception as exc:
                log.warning("Błąd wysyłania Telegram dla %s: %s", name, exc)
            break   # jeden alert per punkt per run

    if modified:
        ALERT_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        ALERT_STATE_FILE.write_text(json.dumps(state), encoding="utf-8")


def _save(slots: dict, now: datetime) -> None:
    forecast = _compute_forecast(slots, now)
    if forecast:
        log.info("Prognoza: %d klastrów, ważna do %s",
                 len(forecast["clusters"]), forecast["valid_until"])
    else:
        log.info("Prognoza: za mało danych")

    data = {
        "generated": _to_naive_utc(now).strftime("%Y-%m-%dT%H:%M:%S"),
        "slots":     slots,
        "forecast":  forecast,
    }
    OUTPUT_JSON.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log.info("Zapisano: %s (%d slotów)", OUTPUT_JSON, len(slots))
    _check_alerts(forecast, now)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Pobierz dane wyładowań MTG-LI")
    parser.add_argument("--log-level", default="INFO", metavar="LEVEL")
    parser.add_argument("--history-hours", type=int, default=6, metavar="H")
    args = parser.parse_args()

    setup_logging(LOG_DIR, level=args.log_level.upper())
    sys.exit(main(history_hours=args.history_hours))
