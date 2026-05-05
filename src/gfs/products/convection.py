from __future__ import annotations

from pathlib import Path

import numpy as np
import xarray as xr

from .base import GfsProduct
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

def _mlcape(reader: GribReader) -> tuple[xr.DataArray, xr.DataArray]:
    """Gets mixed-layer CAPE and return (mlcape, cape)."""
    mlcape = reader.get_parameter("cape", "surface", 0)

    return mlcape 

class ConvectionProduct(GfsProduct):
    """Single-timestep CAPE × 0–6 km wind-shear composite."""

    def plot(self, file_path: str | Path) -> None:
        wmaxshear, cape = _wmaxshear(GribReader(file_path))

        ax = self.builder.create_figure()
        self.extent = (10, 35, 45, 60)
        cs = self.builder.add_shading(ax, wmaxshear, vmin=250, vmax=2500, step=250, cmap="jet")
        self.builder.add_colorbar(ax, cs, "WmaxShear 0–6 km [m²/s²]")
        self.builder.add_source_label(ax)
        self.builder.add_title(ax, cape.valid_time.values, prefix="WmaxShear 0–6 km")

class MLCape(GfsProduct):
    """MLCAPE"""
    extent = (10, 30, 47, 56)

    def plot(self, file_path: str | Path) -> None:
        mlcape = _mlcape(GribReader(file_path))

        ax = self.builder.create_figure()
        cs = self.builder.add_shading(ax, mlcape, vmin=50, vmax=3000, step=100, cmap="gnuplot2_r") #reverse cmap
        self.builder.add_colorbar(ax, cs, "MLCAPE [J/kg]")
        self.builder.add_source_label(ax)
        self.builder.add_title(ax, mlcape.valid_time.values, prefix="MLCAPE")


class DailyConvectionProduct(GfsProduct):
    """Daily maximum CAPE × wind-shear composite across multiple forecast steps."""

    def plot(self, file_path: str | Path) -> None:
        raise NotImplementedError("Use plot_multiple() with a list of GRIB files.")

    def plot_multiple(self, file_paths: list[str | Path]) -> None:
        """Render the daily max composite from *file_paths*."""
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

    def save_multiple(
        self,
        file_paths: list[str | Path],
        output_path: str | Path,
        dpi: int = 150,
    ) -> None:
        """Render and save the daily composite without displaying."""
        import matplotlib.pyplot as plt
        self.plot_multiple(file_paths)
        plt.savefig(output_path, dpi=dpi, bbox_inches="tight")
        plt.close()
