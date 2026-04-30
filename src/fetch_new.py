"""
fetch_new.py — pobiera nowe pliki radarowe z IMGW i generuje overlayi PNG do Leaflet.

Konfiguracja produktów jest w config/radar_config.json (pole history_minutes).

Użycie:
  python fetch_new.py                     # wszystkie produkty z config
  python fetch_new.py --compo CMAX_250.comp.cmax
  python fetch_new.py --radar pas --product 250.max
  python fetch_new.py --log-level DEBUG
"""

import argparse
import contextlib
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

try:
    from utils import get_list_of_files, download_file
    from polrad import decode_h5_file, render_web_overlay, save_overlay_metadata
    from transfer.ftp import FtpUploader
    from log_setup import setup_logging
except ImportError:
    from src.utils import get_list_of_files, download_file
    from src.polrad import decode_h5_file, render_web_overlay, save_overlay_metadata
    from src.transfer.ftp import FtpUploader
    from src.log_setup import setup_logging

from dotenv import load_dotenv
load_dotenv(override=True)

PROJECT_PATH = Path(os.getenv("PROJECT_PATH", Path(__file__).parent.parent))

CONFIG_FILE  = PROJECT_PATH / "config" / "radar_config.json"
DATA_DIR     = PROJECT_PATH / "data" / "polrad"
OVERLAY_DIR  = PROJECT_PATH / "img" / "polrad" / "overlay"
MANIFEST     = PROJECT_PATH / "img" / "polrad" / "manifest.json"
LOG_DIR      = PROJECT_PATH / "logs"

FTP_IMG_DIR  = os.getenv("FTP_REMOTE_IMG_DIR", "img")

IMGW_PATH_BASE = "/Oper/Polrad/Produkty/HVD"

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────
#  Konfiguracja
# ─────────────────────────────────────────────────────────

def load_config():
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)


def all_imgw_paths(cfg):
    paths = []
    for radar in cfg["radars"]:
        for product in cfg["radar_products"]:
            paths.append({
                "path":       f"{IMGW_PATH_BASE}/HVD_{radar}_{product}",
                "key_prefix": f"{radar.upper()}_{product}",
                "label_base": f"{_station_name(cfg, radar)} – {cfg['product_labels'].get(product, product)}",
            })
    for compo in cfg["compo_products"]:
        short = compo.split(".")[0]
        paths.append({
            "path":       f"{IMGW_PATH_BASE}/HVD_COMPO_{compo}",
            "key_prefix": f"COMPO_{short}",
            "label_base": cfg["product_labels"].get(compo, compo),
        })
    return paths


def _station_name(cfg, radar_id):
    for st in cfg.get("radar_stations", []):
        if st["id"] == radar_id:
            return st["name"]
    return radar_id.upper()


# ─────────────────────────────────────────────────────────
#  Manifest
# ─────────────────────────────────────────────────────────

def load_manifest():
    if MANIFEST.exists():
        with open(MANIFEST, encoding="utf-8") as f:
            return json.load(f)
    return {"updated": "", "products": {}}


