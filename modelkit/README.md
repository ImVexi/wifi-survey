# modelkit

Deterministic floor-plan reconstruction for house scans.

`modelkit` takes a GLB/glTF/OBJ mesh of a building and produces storey levels,
room polygons, regularized wall centre lines, a confirmation list for
ambiguous geometry, a rebuilt GLB, SVG/PNG floor plans and a `metadata.json`
record. No machine learning, no network calls, no timestamps: the same input
bytes always produce byte-identical `metadata.json`, `model.glb`, plans and
`confirm-list.json`.

## Install / requirements

Python 3.10+, `numpy`, `shapely`, `trimesh`, `Pillow`. Run from the repository
root so that `modelkit` is importable:

```sh
python -m modelkit --help
```

## Commands

### Synthetic fixture

```sh
python -m modelkit synth --out tests/fixtures/sample_house.glb \
    --floors 2 --rooms 10 --seed 7
```

Generates a rectilinear two-storey house: one structural slab plus a thin
floor overlay per room, 0.20 m exterior walls, 0.10 m interior walls and one
short return wall so the confirmation list has a real stub. Node names are
generic (`floor_0`, `floor_0__room_03_overlay`, `floor_0__wall_002`).
The same command is available as `python -m modelkit.synthetic`.

### Full pipeline

```sh
python -m modelkit run --input tests/fixtures/sample_house.glb \
    --out out --name sample_house
```

Writes:

```
out/model.glb
out/metadata.json
out/confirm-list.json
out/summary.json
out/plans/floor_0.svg
out/plans/floor_0.png
out/plans/floor_1.svg
out/plans/floor_1.png
out/work/{import,floors,walls,regularized,confirm-list}.json
```

### Chained stages

Each stage persists intermediate JSON and can be run on its own:

```sh
python -m modelkit import     --input scan.glb --out work
python -m modelkit floors     --work work
python -m modelkit walls      --work work
python -m modelkit regularize --work work
python -m modelkit confirm-list --work work
python -m modelkit build      --work work --out out --name my_house
```

`import` writes `work/import.json` and `work/mesh.npz`; later stages read the
mesh NPZ, so no re-import is needed. `build --out` defaults to the work
directory when omitted.

## Pipeline

1. **import** – load the scene, concatenate geometry, drop textures, detect
   the up axis from horizontal face area (convert Z-up/X-up to Y-up), scale
   millimetre/centimetre models to metres, centre XZ and put the lowest slab
   top at `y = 0`.
2. **floors** – area-weighted 2 cm histogram of upward-facing face centroids.
   Peaks above 1 m² are merged within 0.15 m; peaks below 15 % of the largest
   peak are discarded (wall caps, sills). `slab_top_y` is the area-weighted
   upper edge of each peak, `ceiling_y` is the next slab top (or the wall top
   for the topmost storey). Rooms are the union of upward faces within 5 cm
   of the slab top, with long thin faces (wall caps) removed; if a storey has
   no floor overlays, rooms fall back to the holes enclosed by the wall bands.
3. **walls** – vertical faces spanning the storey are projected to XZ and
   clustered into wall faces. Opposing faces are paired with their nearest
   partner, giving centre-line segments and thickness. This is more robust
   than unioning projected triangles, which collapse to lines for perfectly
   vertical walls.
4. **regularize** – snap angles to 0/90° within 5° (otherwise the nearest
   15° multiple), merge collinear segments (offset < 0.05 m, gap < 0.30 m),
   square short diagonals that sit between two perpendicular walls, extend
   loose endpoints into perpendicular walls (< 0.30 m, never shortening a
   wall) and drop degenerate segments.
5. **confirm-list** – stubs (< 0.35 m), jogs (0.05–0.35 m offset), short
   non-orthogonal chamfers and wall-end gaps (0.03–0.30 m). Nothing ambiguous
   is deleted automatically.
6. **build** – rebuild the model from floor slabs and wall boxes
   (`floor_0` → `floor_0__slab` + `wall_001` …), draw SVG/PNG plans and write
   `metadata.json` plus `summary.json`.

## Metadata contract

```json
{
  "schema": 1,
  "model": {"name": "sample_house", "source_sha256": "...", "units": "m", "up_axis": "Y"},
  "floors": [
    {
      "index": 0, "key": "floor_0", "name": "Floor 1",
      "slab_top_y": 0.0, "ceiling_y": 2.7,
      "rooms": [{"name": "Room 01", "polygon": [[1.8, -3.9], ...], "area_m2": 15.3, "centroid": [3.85, -2.03]}],
      "walls": [{"x1": -6.0, "z1": -4.0, "x2": 6.0, "z2": -4.0,
                 "thickness": 0.2, "height": 2.7, "type": "wall"}],
      "outline": [[-6.0, -4.0], ...]
    }
  ],
  "confirmations": [
    {"id": "stub-1", "kind": "stub", "floor": "floor_0", "severity": "warn",
     "message": "...", "segment": [[1.75, 0.0], [2.05, 0.0]], "suggestion": "..."}
  ],
  "warnings": [],
  "generated_by": "modelkit",
  "pipeline_version": 1
}
```

Room polygons are metres in the floor XZ plane, exterior ring only and closed
(first point equals last). Floor indexes start at 0 (lowest storey). All
coordinates and areas are rounded to fixed precision; there are no timestamps
anywhere.

## Determinism

* No `random`, wall-clock or environment data is used by the pipeline.
* Every list is sorted with explicit keys before it is written.
* `metadata.json`, `confirm-list.json`, `summary.json`, `model.glb` and the
  plans are byte-identical between runs on the same input (tested).
* The synthetic generator is seeded (`--seed`); the same seed produces a
  byte-identical GLB.

## Tests

```sh
python -m pytest modelkit/tests -q
```

The suite covers synthetic determinism, floor detection, wall extraction
(≥ 8 segments on the ground floor), the 10-room count, regularization
invariants, the metadata schema, byte stability across two runs and the CLI
(`run` plus every chained stage) on `tests/fixtures/sample_house.glb`.
