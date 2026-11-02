"""Metadata assembly (the contract consumed by the survey server)."""

from __future__ import annotations

import re

from . import PIPELINE_VERSION, SCHEMA, geom2d


def _clean_name(name):
    name = (name or "model").strip()
    name = name.replace("\\", "/").split("/")[-1]
    name = re.sub(r"\.(glb|gltf|obj)$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"[^A-Za-z0-9 _.-]+", "-", name).strip(" .-_")
    return name or "model"


def _dedupe(values):
    seen = set()
    out = []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def assemble(import_record, floors_record, regularized_record,
             confirm_record, name=None):
    segments_by_floor = {f["index"]: f["segments"]
                         for f in regularized_record["floors"]}

    floors = []
    for floor in floors_record["floors"]:
        height = geom2d.q(floor["ceiling_y"] - floor["slab_top_y"])
        walls = []
        for seg in segments_by_floor.get(floor["index"], []):
            walls.append({
                "x1": seg["x1"],
                "z1": seg["z1"],
                "x2": seg["x2"],
                "z2": seg["z2"],
                "thickness": seg["thickness"],
                "height": height,
                "type": "wall",
            })
        floors.append({
            "index": int(floor["index"]),
            "key": floor["key"],
            "name": floor["name"],
            "slab_top_y": geom2d.q(floor["slab_top_y"]),
            "ceiling_y": geom2d.q(floor["ceiling_y"]),
            "rooms": [
                {
                    "name": room["name"],
                    "polygon": room["polygon"],
                    "area_m2": room["area_m2"],
                    "centroid": room["centroid"],
                }
                for room in floor["rooms"]
            ],
            "walls": walls,
            "outline": floor["outline"],
        })

    warnings = _dedupe(
        list(import_record.get("warnings", [])) +
        list(floors_record.get("warnings", [])) +
        list(regularized_record.get("warnings", [])))

    return {
        "schema": SCHEMA,
        "model": {
            "name": _clean_name(name or import_record.get("source_name", "model")),
            "source_sha256": import_record.get("source_sha256", ""),
            "units": "m",
            "up_axis": "Y",
        },
        "floors": floors,
        "confirmations": list(confirm_record.get("confirmations", [])),
        "warnings": warnings,
        "generated_by": "modelkit",
        "pipeline_version": PIPELINE_VERSION,
    }
