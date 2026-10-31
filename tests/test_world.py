"""World (full-width, banded) builds and biomes, on the synthetic world."""

import numpy as np
import pytest

from sdhex.config import BIOME_ENUM, REGIONS, TERRAIN_ENUM, BuildConfig
from sdhex.export import export, read_sdhx
from sdhex.hexgrid import GlobalHexGrid, HexCrop
from sdhex.pipeline import build, neighbor_table, plan_bands
from sdhex.synthetic import SyntheticSources

WORLD = REGIONS["world"]
# Coarse hexes keep the whole world small enough for a unit test.
HEX_KM = 150.0


def _cfg(**kw):
    return BuildConfig(region="world", hex_km=HEX_KM, samples_per_hex=6, with_rivers=False, **kw)


@pytest.fixture(scope="module")
def single():
    cfg = _cfg()
    crop = HexCrop.from_bbox(GlobalHexGrid(HEX_KM), *WORLD)
    return build(cfg, SyntheticSources(WORLD), band_rows=crop.height)


@pytest.fixture(scope="module")
def banded(tmp_path_factory):
    cache = tmp_path_factory.mktemp("bands")
    res = build(_cfg(), SyntheticSources(WORLD), band_rows=10, band_cache=cache)
    return res, cache


def test_world_crop_is_full_width():
    g = GlobalHexGrid(11.0)
    crop = HexCrop.from_bbox(g, *WORLD)
    assert (crop.col0, crop.row0, crop.width, crop.height) == (0, 0, g.global_width, g.global_height)
    assert crop.width * crop.height > 6_000_000
    # East of the last column is the first column (same row): the map wraps.
    nbr = neighbor_table(crop.row_band(0, 4))
    assert nbr[crop.width - 1, 0] == 0
    assert nbr[0, 3] == crop.width - 1


def test_world_needs_bands_at_game_resolution():
    crop = HexCrop.from_bbox(GlobalHexGrid(11.0), *WORLD)
    rows = plan_bands(crop, 10)
    assert 2 <= rows < crop.height and rows % 2 == 0


def test_banded_equals_single_pass(single, banded):
    res, _ = banded
    assert single.sample_grid is not None and res.sample_grid is None
    for name, layer in single.layers.items():
        a, b = layer.data, res.layers[name].data
        if name in ("country", "admin1"):
            ta = single.tables["countries" if name == "country" else "admin1"]
            tb = res.tables["countries" if name == "country" else "admin1"]
            a = np.array([ta[i]["code"] if i >= 0 else None for i in a], dtype=object)
            b = np.array([tb[i]["code"] if i >= 0 else None for i in b], dtype=object)
        assert np.array_equal(a, b), name
    assert len(res.tables["countries"]) == len(single.tables["countries"])


def test_band_cache_resumes(banded, monkeypatch):
    res, cache = banded
    assert len(list(cache.rglob("band_*.npz"))) > 1

    class Exploding(SyntheticSources):
        def elevation(self, sg, need=None):
            raise AssertionError("should have been served from the band cache")

    again = build(_cfg(), Exploding(WORLD), band_rows=10, band_cache=cache)
    for name, layer in res.layers.items():
        assert np.array_equal(layer.data, again.layers[name].data), name


def test_biomes_and_ice(single):
    biome = single.layers["biome"].data
    terrain = single.layers["terrain"].data
    land = terrain != TERRAIN_ENUM["OCEAN"]
    lon, lat = single.crop.centers()
    assert (biome[~land] == 0).all()
    # Every land hex gets a biome, including the gap between ecoregion polygons.
    assert (biome[land] > 0).all()
    assert {BIOME_ENUM["BOREAL_FOREST"], BIOME_ENUM["MEDITERRANEAN"], BIOME_ENUM["TROPICAL_RAINFOREST"]} <= set(
        np.unique(biome)
    )
    # Synthetic glacier (WorldCover snow) in the NE corner -> ICE biome + IMPASSABLE terrain.
    ice = biome == BIOME_ENUM["ICE"]
    assert ice.any()
    assert (terrain[ice] == TERRAIN_ENUM["IMPASSABLE"]).all()
    lon_e = np.where(lon <= -180, lon + 360, lon)  # odd-row edge hex centre sits on the antimeridian
    assert lon_e[ice].min() > 100 and lat[ice].min() > 60


def test_world_export_and_preview(banded, tmp_path):
    res, _ = banded
    cfg = _cfg()
    paths = export(res, cfg, tmp_path)
    header, layers = read_sdhx(paths["sdhx"])
    assert header["crop"]["width"] == header["grid"]["global_width"]
    assert header["biome_enum"]["ICE"] == BIOME_ENUM["ICE"]
    assert layers["biome"].size == res.crop.cells
    assert paths["preview"].stat().st_size > 0 and paths["biomes"].stat().st_size > 0


def test_world_rejects_rivers():
    with pytest.raises(ValueError, match="rivers"):
        build(BuildConfig(region="world", hex_km=HEX_KM), SyntheticSources(WORLD))
