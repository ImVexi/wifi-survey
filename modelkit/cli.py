"""Command line interface: ``python -m modelkit <command>``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import build, confirm, floors, regularize, walls
from .meshio import ImportError_, import_mesh
from .pipeline import run as run_pipeline
from .synthetic import build_house


def _emit(obj):
    print(json.dumps(obj, sort_keys=True, ensure_ascii=True))


def _stage_import(args):
    record = import_mesh(args.input, args.out)
    _emit({
        "stage": "import",
        "triangles": record["triangles"],
        "vertices": record["vertices"],
        "up_axis": record["up_axis_detected"],
        "scale": record["scale_applied"],
        "warnings": record["warnings"],
    })
    return 0


def _stage_floors(args):
    _emit({"stage": "floors", **floors.stage(args.work)})
    return 0


def _stage_walls(args):
    _emit({"stage": "walls", **walls.stage(args.work)})
    return 0


def _stage_regularize(args):
    _emit({"stage": "regularize", **regularize.stage(args.work)})
    return 0


def _stage_confirm(args):
    _emit({"stage": "confirm-list", **confirm.stage(args.work)})
    return 0


def _stage_build(args):
    out_dir = args.out or args.work
    _emit({"stage": "build", **build.stage(args.work, out_dir, name=args.name)})
    return 0


def _stage_run(args):
    summary = run_pipeline(args.input, args.out, name=args.name)
    _emit({"stage": "run", **summary})
    return 0


def _stage_synth(args):
    info = build_house(args.out, floors=args.floors, rooms=args.rooms, seed=args.seed)
    _emit({
        "stage": "synth",
        "path": Path(info["path"]).name,
        "floors": info["floors"],
        "rooms": info["rooms"],
        "seed": info["seed"],
        "wall_boxes": info["wall_boxes"],
    })
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="modelkit",
        description="Deterministic house-scan to floor-plan pipeline")
    sub = parser.add_subparsers(dest="command", required=True)

    synth = sub.add_parser("synth", help="generate a synthetic house GLB")
    synth.add_argument("--out", required=True)
    synth.add_argument("--floors", type=int, default=2)
    synth.add_argument("--rooms", type=int, default=10)
    synth.add_argument("--seed", type=int, default=7)
    synth.set_defaults(func=_stage_synth)

    imp = sub.add_parser("import", help="normalise a scan into a work directory")
    imp.add_argument("--input", required=True)
    imp.add_argument("--out", required=True, dest="out")
    imp.set_defaults(func=_stage_import)

    fl = sub.add_parser("floors", help="detect storeys and rooms")
    fl.add_argument("--work", required=True)
    fl.set_defaults(func=_stage_floors)

    wl = sub.add_parser("walls", help="extract wall bands")
    wl.add_argument("--work", required=True)
    wl.set_defaults(func=_stage_walls)

    rg = sub.add_parser("regularize", help="snap, merge and close wall segments")
    rg.add_argument("--work", required=True)
    rg.set_defaults(func=_stage_regularize)

    cf = sub.add_parser("confirm-list", help="write ambiguous-geometry list")
    cf.add_argument("--work", required=True)
    cf.set_defaults(func=_stage_confirm)

    bd = sub.add_parser("build", help="write GLB, plans, metadata")
    bd.add_argument("--work", required=True)
    bd.add_argument("--out", default=None)
    bd.add_argument("--name", default=None)
    bd.set_defaults(func=_stage_build)

    rn = sub.add_parser("run", help="full pipeline")
    rn.add_argument("--input", required=True)
    rn.add_argument("--out", required=True)
    rn.add_argument("--name", default=None)
    rn.set_defaults(func=_stage_run)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ImportError_ as exc:
        print(f"modelkit: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"modelkit: missing file: {exc}", file=sys.stderr)
        return 2
