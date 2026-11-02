"""Mesh import and normalisation (up axis, units, origin)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import trimesh

from . import geom2d


class ImportError_(Exception):
    """Raised when an input mesh cannot be used."""


def _load_mesh(path):
    try:
        loaded = trimesh.load(str(path), force="mesh")
    except Exception as exc:  # pragma: no cover - trimesh raises many types
        raise ImportError_(f"could not read mesh: {exc}") from exc
    if isinstance(loaded, trimesh.Scene):
        loaded = loaded.dump(concatenate=True)
    vertices = np.asarray(loaded.vertices, dtype=np.float64)
    faces = np.asarray(loaded.faces, dtype=np.int64)
    if vertices.size == 0 or faces.size == 0:
        raise ImportError_("mesh contains no triangles")
    return vertices, faces


def _up_axis(tri, normals, areas):
    """Axis with the most horizontal (slab-like) face area."""
    scores = []
    for axis in range(3):
        mask = np.abs(normals[:, axis]) > 0.9
        scores.append(float(areas[mask].sum()))
    return int(np.argmax(scores)), scores


def _to_y_up(vertices, axis):
    if axis == 1:
        return vertices.copy()
    if axis == 2:  # Z-up -> Y-up: (x, y, z) -> (x, z, -y)
        return np.column_stack([vertices[:, 0], vertices[:, 2], -vertices[:, 1]])
    # X-up -> Y-up
    return np.column_stack([vertices[:, 1], vertices[:, 0], vertices[:, 2]])


def normalize(vertices, faces):
    """Return Y-up, metre scaled, XZ centred vertices with the lowest slab at y=0."""
    tri, normals, areas, centroids = geom2d.tri_props(vertices, faces)
    if len(tri) == 0:
        raise ImportError_("mesh has no usable triangles")

    axis, _scores = _up_axis(tri, normals, areas)
    vertices = _to_y_up(vertices, axis)

    extent = float(np.max(np.ptp(vertices, axis=0)))
    scale = 1.0
    if extent > 200.0:          # millimetres
        scale = 0.001
    elif extent > 60.0:         # centimetres
        scale = 0.01
    vertices = vertices * scale

    # centre XZ on the bounding box centre
    lo = vertices.min(axis=0)
    hi = vertices.max(axis=0)
    vertices[:, 0] -= (lo[0] + hi[0]) / 2.0
    vertices[:, 2] -= (lo[2] + hi[2]) / 2.0

    # y = 0 at the lowest significant upward-facing plane (lowest slab top)
    tri, normals, areas, centroids = geom2d.tri_props(vertices, faces)
    peaks = geom2d.plane_peaks(tri, normals, areas, centroids,
                               axis=1, sign=1, rel=0.10)
    y_zero = peaks[0]["top"] if peaks else float(vertices[:, 1].min())
    vertices[:, 1] -= y_zero

    meta = {
        "up_axis_detected": "XYZ"[axis],
        "scale_applied": scale,
        "y_offset": geom2d.q(y_zero, 6),
    }
    return vertices, faces, meta


def import_mesh(input_path, work_dir, name=None):
    """Stage 1: load, normalise and persist the mesh for later stages."""
    from .io_utils import sha256_file, write_json, write_mesh_npz

    input_path = Path(input_path)
    if not input_path.is_file():
        raise ImportError_(f"input not found: {input_path}")

    vertices, faces = _load_mesh(input_path)
    vertices, faces, meta = normalize(vertices, faces)

    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    write_mesh_npz(work_dir / "mesh.npz", vertices, faces)

    lo = vertices.min(axis=0)
    hi = vertices.max(axis=0)
    record = {
        "schema": 1,
        "source_name": input_path.name,
        "source_sha256": sha256_file(input_path),
        "units": "m",
        "up_axis": "Y",
        "up_axis_detected": meta["up_axis_detected"],
        "scale_applied": meta["scale_applied"],
        "y_offset": meta["y_offset"],
        "vertices": int(len(vertices)),
        "triangles": int(len(faces)),
        "bbox": {
            "min": [geom2d.q(v) for v in lo],
            "max": [geom2d.q(v) for v in hi],
        },
        "mesh_file": "mesh.npz",
        "warnings": [],
    }
    if meta["up_axis_detected"] != "Y":
        record["warnings"].append(
            f"input up axis detected as {meta['up_axis_detected']}; converted to Y-up")
    if meta["scale_applied"] != 1.0:
        record["warnings"].append(
            f"input scaled by {meta['scale_applied']} to metres")
    write_json(work_dir / "import.json", record)

    return record
