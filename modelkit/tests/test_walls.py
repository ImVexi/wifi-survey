def test_floor_zero_has_enough_segments(walls_record):
    floor0 = walls_record["floors"][0]
    assert len(floor0["segments"]) >= 8


def test_wall_segments_are_sane(walls_record):
    for floor in walls_record["floors"]:
        for seg in floor["segments"]:
            assert seg["length"] > 0.05
            assert 0.04 <= seg["thickness"] <= 0.5
            assert seg["x1"] != seg["x2"] or seg["z1"] != seg["z2"]


def test_exterior_walls_present(walls_record):
    floor0 = walls_record["floors"][0]
    lengths = sorted([seg["length"] for seg in floor0["segments"]], reverse=True)
    assert lengths[0] >= 10.0
    assert sum(1 for s in floor0["segments"] if s["thickness"] >= 0.15) >= 4


def test_regularized_floor_has_full_ring(regularized_record):
    floor0 = regularized_record["floors"][0]
    angles = [seg["angle"] for seg in floor0["segments"]]
    assert angles.count(0.0) >= 3
    assert angles.count(90.0) >= 3


def test_stub_is_preserved(regularized_record):
    floor0 = regularized_record["floors"][0]
    assert any(seg["length"] < 0.35 for seg in floor0["segments"])


def test_no_duplicate_segments(regularized_record):
    for floor in regularized_record["floors"]:
        seen = set()
        for seg in floor["segments"]:
            key = (round(seg["x1"], 2), round(seg["z1"], 2),
                   round(seg["x2"], 2), round(seg["z2"], 2))
            assert key not in seen
            seen.add(key)


def test_action_counts(regularized_record):
    for floor in regularized_record["floors"]:
        actions = floor["actions"]
        assert actions["output"] == len(floor["segments"])
        assert actions["output"] >= 8
