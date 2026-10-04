"""The welcome card's subtitle script and its end-to-end render.

The render is where things went wrong on a real TV (blurry, low-bitrate,
CRT-warped), so the pixel properties that fixed it are pinned here.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from timewarptv.config import config_from_dict
from timewarptv.guide_gen import CARD_HEIGHT, CARD_WIDTH, ass_script, generate_guide


def test_script_keeps_the_overlay_canvas_and_one_event_per_line():
    script = ass_script("{\\an8}ONE\n{\\an7}TWO\n")

    assert "PlayResX: 1280" in script and "PlayResY: 720" in script
    assert "ScaledBorderAndShadow: yes" in script      # edges scale with the card
    assert script.count("Dialogue:") == 2


def test_blank_lines_are_not_events():
    assert ass_script("A\n\n  \nB").count("Dialogue:") == 2


@pytest.mark.skipif(
    not (shutil.which("mpv") and shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="needs mpv, ffmpeg and ffprobe",
)
def test_rendered_card_is_1080p_sharp_and_swapped_in_cleanly(tmp_path):
    from timewarptv.config import CrtConfig

    (tmp_path / "guide").mkdir()
    config = config_from_dict({"channels": [
        {"number": 1, "name": "Guide", "path": str(tmp_path / "guide")},
    ]})
    out = tmp_path / "guide" / "welcome.mp4"

    generate_guide(config, out, seconds=2)

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,pix_fmt", "-of", "csv=p=0", str(out)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert probe == f"{CARD_WIDTH},{CARD_HEIGHT},yuv420p"
    # Taller than the CRT cut-off, so it plays clean rather than curved.
    assert CARD_HEIGHT > CrtConfig().max_height
    # Nothing but the finished episode is left in the channel folder.
    assert sorted(p.name for p in out.parent.iterdir()) == ["welcome.mp4"]
