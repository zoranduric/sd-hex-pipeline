"""Build per-hex game data for one region."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import geopandas as gpd
import numpy as np
from rasterio import features
from shapely.geometry import LineString, MultiLineString

from .config import (
    FLAG_COASTAL,
    FLAG_LAKE,
    FLAG_LAND,
    FLAG_NAVIGABLE,
    FLAG_RIVER,
    TERRAIN_ENUM,
    BuildConfig,
    ClassifyParams,
)
from .hexgrid import NEIGHBOR_DIRS, GlobalHexGrid, HexCrop, axial_to_offset, cube_round, offset_to_axial
from .sampling import Aggregator, SampleGrid, hex_index_raster, per_degree_for
from .sources import Sources

log = logging.getLogger(__name__)

T = TERRAIN_ENUM

# (dq, dr) -> direction index, encoded as (dq + 1) * 3 + (dr + 1).
_DIR_LUT = np.full(9, -1, dtype=np.int8)
for _d, (_dq, _dr) in enumerate(NEIGHBOR_DIRS):
    _DIR_LUT[(_dq + 1) * 3 + (_dr + 1)] = _d


@dataclass
class Layer:
    data: np.ndarray
    dtype: str  # "u8" or "i32"
    description: str


@dataclass
class BuildResult:
    crop: HexCrop
    sample_grid: SampleGrid
    hex_idx: np.ndarray
    layers: dict[str, Layer]
    tables: dict[str, list[dict]] = field(default_factory=dict)
    river_raster: np.ndarray | None = None


def neighbor_table(crop: HexCrop) -> np.ndarray:
    """(cells, 6) int32 neighbour indices in NEIGHBOR_DIRS order, ``-1`` outside."""
    rows, cols = np.divmod(np.arange(crop.cells), crop.width)
    q, r = offset_to_axial(cols + crop.col0, rows + crop.row0)
    out = np.empty((crop.cells, 6), dtype=np.int32)
    for d, (dq, dr) in enumerate(NEIGHBOR_DIRS):
        gc, gr = axial_to_offset(q + dq, r + dr)
        out[:, d] = crop.local_index(gc, gr)
    return out


def _local_axial(crop: HexCrop, idx: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    rows, cols = np.divmod(idx, crop.width)
    return offset_to_axial(cols + crop.col0, rows + crop.row0)


def _rasterize(gdf: gpd.GeoDataFrame, sg: SampleGrid, values=None, dtype="uint8", all_touched=False) -> np.ndarray:
    if gdf is None or len(gdf) == 0:
        return np.zeros(sg.shape, dtype=dtype)
    if values is None:
        values = np.ones(len(gdf), dtype=np.int64)
    shapes = ((g, int(v)) for g, v in zip(gdf.geometry, values) if g is not None and not g.is_empty)
    return features.rasterize(
        shapes, out_shape=sg.shape, transform=sg.transform, fill=0, dtype=dtype, all_touched=all_touched
    )


def _iter_lines(geom):
    if isinstance(geom, LineString):
        yield geom
    elif isinstance(geom, MultiLineString):
        yield from geom.geoms


def _densify(coords: np.ndarray, step: float) -> np.ndarray:
    seg = np.diff(coords, axis=0)
    n = np.maximum(1, np.ceil(np.hypot(seg[:, 0], seg[:, 1]) / step).astype(int))
    parts = [coords[i] + seg[i] * (np.arange(n[i])[:, None] / n[i]) for i in range(len(seg))]
    parts.append(coords[-1:])
    return np.concatenate(parts)


def _hex_line(crop: HexCrop, a: int, b: int) -> list[int]:
    """Hexes on the straight line a -> b (inclusive), for non-adjacent jumps."""
    qa, ra = _local_axial(crop, np.array([a]))
    qb, rb = _local_axial(crop, np.array([b]))
    qa, ra, qb, rb = int(qa[0]), int(ra[0]), int(qb[0]), int(rb[0])
    dist = (abs(qa - qb) + abs(ra - rb) + abs((qa + ra) - (qb + rb))) // 2
    t = (np.arange(dist + 1) / max(dist, 1))[:, None]
    eps = 1e-6
    q, r = cube_round(qa + (qb - qa) * t[:, 0] + eps, ra + (rb - ra) * t[:, 0] + eps)
    gc, gr = axial_to_offset(q, r)
    return [int(i) for i in crop.local_index(gc, gr)]


def river_layers(crop: HexCrop, rivers: gpd.GeoDataFrame, params: ClassifyParams):
    """6-bit connection mask (which neighbours the river flows to) and max class per hex."""
    conn = np.zeros(crop.cells, dtype=np.uint8)
    rclass = np.zeros(crop.cells, dtype=np.uint8)
    if rivers is None or len(rivers) == 0:
        return conn, rclass
    thresholds = np.asarray(params.river_class_min_cms)
    step = crop.grid.size_deg / 4.0
    for geom, q_cms in zip(rivers.geometry, rivers["discharge_cms"].to_numpy()):
        cls = int(np.searchsorted(thresholds, q_cms, side="right"))
        if cls == 0 or geom is None:
            continue
        for line in _iter_lines(geom):
            pts = _densify(np.asarray(line.coords)[:, :2], step)
            idx = crop.lonlat_to_index(pts[:, 0], pts[:, 1])
            keep = np.r_[True, idx[1:] != idx[:-1]]
            idx = idx[keep]
            inside = idx[idx >= 0]
            np.maximum.at(rclass, inside, cls)
            if idx.size < 2:
                continue
            a, b = idx[:-1], idx[1:]
            ok = (a >= 0) & (b >= 0)
            a, b = a[ok], b[ok]
            if a.size == 0:
                continue
            qa, ra = _local_axial(crop, a)
            qb, rb = _local_axial(crop, b)
            dq, dr = qb - qa, rb - ra
            adjacent = (np.abs(dq) <= 1) & (np.abs(dr) <= 1)
            code = np.where(adjacent, (dq + 1) * 3 + (dr + 1), 4)
            d = _DIR_LUT[code]
            good = d >= 0
            np.bitwise_or.at(conn, a[good], (1 << d[good]).astype(np.uint8))
            np.bitwise_or.at(conn, b[good], (1 << ((d[good] + 3) % 6)).astype(np.uint8))
            for ja, jb in zip(a[~good], b[~good]):
                path = _hex_line(crop, int(ja), int(jb))
                for pa, pb in zip(path[:-1], path[1:]):
                    if pa < 0 or pb < 0:
                        continue
                    dd = crop.direction_between(pa, pb)
                    if dd >= 0:
                        conn[pa] |= 1 << dd
                        conn[pb] |= 1 << ((dd + 3) % 6)
                    rclass[pa] = max(rclass[pa], cls)
                    rclass[pb] = max(rclass[pb], cls)
    return conn, rclass


def classify_terrain(
    land_frac, lake_frac, elev_mean, elev_std, bare_frac, river_class, p: ClassifyParams
) -> np.ndarray:
    terrain = np.full(land_frac.shape, T["GRASSLAND"], dtype=np.uint8)
    em = np.nan_to_num(elev_mean, nan=0.0)
    es = np.nan_to_num(elev_std, nan=0.0)
    terrain[bare_frac >= p.desert_bare_frac] = T["DESERT"]
    terrain[(em >= p.hills_mean_m) | (es >= p.hills_std_m)] = T["HILLS"]
    terrain[(em >= p.mountain_mean_m) | (es >= p.mountain_std_m)] = T["MOUNTAIN"]
    terrain[em >= p.high_mountain_mean_m] = T["HIGH_MOUNTAIN"]
    if p.river_terrain_min_class > 0:
        lowland = np.isin(terrain, (T["GRASSLAND"], T["HILLS"], T["DESERT"]))
        terrain[lowland & (river_class >= p.river_terrain_min_class)] = T["RIVER"]
    water = (land_frac < p.land_min_frac) | (lake_frac >= p.lake_min_frac)
    terrain[water] = T["OCEAN"]
    return terrain


def _dominant_table(agg: Aggregator, raster: np.ndarray, gdf: gpd.GeoDataFrame, columns: list[str]):
    ids, _ = agg.dominant(raster, ignore=0)
    # Raster values are 1-based feature indices; compact to features that won.
    used = np.unique(ids[ids > 0])
    remap = np.full(len(gdf) + 1, -1, dtype=np.int64)
    remap[used] = np.arange(used.size)
    out = np.where(ids > 0, remap[np.clip(ids, 0, None)], -1).astype(np.int32)
    table = []
    for new_id, feat in enumerate(used):
        row = gdf.iloc[int(feat) - 1]
        table.append({"id": new_id, **{c: (None if row[c] is None else str(row[c])) for c in columns}})
    return out, table


def build(cfg: BuildConfig, sources: Sources) -> BuildResult:
    grid = GlobalHexGrid(cfg.hex_km, cfg.lat_top, cfg.lat_bottom)
    crop = HexCrop.from_bbox(grid, *cfg.resolved_bbox())
    sg = SampleGrid.covering(crop.lonlat_bounds(1.5), per_degree_for(crop, cfg.samples_per_hex))
    log.info(
        "grid %d x %d global, crop %d x %d = %d hexes at %.1f km; samples %d x %d",
        grid.global_width, grid.global_height, crop.width, crop.height, crop.cells, cfg.hex_km, sg.nx, sg.ny,
    )
    hex_idx = hex_index_raster(crop, sg)
    agg = Aggregator(hex_idx, crop.cells)
    bounds = sg.bounds
    p = cfg.classify

    log.info("land / lakes")
    land = _rasterize(sources.land(bounds), sg)
    lakes = _rasterize(sources.lakes(bounds), sg) & land
    land_frac = agg.fraction(land)
    lake_frac = agg.fraction(lakes)
    dry = (land == 1) & (lakes == 0)

    log.info("elevation")
    elev = sources.elevation(sg)
    e_mean, e_std, e_min, e_max = agg.masked_stats(elev, np.isfinite(elev) & dry)
    no_elev = int((((land_frac - lake_frac) > 0.25) & ~np.isfinite(e_mean)).sum())
    if no_elev:
        # e.g. Copernicus GLO-90 omits Armenia and Azerbaijan; these hexes classify as flat.
        log.warning("%d land hexes have no elevation data (treated as 0 m, flat)", no_elev)

    layers_lc: dict[str, np.ndarray] = {}
    zeros = np.zeros(crop.cells)
    if cfg.with_landcover:
        log.info("land cover")
        lc = sources.landcover(sg)
        lc_land = np.where(dry, lc, 0)
        lc_dom, _ = agg.dominant(lc_land, ignore=0)
        on_land = agg.fraction(dry & (lc > 0))
        def frac(*codes):
            f = agg.fraction(np.isin(lc_land, codes))
            return np.divide(f, on_land, out=np.zeros(crop.cells), where=on_land > 0)
        layers_lc = {
            "forest_pct": frac(10, 20),
            "crop_pct": frac(40),
            "urban_pct": frac(50),
            "wetland_pct": frac(90, 95),
            "bare_pct": frac(60, 70),
        }
    else:
        lc_dom = np.full(crop.cells, -1)
        layers_lc = {k: zeros for k in ("forest_pct", "crop_pct", "urban_pct", "wetland_pct", "bare_pct")}

    log.info("countries")
    countries = sources.countries(bounds)
    c_raster = _rasterize(countries, sg, np.arange(1, len(countries) + 1), dtype="uint16")
    country_id, country_table = _dominant_table(agg, c_raster, countries, ["code", "name"])

    admin1_id = np.full(crop.cells, -1, dtype=np.int32)
    admin1_table: list[dict] = []
    if cfg.with_admin1:
        log.info("admin-1 regions")
        admin1 = sources.admin1(bounds)
        a_raster = _rasterize(admin1, sg, np.arange(1, len(admin1) + 1), dtype="uint32")
        admin1_id, admin1_table = _dominant_table(agg, a_raster, admin1, ["code", "name", "country"])

    conn = np.zeros(crop.cells, dtype=np.uint8)
    rclass = np.zeros(crop.cells, dtype=np.uint8)
    river_raster = None
    if cfg.with_rivers:
        log.info("rivers")
        rivers = sources.rivers(bounds)
        if len(rivers):
            rivers = rivers[rivers["discharge_cms"] >= p.river_class_min_cms[0]]
        conn, rclass = river_layers(crop, rivers, p)
        cls = np.searchsorted(np.asarray(p.river_class_min_cms), rivers["discharge_cms"].to_numpy(), side="right")
        river_raster = _rasterize(rivers, sg, cls, all_touched=True)

    terrain = classify_terrain(land_frac, lake_frac, e_mean, e_std, layers_lc["bare_pct"], rclass, p)

    nbr = neighbor_table(crop)
    is_land = terrain != T["OCEAN"]
    is_lake = lake_frac >= p.lake_min_frac
    is_sea = ~is_land & ~is_lake
    sea_nbr = np.zeros(crop.cells, dtype=bool)
    for d in range(6):
        n = nbr[:, d]
        sea_nbr |= (n >= 0) & is_sea[np.clip(n, 0, None)]
    flags = (
        is_land * FLAG_LAND
        | is_lake * FLAG_LAKE
        | (is_land & sea_nbr) * FLAG_COASTAL
        | (rclass > 0) * FLAG_RIVER
        | (rclass >= p.navigable_min_class) * FLAG_NAVIGABLE
    ).astype(np.uint8)

    def i32(a, fill=0):
        return np.where(np.isfinite(a), np.rint(a), fill).astype(np.int32)

    def pct(a):
        return np.clip(np.rint(np.asarray(a) * 100), 0, 100).astype(np.uint8)

    layers = {
        "terrain": Layer(terrain, "u8", "Game terrain enum (see terrain_enum)"),
        "flags": Layer(flags, "u8", "Bitfield: 1 land, 2 lake, 4 coastal, 8 river, 16 navigable river"),
        "landcover": Layer(np.where(lc_dom > 0, lc_dom, 0).astype(np.uint8), "u8",
                           "Dominant ESA WorldCover class on dry land (0 = none)"),
        "land_pct": Layer(pct(land_frac), "u8", "Share of hex covered by land (incl. lakes), 0-100"),
        "lake_pct": Layer(pct(lake_frac), "u8", "Share of hex covered by lakes, 0-100"),
        "forest_pct": Layer(pct(layers_lc["forest_pct"]), "u8", "Tree cover + shrubland share of dry land"),
        "crop_pct": Layer(pct(layers_lc["crop_pct"]), "u8", "Cropland share of dry land"),
        "urban_pct": Layer(pct(layers_lc["urban_pct"]), "u8", "Built-up share of dry land"),
        "wetland_pct": Layer(pct(layers_lc["wetland_pct"]), "u8", "Wetland + mangrove share of dry land"),
        "bare_pct": Layer(pct(layers_lc["bare_pct"]), "u8", "Bare/sparse + snow share of dry land"),
        "river_mask": Layer(conn, "u8", "6-bit mask of neighbour directions the river connects to"),
        "river_class": Layer(rclass, "u8", "Largest river: 0 none, 1 minor, 2 major, 3 navigable"),
        "elev_mean": Layer(i32(e_mean), "i32", "Mean elevation of dry land, metres"),
        "elev_min": Layer(i32(e_min), "i32", "Min elevation, metres"),
        "elev_max": Layer(i32(e_max), "i32", "Max elevation, metres"),
        "elev_std": Layer(i32(e_std), "i32", "Elevation standard deviation (ruggedness), metres"),
        "country": Layer(country_id, "i32", "Index into countries table, -1 none"),
        "admin1": Layer(admin1_id, "i32", "Index into admin1 table, -1 none"),
    }
    return BuildResult(
        crop=crop,
        sample_grid=sg,
        hex_idx=hex_idx,
        layers=layers,
        tables={"countries": country_table, "admin1": admin1_table},
        river_raster=river_raster,
    )
