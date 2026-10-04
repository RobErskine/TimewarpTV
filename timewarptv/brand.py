"""The Time Warp TV logo and wordmark, drawn straight from their SVG files.

The artwork lives in ``assets/`` as plain SVG and stays the source of truth:
replace ``logo.svg`` or ``wordmark.svg`` and every screen picks up the new art.
libass (which draws all of the on-screen display) can't read SVG, but an ASS
drawing uses the same primitives as an SVG path, so the conversion is direct:

    SVG  M x y        ->  ASS  m x y
    SVG  L x y        ->  ASS  l x y
    SVG  C a b c d e f ->  ASS  b a b c d e f
    SVG  Z            ->  (closed implicitly by the next m)

Only what the artwork actually uses is supported - absolute M/L/C/Z, no
transforms - and anything else raises, so a future export with, say, relative
commands fails loudly in a test instead of drawing garbage on the TV.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import List, Optional, Tuple

log = logging.getLogger(__name__)

ASSETS_DIR = Path(__file__).resolve().parent / "assets"
LOGO_SVG = ASSETS_DIR / "logo.svg"
WORDMARK_SVG = ASSETS_DIR / "wordmark.svg"

_SVG_NS = "{http://www.w3.org/2000/svg}"
_TOKEN = re.compile(r"[MLCZmlczHhVvSsQqTtAa]|-?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
_ARITY = {"M": 2, "L": 2, "C": 6}


@dataclass(frozen=True)
class Art:
    """Vector artwork, with coordinates relative to its viewBox's top-left."""

    commands: Tuple[Tuple[str, Tuple[float, ...]], ...]
    width: float
    height: float

    @property
    def aspect(self) -> float:
        return self.width / self.height


def parse_svg(path: Path) -> Art:
    """Read every filled path in an SVG into one piece of artwork."""
    root = ET.parse(path).getroot()
    viewbox = root.get("viewBox")
    if not viewbox:
        raise ValueError(f"{path}: no viewBox")
    min_x, min_y, width, height = (float(v) for v in viewbox.replace(",", " ").split())

    commands: List[Tuple[str, Tuple[float, ...]]] = []
    for element in root.iter(f"{_SVG_NS}path"):
        if element.get("fill", "").lower() == "none":
            continue
        if element.get("transform"):
            raise ValueError(f"{path}: path transforms are not supported")
        commands.extend(_parse_d(element.get("d", ""), min_x, min_y, path))
    if not commands:
        raise ValueError(f"{path}: no drawable paths")
    return Art(tuple(commands), width, height)


def _parse_d(d: str, dx: float, dy: float, path: Path):
    tokens = _TOKEN.findall(d)
    out = []
    i, op = 0, None
    while i < len(tokens):
        token = tokens[i]
        if token.isalpha():
            op = token
            i += 1
            if op in ("Z", "z"):
                continue
            if op not in _ARITY:
                raise ValueError(f"{path}: SVG path command {op!r} is not supported")
        elif op is None or op in ("Z", "z"):
            raise ValueError(f"{path}: malformed path data near {token!r}")
        n = _ARITY[op]
        values = [float(t) for t in tokens[i : i + n]]
        if len(values) != n:
            raise ValueError(f"{path}: truncated {op} command")
        # Shift to the viewBox origin, so (0, 0) is the artwork's top-left.
        shifted = tuple(v - (dx if k % 2 == 0 else dy) for k, v in enumerate(values))
        out.append((op, shifted))
        i += n
        if op == "M":
            op = "L"  # extra coordinate pairs after M are implicit line-tos
    return out


def drawing(art: Art, height: float) -> str:
    """ASS drawing commands for ``art`` scaled to ``height`` canvas pixels."""
    scale = height / art.height
    ass_op = {"M": "m", "L": "l", "C": "b"}
    parts = []
    for op, values in art.commands:
        coords = " ".join(f"{v * scale:.1f}" for v in values)
        parts.append(f"{ass_op[op]} {coords}")
    return " ".join(parts)


def art_ass(
    art: Art,
    *,
    x: float,
    y: float,
    height: float,
    fill: str,
    edge: Optional[str] = None,
    alpha: int = 0,
) -> str:
    """One ASS event drawing ``art`` with its top-left corner at (x, y).

    ``edge`` gives the artwork the same thin dark outline as the OSD text, so it
    stays legible over bright video. ``alpha`` is ASS transparency (0 = solid,
    255 = invisible). Anchored top-left (``\\an7``) with coordinates starting
    at 0,0 - the placement that lands exactly.
    """
    a = f"&H{alpha:02X}&"
    border = rf"\bord1\3c{edge}\3a{a}" if edge else r"\bord0"
    return (
        rf"{{\an7\pos({round(x)},{round(y)})\p1\c{fill}\1a{a}{border}\shad0\blur0.5}}"
        f"{drawing(art, height)}{{\\p0}}"
    )


@lru_cache(maxsize=None)
def _load(path: Path) -> Optional[Art]:
    try:
        return parse_svg(path)
    except (OSError, ET.ParseError, ValueError) as exc:
        log.warning("brand artwork unavailable (%s): %s", path.name, exc)
        return None


def logo() -> Optional[Art]:
    """The Time Warp TV mark, or None if the file is missing or unreadable."""
    return _load(LOGO_SVG)


def wordmark() -> Optional[Art]:
    """The TIME WARP TV wordmark, or None if the file is missing or unreadable."""
    return _load(WORDMARK_SVG)


__all__ = ["Art", "parse_svg", "drawing", "art_ass", "logo", "wordmark"]
