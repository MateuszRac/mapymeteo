from __future__ import annotations

import os
from pathlib import Path

import cartopy
from dotenv import load_dotenv

load_dotenv()
_data_dir = os.getenv("CARTOPY_DATA_DIR")
if _data_dir:
    Path(_data_dir).mkdir(parents=True, exist_ok=True)
    cartopy.config["data_dir"] = _data_dir

import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
import matplotlib.patches as mpatches
import matplotlib.patheffects as pe
import cartopy.crs as ccrs
import cartopy.feature as cfeature
import numpy as np
import pandas as pd
from scipy.ndimage import minimum_filter, maximum_filter, gaussian_filter
from scipy.spatial import cKDTree
import xarray as xr

# ── Overlay design constants ─────────────────────────────────────────────────
_BAR_H    = 0.050   # height of top/bottom info bars (axes fraction)
_CBAR_Y   = 0.074   # colorbar inset bottom edge (gap above source bar for tick labels)
_CBAR_H   = 0.028   # colorbar inset height
_CBAR_BG  = 0.090   # extra dark band above source bar (for colorbar zone)
_OVL_BG   = "#000814"
_OVL_A    = 0.78
_FG       = "#ffffff"
_FG_DIM   = "#aaccff"
_ATTR     = "Dane: GFS/NCEP  ·  Wizualizacja: mapymeteo.pl"


