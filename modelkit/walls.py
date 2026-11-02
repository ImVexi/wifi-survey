"""Wall extraction: pair opposing vertical faces into centre-line bands."""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
from shapely.geometry import Polygon

from . import geom2d
from .io_utils import read_json, read_mesh_npz, write_json

MIN_THICKNESS = 0.03
MAX_THICKNESS = 0.50
MIN_FACE_LENGTH = 0.15
MIN_BAND_LENGTH = 0.12
FACE_MERGE_GAP = 0.02
ANGLE_BUCKET = 1.0


def _face_segments(vertices, faces, y0, y1):
    """Vertical triangles projected to XZ as face segments with normals."""
    tri, normals, areas, centroids = geom2d.tri_props(vertices, faces)
    if len(tri) == 0:
        return []
    y_min = tri[:, :, 1].min(axis=1)
    y_max = tri[:, :, 1].max(axis=1)
    overlap = np.minimum(y_max, y1) - np.maximum(y_min, y0)
    height = max(y1 - y0, 1e-6)
    mask = (np.abs(normals[:, 1]) < 0.3) & (overlap > max(0.35 * height, 0.5))

    out = []
    for triangle, normal in zip(tri[mask], normals[mask]):
        pts = triangle[:, [0, 2]]
        pairs = ((0, 1), (1, 2), (2, 0))
        best = max(pairs, key=lambda p: float(np.linalg.norm(pts[p[0]] - pts[p[1]])))
        p0 = pts[best[0]]
        p1 = pts[best[1]]
        delta = p1 - p0
        length = float(np.linalg.norm(delta))
        if length < MIN_FACE_LENGTH:
            continue
        nxz = np.array([normal[0], normal[2]], dtype=float)
        nlen = float(np.linalg.norm(nxz))
        if nlen < 1e-6:
            continue
        nxz /= nlen
        direction = delta / length
        theta = math.degrees(math.atan2(direction[1], direction[0])) % 180.0
        canon = np.array([-math.sin(math.radians(theta)),
                          math.cos(math.radians(theta))])
        # intervals are measured along the canonical direction so that
        # faces traversed in opposite order still share one coordinate
        t0 = float(np.array([math.cos(math.radians(theta)),
                             math.sin(math.radians(theta))]) @ p0)
        t1 = float(np.array([math.cos(math.radians(theta)),
                             math.sin(math.radians(theta))]) @ p1)
        out.append({
            "angle": theta,
            "offset": float(canon @ p0),
            "t0": min(t0, t1),
            "t1": max(t0, t1),
            "normal": nxz,
            "sign": 1 if float(nxz @ canon) >= 0.0 else -1,
            "length": length,
        })
    return out


def _cluster_faces(face_segments):
    """Merge coplanar triangles into single face intervals."""
    buckets = {}
    for face in face_segments:
        key = (round(face["angle"], 1), round(face["offset"], 3), face["sign"])
        bucket = buckets.setdefault(key, {
            "angle": face["angle"],
            "offset": face["offset"],
            "intervals": [],
            "weights": {},
            "normal": face["normal"],
        })
        bucket["intervals"].append((min(face["t0"], face["t1"]),
                                    max(face["t0"], face["t1"])))
        sign = face["sign"]
        bucket["weights"][sign] = bucket["weights"].get(sign, 0.0) + face["length"]

    out = []
    for bucket in buckets.values():
        intervals = sorted(bucket["intervals"])
        merged = []
        lo, hi = intervals[0]
        for a, b in intervals[1:]:
            if a - hi <= FACE_MERGE_GAP:
                hi = max(hi, b)
            else:
                merged.append((lo, hi))
                lo, hi = a, b
        merged.append((lo, hi))

        sign = max(bucket["weights"], key=lambda s: bucket["weights"][s])
        theta = math.radians(bucket["angle"])
        direction = np.array([math.cos(theta), math.sin(theta)])
        normal = np.array([-direction[1], direction[0]]) * sign
        for lo, hi in merged:
            if hi - lo < MIN_FACE_LENGTH:
                continue
            out.append({
                "angle": bucket["angle"],
                "offset": bucket["offset"],
                "lo": lo,
                "hi": hi,
                "normal": normal,
                "sign": sign,
            })
    out.sort(key=lambda f: (round(f["angle"], 1), round(f["offset"], 4), f["lo"]))
    return out


