#!/usr/bin/env python3
"""Eksportuje palety kolorów do config/palettes.json.

Styl palety pochodzi z PALETTE_STYLE w .env (domyślnie: noaa).
  noaa   — pliki .pal z data/color_tables/ (fallback: imgw)
  imgw   — palety IMGW
  nexrad — palety NEXRAD
"""
import json
import os
import sys
from pathlib import Path

PROJECT_PATH = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_PATH / "src"))

from dotenv import load_dotenv
load_dotenv(PROJECT_PATH / ".env", override=True)

from generate_palettes import generate, COLOR_TABLES, OUTPUT
from transfer.ftp import FtpUploader

if __name__ == "__main__":
    style = os.getenv("PALETTE_STYLE", "noaa")
    print(f"Generowanie palettes.json z: {COLOR_TABLES} (styl: {style})")
    data = generate(style=style)
    OUTPUT.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Zapisano: {OUTPUT}")

    ftp_img_dir = os.getenv("FTP_REMOTE_IMG_DIR", "img")
    remote_path = str(Path(ftp_img_dir).parent / "config" / "palettes.json")
    uploader = FtpUploader()
    if not uploader.is_configured():
        print("WARN: FTP niekonfigurowany — pominięto upload.")
    else:
        print(f"Upload -> {remote_path}")
        with uploader.session() as sess:
            sess.upload(OUTPUT, remote_path)
        print("Gotowe.")
