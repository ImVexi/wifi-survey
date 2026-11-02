"""Plan-frame geometry helpers (XZ plane, metres).

All stages share this module so that rounding, angle snapping and the
segment data structure stay consistent across the pipeline.
"""

from __future__ import annotations

import math

import numpy as np
from shapely.geometry import Polygon
from shapely.ops import unary_union
from shapely.prepared import prep

# ---------------------------------------------------------------- numbers


def q(value, nd=4):
    """Round for output; normalises negative zero and tiny noise."""
    value = float(value)
    if abs(value) < 0.5 * 10.0 ** (-nd):
        value = 0.0
    return round(value, nd)


# ---------------------------------------------------------------- shapely


def polys(geom):
    """Flat list of polygons from any shapely geometry."""
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom]
    if geom.geom_type == "MultiPolygon":
        return list(geom.geoms)
    return [g for g in getattr(geom, "geoms", []) if g.geom_type == "Polygon"]


def clean_polygon(geom, eps=0.004):
    """Remove slivers and duplicate vertices without moving faces."""
    if geom is None or geom.is_empty:
        return Polygon()
    out = geom.buffer(eps, join_style=2).buffer(-eps, join_style=2)
    if not out.is_valid:
        out = out.buffer(0)
    return out


def ring_of(poly):
    """Exterior ring as closed [[x, z], ...] with rounded coordinates."""
    return [[q(x), q(z)] for x, z in poly.exterior.coords]


def outline_of(polygons, buffer=0.06):
    """Footprint outline (exterior ring only) of a list of room polygons."""
    union = unary_union([p for p in polygons if not p.is_empty])
    if union.is_empty:
        return []
    grown = union.buffer(buffer, join_style=2).buffer(0)
    parts = polys(grown)
    if not parts:
        return []
    best = max(parts, key=lambda p: p.area)
    return ring_of(best)


def ensure_ccw(poly):
    """Return the polygon with a counter-clockwise exterior ring."""
    if poly.exterior.is_ccw:
        return poly
    from shapely.geometry import Polygon as _P

    return _P(list(poly.exterior.coords)[::-1], [list(r.coords) for r in poly.interiors])


# ---------------------------------------------------------------- triangles


def tri_props(V, F):
    """Triangle vertices, unit normals, areas and centroids."""
    tri = np.asarray(V, dtype=np.float64)[np.asarray(F, dtype=np.int64)]
    cross = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    length = np.linalg.norm(cross, axis=1)
    ok = length > 1e-12
    tri = tri[ok]
    length = length[ok]
    normals = cross[ok] / length[:, None]
    areas = 0.5 * length
    centroids = tri.mean(axis=1)
    return tri, normals, areas, centroids


def _weighted_percentile(values, weights, pct):
    order = np.argsort(values, kind="stable")
    values = values[order]
    weights = weights[order]
    cumulative = np.cumsum(weights)
    if cumulative[-1] <= 0:
        return float(values[-1])
    idx = int(np.searchsorted(cumulative / cumulative[-1], pct / 100.0))
    return float(values[min(idx, len(values) - 1)])


def plane_peaks(tri, normals, areas, centroids, axis=1, sign=1,
                binw=0.02, min_area=1.0, merge=0.15, rel=0.15):
    """Area weighted histogram peaks of horizontal faces.

    Faces whose normal points along ``sign * axis`` are binned by the
    centroid coordinate; local maxima with at least ``min_area`` m2 are
    merged when closer than ``merge`` metres. Peaks smaller than
    ``rel`` * largest peak are dropped (wall caps, window sills, ...).
    """
    dot = normals[:, axis] * sign
    mask = dot > 0.9
    if not mask.any():
        return []
    values = centroids[mask, axis]
    weights = areas[mask]
    lo = float(values.min())
    hi = float(values.max())
    nbins = max(1, int(math.ceil((hi - lo) / binw)) + 1)
    hist, edges = np.histogram(values, bins=nbins,
                               range=(lo, lo + nbins * binw), weights=weights)
    centers = edges[:-1] + binw / 2.0

    local = [i for i in range(nbins)
             if hist[i] >= min_area
             and (i == 0 or hist[i] >= hist[i - 1])
             and (i == nbins - 1 or hist[i] >= hist[i + 1])]
    local.sort(key=lambda i: (-hist[i], centers[i]))

    kept = []
    for i in local:
        if all(abs(centers[i] - centers[j]) > merge for j in kept):
            kept.append(i)
    if not kept:
        return []

    biggest = max(hist[i] for i in kept)
    floor = max(min_area, rel * biggest)
    out = []
    for i in sorted(kept, key=lambda k: centers[k]):
        if hist[i] < floor:
            continue
        near = np.abs(values - centers[i]) <= max(merge, 0.10)
        if not near.any():
            continue
        y_mean = float(np.average(values[near], weights=weights[near]))
        y_top = _weighted_percentile(values[near], weights[near], 90.0)
        out.append({
            "y": y_mean,
            "top": y_top,
            "area": float(hist[i]),
            "cluster_area": float(weights[near].sum()),
            "faces": int(near.sum()),
        })
    return out


