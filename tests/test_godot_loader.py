"""Run the GDScript loader in headless Godot against a Python-built .sdhx.

Skipped unless GODOT_BIN points at a Godot 4.x executable.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from sdhex.config import BuildConfig
from sdhex.export import export, read_sdhx
from sdhex.pipeline import build, neighbor_table
from sdhex.synthetic import SyntheticSources

GODOT = os.environ.get("GODOT_BIN")
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(not GODOT, reason="set GODOT_BIN to run the Godot loader check")
def test_godot_loader_matches_python(tmp_path):
    bbox = (19.5, 44.0, 21.5, 45.5)
    cfg = BuildConfig(bbox=bbox, hex_km=11.0)
    res = build(cfg, SyntheticSources(bbox))
    paths = export(res, cfg, tmp_path / "out")
    _, layers = read_sdhx(paths["sdhx"])
    lon, lat = res.crop.centers()
    probes = [0, res.crop.cells // 2, res.crop.cells - 1, res.crop.width * 3 + 1]
    expected = {
        "width": res.crop.width,
        "height": res.crop.height,
        "sums": {k: int(v.astype(np.int64).sum()) for k, v in layers.items()},
        "neighbors_flat": neighbor_table(res.crop).ravel().tolist(),
        "centers": [{"i": i, "lon": float(lon[i]), "lat": float(lat[i])} for i in probes],
    }
    exp_path = tmp_path / "expected.json"
    exp_path.write_text(json.dumps(expected))

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "project.godot").write_text('config_version=5\n\n[application]\nconfig/name="loader-check"\n')
    shutil.copy(ROOT / "godot" / "hex_map_data.gd", proj)
    shutil.copy(ROOT / "godot" / "tests" / "check_loader.gd", proj)

    subprocess.run([GODOT, "--headless", "--path", str(proj), "--import"], capture_output=True, timeout=180)
    out = subprocess.run(
        [GODOT, "--headless", "--path", str(proj), "--script", "res://check_loader.gd", "--",
         str(paths["sdhx"]), str(exp_path)],
        capture_output=True, text=True, timeout=180,
    )
    assert out.returncode == 0 and "LOADER_OK" in out.stdout, out.stdout + out.stderr
    assert "SCRIPT ERROR" not in out.stderr, out.stderr
