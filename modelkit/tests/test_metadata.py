import json

from modelkit.pipeline import run


def test_top_level_contract(metadata):
    assert list(metadata.keys()) == [
        "schema", "model", "floors", "confirmations", "warnings",
        "generated_by", "pipeline_version",
    ]
    assert metadata["schema"] == 1
    assert metadata["generated_by"] == "modelkit"
    assert metadata["pipeline_version"] == 1


def test_model_block(metadata):
    model = metadata["model"]
    assert set(model) == {"name", "source_sha256", "units", "up_axis"}
    assert model["name"] == "sample_house"
    assert model["units"] == "m"
    assert model["up_axis"] == "Y"
    assert len(model["source_sha256"]) == 64


def test_floor_blocks(metadata):
    assert len(metadata["floors"]) == 2
    for index, floor in enumerate(metadata["floors"]):
        assert floor["index"] == index
        assert floor["key"] == f"floor_{index}"
        assert set(floor) == {
            "index", "key", "name", "slab_top_y", "ceiling_y",
            "rooms", "walls", "outline",
        }
        for room in floor["rooms"]:
            assert set(room) == {"name", "polygon", "area_m2", "centroid"}
            assert room["polygon"][0] == room["polygon"][-1]
        for wall in floor["walls"]:
            assert set(wall) == {"x1", "z1", "x2", "z2", "thickness",
                                 "height", "type"}
            assert wall["type"] == "wall"
            assert wall["height"] > 0
        assert floor["outline"][0] == floor["outline"][-1]


def test_confirmations_contract(metadata):
    for item in metadata["confirmations"]:
        assert set(item) == {"id", "kind", "floor", "severity", "message",
                             "segment", "suggestion"}
        assert item["kind"] in {"stub", "jog", "chamfer", "gap"}
        assert item["severity"] in {"info", "warn"}
        assert len(item["segment"]) == 2
    kinds = {item["kind"] for item in metadata["confirmations"]}
    assert "stub" in kinds


def test_no_timestamps(metadata, pipeline_out):
    text = (pipeline_out / "metadata.json").read_text(encoding="utf-8").lower()
    for banned in ("timestamp", "generated_at", "created_at", "date-time"):
        assert banned not in text


def test_metadata_round_trip(metadata):
    assert json.loads(json.dumps(metadata)) == metadata


def test_metadata_and_model_are_byte_stable(tmp_path, fixture_glb):
    first = tmp_path / "first"
    second = tmp_path / "second"
    run(fixture_glb, first, name="sample_house")
    run(fixture_glb, second, name="sample_house")
    assert (first / "metadata.json").read_bytes() == (second / "metadata.json").read_bytes()
    assert (first / "model.glb").read_bytes() == (second / "model.glb").read_bytes()
    assert (first / "confirm-list.json").read_bytes() == (second / "confirm-list.json").read_bytes()
