from __future__ import annotations

from pathlib import Path

import xarray as xr

from .base import GfsProduct
from ..reader import GribReader
from ..utils import thetae as calc_thetae


class ThetaEProduct(GfsProduct):
    """850 hPa equivalent potential temperature with MSLP contours.

    Layers:
    - θe 850 hPa shading (nipy_spectral)
    - MSLP isobars every 5 hPa
    - H/L markers at pressure extrema
    - Hatching where PRATE > 0.5 mm/hr
    """

    def plot(self, file_path: str | Path) -> None:
        reader = GribReader(file_path)

        mslp = reader.get_parameter("prmsl", "meanSea", 0) / 100
        prate = reader.get_parameter("prate", "surface", 0) * 3600
        t850 = reader.get_parameter("t", "isobaricInhPa", 850)
        q850 = reader.get_parameter("q", "isobaricInhPa", 850)

        thetae850 = xr.DataArray(
            calc_thetae(t850, q850, 850) - 273.15,
            dims=("latitude", "longitude"),
            coords={"latitude": q850.latitude, "longitude": q850.longitude},
            name="ThetaE",
        )

        ax = self.builder.create_figure()
        self.builder.add_contours(ax, mslp, step=5, vmin=920, vmax=1070, sigma=1.5)
        self.builder.add_min_max_points(ax, mslp)
        self.builder.add_hatched_area(ax, prate > 0.5)
        cs = self.builder.add_shading(ax, thetae850, vmin=-10, vmax=50, step=2, cmap="nipy_spectral")

        self.builder.add_colorbar(ax, cs, "Temperatura Ekwiwalentno-Potencjalna θe 850hPa [°C]")
        self.builder.add_source_label(ax)
        self.builder.add_title(ax, mslp.valid_time.values, prefix="θe 850 hPa  ·  MSLP")
