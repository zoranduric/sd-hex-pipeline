import numpy as np
import pytest

from sdhex.config import FLAG_COASTAL, FLAG_LAND, FLAG_NAVIGABLE, TERRAIN_ENUM, BuildConfig
from sdhex.export import export, read_sdhx
from sdhex.pipeline import build, neighbor_table
from sdhex.synthetic import SyntheticSources

BBOX = (19.5, 44.0, 21.5, 45.5)


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    cfg = BuildConfig(bbox=BBOX, hex_km=11.0)
    res = build(cfg, SyntheticSources(BBOX))
    out = tmp_path_factory.mktemp("out")
    paths = export(res, cfg, out)
    return cfg, res, paths


def test_layers_roundtrip(built):
    _, res, paths = built
    header, layers = read_sdhx(paths["sdhx"])
    assert header["crop"]["cells"] == res.crop.cells
    for name, layer in res.layers.items():
        assert np.array_equal(layers[name], layer.data.astype(layers[name].dtype)), name
        entry = next(e for e in header["layers"] if e["name"] == name)
        assert entry["offset"] % 16 == 0
    assert paths["texture"].exists() and paths["preview"].exists()


def test_sea_land_and_coast(built):
    _, res, _ = built
    lon, _ = res.crop.centers()
    terrain = res.layers["terrain"].data
    flags = res.layers["flags"].data
    west = lon < BBOX[0] + 0.25 * (BBOX[2] - BBOX[0])
    assert (terrain[west] == TERRAIN_ENUM["OCEAN"]).all()
    assert (flags[west] & FLAG_LAND == 0).all()
    coastal = (flags & FLAG_COASTAL) > 0
    assert coastal.any()
    nbr = neighbor_table(res.crop)
    for h in np.flatnonzero(coastal):
        ns = nbr[h][nbr[h] >= 0]
        assert (terrain[ns] == TERRAIN_ENUM["OCEAN"]).any()


def test_terrain_variety(built):
    _, res, _ = built
    terrain = res.layers["terrain"].data
    for name in ("GRASSLAND", "MOUNTAIN", "HIGH_MOUNTAIN", "RIVER", "OCEAN"):
        assert (terrain == TERRAIN_ENUM[name]).any(), name


def test_countries_and_admin1(built):
    _, res, _ = built
    codes = {c["code"] for c in res.tables["countries"]}
    assert codes == {"NOR", "SOU"}
    assert len(res.tables["admin1"]) == 4
    land = res.layers["terrain"].data != TERRAIN_ENUM["OCEAN"]
    assert (res.layers["country"].data[land] >= 0).all()


def test_river_masks_symmetric_and_navigable(built):
    _, res, _ = built
    mask = res.layers["river_mask"].data
    rclass = res.layers["river_class"].data
    flags = res.layers["flags"].data
    nbr = neighbor_table(res.crop)
    assert (rclass == 3).sum() > 5 and (rclass == 1).any()
    assert ((flags & FLAG_NAVIGABLE) > 0).sum() == (rclass >= 3).sum()
    for h in np.flatnonzero(mask):
        for d in range(6):
            if mask[h] & (1 << d):
                n = nbr[h, d]
                assert n >= 0
                assert mask[n] & (1 << ((d + 3) % 6)), (h, d, n)
    # The big river crosses the whole land width, so its hexes form a connected chain.
    navigable = set(np.flatnonzero(rclass == 3).tolist())
    start = next(iter(navigable))
    seen, stack = {start}, [start]
    while stack:
        h = stack.pop()
        for d in range(6):
            n = int(nbr[h, d])
            if mask[h] & (1 << d) and n in navigable and n not in seen:
                seen.add(n)
                stack.append(n)
    assert len(seen) >= 0.9 * len(navigable)
