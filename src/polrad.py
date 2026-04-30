import h5py
import json
import os
import re
from datetime import datetime

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
from matplotlib.colors import BoundaryNorm, ListedColormap, Normalize
from mpl_toolkits.axes_grid1 import make_axes_locatable
import numpy as np
from pyproj import Proj, Transformer

# ─────────────────────────────────────────────────────────
#  Dekodowanie pliku HDF5 z IMGW
# ─────────────────────────────────────────────────────────

def decode_h5_file(file_path, output_projection="EPSG:4326"):
    """Wczytuje plik HDF5 IMGW i zwraca słownik z siatką lon/lat oraz danymi radarowymi."""
    with h5py.File(file_path, "r") as f:
        datasets = [k for k in f.keys() if "dataset" in k.lower()]

        # Metadane z pierwszego datasetu
        first_ds = datasets[0]
        what_ds = f[f"{first_ds}/what"]

        # Lokalizacja siatki (where) może być globalna lub per-dataset
        where = f.get("where")
        if where is None or "xsize" not in where.attrs:
            where = f[f"{first_ds}/where"]

        how = f["how"]

        quantity = what_ds.attrs["quantity"].decode()
        product  = what_ds.attrs["product"].decode()
        system   = how.attrs["system"].decode()

        startdate = what_ds.attrs["startdate"].decode()
        starttime = what_ds.attrs["starttime"].decode()
        start_date = datetime.strptime(f"{startdate}{starttime}", "%Y%m%d%H%M%S")

        lon_mesh, lat_mesh, lonlat_2_radar = _build_lonlat_mesh(where, output_projection)

        radar_data = {}
        for ds_name in datasets:
            dset = f[f"/{ds_name}/data1/data"]
            radar_data[ds_name] = _decode_radar_array(dset, what_ds)

    return {
        "radar_data":     radar_data,
        "lon_mesh":       lon_mesh,
        "lat_mesh":       lat_mesh,
        "lonlat_2_radar": lonlat_2_radar,
        "quantity":       quantity,
        "product":        product,
        "system":         system,
        "start_date":     start_date,
    }


def _decode_radar_array(dset, what_ds):
    """Przelicza surowe piksele HDF5 na wartości fizyczne (dBZ, mm/h itp.)."""
    data = dset[:].astype(float)

    nodata   = what_ds.attrs["nodata"]
    undetect = what_ds.attrs["undetect"]
    gain     = what_ds.attrs["gain"]
    offset   = what_ds.attrs["offset"]

    data[data == nodata]   = np.nan
    data[data == undetect] = np.nan   # traktuj undetect jak brak danych (transparentne)
    data = data * gain + offset

    return np.flipud(data)


def _build_lonlat_mesh(where, output_projection="EPSG:4326"):
    """Buduje siatkę lon/lat z metadanych HDF5 metodą interpolacji dwuliniowej."""
    UL_lon, UL_lat = where.attrs["UL_lon"], where.attrs["UL_lat"]
    UR_lon, UR_lat = where.attrs["UR_lon"], where.attrs["UR_lat"]
    LL_lon, LL_lat = where.attrs["LL_lon"], where.attrs["LL_lat"]
    LR_lon, LR_lat = where.attrs["LR_lon"], where.attrs["LR_lat"]

    xsize = int(where.attrs["xsize"])
    ysize = int(where.attrs["ysize"])

    projdef = where.attrs["projdef"].decode()
    # Zamiana przestarzałego +ellps=sphere na równoważne parametry pyproj
    projdef = projdef.replace("+ellps=sphere", "+R=6378137 +nadgrids=@null +no_defs")

    lonlat_to_radar = Transformer.from_proj(Proj("EPSG:4326"), Proj(projdef), always_xy=True)
    radar_to_out    = Transformer.from_proj(Proj(projdef), Proj(output_projection), always_xy=True)

    # Narożniki w układzie radaru
    corners_in = [(LL_lon, LL_lat), (LR_lon, LR_lat), (UL_lon, UL_lat), (UR_lon, UR_lat)]
    (LL_x, LL_y), (LR_x, LR_y), (UL_x, UL_y), (UR_x, UR_y) = [
        lonlat_to_radar.transform(lon, lat) for lon, lat in corners_in
    ]

    # Interpolacja dwuliniowa na regularną siatkę pikseli
    u = np.linspace(0, 1, xsize + 1)
    v = np.linspace(0, 1, ysize + 1)
    U, V = np.meshgrid(u, v)

    X = (1-U)*(1-V)*LL_x + U*(1-V)*LR_x + (1-U)*V*UL_x + U*V*UR_x
    Y = (1-U)*(1-V)*LL_y + U*(1-V)*LR_y + (1-U)*V*UL_y + U*V*UR_y

    lon_mesh, lat_mesh = radar_to_out.transform(X, Y)
    return lon_mesh, lat_mesh, lonlat_to_radar


