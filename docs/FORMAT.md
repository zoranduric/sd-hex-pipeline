# `.sdhx` binary format (version 1)

Little-endian. Designed so that Godot can read each layer with a single
`PackedByteArray.slice()` (plus `to_int32_array()` for i32 layers).

```
offset  size  field
0       4     magic "SDHX"
4       4     u32 version (= 1)
8       4     u32 header_len
12      N     UTF-8 JSON header (header_len bytes)
...           zero padding
L_i     B_i   layer i raw data, at the absolute offset in header.layers[i].offset (16-byte aligned)
```

Each layer is `cells = crop.width * crop.height` values in row-major order
(`idx = row * width + col`), with row 0 = north and odd-r offset storage.
Only `u8` and `i32` dtypes are used.

## Header JSON

| Key | Meaning |
|---|---|
| `format`, `version` | `"sdhx"`, `1` |
| `region`, `bbox` | Region name (or `"custom"`) and requested `[min_lon, min_lat, max_lon, max_lat]` |
| `grid` | Global lattice: `hex_km`, `global_width`, `global_height`, `lat_top`, `lat_bottom`, `lon0`, `dx_deg`, `dy_deg`, `layout`, `neighbor_dirs_axial` (columns always wrap east–west) |
| `crop` | `col0`, `row0` (even), `width`, `height`, `cells` of this file inside the global grid |
| `terrain_enum` | `{name: value}` for the `terrain` layer |
| `landcover_classes` | WorldCover class code → name |
| `countries` | `[{id, code, name}]`; `country` layer indexes this (`-1` = none) |
| `admin1` | `[{id, code, name, country}]`; `admin1` layer indexes this (`-1` = none) |
| `classify_params` | Thresholds used for terrain classification |
| `attribution` | Required data credits |
| `layers` | `[{name, dtype, offset, bytes, description}]` |

Hex centre of global `(col, row)`:
`lon = lon0 + (col + 0.5 * (row & 1)) * dx_deg` (wrapped to ±180), `lat = lat_top - row * dy_deg`.

## Layers

| Name | Type | Meaning |
|---|---|---|
| `terrain` | u8 | Game terrain enum |
| `flags` | u8 | 1 land, 2 lake, 4 coastal, 8 river, 16 navigable river |
| `landcover` | u8 | Dominant WorldCover class on dry land (0 none) |
| `land_pct`, `lake_pct` | u8 | Share of the hex, 0–100 |
| `forest_pct`, `crop_pct`, `urban_pct`, `wetland_pct`, `bare_pct` | u8 | Share of dry land, 0–100 |
| `river_mask` | u8 | Bit *d* set = river continues to neighbour in direction *d* (E, NE, NW, W, SW, SE) |
| `river_class` | u8 | 0 none, 1 minor (≥ 50 m³/s), 2 major (≥ 200), 3 navigable (≥ 500) |
| `elev_mean`, `elev_min`, `elev_max`, `elev_std` | i32 | Metres, over dry land |
| `country`, `admin1` | i32 | Index into header tables, −1 none |

Readers must ignore unknown layers and header keys. New layers may be added without a version bump.
The reference readers are `sdhex.export.read_sdhx` (Python) and `godot/hex_map_data.gd` (GDScript),
and CI checks that they agree.
