from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

import matplotlib.pyplot as plt

from ..map_builder import MapBuilder


class GfsProduct(ABC):
    """Base class for all GFS map products.

    Subclasses implement :meth:`plot` to render one forecast step.
    Override ``extent`` as ``(min_lon, max_lon, min_lat, max_lat)`` to set
    a product-specific map extent.
    """

    extent: tuple[float, float, float, float] | None = None

    def __init__(self, builder: MapBuilder | None = None):
        if builder is None:
            builder = MapBuilder(*self.extent) if self.extent else MapBuilder()
        self.builder = builder

    @abstractmethod
    def plot(self, file_path: str | Path) -> None:
        """Render the product from a single GRIB file."""

    def save(self, file_path: str | Path, output_path: str | Path, dpi: int = 150) -> None:
        """Render the product and save it to *output_path* without displaying."""
        self.plot(file_path)
        plt.savefig(output_path, dpi=dpi, bbox_inches="tight")
        plt.close()