# ─────────────────────────────────────────────────────────
#  Palety kolorów
# ─────────────────────────────────────────────────────────

def color_palette(quantity, ctype="imgw", PROJECT_PATH=None):
    """Zwraca (cmap, norm, label) dla danej wielkości i stylu palety."""
    if ctype == "noaa" and PROJECT_PATH:
        pal_path = os.path.join(PROJECT_PATH, "data", "color_tables", f"{quantity}.pal")
        if os.path.exists(pal_path):
            cmap, norm = load_pal_file(pal_path)
            labels = {
                "DBZH":  "Odbiciowość [dBZ]",
                "VRADH": "Wiatr radialny [m/s]",
                "RHOHV": "Współczynnik korelacji",
                "KDP":   "KDP [°/km]",
                "ZDR":   "ZDR [dB]",
            }
            return cmap, norm, labels.get(quantity, quantity)

    if ctype in ("imgw", "noaa"):
        return _palette_imgw(quantity)
    if ctype == "nexrad":
        return _palette_nexrad(quantity)

    raise ValueError(f"Nieznany typ palety: {ctype}")


def _palette_imgw(quantity):
    if quantity == "DBZH":
        colors = [
            "#0000c8","#0051eb","#0079fb","#31c8ff","#5fe0fd","#78ebfe",
            "#fffffd","#fff9d4","#fef6c4","#ffe501","#fe9702","#fe7300",
            "#ff3f00","#dc1500","#c80000","#800000","#8F0056","#C301A2","#FF00FF",
        ]
        cmap = ListedColormap(colors)
        cmap.set_under((0, 0, 0, 0))
        cmap.set_bad((0, 0, 0, 0))
        return cmap, Normalize(vmin=5.0, vmax=65.0), "Odbiciowość [dBZ]"

    if quantity == "RATE":
        bounds_log = np.linspace(-2, 1.8, 24)
        bounds = 10 ** bounds_log
        colors = [
            "#d4f0ff","#a0d8f0","#70c0e8","#40a8e0","#1e90d8","#00c8a0",
            "#40e060","#a0f000","#ffff00","#ffd000","#ff9900","#ff6600",
            "#ff3300","#e00000","#b00000","#800000","#c000c0","#9900cc",
            "#6600bb","#d4b0f0","#e8d0f8","#c8c8c8","#909090",
        ]
        cmap = ListedColormap(colors)
        cmap.set_under((0, 0, 0, 0))
        cmap.set_bad((0, 0, 0, 0))
        return cmap, BoundaryNorm(bounds, ncolors=len(colors)), "Natężenie opadu [mm/h]"

    # Dla pozostałych ilości deleguj do nexrad
    return _palette_nexrad(quantity)


def _palette_nexrad(quantity):
    _jet = lambda n: plt.cm.jet(np.linspace(0, 1, n))[:, :3]

    specs = {
        "ZDR":   (Normalize(vmin=-1,  vmax=3),   "Odbiciowość różnicowa ZDR [dB]"),
        "RHOHV": (Normalize(vmin=0.7, vmax=1.0),  "Współczynnik korelacji RHOHV"),
        "KDP":   (Normalize(vmin=-1,  vmax=3),   "KDP [°/km]"),
        "HGHT":  (Normalize(vmin=0,   vmax=15),  "Wysokość wierzchołków echa [km]"),
        "PHIDP": (Normalize(vmin=0,   vmax=180), "PHIDP [°]"),
        "SPEED": (Normalize(vmin=0,   vmax=30),  "Wiatr radialny [m/s]"),
    }

    if quantity in specs:
        norm, label = specs[quantity]
        cmap = ListedColormap(_jet(256))
        cmap.set_under((0, 0, 0, 0))
        cmap.set_bad((0, 0, 0, 0))
        return cmap, norm, label

    if quantity in ("VRADH", "DBZH"):
        # Duże tablice kolorów NEXRAD — zachowane z oryginału
        return _palette_nexrad_full(quantity)

    raise ValueError(f"Nieznana wielkość: {quantity}")


