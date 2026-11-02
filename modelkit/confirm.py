"""Confirmation list for ambiguous regularized geometry.

Nothing ambiguous is deleted automatically.  Short stubs, jogs, chamfers
and near-touching wall ends are reported here so a human (or the server
UI) can confirm or reject them.
"""

from __future__ import annotations

import math
from pathlib import Path

from . import geom2d
from .io_utils import read_json, write_json

STUB_MAX = 0.35
JOG_MIN = 0.05
JOG_MAX = 0.35
GAP_MIN = 0.03
GAP_MAX = 0.30


def _segment_json(seg):
    return [[geom2d.q(seg["x1"]), geom2d.q(seg["z1"])],
            [geom2d.q(seg["x2"]), geom2d.q(seg["z2"])]]


def _parallel_overlap(a, b):
    """(offset difference, overlap length) for two parallel segments."""
    if abs((a["angle"] - b["angle"]) % 180.0) > 0.6:
        return None
    theta = math.radians(a["angle"])
    direction = (math.cos(theta), math.sin(theta))
    normal = (-direction[1], direction[0])
    off_a = normal[0] * a["x1"] + normal[1] * a["z1"]
    off_b = normal[0] * b["x1"] + normal[1] * b["z1"]
    ta = sorted([direction[0] * a["x1"] + direction[1] * a["z1"],
                 direction[0] * a["x2"] + direction[1] * a["z2"]])
    tb = sorted([direction[0] * b["x1"] + direction[1] * b["z1"],
                 direction[0] * b["x2"] + direction[1] * b["z2"]])
    overlap = min(ta[1], tb[1]) - max(ta[0], tb[0])
    return abs(off_a - off_b), overlap


def _end_distance(a, b):
    best = None
    for pa in ((a["x1"], a["z1"]), (a["x2"], a["z2"])):
        for pb in ((b["x1"], b["z1"]), (b["x2"], b["z2"])):
            dist = math.hypot(pa[0] - pb[0], pa[1] - pb[1])
            if best is None or dist < best:
                best = dist
    return best


def build(floors_record, regularized_record):
    confirmations = []
    counters = {"stub": 0, "jog": 0, "chamfer": 0, "gap": 0}
    index_of = {f["index"]: f for f in floors_record["floors"]}

    def add(kind, floor, severity, message, segment, suggestion):
        counters[kind] += 1
        confirmations.append({
            "id": f"{kind}-{counters[kind]}",
            "kind": kind,
            "floor": floor["key"],
            "severity": severity,
            "message": message,
            "segment": segment,
            "suggestion": suggestion,
        })

    for entry in regularized_record["floors"]:
        floor = index_of.get(entry["index"], {"key": f"floor_{entry['index']}"})
        segments = entry["segments"]

        for seg in segments:
            if seg["length"] < STUB_MAX:
                add("stub", floor, "warn",
                    f"Short wall segment ({seg['length']:.2f} m) may be a stub.",
                    _segment_json(seg),
                    "Confirm whether this is a genuine return wall; "
                    "it is kept as-is until confirmed.")

        for i, seg in enumerate(segments):
            if abs(seg["angle"] % 90.0) > 0.5 and seg["length"] < 0.60:
                add("chamfer", floor, "info",
                    f"Short non-orthogonal segment ({seg['length']:.2f} m) "
                    f"at {seg['angle']:.0f} degrees.",
                    _segment_json(seg),
                    "Confirm the chamfer; squaring it would move the corner "
                    "by less than the face tolerance.")

            for other in segments[i + 1:]:
                pair = _parallel_overlap(seg, other)
                if pair is None:
                    continue
                offset, overlap = pair
                if JOG_MIN < offset <= JOG_MAX and overlap > 0.50:
                    add("jog", floor, "info",
                        f"Parallel walls offset by {offset:.2f} m over "
                        f"{overlap:.2f} m may form a jog.",
                        _segment_json(seg),
                        "Confirm the jog; if unintentional the two segments "
                        "can be merged onto one line.")
                if abs((seg["angle"] - other["angle"]) % 180.0 - 90.0) <= 0.6:
                    gap = _end_distance(seg, other)
                    if GAP_MIN <= gap <= GAP_MAX:
                        add("gap", floor, "warn",
                            f"Wall ends are {gap:.2f} m apart at a corner.",
                            _segment_json(seg),
                            "Confirm whether the gap is a doorway or an "
                            "unclosed corner before extending the wall.")

    return {"schema": 1, "confirmations": confirmations}


def stage(work_dir):
    """CLI stage: confirm-list --work DIR."""
    work_dir = Path(work_dir)
    floors = read_json(work_dir / "floors.json")
    regularized = read_json(work_dir / "regularized.json")
    record = build(floors, regularized)
    write_json(work_dir / "confirm-list.json", record)
    counts = {}
    for item in record["confirmations"]:
        counts[item["kind"]] = counts.get(item["kind"], 0) + 1
    return {"confirmations": len(record["confirmations"]), "by_kind": counts}
