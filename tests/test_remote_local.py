"""Exercise RemoteSources end-to-end against a local HTTP server.

Fake tiles use the real naming schemes, file formats (tiled GeoTIFF with
overviews, zipped shapefiles) and column names, so HTTP range reads, 404
handling and zip discovery are covered without internet access.
"""

import subprocess
import sys
import zipfile
from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from rasterio.enums import Resampling
from rasterio.transform import from_origin
from shapely.geometry import LineString, box

from sdhex import sources
from sdhex.config import TERRAIN_ENUM, BuildConfig
from sdhex.pipeline import build

BBOX = (19.6, 44.2, 21.4, 45.4)


def _write_tif(path: Path, lon0, lat1, deg, px, data, nodata, dtype):
    path.parent.mkdir(parents=True, exist_ok=True)
    profile = dict(
        driver="GTiff", width=px, height=px, count=1, dtype=dtype, crs="EPSG:4326",
        transform=from_origin(lon0, lat1, deg / px, deg / px), tiled=True, blockxsize=128, blockysize=128,
        nodata=nodata,
    )
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data.astype(dtype), 1)
        dst.build_overviews([2, 4, 8], Resampling.average)


def _zip_shp(gdf: gpd.GeoDataFrame, zpath: Path, stem: str, subdir: str = ""):
    tmp = zpath.parent / f"_{stem}"
    tmp.mkdir(parents=True, exist_ok=True)
    gdf.to_file(tmp / f"{stem}.shp")
    with zipfile.ZipFile(zpath, "w") as z:
        for f in tmp.iterdir():
            z.write(f, f"{subdir}{f.name}")


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    root = tmp_path_factory.mktemp("www")
    # DEM: 1x1 deg tiles, only for lat 44 (lat 45 tiles "missing" -> 404 like ocean).
    for lon in (19, 20, 21):
        name = f"Copernicus_DSM_COG_30_N44_00_E0{lon}_00_DEM"
        yy, xx = np.mgrid[0:240, 0:240]
        data = 100.0 + (lon - 19) * 400 + xx * 1.5
        _write_tif(root / "dem" / name / f"{name}.tif", lon, 45, 1.0, 240, data, None, "float32")
    # WorldCover: one 3x3 deg tile N42E018 -> covers lat 42..45, lon 18..21.
    lc = np.full((720, 720), 40, dtype=np.uint8)
    lc[:, 400:] = 10
    _write_tif(
        root / "wc" / "ESA_WorldCover_10m_2021_v200_N42E018_Map.tif", 18, 45, 3.0, 720, lc, 0, "uint8"
    )
    ne = root / "ne"
    ne.mkdir()
    _zip_shp(gpd.GeoDataFrame(geometry=[box(19.9, 40, 25, 50)], crs=4326), ne / "ne_10m_land.zip", "ne_10m_land")
    _zip_shp(gpd.GeoDataFrame(geometry=[box(20.8, 45.0, 21.0, 45.2)], crs=4326), ne / "ne_10m_lakes.zip", "ne_10m_lakes")
    _zip_shp(
        gpd.GeoDataFrame({"ADM0_A3": ["SRB", "ROU"], "NAME": ["Serbia", "Romania"]},
                         geometry=[box(15, 40, 21, 50), box(21, 40, 30, 50)], crs=4326),
        ne / "ne_10m_admin_0_countries.zip", "ne_10m_admin_0_countries",
    )
    _zip_shp(
        gpd.GeoDataFrame({"adm1_code": ["SRB-1", "ROU-1"], "name": ["Vojvodina", "Timis"], "adm0_a3": ["SRB", "ROU"]},
                         geometry=[box(15, 40, 21, 50), box(21, 40, 30, 50)], crs=4326),
        ne / "ne_10m_admin_1_states_provinces.zip", "ne_10m_admin_1_states_provinces",
    )
    hydro = root / "hydro"
    hydro.mkdir()
    _zip_shp(
        gpd.GeoDataFrame({"HYRIV_ID": [1, 2], "DIS_AV_CMS": [5500.0, 10.0]},
                         geometry=[LineString([(21.3, 44.7), (20.0, 44.9)]), LineString([(20.5, 45.3), (20.5, 44.3)])],
                         crs=4326),
        hydro / "HydroRIVERS_v10_eu_shp.zip", "HydroRIVERS_v10_eu", subdir="HydroRIVERS_v10_eu_shp/",
    )
    # Second continental file, as when a bbox straddles two HydroSHEDS regions.
    _zip_shp(
        gpd.GeoDataFrame({"HYRIV_ID": [3], "DIS_AV_CMS": [800.0]},
                         geometry=[LineString([(20.2, 44.2), (20.2, 45.4)])], crs=4326),
        hydro / "HydroRIVERS_v10_as_shp.zip", "HydroRIVERS_v10_as", subdir="HydroRIVERS_v10_as_shp/",
    )

    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).with_name("range_server.py")), str(root)],
        stdout=subprocess.PIPE,
        text=True,
    )
    port = int(proc.stdout.readline())
    yield f"127.0.0.1:{port}"
    proc.terminate()
    proc.wait(timeout=10)


