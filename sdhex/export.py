"""Write the .sdhx binary consumed by Godot, plus JSON metadata and previews.

File layout (little-endian):
    0   4 bytes  magic "SDHX"
    4   u32      format version
    8   u32      header JSON length in bytes (N)
    12  N bytes  UTF-8 JSON header (grid, crop, layer table, lookup tables)
    ... layers, each starting at a 16-byte-aligned absolute offset given in the header.
Layer dtypes are only "u8" and "i32" so Godot can load them with
PackedByteArray.slice() and PackedByteArray.to_int32_array().
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

import numpy as np
from PIL import Image

from .config import TERRAIN_COLORS, TERRAIN_ENUM, WORLDCOVER_CLASSES, BuildConfig
from .hexgrid import NEIGHBOR_DIRS
from .pipeline import BuildResult

MAGIC = b"SDHX"
VERSION = 1
_DTYPES = {"u8": np.dtype("<u1"), "i32": np.dtype("<i4")}

ATTRIBUTION = [
    "Contains modified Copernicus DEM GLO-90 data, provided under COPERNICUS by the European Union and ESA.",
    "ESA WorldCover 2021 v200, (c) ESA WorldCover project / Contains modified Copernicus Sentinel data (2021) "
    "processed by ESA WorldCover consortium. CC BY 4.0.",
    "HydroRIVERS v1.0: Lehner, B., Grill G. (2013), Hydrological Processes 27(15): 2171-2186. "
    "Data available at www.hydrosheds.org.",
    "Made with Natural Earth. Free vector and raster map data @ naturalearthdata.com.",
]


def _align(n: int, a: int = 16) -> int:
    return (n + a - 1) // a * a


def header_dict(res: BuildResult, cfg: BuildConfig) -> dict:
    g = res.crop.grid
    return {
        "format": "sdhx",
        "version": VERSION,
        "region": cfg.region if cfg.bbox is None else "custom",
        "bbox": list(cfg.resolved_bbox()),
        "grid": {
            "hex_km": g.hex_km,
            "global_width": g.global_width,
            "global_height": g.global_height,
            "lat_top": g.lat_top,
            "lat_bottom": g.lat_bottom,
            "lon0": g.lon0,
            "dx_deg": g.dx,
            "dy_deg": g.dy,
            "layout": "pointy-top, odd-r offset, row 0 = north, idx = row * width + col",
            "neighbor_dirs_axial": [list(d) for d in NEIGHBOR_DIRS],
        },
        "crop": {
            "col0": res.crop.col0,
            "row0": res.crop.row0,
            "width": res.crop.width,
            "height": res.crop.height,
            "cells": res.crop.cells,
        },
        "terrain_enum": TERRAIN_ENUM,
        "landcover_classes": {str(k): v for k, v in WORLDCOVER_CLASSES.items()},
        "countries": res.tables.get("countries", []),
        "admin1": res.tables.get("admin1", []),
        "classify_params": {k: v for k, v in vars(cfg.classify).items()},
        "attribution": ATTRIBUTION,
    }


def write_sdhx(path: Path, header: dict, layers: dict) -> None:
    cells = header["crop"]["cells"]
    table = []
    blobs = []
    # Two passes: header size depends on offsets, offsets depend on header size.
    for _ in range(2):
        hjson = json.dumps({**header, "layers": table}, separators=(",", ":")).encode("utf-8")
        offset = _align(12 + len(hjson) + 256)
        table, blobs = [], []
        for name, layer in layers.items():
            arr = np.ascontiguousarray(layer.data, dtype=_DTYPES[layer.dtype])
            if arr.shape != (cells,):
                raise ValueError(f"layer {name} has shape {arr.shape}, expected ({cells},)")
            raw = arr.tobytes()
            table.append(
                {"name": name, "dtype": layer.dtype, "offset": offset, "bytes": len(raw), "description": layer.description}
            )
            blobs.append((offset, raw))
            offset = _align(offset + len(raw))
    hjson = json.dumps({**header, "layers": table}, separators=(",", ":")).encode("utf-8")
    first = table[0]["offset"] if table else _align(12 + len(hjson))
    if 12 + len(hjson) > first:
        raise RuntimeError("header grew past first layer offset")
    buf = bytearray(max([o + len(b) for o, b in blobs], default=first))
    buf[0:12] = MAGIC + struct.pack("<II", VERSION, len(hjson))
    buf[12 : 12 + len(hjson)] = hjson
    for o, b in blobs:
        buf[o : o + len(b)] = b
    path.write_bytes(bytes(buf))


def read_sdhx(path: Path) -> tuple[dict, dict[str, np.ndarray]]:
    """Reference reader mirroring godot/hex_map_data.gd."""
    data = Path(path).read_bytes()
    if data[:4] != MAGIC:
        raise ValueError("not an SDHX file")
    version, hlen = struct.unpack_from("<II", data, 4)
    if version != VERSION:
        raise ValueError(f"unsupported version {version}")
    header = json.loads(data[12 : 12 + hlen].decode("utf-8"))
    layers = {}
    for entry in header["layers"]:
        o, n = entry["offset"], entry["bytes"]
        layers[entry["name"]] = np.frombuffer(data[o : o + n], dtype=_DTYPES[entry["dtype"]])
    return header, layers


def _terrain_rgb(terrain: np.ndarray) -> np.ndarray:
    lut = np.zeros((256, 3), dtype=np.uint8)
    for k, c in TERRAIN_COLORS.items():
        lut[k] = c
    return lut[terrain]


def write_hex_texture(path: Path, res: BuildResult) -> None:
    """One texel per hex at (col, row): RGB terrain colour, A = flags. Usable as a Godot data texture."""
    c = res.crop
    rgb = _terrain_rgb(res.layers["terrain"].data).reshape(c.height, c.width, 3)
    a = res.layers["flags"].data.reshape(c.height, c.width, 1)
    Image.fromarray(np.concatenate([rgb, a], axis=2), "RGBA").save(path)


def write_preview(path: Path, res: BuildResult, max_px: int = 4096) -> None:
    """Hex-shaped render with country borders, rivers and faint hex outlines."""
    idx = res.hex_idx
    inside = idx >= 0
    safe = np.clip(idx, 0, None)
    terrain = res.layers["terrain"].data
    country = res.layers["country"].data
    img = _terrain_rgb(terrain[safe]).astype(np.float32)
    img[~inside] = (20, 20, 24)

    hex_edge = np.zeros(idx.shape, dtype=bool)
    hex_edge[:, :-1] |= idx[:, :-1] != idx[:, 1:]
    hex_edge[:-1, :] |= idx[:-1, :] != idx[1:, :]
    img[hex_edge & inside] *= 0.82

    cty = np.where(inside, country[safe], -2)
    border = np.zeros(idx.shape, dtype=bool)
    border[:, :-1] |= cty[:, :-1] != cty[:, 1:]
    border[:-1, :] |= cty[:-1, :] != cty[1:, :]
    land = terrain[safe] != TERRAIN_ENUM["OCEAN"]
    img[border & inside & land] = (25, 20, 18)

    if res.river_raster is not None:
        rr = res.river_raster
        img[rr == 1] = img[rr == 1] * 0.5 + np.array([60, 110, 200]) * 0.5
        img[rr >= 2] = (40, 95, 200)

    out = Image.fromarray(img.clip(0, 255).astype(np.uint8), "RGB")
    scale = min(1.0, max_px / max(out.size))
    if scale < 1.0:
        out = out.resize((int(out.width * scale), int(out.height * scale)), Image.LANCZOS)
    out.save(path)


def export(res: BuildResult, cfg: BuildConfig, out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    header = header_dict(res, cfg)
    paths = {
        "sdhx": out_dir / "hexmap.sdhx",
        "meta": out_dir / "hexmap.meta.json",
        "texture": out_dir / "hex_terrain.png",
        "preview": out_dir / "preview.png",
    }
    write_sdhx(paths["sdhx"], header, res.layers)
    _, layers = read_sdhx(paths["sdhx"])
    stats = {
        "terrain_counts": {
            name: int((layers["terrain"] == v).sum()) for name, v in TERRAIN_ENUM.items()
        },
        "land_hexes": int(((layers["flags"] & 1) > 0).sum()),
        "river_hexes": int((layers["river_class"] > 0).sum()),
        "navigable_hexes": int(((layers["flags"] & 16) > 0).sum()),
    }
    meta = {**header, "stats": stats, "layers": [
        {"name": k, "dtype": v.dtype, "description": v.description} for k, v in res.layers.items()
    ]}
    paths["meta"].write_text(json.dumps(meta, indent=2), encoding="utf-8")
    write_hex_texture(paths["texture"], res)
    write_preview(paths["preview"], res)
    return paths
