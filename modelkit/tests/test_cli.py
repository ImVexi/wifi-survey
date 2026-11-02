import json
import os
import subprocess
import sys
import xml.dom.minidom
from pathlib import Path

from PIL import Image

REPO = Path(__file__).resolve().parents[2]


def cli(*args, cwd=None):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO)
    result = subprocess.run(
        [sys.executable, "-m", "modelkit", *args],
        cwd=str(cwd or REPO), env=env, capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_run_command_writes_all_outputs(tmp_path, fixture_glb):
    out = tmp_path / "out"
    stdout = cli("run", "--input", str(fixture_glb), "--out", str(out),
                 "--name", "sample_house")
    summary = json.loads(stdout)
    assert summary["floors"] == 2
    assert summary["rooms"] == 10
    assert summary["walls"] >= 16

    for name in ("model.glb", "metadata.json", "confirm-list.json",
                 "summary.json"):
        assert (out / name).is_file()
    for index in (0, 1):
        assert (out / "plans" / f"floor_{index}.svg").is_file()
        assert (out / "plans" / f"floor_{index}.png").is_file()

    metadata = json.loads((out / "metadata.json").read_text(encoding="utf-8"))
    assert len(metadata["floors"]) == 2

    svg = xml.dom.minidom.parse(str(out / "plans" / "floor_0.svg"))
    assert svg.documentElement.tagName == "svg"
    with Image.open(out / "plans" / "floor_0.png") as image:
        image.verify()
    assert (out / "plans" / "floor_0.svg").stat().st_size > 500
    assert (out / "plans" / "floor_0.png").stat().st_size > 1000


def test_stage_chain_commands(tmp_path, fixture_glb):
    work = tmp_path / "work"
    out = tmp_path / "out"
    cli("import", "--input", str(fixture_glb), "--out", str(work))
    assert (work / "import.json").is_file()
    assert (work / "mesh.npz").is_file()

    cli("floors", "--work", str(work))
    assert json.loads((work / "floors.json").read_text())["floors"]

    cli("walls", "--work", str(work))
    assert json.loads((work / "walls.json").read_text())["floors"]

    cli("regularize", "--work", str(work))
    assert json.loads((work / "regularized.json").read_text())["floors"]

    cli("confirm-list", "--work", str(work))
    assert "confirmations" in json.loads((work / "confirm-list.json").read_text())

    cli("build", "--work", str(work), "--out", str(out))
    assert (out / "model.glb").is_file()
    assert (out / "metadata.json").is_file()


def test_synthetic_module_entrypoint(tmp_path):
    target = tmp_path / "generated.glb"
    stdout = cli("synth", "--out", str(target))
    assert json.loads(stdout)["rooms"] == 10
    assert target.is_file() and target.stat().st_size > 1000

    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO)
    second = tmp_path / "direct.glb"
    result = subprocess.run(
        [sys.executable, "-m", "modelkit.synthetic", "--out", str(second),
         "--floors", "2", "--rooms", "10", "--seed", "7"],
        cwd=str(REPO), env=env, capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stderr
    assert second.read_bytes() == target.read_bytes()
