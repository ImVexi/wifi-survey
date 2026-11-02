"""Build stage: GLB, floor plans, metadata and summary."""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import trimesh
from shapely.geometry import Polygon
from shapely.ops import unary_union

from . import geom2d, plans
from .io_utils import read_json, write_json
from .metadata import assemble

SLAB_THICKNESS = 0.15
WALL_COLOUR = (0.82, 0.82, 0.82)
SLAB_COLOUR = (0.62, 0.64, 0.68)


def _material(mesh, colour, name):
    mesh.visual = trimesh.visual.TextureVisuals(
        material=trimesh.visual.material.PBRMaterial(
            name=name,
            baseColorFactor=[int(round(c * 255)) for c in colour] + [255],
            metallicFactor=0.0,
            roughnessFactor=0.9,
            doubleSided=True,
        ))
    return mesh


def _prism(polygon, y0, y1):
    height = float(y1 - y0)
    if height <= 1e-4 or polygon.is_empty or polygon.area <= 1e-6:
        return None
    try:
        mesh = trimesh.creation.extrude_polygon(polygon, height=height)
    except Exception:
        return None
    transform = np.array([
        [1, 0, 0, 0],
        [0, 0, 1, y0],
        [0, 1, 0, 0],
        [0, 0, 0, 1],
    ], dtype=float)
    mesh.apply_transform(transform)
    if mesh.volume < 0:
        mesh.invert()
    return mesh


def _wall_mesh(seg, y0, y1):
    dx = seg["x2"] - seg["x1"]
    dz = seg["z2"] - seg["z1"]
    length = math.hypot(dx, dz)
    if length <= 1e-6:
        return None
    theta = math.atan2(dz, dx)
    thickness = max(0.06, float(seg["thickness"]))
    mesh = trimesh.creation.box(extents=(length + thickness,
                                         y1 - y0, thickness))
    rotation = trimesh.transformations.rotation_matrix(-theta, [0, 1, 0])
    mesh.apply_transform(rotation)
    mesh.apply_translation(((seg["x1"] + seg["x2"]) / 2.0,
                            (y0 + y1) / 2.0,
                            (seg["z1"] + seg["z2"]) / 2.0))
    return mesh


def _outline_polygon(floor):
    if floor.get("outline"):
        polygon = Polygon(floor["outline"])
        if polygon.is_valid and polygon.area > 0:
            return polygon
    rooms = [Polygon(r["polygon"]) for r in floor["rooms"]]
    if not rooms:
        return Polygon()
    union = unary_union(rooms)
    if not union.is_valid:
        union = union.buffer(0)
    parts = geom2d.polys(union)
    if not parts:
        return Polygon()
    return max(parts, key=lambda p: p.area)


def build_scene(floors_record, regularized_record):
    segments_by_floor = {f["index"]: f["segments"]
                         for f in regularized_record["floors"]}
    scene = trimesh.Scene()
    wall_number = 0
    counts = {"slabs": 0, "walls": 0}

    for floor in floors_record["floors"]:
        node = floor["key"]
        scene.graph.update(frame_to=node, frame_from="world", matrix=np.eye(4))
        slab_top = float(floor["slab_top_y"])
        ceiling = float(floor["ceiling_y"])

        outline = _outline_polygon(floor)
        slab = _prism(outline, slab_top - SLAB_THICKNESS, slab_top)
        if slab is not None:
            _material(slab, SLAB_COLOUR, f"slab_{floor['index']}")
            scene.add_geometry(slab, node_name=f"{node}__slab",
                               geom_name=f"{node}__slab",
                               parent_node_name=node)
            counts["slabs"] += 1

        for seg in segments_by_floor.get(floor["index"], []):
            mesh = _wall_mesh(seg, slab_top, ceiling)
            if mesh is None:
                continue
            wall_number += 1
            name = f"wall_{wall_number:03d}"
            _material(mesh, WALL_COLOUR, name)
            scene.add_geometry(mesh, node_name=name, geom_name=name,
                               parent_node_name=node)
            counts["walls"] += 1

    return scene, counts


def stage(work_dir, out_dir, name=None):
    """CLI stage: build --work DIR [--out DIR]."""
    work_dir = Path(work_dir)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "plans").mkdir(parents=True, exist_ok=True)

    import_record = read_json(work_dir / "import.json")
    floors_record = read_json(work_dir / "floors.json")
    regularized = read_json(work_dir / "regularized.json")
    confirm_record = read_json(work_dir / "confirm-list.json")

    scene, counts = build_scene(floors_record, regularized)
    glb_path = out_dir / "model.glb"
    scene.export(str(glb_path), file_type="glb")

    metadata = assemble(import_record, floors_record, regularized,
                        confirm_record, name=name)
    write_json(out_dir / "metadata.json", metadata)
    write_json(out_dir / "confirm-list.json", confirm_record)

    outputs = ["model.glb", "metadata.json", "confirm-list.json", "summary.json"]
    for floor in metadata["floors"]:
        svg = f"plans/floor_{floor['index']}.svg"
        png = f"plans/floor_{floor['index']}.png"
        plans.write_svg(out_dir / svg, floor)
        plans.write_png(out_dir / png, floor)
        outputs.extend([svg, png])

    by_kind = {}
    for item in metadata["confirmations"]:
        by_kind[item["kind"]] = by_kind.get(item["kind"], 0) + 1
    summary = {
        "schema": 1,
        "name": metadata["model"]["name"],
        "source_sha256": metadata["model"]["source_sha256"],
        "floors": len(metadata["floors"]),
        "rooms": sum(len(f["rooms"]) for f in metadata["floors"]),
        "walls": sum(len(f["walls"]) for f in metadata["floors"]),
        "confirmations": {
            "total": len(metadata["confirmations"]),
            "by_kind": dict(sorted(by_kind.items())),
        },
        "warnings": metadata["warnings"],
        "outputs": outputs,
    }
    write_json(out_dir / "summary.json", summary)
    return summary
