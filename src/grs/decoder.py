"""Parsowanie plików ASC (IMGW GRS — sumy opadów 60-minutowe)."""

import re
from datetime import datetime
from pathlib import Path

import numpy as np
from pyproj import Transformer

# Układ współrzędnych danych GRS — PUWG-1992 (EPSG:2180)
_GRS_CRS = "EPSG:2180"

_HEADER_KEYS = {
    "ncols", "nrows", "xllcorner", "yllcorner",
    "xllcenter", "yllcenter", "cellsize", "nodata_value",
}
_INT_KEYS = {"ncols", "nrows"}


class GrsDecoder:
    """Dekoduje plik ASC do struktury zgodnej z RadarRenderer."""

    def decode(self, asc_path: str | Path, projection: str = "EPSG:3857") -> dict:
        """
        Parsuje plik .asc i zwraca słownik zgodny ze strukturą RadarRenderer:
        {radar_data, lon_mesh, lat_mesh, start_date, quantity, product, system}
        """
        asc_path = Path(asc_path)
        timestamp = self._timestamp_from_filename(asc_path.name)
        header, data = self._parse_asc(asc_path)

        ncols    = header["ncols"]
        nrows    = header["nrows"]
        xll      = header["xllcorner"]
        yll      = header["yllcorner"]
        cellsize = header["cellsize"]
        nodata   = header.get("nodata_value", -999.0)

        # Centra komórek; wiersze ASC idą od północy (wiersz 0) do południa (wiersz nrows-1)
        x_centers = xll + (np.arange(ncols) + 0.5) * cellsize
        y_centers = yll + (nrows - np.arange(nrows) - 0.5) * cellsize

        xx, yy = np.meshgrid(x_centers, y_centers)

        transformer = Transformer.from_crs(_GRS_CRS, projection, always_xy=True)
        x_mesh, y_mesh = transformer.transform(xx, yy)

        data = np.where((data == nodata) | (data < 0), np.nan, data)

        return {
            "radar_data": {"dataset1": data.astype(np.float32)},
            "lon_mesh":   x_mesh,
            "lat_mesh":   y_mesh,
            "start_date": timestamp,
            "quantity":   "PRECIP",
            "product":    "GRS",
            "system":     "GRS",
        }

    @staticmethod
    def _parse_asc(path: Path) -> tuple[dict, np.ndarray]:
        """Wczytuje nagłówek i dane z pliku ESRI ASCII Raster."""
        header: dict = {}

        with open(path, encoding="ascii", errors="replace") as f:
            lines = f.readlines()

        data_start = len(lines)
        for i, line in enumerate(lines):
            parts = line.strip().lower().split(None, 1)
            if len(parts) == 2 and parts[0] in _HEADER_KEYS:
                key = parts[0]
                header[key] = int(float(parts[1])) if key in _INT_KEYS else float(parts[1])
            elif header:
                data_start = i
                break

        # Obsługa xllcenter/yllcenter (alternatywny wariant nagłówka ASC)
        if "xllcenter" in header and "xllcorner" not in header:
            cs = header.get("cellsize", 1.0)
            header["xllcorner"] = header["xllcenter"] - 0.5 * cs
            header["yllcorner"] = header["yllcenter"] - 0.5 * cs

        nrows  = header["nrows"]
        ncols  = header["ncols"]
        nodata = float(header.get("nodata_value", -999.0))

        data = np.full((nrows, ncols), nodata, dtype=np.float32)
        for i, line in enumerate(lines[data_start: data_start + nrows]):
            vals = line.split()
            if len(vals) == ncols:
                data[i] = list(map(float, vals))

        return header, data

    @staticmethod
    def _timestamp_from_filename(filename: str) -> datetime:
        """Odczytuje timestamp z nazwy: YYYYMMDDHHMM_acc0060_grs.asc"""
        match = re.match(r"(\d{12})", filename)
        if not match:
            raise ValueError(f"Nie można odczytać daty z nazwy pliku: {filename}")
        return datetime.strptime(match.group(1), "%Y%m%d%H%M")
