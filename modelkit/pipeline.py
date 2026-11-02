"""Pipeline orchestration used by ``python -m modelkit run``."""

from __future__ import annotations

from pathlib import Path

from . import build, confirm, floors, regularize, walls
from .meshio import import_mesh


def run(input_path, out_dir, name=None):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    work_dir = out_dir / "work"

    import_mesh(input_path, work_dir)
    floors.stage(work_dir)
    walls.stage(work_dir)
    regularize.stage(work_dir)
    confirm.stage(work_dir)
    return build.stage(work_dir, out_dir, name=name)
