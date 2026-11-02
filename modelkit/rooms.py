"""Room extraction: upward faces belonging to each storey's slab top."""

from __future__ import annotations

import numpy as np
from shapely.geometry import Polygon

from . import geom2d

MIN_ROOM_AREA = 1.5


def _rooms_from_mass(mass, min_area=MIN_ROOM_AREA):
    out = []
    for poly in geom2d.polys(mass):
        if poly.area < min_area:
            continue
        holes = [Polygon(ring) for ring in poly.interiors]
        holes = [h for h in holes if h.is_valid and h.area >= min_area]
        hole_area = sum(h.area for h in holes)
        if holes and hole_area > 0.30 * poly.area:
            # the polygon is the slab frame around the rooms, not a room
            out.extend(holes)
        else:
            out.append(poly)
    cleaned = []
    for poly in out:
        poly = poly.buffer(0)
        if poly.is_empty:
            continue
        poly = poly.simplify(0.01, preserve_topology=True)
        if poly.area >= min_area and poly.is_valid and not poly.is_empty:
            cleaned.append(poly)
    return cleaned


def detect_rooms(vertices, faces, slab_top, min_area=MIN_ROOM_AREA):
    """Detect room polygons on one storey.

    The primary signal is the union of upward facing triangles whose
    centroid lies within 5 cm of the slab top.  If a storey has no
    per-room floor faces at all, the caller falls back to wall loops.
    """
    tri, normals, areas, centroids = geom2d.tri_props(vertices, faces)
    if len(tri) == 0:
        return []
    mask = (normals[:, 1] > 0.9) & (np.abs(centroids[:, 1] - slab_top) <= 0.05)
    if not mask.any():
        return []
    candidates = tri[mask]

    # Drop long thin faces: wall caps sit at the same height as the slab
    # above, but their projected triangle is only a few centimetres wide.
    xy = candidates[:, :, [0, 2]]
    area2d = 0.5 * np.abs(
        (xy[:, 1, 0] - xy[:, 0, 0]) * (xy[:, 2, 1] - xy[:, 0, 1]) -
        (xy[:, 2, 0] - xy[:, 0, 0]) * (xy[:, 1, 1] - xy[:, 0, 1]))
    edges = np.stack([
        np.linalg.norm(xy[:, 1] - xy[:, 0], axis=1),
        np.linalg.norm(xy[:, 2] - xy[:, 1], axis=1),
        np.linalg.norm(xy[:, 0] - xy[:, 2], axis=1),
    ], axis=1)
    longest = np.maximum(edges.max(axis=1), 1e-9)
    width = 2.0 * area2d / longest
    keep = (width >= 0.20) & (area2d >= 0.05)
    if not keep.any():
        return []
    mass = geom2d.project_triangles(candidates[keep])
    rooms = _rooms_from_mass(mass, min_area)
    rooms.sort(key=lambda p: (round(p.centroid.y, 2), round(p.centroid.x, 2)))
    return rooms


def rooms_from_wall_loops(vertices, faces, slab_top, ceiling, min_area=MIN_ROOM_AREA):
    """Fallback: rooms are the holes enclosed by the wall mass."""
    from shapely.geometry import Polygon

    from .walls import extract

    _bands, mass = extract(vertices, faces, slab_top, ceiling)
    if mass.is_empty:
        return []
    rooms = []
    for poly in geom2d.polys(mass):
        for ring in poly.interiors:
            room = Polygon(ring)
            if room.is_valid and room.area >= min_area:
                rooms.append(room)
    rooms.sort(key=lambda p: (round(p.centroid.y, 2), round(p.centroid.x, 2)))
    return rooms
