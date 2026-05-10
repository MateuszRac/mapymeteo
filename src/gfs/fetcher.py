from __future__ import annotations

from ftplib import FTP
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlparse
import re

import pandas as pd
import requests
from bs4 import BeautifulSoup


_NOMADS_BASE = "https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod/"
_FILTER_BASE = "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl"

DEFAULT_PARAMS: dict = {
    "var_PRMSL": "on",
    "var_PRATE": "on",
    "var_TMP": "on",
    "var_SPFH": "on",
    "var_CAPE": "on",
    "var_HGT": "on",
    "var_UGRD": "on",
    "var_VGRD": "on",
    "var_USTM" : "on",
    "var_VSTM": "on",
    "var_ACPCP": "on",
    "lev_mean_sea_level": "on",
    "lev_surface": "on",
    "lev_850_mb": "on",
    "lev_500_mb": "on",
    "lev_450_mb": "on",
    "lev_180-0_mb_above_ground": "on",
    "lev_10_m_above_ground": "on",
    "lev_6000-0_m_above_ground": "on",
    "subregion": "",
    "toplat": 70,
    "bottomlat": 25,
    "leftlon": -30,
    "rightlon": 40,
}


class GfsFetcher:
    """Downloads GFS GRIB files from NOAA NOMADS.

    Parameters
    ----------
    output_dir : path where downloaded GRIB files are saved
    timeout : HTTP/FTP connection timeout in seconds
    """

    def __init__(self, output_dir: str | Path, timeout: int = 30):
        self.output_dir = Path(output_dir)
        self.timeout = timeout
        self._session = requests.Session()

    # ------------------------------------------------------------------
    # Listing
    # ------------------------------------------------------------------

    def find_latest_run(self, min_files: int = 49) -> pd.DataFrame:
        """Find the latest complete GFS run on NOMADS with minimal requests.

        Iterates from the newest date/run backwards and stops as soon as a run
        with at least *min_files* GRIB files is found.

        Columns: cycle_date, run_hour, file_name, url
        """
        root_links = self._get_links(_NOMADS_BASE)
        gfs_dirs = sorted(
            [x.rstrip("/") for x in root_links if x.startswith("gfs.") and x.endswith("/")],
            reverse=True,
        )

        for gfs_dir in gfs_dirs:
            gfs_url = urljoin(_NOMADS_BASE, gfs_dir + "/")
            try:
                run_links = self._get_links(gfs_url)
            except requests.HTTPError:
                continue

            run_dirs = sorted(
                [x.rstrip("/") for x in run_links if re.fullmatch(r"(00|06|12|18)/", x)],
                reverse=True,
            )

            for run in run_dirs:
                atmos_url = urljoin(gfs_url, f"{run}/atmos/")
                try:
                    file_links = self._get_links(atmos_url)
                except requests.HTTPError:
                    continue

                cycle_date = gfs_dir.replace("gfs.", "")
                records = [
                    {
                        "cycle_date": cycle_date,
                        "run_hour": run,
                        "file_name": fname,
                        "url": urljoin(atmos_url, fname),
                    }
                    for fname in file_links
                    if not fname.endswith("/")
                    and re.search(r"\.pgrb2\.0p25", fname)
                    and ".idx" not in fname.lower()
                    and "anl" not in fname.lower()
                ]

                if len(records) >= min_files:
                    return pd.DataFrame(records)

        raise RuntimeError("No complete GFS run found on NOMADS.")

    # ------------------------------------------------------------------
    # URL building
    # ------------------------------------------------------------------

    def build_url(
        self,
        cycle_date: str,
        run_hour: str,
        file_name: str,
        params: dict | None = None,
    ) -> str:
        """Return a filtered NOMADS download URL for a single file."""
        p = dict(DEFAULT_PARAMS if params is None else params)
        p["dir"] = f"/gfs.{cycle_date}/{run_hour}/atmos"
        p["file"] = file_name
        return f"{_FILTER_BASE}?{urlencode(p)}"

    # ------------------------------------------------------------------
    # Downloading
    # ------------------------------------------------------------------

    def download(self, url: str, filename: str | None = None) -> Path:
        """Download a single file via HTTP/HTTPS and return its local path."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        if filename is None:
            filename = Path(urlparse(url).path).name
        dest = self.output_dir / filename

        with self._session.get(url, stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
        return dest

    def download_ftp(self, url: str, filename: str | None = None) -> Path:
        """Download a single file via anonymous FTP and return its local path."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        parsed = urlparse(url)
        if parsed.scheme != "ftp":
            raise ValueError("URL must start with ftp://")
        if filename is None:
            filename = Path(parsed.path).name
        dest = self.output_dir / filename

        ftp = FTP(parsed.hostname, timeout=self.timeout)
        ftp.login()
        with open(dest, "wb") as f:
            ftp.retrbinary(f"RETR {parsed.path}", f.write, blocksize=8192)
        ftp.quit()
        return dest

    def fetch_latest(
        self,
        n_files: int = 49,
        params: dict | None = None,
        clear_first: bool = False,
    ) -> list[Path]:
        """List NOMADS, resolve the latest complete run, and download *n_files*.

        Parameters
        ----------
        n_files : maximum number of forecast files to download
        params : override the default NOMADS filter parameters
        clear_first : remove existing files in output_dir before downloading
        """
        if clear_first:
            self.clear()

        run_df = self.find_latest_run(min_files=n_files)

        paths: list[Path] = []
        for _, row in run_df.head(n_files).iterrows():
            url = self.build_url(row["cycle_date"], row["run_hour"], row["file_name"], params)
            path = self.download(url, filename=row["file_name"])
            paths.append(path)
        return paths

    def summarize_files(self, file_paths: list[Path] | None = None) -> "pd.DataFrame":
        """Scan one GRIB file and return a table of available parameters/levels.

        All files in a single GFS run share the same variable set, so only the
        first file is scanned. Pass file_paths to override the auto-discovery.
        """
        from .reader import GribReader

        if file_paths is None:
            file_paths = sorted(self.output_dir.glob("*.pgrb2*"))
        if not file_paths:
            raise FileNotFoundError(f"No GRIB files found in {self.output_dir}")
        return GribReader(file_paths[0]).scan_file()

    def clear(self) -> None:
        """Delete all files in the output directory (leaves subdirs intact)."""
        if self.output_dir.exists():
            for f in self.output_dir.iterdir():
                if f.is_file():
                    f.unlink()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_links(self, url: str) -> list[str]:
        r = self._session.get(url, timeout=self.timeout)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        return [
            a["href"].strip()
            for a in soup.find_all("a", href=True)
            if a["href"].strip() not in ("../", "/")
        ]
