"""The standby screensaver: a logo drifting and bouncing, DVD-player style.

Standby used to be the word STANDBY, fixed in the middle of a black screen -
exactly the kind of picture that burns into a TV left on overnight. Instead the
logo drifts slowly and bounces off the edges, and the wordmark sits in one
corner and moves to another every minute or so. Nothing stays in one place.

The motion is worked out from the clock, not stepped frame by frame: along each
axis the logo travels at a constant speed and reflects off the two walls, which
is a triangle wave. So every bounce is an exact reflection, a corner hit is
exactly a corner hit, and the path never drifts no matter how long it runs or
how unevenly frames arrive.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

# The four corners, clockwise from top-left.
CORNERS = ("top-left", "top-right", "bottom-right", "bottom-left")


def bounce(start: float, velocity: float, t: float, lo: float, hi: float) -> float:
    """Where a point moving at ``velocity`` from ``start`` is after ``t`` seconds,
    bouncing between ``lo`` and ``hi`` (a triangle wave)."""
    span = hi - lo
    if span <= 0:
        return lo
    d = (start - lo + velocity * t) % (2 * span)
    return lo + (d if d <= span else 2 * span - d)


@dataclass(frozen=True)
class Frame:
    x: float        # top-left of the logo, canvas pixels
    y: float
    corner: int     # index into CORNERS for the wordmark


class Screensaver:
    """Positions for the bouncing logo and the corner-hopping wordmark."""

    def __init__(
        self,
        *,
        width: float,
        height: float,
        logo_w: float,
        logo_h: float,
        margin: float = 24,
        speed: float = 70.0,
        corner_seconds: float = 60.0,
        clock: Callable[[], float],
        rng: Optional[random.Random] = None,
    ) -> None:
        self._rng = rng or random.Random()
        self._clock = clock
        self._x_range = (margin, width - margin - logo_w)
        self._y_range = (margin, height - margin - logo_h)
        # A random start and a random diagonal - never close to flat or
        # vertical, so it crosses the whole screen and visits every edge.
        self._x0 = self._rng.uniform(*self._x_range)
        self._y0 = self._rng.uniform(*self._y_range)
        angle = math.radians(self._rng.uniform(28, 62))
        self._vx = speed * math.cos(angle) * self._rng.choice((-1, 1))
        self._vy = speed * math.sin(angle) * self._rng.choice((-1, 1))
        self._corner_seconds = corner_seconds
        self._corner = self._rng.randrange(len(CORNERS))
        self._next_corner_move = clock() + corner_seconds
        self._t0 = clock()

    def frame(self) -> Frame:
        now = self._clock()
        if now >= self._next_corner_move:
            # Always a different corner, so the wordmark visibly moves.
            others = [i for i in range(len(CORNERS)) if i != self._corner]
            self._corner = self._rng.choice(others)
            self._next_corner_move = now + self._corner_seconds
        t = now - self._t0
        return Frame(
            x=bounce(self._x0, self._vx, t, *self._x_range),
            y=bounce(self._y0, self._vy, t, *self._y_range),
            corner=self._corner,
        )

    @property
    def x_range(self) -> Tuple[float, float]:
        return self._x_range

    @property
    def y_range(self) -> Tuple[float, float]:
        return self._y_range


__all__ = ["Screensaver", "Frame", "bounce", "CORNERS"]
