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
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
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

CONFIG_FILE   = PROJECT_PATH / "config" / "radar_config.json"
PALETTES_FILE = PROJECT_PATH / "config" / "palettes.json"
DATA_DIR      = PROJECT_PATH / "data" / "polrad"
OVERLAY_DIR   = PROJECT_PATH / "img" / "polrad" / "overlay"
MANIFEST      = PROJECT_PATH / "img" / "polrad" / "manifest.json"
LOG_DIR       = PROJECT_PATH / "logs"

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
                "label_base": f"{_station_name(cfg, radar)} – {product}",
            })
    for compo in cfg["compo_products"]:
        short = compo.split(".")[0]
        paths.append({
            "path":       f"{IMGW_PATH_BASE}/HVD_COMPO_{compo}",
            "key_prefix": f"COMPO_{short}",
            "label_base": compo,
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
            "quantity":  frame_meta["quantity"],
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

def process_path(path_info, cfg, manifest, manifest_lock, ftp_uploader=None, max_new=None):
    """
    Dla jednej ścieżki IMGW:
      - usuwa lokalne pliki poza oknem historii,
      - generuje i uploaduje nowe obrazy (własna sesja FTP per-wątek),
      - zwraca (liczba nowych overlayów, lista ścieżek FTP do usunięcia).

    Kasowanie ze zdalnego FTP jest celowo odroczone do main() — najpierw nowe
    pliki muszą trafić na serwer i manifest musi być zaktualizowany, żeby
    użytkownicy nigdy nie widzieli luki (404 na plik z manifestu).
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
        return 0, []

    df = df[df["filename"].str.endswith(".h5")].copy()
    if df.empty:
        log.info("[%s] Brak plików .h5.", key_prefix)
        return 0, []

    df = df.sort_values("timestamp")
    log.debug("[%s] Łącznie plików .h5 na serwerze: %d", key_prefix, len(df))

    units = df["unit"].dropna().unique().tolist()
    if not units:
        units = [None]

    total          = 0
    ftp_to_delete  = []   # zdalny cleanup odroczony — wykonuje main() po uploadzię manifestu

    ctx = ftp_uploader.session() if ftp_uploader else contextlib.nullcontext()
    with ctx as ftp_session:
        for unit in units:
            state_key   = f"{key_prefix}__{unit}" if unit else key_prefix
            product_key = state_key
            unit_label  = cfg["unit_labels"].get(unit, unit) if unit else ""
            label       = f"{label_base} – {unit_label}" if unit_label else label_base

            df_unit     = df[df["unit"] == unit] if unit else df
            overlay_dir = OVERLAY_DIR / product_key
            overlay_dir.mkdir(parents=True, exist_ok=True)

            # ── 1. Lokalne kasowanie + zebranie ścieżek FTP do późniejszego usunięcia ──
            deleted_count = 0
            for png_file in sorted(overlay_dir.glob("*.png")):
                try:
                    png_ts = datetime.strptime(png_file.stem, "%Y%m%d%H%M%S")
                except ValueError:
                    continue
                if png_ts < cutoff:
                    log.debug("[%s] Lokalnie usuwam: %s", product_key, png_file.name)
                    png_file.unlink()
                    json_file = overlay_dir / f"{png_file.stem}.json"
                    json_file.unlink(missing_ok=True)
                    # Rejestruj do usunięcia z FTP — nie kasujemy teraz
                    if ftp_uploader:
                        ftp_to_delete.append(_to_remote(png_file))
                        ftp_to_delete.append(_to_remote(json_file))
                    deleted_count += 1

            if deleted_count:
                log.info("[%s] Lokalnie usunięto %d starych obrazów (poza oknem %d min)",
                         product_key, deleted_count, history_minutes)

            with manifest_lock:
                manifest_remove_before(manifest, product_key, cutoff_iso)

            # ── 2. Ustal które pliki z okna wymagają wygenerowania ───────────────
            existing_stems = {f.stem for f in overlay_dir.glob("*.png")}
            df_window = df_unit[
                df_unit["timestamp"].notna() & (df_unit["timestamp"] >= cutoff)
            ]
            df_new = df_window[~df_window["timestamp"].apply(
                lambda t: t.strftime("%Y%m%d%H%M%S")
            ).isin(existing_stems)].sort_values("timestamp", ascending=False)

            if max_new is not None:
                df_new = df_new.head(max_new)

            log.info("[%s] Okno %d min: %d dostępnych, %d już istnieje, %d do wygenerowania%s",
                     product_key, history_minutes,
                     len(df_window), len(existing_stems), len(df_new),
                     f" (limit {max_new})" if max_new else "")

            if df_new.empty:
                continue

            # ── 3. Pobierz, wygeneruj i uploaduj nowe obrazy ─────────────────────
            product_data_dir = DATA_DIR / key_prefix.replace(":", "_")
            product_data_dir.mkdir(parents=True, exist_ok=True)

            for _, row in df_new.iterrows():
                h5_path = product_data_dir / row["filename"]
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
                with manifest_lock:
                    manifest_add_frame(manifest, product_key, label, frame_meta, image_rel)

                if ftp_session:
                    ftp_session.upload(png_path, _to_remote(png_path))
                    ftp_session.upload(json_path, _to_remote(json_path))

                h5_path.unlink(missing_ok=True)
                total += 1

    log.info("[%s] Gotowe: +%d nowych overlayów.", key_prefix, total)
    return total, ftp_to_delete


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
    else:
        log.warning("FTP niekonfigurowany (brak FTP_HOST/FTP_USER w .env) — transfer pominięty")
        uploader = None

    # ── Przetwarzanie dwufazowe ──────────────────────────────────────────────
    # Faza 1: tylko najnowsza klatka każdego produktu → szybki update strony
    # Faza 2: pozostałe (backfill) → wypełnienie historii
    manifest      = load_manifest()
    manifest_lock = threading.Lock()

    max_workers = min(cfg.get("workers", os.cpu_count() or 4), len(selected))
    log.info("Wątki: %d", max_workers)

    total         = 0
    ftp_to_delete = []

    def _run_pass(pass_label, max_new=None):
        nonlocal total, ftp_to_delete
        log.info("── %s ──", pass_label)
        futures = {}
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            for pi in selected:
                f = executor.submit(
                    process_path, pi, cfg, manifest, manifest_lock, uploader, max_new
                )
                futures[f] = pi
            for f in as_completed(futures):
                pi = futures[f]
                try:
                    n, deletes = f.result()
                    total         += n
                    ftp_to_delete += deletes
                except Exception as e:
                    log.error("Błąd [%s]: %s", pi["key_prefix"], e, exc_info=True)

    _run_pass("Faza 1 — najnowsza klatka", max_new=1)

    # Wyślij manifest po fazie 1 — strona natychmiast pokazuje świeże dane
    save_manifest(manifest)
    if uploader:
        with uploader.session() as sess:
            log.info("Uploading manifest.json (faza 1)")
            sess.upload(MANIFEST, _to_remote(MANIFEST))

    _run_pass("Faza 2 — backfill historii")

    # ── Prawidłowa kolejność FTP: nowe pliki są już na serwerze ─────────────
    # 1. Zapisz i wyślij manifest — od tej chwili klienci widzą nowy stan
    # 2. Dopiero teraz kasuj stare pliki — manifest już na nie nie wskazuje
    save_manifest(manifest)
    if uploader:
        with uploader.session() as sess:
            log.info("Uploading manifest.json (faza 2)")
            sess.upload(MANIFEST, _to_remote(MANIFEST))
            if PALETTES_FILE.exists():
                remote_palettes = str(Path(FTP_IMG_DIR).parent / "config" / "palettes.json")
                log.info("Uploading palettes.json na FTP → %s", remote_palettes)
                sess.upload(PALETTES_FILE, remote_palettes)
            if ftp_to_delete:
                log.info("Kasowanie %d starych plików z FTP", len(ftp_to_delete))
                for remote_path in ftp_to_delete:
                    sess.delete(remote_path)

    log.info("═══ fetch_new koniec — łącznie wygenerowanych: %d ═══", total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
