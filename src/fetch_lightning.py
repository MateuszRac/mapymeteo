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

PROJECT_PATH    = Path(os.getenv("PROJECT_PATH", str(ROOT)))
MTG_DATA_DIR    = PROJECT_PATH / "data" / "mtg"
OUTPUT_JSON     = PROJECT_PATH / "img" / "polrad" / "lightning.json"
LOG_DIR         = PROJECT_PATH / "logs"

COLLECTION_ID   = "EO:EUM:DAT:0782"
LON_MIN, LON_MAX = 10.0, 30.0
LAT_MIN, LAT_MAX = 40.0, 60.0
SLOT_MINUTES    = 10
FTP_REMOTE_DIR  = os.getenv("FTP_REMOTE_IMG_DIR", "img")

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

def main(history_hours: int = 3) -> int:
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


def _save(slots: dict, now: datetime) -> None:
    data = {
        "generated": _to_naive_utc(now).strftime("%Y-%m-%dT%H:%M:%S"),
        "slots": slots,
    }
    OUTPUT_JSON.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log.info("Zapisano: %s (%d slotów)", OUTPUT_JSON, len(slots))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Pobierz dane wyładowań MTG-LI")
    parser.add_argument("--log-level", default="INFO", metavar="LEVEL")
    parser.add_argument("--history-hours", type=int, default=3, metavar="H")
    args = parser.parse_args()

    setup_logging(LOG_DIR, level=args.log_level.upper())
    sys.exit(main(history_hours=args.history_hours))
