import pytest


def test_angles_are_axis_or_15_degree_multiples(regularized_record):
    for floor in regularized_record["floors"]:
        for seg in floor["segments"]:
            assert seg["angle"] % 15.0 == pytest.approx(0.0, abs=1e-6)


def test_no_zero_length_segments(regularized_record):
    for floor in regularized_record["floors"]:
        for seg in floor["segments"]:
            assert seg["length"] > 0.08


def test_thickness_is_physical(regularized_record):
    for floor in regularized_record["floors"]:
        for seg in floor["segments"]:
            assert 0.04 <= seg["thickness"] <= 0.6


def test_segments_stay_inside_the_footprint(regularized_record):
    for floor in regularized_record["floors"]:
        for seg in floor["segments"]:
            assert abs(seg["x1"]) <= 8.0
            assert abs(seg["x2"]) <= 8.0
            assert abs(seg["z1"]) <= 6.0
            assert abs(seg["z2"]) <= 6.0


def test_merge_and_snap_actions_are_reported(regularized_record):
    for floor in regularized_record["floors"]:
        actions = floor["actions"]
        assert set(actions) == {"input", "snapped", "merged_away",
                                "chamfers_squared", "extended", "output"}
        assert actions["input"] >= actions["output"]


def test_snap_angle_rules():
    from modelkit.geom2d import snap_angle

    assert snap_angle(3.0) == 0.0
    assert snap_angle(88.0) == 90.0
    assert snap_angle(38.0) == 45.0
    assert snap_angle(22.0) == 15.0


def test_short_chamfer_is_squared():
    from modelkit.geom2d import seg_make
    from modelkit.regularize import regularize

    segments = [
        seg_make((0.0, 0.0), (1.0, 0.0), 0.1),
        seg_make((1.0, 0.0), (1.2, 0.2), 0.1),
        seg_make((1.2, 0.2), (1.2, 1.0), 0.1),
    ]
    out, actions = regularize(segments)
    assert actions["chamfers_squared"] == 1
    assert len(out) == 2
    assert all(seg["angle"] % 90.0 == 0.0 for seg in out)
    ends = {(round(seg["x1"], 3), round(seg["z1"], 3)) for seg in out}
    ends |= {(round(seg["x2"], 3), round(seg["z2"], 3)) for seg in out}
    assert (1.2, 0.0) in ends


def test_regularize_is_deterministic(regularized_record):
    from modelkit.regularize import regularize

    for floor in regularized_record["floors"]:
        again, _ = regularize(list(floor["segments"]))
        assert again == floor["segments"]
