"""
Generuje config/palettes.json z palet uzywanych przez renderer (styl noaa).

Dla wielkosci z plikiem .pal w data/color_tables/ uzywa tych kolorow.
Dla pozostalych odpada na palette imgw/nexrad.

Uzycie:
  python generate_palettes.py
"""

import json
import math
import os
import sys
from pathlib import Path

import numpy as np
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=True)
sys.path.insert(0, str(ROOT / "src"))

from radar.palette import RadarPalette
from matplotlib.colors import BoundaryNorm

COLOR_TABLES = ROOT / "data" / "color_tables"
OUTPUT       = ROOT / "config" / "palettes.json"

QUANTITIES = {
    "DBZH":   {"ticks": [5, 10, 20, 30, 40, 50, 60, 65]},
    "RATE":   {"ticks": [0.01, 0.05, 0.1, 0.5, 1, 5, 10, 50], "log": True},
    "VRADH":  {"ticks": [-30, -20, -10, 0, 10, 20, 30]},
    "ZDR":    {"ticks": [-1, 0, 1, 2, 3]},
    "RHOHV":  {"ticks": [0.7, 0.8, 0.9, 0.95, 1.0]},
    "KDP":    {"ticks": [-1, 0, 1, 2, 3]},
    "PHIDP":  {"ticks": [0, 30, 60, 90, 120, 150, 180]},
    "HGHT":   {"ticks": [0, 3, 6, 9, 12, 15]},
    "PRECIP": {"ticks": [0.1, 1, 5, 10, 20, 50, 100]},
}

N_AUTO_TICKS = 7  # liczba ticków gdy predefined są poza zakresem


def _hex(rgba) -> str:
    return "#{:02x}{:02x}{:02x}".format(
        min(255, int(rgba[0] * 255)),
        min(255, int(rgba[1] * 255)),
        min(255, int(rgba[2] * 255)),
    )


def sample_colors(cmap, norm, n: int = 64) -> list:
    if isinstance(norm, BoundaryNorm):
        n_bins = norm.N
        return [_hex(cmap((i + 0.5) / n_bins)) for i in range(n_bins)]
    return [_hex(cmap(i / (n - 1))) for i in range(n)]


def _pct(v, vmin, vmax, is_log: bool) -> float:
    if is_log:
        return (math.log10(v) - math.log10(vmin)) / (math.log10(vmax) - math.log10(vmin)) * 100
    return (v - vmin) / (vmax - vmin) * 100


def _auto_ticks(vmin, vmax, n: int, is_log: bool) -> list:
    if is_log:
        vals = np.logspace(math.log10(vmin), math.log10(vmax), n)
    else:
        vals = np.linspace(vmin, vmax, n)
    result = []
    for v in vals:
        v = float(v)
        label = f"{v:.2g}"
        result.append({"value": v, "label": label, "pct": _pct(v, vmin, vmax, is_log)})
    return result


def build_ticks(tick_values, vmin, vmax, is_log: bool, norm=None) -> list:
    in_range = [v for v in tick_values if vmin <= v <= vmax]
    if not in_range:
        return _auto_ticks(vmin, vmax, N_AUTO_TICKS, is_log)
    result = []
    if isinstance(norm, BoundaryNorm):
        bounds = list(norm.boundaries)
        n_segs = len(bounds) - 1
        for v in in_range:
            label = str(v) if v != int(v) else str(int(v))
            idx = min(range(len(bounds)), key=lambda i: abs(bounds[i] - v))
            result.append({"value": v, "label": label, "pct": round(idx / n_segs * 100, 3)})
    else:
        for v in in_range:
            label = str(v) if v != int(v) else str(int(v))
            result.append({"value": v, "label": label, "pct": _pct(v, vmin, vmax, is_log)})
    return result


def generate(pal_dir=COLOR_TABLES, style: str | None = None) -> dict:
    if style is None:
        style = os.getenv("PALETTE_STYLE", "noaa")
    pal    = RadarPalette(pal_dir=pal_dir)
    output = {}

    for qty, meta in QUANTITIES.items():
        try:
            cmap, norm, label = pal.get(qty, style=style)
        except ValueError as e:
            print(f"  Pominięto {qty}: {e}")
            continue

        is_log      = meta.get("log", False)
        is_discrete = isinstance(norm, BoundaryNorm)

        if is_discrete:
            colors = sample_colors(cmap, norm)
            vmin   = float(norm.boundaries[0])
            vmax   = float(norm.boundaries[-1])
        else:
            colors = sample_colors(cmap, norm, n=64)
            vmin   = float(norm.vmin)
            vmax   = float(norm.vmax)

        pal_src = "(.pal)" if (pal_dir / f"{qty}.pal").exists() else "(fallback)"
        print(f"  {qty} {pal_src}: {len(colors)} kolorow, vmin={vmin}, vmax={vmax}")

        output[qty] = {
            "label":    label,
            "type":     "log" if is_log else ("boundary" if is_discrete else "linear"),
            "discrete": is_discrete,
            "vmin":     vmin,
            "vmax":     vmax,
            "colors":   colors,
            "ticks":    build_ticks(meta["ticks"], vmin, vmax, is_log, norm=norm),
        }

    return output


if __name__ == "__main__":
    style = os.getenv("PALETTE_STYLE", "noaa")
    print(f"Generowanie palettes.json z: {COLOR_TABLES} (styl: {style})")
    data = generate()
    OUTPUT.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nZapisano: {OUTPUT}")
