"""modelkit - deterministic floor-plan reconstruction from a house scan.

The package turns a GLB/glTF/OBJ mesh of a house into:

  * storey levels (slab tops / ceilings),
  * room polygons per storey,
  * regularized wall centre-line segments,
  * a confirmation list for ambiguous geometry,
  * a rebuilt GLB, SVG/PNG plans and a machine readable metadata.json.

Everything is deterministic: the same input bytes and options produce
byte-identical metadata, plans and rebuilt model.
"""

__version__ = "1.0.0"
PIPELINE_VERSION = 1
SCHEMA = 1

__all__ = ["__version__", "PIPELINE_VERSION", "SCHEMA"]
