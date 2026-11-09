"""Render the app icons in src/casttrail/web/static/img from the CastTrail logo.

    uv run poe generate-icons

The PNGs and logo-maskable.svg are committed, so a build never needs this; run it when
logo.svg changes. It reads the shapes in logo.svg and draws them with Pillow at eight
times the size before shrinking, for smooth edges. It handles only a rounded tile, a
stroked curve (``M`` and ``C``) and a filled polygon (``M``, ``L`` and ``Z``), and fails
if the SVG gains anything else, so a new shape is noticed rather than silently dropped.
"""

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image, ImageColor, ImageDraw

IMG = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "casttrail"
    / "web"
    / "static"
    / "img"
)
SVG = IMG / "logo.svg"
SUPERSAMPLE = 8
CURVE_STEPS = 64
# A maskable icon keeps its mark inside the middle 80%, since the platform crops it.
MASKABLE_SCALE = 0.64

# name, size, rounded tile, mark scale, keep the alpha channel
ICONS = [
    ("icon-192.png", 192, True, 1.0, True),
    ("icon-512.png", 512, True, 1.0, True),
    ("icon-maskable-512.png", 512, False, MASKABLE_SCALE, False),
    ("apple-touch-icon.png", 180, False, 1.0, False),
    ("favicon-32.png", 32, True, 1.0, True),
]

Point = tuple[float, float]
_TOKEN = re.compile(r"[MCLZ]|-?\d+(?:\.\d+)?")


def _numbers(tokens: list[str], start: int, count: int) -> list[float]:
    return [float(t) for t in tokens[start : start + count]]


def _flatten(path: str) -> tuple[list[Point], bool]:
    """Turn an ``M``/``C``/``L``/``Z`` path into points, and say if it is closed."""
    tokens = _TOKEN.findall(path)
    points: list[Point] = []
    closed = False
    i = 0
    while i < len(tokens):
        command = tokens[i]
        i += 1
        if command == "M" or command == "L":
            x, y = _numbers(tokens, i, 2)
            points.append((x, y))
            i += 2
        elif command == "C":
            x1, y1, x2, y2, x3, y3 = _numbers(tokens, i, 6)
            x0, y0 = points[-1]
            for step in range(1, CURVE_STEPS + 1):
                t = step / CURVE_STEPS
                u = 1 - t
                points.append(
                    (
                        u**3 * x0 + 3 * u * u * t * x1 + 3 * u * t * t * x2 + t**3 * x3,
                        u**3 * y0 + 3 * u * u * t * y1 + 3 * u * t * t * y2 + t**3 * y3,
                    )
                )
            i += 6
        elif command == "Z":
            closed = True
        else:
            msg = f"logo.svg has an unsupported path command {command!r}"
            raise ValueError(msg)
    return points, closed


def render(svg: ET.Element, size: int, *, rounded: bool, scale: float) -> Image.Image:
    """Draw the SVG's shapes at ``size`` pixels square.

    Args:
        svg: The parsed logo.
        size: The width and height of the result.
        rounded: Whether the tile has rounded corners and transparent outside them.
        scale: How much to shrink the mark inside the tile (1.0 is its own size).

    Returns:
        The image, with an alpha channel.
    """
    view = [float(n) for n in svg.attrib["viewBox"].split()]
    big = size * SUPERSAMPLE
    unit = big / view[2]
    # The mark is scaled about the middle of the tile.
    shift = (1 - scale) / 2 * view[2]

    def place(point: Point) -> Point:
        return ((point[0] * scale + shift) * unit, (point[1] * scale + shift) * unit)

    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    for shape in svg:
        kind = shape.tag.rpartition("}")[2]
        attr = shape.attrib
        if kind == "rect":
            radius = float(attr.get("rx", 0)) * unit if rounded else 0
            draw.rounded_rectangle(
                (0, 0, big - 1, big - 1), radius=radius, fill=attr["fill"]
            )
        elif kind == "path" and attr.get("fill", "none") == "none":
            points = [place(p) for p in _flatten(attr["d"])[0]]
            colour = ImageColor.getrgb(attr["stroke"])
            width = round(float(attr["stroke-width"]) * scale * unit)
            draw.line(points, fill=colour, width=width)
            # A dot at every point gives round caps and joins, and no gaps between the
            # short segments a curve is cut into.
            for x, y in points:
                draw.ellipse(
                    (x - width / 2, y - width / 2, x + width / 2, y + width / 2),
                    fill=colour,
                )
        elif kind == "path":
            points, closed = _flatten(attr["d"])
            if not closed:
                msg = "logo.svg has a filled path that is not closed"
                raise ValueError(msg)
            draw.polygon([place(p) for p in points], fill=attr["fill"])
        else:
            msg = f"logo.svg has an unsupported <{kind}>"
            raise ValueError(msg)
    return image.resize((size, size), Image.Resampling.LANCZOS)


def maskable_svg(source: str) -> str:
    """Build logo-maskable.svg from logo.svg.

    Same shapes, but the tile is full-bleed (no rounded corners) and the mark is shrunk
    into the safe zone the platform's crop leaves alone.

    Args:
        source: The text of logo.svg.

    Returns:
        The text of logo-maskable.svg.

    Raises:
        ValueError: If logo.svg has no tile followed by shapes.
    """
    inner = re.search(r"<rect[^>]*/>\s*(.*)</svg>", source, flags=re.DOTALL)
    rect = re.search(r"<rect[^>]*/>", source)
    if inner is None or rect is None:
        msg = "logo.svg has no <rect> followed by shapes"
        raise ValueError(msg)
    shift = (1 - MASKABLE_SCALE) / 2 * 512
    full_bleed = re.sub(r'\s+rx="[^"]*"', "", rect.group(0))
    head = source[: source.index("<rect")]
    group = f'<g transform="translate({shift:g} {shift:g}) scale({MASKABLE_SCALE})">'
    shapes = inner.group(1).strip()
    return "\n".join([f"{head}{full_bleed}", group, shapes, "</g>", "</svg>", ""])


def main() -> None:
    """Write every icon, and the maskable SVG."""
    source = SVG.read_text(encoding="utf-8")
    svg = ET.fromstring(source)
    for name, size, rounded, scale, keep_alpha in ICONS:
        image = render(svg, size, rounded=rounded, scale=scale)
        if not keep_alpha:
            image = image.convert("RGB")
        image.save(IMG / name)
        print(f"{name} {size}x{size}")
    (IMG / "logo-maskable.svg").write_text(
        maskable_svg(source), encoding="utf-8", newline="\n"
    )
    print("logo-maskable.svg")


if __name__ == "__main__":
    main()
