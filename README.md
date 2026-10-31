# sd-hex-pipeline

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

Open [`notebooks/build_balkans.ipynb`](notebooks/build_balkans.ipynb) in Colab
(*File → Open notebook → GitHub* → `zoranduric/sd-hex-pipeline`) and choose *Run all*.
A CPU runtime is enough. Downloads are cached in Google Drive, so reruns are fast.

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
6. Bare/snow ≥ 50 % → `DESERT`
7. Otherwise → `GRASSLAND`

Forest, cropland, urban and wetland are **fraction layers** (`forest_pct`, `urban_pct`, …) plus the
dominant `landcover` class, not terrain types. Together with the elevation statistics, the game can
derive modifiers or reclassify without re-running the pipeline.

## Limitations / not verified yet

- Real downloads have not been run end to end yet. The pipeline was developed in a sandbox without
  access to the data hosts, and is tested against a local HTTP range server plus a synthetic world.
  Expect to tweak things on the first Colab run, then please open an issue with the log (`-v`).
- Areas crossing the antimeridian (±180°) are rejected for now.
- HydroRIVERS is fetched for Europe only (`eu`); other continents need their own zip (see `sources.py`).

## Roadmap

- Satellite base texture from commercial-safe imagery (e.g. NASA Blue Marble / Sentinel-2 cloudless
  composites with suitable licences), aligned to the same equirectangular extent.
- GHSL population per hex, for production and manpower.
- World builds processed in latitude bands, so a full world fits in Colab RAM.
- Hex-edge rivers (rivers on edges instead of through hexes) as an option.
- Admin-1 → game regions/states clustering (where `opengs-maptool`-style Voronoi grouping fits).

## License

Code: MIT. Data you produce with it carries the source licences and attribution in
[DATA_SOURCES.md](DATA_SOURCES.md). The same attribution strings are embedded in every `.sdhx` header.
