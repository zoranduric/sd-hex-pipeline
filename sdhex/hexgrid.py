"""Global pointy-top hex grid laid over an equirectangular (lon/lat) map.

Conventions (must stay in sync with the Godot side, see docs/FORMAT.md):

* Pointy-top hexes, "odd-r" offset storage: odd rows are shifted half a hex east.
* Row 0 is the northernmost row; rows increase southward (Godot +Z).
* Columns wrap east-west (``global_width`` columns around the equator).
* Axial <-> offset:  q = col - (row - (row & 1)) // 2,  col = q + (row - (row & 1)) // 2.
* Neighbour direction indices match ``HexGrid.NEIGHBOR_DIRS`` in the game:
  0 E (+1,0), 1 NE (+1,-1), 2 NW (0,-1), 3 W (-1,0), 4 SW (-1,+1), 5 SE (0,+1).

Because the grid is regular in lon/lat space, real-world hex area shrinks with
cos(latitude). That is the deliberate "flat map" trade-off.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

EQUATOR_KM = 40075.017
SQRT3 = math.sqrt(3.0)

NEIGHBOR_DIRS: tuple[tuple[int, int], ...] = (
    (1, 0),
    (1, -1),
    (0, -1),
    (-1, 0),
    (-1, 1),
    (0, 1),
)


def opposite_dir(d: int) -> int:
    return (d + 3) % 6


def offset_to_axial(col, row):
    row = np.asarray(row)
    return np.asarray(col) - (row - (row & 1)) // 2, row


def axial_to_offset(q, r):
    r = np.asarray(r)
    return np.asarray(q) + (r - (r & 1)) // 2, r


def cube_round(qf: np.ndarray, rf: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Round fractional axial coordinates to the containing hex (vectorised)."""
    xf = np.asarray(qf, dtype=np.float64)
    zf = np.asarray(rf, dtype=np.float64)
    yf = -xf - zf
    rx = np.rint(xf)
    ry = np.rint(yf)
    rz = np.rint(zf)
    dx = np.abs(rx - xf)
    dy = np.abs(ry - yf)
    dz = np.abs(rz - zf)
    fix_x = (dx > dy) & (dx > dz)
    fix_z = ~fix_x & ~(dy > dz)
    rx = np.where(fix_x, -ry - rz, rx)
    rz = np.where(fix_z, -rx - ry, rz)
    return rx.astype(np.int64), rz.astype(np.int64)


@dataclass(frozen=True)
class GlobalHexGrid:
    """A world-wrapping hex grid defined by hex spacing and a latitude band."""

    hex_km: float = 11.0
    lat_top: float = 84.0
    lat_bottom: float = -60.0

    @property
    def global_width(self) -> int:
        return max(6, int(round(EQUATOR_KM / self.hex_km)))

    @property
    def dx(self) -> float:
        """Degrees of longitude between east-west neighbours (= flat-to-flat width)."""
        return 360.0 / self.global_width

    @property
    def size_deg(self) -> float:
        """Hex circumradius in equatorial degrees."""
        return self.dx / SQRT3

    @property
    def dy(self) -> float:
        """Degrees of latitude between row centres."""
        return 1.5 * self.size_deg

    @property
    def lon0(self) -> float:
        return -180.0 + self.dx / 2.0

    @property
    def global_height(self) -> int:
        return int(math.floor((self.lat_top - self.lat_bottom) / self.dy)) + 1

    def center_lonlat(self, col, row) -> tuple[np.ndarray, np.ndarray]:
        col = np.asarray(col)
        row = np.asarray(row)
        lon = self.lon0 + (col + 0.5 * (row & 1)) * self.dx
        lon = (lon + 180.0) % 360.0 - 180.0
        lat = self.lat_top - row * self.dy
        return lon, lat

    def lonlat_to_offset(self, lon, lat) -> tuple[np.ndarray, np.ndarray]:
        """Return global (col, row) of the hex containing each lon/lat point.

        Columns are wrapped into ``[0, global_width)``; rows are not clamped.
        """
        px = (np.asarray(lon, dtype=np.float64) - self.lon0) / self.dx
        py = (self.lat_top - np.asarray(lat, dtype=np.float64)) / self.dy
        # Same linear map as Godot's world_to_axial, expressed in hex units.
        q, r = cube_round(px - 0.5 * py, py)
        col, row = axial_to_offset(q, r)
        return np.mod(col, self.global_width), row


