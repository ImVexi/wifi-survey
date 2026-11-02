from shapely.geometry import Point, Polygon


def test_ten_rooms_detected(floors_record):
    total = sum(len(f["rooms"]) for f in floors_record["floors"])
    assert total == 10
    assert [len(f["rooms"]) for f in floors_record["floors"]] == [5, 5]


def test_room_polygons_are_closed(floors_record):
    for floor in floors_record["floors"]:
        for room in floor["rooms"]:
            ring = room["polygon"]
            assert len(ring) >= 4
            assert ring[0] == ring[-1]


def test_room_areas_and_centroids(floors_record):
    for floor in floors_record["floors"]:
        for room in floor["rooms"]:
            polygon = Polygon(room["polygon"])
            assert polygon.is_valid
            assert room["area_m2"] == polygon.area or abs(room["area_m2"] - polygon.area) < 0.01
            assert room["area_m2"] > 1.5
            assert polygon.contains(Point(*room["centroid"]))


def test_slab_frame_is_split_into_rooms():
    from modelkit.rooms import _rooms_from_mass
    from shapely.geometry import Polygon

    outer = Polygon([(0, 0), (4, 0), (4, 3), (0, 3)])
    hole = Polygon([(0.5, 0.5), (3.5, 0.5), (3.5, 2.5), (0.5, 2.5)])
    frame = outer.difference(hole)
    rooms = _rooms_from_mass(frame)
    assert len(rooms) == 1
    assert abs(rooms[0].area - hole.area) < 0.05


def test_room_names_are_sequential_and_generic(floors_record):
    names = [room["name"] for floor in floors_record["floors"] for room in floor["rooms"]]
    assert names == [f"Room {i:02d}" for i in range(1, 11)]
    lowered = " ".join(names).lower()
    assert "basement" not in lowered and "garage" not in lowered
