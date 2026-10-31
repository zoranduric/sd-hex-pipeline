"""Data sources. Everything here is licensed for commercial use with attribution.

See DATA_SOURCES.md for licences and required attribution text.
"""

from __future__ import annotations

import logging
import math
import os
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Protocol

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
import requests
from rasterio.enums import Resampling
from rasterio.errors import RasterioIOError, WindowError
from rasterio.windows import Window

from .sampling import SampleGrid

log = logging.getLogger(__name__)

COPDEM_URL = "https://copernicus-dem-90m.s3.amazonaws.com/{name}/{name}.tif"
WORLDCOVER_URL = (
    "https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/"
    "ESA_WorldCover_10m_2021_v200_{tile}_Map.tif"
)
NE_BASE = "https://naciscdn.org/naturalearth/10m"
NE_LAND = f"{NE_BASE}/physical/ne_10m_land.zip"
NE_LAKES = f"{NE_BASE}/physical/ne_10m_lakes.zip"
NE_ADMIN0 = f"{NE_BASE}/cultural/ne_10m_admin_0_countries.zip"
NE_ADMIN1 = f"{NE_BASE}/cultural/ne_10m_admin_1_states_provinces.zip"
HYDRORIVERS_URL = "https://data.hydrosheds.org/file/HydroRIVERS/HydroRIVERS_v10_{region}_shp.zip"
# Generous lon/lat boxes around each HydroSHEDS continental file. Their real
# borders follow river basins, so neighbouring boxes overlap on purpose: a bbox
# near a boundary loads both files (basins never appear in two files).
HYDRORIVERS_REGIONS: dict[str, tuple[tuple[float, float, float, float], ...]] = {
    "eu": ((-25.0, 12.0, 70.0, 82.0),),     # Europe + Middle East
    # Africa: the coast east of Tunisia stays below 33.5N, so Crete/Cyprus don't pull it in.
    "af": ((-26.0, -36.0, 12.0, 38.0), (12.0, -36.0, 56.0, 33.5)),
    "as": ((55.0, -12.0, 180.0, 56.0),),    # South/East/SE Asia
    "si": ((55.0, 45.0, 180.0, 82.0),),     # Siberia
    "au": ((90.0, -56.0, 180.0, 25.0),),    # Australasia + Pacific
    "na": ((-180.0, 5.0, -50.0, 72.0),),    # North + Central America
    "sa": ((-93.0, -56.0, -32.0, 15.0),),   # South America
    "ar": ((-180.0, 50.0, -10.0, 84.0),),   # North American Arctic
}


def hydrorivers_regions(bounds) -> list[str]:
    lon_min, lat_min, lon_max, lat_max = bounds
    return [
        k for k, boxes in HYDRORIVERS_REGIONS.items()
        if any(lon_min < c and lon_max > a and lat_min < d and lat_max > b for a, b, c, d in boxes)
    ]

# RESOLVE Ecoregions 2017 (CC BY 4.0), ~150 MB, used for biomes.
ECOREGIONS_URL = "https://storage.googleapis.com/teow2016/Ecoregions2017.zip"

ALLOWED_HOSTS = (
    "copernicus-dem-90m.s3.amazonaws.com",
    "esa-worldcover.s3.eu-central-1.amazonaws.com",
    "naciscdn.org",
    "data.hydrosheds.org",
    "storage.googleapis.com",
)
# Shared hosts are only trusted for specific path prefixes.
HOST_PATH_PREFIX = {"storage.googleapis.com": "/teow2016/"}

GDAL_ENV = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
    "GDAL_HTTP_MAX_RETRY": "4",
    "GDAL_HTTP_RETRY_DELAY": "2",
    "VSI_CACHE": "TRUE",
}

# How GDAL reports a tile that does not exist on the server.
_MISSING_MARKERS = ("404", "does not exist", "No such file")


class Sources(Protocol):
    """What the pipeline needs. Rasters come back already on the sample grid."""

    # ``need`` (optional bool raster on ``sg``) lets sources skip tiles with no land.
    def elevation(self, sg: SampleGrid, need: np.ndarray | None = None) -> np.ndarray: ...  # float32 m, NaN = none

    def landcover(self, sg: SampleGrid, need: np.ndarray | None = None) -> np.ndarray: ...  # uint8, 0 = none

    def biomes(self, bounds) -> gpd.GeoDataFrame: ...  # columns: biome_num (WWF 1-14), geometry

    def land(self, bounds) -> gpd.GeoDataFrame: ...

    def lakes(self, bounds) -> gpd.GeoDataFrame: ...

    def countries(self, bounds) -> gpd.GeoDataFrame: ...  # columns: code, name, geometry

    def admin1(self, bounds) -> gpd.GeoDataFrame: ...  # columns: code, name, country, geometry

    def rivers(self, bounds) -> gpd.GeoDataFrame: ...  # columns: discharge_cms, geometry


