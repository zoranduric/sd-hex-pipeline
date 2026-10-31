"""Dense lon/lat sample raster and per-hex aggregation helpers."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from rasterio.transform import Affine, from_origin

from .hexgrid import HexCrop


@dataclass(frozen=True)
class SampleGrid:
    """A north-up EPSG:4326 raster snapped to whole degrees at ``per_degree`` px/deg.

    Snapping to integer degrees makes 1x1 deg (DEM) and 3x3 deg (WorldCover)
    source tiles land on exact pixel boundaries, so no reprojection is needed.
    """

    lon_min: float
    lat_max: float
    per_degree: int
    nx: int
    ny: int

    @classmethod
    def covering(cls, bounds: tuple[float, float, float, float], per_degree: int) -> "SampleGrid":
        lon_min, lat_min, lon_max, lat_max = bounds
        lon_a = math.floor(lon_min)
        lat_b = math.ceil(lat_max)
        lon_b = math.ceil(lon_max)
        lat_a = math.floor(lat_min)
        return cls(
            float(lon_a),
            float(lat_b),
            per_degree,
            (lon_b - lon_a) * per_degree,
            (lat_b - lat_a) * per_degree,
        )

    @property
    def res(self) -> float:
        return 1.0 / self.per_degree

    @property
    def shape(self) -> tuple[int, int]:
        return (self.ny, self.nx)

    @property
    def transform(self) -> Affine:
        return from_origin(self.lon_min, self.lat_max, self.res, self.res)

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return (
            self.lon_min,
            self.lat_max - self.ny * self.res,
            self.lon_min + self.nx * self.res,
            self.lat_max,
        )

    def pixel_centers_row(self, row: int) -> tuple[np.ndarray, np.ndarray]:
        lon = self.lon_min + (np.arange(self.nx) + 0.5) * self.res
        lat = np.full(self.nx, self.lat_max - (row + 0.5) * self.res)
        return lon, lat

    def window_for(self, bounds: tuple[float, float, float, float]):
        """Pixel window (row0, col0, h, w) of ``bounds`` clipped to this grid, or None."""
        lon_min, lat_min, lon_max, lat_max = bounds
        c0 = max(0, int(round((lon_min - self.lon_min) * self.per_degree)))
        c1 = min(self.nx, int(round((lon_max - self.lon_min) * self.per_degree)))
        r0 = max(0, int(round((self.lat_max - lat_max) * self.per_degree)))
        r1 = min(self.ny, int(round((self.lat_max - lat_min) * self.per_degree)))
        if c1 <= c0 or r1 <= r0:
            return None
        return r0, c0, r1 - r0, c1 - c0

    def window_bounds(self, r0: int, c0: int, h: int, w: int) -> tuple[float, float, float, float]:
        return (
            self.lon_min + c0 * self.res,
            self.lat_max - (r0 + h) * self.res,
            self.lon_min + (c0 + w) * self.res,
            self.lat_max - r0 * self.res,
        )


def per_degree_for(crop: HexCrop, samples_per_hex: int) -> int:
    """Samples per degree so a hex is ~``samples_per_hex`` samples across."""
    return max(4, int(math.ceil(samples_per_hex / crop.grid.dx)))


def hex_index_raster(crop: HexCrop, sg: SampleGrid) -> np.ndarray:
    """Local hex index of every sample pixel (``-1`` outside the crop), int32."""
    out = np.empty(sg.shape, dtype=np.int32)
    for row in range(sg.ny):
        lon, lat = sg.pixel_centers_row(row)
        out[row] = crop.lonlat_to_index(lon, lat)
    return out


class Aggregator:
    """Fast per-hex reductions over a sample raster (sort once, reduce many)."""

    def __init__(self, hex_idx: np.ndarray, cells: int):
        flat = hex_idx.ravel()
        valid = np.flatnonzero(flat >= 0)
        order = valid[np.argsort(flat[valid], kind="stable")]
        self.cells = cells
        self.order = order
        self.sorted_idx = flat[order]
        self.count = np.bincount(self.sorted_idx, minlength=cells).astype(np.int64)
        self.present = np.flatnonzero(self.count)
        self.starts = np.searchsorted(self.sorted_idx, self.present)

    def _gather(self, values: np.ndarray) -> np.ndarray:
        return values.ravel()[self.order]

    def fraction(self, mask: np.ndarray) -> np.ndarray:
        hits = np.bincount(self.sorted_idx, weights=self._gather(mask).astype(np.float64), minlength=self.cells)
        return np.divide(hits, self.count, out=np.zeros(self.cells), where=self.count > 0)

    def masked_stats(self, values: np.ndarray, valid: np.ndarray):
        """Mean, std, min, max of ``values`` where ``valid`` per hex (NaN if none)."""
        v = self._gather(values).astype(np.float64)
        ok = self._gather(valid).astype(bool)
        idx = self.sorted_idx[ok]
        v = v[ok]
        n = np.bincount(idx, minlength=self.cells).astype(np.float64)
        s = np.bincount(idx, weights=v, minlength=self.cells)
        s2 = np.bincount(idx, weights=v * v, minlength=self.cells)
        with np.errstate(invalid="ignore", divide="ignore"):
            mean = s / n
            std = np.sqrt(np.maximum(s2 / n - mean * mean, 0.0))
        vmin = np.full(self.cells, np.nan)
        vmax = np.full(self.cells, np.nan)
        if idx.size:
            present = np.flatnonzero(n)
            starts = np.searchsorted(idx, present)
            vmin[present] = np.minimum.reduceat(v, starts)
            vmax[present] = np.maximum.reduceat(v, starts)
        return mean, std, vmin, vmax

    def dominant(self, categories: np.ndarray, ignore: int | None = None) -> tuple[np.ndarray, np.ndarray]:
        """Most common category per hex and its sample share. ``-1`` where none."""
        cat = self._gather(categories).astype(np.int64)
        idx = self.sorted_idx
        if ignore is not None:
            keep = cat != ignore
            cat = cat[keep]
            idx = idx[keep]
        result = np.full(self.cells, -1, dtype=np.int64)
        share = np.zeros(self.cells)
        if cat.size == 0:
            return result, share
        span = int(cat.max()) + 1
        keys, counts = np.unique(idx * span + cat, return_counts=True)
        hexes = keys // span
        # Sort by (hex, count) so the last entry per hex is its mode.
        order = np.lexsort((counts, hexes))
        hexes = hexes[order]
        last = np.r_[hexes[1:] != hexes[:-1], True]
        winners = order[last]
        result[keys[winners] // span] = keys[winners] % span
        share[keys[winners] // span] = counts[winners] / self.count[keys[winners] // span]
        return result, share
