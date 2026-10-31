# sd-hex-pipeline

[![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/zoranduric/sd-hex-pipeline/blob/main/notebooks/build_balkans.ipynb)

Bakes real-world GIS data into a **pointy-top hex grid** for a Godot grand-strategy map
(*Strategic Deterrence*). Elevation, land cover, coastlines, lakes, countries, provinces and
rivers become flat per-hex arrays. Godot loads them in milliseconds with
`godot/hex_map_data.gd`.

Everything is built from data you can ship in a **commercial** game (see
[DATA_SOURCES.md](DATA_SOURCES.md)). Google Earth Engine is deliberately not used.

| | |
|---|---|
| Default region | Balkans / SE Europe, `13–30°E, 34.5–48.5°N` |
| Default hex size | 11 km centre-to-centre (≈ 1.45× Supreme Ruler's 16 km linear, ≈ 2.1× hex density) |
| Balkans @ 11 km | 172 × 165 = 28,380 hexes (download time dominates; expect minutes on Colab CPU) |
| Whole world @ 11 km | 3643 × 1683 grid, ~6.1M hexes total, roughly 1.5–2M land |

## Quick start (Google Colab)

1. Click [![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/zoranduric/sd-hex-pipeline/blob/main/notebooks/build_balkans.ipynb)
2. *Runtime → Run all* (CPU is fine). The preview is shown and a zip of the outputs downloads automatically.

**Private repo:** Colab asks you to authorise GitHub when opening the notebook. For the install step, add a Colab secret
`GH_TOKEN` holding a fine-grained token with read-only *Contents* access to this repo.

**Hex density:** set `HEX_KM` in the first cell. For the Balkans: 11 km = 28k hexes (≈2× Supreme Ruler),
9.2 km = 40k (≈3×), 8 km = 54k (≈4×), 5.5 km = 112k (≈8×).

To keep downloads cached between sessions, set `USE_DRIVE = True` in the first cell. Colab will then ask to
connect to your Google Drive, and outputs are saved to `MyDrive/sd-hex-pipeline/`.

## Quick start (local)

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
sdhex regions
sdhex build --region balkans --hex-km 11 --out out --cache cache -v
sdhex build --bbox 19.5,44,21.5,45.5 --hex-km 5     # custom area, finer hexes
sdhex build --synthetic                              # offline fake world, no downloads
pytest -q                                            # set GODOT_BIN to also test the GDScript loader
```

Outputs go to `out/<region>_<hexkm>km/`:

| File | Use |
|---|---|
| `hexmap.sdhx` | Binary layers for the game ([format](docs/FORMAT.md)) |
| `hexmap.meta.json` | Header + stats + layer docs, human readable |
| `hex_terrain.png` | One texel per hex: RGB terrain colour, A flags |
| `preview.png` | Hex render with borders and rivers, for eyeballing |
| `preview_biomes.png` | Same, coloured by biome |

## Regions

`sdhex regions` lists presets. Any area works with `--bbox lon_min,lat_min,lon_max,lat_max`.

| Preset | Hexes @ 11 km | @ 9.2 km | River files |
|---|---|---|---|
| `balkans` | 28k | 40k | eu |
| `finland` | 16k | 24k | eu |
| `baltics` | 6k | 9k | eu |
| `india` | 142k | 203k | eu, as, au |
| `middle_east` | 140k | 199k | eu, af, as |
| `world` | 6.1M | 8.7M | none (rivers not supported yet) |

- HydroRIVERS continent files are chosen automatically from the bbox. Override with `--river-regions eu,as`.
  Each file is a large one-off download (cached).
- **High latitudes:** hexes are regular in lon/lat, so in real distance they get narrower east–west by
  `cos(lat)`. At Finland's 65°N, an 11 km hex is about 4.6 km wide, so the map looks stretched compared with
  reality and you get ~2.4× more hexes per km² than in the tropics. This is the flat-map trade-off.
  For a Nordic-only game you could use a larger `--hex-km`.
- **Data gap:** Copernicus GLO-90 has no tiles for Armenia and Azerbaijan. Hexes there get no elevation
  (classified as flat), and the build logs a warning.

## Grid conventions (must match the game)

- Pointy-top hexes with **odd-r offset storage**: odd rows are shifted half a hex east.
  `idx = row * width + col`, and row 0 is north.
- Axial ↔ offset: `col = q + (r - (r & 1)) / 2` and `q = col - (r - (r & 1)) / 2` (floor division).
- Direction order is `E, NE, NW, W, SW, SE` = `(1,0) (1,-1) (0,-1) (-1,0) (-1,1) (0,1)` in axial.
- The world grid is one global equirectangular hex lattice (latitudes −60° to 84°) that **wraps east–west**.
  Each crop records `col0` and `row0` (row0 is always even), so regional builds tile into the
  same world grid later.
- On a flat lat/lon map a hex covers fewer real km² toward the poles (cos(lat)).
  At 45°N, an 11 km hex spans about 7.8 km east–west. This is the usual flat-map trade-off.
  Gameplay should use per-hex area or a latitude factor where it matters, such as supply or production.

## Godot integration

**Step-by-step plan for the game repo (give this to your coding agent):** [docs/GODOT_HANDOFF.md](docs/GODOT_HANDOFF.md)

```gdscript
var map := HexMapData.load_file("res://data/hexmap.sdhx")
var nbrs := map.build_neighbors_flat()          # PackedInt32Array, 6 per hex, -1 = edge
var tex := ImageTexture.create_from_image(map.make_data_image())  # R terrain G flags B river_mask A river_class
if map.flags[i] & HexMapData.FLAG_NAVIGABLE: ...
```

- `hex_map_data.gd` is pure data (`RefCounted`, Packed arrays, no Nodes), so it belongs in `scripts/core/`
  and ports 1:1 to C#.
- **Exporting the game:** add `*.sdhx` to *Export → Resources → Filters to export non-resource files*,
  or the file will be missing in exported builds.
- In the map shader, convert axial to odd-r before `texelFetch` (see the doc comment on `make_data_image`).
### Whole world

```bash
sdhex build --region world --hex-km 11 --workers 32 --cache /content/drive/MyDrive/sd-hex-pipeline/cache
```

- Lat 84°N to 60°S, full 360° with east–west wrap. Antarctica is left out. 11 km gives ~6.1M hexes and
  an `.sdhx` of ~230 MB. Rivers are switched off automatically for now.
- The world is processed in **latitude bands** (≤ 24M sample pixels each, ~6° of latitude at 11 km), so
  RAM stays at a few GB. Each finished band is saved to `<cache>/bands/`. If Colab disconnects, run again
  with the same settings and it resumes from the last finished band. **Use `USE_DRIVE = True`** so the
  cache survives the runtime.
- Banded output is identical to a single-pass build (tested).
- DEM and WorldCover tiles with no land are skipped. Expect tens of GB of streamed reads and several
  hours on the first run. Test with `--hex-km 50` first (a few minutes).
- **GPU does not help.** The pipeline is limited by network reads and CPU (polygon rasterising and
  per-hex statistics), not by maths a GPU could speed up. Choose a **High-RAM CPU** runtime and more
  `--workers` (parallel downloads). A GPU runtime just uses compute units faster. GPUs become useful later
  for satellite texture work (upscaling, de-lighting, normal maps).

- Rivers are stored per hex (`river_class`), and `river_mask` gives the directions the river connects.
  This supports both "river hex" movement costs and drawing river lines between hex centres.

## Terrain classification

Defaults are in `sdhex/config.py` (`ClassifyParams`), and the values used for a build are
written to the header. The first matching rule wins:

1. Land < 50 %, or lake ≥ 50 % → `OCEAN` (lakes are also flagged with `FLAG_LAKE`)
2. Mean elevation ≥ 1800 m → `HIGH_MOUNTAIN`
3. Mean ≥ 1200 m or std-dev ≥ 250 m → `MOUNTAIN`
4. Non-mountain hex with a navigable river (≥ 500 m³/s mean discharge) → `RIVER`
5. Mean ≥ 500 m or std-dev ≥ 80 m → `HILLS`
6. Bare/sparse (WorldCover 60) ≥ 50 % → `DESERT`
7. Otherwise → `GRASSLAND`

Snow/ice (WorldCover 70) ≥ 50 % of the hex overrides rules 2–7 with `IMPASSABLE`.

Forest, cropland, urban and wetland are **fraction layers** (`forest_pct`, `urban_pct`, …) plus the
dominant `landcover` class, not terrain types. Together with the elevation statistics, the game can
derive modifiers or reclassify without re-running the pipeline.

### Biomes

`terrain` is the **landform** (how hard to move and fight). `biome` is a separate **climate zone** layer
from RESOLVE Ecoregions 2017. It drives attrition, supply, seasons and visuals. Keeping them separate
avoids an explosion of combined types like "forested tropical hills":

| `biome` | Source (WWF biome) | Typical game use |
|---|---|---|
| 1 `TROPICAL_RAINFOREST` | 1 | Jungle: with high `forest_pct`, heavy attrition and slow movement |
| 2 `TROPICAL_DRY_FOREST` | 2, 3 | |
| 3 `TEMPERATE_FOREST` | 4, 5 | |
| 4 `BOREAL_FOREST` | 6 | Taiga, winter attrition |
| 5 `SAVANNA` | 7 | |
| 6 `STEPPE` | 8 | Temperate grassland |
| 7 `WETLAND` | 9 | Flooded grassland |
| 8 `ALPINE` | 10 | Montane grassland/shrub |
| 9 `TUNDRA` | 11 | |
| 10 `MEDITERRANEAN` | 12 | |
| 11 `DESERT` | 13 | Desert & xeric shrub: water supply, heat attrition |
| 12 `MANGROVE` | 14 | |
| 13 `ICE` | snow ≥ 50 % | Glaciers/ice caps (also `IMPASSABLE` terrain) |

`DESERT` terrain (bare ground) and `DESERT` biome are different on purpose. For example, a rocky desert
mountain is `MOUNTAIN` terrain in the `DESERT` biome. Water hexes have biome 0. Land hexes falling between
ecoregion polygons take the most common biome of their neighbours. Disable with `--no-biomes`.

## Limitations / not verified yet

- Real downloads have not been run end to end yet. The pipeline was developed in a sandbox without
  access to the data hosts, and is tested against a local HTTP range server plus a synthetic world.
  Expect to tweak things on the first Colab run, then please open an issue with the log (`-v`).
- Custom areas crossing the antimeridian (±180°) are rejected for now. Only the full-width world wraps.
- HydroRIVERS continent boxes in `sources.py` are approximate (generous). A bbox near a boundary may
  download one extra file.

## Roadmap

- Satellite base texture from commercial-safe imagery (e.g. NASA Blue Marble / Sentinel-2 cloudless
  composites with suitable licences), aligned to the same equirectangular extent.
- GHSL population per hex, for production and manpower.
- Rivers for whole-world builds (stream HydroRIVERS per band).
- Hex-edge rivers (rivers on edges instead of through hexes) as an option.
- Admin-1 → game regions/states clustering (where `opengs-maptool`-style Voronoi grouping fits).

## License

Code: MIT. Data you produce with it carries the source licences and attribution in
[DATA_SOURCES.md](DATA_SOURCES.md). The same attribution strings are embedded in every `.sdhx` header.
