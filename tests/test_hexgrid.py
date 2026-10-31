import math

import numpy as np
import pytest

from sdhex.hexgrid import (
    NEIGHBOR_DIRS,
    GlobalHexGrid,
    HexCrop,
    axial_to_offset,
    offset_to_axial,
    opposite_dir,
)

SQRT3 = math.sqrt(3.0)


@pytest.fixture
def grid():
    return GlobalHexGrid(hex_km=11.0)


def test_grid_dimensions(grid):
    assert grid.global_width == round(40075.017 / 11.0)
    assert grid.dy == pytest.approx(grid.dx * SQRT3 / 2)
    assert grid.global_height * grid.dy == pytest.approx(144.0, abs=grid.dy)


def test_axial_offset_roundtrip():
    rng = np.random.default_rng(0)
    col = rng.integers(0, 5000, 1000)
    row = rng.integers(0, 3000, 1000)
    q, r = offset_to_axial(col, row)
    c2, r2 = axial_to_offset(q, r)
    assert np.array_equal(c2, col) and np.array_equal(r2, row)


def test_centers_map_to_themselves(grid):
    rng = np.random.default_rng(1)
    col = rng.integers(0, grid.global_width, 5000)
    row = rng.integers(0, grid.global_height, 5000)
    lon, lat = grid.center_lonlat(col, row)
    c2, r2 = grid.lonlat_to_offset(lon, lat)
    assert np.array_equal(c2, col) and np.array_equal(r2, row)


def _world(grid, col, row):
    """Godot world coordinates (HEX_SIZE = 1) of a hex centre: same formula as axial_to_world."""
    q, r = offset_to_axial(col, row)
    return SQRT3 * (q + r / 2.0), 1.5 * r


def test_point_lands_in_nearest_center(grid):
    rng = np.random.default_rng(2)
    lon = rng.uniform(10, 30, 3000)
    lat = rng.uniform(30, 50, 3000)
    col, row = grid.lonlat_to_offset(lon, lat)
    px = (lon - grid.lon0) / grid.dx
    py = (grid.lat_top - lat) / grid.dy
    x, z = SQRT3 * px, 1.5 * py
    for i in range(lon.size):
        best = None
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                c, r = col[i] + dc, row[i] + dr
                cx, cz = _world(grid, c, r)
                dist = (cx - x[i]) ** 2 + (cz - z[i]) ** 2
                if best is None or dist < best[0] - 1e-12:
                    best = (dist, c, r)
        assert (best[1], best[2]) == (col[i], row[i])


def test_columns_wrap_at_antimeridian(grid):
    _, lat_even = grid.center_lonlat(0, 1000)
    c_east, _ = grid.lonlat_to_offset(179.99, lat_even)
    c_west, _ = grid.lonlat_to_offset(-179.99, lat_even)
    assert int(c_east) == grid.global_width - 1
    assert int(c_west) == 0
    # Odd rows are shifted half a hex east, so their last hex straddles +-180.
    _, lat_odd = grid.center_lonlat(0, 1001)
    c_east, _ = grid.lonlat_to_offset(179.99, lat_odd)
    c_west, _ = grid.lonlat_to_offset(-179.99, lat_odd)
    assert int(c_east) == int(c_west) == grid.global_width - 1


def test_crop_neighbors_symmetric(grid):
    crop = HexCrop.from_bbox(grid, 19.0, 44.0, 22.0, 46.0)
    assert crop.row0 % 2 == 0
    for idx in range(crop.cells):
        for d in range(6):
            n = crop.neighbor(idx, d)
            if n >= 0:
                assert crop.neighbor(n, opposite_dir(d)) == idx


def test_neighbor_dirs_match_game_order():
    # E, NE, NW, W, SW, SE as in HexGrid.NEIGHBOR_DIRS.
    assert NEIGHBOR_DIRS == ((1, 0), (1, -1), (0, -1), (-1, 0), (-1, 1), (0, 1))


def test_north_is_negative_row(grid):
    crop = HexCrop.from_bbox(grid, 19.0, 44.0, 22.0, 46.0)
    idx = crop.width * 4 + 5
    lon, lat = crop.centers()
    ne = crop.neighbor(idx, 1)
    se = crop.neighbor(idx, 5)
    assert lat[ne] > lat[idx] > lat[se]
    assert lon[crop.neighbor(idx, 0)] > lon[idx]


def test_bad_bbox_rejected(grid):
    with pytest.raises(ValueError):
        HexCrop.from_bbox(grid, 20, 45, 19, 46)
    with pytest.raises(ValueError):
        HexCrop.from_bbox(grid, 170, 0, 190, 10)
