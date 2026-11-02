"""Deterministic JSON/npz IO helpers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(obj, indent=2, ensure_ascii=True, sort_keys=False)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text + "\n")
    return path


def read_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def write_mesh_npz(path, vertices, faces):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(path, V=np.asarray(vertices, dtype=np.float64),
                        F=np.asarray(faces, dtype=np.int64))
    return path


def read_mesh_npz(path):
    with np.load(path) as data:
        return data["V"], data["F"].astype(np.int64)


def rel(path, base):
    """Path relative to base as posix string (never absolute)."""
    try:
        return Path(path).resolve().relative_to(Path(base).resolve()).as_posix()
    except ValueError:
        return Path(path).name
