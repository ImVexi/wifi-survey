"""Bridge between uploaded scan files and the deterministic modelkit pipeline."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

ALLOWED_SUFFIXES = {".glb", ".gltf", ".obj"}
MAX_UPLOAD_BYTES = 150 * 1024 * 1024


class ModelImportError(RuntimeError):
    """The uploaded file could not be turned into a house model."""


def safe_suffix(filename: str) -> str:
    suffix = Path(filename or "").suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise ModelImportError(
            "unsupported file type %r (expected .glb, .gltf or .obj)" % (suffix or filename)
        )
    return suffix


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _clean_name(value: str) -> str:
    value = "".join(ch for ch in value.strip() if ch.isprintable())
    return value[:128] or "Imported model"


def import_house(
    source: str | Path, models_root: str | Path, *, name: str | None = None,
) -> dict[str, Any]:
    """Run modelkit on ``source`` inside a fresh directory and return the result.

    The caller is responsible for authorising the upload; this function only
    touches ``models_root``. It is synchronous and should run in a worker thread.
    """
    source = Path(source)
    models_root = Path(models_root)
    suffix = safe_suffix(source.name)
    model_id = uuid.uuid4().hex[:12]
    target = models_root / model_id
    target.mkdir(parents=True, exist_ok=False)
    scan_path = target / ("source" + suffix)
    shutil.copyfile(source, scan_path)

    try:
        from modelkit.pipeline import run as run_pipeline
    except Exception as exc:  # pragma: no cover - depends on install layout
        raise ModelImportError(
            "modelkit is not installed; install the modelkit package to import scans"
        ) from exc

    out_dir = target / "build"
    try:
        run_pipeline(scan_path, out_dir, name=_clean_name(name or source.stem))
    except Exception as exc:
        shutil.rmtree(target, ignore_errors=True)
        raise ModelImportError("modelkit could not parse this scan: %s" % exc) from exc

    metadata_path = out_dir / "metadata.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        shutil.rmtree(target, ignore_errors=True)
        raise ModelImportError("modelkit produced no readable metadata") from exc

    floors = metadata.get("floors")
    if not isinstance(floors, list) or not floors:
        shutil.rmtree(target, ignore_errors=True)
        raise ModelImportError("no floors were detected in this scan")

    # Keep a stable, relative location for the active model's viewer assets.
    model_file = out_dir / "model.glb"
    if not model_file.is_file():
        shutil.rmtree(target, ignore_errors=True)
        raise ModelImportError("modelkit produced no model.glb")

    metadata.setdefault("model", {})
    metadata["model"]["source_sha256"] = sha256_file(scan_path)
    metadata["model"]["name"] = _clean_name(name or metadata["model"].get("name") or source.stem)
    metadata_path.write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    return {
        "id": model_id,
        "directory": str(target),
        "build_dir": str(out_dir),
        "metadata": metadata,
        "floors": floors,
        "sha256": metadata["model"]["source_sha256"],
        "name": metadata["model"]["name"],
    }


def remove_model_dir(directory: str | Path) -> None:
    path = Path(directory)
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)


def active_asset(model_row, relative: str) -> Path | None:
    """Resolve a viewer asset inside one model's build directory."""
    base = Path(model_row["directory"]) / "build"
    target = (base / relative).resolve()
    try:
        target.relative_to(base.resolve())
    except ValueError:
        return None
    return target if target.is_file() else None


def model_public(row) -> dict[str, Any]:
    """Serialise the active model row without leaking absolute paths."""
    metadata: dict[str, Any] = {}
    try:
        metadata = json.loads(row["metadata"] or "{}")
    except ValueError:
        metadata = {}
    return {
        "id": row["id"],
        "name": row["name"],
        "floors_count": row["floors_count"],
        "rooms_count": row["rooms_count"],
        "created_at": row["created_at"],
        "source_sha256": row["source_sha256"],
        "units": (metadata.get("model") or {}).get("units", "m"),
        "up_axis": (metadata.get("model") or {}).get("up_axis", "Y"),
        "warnings": metadata.get("warnings") or [],
    }


def cleanup_partial(models_root: str | Path) -> None:
    """Remove stale temp dirs left by a killed import (best effort)."""
    root = Path(models_root)
    if not root.is_dir():
        return
    for entry in root.iterdir():
        if entry.is_dir() and entry.name.startswith(".tmp-"):
            shutil.rmtree(entry, ignore_errors=True)
    _ = os  # keep import explicit for callers monkeypatching os in tests
