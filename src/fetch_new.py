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
    from imgw.client import ImgwClient
    from radar.decoder import RadarDecoder
    from radar.renderer import RadarRenderer
    from radar.palette import RadarPalette
    from grs.decoder import GrsDecoder
    from transfer.ftp import FtpUploader
    from log_setup import setup_logging
except ImportError:
    from src.imgw.client import ImgwClient
    from src.radar.decoder import RadarDecoder
    from src.radar.renderer import RadarRenderer
    from src.radar.palette import RadarPalette
    from src.grs.decoder import GrsDecoder
    from src.transfer.ftp import FtpUploader
    from src.log_setup import setup_logging

from dotenv import load_dotenv
load_dotenv(override=True)

PROJECT_PATH = Path(os.getenv("PROJECT_PATH", Path(__file__).parent.parent))

CONFIG_FILE   = PROJECT_PATH / "config" / "radar_config.json"
PALETTES_FILE = PROJECT_PATH / "config" / "palettes.json"
DATA_DIR      = PROJECT_PATH / "data" / "polrad"
GRS_DATA_DIR  = PROJECT_PATH / "data" / "grs"
OVERLAY_DIR   = PROJECT_PATH / "img" / "polrad" / "overlay"
GRS_OVERLAY_DIR = PROJECT_PATH / "img" / "polrad" / "overlay" / "GRS"
MANIFEST      = PROJECT_PATH / "img" / "polrad" / "manifest.json"
LOG_DIR       = PROJECT_PATH / "logs"
COLOR_TABLES  = PROJECT_PATH / "data" / "color_tables"

FTP_IMG_DIR  = os.getenv("FTP_REMOTE_IMG_DIR", "img")

IMGW_PATH_BASE = "/Oper/Polrad/Produkty/HVD"
GRS_IMGW_PATH  = "/Oper/Nowcasting/rain_grs/grs_60_asc"
GRS_PRODUCT_KEY = "GRS"
GRS_LABEL       = "GRS – Suma opadów 60 min"

_imgw_client    = ImgwClient()
_radar_decoder  = RadarDecoder()
_grs_decoder    = GrsDecoder()
_radar_renderer = RadarRenderer(palette=RadarPalette(pal_dir=COLOR_TABLES))

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

    df = _imgw_client.get_file_list(path)
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

                if not _imgw_client.download_file(row["url"], str(h5_path)):
                    log.error("[%s] Nie udało się pobrać: %s", product_key, row["filename"])
                    continue

                try:
                    radar_data = _radar_decoder.decode(str(h5_path), projection="EPSG:3857")
                except Exception as e:
                    log.error("[%s] Błąd dekodowania HDF5 (%s): %s",
                              product_key, row["filename"], e)
                    h5_path.unlink(missing_ok=True)
                    continue

                ts_str    = radar_data["start_date"].strftime("%Y%m%d%H%M%S")
                png_path  = overlay_dir / f"{ts_str}.png"
                json_path = overlay_dir / f"{ts_str}.json"

                frame_meta = _radar_renderer.render_overlay(radar_data, str(png_path), style="noaa")
                with open(json_path, "w", encoding="utf-8") as _jf:
                    json.dump(frame_meta, _jf, ensure_ascii=False, indent=2)
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
#  Przetwarzanie danych GRS (pliki .asc)
# ─────────────────────────────────────────────────────────

