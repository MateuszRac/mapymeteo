"""Orkiestrator pipeline: pobieranie, dekodowanie i renderowanie danych radarowych."""

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

from ..radar.decoder import RadarDecoder
from ..radar.renderer import RadarRenderer
from ..imgw.client import ImgwClient
from .manifest import ManifestManager

log = logging.getLogger(__name__)


class RadarPipeline:
    """Laczy klienta IMGW, dekoder i renderer w jeden pipeline przetwarzajacy dane."""

    def __init__(
        self,
        project_path: str | Path,
        overlay_dir: str | Path | None = None,
        data_dir: str | Path | None = None,
        manifest_path: str | Path | None = None,
    ):
        p = Path(project_path)
        self._project_path  = p
        self._overlay_dir   = Path(overlay_dir)   if overlay_dir   else p / "img" / "polrad" / "overlay"
        self._data_dir      = Path(data_dir)       if data_dir      else p / "data" / "polrad"
        self._manifest_path = Path(manifest_path)  if manifest_path else p / "img" / "polrad" / "manifest.json"
        self._ftp_img_dir   = os.getenv("FTP_REMOTE_IMG_DIR", "img")

        self._client   = ImgwClient()
        self._decoder  = RadarDecoder()
        self._renderer = RadarRenderer()
        self._manifest = ManifestManager(self._manifest_path).load()

        log.debug("RadarPipeline zainicjalizowany: project=%s", project_path)

    def process_path(self, path_info: dict, cfg: dict, ftp_session=None) -> int:
        """
        Dla jednej sciezki IMGW:
          - usuwa lokalne obrazy starsze niz history_minutes (+ zdalne na FTP),
          - generuje obrazy dla plikow w oknie, ktorych PNG jeszcze nie ma,
          - uploaduje nowe pliki na FTP.
        Zwraca liczbe nowo wygenerowanych overlayow.
        """
        history_minutes = cfg.get("history_minutes", 60)
        cutoff     = datetime.utcnow() - timedelta(minutes=history_minutes)
        cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%S")

        path       = path_info["path"]
        key_prefix = path_info["key_prefix"]
        label_base = path_info["label_base"]

        log.info("[%s] Sprawdzam: %s", key_prefix, path)

        df = self._client.get_file_list(path)
        if df is None or df.empty:
            log.info("[%s] Brak plikow.", key_prefix)
            return 0

        df = df[df["filename"].str.endswith(".h5")].copy()
        if df.empty:
            log.info("[%s] Brak plikow .h5.", key_prefix)
            return 0

        df = df.sort_values("timestamp")
        log.debug("[%s] Lacznie plikow .h5 na serwerze: %d", key_prefix, len(df))

        units = df["unit"].dropna().unique().tolist() or [None]
        total = 0
        manifest_changed = False

        for unit in units:
            state_key   = f"{key_prefix}__{unit}" if unit else key_prefix
            product_key = state_key
            unit_label  = cfg["unit_labels"].get(unit, unit) if unit else ""
            label       = f"{label_base} – {unit_label}" if unit_label else label_base

            df_unit     = df[df["unit"] == unit] if unit else df
            overlay_dir = self._overlay_dir / product_key
            overlay_dir.mkdir(parents=True, exist_ok=True)

            # 1. Usun stare obrazy
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
                        ftp_session.delete(self._to_remote(png_file))
                        ftp_session.delete(self._to_remote(json_file))
                    deleted_count += 1
                    manifest_changed = True

            if deleted_count:
                log.info("[%s] Usunieto %d starych obrazow (poza oknem %d min)",
                         product_key, deleted_count, history_minutes)

            self._manifest.remove_frames_before(product_key, cutoff_iso)

            # 2. Ustal ktore pliki z okna wymagaja wygenerowania
            existing_stems = {f.stem for f in overlay_dir.glob("*.png")}
            df_window = df_unit[
                df_unit["timestamp"].notna() & (df_unit["timestamp"] >= cutoff)
            ]
            df_new = df_window[~df_window["timestamp"].apply(
                lambda t: t.strftime("%Y%m%d%H%M%S")
            ).isin(existing_stems)]

            log.info("[%s] Okno %d min: %d dostepnych, %d juz istnieje, %d do wygenerowania",
                     product_key, history_minutes,
                     len(df_window), len(existing_stems), len(df_new))

            if df_new.empty:
                continue

            # 3. Pobierz i wygeneruj nowe obrazy
            self._data_dir.mkdir(parents=True, exist_ok=True)

            for _, row in df_new.iterrows():
                h5_path = self._data_dir / row["filename"]
                log.info("[%s] Pobieram: %s", product_key, row["filename"])

                if not self._client.download_file(row["url"], str(h5_path)):
                    log.error("[%s] Nie udalo sie pobrac: %s", product_key, row["filename"])
                    continue

                try:
                    radar_data = self._decoder.decode(str(h5_path), projection="EPSG:3857")
                except Exception as e:
                    log.error("[%s] Blad dekodowania HDF5 (%s): %s",
                              product_key, row["filename"], e)
                    h5_path.unlink(missing_ok=True)
                    continue

                ts_str    = radar_data["start_date"].strftime("%Y%m%d%H%M%S")
                png_path  = overlay_dir / f"{ts_str}.png"
                json_path = overlay_dir / f"{ts_str}.json"

                frame_meta = self._renderer.render_overlay(radar_data, str(png_path))
                _save_json(frame_meta, json_path)
                log.info("[%s] Wygenerowano: %s.png", product_key, ts_str)

                image_rel = "../" + str(png_path.relative_to(self._project_path)).replace("\\", "/")
                self._manifest.add_frame(product_key, label, frame_meta, image_rel)

                if ftp_session:
                    ftp_session.upload(png_path, self._to_remote(png_path))
                    ftp_session.upload(json_path, self._to_remote(json_path))

                h5_path.unlink(missing_ok=True)
                total += 1
                manifest_changed = True

        if manifest_changed:
            self._manifest.save()
            if ftp_session:
                log.info("[%s] Uploading manifest.json na FTP", key_prefix)
                ftp_session.upload(self._manifest_path, self._to_remote(self._manifest_path))

        log.info("[%s] Gotowe: +%d nowych overlayow.", key_prefix, total)
        return total

    def _to_remote(self, local_path: Path) -> str:
        rel = local_path.relative_to(self._project_path / "img")
        return (Path(self._ftp_img_dir) / rel).as_posix()


def _save_json(data: dict, path: Path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