# ---------------------------------------------------------------- segments
#
# Segment dict layout (JSON friendly, deterministic):
#   x1, z1, x2, z2 : endpoints in metres (centre line)
#   thickness      : wall thickness in metres
#   length         : computed length
#   angle          : degrees, 0..180, snapped to 15 degree multiples


def snap_angle(degrees, axis_tol=5.0):
    """Snap to 0/90 when within ``axis_tol``, else to the nearest 15 deg."""
    angle = float(degrees) % 180.0
    for candidate in (0.0, 90.0, 180.0):
        if abs(angle - candidate) <= axis_tol:
            return 0.0 if candidate == 180.0 else candidate
    return round(angle / 15.0) * 15.0 % 180.0


def seg_make(p0, p1, thickness):
    x1, z1 = float(p0[0]), float(p0[1])
    x2, z2 = float(p1[0]), float(p1[1])
    dx, dz = x2 - x1, z2 - z1
    length = math.hypot(dx, dz)
    if length <= 0.0:
        return None
    angle = math.degrees(math.atan2(dz, dx)) % 180.0
    return {
        "x1": q(x1), "z1": q(z1),
        "x2": q(x2), "z2": q(z2),
        "thickness": q(max(0.04, min(float(thickness), 0.60))),
        "length": q(length),
        "angle": q(angle, 3),
    }


def seg_dir(seg):
    dx = seg["x2"] - seg["x1"]
    dz = seg["z2"] - seg["z1"]
    length = math.hypot(dx, dz)
    if length <= 1e-12:
        return (1.0, 0.0)
    return (dx / length, dz / length)


def seg_points(seg):
    return ((seg["x1"], seg["z1"]), (seg["x2"], seg["z2"]))


def seg_recompute(seg):
    p0, p1 = seg_points(seg)
    dx, dz = p1[0] - p0[0], p1[1] - p0[1]
    seg["length"] = q(math.hypot(dx, dz))
    seg["angle"] = q(math.degrees(math.atan2(dz, dx)) % 180.0, 3)
    return seg


def seg_dedupe(segs, tol=1e-3):
    seen = set()
    out = []
    for seg in segs:
        key = (round(seg["x1"], 3), round(seg["z1"], 3),
               round(seg["x2"], 3), round(seg["z2"], 3),
               round(seg["thickness"], 3))
        if key in seen:
            continue
        seen.add(key)
        out.append(seg)
    return out


def merge_collinear(segs, angle_tol=0.6, offset_tol=0.05, gap_tol=0.30):
    """Merge segments that lie on the same line and nearly touch."""
    groups = {}
    for seg in segs:
        groups.setdefault(round(seg["angle"], 1), []).append(seg)

    merged = []
    for angle in sorted(groups):
        theta = math.radians(angle)
        direction = np.array([math.cos(theta), math.sin(theta)])
        normal = np.array([-direction[1], direction[0]])
        items = []
        for seg in groups[angle]:
            p0 = np.array([seg["x1"], seg["z1"]], dtype=float)
            p1 = np.array([seg["x2"], seg["z2"]], dtype=float)
            t0 = float(p0 @ direction)
            t1 = float(p1 @ direction)
            items.append({
                "offset": float(p0 @ normal),
                "lo": min(t0, t1),
                "hi": max(t0, t1),
                "thickness": seg["thickness"],
                "length": seg["length"],
            })
        items.sort(key=lambda it: (round(it["offset"], 4), it["lo"]))
        current = None
        for item in items:
            if current is None:
                current = dict(item)
                continue
            same_line = abs(item["offset"] - current["offset"]) <= offset_tol
            touches = item["lo"] - current["hi"] <= gap_tol
            if same_line and touches:
                total = current["length"] + item["length"]
                if total > 0:
                    current["offset"] = (
                        (current["offset"] * current["length"] +
                         item["offset"] * item["length"]) / total)
                    current["thickness"] = (
                        (current["thickness"] * current["length"] +
                         item["thickness"] * item["length"]) / total)
                current["hi"] = max(current["hi"], item["hi"])
                current["lo"] = min(current["lo"], item["lo"])
                current["length"] = current["hi"] - current["lo"]
            else:
                merged.append((angle, current))
                current = dict(item)
        if current is not None:
            merged.append((angle, current))

    out = []
    for angle, item in merged:
        theta = math.radians(angle)
        direction = np.array([math.cos(theta), math.sin(theta)])
        normal = np.array([-direction[1], direction[0]])
        base = normal * item["offset"]
        p0 = base + direction * item["lo"]
        p1 = base + direction * item["hi"]
        seg = seg_make(p0, p1, item["thickness"])
        if seg is not None and seg["length"] >= 0.08:
            out.append(seg)
    out.sort(key=lambda s: (s["angle"], round(s["z1"], 3), round(s["x1"], 3)))
    return seg_dedupe(out)


