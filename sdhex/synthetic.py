"""Offline synthetic world for tests and for iterating on the Godot loader without downloads."""

from __future__ import annotations

import geopandas as gpd
import numpy as np
from shapely.geometry import LineString, box

from .sampling import SampleGrid


class SyntheticSources:
    """West third is sea, a lake in the middle, mountains in the south-east,
    two countries split north/south of a big east-west river."""

    cache_tag = "synthetic-v1"

    def __init__(self, bbox):
        self.bbox = bbox
        lon_min, lat_min, lon_max, lat_max = bbox
        self.w = lon_max - lon_min
        self.h = lat_max - lat_min

    def _x(self, f):
        return self.bbox[0] + f * self.w

    def _y(self, f):
        return self.bbox[1] + f * self.h

    def _lonlat(self, sg: SampleGrid):
        lon = sg.lon_min + (np.arange(sg.nx) + 0.5) * sg.res
        lat = sg.lat_max - (np.arange(sg.ny) + 0.5) * sg.res
        return np.meshgrid(lon, lat)

    def elevation(self, sg: SampleGrid, need=None) -> np.ndarray:
        lon, lat = self._lonlat(sg)
        fx = (lon - self.bbox[0]) / self.w
        fy = (lat - self.bbox[1]) / self.h
        base = 100 + 300 * fx
        peak = 2600 * np.exp(-(((fx - 0.85) / 0.08) ** 2 + ((fy - 0.15) / 0.08) ** 2))
        ridges = 150 * np.sin(lon * 40) * (fx > 0.6)
        return (base + peak + ridges).astype(np.float32)

    def landcover(self, sg: SampleGrid, need=None) -> np.ndarray:
        lon, lat = self._lonlat(sg)
        fy = (lat - self.bbox[1]) / self.h
        fx = (lon - self.bbox[0]) / self.w
        lc = np.where(fy > 0.6, 10, 40).astype(np.uint8)
        lc[(np.abs(fx - 0.6) < 0.04) & (np.abs(fy - 0.45) < 0.04)] = 50
        # A glacier in the far north-east corner.
        lc[(fx > 0.9) & (fy > 0.9)] = 70
        return lc

    def biomes(self, bounds):
        # North: boreal; south: Mediterranean; a rainforest block; a gap (no polygon)
        # in the middle band to exercise neighbour filling.
        return gpd.GeoDataFrame(
            {"biome_num": [6, 12, 1]},
            geometry=[
                box(self._x(-0.5), self._y(0.55), self._x(1.5), self._y(1.5)),
                box(self._x(-0.5), self._y(-0.5), self._x(1.5), self._y(0.45)),
                box(self._x(0.7), self._y(0.05), self._x(0.8), self._y(0.3)),
            ],
            crs=4326,
        )

    def land(self, bounds):
        return gpd.GeoDataFrame(geometry=[box(self._x(0.33), self._y(-0.5), self._x(1.5), self._y(1.5))], crs=4326)

    def lakes(self, bounds):
        return gpd.GeoDataFrame(geometry=[box(self._x(0.45), self._y(0.7), self._x(0.55), self._y(0.85))], crs=4326)

    def countries(self, bounds):
        return gpd.GeoDataFrame(
            {"code": ["NOR", "SOU"], "name": ["Northland", "Southland"]},
            geometry=[
                box(self._x(-0.5), self._y(0.5), self._x(1.5), self._y(1.5)),
                box(self._x(-0.5), self._y(-0.5), self._x(1.5), self._y(0.5)),
            ],
            crs=4326,
        )

    def admin1(self, bounds):
        geoms, codes = [], []
        for i, (fx0, fy0) in enumerate([(-0.5, 0.5), (0.66, 0.5), (-0.5, -0.5), (0.66, -0.5)]):
            geoms.append(box(self._x(fx0), self._y(fy0), self._x(fx0 + 1.16 if fx0 < 0 else 1.5), self._y(fy0 + 1.0)))
            codes.append(f"R{i}")
        return gpd.GeoDataFrame(
            {"code": codes, "name": codes, "country": ["NOR", "NOR", "SOU", "SOU"]}, geometry=geoms, crs=4326
        )

    def rivers(self, bounds):
        big = LineString([(self._x(1.0), self._y(0.42)), (self._x(0.7), self._y(0.47)), (self._x(0.3), self._y(0.40))])
        minor = LineString([(self._x(0.6), self._y(0.95)), (self._x(0.62), self._y(0.47))])
        return gpd.GeoDataFrame({"discharge_cms": [2000.0, 80.0]}, geometry=[big, minor], crs=4326)
