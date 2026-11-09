import importlib.util
import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from PIL import Image, ImageChops

if TYPE_CHECKING:
    from types import ModuleType

ROOT = Path(__file__).parents[2]
IMG = ROOT / "src" / "casttrail" / "web" / "static" / "img"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "generate_icons", ROOT / "scripts" / "generate_icons.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gen = _load()


def _paths() -> tuple[str, str, float]:
    """The trail's and the star's path data, and the trail's stroke width."""
    root = ET.fromstring((IMG / "logo.svg").read_text(encoding="utf-8"))
    paths = [e for e in root if e.tag.endswith("path")]
    trail = next(e for e in paths if e.attrib.get("fill") == "none")
    star = next(e for e in paths if e.attrib.get("fill") != "none")
    return trail.attrib["d"], star.attrib["d"], float(trail.attrib["stroke-width"])


# --- the logo ---------------------------------------------------------------------


def _inside(star: list[tuple[float, float]], point: tuple[float, float]) -> bool:
    """Whether a point is inside the star's outline (ray casting)."""
    x, y = point
    inside = False
    for (x1, y1), (x2, y2) in zip(star, [*star[1:], star[0]], strict=True):
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def test_the_trail_ends_hidden_underneath_the_star() -> None:
    trail_d, star_d, width = _paths()
    trail, _ = gen._flatten(trail_d)
    star, closed = gen._flatten(star_d)
    end_x, end_y = trail[-1]
    # The rounded end is a circle of half the stroke width. All of it, all round, must
    # sit inside the star, or a white edge would show beside the star.
    rim = [
        (
            end_x + width / 2 * math.cos(a / 16 * math.tau),
            end_y + width / 2 * math.sin(a / 16 * math.tau),
        )
        for a in range(16)
    ]

    assert closed
    assert _inside(star, (end_x, end_y))
    assert all(_inside(star, point) for point in rim)


def test_the_trail_is_drawn_before_the_star_so_the_star_covers_it() -> None:
    root = ET.fromstring((IMG / "logo.svg").read_text(encoding="utf-8"))
    paths = [e for e in root if e.tag.endswith("path")]

    assert [e.attrib["fill"] for e in paths] == ["none", "#ffc53d"]


def test_the_trail_comes_from_below_and_left_and_enters_under_the_star() -> None:
    trail_d, star_d, _ = _paths()
    trail, _ = gen._flatten(trail_d)
    star, _ = gen._flatten(star_d)
    start_x, start_y = trail[0]

    assert start_y > max(y for _, y in star)
    assert start_x < min(x for x, _ in star)


def test_nothing_white_shows_above_the_stars_lower_tips() -> None:
    icon = Image.open(IMG / "icon-512.png").convert("RGB")
    _, star_d, _ = _paths()
    star, _ = gen._flatten(star_d)
    # The star's two arms and body span these rows; the trail may only appear below
    # where it goes behind the star's underside, never beside the arms or body.
    white_rows = [
        y
        for y in range(512)
        for x in range(512)
        if icon.getpixel((x, y)) == (255, 255, 255)
    ]

    assert white_rows
    assert min(white_rows) > 200
    assert min(white_rows) > min(y for _, y in star) + 100


# --- the generated files ----------------------------------------------------------


def _render(
    name: str, size: int, rounded: bool, scale: float, alpha: bool
) -> Image.Image:
    svg = ET.fromstring((IMG / "logo.svg").read_text(encoding="utf-8"))
    image: Image.Image = gen.render(svg, size, rounded=rounded, scale=scale)
    return image if alpha else image.convert("RGB")


@pytest.mark.parametrize(("name", "size", "rounded", "scale", "alpha"), gen.ICONS)
def test_the_committed_icons_are_what_the_logo_renders_to(
    name: str, size: int, rounded: bool, scale: float, alpha: bool
) -> None:
    committed = Image.open(IMG / name)
    expected = _render(name, size, rounded, scale, alpha)

    assert committed.mode == expected.mode
    assert committed.size == expected.size
    assert ImageChops.difference(committed, expected).getbbox() is None


def test_the_committed_maskable_svg_is_what_the_logo_produces() -> None:
    source = (IMG / "logo.svg").read_text(encoding="utf-8")

    assert (IMG / "logo-maskable.svg").read_text(encoding="utf-8") == gen.maskable_svg(
        source
    )


def test_the_maskable_icon_has_no_transparency_and_a_clear_safe_zone() -> None:
    icon = Image.open(IMG / "icon-maskable-512.png")
    margin = round(512 * 0.1)
    corner = icon.getpixel((0, 0))
    edge_band = [
        icon.getpixel((x, y))
        for x in range(512)
        for y in (0, margin - 1, 511, 512 - margin)
    ]

    assert icon.mode == "RGB"
    assert corner == (0x2D, 0x3A, 0x8C)
    assert set(edge_band) == {corner}


def test_the_rounded_icons_have_transparent_corners() -> None:
    icon = Image.open(IMG / "icon-512.png")

    assert icon.getchannel("A").getpixel((0, 0)) == 0
    assert icon.getchannel("A").getpixel((256, 256)) == 255


def test_the_visible_trail_is_drawn_solid_with_no_gaps_in_the_stroke() -> None:
    icon = Image.open(IMG / "icon-512.png").convert("RGB")
    white = (255, 255, 255)
    trail_d, star_d, _ = _paths()
    trail, _ = gen._flatten(trail_d)
    star, _ = gen._flatten(star_d)
    # Every point along the trail's centre line that is not behind the star must be
    # white, which a stroke drawn as separate segments can fail (hairline gaps showed up
    # before dots were added).
    showing = [(x, y) for x, y in trail if not _inside(star, (x, y))]

    assert len(showing) > 20
    assert all(icon.getpixel((round(x), round(y))) == white for x, y in showing)


# --- the generator refuses what it cannot draw ------------------------------------


def _svg(body: str) -> ET.Element:
    return ET.fromstring(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">{body}</svg>'
    )


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ('<circle cx="1" cy="1" r="1" fill="#fff"/>', "unsupported <circle>"),
        ('<ellipse rx="1" ry="1"/>', "unsupported <ellipse>"),
        (
            '<path d="M0 0 Q 1 1 2 2" fill="none" stroke="#fff" stroke-width="2"/>',
            "path command",
        ),
        ('<path d="M0 0 L 10 0 L 10 10" fill="#fff"/>', "not closed"),
    ],
)
def test_a_shape_the_generator_cannot_draw_is_an_error_not_a_silent_gap(
    body: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        gen.render(_svg(body), 32, rounded=True, scale=1.0)


def test_a_logo_with_no_tile_cannot_make_a_maskable_svg() -> None:
    with pytest.raises(ValueError, match="no <rect>"):
        gen.maskable_svg('<svg viewBox="0 0 512 512"><path d="M0 0"/></svg>')


def test_the_maskable_svg_is_full_bleed_and_scaled_into_the_safe_zone() -> None:
    text = (IMG / "logo-maskable.svg").read_text(encoding="utf-8")

    assert "rx=" not in text
    assert re.search(r'transform="translate\(92\.16 92\.16\) scale\(0\.64\)"', text)
