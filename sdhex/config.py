"""Region presets and tunable classification parameters."""

from __future__ import annotations

from dataclasses import dataclass, field

# (lon_min, lat_min, lon_max, lat_max) in WGS84 degrees.
REGIONS: dict[str, tuple[float, float, float, float]] = {
    # Slovenia -> Istanbul, Moldova -> Crete.
    "balkans": (13.0, 34.5, 30.0, 48.5),
    # Small fast smoke-test region around Belgrade (confluence of Sava and Danube).
    "belgrade": (19.5, 44.0, 21.5, 45.5),
    "finland": (19.0, 59.5, 32.0, 70.2),
    # Estonia, Latvia, Lithuania (+ Kaliningrad edge).
    "baltics": (20.5, 53.5, 28.5, 60.0),
    # Indian subcontinent incl. Pakistan, Nepal, Bangladesh, Sri Lanka.
    "india": (60.5, 5.5, 98.0, 37.5),
    # Egypt/Turkey -> Iran, Caucasus -> Yemen.
    "middle_east": (25.0, 12.0, 63.5, 42.5),
}

# Game terrain enum. Values match TerrainData in the Godot project.
TERRAIN_ENUM: dict[str, int] = {
    "OCEAN": 0,
    "GRASSLAND": 1,
    "DESERT": 2,
    "HILLS": 3,
    "MOUNTAIN": 4,
    "HIGH_MOUNTAIN": 5,
    "RIVER": 6,
    "IMPASSABLE": 7,
}

TERRAIN_COLORS: dict[int, tuple[int, int, int]] = {
    0: (38, 70, 120),
    1: (128, 168, 88),
    2: (214, 196, 140),
    3: (160, 150, 96),
    4: (128, 112, 96),
    5: (232, 232, 236),
    6: (70, 130, 190),
    7: (40, 40, 40),
}

# ESA WorldCover v200 class codes.
WORLDCOVER_CLASSES: dict[int, str] = {
    10: "tree_cover",
    20: "shrubland",
    30: "grassland",
    40: "cropland",
    50: "built_up",
    60: "bare_sparse",
    70: "snow_ice",
    80: "water",
    90: "herbaceous_wetland",
    95: "mangroves",
    100: "moss_lichen",
}

# Hex flags (u8 bitfield).
FLAG_LAND = 1 << 0
FLAG_LAKE = 1 << 1
FLAG_COASTAL = 1 << 2
FLAG_RIVER = 1 << 3
FLAG_NAVIGABLE = 1 << 4


@dataclass
class ClassifyParams:
    """Thresholds for turning sampled data into game terrain. Tune per taste."""

    land_min_frac: float = 0.5
    lake_min_frac: float = 0.5
    high_mountain_mean_m: float = 1800.0
    mountain_mean_m: float = 1200.0
    mountain_std_m: float = 250.0
    hills_mean_m: float = 500.0
    hills_std_m: float = 80.0
    desert_bare_frac: float = 0.5
    # Mean discharge (m^3/s, HydroRIVERS DIS_AV_CMS) per river class 1..3.
    river_class_min_cms: tuple[float, float, float] = (50.0, 200.0, 500.0)
    # Hexes carrying a river of at least this class become RIVER terrain
    # (only when they would otherwise be GRASSLAND, HILLS or DESERT). 0 disables.
    river_terrain_min_class: int = 3
    # Rivers of at least this class set FLAG_NAVIGABLE.
    navigable_min_class: int = 3


@dataclass
class BuildConfig:
    region: str = "balkans"
    bbox: tuple[float, float, float, float] | None = None
    hex_km: float = 11.0
    samples_per_hex: int = 10
    lat_top: float = 84.0
    lat_bottom: float = -60.0
    with_landcover: bool = True
    with_rivers: bool = True
    with_admin1: bool = True
    classify: ClassifyParams = field(default_factory=ClassifyParams)

    def resolved_bbox(self) -> tuple[float, float, float, float]:
        if self.bbox is not None:
            return self.bbox
        if self.region not in REGIONS:
            raise ValueError(f"unknown region '{self.region}', known: {sorted(REGIONS)}")
        return REGIONS[self.region]