def _lat_tag(lat: int, width: int = 2) -> str:
    return f"{'N' if lat >= 0 else 'S'}{abs(lat):0{width}d}"


def _lon_tag(lon: int) -> str:
    return f"{'E' if lon >= 0 else 'W'}{abs(lon):03d}"


def copdem_tiles(bounds) -> list[tuple[str, tuple[int, int, int, int]]]:
    lon_min, lat_min, lon_max, lat_max = bounds
    out = []
    for lat in range(math.floor(lat_min), math.ceil(lat_max)):
        for lon in range(math.floor(lon_min), math.ceil(lon_max)):
            name = f"Copernicus_DSM_COG_30_{_lat_tag(lat)}_00_{_lon_tag(lon)}_00_DEM"
            out.append((COPDEM_URL.format(name=name), (lon, lat, lon + 1, lat + 1)))
    return out


def worldcover_tiles(bounds) -> list[tuple[str, tuple[int, int, int, int]]]:
    lon_min, lat_min, lon_max, lat_max = bounds
    out = []
    for lat in range(math.floor(lat_min / 3) * 3, math.ceil(lat_max / 3) * 3, 3):
        for lon in range(math.floor(lon_min / 3) * 3, math.ceil(lon_max / 3) * 3, 3):
            tile = f"{_lat_tag(lat)}{_lon_tag(lon)}"
            out.append((WORLDCOVER_URL.format(tile=tile), (lon, lat, lon + 3, lat + 3)))
    return out


def _read_tile_into(url, tile_bounds, sg: SampleGrid, out: np.ndarray, resampling, nodata_out) -> bool:
    win = sg.window_for(tile_bounds)
    if win is None:
        return False
    r0, c0, h, w = win
    wb = sg.window_bounds(r0, c0, h, w)
    try:
        with rasterio.Env(**GDAL_ENV), rasterio.open(url) as src:
            # Clip to the file (tiles can be offset by half a pixel); avoid
            # boundless reads, which bypass COG overviews.
            src_win = src.window(*wb).intersection(Window(0, 0, src.width, src.height))
            # GDAL picks a COG overview automatically for decimated reads.
            data = src.read(1, window=src_win, out_shape=(h, w), resampling=resampling)
            if src.nodata is not None and nodata_out is not None:
                data = np.where(data == src.nodata, nodata_out, data)
    except (RasterioIOError, WindowError) as exc:
        msg = str(exc)
        # Missing tiles (HTTP 404) are normal over open ocean. Anything else
        # (timeouts, 5xx) would silently corrupt the bake, so fail loudly.
        if isinstance(exc, WindowError) or any(s in msg for s in _MISSING_MARKERS):
            log.debug("no tile %s", url)
            return False
        raise RuntimeError(f"failed to read {url}: {msg}") from exc
    out[r0 : r0 + h, c0 : c0 + w] = data
    return True


def _mosaic(tiles, sg: SampleGrid, dtype, fill, resampling, workers: int) -> np.ndarray:
    out = np.full(sg.shape, fill, dtype=dtype)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(
            pool.map(lambda t: _read_tile_into(t[0], t[1], sg, out, resampling, fill), tiles)
        )
    log.info("read %d/%d tiles", sum(results), len(tiles))
    return out


def _check_host(url: str) -> None:
    parts = url.split("/")
    host = parts[2] if len(parts) > 2 else ""
    if not url.startswith(("https://", "http://")) or host not in ALLOWED_HOSTS:
        raise ValueError(f"refusing to download from unexpected host {host}")
    prefix = HOST_PATH_PREFIX.get(host)
    if prefix and not ("/" + "/".join(parts[3:])).startswith(prefix):
        raise ValueError(f"refusing to download unexpected path on {host}")


def _tiles_with_land(tiles, sg: SampleGrid, need: np.ndarray | None):
    if need is None:
        return tiles
    keep = []
    for url, tb in tiles:
        win = sg.window_for(tb)
        if win is not None and need[win[0] : win[0] + win[2], win[1] : win[1] + win[3]].any():
            keep.append((url, tb))
    log.debug("%d/%d tiles touch land", len(keep), len(tiles))
    return keep


