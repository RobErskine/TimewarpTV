"""SVG artwork -> ASS drawings. The logo files are the source of truth."""

from __future__ import annotations

import pytest

from timewarptv import brand

SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vb}"><path d="{d}" fill="#000"/></svg>"""


def _write(tmp_path, d, vb="100 200 50 20"):
    path = tmp_path / "art.svg"
    path.write_text(SVG.format(vb=vb, d=d))
    return path


def test_the_real_logo_and_wordmark_parse():
    logo, wordmark = brand.logo(), brand.wordmark()

    assert logo is not None and wordmark is not None
    assert 0.8 < logo.aspect < 1.1            # roughly square mark
    assert wordmark.aspect > 5                # long wordmark


def test_coordinates_are_relative_to_the_viewbox(tmp_path):
    art = brand.parse_svg(_write(tmp_path, "M100 200 L150 220 Z"))

    assert art.commands == (("M", (0.0, 0.0)), ("L", (50.0, 20.0)))


def test_svg_commands_map_to_ass_and_scale(tmp_path):
    art = brand.parse_svg(
        _write(tmp_path, "M100 200 L150 200 C110 205 120 210 130 220 Z")
    )

    # 20 units tall -> drawn 40 px tall: everything doubles.
    assert brand.drawing(art, height=40) == (
        "m 0.0 0.0 l 100.0 0.0 b 20.0 10.0 40.0 20.0 60.0 40.0"
    )


def test_extra_pairs_after_move_are_line_tos(tmp_path):
    art = brand.parse_svg(_write(tmp_path, "M100 200 110 210 120 220"))

    assert [op for op, _ in art.commands] == ["M", "L", "L"]


def test_unsupported_commands_fail_loudly(tmp_path):
    """A future export using relative or arc commands must not draw garbage."""
    for d in ("m100 200 l5 5", "M100 200 A5 5 0 0 1 110 210", "M100 200 H150"):
        with pytest.raises(ValueError):
            brand.parse_svg(_write(tmp_path, d))


def test_transforms_are_refused(tmp_path):
    path = tmp_path / "t.svg"
    path.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
        '<path transform="scale(2)" d="M0 0 L5 5 Z"/></svg>'
    )
    with pytest.raises(ValueError):
        brand.parse_svg(path)


def test_missing_artwork_is_none_not_a_crash(tmp_path):
    """No logo file must mean no logo on screen, never a crashed TV."""
    assert brand._load(tmp_path / "nope.svg") is None


def test_art_is_anchored_top_left_at_the_given_point(tmp_path):
    art = brand.parse_svg(_write(tmp_path, "M100 200 L150 220 Z"))

    ass = brand.art_ass(art, x=10, y=20, height=20, fill="&H00FFFFFF")

    assert ass.startswith(r"{\an7\pos(10,20)\p1")
    assert ass.endswith(r"{\p0}")
