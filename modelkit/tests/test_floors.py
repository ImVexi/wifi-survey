import pytest


def test_two_floors_detected(floors_record):
    floors = floors_record["floors"]
    assert len(floors) == 2
    assert [f["index"] for f in floors] == [0, 1]
    assert [f["key"] for f in floors] == ["floor_0", "floor_1"]


def test_floor_heights(floors_record):
    tops = [f["slab_top_y"] for f in floors_record["floors"]]
    assert tops[0] == pytest.approx(0.0, abs=0.05)
    assert tops[1] == pytest.approx(2.7, abs=0.05)
    assert floors_record["floors"][0]["ceiling_y"] == pytest.approx(2.7, abs=0.05)


def test_every_floor_has_rooms_and_outline(floors_record):
    for floor in floors_record["floors"]:
        assert len(floor["rooms"]) >= 1
        assert floor["outline"]
        assert floor["outline"][0] == floor["outline"][-1]
        assert floor["ceiling_y"] > floor["slab_top_y"]


def test_floor_names_are_generic(floors_record):
    assert [f["name"] for f in floors_record["floors"]] == ["Floor 1", "Floor 2"]