def _pair_faces(faces):
    """Pair opposing faces of the same wall into centre-line bands."""
    buckets = {}
    for face in faces:
        key = round(face["angle"] / ANGLE_BUCKET) * ANGLE_BUCKET
        buckets.setdefault(key, []).append(face)

    bands = []
    for angle in sorted(buckets):
        group = buckets[angle]
        theta = math.radians(angle)
        direction = np.array([math.cos(theta), math.sin(theta)])
        normal = np.array([-direction[1], direction[0]])
        used = set()
        for i, a in enumerate(group):
            candidates = []
            for j, b in enumerate(group):
                if i == j or a["sign"] == b["sign"]:
                    continue
                thickness = abs(b["offset"] - a["offset"])
                if not (MIN_THICKNESS <= thickness <= MAX_THICKNESS):
                    continue
                lo = max(a["lo"], b["lo"])
                hi = min(a["hi"], b["hi"])
                if hi - lo < MIN_BAND_LENGTH:
                    continue
                candidates.append((thickness, -(hi - lo), j))
            if not candidates:
                continue
            # pair each face with its nearest opposing face only: this
            # avoids phantom bands across junctions with a wider gap
            thickness, _neg_overlap, j = min(candidates)
            pair = (min(i, j), max(i, j))
            if pair in used:
                continue
            used.add(pair)
            b = group[j]
            lo = max(a["lo"], b["lo"])
            hi = min(a["hi"], b["hi"])
            centre = (a["offset"] + b["offset"]) / 2.0
            p0 = normal * centre + direction * lo
            p1 = normal * centre + direction * hi
            seg = geom2d.seg_make(p0, p1, thickness)
            if seg is not None:
                bands.append(seg)

    seen = set()
    unique = []
    for seg in bands:
        key = (round(seg["x1"], 2), round(seg["z1"], 2),
               round(seg["x2"], 2), round(seg["z2"], 2),
               round(seg["thickness"], 2))
        if key in seen:
            continue
        seen.add(key)
        unique.append(seg)
    unique.sort(key=lambda s: (round(s["z1"], 3), round(s["x1"], 3),
                               round(s["angle"], 2), round(s["length"], 3)))
    return unique


def wall_mass(segments):
    """Union of wall band rectangles (diagnostics and room fallback)."""
    rectangles = []
    for seg in segments:
        dx = seg["x2"] - seg["x1"]
        dz = seg["z2"] - seg["z1"]
        length = math.hypot(dx, dz)
        if length < 1e-6:
            continue
        d = np.array([dx / length, dz / length])
        n = np.array([-d[1], d[0]]) * (seg["thickness"] / 2.0)
        p0 = np.array([seg["x1"], seg["z1"]])
        p1 = np.array([seg["x2"], seg["z2"]])
        rectangles.append(Polygon([p0 + n, p1 + n, p1 - n, p0 - n]))
    if not rectangles:
        return Polygon()
    from shapely.ops import unary_union

    mass = unary_union(rectangles)
    if not mass.is_valid:
        mass = mass.buffer(0)
    return mass


def extract(vertices, faces, y0, y1):
    """Raw wall bands plus a mass polygon for one storey."""
    segments = _cluster_faces(_face_segments(vertices, faces, y0, y1))
    bands = _pair_faces(segments)
    mass = wall_mass(bands)
    return bands, mass


def detect(vertices, faces, floors):
    out = []
    warnings = []
    for floor in floors:
        y0 = float(floor["slab_top_y"])
        y1 = float(floor["ceiling_y"])
        bands, mass = extract(vertices, faces, y0, y1)
        if not bands:
            warnings.append(f"floor {floor['index']}: no wall bands found")
        out.append({
            "index": int(floor["index"]),
            "segments": bands,
            "mass_area_m2": geom2d.q(mass.area, 3) if not mass.is_empty else 0.0,
        })
    return out, warnings


def stage(work_dir):
    """CLI stage: walls --work DIR."""
    work_dir = Path(work_dir)
    vertices, faces = read_mesh_npz(work_dir / "mesh.npz")
    floors = read_json(work_dir / "floors.json")
    detected, warnings = detect(vertices, faces, floors["floors"])
    record = {"schema": 1, "floors": detected, "warnings": warnings}
    write_json(work_dir / "walls.json", record)
    return {
        "floors": len(detected),
        "segments": [len(f["segments"]) for f in detected],
        "warnings": warnings,
    }
