"""Synthetic two-storey house generator.

``python -m modelkit.synthetic --out sample_house.glb --floors 2 --rooms 10 --seed 7``

The generator is deliberately independent of any real scan.  It produces

  * one structural slab + one thin floor overlay per room,
  * exterior wall boxes (0.20 m) and interior wall boxes (0.10 m),
  * a deterministic rectilinear room partition per storey.

Room counts are distributed over the requested storeys; the default
(2 floors / 10 rooms) gives five rooms per storey.  Layouts only use
generic names -- the mesh has no room names at all, only nodes such as
``floor_0__room_03_floor`` and ``floor_0__wall_002``.
"""

from __future__ import annotations

import argparse
import random
from pathlib import Path

import numpy as np
import trimesh

STOREY_HEIGHT = 2.7
SLAB_THICKNESS = 0.15
OVERLAY_THICKNESS = 0.02
EXTERIOR_THICKNESS = 0.20
INTERIOR_THICKNESS = 0.10

FOOTPRINT = (-6.0, -4.0, 6.0, 4.0)  # x0, z0, x1, z1 (metres)
MIN_ROOM_SIDE = 2.0


def _box(x0, z0, x1, z1, y0, y1):
    mesh = trimesh.creation.box(extents=(x1 - x0, y1 - y0, z1 - z0))
    mesh.apply_translation(((x0 + x1) / 2.0, (y0 + y1) / 2.0, (z0 + z1) / 2.0))
    return mesh


def _partition(rng, rect, count, runs):
    """Recursively split a rectangle into ``count`` rooms and wall runs."""
    x0, z0, x1, z1 = rect
    if count <= 1:
        return [rect]

    width = x1 - x0
    depth = z1 - z0
    axis = "x" if width >= depth else "z"
    if axis == "x" and width < 2 * MIN_ROOM_SIDE and depth >= 2 * MIN_ROOM_SIDE:
        axis = "z"
    elif axis == "z" and depth < 2 * MIN_ROOM_SIDE and width >= 2 * MIN_ROOM_SIDE:
        axis = "x"

    if axis == "x":
        share = count * width / max(width + depth, 1e-9)
        n_left = max(1, min(count - 1, int(round(share))))
        lo, hi = x0 + MIN_ROOM_SIDE, x1 - MIN_ROOM_SIDE
        if hi <= lo:
            lo = hi = (x0 + x1) / 2.0
        cut = x0 + width * (n_left / count + rng.uniform(-0.07, 0.07))
        cut = min(max(cut, lo), hi)
        cut = round(cut * 20.0) / 20.0
        cut = min(max(cut, lo), hi)
        runs.append((cut, z0, cut, z1))
        return (_partition(rng, (x0, z0, cut, z1), n_left, runs) +
                _partition(rng, (cut, z0, x1, z1), count - n_left, runs))

    share = count * depth / max(width + depth, 1e-9)
    n_front = max(1, min(count - 1, int(round(share))))
    lo, hi = z0 + MIN_ROOM_SIDE, z1 - MIN_ROOM_SIDE
    if hi <= lo:
        lo = hi = (z0 + z1) / 2.0
    cut = z0 + depth * (n_front / count + rng.uniform(-0.07, 0.07))
    cut = min(max(cut, lo), hi)
    cut = round(cut * 20.0) / 20.0
    cut = min(max(cut, lo), hi)
    runs.append((x0, cut, x1, cut))
    return (_partition(rng, (x0, z0, x1, cut), n_front, runs) +
            _partition(rng, (x0, cut, x1, z1), count - n_front, runs))


def _room_slab(rect, slab_top_y):
    """Structural slab + thin overlay for one room, inset to the wall faces."""
    x0, z0, x1, z1 = rect
    fx0, fz0, fx1, fz1 = FOOTPRINT
    ix0 = 0.10 if abs(x0 - fx0) < 1e-6 else 0.06
    ix1 = 0.10 if abs(x1 - fx1) < 1e-6 else 0.06
    iz0 = 0.10 if abs(z0 - fz0) < 1e-6 else 0.06
    iz1 = 0.10 if abs(z1 - fz1) < 1e-6 else 0.06
    a0, b0 = x0 + ix0, z0 + iz0
    a1, b1 = x1 - ix1, z1 - iz1
    structural = _box(a0, b0, a1, b1,
                      slab_top_y - SLAB_THICKNESS,
                      slab_top_y - OVERLAY_THICKNESS)
    overlay = _box(a0, b0, a1, b1,
                   slab_top_y - OVERLAY_THICKNESS, slab_top_y)
    return structural, overlay


