from __future__ import annotations

from pathlib import Path

from .base import GfsProduct
from ..reader import GribReader


class MslpProduct(GfsProduct):
    """MSLP isobars with surface CAPE shading."""

    NAME = "mslp"
    TITLE = "MSLP · Surface CAPE"

    def _render(self, file_path: str | Path) -> None:
        reader = GribReader(file_path)
        mslp = reader.get_parameter("prmsl", "meanSea", 0) / 100
        cape = reader.get_parameter("cape", "surface", 0)

        ax = self.builder.create_figure()
        self.builder.add_shading(ax, cape, vmin=100, vmax=2000, cmap="YlOrRd")
        self.builder.add_contours(ax, mslp, step=5, vmin=920, vmax=1070, sigma=1.5)
        self.builder.add_source_label(ax)
        self.builder.add_title(ax, mslp.valid_time.values, prefix="MSLP  ·  Surface CAPE")
