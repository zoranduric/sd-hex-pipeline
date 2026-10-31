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
    # Whole grid (84N..60S, no Antarctica). Built in latitude bands; rivers not yet supported.
    "world": (-180.0, -60.0, 180.0, 84.0),
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

# Climate / vegetation zone, orthogonal to terrain (landform). A hex is e.g.
# HILLS + TROPICAL_RAINFOREST. Derived from RESOLVE Ecoregions 2017 biomes,
# with ICE from ESA WorldCover permanent snow/ice.
BIOME_ENUM: dict[str, int] = {
    "NONE": 0,
    "TROPICAL_RAINFOREST": 1,
    "TROPICAL_DRY_FOREST": 2,
    "TEMPERATE_FOREST": 3,
    "BOREAL_FOREST": 4,
    "SAVANNA": 5,
    "STEPPE": 6,
    "WETLAND": 7,
    "ALPINE": 8,
    "TUNDRA": 9,
    "MEDITERRANEAN": 10,
    "DESERT": 11,
    "MANGROVE": 12,
    "ICE": 13,
}

# RESOLVE/WWF BIOME_NUM -> BIOME_ENUM. Anything else (e.g. "Rock and Ice", N/A) -> 0,
# later filled from neighbours or overridden by ICE.
WWF_BIOME_TO_GAME: dict[int, int] = {
    1: 1,   # Tropical & Subtropical Moist Broadleaf Forests
    2: 2,   # Tropical & Subtropical Dry Broadleaf Forests
    3: 2,   # Tropical & Subtropical Coniferous Forests
    4: 3,   # Temperate Broadleaf & Mixed Forests
    5: 3,   # Temperate Conifer Forests
    6: 4,   # Boreal Forests/Taiga
    7: 5,   # Tropical & Subtropical Grasslands, Savannas & Shrublands
    8: 6,   # Temperate Grasslands, Savannas & Shrublands
    9: 7,   # Flooded Grasslands & Savannas
    10: 8,  # Montane Grasslands & Shrublands
    11: 9,  # Tundra
    12: 10, # Mediterranean Forests, Woodlands & Scrub
    13: 11, # Deserts & Xeric Shrublands
    14: 12, # Mangroves
}

BIOME_COLORS: dict[int, tuple[int, int, int]] = {
    0: (38, 70, 120),
    1: (20, 110, 40),
    2: (120, 150, 60),
    3: (60, 130, 70),
    4: (40, 90, 80),
    5: (190, 180, 90),
    6: (170, 175, 110),
    7: (80, 140, 140),
    8: (150, 140, 120),
    9: (160, 170, 160),
    10: (170, 150, 80),
    11: (225, 205, 150),
    12: (30, 120, 100),
    13: (240, 245, 250),
}

TERRAIN_COLORS: dict[int, tuple[int, int, int]] = {
    0: (38, 70, 120),
    1: (128, 168, 88),
    2: (214, 196, 140),
    3: (160, 150, 96),
    4: (128, 112, 96),
    5: (232, 232, 236),
    6: (70, 130, 190),
    7: (200, 215, 235),  # IMPASSABLE (mostly ice sheets)
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
    # Permanent snow/ice share (WorldCover class 70) that makes a hex IMPASSABLE
    # terrain and ICE biome (Greenland, Svalbard, big glaciers).
    ice_frac: float = 0.5
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
    with_biomes: bool = True
    classify: ClassifyParams = field(default_factory=ClassifyParams)

    def resolved_bbox(self) -> tuple[float, float, float, float]:
        if self.bbox is not None:
            return self.bbox
        if self.region not in REGIONS:
            raise ValueError(f"unknown region '{self.region}', known: {sorted(REGIONS)}")
        return REGIONS[self.region]
