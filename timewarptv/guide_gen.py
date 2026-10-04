"""Render the welcome / channel-guide screen into a video file.

Channel 1 is a "welcome channel", the way a hotel TV has one: tune to it and it
tells you what else is on. The running box has no concept of such a channel -
it is an ordinary channel folder holding one very boring episode, produced here.

Why a video file rather than something the app draws live: a channel is a
folder of episodes, and keeping it that way means the welcome screen needs no
new state, no new code path, and cannot break playback. The cost is that the
card has to be regenerated when the line-up changes - which is what this module
is for. It runs headless, so it works on the Pi itself as well as a desktop::

    timewarptv --make-guide        # -> <config folder>/01-guide/welcome.mp4

The card is drawn by :func:`timewarptv.overlay.guide_ass`, so it uses the same
font, phosphor green and edge as the channel banner and volume bar. libass does
the drawing (as it does for every other readout), via mpv's encoding mode, which
burns the text into a single frame without needing a window or a display.

Picture quality matters more here than anywhere else - it's small text, not a
moving picture - so the card is made to stay sharp on the way to the screen:

* **1920x1080.** Sharp when shrunk to a smaller TV (a 1366x768 panel is common
  in the kind of room this box ends up in), native on a 1080p one. Being taller
  than ``crt.max_height`` it also plays without the CRT effect, whose curvature
  would otherwise resample every pixel of the text.
* **Near-lossless encode.** A still costs almost nothing to encode well, and
  thin saturated-green strokes are exactly what a default-quality encode smears.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from .config import Config, load_config
from .overlay import CANVAS_H, CANVAS_W, guide_ass

log = logging.getLogger(__name__)

GUIDE_FILENAME = "welcome.mp4"

# What the remote can do, spelled out at the foot of the card. Two short lines
# read better on a TV across the room than one long one.
DEFAULT_HINT = (
    "UP / DOWN  change channel        LEFT / RIGHT  skip episode\n"
    "+ / -  volume      HOME  this guide      BACK  last channel"
)

# Long enough that the loop point is rare, short enough to stay a small file.
# The picture never changes, so the encoder spends almost nothing on it.
DEFAULT_SECONDS = 300

CARD_WIDTH = 1920
CARD_HEIGHT = 1080

FONTS_DIR = Path(__file__).resolve().parent / "assets" / "fonts"


def ass_script(events: str) -> str:
    """Wrap overlay events (one per line) into a standalone .ass subtitle file.

    The events are laid out on the same 1280x720 canvas as every overlay, and
    libass scales them to whatever size the frame is rendered at.
    """
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {CANVAS_W}\n"
        f"PlayResY: {CANVAS_H}\n"
        "WrapStyle: 2\n"
        "ScaledBorderAndShadow: yes\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
        "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding\n"
        "Style: Default,VT323,40,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,"
        "0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text\n"
    )
    lines = [
        f"Dialogue: 0,0:00:00.00,9:00:00.00,Default,,0,0,0,,{event}"
        for event in events.split("\n")
        if event.strip()
    ]
    return header + "\n".join(lines) + "\n"


def render_card(
    config: Config,
    out_png: Path,
    *,
    hint: str = DEFAULT_HINT,
    mpv_binary: str = "mpv",
    width: int = CARD_WIDTH,
    height: int = CARD_HEIGHT,
) -> Path:
    """Draw the guide card to a PNG, headless, and return its path."""
    if shutil.which(mpv_binary) is None:
        raise RuntimeError(
            f"the '{mpv_binary}' binary was not found. Install it with "
            "`brew install mpv` (macOS) or `sudo apt install mpv` (Linux)."
        )
    out_png.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="timewarptv-guide-") as tmp:
        subs = Path(tmp) / "card.ass"
        subs.write_text(
            ass_script(guide_ass(config.channels, config.ui, hint=hint)),
            encoding="utf-8",
        )
        cmd = [
            mpv_binary, "--no-config", "--msg-level=all=error",
            f"av://lavfi:color=c=black:s={width}x{height}:r=1:d=1",
            f"--sub-file={subs}",
            f"--sub-fonts-dir={FONTS_DIR}",
            "--frames=1",
            f"--o={out_png}", "--of=image2", "--ofopts=update=1", "--ovc=png",
        ]
        log.info("running: %s", " ".join(cmd))
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    if not out_png.is_file() or out_png.stat().st_size == 0:
        raise RuntimeError(f"mpv did not write the guide card to {out_png}")
    log.info("wrote guide card: %s", out_png)
    return out_png


def generate_guide(
    config: Config,
    out_path: Path,
    *,
    seconds: int = DEFAULT_SECONDS,
    hint: str = DEFAULT_HINT,
    keep_png: bool = False,
) -> Path:
    """Render the card and encode it as the welcome channel's one episode.

    Written to a side file and swapped in at the end, so a running TV that has
    the old card open never reads a half-written one - and a failed run never
    leaves a broken ``.mp4`` for the channel to pick up.
    """
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg not found (brew install ffmpeg / apt install ffmpeg)")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="timewarptv-guide-") as tmp:
        png = render_card(config, Path(tmp) / "card.png", hint=hint)
        # Not "*.mp4": a leftover from a failed run must not become an episode.
        partial = out_path.with_name(out_path.name + ".partial")
        # A still at 5fps with a near-lossless quality setting: a tiny file that
        # keeps thin text sharp. yuv420p High profile is what the Pi 3 decodes
        # in hardware; 1080p is the most it will output anyway. The fastest
        # preset on two threads: for a picture that never moves, a slower one
        # buys nothing, and pinning every core of a Pi 3 for minutes is the
        # kind of load that trips its under-voltage warning.
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-loop", "1", "-framerate", "5", "-i", str(png),
            "-t", str(seconds),
            "-c:v", "libx264", "-preset", "veryfast", "-threads", "2",
            "-tune", "stillimage",
            "-crf", "10", "-pix_fmt", "yuv420p", "-profile:v", "high",
            "-r", "5", "-g", "50",
            "-movflags", "+faststart",
            "-f", "mp4", str(partial),
        ]
        log.info("running: %s", " ".join(cmd))
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        os.replace(partial, out_path)
        if keep_png:
            shutil.copy(png, out_path.with_suffix(".png"))
    log.info("wrote welcome channel: %s", out_path)
    return out_path


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render the welcome / channel-guide screen as channel 1's episode."
    )
    parser.add_argument("-c", "--config", required=True, help="path to config.yaml")
    parser.add_argument(
        "-o", "--out", required=True,
        help=f"output video (e.g. /media/timewarptv/01-guide/{GUIDE_FILENAME})",
    )
    parser.add_argument(
        "--seconds", type=int, default=DEFAULT_SECONDS,
        help=f"length of the clip before it loops (default {DEFAULT_SECONDS})",
    )
    parser.add_argument(
        "--keep-png", action="store_true",
        help="also keep the rendered still, next to the video",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    config = load_config(args.config)
    generate_guide(
        config, Path(args.out), seconds=args.seconds, keep_png=args.keep_png
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "generate_guide", "render_card", "ass_script", "GUIDE_FILENAME", "DEFAULT_HINT",
]
