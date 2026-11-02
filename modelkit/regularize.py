"""Regularization: angle snapping, collinear merge, corner closure."""

from __future__ import annotations

import math
from pathlib import Path

from . import geom2d
from .io_utils import read_json, write_json

MIN_LENGTH = 0.12


def _snap_segment(seg):
    target = geom2d.snap_angle(seg["angle"])
    delta = math.radians(target - seg["angle"])
    if abs(delta) < 1e-9:
        return seg, False
    theta = math.radians(target)
    direction = (math.cos(theta), math.sin(theta))
    mx = (seg["x1"] + seg["x2"]) / 2.0
    mz = (seg["z1"] + seg["z2"]) / 2.0
    half = seg["length"] / 2.0
    p0 = (mx - direction[0] * half, mz - direction[1] * half)
    p1 = (mx + direction[0] * half, mz + direction[1] * half)
    out = geom2d.seg_make(p0, p1, seg["thickness"])
    return (out if out is not None else seg), True


def square_chamfers(segments, max_len=0.45, touch=0.10):
    """Replace a short diagonal between two perpendicular walls by a corner.

    Only fires when the diagonal has exactly two neighbours, both nearly
    perpendicular and both touching its endpoints.  The neighbours are
    extended/trimmed to their line intersection and the diagonal is dropped.
    """
    out = [dict(seg) for seg in segments]
    removed = set()
    count = 0
    for i, seg in enumerate(out):
        if i in removed:
            continue
        off_axis = min(seg["angle"] % 90.0, 90.0 - seg["angle"] % 90.0)
        if off_axis <= 5.0 or not (0.10 <= seg["length"] <= max_len):
            continue
        neighbours = []
        for j, other in enumerate(out):
            if i == j or j in removed:
                continue
            dist, end_a, end_b = geom2d._closest_endpoints(seg, other)
            if dist <= touch:
                neighbours.append((j, end_a, end_b))
        if len(neighbours) != 2:
            continue
        (j1, _ea1, end_b1), (j2, _ea2, end_b2) = neighbours
        first, second = out[j1], out[j2]
        if abs(((first["angle"] - second["angle"]) % 180.0) - 90.0) > 3.0:
            continue
        hit = geom2d.line_intersection(first, second)
        if hit is None:
            continue
        ends = [(seg["x1"], seg["z1"]), (seg["x2"], seg["z2"])]
        if min(math.hypot(hit[0] - x, hit[1] - z) for x, z in ends) > max_len:
            continue

        proposals = []
        for target, which in ((first, end_b1), (second, end_b2)):
            far = (target["x2"], target["z2"]) if which == "start" else (target["x1"], target["z1"])
            new_length = math.hypot(hit[0] - far[0], hit[1] - far[1])
            if new_length < 0.10:
                proposals = []
                break
            proposals.append((target, which, new_length))
        if not proposals:
            continue
        for target, which, _new_length in proposals:
            if which == "start":
                target["x1"], target["z1"] = float(hit[0]), float(hit[1])
            else:
                target["x2"], target["z2"] = float(hit[0]), float(hit[1])
            geom2d.seg_recompute(target)
        removed.add(i)
        count += 1
    out = [seg for k, seg in enumerate(out) if k not in removed]
    return out, count


def regularize(segments):
    """Return (segments, actions) with deterministic ordering."""
    start = len(segments)
    snapped = []
    snap_count = 0
    for seg in segments:
        new, changed = _snap_segment(seg)
        snapped.append(new)
        snap_count += int(changed)

    merged = geom2d.merge_collinear(snapped)
    chamfered, chamfer_count = square_chamfers(merged)
    extended = geom2d.extend_corners(chamfered)
    merged_again = geom2d.merge_collinear(extended)

    final = []
    for seg in merged_again:
        if seg["length"] < MIN_LENGTH:
            continue
        if not (0.04 <= seg["thickness"] <= 0.60):
            continue
        # keep the invariant "axes or 15 degree multiples"
        seg["angle"] = geom2d.q(geom2d.snap_angle(seg["angle"]), 3)
        final.append(seg)
    final.sort(key=lambda s: (round(s["angle"], 3), round(s["z1"], 3),
                             round(s["x1"], 3), round(s["length"], 3)))
    final = geom2d.seg_dedupe(final)
    actions = {
        "input": start,
        "snapped": snap_count,
        "merged_away": max(0, len(snapped) - len(merged)),
        "chamfers_squared": chamfer_count,
        "extended": max(0, len(merged_again) - len(chamfered)),
        "output": len(final),
    }
    return final, actions


def stage(work_dir):
    """CLI stage: regularize --work DIR."""
    work_dir = Path(work_dir)
    walls = read_json(work_dir / "walls.json")
    floors = []
    for floor in walls["floors"]:
        segments, actions = regularize(floor["segments"])
        floors.append({
            "index": int(floor["index"]),
            "segments": segments,
            "actions": actions,
        })
    record = {"schema": 1, "floors": floors, "warnings": list(walls.get("warnings", []))}
    write_json(work_dir / "regularized.json", record)
    return {
        "floors": len(floors),
        "segments": [len(f["segments"]) for f in floors],
        "actions": [f["actions"] for f in floors],
    }
