#!/usr/bin/env python3
"""Jednorazowy upload config/palettes.json na serwer FTP."""
import os, sys
from pathlib import Path

PROJECT_PATH = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_PATH / "src"))

from dotenv import load_dotenv
load_dotenv(PROJECT_PATH / ".env", override=True)

try:
    from transfer.ftp import FtpUploader
except ImportError:
    from src.transfer.ftp import FtpUploader

PALETTES_FILE = PROJECT_PATH / "config" / "palettes.json"
FTP_IMG_DIR   = os.getenv("FTP_REMOTE_IMG_DIR", "img")
REMOTE_PATH   = str(Path(FTP_IMG_DIR).parent / "config" / "palettes.json")

if not PALETTES_FILE.exists():
    print("ERROR: config/palettes.json nie istnieje — uruchom najpierw sh/export_palettes.py")
    sys.exit(1)

uploader = FtpUploader()
if not uploader.is_configured():
    print("ERROR: FTP niekonfigurowany (brak FTP_HOST/FTP_USER w .env)")
    sys.exit(1)

print(f"Upload {PALETTES_FILE} → {REMOTE_PATH}")
with uploader.session() as sess:
    sess.upload(PALETTES_FILE, REMOTE_PATH)
print("Gotowe.")
