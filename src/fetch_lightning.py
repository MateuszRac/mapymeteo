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
ALERT_COOLDOWN_S  = 3600   # min. przerwa między alertami dla tej samej lokalizacji [s]

COLLECTION_ID   = "EO:EUM:DAT:0782"
LON_MIN, LON_MAX = 10.0, 30.0
LAT_MIN, LAT_MAX = 40.0, 60.0
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
FORECAST_EPS_KM          = 50    # promień klastrowania DBSCAN [km]
FORECAST_SEARCH_KM       = 60    # promień szukania historii dla klastra [km]
FORECAST_BUFFER_KM       = 5     # bazowy bufor wokół bieżącego polygonu [km]
FORECAST_SPREAD_KM_PER_H = 1.0   # przyrost buforu końcowego z czasem [km/h]
FORECAST_SPREAD_FRACTION = 0.05  # przyrost buforu końcowego z odległością [udział dystansu]
FORECAST_MAX_KMH         = 100   # maks. prędkość komórki [km/h]
FORECAST_MIN_SAMPLES     = 2     # min punktów w klastrze DBSCAN
FORECAST_MIN_CLUSTER     = 3     # min punktów klastra do rysowania polygonu
FORECAST_INTENSE_DENS    = 10.0  # próg gęstości dla klastra intensywnego [/km²/10min]

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


def _max_density_km2(cl_km: np.ndarray, cl_n: np.ndarray,
                     cell_km: float = 10.0) -> float:
    """Maksymalna gęstość wyładowań w siatce cell_km × cell_km [/km²]."""
    if len(cl_km) == 0:
        return 0.0
    lat_min = cl_km[:, 0].min()
    lon_min = cl_km[:, 1].min()
    grid: dict[tuple, int] = {}
    for pt, n in zip(cl_km, cl_n):
        key = (int((pt[0] - lat_min) / cell_km), int((pt[1] - lon_min) / cell_km))
        grid[key] = grid.get(key, 0) + int(n)
    return round(max(grid.values()) / cell_km ** 2, 4) if grid else 0.0


def _to_km(pts_deg: np.ndarray) -> np.ndarray:
    return pts_deg * _SCALE


def _to_deg(pts_km: np.ndarray) -> np.ndarray:
    return pts_km / _SCALE


def _dbscan(pts_km: np.ndarray, eps_km: float, min_samples: int) -> np.ndarray:
    """DBSCAN w czystym numpy, odległości w km. Zwraca etykiety (-1 = szum)."""
    n       = len(pts_km)
    labels  = np.full(n, -1, dtype=int)
    visited = np.zeros(n, dtype=bool)
    dists   = np.sqrt(((pts_km[:, None] - pts_km[None, :]) ** 2).sum(axis=2))
    cid     = 0

    for i in range(n):
        if visited[i]:
            continue
        visited[i] = True
        nbrs = np.where(dists[i] <= eps_km)[0]
        if len(nbrs) < min_samples:
            continue
        labels[i] = cid
        seeds = set(nbrs.tolist())
        seeds.discard(i)
        while seeds:
            q = seeds.pop()
            if not visited[q]:
                visited[q] = True
                q_nbrs = np.where(dists[q] <= eps_km)[0]
                if len(q_nbrs) >= min_samples:
                    seeds.update(q_nbrs.tolist())
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
) -> tuple[float, float, float]:
    """Regresja centroidów historycznych punktów śledząc klaster wstecz slot po slot.

    Zaczyna od aktualnego centroidu (center_km) i dla każdego binu historycznego
    szuka punktów blisko POPRZEDNIO znalezionego centroidu — nie stałego centrum.
    Dzięki temu poprawnie śledzi burze, które przesunęły się o >FORECAST_SEARCH_KM.

    Zwraca (dlat_deg, dlon_deg, speed_kmh) — przemieszczenie za FORECAST_AHEAD_H h.
    """
    if not bins_by_t:
        return 0.0, 0.0, 0.0

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
        search_pos = centroid   # kolejny krok szuka blisko tego centroidu

    if len(chain) < 2:
        return 0.0, 0.0, 0.0

    chain.sort(key=lambda c: c[0])
    nc      = len(chain)
    weights = np.arange(1, nc + 1, dtype=float)   # nowsze sloty → wyższa waga
    times   = np.array([c[0]    for c in chain])
    ys      = np.array([c[1][0] for c in chain])   # lat [km]
    xs      = np.array([c[1][1] for c in chain])   # lon [km]

    # Normalizuj czas od zera — Unix timestamps (~1.7e9) powodują
    # katastrofalne skrócenie w lstsq i dają slope ≈ 0.
    t0     = times[0]
    tn     = times - t0   # sekundy od najstarszego binu [0 … ~10800]

    T  = np.column_stack([tn, np.ones(nc)])
    Tw = T * weights[:, None]
    try:
        cy, *_ = np.linalg.lstsq(Tw, ys * weights, rcond=None)
        cx, *_ = np.linalg.lstsq(Tw, xs * weights, rcond=None)
    except np.linalg.LinAlgError:
        return 0.0, 0.0, 0.0

    vel_y_kmh = float(cy[0]) * 3600.0   # km/s → km/h
    vel_x_kmh = float(cx[0]) * 3600.0
    speed_kmh = math.sqrt(vel_y_kmh ** 2 + vel_x_kmh ** 2)

    if speed_kmh > FORECAST_MAX_KMH:
        scale      = FORECAST_MAX_KMH / speed_kmh
        vel_y_kmh *= scale
        vel_x_kmh *= scale
        speed_kmh  = FORECAST_MAX_KMH

    dlat = vel_y_kmh * FORECAST_AHEAD_H / _KM_PER_LAT
    dlon = vel_x_kmh * FORECAST_AHEAD_H / _KM_PER_LON
    return dlat, dlon, round(speed_kmh)


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

        center_km             = cl_km.mean(axis=0)
        center_deg            = cl_deg.mean(axis=0)
        dlat, dlon, speed_kmh = _estimate_velocity(bins_by_t, center_km)

        # Kierunek ruchu (0° = N, zgodnie z ruchem wskazówek zegara)
        dy_km         = dlat * _KM_PER_LAT
        dx_km         = dlon * _KM_PER_LON
        dir_deg       = round(math.degrees(math.atan2(dx_km, dy_km)) % 360)
        dir_compass   = _compass(dir_deg)

        # Pole aktualnego klastra (przed rzutowaniem)
        hull_cur_km   = _convex_hull_km(cl_km)
        area_km2      = round(_polygon_area_km2(hull_cur_km), 1)

        # Statystyki z ostatnich 10 min
        m10           = cl_t >= t10_cut
        cl_km_10      = cl_km[m10]
        cl_n_10       = cl_n[m10]
        count_10min   = int(cl_n_10.sum()) if m10.any() else 0
        density_km2   = round(count_10min / area_km2, 4) if area_km2 > 0 else 0.0
        max_dens_km2  = _max_density_km2(cl_km_10, cl_n_10)

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
            "speed_kmh":         speed_kmh,
            "direction_deg":     dir_deg,
            "direction_compass": dir_compass,
            "stats": {
                "count_10min":     count_10min,
                "area_km2":        area_km2,
                "density_km2":     density_km2,
                "max_density_km2": max_dens_km2,
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
