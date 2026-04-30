"""Zarzadzanie manifestem img/polrad/manifest.json."""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


class ManifestManager:
    """Laduje, aktualizuje i zapisuje manifest produktow radarowych."""

    def __init__(self, manifest_path: str | Path):
        self._path = Path(manifest_path)
        self._data: dict = {"updated": "", "products": {}}

    def load(self) -> "ManifestManager":
        if self._path.exists():
            with open(self._path, encoding="utf-8") as f:
                self._data = json.load(f)
        else:
            self._data = {"updated": "", "products": {}}
        return self

    def save(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._data["updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
        log.debug("Manifest zapisany: %s", self._path)

    def add_frame(self, product_key: str, label: str, frame_meta: dict, image_rel_path: str):
        """Dodaje ramke do produktu; pomija duplikaty (ten sam timestamp)."""
        if product_key not in self._data["products"]:
            self._data["products"][product_key] = {"label": label, "frames": []}

        frames = self._data["products"][product_key]["frames"]
        existing_ts = {fr["timestamp"] for fr in frames}
        if frame_meta["timestamp"] not in existing_ts:
            frames.append({
                "timestamp": frame_meta["timestamp"],
                "image":     image_rel_path,
                "bounds":    frame_meta["bounds"],
            })
            frames.sort(key=lambda fr: fr["timestamp"])

    def remove_frames_before(self, product_key: str, cutoff_iso: str):
        """Usuwa ramki starsze niz cutoff_iso (format YYYY-MM-DDTHH:MM:SS)."""
        if product_key not in self._data["products"]:
            return
        frames = self._data["products"][product_key]["frames"]
        before = len(frames)
        self._data["products"][product_key]["frames"] = [
            fr for fr in frames if fr["timestamp"] >= cutoff_iso
        ]
        removed = before - len(self._data["products"][product_key]["frames"])
        if removed:
            log.debug("Manifest: usunieto %d starych wpisow dla %s", removed, product_key)