def _wall_box(run, thickness, y0, y1):
    x1, z1, x2, z2 = run
    if abs(z2 - z1) < 1e-9:  # wall runs along x
        return _box(min(x1, x2), z1 - thickness / 2.0,
                    max(x1, x2), z1 + thickness / 2.0, y0, y1)
    return _box(x1 - thickness / 2.0, min(z1, z2),
                x1 + thickness / 2.0, max(z1, z2), y0, y1)


def _stub_run(runs):
    """One short (0.30 m) return so the confirmation list has a real stub."""
    if not runs:
        return None
    run = max(runs, key=lambda r: abs(r[2] - r[0]) + abs(r[3] - r[1]))
    mx = (run[0] + run[2]) / 2.0
    mz = (run[1] + run[3]) / 2.0
    if abs(run[3] - run[1]) < 1e-9:      # horizontal wall -> stub runs in z
        if mz + 0.30 <= FOOTPRINT[3]:
            return (mx, mz, mx, mz + 0.30)
        return (mx, mz, mx, mz - 0.30)
    if mx + 0.30 <= FOOTPRINT[2]:
        return (mx, mz, mx + 0.30, mz)
    return (mx, mz, mx - 0.30, mz)


def build_house(out_path, floors=2, rooms=10, seed=7):
    """Write the synthetic house GLB and return a summary dict."""
    floors = max(1, int(floors))
    rooms = max(floors, int(rooms))
    fx0, fz0, fx1, fz1 = FOOTPRINT
    per_floor = [rooms // floors] * floors
    for i in range(rooms % floors):
        per_floor[i] += 1

    scene = trimesh.Scene()
    wall_total = 0
    floor_info = []

    for floor in range(floors):
        rng = random.Random(seed * 1009 + floor)
        runs = []
        room_rects = _partition(rng, (fx0, fz0, fx1, fz1), per_floor[floor], runs)
        room_rects = sorted(room_rects, key=lambda r: (round(r[1], 3), round(r[0], 3)))
        if floor == 0:
            stub = _stub_run(runs)
            if stub is not None:
                runs = runs + [stub]

        slab_top = floor * STOREY_HEIGHT + SLAB_THICKNESS
        wall_top = slab_top + STOREY_HEIGHT
        node = f"floor_{floor}"
        scene.graph.update(frame_to=node, frame_from="world", matrix=np.eye(4))

        for i, rect in enumerate(room_rects, start=1):
            structural, overlay = _room_slab(rect, slab_top)
            scene.add_geometry(structural,
                               node_name=f"{node}__room_{i:02d}_slab",
                               geom_name=f"{node}__room_{i:02d}_slab",
                               parent_node_name=node)
            scene.add_geometry(overlay,
                               node_name=f"{node}__room_{i:02d}_overlay",
                               geom_name=f"{node}__room_{i:02d}_overlay",
                               parent_node_name=node)

        # exterior walls
        exterior = [
            (fx0, fz0, fx1, fz0),
            (fx0, fz1, fx1, fz1),
            (fx0, fz0, fx0, fz1),
            (fx1, fz0, fx1, fz1),
        ]
        for index, run in enumerate(exterior + list(runs)):
            thickness = EXTERIOR_THICKNESS if index < len(exterior) else INTERIOR_THICKNESS
            mesh = _wall_box(run, thickness, slab_top, wall_top)
            wall_total += 1
            scene.add_geometry(mesh,
                               node_name=f"{node}__wall_{index + 1:03d}",
                               geom_name=f"{node}__wall_{index + 1:03d}",
                               parent_node_name=node)

        floor_info.append({
            "floor": floor,
            "rooms": len(room_rects),
            "walls": len(exterior) + len(runs),
            "slab_top_y": round(slab_top, 3),
            "wall_top_y": round(wall_top, 3),
        })

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    scene.export(str(out_path), file_type="glb")
    return {
        "path": str(out_path),
        "floors": floors,
        "rooms": rooms,
        "seed": seed,
        "wall_boxes": wall_total,
        "per_floor": floor_info,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(prog="modelkit.synthetic",
                                     description="Generate a synthetic house GLB")
    parser.add_argument("--out", required=True, help="output .glb path")
    parser.add_argument("--floors", type=int, default=2)
    parser.add_argument("--rooms", type=int, default=10)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args(argv)
    info = build_house(args.out, floors=args.floors, rooms=args.rooms, seed=args.seed)
    print(f"wrote {info['path']} ({info['floors']} floors, {info['rooms']} rooms)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