def _palette_nexrad_full(quantity):
    """Pełne palety kolorów NEXRAD dla VRADH i DBZH (512+ wpisów)."""
    if quantity == "VRADH":
        raw = np.array([[151,11,124],[124,4,149],[123,4,149],[120,5,149],[120,4,149],[118,4,149],[117,4,150],[115,4,150],[114,5,151],[112,5,150],[110,4,151],[109,4,150],[108,4,151],[107,4,151],[105,5,151],[102,4,150],[102,4,152],[101,4,152],[99,5,152],[97,4,152],[96,5,152],[94,5,152],[93,5,153],[89,4,153],[86,4,153],[84,4,152],[81,4,152],[77,5,152],[75,4,152],[72,4,153],[69,5,153],[66,3,152],[63,2,152],[61,3,153],[57,3,153],[54,3,153],[51,3,152],[48,2,153],[45,3,153],[42,2,153],[38,2,153],[36,3,153],[33,2,153],[30,2,152],[28,2,154],[28,7,155],[28,12,156],[28,16,157],[28,21,158],[29,25,161],[28,31,162],[28,34,163],[29,38,164],[28,42,166],[29,47,167],[29,51,169],[29,56,170],[29,60,172],[29,65,173],[29,69,174],[29,74,176],[29,79,177],[31,83,179],[29,87,180],[30,91,182],[30,96,183],[30,101,185],[31,106,187],[32,113,189],[33,118,191],[34,124,193],[35,130,194],[35,136,196],[36,141,199],[37,147,201],[37,153,202],[38,159,205],[39,164,206],[40,171,209],[41,176,210],[42,182,213],[42,188,215],[44,194,217],[44,198,219],[45,205,220],[46,211,222],[47,217,224],[47,222,227],[50,224,227],[53,224,227],[57,225,228],[60,225,228],[63,225,228],[66,225,228],[69,226,229],[72,226,229],[76,227,230],[79,227,230],[82,227,230],[84,227,230],[88,228,231],[91,228,231],[95,229,232],[98,229,232],[101,229,232],[104,229,232],[108,230,233],[110,230,233],[114,230,233],[117,232,234],[120,231,234],[123,231,234],[126,232,235],[129,232,235],[133,232,235],[135,232,234],[139,233,236],[141,233,236],[144,234,237],[147,234,237],[151,234,237],[153,234,237],[156,235,238],[158,235,238],[162,236,239],[165,236,239],[168,236,239],[171,236,239],[174,236,240],[177,237,240],[180,239,241],[181,238,239],[173,239,229],[167,239,220],[160,240,212],[154,240,203],[148,241,194],[142,242,186],[136,243,177],[129,243,168],[123,244,160],[117,244,150],[111,245,142],[104,245,133],[98,246,125],[92,247,117],[86,248,108],[79,248,99],[73,249,90],[67,249,81],[61,250,72],[54,251,64],[48,253,55],[44,250,50],[43,248,48],[41,246,46],[39,244,44],[36,241,41],[35,239,38],[32,237,37],[31,235,35],[29,232,32],[27,231,30],[25,228,28],[23,227,26],[21,223,23],[19,221,21],[17,219,19],[15,217,16],[13,214,14],[11,212,12],[8,210,10],[7,208,9],[5,205,5],[3,203,3],[3,200,3],[3,198,2],[3,196,3],[2,192,3],[2,191,2],[2,188,2],[2,186,2],[2,184,2],[2,181,2],[2,179,2],[2,176,2],[2,174,2],[3,170,2],[2,168,2],[2,166,2],[2,164,3],[2,161,2],[2,159,2],[2,156,3],[2,154,2],[2,153,2],[3,150,2],[2,147,2],[2,145,2],[2,142,2],[2,141,2],[2,138,2],[3,136,2],[2,134,2],[3,132,2],[3,129,2],[3,126,2],[2,125,2],[3,123,2],[2,120,2],[4,118,2],[4,116,2],[4,114,2],[4,113,2],[4,111,2],[4,107,2],[5,105,3],[4,103,3],[7,102,5],[13,103,10],[19,105,15],[25,104,20],[32,105,26],[37,105,31],[44,106,36],[50,107,42],[56,108,47],[62,109,52],[68,109,58],[74,111,63],[80,111,68],[86,111,73],[93,113,79],[99,113,84],[105,114,89],[111,114,94],[117,115,100],[123,116,105],[129,117,111],[136,117,116],[138,118,118],[138,117,118],[138,117,119],[138,117,119],[139,116,120],[138,117,120],[138,117,121],[138,116,120],[138,116,122],[139,116,123],[138,117,123],[138,114,123],[138,115,125],[138,115,124],[138,115,125],[139,114,125],[138,115,126],[138,114,127],[138,115,127],[139,114,127],[139,114,128],[138,113,128],[137,109,124],[135,104,116],[135,99,112],[133,93,105],[132,88,100],[130,83,94],[129,78,89],[127,73,83],[126,67,76],[124,62,70],[124,57,64],[122,51,59],[121,46,52],[120,41,46],[119,36,40],[116,30,34],[116,25,29],[114,20,22],[113,15,17],[113,9,11],[111,4,5],[110,0,0],[113,1,0],[115,0,0],[118,0,1],[120,0,0],[123,0,0],[125,0,0],[128,0,0],[130,0,0],[133,0,0],[135,0,0],[138,0,0],[140,0,0],[142,0,0],[145,0,0],[148,0,0],[150,1,0],[152,0,0],[154,0,0],[158,0,0],[161,0,0],[163,0,1],[166,0,0],[169,0,0],[171,0,0],[174,0,0],[177,0,0],[180,1,1],[182,0,0],[186,0,1],[189,0,0],[192,0,0],[195,0,0],[197,0,0],[201,0,0],[203,0,1],[206,0,0],[208,0,0],[211,0,0],[214,0,0],[217,0,1],[220,0,0],[223,0,0],[226,0,0],[226,2,4],[229,6,11],[230,10,16],[231,15,22],[232,18,27],[233,22,34],[234,27,38],[235,31,46],[236,34,51],[236,38,58],[238,42,63],[240,47,70],[240,50,75],[242,54,82],[242,58,87],[244,63,94],[245,66,99],[246,71,105],[247,74,111],[249,79,116],[249,82,123],[251,87,129],[251,90,133],[251,92,136],[251,96,139],[251,100,143],[251,102,146],[252,106,150],[253,108,153],[252,113,156],[252,116,160],[253,120,163],[253,123,166],[252,126,170],[253,129,173],[253,133,177],[253,137,180],[254,140,183],[254,143,186],[254,146,190],[254,149,192],[255,153,197],[254,157,200],[254,160,203],[255,163,200],[255,166,198],[255,168,197],[255,172,194],[255,175,192],[255,178,190],[255,181,188],[254,184,186],[255,188,184],[255,191,182],[255,194,181],[254,198,179],[255,200,176],[255,204,174],[255,207,172],[254,210,170],[254,213,168],[255,217,166],[255,219,164],[255,223,162],[255,226,161],[255,226,158],[255,225,156],[255,223,154],[255,221,152],[255,219,150],[255,217,148],[254,215,146],[255,213,144],[255,211,142],[255,208,141],[255,207,139],[254,204,137],[255,203,135],[255,200,132],[255,199,131],[255,196,129],[255,194,127],[255,192,125],[255,190,123],[255,188,121],[255,187,119],[255,184,117],[255,182,116],[254,180,113],[254,178,112],[254,175,110],[254,173,108],[254,171,106],[254,169,105],[252,166,103],[254,164,101],[253,162,99],[253,160,99],[253,157,97],[253,155,94],[253,153,92],[253,151,90],[252,148,88],[253,147,87],[252,144,85],[253,143,83],[252,139,81],[253,137,80],[251,134,78],[250,133,77],[248,132,76],[247,130,75],[245,129,75],[244,128,75],[242,126,72],[240,125,72],[238,123,71],[237,122,71],[235,120,69],[234,119,69],[232,117,68],[230,117,67],[228,114,66],[227,113,65],[225,112,64],[224,110,64],[222,109,63],[221,108,62],[219,106,61],[216,105,60],[216,103,60],[214,102,58],[211,100,56],[210,99,57],[209,96,57],[206,96,55],[204,95,54],[203,92,53],[201,90,52],[199,89,51],[197,86,50],[196,86,48],[194,84,48],[192,82,48],[190,81,46],[188,80,47],[186,79,45],[185,76,44],[183,75,42],[181,74,42],[179,72,41],[178,71,40]])
        norm = Normalize(vmin=-33, vmax=30)
        label = "Wiatr radialny [m/s]"
    else:  # DBZH
        raw = np.array([[141,129,127],[143,130,126],[142,131,122],[143,132,122],[142,131,119],[143,133,118],[143,133,117],[145,134,115],[144,134,112],[145,134,111],[144,135,108],[146,137,108],[145,137,105],[146,138,104],[147,139,102],[147,139,101],[147,139,98],[148,140,97],[147,141,95],[148,142,93],[148,142,91],[149,143,90],[148,143,86],[150,145,86],[149,144,83],[151,146,84],[151,146,87],[153,149,88],[153,149,89],[155,151,92],[156,152,93],[158,154,97],[158,154,97],[160,156,100],[160,156,100],[162,159,104],[162,161,105],[164,162,107],[165,163,109],[167,165,111],[167,165,112],[169,167,115],[169,168,116],[170,170,119],[171,170,120],[173,172,123],[174,173,124],[176,175,127],[176,176,128],[178,179,130],[178,178,132],[180,180,134],[181,181,135],[183,183,138],[184,184,140],[186,186,142],[186,186,143],[188,188,146],[189,189,147],[190,191,150],[191,192,151],[192,194,154],[194,194,154],[196,196,158],[196,197,159],[198,198,163],[199,200,163],[201,202,167],[201,203,167],[203,205,170],[204,206,171],[206,209,174],[206,208,175],[209,210,177],[209,211,179],[211,213,180],[208,210,180],[207,209,180],[205,208,180],[204,207,180],[202,205,180],[201,204,180],[199,203,180],[198,202,180],[197,200,181],[196,200,180],[194,198,180],[193,196,180],[191,195,180],[191,195,180],[189,192,180],[188,193,180],[186,191,180],[185,190,180],[182,188,180],[183,188,180],[180,186,180],[180,185,180],[178,183,180],[177,183,180],[175,181,181],[175,181,180],[173,179,180],[173,179,181],[170,177,180],[170,176,180],[168,175,181],[168,174,181],[166,172,180],[166,173,181],[164,171,180],[164,171,181],[162,168,180],[161,168,180],[159,167,180],[159,166,181],[157,164,181],[157,162,181],[154,162,181],[155,161,181],[153,159,181],[153,159,180],[150,157,181],[150,157,181],[148,155,181],[147,155,181],[145,153,180],[144,152,180],[141,149,179],[139,149,179],[137,147,178],[136,147,178],[133,143,177],[133,143,177],[129,140,176],[128,140,176],[125,136,175],[124,137,175],[121,135,174],[121,134,174],[117,132,172],[116,130,172],[113,129,173],[112,128,172],[109,126,170],[108,125,171],[106,123,170],[104,122,170],[101,120,168],[100,118,168],[98,117,169],[98,116,168],[95,115,167],[95,115,166],[93,113,166],[92,112,166],[90,111,165],[90,110,166],[88,110,165],[87,109,165],[85,107,164],[85,107,164],[82,106,162],[82,104,164],[80,104,162],[78,103,163],[78,102,162],[77,103,162],[75,100,161],[74,100,161],[73,98,160],[72,99,161],[70,97,159],[69,97,160],[67,94,158],[68,96,160],[68,98,161],[70,102,165],[70,105,164],[72,109,168],[72,111,169],[74,115,172],[75,116,173],[76,121,176],[77,124,177],[79,127,179],[79,130,181],[81,134,183],[81,137,184],[83,140,186],[83,143,189],[85,146,191],[84,149,192],[87,153,195],[89,155,196],[90,159,198],[90,161,200],[92,165,203],[92,169,204],[94,172,207],[93,173,206],[94,176,204],[92,177,202],[93,179,201],[91,180,198],[92,183,197],[91,183,195],[91,186,194],[89,186,191],[90,189,190],[89,190,189],[89,192,187],[88,193,184],[88,195,183],[87,197,181],[86,199,180],[86,200,177],[86,202,176],[85,203,173],[85,205,172],[84,206,170],[84,209,169],[83,210,166],[83,212,165],[83,213,163],[81,214,159],[78,214,153],[76,214,148],[72,214,142],[70,214,137],[67,214,131],[64,215,127],[61,214,119],[59,214,115],[56,214,108],[55,214,103],[51,214,96],[48,214,90],[45,214,85],[43,214,80],[39,214,73],[37,214,69],[34,214,62],[32,214,57],[28,214,51],[27,214,46],[23,214,40],[21,214,35],[19,215,29],[16,215,23],[15,212,20],[14,210,20],[13,206,19],[14,203,20],[13,200,19],[14,197,19],[13,193,18],[13,191,19],[13,186,18],[13,184,19],[12,181,16],[13,178,18],[12,175,17],[13,172,18],[12,168,17],[12,166,17],[12,162,16],[12,160,17],[11,156,16],[12,153,17],[10,150,16],[12,146,16],[11,143,14],[11,141,16],[11,137,15],[11,136,14],[10,133,14],[11,132,15],[11,130,14],[10,128,14],[10,127,13],[11,125,14],[10,123,13],[11,122,13],[10,120,12],[10,119,13],[10,116,12],[10,115,12],[9,113,11],[10,112,13],[8,110,10],[10,109,11],[9,106,10],[10,104,11],[9,103,10],[10,102,11],[9,99,9],[9,98,10],[9,96,9],[9,95,10],[13,97,8],[24,102,9],[33,106,8],[44,113,8],[53,117,7],[63,123,7],[73,129,7],[83,134,7],[93,138,5],[102,145,6],[112,149,4],[123,155,5],[132,160,4],[142,166,4],[151,170,3],[163,176,5],[171,180,3],[182,187,3],[191,191,2],[201,196,2],[210,202,1],[221,208,2],[230,212,1],[241,219,1],[251,223,0],[254,225,1],[253,222,3],[253,222,5],[252,219,6],[251,218,9],[250,214,10],[250,215,12],[248,210,13],[248,210,16],[247,208,17],[247,207,20],[244,204,21],[245,203,23],[243,201,25],[243,199,27],[242,196,28],[241,194,31],[240,193,33],[240,192,35],[238,189,35],[239,188,39],[237,185,39],[237,184,42],[235,180,43],[235,180,45],[234,179,45],[236,179,44],[236,178,41],[237,179,40],[237,178,37],[239,179,36],[238,178,35],[241,179,33],[241,178,30],[242,178,29],[242,178,27],[245,178,25],[244,178,23],[246,178,22],[246,177,19],[247,178,18],[248,177,15],[249,179,15],[248,177,11],[251,178,10],[251,177,8],[253,178,7],[253,177,4],[254,177,3],[254,177,1],[255,1,1],[249,0,1],[246,2,2],[243,3,3],[239,3,4],[234,3,3],[231,4,5],[227,5,4],[224,6,6],[219,6,7],[216,6,7],[212,7,8],[209,8,9],[204,9,9],[201,11,11],[196,10,10],[194,11,12],[189,11,11],[187,12,13],[183,12,13],[179,14,14],[175,13,14],[172,15,16],[167,15,16],[164,16,17],[162,15,17],[163,15,16],[163,15,14],[165,14,15],[165,13,13],[166,13,14],[166,12,12],[167,12,12],[167,10,11],[168,11,11],[168,9,10],[169,9,10],[169,8,8],[170,8,8],[170,6,7],[172,7,7],[171,5,5],[173,5,5],[173,4,4],[174,4,4],[175,2,3],[175,3,3],[174,1,1],[177,1,2],[176,0,1],[255,252,255],[253,247,255],[253,242,255],[251,235,255],[250,231,255],[248,224,254],[248,219,255],[246,212,254],[246,208,254],[244,202,254],[244,197,254],[242,191,253],[241,186,255],[240,180,253],[238,175,254],[237,168,252],[237,164,253],[235,157,253],[235,153,252],[233,146,252],[232,141,253],[231,135,252],[230,130,253],[228,125,252],[228,119,252],[227,116,252],[229,117,253],[229,116,252],[231,116,252],[232,116,252],[234,117,253],[234,116,252],[236,117,253],[236,116,253],[238,117,254],[238,116,253],[240,117,254],[240,117,252],[243,117,255],[242,116,253],[244,117,254],[245,116,254],[247,117,255],[247,116,254],[249,117,255],[250,116,254],[252,116,255],[252,116,254],[254,117,255],[254,116,255],[170,0,253],[167,1,249],[164,0,248],[160,0,246],[158,0,244],[152,0,242],[151,0,241],[147,1,239],[144,0,239],[140,0,236],[137,0,235],[134,0,233],[131,1,230],[127,1,228],[124,0,228],[120,0,226],[118,0,225],[112,0,222],[110,0,221],[107,0,219],[104,0,219],[100,1,215],[99,0,215],[94,0,212],[91,0,211]])
        norm = Normalize(vmin=-30, vmax=70)
        label = "Odbiciowość [dBZ]"

    colors = raw / 255.0
    cmap = ListedColormap(colors)
    cmap.set_under((0, 0, 0, 0))
    cmap.set_bad((0, 0, 0, 0))
    return cmap, norm, label