@dataclass(frozen=True)
class HexCrop:
    """A rectangular window (in offset coordinates) of the global grid."""

    grid: GlobalHexGrid
    col0: int
    row0: int
    width: int
    height: int

    @property
    def cells(self) -> int:
        return self.width * self.height

    @classmethod
    def from_bbox(cls, grid: GlobalHexGrid, lon_min, lat_min, lon_max, lat_max) -> "HexCrop":
        if lon_min >= lon_max or lat_min >= lat_max:
            raise ValueError("bbox must be (lon_min, lat_min, lon_max, lat_max) with min < max")
        if lon_min < -180 or lon_max > 180:
            raise ValueError("bbox crossing the antimeridian is not supported yet")
        full_width = lon_min <= -180 and lon_max >= 180
        row_a = max(0, int(math.ceil((grid.lat_top - lat_max) / grid.dy)))
        row_b = min(grid.global_height - 1, int(math.floor((grid.lat_top - lat_min) / grid.dy)))
        # Keep the crop's first row even so local odd-r parity equals global parity.
        row_a -= row_a & 1
        if full_width:
            # Whole circumference: columns wrap, neighbours across +-180 connect.
            col_a, col_b = 0, grid.global_width - 1
        else:
            col_a = int(math.ceil((lon_min - grid.lon0) / grid.dx))
            col_b = int(math.floor((lon_max - grid.lon0) / grid.dx))
        if row_b < row_a or col_b < col_a:
            raise ValueError("bbox is smaller than one hex")
        return cls(grid, col_a, row_a, col_b - col_a + 1, row_b - row_a + 1)

    def local_index(self, gcol, grow) -> np.ndarray:
        """Map global offset coords to local flat indices (``-1`` when outside)."""
        gcol = np.asarray(gcol)
        grow = np.asarray(grow)
        lc = np.mod(gcol - self.col0, self.grid.global_width)
        lr = grow - self.row0
        inside = (lc < self.width) & (lr >= 0) & (lr < self.height)
        return np.where(inside, lr * self.width + lc, -1)

    def lonlat_to_index(self, lon, lat) -> np.ndarray:
        gcol, grow = self.grid.lonlat_to_offset(lon, lat)
        return self.local_index(gcol, grow)

    def centers(self) -> tuple[np.ndarray, np.ndarray]:
        rows, cols = np.divmod(np.arange(self.cells), self.width)
        return self.grid.center_lonlat(cols + self.col0, rows + self.row0)

    @property
    def full_width(self) -> bool:
        return self.width == self.grid.global_width

    def row_band(self, r0: int, n: int) -> "HexCrop":
        """Sub-crop of local rows [r0, r0 + n); r0 must be even to keep odd-r parity."""
        if r0 % 2:
            raise ValueError("band must start on an even row")
        return HexCrop(self.grid, self.col0, self.row0 + r0, self.width, min(n, self.height - r0))

    def lonlat_bounds(self, margin_hexes: float = 1.0) -> tuple[float, float, float, float]:
        m = margin_hexes * self.grid.dx
        lat_hi = self.grid.lat_top - self.row0 * self.grid.dy
        lat_lo = self.grid.lat_top - (self.row0 + self.height - 1) * self.grid.dy
        lat = (max(-90.0, lat_lo - m), min(90.0, lat_hi + m))
        if self.full_width:
            return (-180.0, lat[0], 180.0, lat[1])
        lon_lo = self.grid.lon0 + self.col0 * self.grid.dx
        lon_hi = self.grid.lon0 + (self.col0 + self.width - 1 + 0.5) * self.grid.dx
        return (max(-180.0, lon_lo - m), lat[0], min(180.0, lon_hi + m), lat[1])

    def neighbor(self, idx: int, d: int) -> int:
        """Local neighbour index in direction ``d`` (``-1`` outside the crop)."""
        lr, lc = divmod(int(idx), self.width)
        q, r = offset_to_axial(lc + self.col0, lr + self.row0)
        dq, dr = NEIGHBOR_DIRS[d]
        gcol, grow = axial_to_offset(q + dq, r + dr)
        return int(self.local_index(gcol, grow))

    def direction_between(self, a: int, b: int) -> int:
        """Direction index from local hex ``a`` to adjacent hex ``b``, or ``-1``."""
        for d in range(6):
            if self.neighbor(a, d) == b:
                return d
        return -1