@pytest.fixture
def patched(server, monkeypatch, tmp_path):
    base = f"http://{server}"
    monkeypatch.setattr(sources, "ALLOWED_HOSTS", (server,))
    monkeypatch.setattr(sources, "COPDEM_URL", base + "/dem/{name}/{name}.tif")
    monkeypatch.setattr(sources, "WORLDCOVER_URL", base + "/wc/ESA_WorldCover_10m_2021_v200_{tile}_Map.tif")
    monkeypatch.setattr(sources, "NE_LAND", base + "/ne/ne_10m_land.zip")
    monkeypatch.setattr(sources, "NE_LAKES", base + "/ne/ne_10m_lakes.zip")
    monkeypatch.setattr(sources, "NE_ADMIN0", base + "/ne/ne_10m_admin_0_countries.zip")
    monkeypatch.setattr(sources, "NE_ADMIN1", base + "/ne/ne_10m_admin_1_states_provinces.zip")
    monkeypatch.setattr(sources, "HYDRORIVERS_URL", base + "/hydro/HydroRIVERS_v10_{region}_shp.zip")
    return sources.RemoteSources(tmp_path / "cache", workers=4)


def test_remote_build(patched):
    res = build(BuildConfig(bbox=BBOX, hex_km=11.0), patched)
    lon, lat = res.crop.centers()
    terrain = res.layers["terrain"].data
    elev = res.layers["elev_mean"].data
    land = terrain != TERRAIN_ENUM["OCEAN"]

    assert (terrain[lon < 19.85] == TERRAIN_ENUM["OCEAN"]).all()
    # DEM exists only for lat 44..45: elevation rises eastwards there.
    south = land & (lat < 44.9) & (lat > 44.3)
    assert elev[south & (lon < 20.4)].mean() < elev[south & (lon > 20.6)].mean()
    # Lat 45+ DEM tiles 404 -> no elevation samples -> 0, without crashing.
    assert (elev[land & (lat > 45.1)] == 0).all()
    # Land cover from the only 3x3 tile (lon 18..21, lat 42..45): forest east of lon 19.67.
    covered = land & (lat < 44.9) & (lon < 20.9)
    assert (res.layers["forest_pct"].data[covered] > 50).mean() > 0.9
    names = {c["code"] for c in res.tables["countries"]}
    assert names == {"SRB", "ROU"}
    assert {a["code"] for a in res.tables["admin1"]} == {"SRB-1", "ROU-1"}
    # Big river (5500 m3/s) is navigable; 10 m3/s stream is filtered out.
    assert (res.layers["river_class"].data == 3).sum() >= 8
    assert res.layers["river_class"].data.max() == 3
    assert not (res.layers["river_class"].data == 1).any()


def test_rivers_merge_multiple_continent_files(patched):
    eu_only = patched.rivers((19.5, 44.0, 21.5, 45.5))
    assert sorted(eu_only["discharge_cms"]) == [10.0, 5500.0]
    patched.river_regions = ["eu", "as"]
    both = patched.rivers((19.5, 44.0, 21.5, 45.5))
    assert sorted(both["discharge_cms"]) == [10.0, 800.0, 5500.0]
    patched.river_regions = ["xx"]
    with pytest.raises(ValueError, match="unknown HydroRIVERS"):
        patched.rivers((19.5, 44.0, 21.5, 45.5))


def test_refuses_unknown_host(tmp_path):
    with pytest.raises(ValueError):
        sources.download("https://example.com/x.zip", tmp_path)


def test_server_error_is_fatal_not_silent(patched, monkeypatch, server):
    monkeypatch.setattr(sources, "COPDEM_URL", f"http://{server}/fail/{{name}}.tif")
    monkeypatch.setitem(sources.GDAL_ENV, "GDAL_HTTP_MAX_RETRY", "0")
    with pytest.raises(RuntimeError, match="failed to read"):
        build(BuildConfig(bbox=BBOX, hex_km=11.0, with_landcover=False, with_rivers=False), patched)
