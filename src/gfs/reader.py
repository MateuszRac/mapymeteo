from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr


class GribReader:
    """Opens a single GFS GRIB file and extracts variables.

    Parameters
    ----------
    file_path : path to the .grib2 file
    """

    def __init__(self, file_path: str | Path):
        self.file_path = Path(file_path)

    def get_parameter(
        self,
        parameter: str,
        type_of_level: str,
        level: int,
        step_type: str | None = "instant",
    ) -> xr.DataArray:
        """Load a single variable from the GRIB file.

        Parameters
        ----------
        parameter : GRIB short name (e.g. 'prmsl', 't', 'cape')
        type_of_level : GRIB level type (e.g. 'meanSea', 'isobaricInhPa', 'surface')
        level : level value (e.g. 850, 0)
        step_type : 'instant', 'accum', or None to omit the filter
        """
        filter_keys: dict = {"typeOfLevel": type_of_level, "level": level}
        if step_type is not None:
            filter_keys["stepType"] = step_type

        ds = xr.open_dataset(
            self.file_path,
            engine="cfgrib",
            filter_by_keys=filter_keys,
        )
        return ds[parameter]

    def get_variables(
        self,
        type_of_level: str = "surface",
        step_type: str | None = "instant",
    ):
        """Return the xarray variables dict for a given level type."""
        filter_keys: dict = {"typeOfLevel": type_of_level}
        if step_type is not None:
            filter_keys["stepType"] = step_type

        ds = xr.open_dataset(
            self.file_path,
            engine="cfgrib",
            filter_by_keys=filter_keys,
        )
        return ds.variables

    def get_type_of_levels(self, step_type: str | None = "instant") -> np.ndarray:
        """Return the unique typeOfLevel values available in the file."""
        filter_keys: dict = {}
        if step_type is not None:
            filter_keys["stepType"] = step_type

        ds = xr.open_dataset(
            self.file_path,
            engine="cfgrib",
            filter_by_keys=filter_keys,
        )
        return np.unique(ds["typeOfLevel"].values)

    def scan_file(self) -> pd.DataFrame:
        """Return a table of all parameters, typeOfLevel, level, and stepType in the file.

        Uses cfgrib.open_datasets which splits the GRIB into logical groups.
        Useful for discovering what to pass to get_parameter().
        """
        import cfgrib

        records = []
        for ds in cfgrib.open_datasets(str(self.file_path)):
            for var in ds.data_vars:
                da = ds[var]
                type_of_level = da.attrs.get("GRIB_typeOfLevel", "?")
                step_type = da.attrs.get("GRIB_stepType", "?")

                level_val = da.coords.get(type_of_level, da.coords.get("level", None))
                if level_val is not None:
                    levels = sorted(set(float(v) for v in level_val.values.flatten()))
                else:
                    levels = [None]

                for lev in levels:
                    records.append({
                        "parameter": var,
                        "typeOfLevel": type_of_level,
                        "level": lev,
                        "stepType": step_type,
                    })

        return pd.DataFrame(records).drop_duplicates().reset_index(drop=True)
