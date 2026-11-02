"""SVG and PNG floor plans drawn from metadata floor records."""

from __future__ import annotations

import math
from pathlib import Path
from xml.sax.saxutils import escape

from PIL import Image, ImageDraw, ImageFont

PADDING = 0.6  # metres around the plan


def _bounds(floor):
    xs, zs = [], []
    rings = [floor.get("outline") or []] + [r["polygon"] for r in floor["rooms"]]
    for ring in rings:
        for x, z in ring:
            xs.append(x)
            zs.append(z)
    for wall in floor["walls"]:
        xs.extend([wall["x1"], wall["x2"]])
        zs.extend([wall["z1"], wall["z2"]])
    if not xs:
        return (-1.0, -1.0, 1.0, 1.0)
    return (min(xs), min(zs), max(xs), max(zs))


class _Projector:
    def __init__(self, floor, scale):
        self.minx, self.minz, self.maxx, self.maxz = _bounds(floor)
        self.scale = scale
        self.width = int(math.ceil((self.maxx - self.minx + 2 * PADDING) * scale))
        self.height = int(math.ceil((self.maxz - self.minz + 2 * PADDING) * scale))

    def __call__(self, x, z):
        px = (x - self.minx + PADDING) * self.scale
        py = (self.maxz + PADDING - z) * self.scale
        return px, py


def write_svg(path, floor, scale=60.0):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    project = _Projector(floor, scale)
    width = max(project.width, 1)
    height = max(project.height, 1)
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}">',
        f"<title>{escape(floor['name'])}</title>",
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#ffffff"/>',
        '<g id="rooms">',
    ]
    for room in floor["rooms"]:
        points = " ".join(
            f"{project(x, z)[0]:.2f},{project(x, z)[1]:.2f}"
            for x, z in room["polygon"])
        parts.append(
            f'<polygon points="{points}" fill="#eef2f5" stroke="#c9d2da" '
            'stroke-width="1"/>')
    parts.append("</g>")

    if floor.get("outline"):
        points = " ".join(
            f"{project(x, z)[0]:.2f},{project(x, z)[1]:.2f}"
            for x, z in floor["outline"])
        parts.append(
            f'<polygon points="{points}" fill="none" stroke="#111827" '
            'stroke-width="2.5"/>')

    parts.append('<g id="walls" stroke="#111827" stroke-linecap="butt">')
    for wall in floor["walls"]:
        x1, y1 = project(wall["x1"], wall["z1"])
        x2, y2 = project(wall["x2"], wall["z2"])
        stroke = max(1.5, wall["thickness"] * scale)
        parts.append(
            f'<line x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" y2="{y2:.2f}" '
            f'stroke-width="{stroke:.2f}"/>')
    parts.append("</g>")

    parts.append('<g id="labels" font-family="Helvetica, Arial, sans-serif" '
                 'font-size="11" text-anchor="middle" fill="#374151">')
    for room in floor["rooms"]:
        cx, cz = room["centroid"]
        px, py = project(cx, cz)
        parts.append(
            f'<text x="{px:.2f}" y="{py:.2f}" dominant-baseline="central">'
            f"{escape(room['name'])}</text>")
    parts.append("</g>")
    parts.append("</svg>")

    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(parts) + "\n")
    return path


def _font():
    try:
        return ImageFont.load_default(size=13)
    except TypeError:  # Pillow < 10
        return ImageFont.load_default()


def write_png(path, floor, scale=60.0, max_pixels=2400):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    minx, minz, maxx, maxz = _bounds(floor)
    span = max(maxx - minx, maxz - minz, 1.0) + 2 * PADDING
    effective = min(scale, max_pixels / span)
    project = _Projector(floor, effective)
    width = max(project.width, 1)
    height = max(project.height, 1)
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)

    for room in floor["rooms"]:
        points = [project(x, z) for x, z in room["polygon"]]
        draw.polygon(points, fill=(238, 242, 245), outline=(201, 210, 218))

    if floor.get("outline"):
        points = [project(x, z) for x, z in floor["outline"]]
        draw.line(points, fill=(17, 24, 39), width=3)

    for wall in floor["walls"]:
        x1, y1 = project(wall["x1"], wall["z1"])
        x2, y2 = project(wall["x2"], wall["z2"])
        width_px = max(2, int(round(wall["thickness"] * effective)))
        draw.line([(x1, y1), (x2, y2)], fill=(17, 24, 39), width=width_px)

    font = _font()
    for room in floor["rooms"]:
        cx, cz = room["centroid"]
        px, py = project(cx, cz)
        text = room["name"]
        try:
            box = draw.textbbox((0, 0), text, font=font)
            tw, th = box[2] - box[0], box[3] - box[1]
        except AttributeError:  # pragma: no cover - very old Pillow
            tw, th = draw.textsize(text, font=font)
        draw.text((px - tw / 2, py - th / 2), text, fill=(55, 65, 81), font=font)

    image.save(path, format="PNG")
    return path