def save_manifest(manifest):
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    manifest["updated"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    log.debug("Manifest zapisany: %s", MANIFEST)


def manifest_add_frame(manifest, product_key, label, frame_meta, image_rel_path):
    if product_key not in manifest["products"]:
        manifest["products"][product_key] = {"label": label, "frames": []}
    frames = manifest["products"][product_key]["frames"]
    existing_ts = {fr["timestamp"] for fr in frames}
    if frame_meta["timestamp"] not in existing_ts:
        frames.append({
            "timestamp": frame_meta["timestamp"],
            "image":     image_rel_path,
            "bounds":    frame_meta["bounds"],
        })
        frames.sort(key=lambda fr: fr["timestamp"])


def manifest_remove_before(manifest, product_key, cutoff_iso):
    if product_key not in manifest["products"]:
        return
    frames = manifest["products"][product_key]["frames"]
    before = len(frames)
    manifest["products"][product_key]["frames"] = [
        fr for fr in frames if fr["timestamp"] >= cutoff_iso
    ]
    removed = before - len(manifest["products"][product_key]["frames"])
    if removed:
        log.debug("Manifest: usunięto %d starych wpisów dla %s", removed, product_key)


# ─────────────────────────────────────────────────────────
#  FTP — przeliczanie ścieżek
# ─────────────────────────────────────────────────────────

def _to_remote(local_path: Path) -> str:
    rel = local_path.relative_to(PROJECT_PATH / "img")
    return (Path(FTP_IMG_DIR) / rel).as_posix()


# ─────────────────────────────────────────────────────────
#  Przetwarzanie jednej ścieżki IMGW
# ─────────────────────────────────────────────────────────

def process_path(path_info, cfg, ftp_session=None):
    """
    Dla jednej ścieżki IMGW:
      - usuwa lokalne obrazy starsze niż history_minutes (+ zdalne na FTP),
      - generuje obrazy dla plików w oknie, których PNG jeszcze nie ma,
      - uploaduje nowe pliki na FTP.
    Zwraca liczbę nowo wygenerowanych overlayów.
    """
    history_minutes = cfg.get("history_minutes", 60)
    cutoff     = datetime.utcnow() - timedelta(minutes=history_minutes)
    cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%S")

    path       = path_info["path"]
    key_prefix = path_info["key_prefix"]
    label_base = path_info["label_base"]

    log.info("[%s] Sprawdzam: %s", key_prefix, path)

    df = get_list_of_files(path)
    if df is None or df.empty:
        log.info("[%s] Brak plików na serwerze.", key_prefix)
        return 0

    df = df[df["filename"].str.endswith(".h5")].copy()
    if df.empty:
        log.info("[%s] Brak plików .h5.", key_prefix)
        return 0

    df = df.sort_values("timestamp")
    log.debug("[%s] Łącznie plików .h5 na serwerze: %d", key_prefix, len(df))

    units = df["unit"].dropna().unique().tolist()
    if not units:
        units = [None]

    manifest = load_manifest()
    total    = 0
    manifest_changed = False

    for unit in units:
        state_key   = f"{key_prefix}__{unit}" if unit else key_prefix
        product_key = state_key
        unit_label  = cfg["unit_labels"].get(unit, unit) if unit else ""
        label       = f"{label_base} – {unit_label}" if unit_label else label_base

        df_unit     = df[df["unit"] == unit] if unit else df
        overlay_dir = OVERLAY_DIR / product_key
        overlay_dir.mkdir(parents=True, exist_ok=True)

        # ── 1. Usuń obrazy poza oknem historii ───────────────────────────────
        deleted_count = 0
        for png_file in sorted(overlay_dir.glob("*.png")):
            try:
                png_ts = datetime.strptime(png_file.stem, "%Y%m%d%H%M%S")
            except ValueError:
                continue
            if png_ts < cutoff:
                log.info("[%s] Usuwam stary obraz: %s", product_key, png_file.name)
                png_file.unlink()
                json_file = overlay_dir / f"{png_file.stem}.json"
                json_file.unlink(missing_ok=True)
                if ftp_session:
                    ftp_session.delete(_to_remote(png_file))
                    ftp_session.delete(_to_remote(json_file))
                deleted_count += 1
                manifest_changed = True

        if deleted_count:
            log.info("[%s] Usunięto %d starych obrazów (poza oknem %d min)",
                     product_key, deleted_count, history_minutes)

        manifest_remove_before(manifest, product_key, cutoff_iso)

        # ── 2. Ustal które pliki z okna wymagają wygenerowania ───────────────
        existing_stems = {f.stem for f in overlay_dir.glob("*.png")}
        df_window = df_unit[
            df_unit["timestamp"].notna() & (df_unit["timestamp"] >= cutoff)
        ]
        df_new = df_window[~df_window["timestamp"].apply(
            lambda t: t.strftime("%Y%m%d%H%M%S")
        ).isin(existing_stems)]

        log.info("[%s] Okno %d min: %d dostępnych, %d już istnieje, %d do wygenerowania",
                 product_key, history_minutes,
                 len(df_window), len(existing_stems), len(df_new))

        if df_new.empty:
            continue

        # ── 3. Pobierz i wygeneruj nowe obrazy ───────────────────────────────
        DATA_DIR.mkdir(parents=True, exist_ok=True)

        for _, row in df_new.iterrows():
            h5_path = DATA_DIR / row["filename"]
            log.info("[%s] Pobieram: %s", product_key, row["filename"])

            if not download_file(row["url"], str(h5_path)):
                log.error("[%s] Nie udało się pobrać: %s", product_key, row["filename"])
                continue

            try:
                radar_data = decode_h5_file(str(h5_path), output_projection="EPSG:3857")
            except Exception as e:
                log.error("[%s] Błąd dekodowania HDF5 (%s): %s",
                          product_key, row["filename"], e)
                h5_path.unlink(missing_ok=True)
                continue

            ts_str    = radar_data["start_date"].strftime("%Y%m%d%H%M%S")
            png_path  = overlay_dir / f"{ts_str}.png"
            json_path = overlay_dir / f"{ts_str}.json"

            frame_meta = render_web_overlay(radar_data, str(png_path))
            save_overlay_metadata(frame_meta, str(json_path))
            log.info("[%s] Wygenerowano: %s.png", product_key, ts_str)

            image_rel = "../" + str(png_path.relative_to(PROJECT_PATH)).replace("\\", "/")
            manifest_add_frame(manifest, product_key, label, frame_meta, image_rel)

            if ftp_session:
                ftp_session.upload(png_path, _to_remote(png_path))
                ftp_session.upload(json_path, _to_remote(json_path))

            h5_path.unlink(missing_ok=True)
            total += 1
            manifest_changed = True

    if manifest_changed:
        save_manifest(manifest)
        if ftp_session:
            log.info("[%s] Uploading manifest.json na FTP", key_prefix)
            ftp_session.upload(MANIFEST, _to_remote(MANIFEST))

    log.info("[%s] Gotowe: +%d nowych overlayów.", key_prefix, total)
    return total


# ─────────────────────────────────────────────────────────
#  CLI
# ─────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Pobierz nowe dane radarowe z IMGW")
    grp = parser.add_mutually_exclusive_group()
    grp.add_argument("--radar",  help="Kod radaru (np. pas)")
    grp.add_argument("--compo",  help="Produkt kompozytowy (np. CMAX_250.comp.cmax)")
    parser.add_argument("--product",   help="Produkt radaru (np. 250.max) — z --radar")
    parser.add_argument("--limit",     type=int, default=1,
                        help="(ignorowany — zachowany dla kompatybilności)")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="Poziom logowania (domyślnie: INFO)")
    args = parser.parse_args()

    setup_logging(LOG_DIR, level=args.log_level)

    log.info("═══ fetch_new start (%s UTC) ═══", datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))
    log.info("PROJECT_PATH: %s", PROJECT_PATH)

    cfg = load_config()
    history_minutes = cfg.get("history_minutes", 60)
    log.info("Okno historii: %d minut", history_minutes)

    all_paths = all_imgw_paths(cfg)

    if args.radar:
        if not args.product:
            parser.error("--radar wymaga --product")
        selected = [p for p in all_paths
                    if p["key_prefix"] == f"{args.radar.upper()}_{args.product}"]
    elif args.compo:
        short    = args.compo.split(".")[0]
        selected = [p for p in all_paths if p["key_prefix"] == f"COMPO_{short}"]
    else:
        selected = all_paths

    if not selected:
        log.error("Nie znaleziono pasującej ścieżki w konfiguracji.")
        return 1

    log.info("Produktów do sprawdzenia: %d", len(selected))

    # ── Konfiguracja FTP ─────────────────────────────────────────────────────
    uploader = FtpUploader()
    if uploader.is_configured():
        log.info("FTP aktywny: %s:%s%s  folder zdalny: %s/",
                 uploader.host, uploader.port,
                 " (FTPS)" if uploader.tls else "",
                 FTP_IMG_DIR)
        ctx = uploader.session()
    else:
        log.warning("FTP niekonfigurowany (brak FTP_HOST/FTP_USER w .env) — transfer pominięty")
        ctx = contextlib.nullcontext()

    # ── Przetwarzanie ────────────────────────────────────────────────────────
    total = 0
    with ctx as ftp_session:
        for path_info in selected:
            total += process_path(path_info, cfg, ftp_session=ftp_session)

    log.info("═══ fetch_new koniec — łącznie wygenerowanych: %d ═══", total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