def line_intersection(seg_a, seg_b):
    """Intersection of two infinite lines (2-D), or None when parallel."""
    p = np.array([seg_a["x1"], seg_a["z1"]], dtype=float)
    r = np.array(seg_dir(seg_a), dtype=float)
    qq = np.array([seg_b["x1"], seg_b["z1"]], dtype=float)
    s = np.array(seg_dir(seg_b), dtype=float)
    denom = r[0] * s[1] - r[1] * s[0]
    if abs(denom) < 1e-9:
        return None
    diff = qq - p
    t = (diff[0] * s[1] - diff[1] * s[0]) / denom
    return p + r * t


def _closest_endpoints(a, b):
    """Nearest endpoint pair of two segments: (distance, end_a, end_b)."""
    ends_a = ("start", "end")
    ends_b = ("start", "end")
    best = None
    for ea in ends_a:
        pa = np.array([a["x1"], a["z1"]] if ea == "start"
                      else [a["x2"], a["z2"]], dtype=float)
        for eb in ends_b:
            pb = np.array([b["x1"], b["z1"]] if eb == "start"
                          else [b["x2"], b["z2"]], dtype=float)
            dist = float(np.linalg.norm(pa - pb))
            if best is None or dist < best[0]:
                best = (dist, ea, eb)
    return best


def extend_corners(segs, max_gap=0.30):
    """Move loose endpoints onto the perpendicular wall line they nearly meet."""
    out = [dict(s) for s in segs]
    for i, seg in enumerate(out):
        for end in ("start", "end"):
            point = np.array(
                [seg["x1"], seg["z1"]] if end == "start"
                else [seg["x2"], seg["z2"]], dtype=float)
            angle = seg["angle"]
            best = None
            for j, other in enumerate(out):
                if i == j:
                    continue
                delta = abs((other["angle"] - angle) % 180.0)
                if abs(delta - 90.0) > 3.0:
                    continue
                other_dir = np.array(seg_dir(other), dtype=float)
                other_normal = np.array([-other_dir[1], other_dir[0]])
                offset = float(other_normal @ (point -
                                np.array([other["x1"], other["z1"]])))
                t = float(other_dir @ (point - np.array([other["x1"], other["z1"]])))
                if not (-0.06 <= t <= other["length"] + 0.06):
                    continue
                if not (0.01 <= abs(offset) <= max_gap):
                    continue
                score = abs(offset)
                if best is None or score < best[0]:
                    best = (score, j)
            if best is None:
                continue
            other = out[best[1]]
            hit = line_intersection(seg, other)
            if hit is None:
                continue
            # never shorten a wall: only extend a loose end outwards
            far = np.array(
                [seg["x2"], seg["z2"]] if end == "start"
                else [seg["x1"], seg["z1"]], dtype=float)
            old_length = float(np.linalg.norm(point - far))
            new_length = float(np.linalg.norm(hit - far))
            if new_length < old_length - 1e-6:
                continue
            if new_length > old_length + max_gap + 0.05:
                continue
            if end == "start":
                seg["x1"], seg["z1"] = float(hit[0]), float(hit[1])
            else:
                seg["x2"], seg["z2"] = float(hit[0]), float(hit[1])
    for seg in out:
        seg_recompute(seg)
        seg["x1"], seg["z1"] = q(seg["x1"]), q(seg["z1"])
        seg["x2"], seg["z2"] = q(seg["x2"]), q(seg["z2"])
    return [s for s in out if s["length"] >= 0.08]


# ---------------------------------------------------------------- misc


def project_triangles(triangles, precision=5):
    """Union of vertical triangles projected to the XZ plane."""
    pieces = []
    for triangle in triangles:
        pts = np.unique(np.round(triangle[:, [0, 2]], precision), axis=0)
        if len(pts) < 3:
            continue
        poly = Polygon(pts)
        if poly.area > 1e-7 and poly.is_valid:
            pieces.append(poly)
    if not pieces:
        return Polygon()
    return clean_polygon(unary_union(pieces))


def prepare(geom):
    return prep(geom)
