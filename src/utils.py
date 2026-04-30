import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

import h5py
import numpy as np
import pandas as pd
import requests
from bs4 import BeautifulSoup
from requests.exceptions import ConnectTimeout, ReadTimeout

IMGW_API_URL = "https://danepubliczne.imgw.pl/pl/datastore/getFilesList"
IMGW_BASE_URL = "https://danepubliczne.imgw.pl/pl/"


# ─────────────────────────────────────────────────────────
#  IMGW API
# ─────────────────────────────────────────────────────────

def get_list_of_files(path, product_type="oper"):
    """
    Pobiera listę plików z IMGW dla podanej ścieżki produktu.
    Zwraca DataFrame z kolumnami: url, filename, timestamp, unit, level.
    """
    response = requests.post(
        IMGW_API_URL,
        data={"productType": product_type, "path": path},
        timeout=15,
    )
    if response.status_code != 200:
        return None

    soup = BeautifulSoup(response.text, "html.parser")
    files = [
        (IMGW_BASE_URL + a["href"].strip(), a.get_text(strip=True))
        for a in soup.find_all("a", href=True)
    ]

    df = pd.DataFrame(files, columns=["url", "filename"])
    df["timestamp"] = df["filename"].apply(_timestamp_from_filename)
    df["unit"]      = df["filename"].apply(_unit_from_filename)
    df["level"]     = df["filename"].apply(_level_from_filename)
    return df


def download_file(url, output_path, max_retries=5, chunk_size=65536):
    """Pobiera plik z URL strumieniowo (oszczędność RAM) z retry i backoff wykładniczym."""
    for attempt in range(1, max_retries + 1):
        try:
            with requests.get(url, timeout=(5, 60), stream=True) as response:
                response.raise_for_status()
                with open(output_path, "wb") as f:
                    for chunk in response.iter_content(chunk_size=chunk_size):
                        f.write(chunk)
            return True
        except (ConnectTimeout, ReadTimeout):
            if attempt == max_retries:
                return False
            time.sleep(2 ** attempt)
        except requests.RequestException as e:
            print(f"[błąd pobierania] {e}")
            return False
    return False


# ─────────────────────────────────────────────────────────
#  Pliki i foldery
# ─────────────────────────────────────────────────────────

def clean_folder(folder_path):
    """Usuwa całą zawartość folderu (pliki i podkatalogi)."""
    for entry in Path(folder_path).iterdir():
        try:
            if entry.is_file() or entry.is_symlink():
                entry.unlink()
            elif entry.is_dir():
                shutil.rmtree(entry)
        except Exception as e:
            print(f"[błąd usuwania] {entry}: {e}")


def safe_filename(name):
    """Usuwa znaki niedozwolone w nazwach plików."""
    return re.sub(r'[\\/*?:"<>|]', "", name)


def fix_path(path):
    """Usuwa prefix dodawany przez Git Bash (np. /c/Program Files/Git/...)."""
    if path is None:
        return path
    return re.sub(r"^[A-Za-z]:/Program Files/Git", "", path)


# ─────────────────────────────────────────────────────────
#  Parsowanie nazw plików IMGW
# ─────────────────────────────────────────────────────────

def _timestamp_from_filename(filename):
    match = re.match(r"(\d{14})", filename)
    if match:
        return datetime.strptime(match.group(1), "%Y%m%d%H%M%S")
    return None


def _unit_from_filename(filename):
    match = re.match(r"\d{16}([A-Za-z]+)\.", filename)
    return match.group(1) if match else None


def _level_from_filename(filename):
    parts = filename.split(".")
    return parts[-2] if len(parts) >= 2 else None


# ─────────────────────────────────────────────────────────
#  Narzędzia HDF5
# ─────────────────────────────────────────────────────────

def hdf5_metadata(h5obj):
    """Rekurencyjnie wyciąga metadane z obiektu HDF5 jako słownik Pythona."""
    result = {}
    if hasattr(h5obj, "attrs"):
        result["_attrs"] = {k: _convert_h5_value(v) for k, v in h5obj.attrs.items()}
    if isinstance(h5obj, h5py.Dataset):
        result["shape"] = h5obj.shape
        result["dtype"] = str(h5obj.dtype)
        return result
    for key in h5obj.keys():
        result[key] = hdf5_metadata(h5obj[key])
    return result


def _convert_h5_value(obj):
    if isinstance(obj, bytes):
        return obj.decode()
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, np.generic):
        return obj.item()
    return obj


# ─────────────────────────────────────────────────────────
#  Kompatybilność wsteczna (stare nazwy)
# ─────────────────────────────────────────────────────────

# aliasy zachowane dla notebooka
extract_timestamp_from_filename = _timestamp_from_filename
extract_unit_from_filename       = _unit_from_filename
extract_level_from_filename      = _level_from_filename
hdf5_metadata_only               = hdf5_metadata

def extract_features_from_dataframe(df):
    df["timestamp"] = df["filename"].apply(_timestamp_from_filename)
    df["unit"]      = df["filename"].apply(_unit_from_filename)
    df["level"]     = df["filename"].apply(_level_from_filename)
    return df
