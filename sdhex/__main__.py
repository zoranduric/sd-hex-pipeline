"""Command line: python -m sdhex build --region balkans --hex-km 11 --out out/"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from .config import REGIONS, BuildConfig
from .export import export
from .pipeline import build
from .sources import RemoteSources
from .synthetic import SyntheticSources


def _bbox(text: str) -> tuple[float, float, float, float]:
    parts = [float(p) for p in text.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("bbox must be lon_min,lat_min,lon_max,lat_max")
    lon_min, lat_min, lon_max, lat_max = parts
    if not (-180 <= lon_min < lon_max <= 180 and -90 <= lat_min < lat_max <= 90):
        raise argparse.ArgumentTypeError("bbox out of range or min >= max")
    return lon_min, lat_min, lon_max, lat_max


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sdhex", description="Bake real-world data into a hex grid for Godot.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="build one region")
    b.add_argument("--region", default="balkans", choices=sorted(REGIONS))
    b.add_argument("--bbox", type=_bbox, help="custom lon_min,lat_min,lon_max,lat_max (overrides --region)")
    b.add_argument("--hex-km", type=float, default=11.0, help="centre-to-centre hex spacing at the equator")
    b.add_argument("--samples-per-hex", type=int, default=10, help="sampling density across one hex")
    b.add_argument("--out", type=Path, default=Path("out"))
    b.add_argument("--cache", type=Path, default=Path("cache"))
    b.add_argument("--workers", type=int, default=8)
    b.add_argument("--river-regions", help="HydroRIVERS files to use, e.g. eu,as (default: chosen from the bbox)")
    b.add_argument("--no-landcover", action="store_true")
    b.add_argument("--no-rivers", action="store_true")
    b.add_argument("--no-admin1", action="store_true")
    b.add_argument("--synthetic", action="store_true", help="use a generated offline world (no downloads)")
    b.add_argument("-v", "--verbose", action="store_true")

    sub.add_parser("regions", help="list region presets")

    args = ap.parse_args(argv)
    if args.cmd == "regions":
        for name, bbox in REGIONS.items():
            print(f"{name:10s} {bbox}")
        return 0

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(message)s")
    if not 2.0 <= args.hex_km <= 200.0:
        ap.error("--hex-km must be between 2 and 200")
    if not 2 <= args.samples_per_hex <= 40:
        ap.error("--samples-per-hex must be between 2 and 40")

    cfg = BuildConfig(
        region=args.region,
        bbox=args.bbox,
        hex_km=args.hex_km,
        samples_per_hex=args.samples_per_hex,
        with_landcover=not args.no_landcover,
        with_rivers=not args.no_rivers,
        with_admin1=not args.no_admin1,
    )
    name = "synthetic" if args.synthetic else (args.region if args.bbox is None else "custom")
    out_dir = args.out / f"{name}_{args.hex_km:g}km"
    t0 = time.time()
    if args.synthetic:
        sources = SyntheticSources(cfg.resolved_bbox())
    else:
        sources = RemoteSources(
            args.cache, args.workers,
            river_regions=args.river_regions.split(",") if args.river_regions else None,
        )
    res = build(cfg, sources)
    paths = export(res, cfg, out_dir)
    meta = json.loads(paths["meta"].read_text())
    logging.info("done in %.0fs: %s", time.time() - t0, json.dumps(meta["stats"]))
    for k, p in paths.items():
        print(f"{k:8s} {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
