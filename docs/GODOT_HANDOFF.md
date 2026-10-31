# Hand-off: integrating sd-hex-pipeline output into Strategic Deterrence (Godot)

> **For the coding agent.** Work in the Godot game repo. Do the phases in order and keep each phase
> as a separate commit or PR that leaves the game runnable. The source of truth for the file format is
> [`FORMAT.md`](FORMAT.md). The reference loader is
> [`godot/hex_map_data.gd`](../godot/hex_map_data.gd), which has been tested headless in Godot 4.7.

## 0. What the user has

A folder from Colab, e.g. `balkans_11km/`:

| File | Size (Balkans 11 km) | Goes into the game? |
|---|---|---|
| `hexmap.sdhx` | ~1 MB | **Yes**: all per-hex data (172 × 165 = 28,380 hexes) |
| `hexmap.meta.json` | ~5 KB | Optional. A human-readable copy of the header, useful for review and diffs |
| `hex_terrain.png` | tiny | No. Debug only (1 texel per hex) |
| `preview.png` | ~0.2–2 MB | No. Visual sanity check only |

Place the files as follows:

```
res://data/map/balkans_11km.sdhx          # rename freely
res://data/map/balkans_11km.meta.json     # optional
res://scripts/core/hex_map_data.gd        # copy from this repo, do not fork its logic
```

**Export setting (required):** in *Project → Export → (each preset) → Resources →
"Filters to export non-resource files"*, add `*.sdhx`. Otherwise the map is missing in exported builds.

## 1. Conventions the game must adopt

The pipeline uses these conventions. The game must match them rather than the other way round.

- Pointy-top hexes with **odd-r offset storage**: `idx = row * width + col`, row 0 = north, and odd rows are
  shifted half a hex east.
- Axial ↔ offset: `col = q + ((r - (r & 1)) >> 1)` and `q = col - ((r - (r & 1)) >> 1)`.
  Always use `>> 1` and never `/ 2`, because `/` truncates toward zero for negative numbers.
- Direction order `0 E, 1 NE, 2 NW, 3 W, 4 SW, 5 SE` =
  axial `(1,0) (1,-1) (0,-1) (-1,0) (-1,1) (0,1)`, the same as the existing `HexGrid.NEIGHBOR_DIRS`.
- The file is a **crop** of a global grid: `col0`, `row0` (row0 is always even) give its position in the world.
  Neighbours outside the crop are `-1`. East–west wrap only applies when the crop spans the full world width.
- `terrain` values follow `header.terrain_enum`:
  `OCEAN 0, GRASSLAND 1, DESERT 2, HILLS 3, MOUNTAIN 4, HIGH_MOUNTAIN 5, RIVER 6, IMPASSABLE 7`.

## 2. Phases

### Phase 1: load and validate (no gameplay change)
- Add `hex_map_data.gd` and load the `.sdhx` once at startup in the map/bootstrap code.
- At load, assert that every name in `header.terrain_enum` exists in `TerrainData` with the **same int value**.
  On any mismatch, fail loudly (`push_error` and refuse to start). Silent remaps cause subtle bugs.
- Log `width`, `height`, `cells`, the land hex count and the country count.
- **Acceptance:** a headless test (`godot --headless --script ...`) loads the file. It asserts that
  `cells == width * height` and that every layer length equals `cells`, and spot-checks one known hex.
  For example, the hex containing Belgrade (≈ 20.46°E, 44.82°N) is land and its country code is `SRB`.

### Phase 2: switch HexGrid storage from axial `r*W+q` to odd-r offset
- The current axial parallelogram layout cannot represent a rectangular lat/lon map, and it cannot wrap.
- `HexGrid` keeps its public axial API (`neighbor(q, r, d)`, distance, line, …). Internally it stores
  `idx = row * width + col` and converts with the formulas above.
- Build `_neighbors_flat` from `HexMapData.build_neighbors_flat()`. It has the same shape: 6 ints per hex, -1 for none.
- Update every place that computes `r * W + q` directly. Grep for it and route all of them through `HexGrid.idx()`.
- **Acceptance:** the existing pathfinding/unit tests pass. A new test checks that for every hex and direction,
  `neighbor(neighbor(i, d), (d + 3) % 6) == i` whenever both are ≥ 0.

### Phase 3: renderer (GPU single-quad shader)
- Upload `HexMapData.make_data_image()` as the per-hex data texture
  (`R terrain, G flags, B river_mask, A river_class`) with `texelFetch` and nearest filtering.
- In the fragment shader: pixel → fractional axial → cube_round → **convert to odd-r (col,row)** →
  `texelFetch(data_tex, ivec2(col, row), 0)`.
- Hex centre in world pixels for hex size `S` (centre to corner):
  `x = sqrt(3) * S * (col + 0.5 * (row & 1))` and `y = 1.5 * S * row`.
- Ownership/country lives in a separate `R32I`/`R16` texture. Do not reuse the 30-bit faction mask,
  because the Balkans crop alone has 10+ countries and the world has ~200.
- Borders: compare the owner of the current hex with the owner of the neighbour across the nearest edge,
  per fragment (analytic). This replaces the 1024-px JFA SDF, which cannot resolve hexes at this density.
- **Acceptance:** an in-game screenshot matches `preview.png` (coastlines, mountains, country borders in the
  same places).

### Phase 4: gameplay data
- Initial owners come from the `country` layer, mapped through `header.countries[id].code` (ISO A3) to
  the game's faction IDs. Keep this mapping in a data file, not in code.
- Provinces/states: use the `admin1` layer plus `header.admin1` as the administrative layer (supply,
  production and loyalty aggregate per admin1, not per hex).
- Movement and combat modifiers: use `terrain`, plus `forest_pct`, `urban_pct`, `wetland_pct` and `elev_std` as
  continuous modifiers rather than extra terrain types.
- Rivers: `river_class` (0–3) for crossing penalties. `river_connects(i, d)` / `FLAG_NAVIGABLE`
  for naval/barge movement along rivers.
- Area: hexes shrink east–west with latitude (`cos(lat)`). Where it matters (production, population,
  supply capacity), scale by `cos(center_lonlat(i).y)`.

### Phase 5 (later): satellite underlay
- Hex world space is linear in lon/lat: `px_per_degree = sqrt(3) * S / header.grid.dx_deg`, the same on both
  axes (because `dy_deg = 1.5 * dx_deg / sqrt(3)`).
- An equirectangular image (plain lon/lat, e.g. a 2:1 world image) aligns with one uniform scale plus an offset.
  Hex `(0,0)` of the global grid is at `lon0`, `lat_top`.
  For a crop, offset by `col0` and `row0`.
- Only use imagery whose licence allows commercial redistribution (see `DATA_SOURCES.md`).

## 3. Rules for the agent
- Keep core data scripts pure (`RefCounted`, `Packed*Array`, no Nodes) so they can port to C# later.
- Do not hand-edit `.sdhx` files. To change the data, change the pipeline and rebuild in Colab.
- Do not parse JSON per hex at runtime. Everything per-hex comes from Packed arrays.
- Ship the `header.attribution` strings in the game credits.
- When something in the data looks wrong (wrong terrain, misplaced border), report the hex `(col,row)` and
  `center_lonlat`. Open an issue in `zoranduric/sd-hex-pipeline` rather than patching around it in game code.
