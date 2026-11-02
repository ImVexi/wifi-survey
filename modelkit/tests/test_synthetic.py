import hashlib

import trimesh
from modelkit.synthetic import build_house


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_synthetic_is_byte_deterministic(tmp_path):
    a = tmp_path / "a.glb"
    b = tmp_path / "b.glb"
    build_house(a, floors=2, rooms=10, seed=7)
    build_house(b, floors=2, rooms=10, seed=7)
    assert _sha(a) == _sha(b)


def test_synthetic_counts(tmp_path):
    path = tmp_path / "house.glb"
    info = build_house(path, floors=2, rooms=10, seed=7)
    assert info["floors"] == 2
    assert info["rooms"] == 10
    assert [f["rooms"] for f in info["per_floor"]] == [5, 5]
    assert sum(f["walls"] for f in info["per_floor"]) >= 16


def test_synthetic_node_names_are_generic(tmp_path):
    path = tmp_path / "house.glb"
    build_house(path, floors=2, rooms=10, seed=7)
    scene = trimesh.load(str(path), force="scene")
    nodes = list(scene.graph.nodes)
    assert "floor_0" in nodes
    assert any(name.startswith("floor_0__wall_") for name in nodes)
    assert any(name.startswith("floor_0__room_") for name in nodes)
    lowered = " ".join(nodes).lower()
    for banned in ("basement", "living", "garage", "main_floor", "upper"):
        assert banned not in lowered


def test_synthetic_seed_changes_layout_but_not_counts(tmp_path):
    a = tmp_path / "a.glb"
    b = tmp_path / "b.glb"
    info_a = build_house(a, floors=2, rooms=10, seed=7)
    info_b = build_house(b, floors=2, rooms=10, seed=11)
    assert info_a["per_floor"] == info_b["per_floor"]
    assert _sha(a) != _sha(b)
