"""Storey detection from horizontal face area histograms."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from shapely.geometry import Polygon

from . import geom2d
from .io_utils import read_mesh_npz, write_json
from .rooms import detect_rooms, rooms_from_wall_loops

DEFAULT_STOREY = 2.7
MAX_STOREY = 5.0
MIN_STOREY = 1.8


def _wall_top_y(tri, normals, slab_top):
    """Highest vertical geometry that belongs to this storey."""
    if len(tri) == 0:
        return None
    y_min = tri[:, :, 1].min(axis=1)
    y_max = tri[:, :, 1].max(axis=1)
    mask = (np.abs(normals[:, 1]) < 0.3) & (y_max > slab_top + 0.5) & \
           (y_min < slab_top + MAX_STOREY)
    if not mask.any():
        return None
    top = float(y_max[mask].max())
    if top - slab_top < MIN_STOREY:
        return None
    return top


def detect(vertices, faces):
    """Return (floors, warnings) with levels and room polygons."""
    tri, normals, areas, centroids = geom2d.tri_props(vertices, faces)
    peaks = geom2d.plane_peaks(tri, normals, areas, centroids,
                               axis=1, sign=1, rel=0.15)
    warnings = []
    if not peaks:
        return [], ["no slab-like horizontal faces found; floors not detected"]

    floors = []
    for index, peak in enumerate(peaks):
        slab_top = float(peak["top"])
        wall_top = _wall_top_y(tri, normals, slab_top)
        if index + 1 < len(peaks):
            ceiling = float(peaks[index + 1]["top"])
        else:
            ceiling = wall_top if wall_top is not None else slab_top + DEFAULT_STOREY
        if not (MIN_STOREY <= ceiling - slab_top <= MAX_STOREY):
            fallback = slab_top + DEFAULT_STOREY
            warnings.append(
                f"floor {index}: implausible ceiling {ceiling:.2f} m; using {fallback:.2f} m")
            ceiling = fallback

        rooms = detect_rooms(vertices, faces, slab_top)
        if not rooms:
            rooms = rooms_from_wall_loops(vertices, faces, slab_top, ceiling)
            if rooms:
                warnings.append(
                    f"floor {index}: no floor overlays; rooms taken from wall loops")

        floors.append({
            "index": index,
            "slab_top_y": slab_top,
            "ceiling_y": ceiling,
            "wall_top_y": wall_top if wall_top is not None else ceiling,
            "peak_area": float(peak["area"]),
            "rooms": rooms,
        })
    return floors, warnings


def to_json(floors, warnings):
    """Serialise detected floors, naming rooms globally in floor order."""
    counter = 0
    out = []
    for floor in floors:
        rooms = []
        for poly in floor["rooms"]:
            counter += 1
            centroid = poly.centroid
            rooms.append({
                "name": f"Room {counter:02d}",
                "polygon": geom2d.ring_of(poly),
                "area_m2": geom2d.q(poly.area, 3),
                "centroid": [geom2d.q(centroid.x), geom2d.q(centroid.y)],
            })
        polygons = [Polygon(r["polygon"]) for r in rooms]
        out.append({
            "index": int(floor["index"]),
            "key": f"floor_{floor['index']}",
            "name": f"Floor {floor['index'] + 1}",
            "slab_top_y": geom2d.q(floor["slab_top_y"]),
            "ceiling_y": geom2d.q(floor["ceiling_y"]),
            "rooms": rooms,
            "outline": geom2d.outline_of(polygons),
            "peak_area": geom2d.q(floor["peak_area"], 3),
        })
    return {
        "schema": 1,
        "floors": out,
        "warnings": list(warnings),
    }


def stage(work_dir):
    """CLI stage: floors --work DIR."""
    work_dir = Path(work_dir)
    vertices, faces = read_mesh_npz(work_dir / "mesh.npz")
    floors, warnings = detect(vertices, faces)
    record = to_json(floors, warnings)
    write_json(work_dir / "floors.json", record)
    summary = {
        "floors": len(record["floors"]),
        "rooms": sum(len(f["rooms"]) for f in record["floors"]),
        "levels": [f["slab_top_y"] for f in record["floors"]],
        "warnings": record["warnings"],
    }
    return summary