def process_grs_path(cfg, manifest, manifest_lock, ftp_uploader=None, max_new=None):
    """
    Analogicznie do process_path, ale dla plików ASC z danymi GRS (sumy opadów).

    Ścieżka IMGW: /Oper/Nowcasting/rain_grs/grs_60_asc
    Format pliku: YYYYMMDDHHMM_acc0060_grs.asc

    Zwraca (liczba nowych overlayów, lista ścieżek FTP do usunięcia).
    """
    import re as _re

    history_minutes = cfg.get("history_minutes", 60)
    cutoff     = datetime.utcnow() - timedelta(minutes=history_minutes)
    cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%S")

    log.info("[%s] Sprawdzam: %s", GRS_PRODUCT_KEY, GRS_IMGW_PATH)

    df = _imgw_client.get_file_list(GRS_IMGW_PATH)
    if df is None or df.empty:
        log.info("[%s] Brak plików na serwerze.", GRS_PRODUCT_KEY)
        return 0, []

    # Filtruj tylko pliki .asc i parsuj timestamp z nazwy pliku
    df = df[df["filename"].str.endswith(".asc")].copy()
    if df.empty:
        log.info("[%s] Brak plików .asc.", GRS_PRODUCT_KEY)
        return 0, []

    def _parse_grs_ts(filename):
        m = _re.match(r"(\d{12})", filename)
        return datetime.strptime(m.group(1), "%Y%m%d%H%M") if m else None

    df["timestamp"] = df["filename"].apply(_parse_grs_ts)
    df = df[df["timestamp"].notna()].sort_values("timestamp")
    log.debug("[%s] Łącznie plików .asc na serwerze: %d", GRS_PRODUCT_KEY, len(df))

    GRS_OVERLAY_DIR.mkdir(parents=True, exist_ok=True)
    GRS_DATA_DIR.mkdir(parents=True, exist_ok=True)

    total         = 0
    ftp_to_delete = []

    # ── 1. Lokalne kasowanie starych plików + rejestracja do usunięcia z FTP ──
    deleted_count = 0
    for png_file in sorted(GRS_OVERLAY_DIR.glob("*.png")):
        try:
            png_ts = datetime.strptime(png_file.stem, "%Y%m%d%H%M%S")
        except ValueError:
            continue
        if png_ts < cutoff:
            log.debug("[%s] Lokalnie usuwam: %s", GRS_PRODUCT_KEY, png_file.name)
            png_file.unlink()
            json_file = GRS_OVERLAY_DIR / f"{png_file.stem}.json"
            json_file.unlink(missing_ok=True)
            if ftp_uploader:
                ftp_to_delete.append(_to_remote(png_file))
                ftp_to_delete.append(_to_remote(json_file))
            deleted_count += 1

    if deleted_count:
        log.info("[%s] Lokalnie usunięto %d starych obrazów (poza oknem %d min)",
                 GRS_PRODUCT_KEY, deleted_count, history_minutes)

    with manifest_lock:
        manifest_remove_before(manifest, GRS_PRODUCT_KEY, cutoff_iso)

    # ── 2. Ustal które pliki z okna wymagają wygenerowania ───────────────────
    existing_stems = {f.stem for f in GRS_OVERLAY_DIR.glob("*.png")}
    df_window = df[df["timestamp"] >= cutoff]
    df_new = df_window[~df_window["timestamp"].apply(
        lambda t: t.strftime("%Y%m%d%H%M%S")
    ).isin(existing_stems)].sort_values("timestamp", ascending=False)

    if max_new is not None:
        df_new = df_new.head(max_new)

    log.info("[%s] Okno %d min: %d dostępnych, %d już istnieje, %d do wygenerowania%s",
             GRS_PRODUCT_KEY, history_minutes,
             len(df_window), len(existing_stems), len(df_new),
             f" (limit {max_new})" if max_new else "")

    if df_new.empty:
        return total, ftp_to_delete

    # ── 3. Pobierz, zdekoduj, wygeneruj overlay ───────────────────────────────
    ctx = ftp_uploader.session() if ftp_uploader else contextlib.nullcontext()
    with ctx as ftp_session:
        for _, row in df_new.iterrows():
            asc_path = GRS_DATA_DIR / row["filename"]
            log.info("[%s] Pobieram: %s", GRS_PRODUCT_KEY, row["filename"])

            if not _imgw_client.download_file(row["url"], str(asc_path)):
                log.error("[%s] Nie udało się pobrać: %s", GRS_PRODUCT_KEY, row["filename"])
                continue

            try:
                grs_data = _grs_decoder.decode(str(asc_path), projection="EPSG:3857")
            except Exception as e:
                log.error("[%s] Błąd dekodowania ASC (%s): %s",
                          GRS_PRODUCT_KEY, row["filename"], e)
                asc_path.unlink(missing_ok=True)
                continue

            ts_str    = grs_data["start_date"].strftime("%Y%m%d%H%M%S")
            png_path  = GRS_OVERLAY_DIR / f"{ts_str}.png"
            json_path = GRS_OVERLAY_DIR / f"{ts_str}.json"

            frame_meta = _radar_renderer.render_overlay(grs_data, str(png_path), style="imgw")
            with open(json_path, "w", encoding="utf-8") as _jf:
                json.dump(frame_meta, _jf, ensure_ascii=False, indent=2)
            log.info("[%s] Wygenerowano: %s.png", GRS_PRODUCT_KEY, ts_str)

            image_rel = "../" + str(png_path.relative_to(PROJECT_PATH)).replace("\\", "/")
            with manifest_lock:
                manifest_add_frame(manifest, GRS_PRODUCT_KEY, GRS_LABEL, frame_meta, image_rel)

            if ftp_session:
                ftp_session.upload(png_path, _to_remote(png_path))
                ftp_session.upload(json_path, _to_remote(json_path))

            asc_path.unlink(missing_ok=True)
            total += 1

    log.info("[%s] Gotowe: +%d nowych overlayów.", GRS_PRODUCT_KEY, total)
    return total, ftp_to_delete


# ─────────────────────────────────────────────────────────
#  CLI
# ─────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Pobierz nowe dane radarowe i GRS z IMGW")
    grp = parser.add_mutually_exclusive_group()
    grp.add_argument("--radar",  help="Kod radaru (np. pas)")
    grp.add_argument("--compo",  help="Produkt kompozytowy (np. CMAX_250.comp.cmax)")
    grp.add_argument("--grs",    action="store_true",
                     help="Tylko dane GRS (sumy opadów ASC)")
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

    # Wybór produktów radarowych i flaga GRS
    if args.radar:
        if not args.product:
            parser.error("--radar wymaga --product")
        selected      = [p for p in all_paths
                         if p["key_prefix"] == f"{args.radar.upper()}_{args.product}"]
        process_grs   = False
    elif args.compo:
        short         = args.compo.split(".")[0]
        selected      = [p for p in all_paths if p["key_prefix"] == f"COMPO_{short}"]
        process_grs   = False
    elif args.grs:
        selected      = []
        process_grs   = True
    else:
        selected      = all_paths
        process_grs   = True

    if not selected and not process_grs:
        log.error("Nie znaleziono pasującej ścieżki w konfiguracji.")
        return 1

    log.info("Produktów radarowych do sprawdzenia: %d  |  GRS: %s",
             len(selected), "tak" if process_grs else "nie")

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

    n_tasks     = len(selected) + (1 if process_grs else 0)
    max_workers = min(cfg.get("workers", os.cpu_count() or 4), max(n_tasks, 1))
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
                futures[f] = pi["key_prefix"]
            if process_grs:
                f = executor.submit(
                    process_grs_path, cfg, manifest, manifest_lock, uploader, max_new
                )
                futures[f] = GRS_PRODUCT_KEY
            for f in as_completed(futures):
                key = futures[f]
                try:
                    n, deletes = f.result()
                    total         += n
                    ftp_to_delete += deletes
                except Exception as e:
                    log.error("Błąd [%s]: %s", key, e, exc_info=True)

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