def download(url: str, cache_dir: Path) -> Path:
    """Download ``url`` into ``cache_dir`` once (atomic rename)."""
    _check_host(url)
    cache_dir.mkdir(parents=True, exist_ok=True)
    dest = cache_dir / url.rsplit("/", 1)[-1]
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    tmp = dest.with_suffix(dest.suffix + ".part")
    log.info("downloading %s", url)
    with requests.get(url, stream=True, timeout=120, headers={"User-Agent": "sd-hex-pipeline"}) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)
    os.replace(tmp, dest)
    return dest


def _read_zipped_vector(path: Path, bounds) -> gpd.GeoDataFrame:
    with zipfile.ZipFile(path) as z:
        shp = [n for n in z.namelist() if n.lower().endswith(".shp")]
    if not shp:
        raise ValueError(f"no shapefile in {path}")
    gdf = gpd.read_file(f"/vsizip/{path}/{shp[0]}", bbox=tuple(bounds), engine="pyogrio")
    return gdf.to_crs(4326) if gdf.crs and gdf.crs.to_epsg() != 4326 else gdf


def _col(gdf: gpd.GeoDataFrame, *names: str) -> str:
    lower = {c.lower(): c for c in gdf.columns}
    for n in names:
        if n.lower() in lower:
            return lower[n.lower()]
    raise KeyError(f"none of {names} in {list(gdf.columns)}")


class RemoteSources:
    """Streams COG rasters over HTTP range requests; caches vector downloads."""

    # Part of the band-cache key; bump when source datasets or their handling change.
    cache_tag = "remote-v1"

    def __init__(self, cache_dir: str | Path = "cache", workers: int = 8, river_regions: list[str] | None = None):
        self.cache = Path(cache_dir)
        self.workers = workers
        self.river_regions = river_regions

    def elevation(self, sg: SampleGrid, need: np.ndarray | None = None) -> np.ndarray:
        tiles = _tiles_with_land(copdem_tiles(sg.bounds), sg, need)
        for url, _ in tiles:
            _check_host(url)
        return _mosaic(tiles, sg, np.float32, np.nan, Resampling.average, self.workers)

    def landcover(self, sg: SampleGrid, need: np.ndarray | None = None) -> np.ndarray:
        tiles = _tiles_with_land(worldcover_tiles(sg.bounds), sg, need)
        for url, _ in tiles:
            _check_host(url)
        return _mosaic(tiles, sg, np.uint8, 0, Resampling.mode, self.workers)

    def land(self, bounds) -> gpd.GeoDataFrame:
        return _read_zipped_vector(download(NE_LAND, self.cache), bounds)

    def lakes(self, bounds) -> gpd.GeoDataFrame:
        return _read_zipped_vector(download(NE_LAKES, self.cache), bounds)

    def countries(self, bounds) -> gpd.GeoDataFrame:
        g = _read_zipped_vector(download(NE_ADMIN0, self.cache), bounds)
        return gpd.GeoDataFrame(
            {"code": g[_col(g, "ADM0_A3")], "name": g[_col(g, "NAME")]}, geometry=g.geometry, crs=g.crs
        )

    def admin1(self, bounds) -> gpd.GeoDataFrame:
        g = _read_zipped_vector(download(NE_ADMIN1, self.cache), bounds)
        return gpd.GeoDataFrame(
            {
                "code": g[_col(g, "adm1_code")],
                "name": g[_col(g, "name")],
                "country": g[_col(g, "adm0_a3")],
            },
            geometry=g.geometry,
            crs=g.crs,
        )

    def biomes(self, bounds) -> gpd.GeoDataFrame:
        g = _read_zipped_vector(download(ECOREGIONS_URL, self.cache), bounds)
        return gpd.GeoDataFrame(
            {"biome_num": g[_col(g, "BIOME_NUM")].fillna(0).astype(int)}, geometry=g.geometry, crs=g.crs
        )

    def rivers(self, bounds) -> gpd.GeoDataFrame:
        regions = self.river_regions or hydrorivers_regions(bounds)
        unknown = set(regions) - set(HYDRORIVERS_REGIONS)
        if unknown:
            raise ValueError(f"unknown HydroRIVERS regions {sorted(unknown)}, known: {sorted(HYDRORIVERS_REGIONS)}")
        log.info("HydroRIVERS files: %s", ", ".join(regions) or "none")
        parts = []
        for region in regions:
            g = _read_zipped_vector(download(HYDRORIVERS_URL.format(region=region), self.cache), bounds)
            if len(g):
                parts.append(gpd.GeoDataFrame(
                    {"discharge_cms": g[_col(g, "DIS_AV_CMS")].astype(float)}, geometry=g.geometry, crs=g.crs
                ))
        if not parts:
            return gpd.GeoDataFrame({"discharge_cms": []}, geometry=[], crs=4326)
        return gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=parts[0].crs)