def load_pal_file(pal_path):
    """Wczytuje plik .pal (GR2Analyst/NOAA) i zwraca (cmap, norm)."""
    values, colors_start, colors_end = [], [], []
    scale = 1.0

    with open(pal_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            upper = line.upper()

            if upper.startswith("SCALE:"):
                scale = float(line.split()[1])
                continue
            if upper.startswith(("RF:", "NF:")):
                continue

            if upper.startswith("COLOR4:"):
                parts = line.split()
                val = float(parts[1]) / scale
                r, g, b, a = [min(int(x), 255) for x in parts[2:6]]
                values.append(val)
                colors_start.append((r/255, g/255, b/255, a/255))
                colors_end.append(None)

            elif re.match(r"(?i)^color:", line):
                parts = line.split()
                val = float(parts[1]) / scale
                r1, g1, b1 = [min(int(x), 255) for x in parts[2:5]]
                if len(parts) >= 8:
                    r2, g2, b2 = [min(int(x), 255) for x in parts[5:8]]
                else:
                    r2, g2, b2 = r1, g1, b1
                values.append(val)
                colors_start.append((r1/255, g1/255, b1/255, 1.0))
                colors_end.append((r2/255, g2/255, b2/255, 1.0))

    if not values:
        raise ValueError(f"Brak wpisów color w: {pal_path}")

    order = np.argsort(values)
    values       = [values[i]       for i in order]
    colors_start = [colors_start[i] for i in order]
    colors_end   = [colors_end[i]   for i in order]

    N = 512
    v_min, v_max = values[0], values[-1]
    rgba = np.zeros((N, 4))
    for i in range(N):
        v   = v_min + (v_max - v_min) * i / (N - 1)
        idx = int(np.clip(np.searchsorted(values, v, side="right") - 1, 0, len(values) - 2))
        v0, v1 = values[idx], values[idx + 1]
        t  = (v - v0) / (v1 - v0) if v1 != v0 else 0.0
        c0 = colors_start[idx]
        c1 = colors_end[idx] if colors_end[idx] is not None else colors_start[idx + 1]
        rgba[i] = [c0[j] + t * (c1[j] - c0[j]) for j in range(4)]

    cmap = ListedColormap(rgba)
    cmap.set_under((0, 0, 0, 0))
    cmap.set_bad((0, 0, 0, 0))
    return cmap, Normalize(vmin=v_min, vmax=v_max)


# ─────────────────────────────────────────────────────────
#  Renderowanie wykresów
# ─────────────────────────────────────────────────────────

def _radar_extent(radar_data):
    """Zwraca (lon_min, lon_max, lat_min, lat_max) dla danych radarowych."""
    if radar_data["system"] == "POLCOMP":
        return 14.12, 24.15, 49.00, 54.84
    lon = radar_data["lon_mesh"]
    lat = radar_data["lat_mesh"]
    return float(lon.min()), float(lon.max()), float(lat.min()), float(lat.max())


def plot_image(radar_data, output_file, gdf_shp_1, gdf_shp_2,
               extent=None, dpi=100, width=20, height=20,
               ctype="imgw", PROJECT_PATH=None):
    """Generuje statyczny obraz PNG z siatką, granicami i legendą."""
    fig, ax = plt.subplots(figsize=(width, height), dpi=dpi)
    ax.set_facecolor("#A8A8A8")

    cmap, norm, label = color_palette(radar_data["quantity"], ctype=ctype, PROJECT_PATH=PROJECT_PATH)
    data = radar_data["radar_data"]["dataset1"]

    ax.pcolormesh(
        radar_data["lon_mesh"], radar_data["lat_mesh"], data,
        cmap=cmap, norm=norm, shading="flat",
    )
    ax.grid(True, color="white", linewidth=0.5, alpha=0.4, linestyle="-")

    gdf_shp_1.plot(ax=ax, edgecolor="#111111", facecolor="none", linewidth=0.9)
    gdf_shp_2.plot(ax=ax, edgecolor="#555555", facecolor="none", linewidth=0.3)

    lon_min, lon_max, lat_min, lat_max = _radar_extent(radar_data)
    if extent:
        ax.set_xlim(extent[0], extent[2])
        ax.set_ylim(extent[1], extent[3])
    else:
        ax.set_xlim(lon_min, lon_max)
        ax.set_ylim(lat_min, lat_max)

    # Colorbar
    sm = plt.cm.ScalarMappable(cmap=cmap, norm=norm)
    sm.set_array([])
    divider = make_axes_locatable(ax)
    cax = divider.append_axes("right", size="3%", pad=0.12)
    cbar = plt.colorbar(sm, cax=cax)
    cbar.set_label(label, rotation=90, labelpad=8, fontsize=11, fontweight="bold")

    # Tytuł z datą i godziną
    dt = radar_data["start_date"]
    ax.set_title(
        f"{radar_data['quantity']}  {radar_data['product']}  "
        f"| {dt.strftime('%d-%m-%Y')} {dt.strftime('%H:%M')} UTC",
        fontsize=13, fontweight="bold",
    )

    plt.savefig(output_file, bbox_inches="tight", pad_inches=0.15)
    plt.close()


def render_web_overlay(radar_data, output_png, dataset_key="dataset1",
                       dpi=250, size=10):
    """
    Generuje przezroczysty PNG w projekcji EPSG:3857 do L.imageOverlay w Leaflet.

    Leaflet renderuje imageOverlay liniowo w Web Mercatorze (EPSG:3857).
    Żeby piksele pokrywały się z podkładem mapy, obraz musi być wygenerowany
    w tej samej projekcji — inaczej wystąpią błędy rozmieszczenia przy wysokich
    szerokościach geograficznych.

    radar_data musi być zdekodowany z output_projection="EPSG:3857"
    (patrz decode_h5_file).

    Zwraca:
      bounds: [[lat_sw, lon_sw], [lat_ne, lon_ne]]  (EPSG:4326, format Leaflet)
      timestamp, quantity, product, system
    """
    cmap, norm, _ = color_palette(radar_data["quantity"], ctype="imgw")
    data = radar_data["radar_data"][dataset_key]

    # Siatka w metrach EPSG:3857
    x_mesh = radar_data["lon_mesh"]
    y_mesh = radar_data["lat_mesh"]
    x_min, x_max = float(x_mesh.min()), float(x_mesh.max())
    y_min, y_max = float(y_mesh.min()), float(y_mesh.max())

    # Konwersja narożników do EPSG:4326 — potrzebne przez Leaflet bounds
    to_4326 = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
    lon_sw, lat_sw = to_4326.transform(x_min, y_min)
    lon_ne, lat_ne = to_4326.transform(x_max, y_max)

    # Figura bez marginesów — oś wypełnia całą figurę (kluczowe dla alignmentu)
    fig = plt.figure(figsize=(size, size), dpi=dpi, frameon=False)
    ax = fig.add_axes([0, 0, 1, 1])

    ax.pcolormesh(x_mesh, y_mesh, data, cmap=cmap, norm=norm, shading="flat")

    ax.set_aspect("auto")          # rozciągnij do krawędzi, nie zachowuj proporcji
    ax.set_xlim(x_min, x_max)
    ax.set_ylim(y_min, y_max)
    ax.axis("off")

    # pad_inches=0 i BEZ bbox_inches='tight' — zapisuje dokładnie figsize bez przycięcia
    plt.savefig(output_png, dpi=dpi, pad_inches=0, transparent=True)
    plt.close()

    return {
        "bounds":    [[lat_sw, lon_sw], [lat_ne, lon_ne]],
        "timestamp": radar_data["start_date"].strftime("%Y-%m-%dT%H:%M:%S"),
        "quantity":  radar_data["quantity"],
        "product":   radar_data["product"],
        "system":    radar_data["system"],
    }


def save_overlay_metadata(meta, json_path):
    """Zapisuje metadane overlayu do pliku JSON."""
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