class MapBuilder:
    """Creates and decorates Cartopy maps for GFS products.

    Parameters
    ----------
    min_lon, max_lon, min_lat, max_lat : geographic extent in degrees
    """

    _land = cfeature.NaturalEarthFeature("physical", "land", "50m", facecolor="#e2ffd5")
    _ocean = cfeature.NaturalEarthFeature("physical", "ocean", "50m", facecolor="#dcf4ff")
    _borders = cfeature.NaturalEarthFeature(
        "cultural", "admin_0_boundary_lines_land", "50m",
        edgecolor="black", facecolor="none", linewidth=0.5,
    )
    _coastlines = cfeature.NaturalEarthFeature(
        "physical", "coastline", "50m",
        edgecolor="black", facecolor="none", linewidth=0.8,
    )

    def __init__(
        self,
        min_lon: float = -20,
        max_lon: float = 35,
        min_lat: float = 30,
        max_lat: float = 70,
    ):
        self.extent = [min_lon, max_lon, min_lat, max_lat]
        self._proj = ccrs.LambertConformal(
            central_longitude=20,
            central_latitude=52,
            standard_parallels=(30, 60),
        )

    # ------------------------------------------------------------------
    # Figure setup
    # ------------------------------------------------------------------

    def create_figure(self, figsize: tuple[int, int] = (10, 8)):
        """Create a configured GeoAxes and return it."""
        fig = plt.figure(figsize=figsize, facecolor="#dcf4ff")
        ax = plt.axes(projection=self._proj)
        ax.set_extent(self.extent, crs=ccrs.PlateCarree())
        # Transparent axes background – corners show fig facecolor (ocean blue)
        ax.set_facecolor("none")
        # Lock projected limits – prevents border annotations from pushing the frame
        ax.set_xlim(ax.get_xlim())
        ax.set_ylim(ax.get_ylim())

        ax.add_feature(self._land, zorder=0)
        ax.add_feature(self._ocean, zorder=0)
        ax.add_feature(self._coastlines, zorder=5)
        ax.add_feature(self._borders, zorder=6)

        gl = ax.gridlines(
            draw_labels=True, x_inline=True, y_inline=True,
            linewidth=0.4, color="gray", alpha=0.55, linestyle="--",
        )
        gl.xlabel_style = {"size": 8, "color": "#444444"}
        gl.ylabel_style = {"size": 8, "color": "#444444"}

        # Map fills the entire figure – no white border
        fig.subplots_adjust(left=0, right=1, top=1, bottom=0)
        return ax

    # ------------------------------------------------------------------
    # Shading
    # ------------------------------------------------------------------

    def add_shading(
        self,
        ax,
        data: xr.DataArray,
        vmin: float | None = None,
        vmax: float | None = None,
        step: float | None = None,
        vmin_transp: float = 0,
        cmap: str = "viridis",
        alpha: float = 1.0,
        fast: bool = False,
    ):
        """Add filled color shading for a parameter.

        Parameters
        ----------
        fast : if True, use pcolormesh (faster); otherwise contourf
        """
        lon = data["longitude"]
        lat = data["latitude"]
        values = data.values

        vmin = vmin if vmin is not None else float(np.nanmin(values))
        vmax = vmax if vmax is not None else float(np.nanmax(values))
        step = step if step is not None else (vmax - vmin) / 10
        levels = np.arange(vmin, vmax + step, step)

        base_cmap = plt.get_cmap(cmap)
        norm = mcolors.BoundaryNorm(boundaries=levels, ncolors=base_cmap.N, clip=fast)
        alpha_under = float(np.clip(vmin_transp, 0, 1))
        cmap_mod = base_cmap.with_extremes(under=(*base_cmap(0)[:3], alpha_under))

        if fast:
            return ax.pcolormesh(
                lon, lat, values,
                cmap=cmap_mod, norm=norm, alpha=alpha,
                transform=ccrs.PlateCarree(),
            )

        return ax.contourf(
            lon, lat, values,
            levels=levels, cmap=cmap_mod, norm=norm,
            transform=ccrs.PlateCarree(),
            extend="max", alpha=alpha,
        )

    def add_precip_shading(
        self,
        ax,
        precip: xr.DataArray,
        vmin: float = 0.1,
        vmax: float = 15,
    ):
        """Add precipitation shading with a meteorological color ramp."""
        lon = precip["longitude"].values
        lat = precip["latitude"].values
        data = np.ma.masked_less(precip.values, vmin)
        levels = np.arange(vmin, vmax + 1, 1)

        def _n(v):
            return (v - vmin) / (vmax - vmin)

        anchors = [
            (0.0,      "#dbefff"),
            (_n(5.0),  "#0033aa"),
            (_n(10.0), "#6a00a8"),
            (_n(15.0), "#8b0000"),
            (1.0,      "#8b0000"),
        ]
        cmap = mcolors.LinearSegmentedColormap.from_list("precip_custom", anchors, N=256)
        norm = mcolors.Normalize(vmin=vmin, vmax=vmax)

        return ax.contourf(
            lon, lat, data,
            levels=levels, cmap=cmap, norm=norm,
            transform=ccrs.PlateCarree(), extend="max",
        )

    # ------------------------------------------------------------------
    # Contours
    # ------------------------------------------------------------------

    def add_contours(
        self,
        ax,
        data: xr.DataArray,
        step: float | None = None,
        vmin: float | None = None,
        vmax: float | None = None,
        color: str = "black",
        linewidths: float = 1.0,
        linestyle: str = "-",
        sigma: float | None = None,
    ) -> None:
        """Add contour lines with inline labels."""
        lon = data["longitude"]
        lat = data["latitude"]
        values = data.values.copy()
        if sigma is not None:
            values = gaussian_filter(values, sigma=sigma)

        vmin = vmin if vmin is not None else float(values.min())
        vmax = vmax if vmax is not None else float(values.max())
        step = step if step is not None else (vmax - vmin) / 10
        levels = np.arange(vmin, vmax + step, step)

        cs = ax.contour(
            lon, lat, values,
            levels=levels, colors=color,
            transform=ccrs.PlateCarree(), linewidths=linewidths,
        )
        ax.clabel(cs, inline=True, fontsize=8, fmt="%1.0f")

    # ------------------------------------------------------------------
    # Annotations
    # ------------------------------------------------------------------

    def add_min_max_points(
        self,
        ax,
        data: xr.DataArray,
        min_letter: str = "N",
        min_letter_color: str = "red",
        max_letter: str = "W",
        max_letter_color: str = "blue",
        fontsize: int = 20,
    ) -> None:
        """Mark pressure minima and maxima with letters and values."""
        lon = data["longitude"].values
        lat = data["latitude"].values
        param = data.values
        smooth = gaussian_filter(param, sigma=3.0)

        min_mask = smooth == minimum_filter(smooth, size=40)
        max_mask = smooth == maximum_filter(smooth, size=80)
        min_y, min_x = np.where(min_mask)
        max_y, max_x = np.where(max_mask)

        src = ccrs.PlateCarree()
        proj = ax.projection
        effects = [pe.Stroke(linewidth=2.5, foreground="white"), pe.Normal()]

        x0, x1, y0, y1 = ax.get_extent()
        _, ymax = ax.get_ylim()
        _, xmax = ax.get_xlim()
        xmin, _ = ax.get_xlim()
        ymin, _ = ax.get_ylim()
        dy = ymax - ymin
        dx = xmax - xmin

        def _draw(xy, vals, letter, color):
            xp, yp = xy[:, 0], xy[:, 1]
            ok = (xp >= x0) & (xp <= x1) & (yp >= y0) & (yp <= y1)
            for x, y, v in zip(xp[ok], yp[ok], vals[ok]):
                ax.text(x, y, "x", color=color, fontsize=fontsize * 0.5,
                        ha="center", va="center", zorder=20, path_effects=effects)
                ax.text(x, y + dx * 0.015, letter, color=color, fontsize=fontsize,
                        fontweight="bold", ha="center", va="center", zorder=20,
                        path_effects=effects)
                ax.text(x, y - dy * 0.01, f"{v:.0f}", color=color,
                        fontsize=fontsize * 0.6, ha="center", va="top",
                        zorder=20, path_effects=effects)

        xlim, ylim = ax.get_xlim(), ax.get_ylim()
        _draw(
            proj.transform_points(src, lon[min_x], lat[min_y]),
            param[min_y, min_x], min_letter, min_letter_color,
        )
        _draw(
            proj.transform_points(src, lon[max_x], lat[max_y]),
            param[max_y, max_x], max_letter, max_letter_color,
        )
        ax.set_xlim(xlim)
        ax.set_ylim(ylim)

    def add_hatched_area(
        self,
        ax,
        mask: xr.DataArray,
        hatch: str = "///",
        zorder: int = 12,
    ) -> None:
        """Overlay diagonal hatching for a boolean DataArray mask."""
        data = mask.astype(int)
        cs = ax.contourf(
            data.longitude, data.latitude, data.values,
            levels=[0.5, 1.5], colors="none", hatches=[hatch],
            transform=ccrs.PlateCarree(), zorder=zorder,
        )
        cs.set_edgecolor("none")
        cs.set_linewidth(0.0)
        cs.set_hatch_linewidth(1.2)

    def add_symbol_grid(
        self,
        ax,
        mask: xr.DataArray,
        min_dist_km: float = 150,
        color: str = "green",
        size: float = 30,
        zorder: int = 15,
    ) -> None:
        """Plot thinned point symbols at valid positions in the mask."""
        lon2d, lat2d = np.meshgrid(mask.longitude.values, mask.latitude.values)
        valid = mask.values
        if not np.any(valid):
            return

        lons = lon2d[valid]
        lats = lat2d[valid]
        xy = ax.projection.transform_points(ccrs.PlateCarree(), lons, lats)
        x, y = xy[:, 0], xy[:, 1]

        pts = np.column_stack([x, y])
        tree = cKDTree(pts)
        used = np.zeros(len(pts), dtype=bool)
        keep: list[int] = []
        for i in range(len(pts)):
            if used[i]:
                continue
            keep.append(i)
            used[tree.query_ball_point(pts[i], min_dist_km * 1000.0)] = True

        ax.scatter(
            x[keep], y[keep], s=size, c=color,
            marker="o", edgecolors="white", linewidths=0.6, zorder=zorder,
        )

    # ------------------------------------------------------------------
    # Labels / colorbar
    # ------------------------------------------------------------------

    def add_colorbar(self, ax, cs, label: str, fontsize: int = 11):
        """Add a horizontal colorbar inside the map above the source bar."""
        # Dark backing strip for the colorbar zone
        ax.add_patch(mpatches.Rectangle(
            (0, _BAR_H), 1, _CBAR_BG,
            transform=ax.transAxes, facecolor=_OVL_BG,
            alpha=_OVL_A, zorder=88, clip_on=False,
        ))
        cax = ax.inset_axes([0.01, _CBAR_Y, 0.98, _CBAR_H], zorder=90)
        cbar = plt.colorbar(cs, cax=cax, orientation="horizontal")
        # Label above the bar
        cbar.ax.set_title(label, fontsize=fontsize - 1, color=_FG, pad=3)
        cbar.ax.tick_params(labelsize=fontsize - 3, colors=_FG, labelcolor=_FG, width=1.2)
        cbar.ax.set_facecolor("none")
        cbar.outline.set_edgecolor(_FG)
        cbar.outline.set_linewidth(0.5)
        # Tick labels render below the inset bounds — disable clipping so they're visible
        cax.set_clip_on(False)
        for label in cbar.ax.get_xticklabels():
            label.set_color(_FG)
            label.set_fontweight("bold")
            label.set_clip_on(False)
        return cbar

    def add_title(self, ax, valid_time, prefix: str = "") -> None:
        """Draw a semi-transparent top bar with parameter name and valid time."""
        dt = pd.to_datetime(valid_time).to_pydatetime()
        time_str = dt.strftime("%Y-%m-%d  %H:%M UTC")

        ax.add_patch(mpatches.Rectangle(
            (0, 1 - _BAR_H), 1, _BAR_H,
            transform=ax.transAxes, facecolor=_OVL_BG,
            alpha=_OVL_A, zorder=95, clip_on=False,
        ))
        y_center = 1 - _BAR_H / 2
        if prefix:
            ax.text(
                0.012, y_center, prefix,
                transform=ax.transAxes, color=_FG,
                fontsize=10, fontweight="bold", va="center", ha="left",
                zorder=96, clip_on=False,
            )
        ax.text(
            0.988, y_center, time_str,
            transform=ax.transAxes, color=_FG_DIM,
            fontsize=10, va="center", ha="right",
            zorder=96, clip_on=False,
        )

    def add_source_label(self, ax, text: str = _ATTR) -> None:
        """Draw a semi-transparent bottom attribution bar."""
        ax.add_patch(mpatches.Rectangle(
            (0, 0), 1, _BAR_H,
            transform=ax.transAxes, facecolor=_OVL_BG,
            alpha=_OVL_A, zorder=95, clip_on=False,
        ))
        ax.text(
            0.5, _BAR_H * 0.38, text,
            transform=ax.transAxes, color=_FG,
            fontsize=8.5, alpha=0.92, va="center", ha="center",
            zorder=96, clip_on=False, linespacing=1.4,
        )
