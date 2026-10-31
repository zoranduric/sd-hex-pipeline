"""Build per-hex game data for one region."""

from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path

import geopandas as gpd
import numpy as np
from rasterio import features
from shapely.geometry import LineString, MultiLineString

from .config import (
    BIOME_ENUM,
    FLAG_COASTAL,
    FLAG_LAKE,
    FLAG_LAND,
    FLAG_NAVIGABLE,
    FLAG_RIVER,
    TERRAIN_ENUM,
    WWF_BIOME_TO_GAME,
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
    layers: dict[str, Layer]
    tables: dict[str, list[dict]] = field(default_factory=dict)
    # Sample-raster artefacts for the detailed preview; None for banded (world) builds.
    sample_grid: SampleGrid | None = None
    hex_idx: np.ndarray | None = None
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
    land_frac, lake_frac, elev_mean, elev_std, bare_frac, river_class, p: ClassifyParams, snow_frac=None
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
    if snow_frac is not None:
        terrain[snow_frac >= p.ice_frac] = T["IMPASSABLE"]
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


def _pct(a) -> np.ndarray:
    return np.clip(np.rint(np.asarray(a) * 100), 0, 100).astype(np.uint8)


def _i32(a, fill=0) -> np.ndarray:
    return np.where(np.isfinite(a), np.rint(a), fill).astype(np.int32)


# Raw per-hex arrays produced for one crop (or band) before neighbour-dependent work.
_PART_KEYS = (
    "terrain", "is_lake", "landcover", "biome", "land_pct", "lake_pct", "forest_pct", "crop_pct",
    "urban_pct", "wetland_pct", "bare_pct", "snow_pct", "river_mask", "river_class",
    "elev_mean", "elev_min", "elev_max", "elev_std", "country", "admin1",
)


def _build_part(cfg: BuildConfig, crop: HexCrop, sources: Sources):
    sg = SampleGrid.covering(crop.lonlat_bounds(1.5), per_degree_for(crop, cfg.samples_per_hex))
    log.info(
        "rows %d-%d: %d x %d = %d hexes; samples %d x %d",
        crop.row0, crop.row0 + crop.height - 1, crop.width, crop.height, crop.cells, sg.nx, sg.ny,
    )
    hex_idx = hex_index_raster(crop, sg)
    agg = Aggregator(hex_idx, crop.cells)
    bounds = sg.bounds
    p = cfg.classify
    n = crop.cells

    log.info("land / lakes")
    land = _rasterize(sources.land(bounds), sg)
    lakes = _rasterize(sources.lakes(bounds), sg) & land
    land_frac = agg.fraction(land)
    lake_frac = agg.fraction(lakes)
    dry = (land == 1) & (lakes == 0)
    need = land.astype(bool)

    log.info("elevation")
    elev = sources.elevation(sg, need=need)
    e_mean, e_std, e_min, e_max = agg.masked_stats(elev, np.isfinite(elev) & dry)
    del elev
    no_elev = int((((land_frac - lake_frac) > 0.25) & ~np.isfinite(e_mean)).sum())
    if no_elev:
        # e.g. Copernicus GLO-90 omits Armenia and Azerbaijan; these hexes classify as flat.
        log.warning("%d land hexes have no elevation data (treated as 0 m, flat)", no_elev)

    zeros = np.zeros(n)
    lc_dom = np.full(n, -1)
    fr = {k: zeros for k in ("forest_pct", "crop_pct", "urban_pct", "wetland_pct", "bare_pct", "snow_pct")}
    if cfg.with_landcover:
        log.info("land cover")
        lc = sources.landcover(sg, need=need)
        lc_land = np.where(dry, lc, 0)
        lc_dom, _ = agg.dominant(lc_land, ignore=0)
        on_land = agg.fraction(dry & (lc > 0))
        del lc

        def frac(*codes):
            f = agg.fraction(np.isin(lc_land, codes))
            return np.divide(f, on_land, out=np.zeros(n), where=on_land > 0)

        fr = {
            "forest_pct": frac(10, 20),
            "crop_pct": frac(40),
            "urban_pct": frac(50),
            "wetland_pct": frac(90, 95),
            "bare_pct": frac(60),
            "snow_pct": frac(70),
        }
        del lc_land

    biome = np.zeros(n, dtype=np.uint8)
    if cfg.with_biomes:
        log.info("biomes")
        eco = sources.biomes(bounds)
        lut = np.zeros(256, dtype=np.uint8)
        for wwf, game in WWF_BIOME_TO_GAME.items():
            lut[wwf] = game
        vals = lut[np.clip(eco["biome_num"].to_numpy(dtype=np.int64), 0, 255)] if len(eco) else []
        b_raster = _rasterize(eco, sg, vals) if len(eco) else np.zeros(sg.shape, np.uint8)
        dom, _ = agg.dominant(np.where(dry, b_raster, 0), ignore=0)
        biome = np.where(dom > 0, dom, 0).astype(np.uint8)
    if cfg.with_landcover:
        biome[fr["snow_pct"] >= p.ice_frac] = BIOME_ENUM["ICE"]

    log.info("countries")
    countries = sources.countries(bounds)
    c_raster = _rasterize(countries, sg, np.arange(1, len(countries) + 1), dtype="uint16")
    country_id, country_table = _dominant_table(agg, c_raster, countries, ["code", "name"])
    del c_raster

    admin1_id = np.full(n, -1, dtype=np.int32)
    admin1_table: list[dict] = []
    if cfg.with_admin1:
        log.info("admin-1 regions")
        admin1 = sources.admin1(bounds)
        a_raster = _rasterize(admin1, sg, np.arange(1, len(admin1) + 1), dtype="uint32")
        admin1_id, admin1_table = _dominant_table(agg, a_raster, admin1, ["code", "name", "country"])
        del a_raster

    conn = np.zeros(n, dtype=np.uint8)
    rclass = np.zeros(n, dtype=np.uint8)
    river_raster = None
    if cfg.with_rivers:
        log.info("rivers")
        rivers = sources.rivers(bounds)
        if len(rivers):
            rivers = rivers[rivers["discharge_cms"] >= p.river_class_min_cms[0]]
        conn, rclass = river_layers(crop, rivers, p)
        cls = np.searchsorted(np.asarray(p.river_class_min_cms), rivers["discharge_cms"].to_numpy(), side="right")
        river_raster = _rasterize(rivers, sg, cls, all_touched=True)

    terrain = classify_terrain(
        land_frac, lake_frac, e_mean, e_std, fr["bare_pct"], rclass, p,
        snow_frac=fr["snow_pct"] if cfg.with_landcover else None,
    )
    is_water = terrain == T["OCEAN"]
    biome[is_water] = BIOME_ENUM["NONE"]

    part = {
        "terrain": terrain,
        "is_lake": (lake_frac >= p.lake_min_frac).astype(np.uint8),
        "landcover": np.where(lc_dom > 0, lc_dom, 0).astype(np.uint8),
        "biome": biome,
        "land_pct": _pct(land_frac),
        "lake_pct": _pct(lake_frac),
        **{k: _pct(v) for k, v in fr.items()},
        "river_mask": conn,
        "river_class": rclass,
        "elev_mean": _i32(e_mean),
        "elev_min": _i32(e_min),
        "elev_max": _i32(e_max),
        "elev_std": _i32(e_std),
        "country": country_id,
        "admin1": admin1_id,
    }
    tables = {"countries": country_table, "admin1": admin1_table}
    return part, tables, (sg, hex_idx, river_raster)


def _merge_tables(parts_tables: list[list[dict]], id_arrays: list[np.ndarray], key_cols: list[str]):
    """Merge per-band tables (keyed by ``key_cols``) into one, remapping band-local ids."""
    merged: list[dict] = []
    index: dict[tuple, int] = {}
    out = []
    for table, ids in zip(parts_tables, id_arrays):
        remap = np.full(len(table) + 1, -1, dtype=np.int32)
        for row in table:
            key = tuple(row.get(c) for c in key_cols)
            if key not in index:
                index[key] = len(merged)
                merged.append({**row, "id": len(merged)})
            remap[row["id"]] = index[key]
        out.append(np.where(ids >= 0, remap[np.clip(ids, 0, len(table))], -1).astype(np.int32))
    return merged, np.concatenate(out) if out else np.zeros(0, np.int32)


def _fill_biome_gaps(biome: np.ndarray, land: np.ndarray, nbr: np.ndarray, passes: int = 8) -> None:
    """Coastal land hexes outside every ecoregion polygon take a neighbour's biome."""
    for _ in range(passes):
        holes = np.flatnonzero(land & (biome == 0))
        if holes.size == 0:
            return
        for d in range(6):
            nb = nbr[holes, d]
            take = (nb >= 0) & (biome[holes] == 0)
            vals = np.where(take, biome[np.clip(nb, 0, None)], 0)
            biome[holes] = np.where(biome[holes] == 0, vals, biome[holes])


def _finalize(crop: HexCrop, part: dict, cfg: BuildConfig) -> dict[str, Layer]:
    p = cfg.classify
    terrain = part["terrain"]
    rclass = part["river_class"]
    nbr = neighbor_table(crop)
    is_land = terrain != T["OCEAN"]
    is_lake = part["is_lake"].astype(bool)
    is_sea = ~is_land & ~is_lake
    sea_nbr = np.zeros(crop.cells, dtype=bool)
    for d in range(6):
        nb = nbr[:, d]
        sea_nbr |= (nb >= 0) & is_sea[np.clip(nb, 0, None)]
    flags = (
        is_land * FLAG_LAND
        | is_lake * FLAG_LAKE
        | (is_land & sea_nbr) * FLAG_COASTAL
        | (rclass > 0) * FLAG_RIVER
        | (rclass >= p.navigable_min_class) * FLAG_NAVIGABLE
    ).astype(np.uint8)
    biome = part["biome"].copy()
    if cfg.with_biomes:
        _fill_biome_gaps(biome, is_land, nbr)
    del nbr

    u8, i32 = "u8", "i32"
    return {
        "terrain": Layer(terrain, u8, "Game terrain enum (see terrain_enum)"),
        "flags": Layer(flags, u8, "Bitfield: 1 land, 2 lake, 4 coastal, 8 river, 16 navigable river"),
        "landcover": Layer(part["landcover"], u8, "Dominant ESA WorldCover class on dry land (0 = none)"),
        "biome": Layer(biome, u8, "Climate/vegetation zone (see biome_enum), 0 = none/water"),
        "land_pct": Layer(part["land_pct"], u8, "Share of hex covered by land (incl. lakes), 0-100"),
        "lake_pct": Layer(part["lake_pct"], u8, "Share of hex covered by lakes, 0-100"),
        "forest_pct": Layer(part["forest_pct"], u8, "Tree cover + shrubland share of dry land"),
        "crop_pct": Layer(part["crop_pct"], u8, "Cropland share of dry land"),
        "urban_pct": Layer(part["urban_pct"], u8, "Built-up share of dry land"),
        "wetland_pct": Layer(part["wetland_pct"], u8, "Wetland + mangrove share of dry land"),
        "bare_pct": Layer(part["bare_pct"], u8, "Bare / sparse vegetation share of dry land"),
        "snow_pct": Layer(part["snow_pct"], u8, "Permanent snow and ice share of dry land"),
        "river_mask": Layer(part["river_mask"], u8, "6-bit mask of neighbour directions the river connects to"),
        "river_class": Layer(rclass, u8, "Largest river: 0 none, 1 minor, 2 major, 3 navigable"),
        "elev_mean": Layer(part["elev_mean"], i32, "Mean elevation of dry land, metres"),
        "elev_min": Layer(part["elev_min"], i32, "Min elevation, metres"),
        "elev_max": Layer(part["elev_max"], i32, "Max elevation, metres"),
        "elev_std": Layer(part["elev_std"], i32, "Elevation standard deviation (ruggedness), metres"),
        "country": Layer(part["country"], i32, "Index into countries table, -1 none"),
        "admin1": Layer(part["admin1"], i32, "Index into admin1 table, -1 none"),
    }


# Keep each band's sample raster around this size (pixels) to bound memory.
MAX_SAMPLES_PER_BAND = 24_000_000


def plan_bands(crop: HexCrop, samples_per_hex: int, max_samples: int = MAX_SAMPLES_PER_BAND) -> int:
    """Rows per band (even), so one band's sample raster stays under ``max_samples``."""
    per_deg = per_degree_for(crop, samples_per_hex)
    lon_span = 360.0 if crop.full_width else crop.width * crop.grid.dx + 4 * crop.grid.dx
    px_per_row = math.ceil(lon_span + 2) * per_deg * crop.grid.dy * per_deg
    rows = int(max_samples / max(px_per_row, 1))
    rows = max(2, rows - rows % 2)
    return crop.height if rows >= crop.height else rows


def _band_key(cfg: BuildConfig, crop: HexCrop, band_rows: int, source_tag: str) -> str:
    blob = json.dumps(
        {"v": 2, "cfg": asdict(cfg), "crop": [crop.col0, crop.row0, crop.width, crop.height],
         "band_rows": band_rows, "src": source_tag},
        sort_keys=True, default=str,
    )
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _save_band(path: Path, part: dict, tables: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.stem + ".tmp.npz")
    np.savez(tmp, **part)
    (path.with_suffix(".json")).write_text(json.dumps(tables), encoding="utf-8")
    tmp.replace(path)


def _load_band(path: Path):
    tables_path = path.with_suffix(".json")
    if not (path.exists() and tables_path.exists()):
        return None
    with np.load(path, allow_pickle=False) as z:
        part = {k: z[k] for k in _PART_KEYS}
    return part, json.loads(tables_path.read_text(encoding="utf-8"))


def build(
    cfg: BuildConfig,
    sources: Sources,
    band_rows: int | None = None,
    band_cache: str | Path | None = None,
) -> BuildResult:
    """Build per-hex layers. Large areas (e.g. the world) are processed in latitude bands;
    with ``band_cache`` finished bands are saved so an interrupted run resumes."""
    grid = GlobalHexGrid(cfg.hex_km, cfg.lat_top, cfg.lat_bottom)
    crop = HexCrop.from_bbox(grid, *cfg.resolved_bbox())
    if crop.full_width and cfg.with_rivers:
        raise ValueError("rivers are not supported for full-width (world) builds yet; disable them")
    rows = band_rows or plan_bands(crop, cfg.samples_per_hex)
    rows = max(2, rows - rows % 2) if rows < crop.height else crop.height
    starts = list(range(0, crop.height, rows))
    log.info(
        "grid %d x %d global, crop %d x %d = %d hexes at %.1f km, %d band(s)%s",
        grid.global_width, grid.global_height, crop.width, crop.height, crop.cells, cfg.hex_km,
        len(starts), " (wraps east-west)" if crop.full_width else "",
    )

    if len(starts) == 1:
        part, tables, (sg, hex_idx, river_raster) = _build_part(cfg, crop, sources)
        return BuildResult(crop, _finalize(crop, part, cfg), tables, sg, hex_idx, river_raster)

    cache_dir = None
    if band_cache is not None:
        cache_dir = Path(band_cache) / _band_key(cfg, crop, rows, getattr(sources, "cache_tag", type(sources).__name__))
        log.info("band cache: %s", cache_dir)
    parts, tables = [], []
    for i, r0 in enumerate(starts):
        band = crop.row_band(r0, rows)
        cached = _load_band(cache_dir / f"band_{r0:05d}.npz") if cache_dir else None
        if cached is not None:
            log.info("band %d/%d (rows %d-%d): cached", i + 1, len(starts), band.row0, band.row0 + band.height - 1)
            part, tab = cached
        else:
            log.info("band %d/%d", i + 1, len(starts))
            part, tab, _ = _build_part(cfg, band, sources)
            if cache_dir:
                _save_band(cache_dir / f"band_{r0:05d}.npz", part, tab)
        parts.append(part)
        tables.append(tab)

    merged = {k: np.concatenate([pt[k] for pt in parts]) for k in _PART_KEYS if k not in ("country", "admin1")}
    country_table, merged["country"] = _merge_tables(
        [t["countries"] for t in tables], [pt["country"] for pt in parts], ["code", "name"]
    )
    admin1_table, merged["admin1"] = _merge_tables(
        [t["admin1"] for t in tables], [pt["admin1"] for pt in parts], ["code", "name", "country"]
    )
    del parts
    return BuildResult(crop, _finalize(crop, merged, cfg), {"countries": country_table, "admin1": admin1_table})
