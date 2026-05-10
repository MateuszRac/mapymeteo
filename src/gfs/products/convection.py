from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import xarray as xr

from .base import GfsProduct, _DEFAULT_IMG_DIR, _read_grib_times, _update_manifest
from ..map_builder import MapBuilder
from ..reader import GribReader


def _wmaxshear(reader: GribReader) -> tuple[xr.DataArray, xr.DataArray]:
    """Compute wmax*shear composite and return (wmaxshear, cape)."""
    cape = reader.get_parameter("cape", "surface", 0)

    p500_u = reader.get_parameter("u", "isobaricInhPa", 500)
    p500_v = reader.get_parameter("v", "isobaricInhPa", 500)
    p500_hgt = reader.get_parameter("gh", "isobaricInhPa", 500)
    p450_u = reader.get_parameter("u", "isobaricInhPa", 450)
    p450_v = reader.get_parameter("v", "isobaricInhPa", 450)
    p450_hgt = reader.get_parameter("gh", "isobaricInhPa", 450)

    sfc_hgt = reader.get_parameter("orog", "surface", 0)
    u10 = reader.get_parameter("u10", "heightAboveGround", 10)
    v10 = reader.get_parameter("v10", "heightAboveGround", 10)

    h6km = sfc_hgt + 10 + 6000
    frac = (h6km - p500_hgt) / (p450_hgt - p500_hgt)
    u6km = p500_u + frac * (p450_u - p500_u)
    v6km = p500_v + frac * (p450_v - p500_v)

    shear = np.sqrt((u6km - u10) ** 2 + (v6km - v10) ** 2)
    return np.sqrt(2 * cape) * shear, cape


def _mlcape(reader: GribReader) -> xr.DataArray:
    return reader.get_parameter("cape", "surface", 0)


class ConvectionProduct(GfsProduct):
    """Single-timestep CAPE × 0–6 km wind-shear composite."""

    NAME = "wmaxshear"
    TITLE = "WmaxShear 0–6 km"
    TITLE = "MLCAPE"
    extent = (12, 28, 48, 56)
    def _render(self, file_path: str | Path) -> None:
        wmaxshear, cape = _wmaxshear(GribReader(file_path))

        ax = self.builder.create_figure()
        cs = self.builder.add_shading(ax, wmaxshear, vmin=50, vmax=2500, step=50, cmap="jet")
        self.builder.add_colorbar(ax, cs, "WmaxShear 0–6 km [m²/s²]")
        self.builder.add_source_label(ax)
        self.builder.add_title(ax, cape.valid_time.values, prefix="WmaxShear 0–6 km")


class MLCape(GfsProduct):
    """Mixed-layer CAPE."""

    NAME = "mlcape"
    TITLE = "MLCAPE"
    extent = (10, 30, 47, 56)

    def _render(self, file_path: str | Path) -> None:
        mlcape = _mlcape(GribReader(file_path))

        ax = self.builder.create_figure()
        cs = self.builder.add_shading(ax, mlcape, vmin=50, vmax=3000, step=100, cmap="gnuplot2_r")
        self.builder.add_colorbar(ax, cs, "MLCAPE [J/kg]")
        self.builder.add_source_label(ax)
        self.builder.add_title(ax, mlcape.valid_time.values, prefix="MLCAPE")


