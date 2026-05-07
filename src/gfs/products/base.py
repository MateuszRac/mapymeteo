from __future__ import annotations

import json
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
import xarray as xr

from ..map_builder import MapBuilder

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_IMG_DIR = _PROJECT_ROOT / "img" / "gfs"


def _read_grib_times(file_path: Path) -> tuple[datetime, datetime]:
    """Return (init_time, valid_time) from a GRIB file using cfgrib."""
    for level_type in ("meanSea", "surface", "isobaricInhPa", "heightAboveGround"):
        try:
            ds = xr.open_dataset(
                file_path,
                engine="cfgrib",
                filter_by_keys={"typeOfLevel": level_type},
            )
            init_time = pd.to_datetime(ds.time.values).to_pydatetime().replace(tzinfo=None)
            valid_time = pd.to_datetime(ds.valid_time.values).to_pydatetime().replace(tzinfo=None)
            return init_time, valid_time
        except Exception:
            continue
    raise ValueError(f"Cannot read time metadata from {file_path}")


def _update_manifest(out_dir: Path, meta: dict) -> None:
    """Update img/gfs/manifest.json with a new frame entry."""
    manifest_path = out_dir / "manifest.json"
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError):
            manifest = {"products": {}}
    else:
        manifest = {"products": {}}

    product = meta["product"]
    init_time = meta["init_time"]

    if product not in manifest["products"]:
        manifest["products"][product] = {"name": meta["title"], "runs": {}}

    runs = manifest["products"][product]["runs"]
    if init_time not in runs:
        runs[init_time] = []

    existing = {f["valid_time"] for f in runs[init_time]}
    if meta["valid_time"] not in existing:
        runs[init_time].append({
            "valid_time": meta["valid_time"],
            "forecast_hour": meta["forecast_hour"],
            "image": meta["image"],
        })
        runs[init_time].sort(key=lambda f: f["valid_time"])

    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")


class GfsProduct(ABC):
    """Base class for all GFS map products."""

    NAME: str = ""
    TITLE: str = ""
    extent: tuple[float, float, float, float] | None = None

    def __init__(self, builder: MapBuilder | None = None):
        if builder is None:
            builder = MapBuilder(*self.extent) if self.extent else MapBuilder()
        self.builder = builder

    @abstractmethod
    def _render(self, file_path: str | Path) -> None:
        """Render the product from a single GRIB file. Does not show or save."""

    def plot(
        self,
        file_path: str | Path,
        save: bool = False,
        show: bool = True,
        output_dir: str | Path | None = None,
    ) -> None:
        """Render the product and optionally save PNG+JSON and/or display it.

        Parameters
        ----------
        file_path  : path to the GRIB file
        save       : if True, write PNG and metadata JSON to output_dir
        show       : if True, call plt.show() (useful in Jupyter)
        output_dir : directory for output files; defaults to img/gfs/
        """
        file_path = Path(file_path)
        self._render(file_path)

        if save:
            out_dir = Path(output_dir) if output_dir else _DEFAULT_IMG_DIR
            out_dir.mkdir(parents=True, exist_ok=True)

            init_time, valid_time = _read_grib_times(file_path)
            name = self.NAME or type(self).__name__.lower()
            init_str = init_time.strftime("%Y%m%d%H%M")
            valid_str = valid_time.strftime("%Y%m%d%H%M")
            stem = f"{init_str}_{name}_{valid_str}"

            png_path = out_dir / f"{stem}.png"
            json_path = out_dir / f"{stem}.json"

            plt.savefig(png_path, dpi=150)

            forecast_hours = int((valid_time - init_time).total_seconds() / 3600)
            meta = {
                "product": name,
                "title": self.TITLE or name,
                "init_time": init_str,
                "valid_time": valid_str,
                "forecast_hour": forecast_hours,
                "image": f"{stem}.png",
                "generated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"),
            }
            json_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
            _update_manifest(out_dir, meta)

        if show:
            plt.show()
        else:
            plt.close()

    def save(self, file_path: str | Path, output_path: str | Path, dpi: int = 150) -> None:
        """Render and save to a specific path without displaying (legacy)."""
        self._render(file_path)
        plt.savefig(output_path, dpi=dpi)
        plt.close()
