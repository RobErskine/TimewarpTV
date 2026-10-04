#!/usr/bin/env python3
"""Regenerate the README screenshots in docs/screenshots/.

Each one is the real thing: the real mpv player with the real CRT shader, and
the real on-screen display drawn by the same code the box runs - so when the
look changes, re-run this and the README stays honest.

The footage is Big Buck Bunny (c) Blender Foundation, CC-BY 3.0 - an open film
made to be used like this. A 480p copy is fetched from Wikimedia Commons into
dev-media/ (gitignored) the first time. 480p is standard definition, so it gets
the CRT effect, as a period show on the box would.

Needs a desktop session (mpv opens a window to screenshot) plus mpv and ffmpeg.

    .venv/bin/python scripts/make-screenshots.py
"""

from __future__ import annotations

import functools
import random
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from timewarptv.config import CrtConfig, config_from_dict  # noqa: E402
from timewarptv.crt import write_shader  # noqa: E402
from timewarptv.guide_gen import render_card  # noqa: E402
import timewarptv.overlay as overlay_module  # noqa: E402
from timewarptv.overlay import OverlayManager  # noqa: E402
from timewarptv.player import MpvIpcPlayer  # noqa: E402
from timewarptv.static_gen import COLORBARS_FILENAME, DEFAULT_ASSETS_DIR, main as gen_assets  # noqa: E402
from timewarptv.titles import NowPlaying  # noqa: E402

OUT = REPO / "docs" / "screenshots"
CLIP = REPO / "dev-media" / "screenshot-src" / "bbb-480p.webm"
CLIP_URL = (
    "https://upload.wikimedia.org/wikipedia/commons/transcoded/c/c0/"
    "Big_Buck_Bunny_4K.webm/Big_Buck_Bunny_4K.webm.480p.vp9.webm"
)
USER_AGENT = "TimewarpTV-docs/1.0 (README screenshots; https://github.com/RobErskine/TimewarpTV)"

# The line-up shown on the guide card - the one the box was built for.
LINEUP = [
    (1, "Guide", None), (2, "Playhouse", None), (3, "Discovery", None),
    (4, "Toon Classics", None), (5, "Kids", None), (6, "Action", None),
    (7, "Movie Night", None), (8, "Big Movies", None),
    (9, "Adult Swim", "1997"), (10, "Movies", "1997"),
]


def fetch_clip() -> None:
    if CLIP.exists():
        return
    print("fetching Big Buck Bunny (480p, 2.5 minutes) from Wikimedia Commons...")
    CLIP.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-user_agent", USER_AGENT,
         "-ss", "00:01:00", "-i", CLIP_URL, "-t", "150", "-c", "copy", str(CLIP)],
        check=True,
    )


def config(tmp: Path):
    channels = []
    for number, name, code in LINEUP:
        folder = tmp / f"{number:02d}"
        folder.mkdir()
        entry = {"number": number, "name": name, "path": str(folder)}
        if code:
            entry.update(passcode=code, locked_message=f"{name.upper()} - LOCKED")
        channels.append(entry)
    return config_from_dict({"channels": channels})


class Shooter:
    """A 1280x720 mpv window we can load, pause, overlay and screenshot."""

    def __init__(self, cfg) -> None:
        shader = write_shader(CrtConfig())
        self.player = MpvIpcPlayer(
            fullscreen=False, force_4_3=False,
            glsl_shaders=str(shader), crt_max_height=CrtConfig().max_height,
            fonts_dir=REPO / "timewarptv" / "assets" / "fonts",
            extra_options={"geometry": "1280x720"},
        )
        self.osd = OverlayManager(self.player, cfg)

    def show(self, source: str, at: float = 0.0) -> None:
        self.osd.clear_all()
        if "://" in source:                  # a generated source, e.g. black
            self.player._command("loadfile", source, "replace")
        else:
            self.player.play(Path(source), start=at)
        self.player._command("set_property", "pause", False)
        time.sleep(2.0)                      # decode, and let the CRT settle
        self.player._command("set_property", "pause", True)

    def snap(self, name: str, *, photo: bool = False) -> Path:
        """Screenshot the window. ``photo`` saves a high-quality JPEG instead of
        a PNG: film frames with CRT scanlines are ~5 MB as PNG, ~200 KB as JPEG."""
        time.sleep(0.8)
        png = OUT / f"{name}.png"
        self.player._command("screenshot-to-file", str(png), "window")
        for _ in range(50):
            if png.exists() and png.stat().st_size:
                break
            time.sleep(0.1)
        out = png
        if photo:
            out = OUT / f"{name}.jpg"
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(png),
                            "-q:v", "3", str(out)], check=True)
            png.unlink()
        print("wrote", out.relative_to(REPO))
        return out


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    fetch_clip()
    colorbars = DEFAULT_ASSETS_DIR / COLORBARS_FILENAME
    if not colorbars.exists():
        gen_assets(["--assets-dir", str(DEFAULT_ASSETS_DIR)])

    with tempfile.TemporaryDirectory() as tmp:
        cfg = config(Path(tmp))

        # The guide card, exactly as the box renders it (1080p -> 720p here).
        card = Path(tmp) / "card.png"
        render_card(cfg, card)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(card),
                        "-vf", "scale=1280:720", str(OUT / "guide.png")], check=True)
        print("wrote", (OUT / "guide.png").relative_to(REPO))

        shoot = Shooter(cfg)
        try:
            # Flipping to a channel: banner, now-playing caption, logo.
            shoot.show(str(CLIP), at=60.0)
            shoot.osd.show_channel_bug(7, "Movie Night",
                                       caption=NowPlaying("Big Buck Bunny", "2008"),
                                       duration=0)
            shoot.snap("channel-change", photo=True)

            # Turning it up.
            shoot.show(str(CLIP), at=36.0)
            shoot.osd.show_volume(60, muted=False, duration=0)
            shoot.snap("volume", photo=True)

            # A locked channel's combination lock, part-way through a code.
            shoot.show(str(colorbars))
            shoot.osd.show_channel_bug(10, "Movies", duration=0)
            shoot.osd.show_lock("MOVIES - LOCKED", entered=2, dial=7, length=4)
            shoot.snap("lock", photo=True)

            # Standby: the drifting logo and the corner wordmark. A fixed seed so
            # the shot is repeatable, chosen to keep the two well apart.
            overlay_module.Screensaver = functools.partial(
                overlay_module.Screensaver, rng=random.Random(15)
            )
            shoot.show("av://lavfi:color=c=black:s=1280x720:r=25")
            shoot.osd.show_standby()
            for _ in range(25 * 6):          # let it drift a little first
                shoot.osd.tick()
                time.sleep(1 / 25)
            shoot.snap("standby")
        finally:
            shoot.player.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
