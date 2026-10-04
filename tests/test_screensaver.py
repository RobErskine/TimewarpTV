"""The standby screensaver's motion: DVD-style bouncing, worked out from time."""

from __future__ import annotations

import math
import random

import pytest

from timewarptv.screensaver import CORNERS, Screensaver, bounce
from tests.helpers import FakeClock


@pytest.mark.parametrize("t, expected", [
    (0, 10), (5, 60),      # moving right at 10/s from 10
    (9, 100),              # reaches the right wall at 100...
    (10, 90), (18, 10),    # ...comes back...
    (19, 0),               # ...reaches the left wall...
    (21, 20),              # ...and bounces off it again
])
def test_bounce_reflects_off_both_walls(t, expected):
    assert bounce(10, 10, t, 0, 100) == pytest.approx(expected)


def test_bounce_moving_left_starts_by_going_down():
    assert bounce(50, -10, 2, 0, 100) == pytest.approx(30)


def _saver(clock, seed=1, **kw):
    return Screensaver(width=1280, height=720, logo_w=140, logo_h=150,
                       margin=28, clock=clock, rng=random.Random(seed), **kw)


def test_the_logo_never_leaves_the_screen():
    clock = FakeClock()
    for seed in range(20):
        saver = _saver(clock, seed)
        for _ in range(2000):                      # ~33 minutes at 1s steps
            clock.advance(1.0)
            f = saver.frame()
            assert 28 <= f.x <= 1280 - 28 - 140
            assert 28 <= f.y <= 720 - 28 - 150


def test_motion_is_smooth_and_slow():
    """No jumps between frames: at 25fps it moves a few pixels at a time."""
    clock = FakeClock()
    saver = _saver(clock)
    prev = saver.frame()
    for _ in range(25 * 60):
        clock.advance(1 / 25)
        f = saver.frame()
        assert math.hypot(f.x - prev.x, f.y - prev.y) <= 70 / 25 + 1e-6
        prev = f


def test_it_visits_every_edge():
    """A diagonal path, never flat or vertical, so the whole screen gets used."""
    clock = FakeClock()
    saver = _saver(clock, seed=7)
    xs, ys = [], []
    for _ in range(600):                           # 10 minutes
        clock.advance(1.0)
        f = saver.frame()
        xs.append(f.x)
        ys.append(f.y)
    (x_lo, x_hi), (y_lo, y_hi) = saver.x_range, saver.y_range
    assert min(xs) < x_lo + 70 and max(xs) > x_hi - 70
    assert min(ys) < y_lo + 70 and max(ys) > y_hi - 70


def test_wordmark_moves_to_a_different_corner_every_minute():
    clock = FakeClock()
    saver = _saver(clock, corner_seconds=60)
    corners = [saver.frame().corner]
    for _ in range(10):
        clock.advance(59)
        assert saver.frame().corner == corners[-1]  # not yet
        clock.advance(1.5)
        corners.append(saver.frame().corner)

    assert all(a != b for a, b in zip(corners, corners[1:]))
    assert all(0 <= c < len(CORNERS) for c in corners)