class ConvectivePrecipProduct(GfsProduct):
    """Accumulated convective precipitation shading (greens) + storm motion barbs."""

    NAME = "conv_precip"
    TITLE = "Opad konwekcyjny + wektor burz"
    extent = (10, 30, 47, 56)

    def _render(self, file_path: str | Path) -> None:
        reader = GribReader(file_path)

        # Accumulated convective precip [kg/m² == mm]
        acpcp = reader.get_parameter("prate", "surface", 0, step_type="instant")*60*60

        # Storm motion vectors — GFS stores these at the 0–6000 m layer
        # typeOfLevel='heightAboveGroundLayer', level=6000 (top of layer in cfgrib)
        try:
            ustm = reader.get_parameter("ustm", "heightAboveGroundLayer", 6000, step_type="instant")
            vstm = reader.get_parameter("vstm", "heightAboveGroundLayer", 6000, step_type="instant")
            has_storm = True
        except Exception:
            has_storm = False

        ax = self.builder.create_figure()

        cs = self.builder.add_shading(
            ax, acpcp,
            vmin=0.5, vmax=100, step=2.5,
            cmap="YlGn", vmin_transp=0,
        )
        self.builder.add_colorbar(ax, cs, "Opad konwekcyjny skum. [mm]")

        if has_storm:
            self.builder.add_barbs(ax, ustm, vstm, step=5, color="#1a1a1a")

        self.builder.add_source_label(ax)
        self.builder.add_title(ax, acpcp.valid_time.values, prefix=self.TITLE)


class DailyConvectionProduct(GfsProduct):
    """Daily maximum CAPE × wind-shear composite across multiple forecast steps."""

    NAME = "daily_wmaxshear"
    TITLE = "WmaxShear 0–6 km — dzienny max"

    def _render(self, file_path: str | Path) -> None:
        raise NotImplementedError("Use plot_multiple() with a list of GRIB files.")

    def plot_multiple(
        self,
        file_paths: list[str | Path],
        save: bool = False,
        show: bool = True,
        output_dir: str | Path | None = None,
    ) -> None:
        """Render the daily max composite and optionally save/display it."""
        file_paths = [Path(p) for p in file_paths]
        max_wmaxshear: xr.DataArray | None = None
        last_cape: xr.DataArray | None = None

        for fp in file_paths:
            wmaxshear, cape = _wmaxshear(GribReader(fp))
            max_wmaxshear = wmaxshear if max_wmaxshear is None else np.maximum(max_wmaxshear, wmaxshear)
            last_cape = cape

        if max_wmaxshear is None or last_cape is None:
            raise ValueError("file_paths must not be empty.")

        ax = self.builder.create_figure()
        cs = self.builder.add_shading(ax, max_wmaxshear, vmin=250, vmax=2000, step=50, cmap="jet")
        self.builder.add_colorbar(ax, cs, "WmaxShear 0–6 km dzienny max [m²/s²]")
        self.builder.add_source_label(ax)
        self.builder.add_title(ax, last_cape.valid_time.values, prefix="WmaxShear 0–6 km — dzienny max")

        if save:
            out_dir = Path(output_dir) if output_dir else _DEFAULT_IMG_DIR
            out_dir.mkdir(parents=True, exist_ok=True)

            init_time, _ = _read_grib_times(file_paths[0])
            _, valid_time = _read_grib_times(file_paths[-1])
            init_str = init_time.strftime("%Y%m%d%H%M")
            valid_str = valid_time.strftime("%Y%m%d%H%M")
            stem = f"{init_str}_{self.NAME}_{valid_str}"

            plt.savefig(out_dir / f"{stem}.png", dpi=150)

            forecast_hours = int((valid_time - init_time).total_seconds() / 3600)
            meta = {
                "product": self.NAME,
                "title": self.TITLE,
                "init_time": init_str,
                "valid_time": valid_str,
                "forecast_hour": forecast_hours,
                "image": f"{stem}.png",
                "generated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"),
            }
            (out_dir / f"{stem}.json").write_text(
                json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            _update_manifest(out_dir, meta)

        if show:
            plt.show()
        else:
            plt.close()

    def save_multiple(
        self,
        file_paths: list[str | Path],
        output_path: str | Path,
        dpi: int = 150,
    ) -> None:
        """Render and save the daily composite to a specific path (legacy)."""
        self.plot_multiple(file_paths, save=False, show=False)
        plt.savefig(output_path, dpi=dpi)
        plt.close()
