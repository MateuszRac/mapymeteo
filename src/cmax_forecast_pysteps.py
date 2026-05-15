"""
cmax_forecast_pysteps.py — prognoza CMAX z użyciem PySTEPS.

Algorytm:
  1. Optical flow: PySTEPS Lucas-Kanade (dense_lucaskanade)
  2. Kalibracja ruchu: regresja GFS (wiatry na wielu poziomach) → jeden wektor
  3. Nowcasting: S-PROG (deterministyczny STEPS z dekompozycją kaskadową)
     lub proste ekstrapolowanie semi-Lagrange'a jako fallback

S-PROG vs prosta ekstrapolacja:
  - Prosta ekstrapolacja przesuwałby cały obraz jednorodnie — po 2h
    drobne komórki wyglądają tak samo jak na początku (nierealistyczne).
  - S-PROG rozkłada pole na poziomy przestrzenne (FFT cascade).
    Drobne struktury zanikają szybciej niż duże — fizycznie poprawne.

Uruchomienie:
  python src/cmax_forecast_pysteps.py
  python src/cmax_forecast_pysteps.py --method extrap
  python src/cmax_forecast_pysteps.py --log-level DEBUG --dry-run
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import sys
import threading
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

try:
    from radar.renderer import RadarRenderer
    from radar.palette import RadarPalette
    from transfer.ftp import FtpUploader
    from log_setup import setup_logging
except ImportError:
    from src.radar.renderer import RadarRenderer
    from src.radar.palette import RadarPalette
    from src.transfer.ftp import FtpUploader
    from src.log_setup import setup_logging

from dotenv import load_dotenv
load_dotenv(ROOT / ".env", override=True)

PROJECT_PATH = Path(os.getenv("PROJECT_PATH", str(ROOT)))

OVERLAY_DIR   = PROJECT_PATH / "img" / "polrad" / "overlay"
MANIFEST      = PROJECT_PATH / "img" / "polrad" / "manifest.json"
COLOR_TABLES  = PROJECT_PATH / "data" / "color_tables"
LOG_DIR       = PROJECT_PATH / "logs"
FTP_IMG_DIR   = os.getenv("FTP_REMOTE_IMG_DIR", "img")

_CMAX_CACHE_PATH = PROJECT_PATH / "data" / "cmax" / "cmax_latest.npz"
_SV_DIR          = PROJECT_PATH / "data" / "storm_vectors"
_SV_MAX_AGE_H    = 12
_SV_MAX_DT_H     = 2
_REGR_MIN_PIX    = 200

FORECAST_STEPS    = 24   # liczba klatek
FORECAST_STEP_MIN = 5    # krok [min]
CACHE_MAX_AGE_H   = 4    # maksymalny wiek cache CMAX

PRODUCT_KEY = "COMPO_CMAX_250__DBZH"
LABEL       = "CMAX 250 km – DBZH"

log = logging.getLogger("cmax_forecast_pysteps")


# ─────────────────────────────────────────────────────────────────────────────
#  Ładowanie danych
# ─────────────────────────────────────────────────────────────────────────────

def load_cmax() -> dict | None:
    import time as _t
    if not _CMAX_CACHE_PATH.exists():
        log.error("Brak cache CMAX: %s", _CMAX_CACHE_PATH)
        return None
    c = np.load(_CMAX_CACHE_PATH)
    ts_key = "timestamps" if "timestamps" in c.files else "timestamp"
    ts = c[ts_key]
    age_h = (_t.time() - float(ts[-1])) / 3600.0
    if age_h > CACHE_MAX_AGE_H:
        log.error("Cache CMAX za stary (%.1f h)", age_h)
        return None
    if c["dbz"].ndim != 3 or len(c["dbz"]) < 2:
        log.error("Za mało klatek w cache (%d)", len(c["dbz"]) if c["dbz"].ndim == 3 else 0)
        return None
    log.info("Cache CMAX: %d klatek, ostatnia %s UTC",
             len(c["dbz"]), datetime.utcfromtimestamp(float(ts[-1])).strftime("%H:%M"))
    return {"dbz": c["dbz"], "timestamps": ts, "lats": c["lats"], "lons": c["lons"]}


def load_storm_env() -> dict | None:
    import time as _t
    files = sorted(_SV_DIR.glob("storm_env_*.npz"))
    if not files:
        log.warning("Brak plików storm_env w %s", _SV_DIR)
        return None
    latest = files[-1]
    if _t.time() - latest.stat().st_mtime > _SV_MAX_AGE_H * 3600:
        log.warning("storm_env za stary: %s", latest.name)
        return None
    d = np.load(latest)
    result = {k: d[k] for k in d.files}
    log.info("storm_env: %s (%d kroków)", latest.name, len(result.get("valid_times", [])))
    return result


def find_gfs_time_idx(sv: dict, target_unix: float) -> int | None:
    vt   = sv["valid_times"]
    diff = np.abs(vt - target_unix)
    idx  = int(np.argmin(diff))
    if diff[idx] > _SV_MAX_DT_H * 3600:
        log.warning("Brak kroku GFS blisko radaru (Δ=%.1f h)", diff[idx] / 3600)
        return None
    return idx


# ─────────────────────────────────────────────────────────────────────────────
#  Optical flow + regresja GFS
# ─────────────────────────────────────────────────────────────────────────────

def compute_lk_motion(R_ps: np.ndarray, dt_h: float,
                      dpix_lon_km: float, dpix_lat_km: float
                      ) -> tuple[np.ndarray, np.ndarray]:
    """
    PySTEPS Lucas-Kanade dense optical flow.
    R_ps: (N, H, W) w orientacji PySTEPS (row0=north).
    Zwraca (u_kmh, v_kmh) w układzie geograficznym (northward positive).
    """
    from pysteps.motion.lucaskanade import dense_lucaskanade
    V = dense_lucaskanade(R_ps)   # (2, H, W), px/timestep, standard image coords
    # V[0]: row (positive=south w standard), V[1]: col (positive=east)
    u_kmh =  V[1] * dpix_lon_km / dt_h
    v_kmh = -V[0] * dpix_lat_km / dt_h   # odwróć: nasz układ northward positive
    return u_kmh, v_kmh


def interp_gfs_to_grid(sv: dict, t_idx: int,
                       lats: np.ndarray, lons: np.ndarray) -> dict:
    """Interpoluje wiatry GFS (m/s→km/h) na siatkę CMAX."""
    from scipy.interpolate import RegularGridInterpolator
    gfs_lats = sv["lats"].astype(np.float64)
    gfs_lons = sv["lons"].astype(np.float64)
    flip = gfs_lats[0] > gfs_lats[-1]
    if flip:
        gfs_lats = gfs_lats[::-1]
    pts = np.column_stack([lats.ravel().astype(np.float64),
                           lons.ravel().astype(np.float64)])
    key_map = {
        "u_10m": "u_10m", "v_10m": "v_10m",
        "u_800": "u_800", "v_800": "v_800",
        "u_700": "u_700", "v_700": "v_700",
        "u_600": "u_600", "v_600": "v_600",
        "u_500": "u_500", "v_500": "v_500",
        "u_450": "u_450", "v_450": "v_450",
        "ustm":  "u_ustm", "vstm":  "v_ustm",
    }
    result = {}
    for npz_key, out_key in key_map.items():
        if npz_key not in sv:
            continue
        arr = sv[npz_key][t_idx].astype(np.float64)
        if flip:
            arr = arr[::-1, :]
        interp = RegularGridInterpolator(
            (gfs_lats, gfs_lons), arr,
            method="linear", bounds_error=False, fill_value=None)
        result[out_key] = (interp(pts).reshape(lats.shape) * 3.6).astype(np.float32)
    return result


def gfs_regression(u_of: np.ndarray, v_of: np.ndarray,
                   gfs_grid: dict, dbz: np.ndarray
                   ) -> tuple[float, float] | None:
    """
    Regresja liniowa: wiatry GFS → ruch ech OF.
    Zwraca (u_domain, v_domain) w km/h jako skalary (jednorodne pole).
    """
    LEVELS = ["10m", "800", "700", "600", "500", "450", "ustm"]
    u_cols = [gfs_grid[f"u_{lev}"] for lev in LEVELS if f"u_{lev}" in gfs_grid]
    v_cols = [gfs_grid[f"v_{lev}"] for lev in LEVELS if f"v_{lev}" in gfs_grid]
    if not u_cols:
        return None

    mask = (np.nan_to_num(dbz, nan=0.0) >= 10.0) & np.isfinite(u_of) & np.isfinite(v_of)
    n = int(mask.sum())
    if n < _REGR_MIN_PIX:
        log.debug("Regresja: za mało pikseli ech (%d)", n)
        return None

    def _fit(cols, y):
        X = np.column_stack([c[mask] for c in cols] + [np.ones(n, dtype=np.float32)])
        coeff, _, _, _ = np.linalg.lstsq(X, y[mask], rcond=None)
        return coeff

    coeff_u = _fit(u_cols, u_of)
    coeff_v = _fit(v_cols, v_of)

    # Predykcja jednego wektora — średnie GFS po obszarze echa
    u_means = np.array([float(c[mask].mean()) for c in u_cols] + [1.0])
    v_means = np.array([float(c[mask].mean()) for c in v_cols] + [1.0])
    u_domain = float(u_means @ coeff_u)
    v_domain = float(v_means @ coeff_v)

    lev_names = [lev for lev in LEVELS if f"u_{lev}" in gfs_grid]
    w_str = " ".join(f"{l}={c:.2f}" for l, c in zip(lev_names, coeff_u[:-1]))
    log.info("GFS regresja: %d px, wagi U: %s bias=%.1f → u=%.1f v=%.1f km/h",
             n, w_str, coeff_u[-1], u_domain, v_domain)
    return u_domain, v_domain


def get_motion_vector(cmax: dict) -> tuple[float, float, str]:
    """
    Zwraca (u_kmh, v_kmh, źródło) — jednorodny wektor ruchu dla domeny.
    Priorytet: GFS-regression > LK-mean > fallback(0,0).
    """
    dbz_stack  = cmax["dbz"]
    timestamps = cmax["timestamps"]
    lats, lons = cmax["lats"], cmax["lons"]
    N, H, W    = dbz_stack.shape

    dpix_lat_km = abs(float(lats[0,0] - lats[1,0])) * 111.32
    dpix_lon_km = abs(float(lons[0,1] - lons[0,0])) * 111.32 * float(np.cos(np.radians(float(lats.mean()))))

    valid_dts = [float(timestamps[i+1] - timestamps[i])
                 for i in range(N-1) if 0 < timestamps[i+1] - timestamps[i] <= 900]
    dt_s = float(np.mean(valid_dts)) if valid_dts else 300.0
    dt_h = dt_s / 3600.0

    # PySTEPS wymaga row0=north — flip naszych danych (row0=south)
    R_raw = np.where(np.isfinite(dbz_stack), dbz_stack, -20.0)
    R_ps  = R_raw[:, ::-1, :]   # (N, H, W) row0=north

    # ── LK optical flow ───────────────────────────────────────────────
    try:
        u_of, v_of = compute_lk_motion(R_ps, dt_h, dpix_lon_km, dpix_lat_km)
        lk_ok = True
        log.info("LK OF: u_mean=%.1f v_mean=%.1f km/h",
                 float(np.nanmean(u_of)), float(np.nanmean(v_of)))
    except Exception as exc:
        log.warning("LK OF błąd: %s", exc)
        u_of = v_of = np.full((H, W), np.nan, dtype=np.float32)
        lk_ok = False

    # ── GFS regresja ─────────────────────────────────────────────────
    sv = load_storm_env()
    if sv is not None:
        t_idx = find_gfs_time_idx(sv, float(timestamps[-1]))
        if t_idx is not None:
            dbz_last = np.nan_to_num(dbz_stack[-1], nan=0.0)
            gfs_grid = interp_gfs_to_grid(sv, t_idx, lats, lons)
            res = gfs_regression(u_of, v_of, gfs_grid, dbz_last)
            if res is not None:
                return res[0], res[1], "gfs_regression"

    # ── Fallback: uśredniony LK ───────────────────────────────────────
    if lk_ok and np.any(np.isfinite(u_of)):
        u = float(np.nanmean(u_of))
        v = float(np.nanmean(v_of))
        log.info("Fallback: LK mean u=%.1f v=%.1f km/h", u, v)
        return u, v, "lk_mean"

    log.warning("Brak estymacji ruchu — używam zera")
    return 0.0, 0.0, "none"


# ─────────────────────────────────────────────────────────────────────────────
#  PySTEPS nowcasting
# ─────────────────────────────────────────────────────────────────────────────

def build_pysteps_motion(u_kmh: float, v_kmh: float,
                         H: int, W: int,
                         dt_h: float, dpix_lon_km: float, dpix_lat_km: float
                         ) -> np.ndarray:
    """
    Buduje pole ruchu V (2, H, W) dla PySTEPS z jednorodnego wektora km/h.
    Konwencja PySTEPS: V[0]=row (positive=south), V[1]=col (positive=east).
    Dane są w orientacji PySTEPS (row0=north), więc northward = -row.
    """
    V = np.zeros((2, H, W), dtype=np.float32)
    V[0] = -(v_kmh * dt_h / dpix_lat_km)   # northward → negative row
    V[1] =   u_kmh * dt_h / dpix_lon_km    # eastward  → positive col
    return V


def run_sproc(R_ps: np.ndarray, V: np.ndarray, n_steps: int,
              R_thr: float = 10.0) -> np.ndarray:
    """
    S-PROG (deterministyczny STEPS) — dekompozycja kaskadowa FFT.
    Drobne struktury zanikają szybciej niż duże (fizycznie poprawne).

    R_ps: (N, H, W) ostatnie N klatek, row0=north, w dBZ
    V:    (2, H, W) pole ruchu px/timestep
    Zwraca: (n_steps, H, W) w dBZ, row0=north
    """
    from pysteps.nowcasts.sproc import forecast as sproc_forecast

    # S-PROG potrzebuje co najmniej ar_order+1=3 klatek
    ar_order = min(2, R_ps.shape[0] - 1)
    R_in = R_ps[-(ar_order + 1):]   # ostatnie 3 (lub mniej) klatki

    R_f = sproc_forecast(
        R_in,
        V,
        n_steps,
        n_cascade_levels = 6,
        R_thr            = R_thr,
        ar_order         = ar_order,
    )
    return R_f   # (n_steps, H, W)


def run_extrapolation(R_ps: np.ndarray, V: np.ndarray, n_steps: int) -> np.ndarray:
    """
    Prosta semi-Lagrange'a ekstrapolacja (fallback gdy S-PROG zawiedzie).
    """
    from pysteps.nowcasts.extrapolation import forecast as extrap_forecast
    return extrap_forecast(R_ps[-1], V, n_steps)   # (n_steps, H, W)


def compute_forecast(cmax: dict, method: str = "sproc") -> np.ndarray | None:
    """
    Główna funkcja prognozy.
    Zwraca tablicę (FORECAST_STEPS, H, W) w dBZ, row0=south (nasz układ),
    lub None jeśli coś zawiedzie.
    """
    dbz_stack  = cmax["dbz"]
    timestamps = cmax["timestamps"]
    lats, lons = cmax["lats"], cmax["lons"]
    N, H, W    = dbz_stack.shape

    dpix_lat_km = abs(float(lats[0,0] - lats[1,0])) * 111.32
    dpix_lon_km = abs(float(lons[0,1] - lons[0,0])) * 111.32 * float(np.cos(np.radians(float(lats.mean()))))
    valid_dts   = [float(timestamps[i+1] - timestamps[i])
                   for i in range(N-1) if 0 < timestamps[i+1]-timestamps[i] <= 900]
    dt_s = float(np.mean(valid_dts)) if valid_dts else 300.0
    dt_h = dt_s / 3600.0

    # ── Wektor ruchu ────────────────────────────────────────────────
    u_kmh, v_kmh, source = get_motion_vector(cmax)
    log.info("Wektor ruchu: u=%.1f v=%.1f km/h (źródło: %s)", u_kmh, v_kmh, source)

    V = build_pysteps_motion(u_kmh, v_kmh, H, W, dt_h, dpix_lon_km, dpix_lat_km)

    # ── Flip do PySTEPS (row0=north) ────────────────────────────────
    R_raw = np.where(np.isfinite(dbz_stack), dbz_stack, -20.0).astype(np.float32)
    R_ps  = R_raw[:, ::-1, :]

    # ── Nowcasting ──────────────────────────────────────────────────
    try:
        if method == "sproc":
            log.info("Nowcasting: S-PROG (n_cascade_levels=6)")
            R_f_ps = run_sproc(R_ps, V, FORECAST_STEPS)
        else:
            log.info("Nowcasting: semi-Lagrange'a ekstrapolacja")
            R_f_ps = run_extrapolation(R_ps, V, FORECAST_STEPS)
    except Exception as exc:
        log.warning("S-PROG błąd: %s — próbuję prostą ekstrapolację", exc)
        try:
            R_f_ps = run_extrapolation(R_ps, V, FORECAST_STEPS)
        except Exception as exc2:
            log.error("Ekstrapolacja też zawiodła: %s", exc2)
            return None

    # ── Flip z powrotem do naszego układu (row0=south) ──────────────
    R_f = R_f_ps[:, ::-1, :].astype(np.float32)
    R_f[R_f < 1.0] = np.nan
    log.info("Prognoza gotowa: %d klatek, max dBZ=%.1f",
             len(R_f), float(np.nanmax(R_f)) if np.any(np.isfinite(R_f)) else 0)
    return R_f


# ─────────────────────────────────────────────────────────────────────────────
#  Renderowanie i zapis do manifestu
# ─────────────────────────────────────────────────────────────────────────────

def _to_remote(local_path: Path) -> str:
    rel = local_path.relative_to(PROJECT_PATH / "img")
    return (Path(FTP_IMG_DIR) / rel).as_posix()


def render_and_publish(R_f: np.ndarray, cmax: dict,
                       radar_data_template: dict,
                       ftp_session=None, dry_run: bool = False) -> list[dict]:
    """
    Renderuje klatki prognozy, zapisuje PNG/JSON, aktualizuje manifest.
    Zwraca listę nowych wpisów manifestu.
    """
    renderer   = RadarRenderer(palette=RadarPalette(pal_dir=COLOR_TABLES))
    overlay_dir = OVERLAY_DIR / PRODUCT_KEY
    overlay_dir.mkdir(parents=True, exist_ok=True)
    timestamps  = cmax["timestamps"]

    # Usuń stare pliki forecast
    for old in list(overlay_dir.glob("forecast_*.png")) + list(overlay_dir.glob("forecast_*.json")):
        old.unlink(missing_ok=True)
        if ftp_session and not dry_run:
            try:
                ftp_session.delete(_to_remote(old))
            except Exception:
                pass

    gen_str   = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    new_frames = []

    for step in range(len(R_f)):
        lead_min = (step + 1) * FORECAST_STEP_MIN
        lead_h   = lead_min / 60.0
        fc_dbz   = R_f[step]

        valid_dt = datetime.utcfromtimestamp(float(timestamps[-1]) + lead_h * 3600.0)
        ts_str   = valid_dt.strftime("%Y%m%d%H%M%S")
        png_path  = overlay_dir / f"forecast_{gen_str}_{ts_str}.png"
        json_path = overlay_dir / f"forecast_{gen_str}_{ts_str}.json"

        fc_rd = {**radar_data_template,
                 "radar_data": {"dataset1": fc_dbz},
                 "start_date": valid_dt}

        if not dry_run:
            meta = renderer.render_overlay(fc_rd, str(png_path), style="noaa", dpi=80, size=6)
            meta["is_forecast"]     = True
            meta["forecast_lead_h"] = round(lead_h, 3)
            with open(json_path, "w", encoding="utf-8") as jf:
                json.dump(meta, jf, ensure_ascii=False, indent=2)

            image_rel = "../" + str(png_path.relative_to(PROJECT_PATH)).replace("\\", "/")
            new_frames.append({
                "timestamp":       meta["timestamp"],
                "image":           image_rel,
                "bounds":          meta["bounds"],
                "quantity":        meta["quantity"],
                "is_forecast":     True,
                "forecast_lead_h": round(lead_h, 3),
            })

            if ftp_session:
                ftp_session.upload(png_path, _to_remote(png_path))
                ftp_session.upload(json_path, _to_remote(json_path))
        else:
            log.info("[dry-run] %s  lead=+%dmin", png_path.name, lead_min)

    log.info("Wygenerowano %d klatek prognozy (gen=%s)", len(new_frames), gen_str)
    return new_frames


def update_manifest(new_frames: list[dict]) -> None:
    if MANIFEST.exists():
        try:
            with open(MANIFEST, encoding="utf-8") as f:
                manifest = json.load(f)
        except json.JSONDecodeError:
            manifest = {"updated": "", "products": {}}
    else:
        manifest = {"updated": "", "products": {}}

    if PRODUCT_KEY not in manifest.get("products", {}):
        manifest.setdefault("products", {})[PRODUCT_KEY] = {"label": LABEL, "frames": []}

    # Usuń stare klatki prognozy, dodaj nowe
    frames = [f for f in manifest["products"][PRODUCT_KEY]["frames"] if not f.get("is_forecast")]
    frames.extend(new_frames)
    frames.sort(key=lambda f: f["timestamp"])
    manifest["products"][PRODUCT_KEY]["frames"] = frames
    manifest["updated"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")

    tmp = MANIFEST.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    tmp.replace(MANIFEST)
    log.info("Manifest zaktualizowany (%d klatek łącznie)", len(frames))


# ─────────────────────────────────────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────────────────────────────────────

def run(method: str = "sproc", dry_run: bool = False) -> bool:
    # Wczytaj dane
    cmax = load_cmax()
    if cmax is None:
        return False

    # Oblicz prognozę
    R_f = compute_forecast(cmax, method=method)
    if R_f is None:
        return False

    # Buduj radar_data_template (potrzebny rendererowi dla bounds/proj)
    # Odczytujemy z manifestu istniejącą klatkę CMAX jako szablon
    radar_template = _make_radar_template(cmax)

    uploader = FtpUploader() if not dry_run else None
    if uploader and not uploader.is_configured():
        uploader = None

    ctx = uploader.session() if uploader else contextlib.nullcontext()
    with ctx as ftp_session:
        new_frames = render_and_publish(R_f, cmax, radar_template, ftp_session, dry_run)
        if new_frames and not dry_run:
            update_manifest(new_frames)
            if ftp_session:
                ftp_session.upload(MANIFEST, _to_remote(MANIFEST))
                log.info("Manifest wysłany na FTP")

    return True


def _make_radar_template(cmax: dict) -> dict:
    """
    Buduje minimalny słownik radar_data dla renderera.
    Potrzebujemy lat/lon mesh → konwertujemy z naszych środkowych współrzędnych.
    """
    from pyproj import Transformer
    lats = cmax["lats"]
    lons = cmax["lons"]
    H, W = lats.shape

    # Odtwórz siatkę krawędziową (H+1 x W+1) z pikselowych środków
    dlat = lats[1, 0] - lats[0, 0]
    dlon = lons[0, 1] - lons[0, 0]
    lat_e = np.zeros((H+1, W+1), dtype=np.float32)
    lon_e = np.zeros((H+1, W+1), dtype=np.float32)
    for r in range(H+1):
        lat_e[r, :] = (lats[0, 0] - dlat/2) + r * dlat
    for c in range(W+1):
        lon_e[:, c] = (lons[0, 0] - dlon/2) + c * dlon

    t = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    x_mesh, y_mesh = t.transform(lon_e, lat_e)

    return {
        "lon_mesh":   x_mesh,
        "lat_mesh":   y_mesh,
        "radar_data": {"dataset1": cmax["dbz"][-1]},
        "start_date": datetime.utcfromtimestamp(float(cmax["timestamps"][-1])),
        "projection": "EPSG:3857",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Prognoza CMAX z PySTEPS")
    parser.add_argument("--method", choices=["sproc", "extrap"], default="sproc",
                        help="Metoda nowcastingu: sproc (S-PROG) lub extrap (semi-Lagrange'a)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Nie zapisuj plików ani manifestu — tylko wypisz logi")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    setup_logging(LOG_DIR, level=args.log_level.upper())

    log.info("═══ cmax_forecast_pysteps start (%s UTC) ═══",
             datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))
    log.info("Metoda: %s%s", args.method, " [dry-run]" if args.dry_run else "")

    ok = run(method=args.method, dry_run=args.dry_run)
    sys.exit(0 if ok else 1)
