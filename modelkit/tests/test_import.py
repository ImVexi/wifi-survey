import json

import numpy as np
import trimesh
from modelkit.floors import stage as floors_stage
from modelkit.meshio import import_mesh


def _transform_glb(source, target, matrix):
    mesh = trimesh.load(str(source), force="mesh")
    vertices = np.asarray(mesh.vertices, dtype=np.float64) @ matrix.T
    out = trimesh.Trimesh(vertices, np.asarray(mesh.faces), process=False)
    out.export(str(target))


def test_z_up_input_is_converted(tmp_path, fixture_glb):
    z_up = tmp_path / "z_up.glb"
    # (x, y, z) -> (x, -z, y): a proper rotation into a Z-up frame
    _transform_glb(fixture_glb, z_up, np.array([
        [1, 0, 0],
        [0, 0, -1],
        [0, 1, 0],
    ], dtype=float))
    work = tmp_path / "work"
    record = import_mesh(z_up, work)
    assert record["up_axis_detected"] == "Z"
    summary = floors_stage(work)
    assert summary["floors"] == 2
    assert summary["rooms"] == 10
    levels = summary["levels"]
    assert abs(levels[0]) < 0.05
    assert abs(levels[1] - 2.7) < 0.05


def test_millimetre_input_is_scaled(tmp_path, fixture_glb):
    mm = tmp_path / "mm.glb"
    _transform_glb(fixture_glb, mm, np.diag([1000.0, 1000.0, 1000.0]))
    work = tmp_path / "work"
    record = import_mesh(mm, work)
    assert record["scale_applied"] == 0.001
    assert record["units"] == "m"
    extent = [record["bbox"]["max"][i] - record["bbox"]["min"][i] for i in range(3)]
    assert 8.0 < extent[0] < 16.0
    summary = floors_stage(work)
    assert summary["floors"] == 2
    assert summary["rooms"] == 10


def test_import_record_has_no_absolute_paths(tmp_path, fixture_glb):
    work = tmp_path / "work"
    import_mesh(fixture_glb, work)
    text = (work / "import.json").read_text(encoding="utf-8")
    record = json.loads(text)
    assert record["source_name"] == fixture_glb.name
    assert "/" not in record["source_name"]
    assert "mesh_file" in record
