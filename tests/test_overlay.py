import re

from timewarptv.config import config_from_dict
import pytest

from timewarptv.overlay import OverlayManager
from timewarptv.player import MockPlayer
from tests.helpers import FakeClock, make_show

# The 4:3 frame within the 1280x720 canvas spans x in [160, 1120].
_FRAME_X0, _FRAME_X1 = 160, 1120


def _all_x_positions(ass: str):
    return [int(m) for m in re.findall(r"\\pos\((\d+),", ass)]


def _config(tmp_path):
    make_show(tmp_path, "a", 1)
    return config_from_dict(
        {
            "channel_bug_seconds": 4,
            "osd_duration": 2,
            "channels": [{"number": 3, "name": "Arthur", "path": str(tmp_path / "a")}],
        }
    )


def test_channel_bug_drawn_and_expires(tmp_path):
    clock = FakeClock()
    player = MockPlayer()
    om = OverlayManager(player, _config(tmp_path), clock=clock)

    om.show_channel_bug(3, "Arthur")
    assert 1 in player.overlays  # channel overlay id
    ass = player.overlays[1]
    assert "CH 03" in ass and "Arthur" in ass

    clock.advance(3.9)
    om.tick()
    assert 1 in player.overlays  # not yet expired

    clock.advance(0.2)
    om.tick()
    assert 1 not in player.overlays  # expired after 4s


def test_volume_overlay_has_label_and_bars(tmp_path):
    player = MockPlayer()
    om = OverlayManager(player, _config(tmp_path), clock=FakeClock())
    om.show_volume(45, muted=False)
    ass = player.overlays[2]
    assert "Volume" in ass
    # 20 segments: some drawn as bars (rectangles start "m 0 0 l"), rest as dots
    # (circles of radius 6). Counted by shape, not by \p1 drawings, because the
    # corner logo rides along on this overlay and draws its own.
    assert ass.count("m 0 0 l") + ass.count("m 0 -6 b") == 20


def test_volume_bars_scale_with_level(tmp_path):
    player = MockPlayer()
    om = OverlayManager(player, _config(tmp_path), clock=FakeClock())
    om.show_volume(100, muted=False)
    full = player.overlays[2].count("m 0 0 l")  # rectangle (filled bar) count
    om.show_volume(0, muted=False)
    empty = player.overlays[2].count("m 0 0 l")
    assert full == 20 and empty == 0


def test_muted_volume_overlay(tmp_path):
    player = MockPlayer()
    om = OverlayManager(player, _config(tmp_path), clock=FakeClock())
    om.show_volume(45, muted=True)
    assert "Mute" in player.overlays[2]


def test_standby_overlay_does_not_expire(tmp_path):
    clock = FakeClock()
    player = MockPlayer()
    om = OverlayManager(player, _config(tmp_path), clock=clock)
    om.show_standby()
    clock.advance(1000)
    om.tick()
    assert 3 in player.overlays  # standby id persists
    om.clear_standby()
    assert 3 not in player.overlays


def test_channel_name_with_braces_is_escaped(tmp_path):
    player = MockPlayer()
    om = OverlayManager(player, _config(tmp_path), clock=FakeClock())
    om.show_channel_bug(5, "Weird{name}")
    # Braces in the name must be neutralised (they delimit ASS override blocks).
    ass = player.overlays[1]
    assert "Weird(name)" in ass
    assert "Weird{name}" not in ass


def test_message_overlay(tmp_path):
    player = MockPlayer()
    om = OverlayManager(player, _config(tmp_path), clock=FakeClock())
    om.show_message("CH 12  -  NO CHANNEL")
    assert "NO CHANNEL" in player.overlays[4]


def test_channel_bug_sits_inside_4x3_frame(tmp_path):
    player = MockPlayer()
    om = OverlayManager(player, _config(tmp_path), clock=FakeClock())
    om.show_channel_bug(3, "Arthur")
    xs = _all_x_positions(player.overlays[1])
    assert xs and all(_FRAME_X0 <= x <= _FRAME_X1 for x in xs)


def test_volume_bar_sits_inside_4x3_frame(tmp_path):
    player = MockPlayer()
    om = OverlayManager(player, _config(tmp_path), clock=FakeClock())
    om.show_volume(100, muted=False)  # widest case: all 20 bars drawn
    xs = _all_x_positions(player.overlays[2])
    assert xs and all(_FRAME_X0 <= x <= _FRAME_X1 for x in xs)


def test_overlay_uses_configured_font_and_color(tmp_path):
    player = MockPlayer()
    om = OverlayManager(player, _config(tmp_path), clock=FakeClock())
    om.show_channel_bug(3, "Arthur")
    ass = player.overlays[1]
    assert "\\fnVT323" in ass          # bundled retro font
    assert "&H005AFF4D" in ass         # #4DFF5A -> ASS BBGGRR


# -- corner logo ------------------------------------------------------------
# The mark rides along on the banner and the volume overlay rather than owning
# an overlay slot of its own, so it appears and expires with them.


def _ui_with_logo(on):
    from timewarptv.config import UiConfig

    return UiConfig(logo=on)


def test_logo_rides_on_the_channel_banner():
    from timewarptv.overlay import _channel_bug_ass

    with_logo = _channel_bug_ass(4, "Kids", _ui_with_logo(True))
    without = _channel_bug_ass(4, "Kids", _ui_with_logo(False))

    assert with_logo.endswith(_logo_lines(True))
    assert "\\p1" not in without          # no drawings at all when off
    assert len(with_logo) > len(without)


def test_logo_rides_on_the_volume_bar():
    from timewarptv.overlay import _volume_ass

    with_logo = _volume_ass(45, False, _ui_with_logo(True))
    without = _volume_ass(45, False, _ui_with_logo(False))

    assert with_logo.endswith(_logo_lines(True))
    # The bar itself is unchanged: same 20 segments either way.
    for ass in (with_logo, without):
        assert ass.count("m 0 0 l") + ass.count("m 0 -6 b") == 20


def _logo_lines(on):
    from timewarptv.overlay import _logo_ass

    return _logo_ass(_ui_with_logo(on))


def test_logo_is_the_svg_artwork():
    """The corner mark is drawn from assets/logo.svg as one vector drawing."""
    ass = _logo_lines(True)

    assert "\n" not in ass                      # a single event
    assert r"\p1" in ass and ass.count(" b ") > 50   # its bezier curves


def test_logo_sits_clear_of_the_volume_bar():
    """The mark must not overlap the bar it shares the bottom of the screen with."""
    from timewarptv.overlay import _BAR_ROW_TOP, _LOGO_BOTTOM

    assert _LOGO_BOTTOM < _BAR_ROW_TOP


def test_logo_stays_inside_the_safe_area():
    from timewarptv.overlay import _IX1, _LOGO_RIGHT

    assert _LOGO_RIGHT <= _IX1


# -- welcome / channel-guide card -------------------------------------------
# Rendered once into channel 1's episode by timewarptv.guide_gen, so a
# mistake here ships as a wrong picture on the TV rather than a crash.


class _Ch:
    def __init__(self, number, name, passcode=None):
        self.number = number
        self.name = name
        self.passcode = passcode


def test_guide_lists_every_channel():
    from timewarptv.config import UiConfig
    from timewarptv.overlay import guide_ass

    channels = [_Ch(1, "Guide"), _Ch(2, "Playhouse"), _Ch(10, "Movies")]
    ass = guide_ass(channels, UiConfig())

    for text in ("01", "Guide", "02", "Playhouse", "10", "Movies"):
        assert text in ass


def test_guide_marks_only_locked_channels():
    from timewarptv.config import UiConfig
    from timewarptv.overlay import guide_ass

    ass = guide_ass(
        [_Ch(2, "Playhouse"), _Ch(9, "Adult Swim", passcode="1997")], UiConfig()
    )

    assert ass.count("LOCKED") == 1


def test_guide_header_is_the_logo_and_wordmark():
    from timewarptv.config import UiConfig
    from timewarptv.overlay import guide_ass

    ass = guide_ass([_Ch(1, "Guide")], UiConfig())
    header = [line for line in ass.split("\n") if r"\p1" in line]

    assert len(header) == 2                     # logo + wordmark, as drawings
    assert "TIME WARP TV" not in ass            # not typed out as text


def test_a_custom_station_name_is_typed_instead():
    from timewarptv.config import UiConfig
    from timewarptv.overlay import guide_ass

    ass = guide_ass([_Ch(1, "Guide")], UiConfig(brand="KID TV"))

    assert "KID TV" in ass
    assert r"\p1" not in ass


def test_guide_rows_stay_inside_the_safe_area():
    """A long line-up must shrink to fit, not run off the bottom of the screen."""
    import re

    from timewarptv.config import UiConfig
    from timewarptv.overlay import _GUIDE_ROWS_BOTTOM, guide_ass

    channels = [_Ch(n, f"Channel {n}") for n in range(1, 21)]
    ass = guide_ass(channels, UiConfig())

    # Two header lines sit above the rows; the footer deliberately sits below
    # _GUIDE_ROWS_BOTTOM, so check the rows themselves.
    ys = sorted(int(m) for m in re.findall(r"\\pos\(\d+,(\d+)\)", ass))
    row_ys = ys[2 : 2 + len(channels)]
    assert max(row_ys) <= _GUIDE_ROWS_BOTTOM


def test_guide_hint_becomes_one_multi_line_block():
    from timewarptv.config import UiConfig
    from timewarptv.overlay import guide_ass

    ass = guide_ass([_Ch(1, "Guide")], UiConfig(), hint="first\nsecond")

    assert r"first\Nsecond" in ass


def test_guide_escapes_channel_names():
    from timewarptv.config import UiConfig
    from timewarptv.overlay import guide_ass

    ass = guide_ass([_Ch(1, "Odd {name}")], UiConfig())

    assert "Odd (name)" in ass


def test_multi_line_message_is_one_styled_event():
    """Every line of a message must keep its position and green styling.

    A real newline reaching mpv starts a new, unstyled osd-overlay event, so
    the lock screen's second and third lines landed top-left in plain white.
    """
    from timewarptv.config import UiConfig
    from timewarptv.overlay import _message_ass

    ass = _message_ass("ADULT SWIM - LOCKED\nENTER CODE  [0] _ _ _\n< >  OK", UiConfig())

    assert "\n" not in ass
    assert ass.count(r"\N") == 2
    assert ass.startswith(r"{\an8\pos(")


# -- lock screen layout -------------------------------------------------------


def _boxes(ass):
    """(x, width) of each digit box - every drawing except the backing panel."""
    import re

    out = []
    for line in ass.split("\n")[1:]:
        if r"\p1" in line:
            x = int(re.search(r"\\pos\((\d+),", line).group(1))
            w = int(re.search(r"m 0 0 l (\d+) 0", line).group(1))
            out.append((x, w))
    return out


def test_lock_screen_has_one_box_per_digit():
    from timewarptv.config import UiConfig
    from timewarptv.overlay import _lock_ass

    for length in (1, 4, 6, 8):
        ass = _lock_ass("LOCKED", UiConfig(), entered=0, dial=0, length=length)
        assert len(_boxes(ass)) == length


def test_long_codes_still_fit_inside_the_panel():
    """Passcodes can be up to 8 digits; boxes narrow rather than overflow."""
    from timewarptv.config import UiConfig
    from timewarptv.overlay import _LOCK_PANEL_W, _LOCK_PANEL_X, _lock_ass

    ass = _lock_ass("LOCKED", UiConfig(), entered=3, dial=5, length=8)
    for x, w in _boxes(ass):
        assert x >= _LOCK_PANEL_X and x + w <= _LOCK_PANEL_X + _LOCK_PANEL_W


def test_lock_panel_clears_the_banner_volume_bar_and_logo():
    """Vol +/- and the channel banner both appear over the lock screen."""
    from timewarptv.overlay import (
        _BAR_ROW_TOP, _IY0, _LOCK_PANEL_H, _LOCK_PANEL_Y, _LOGO_TOP,
    )

    panel_bottom = _LOCK_PANEL_Y + _LOCK_PANEL_H
    assert _LOCK_PANEL_Y > _IY0 + 104 + 40              # below the show-name line
    assert panel_bottom < _BAR_ROW_TOP - 62             # above the "Volume" label
    assert panel_bottom < _LOGO_TOP                     # above the corner logo


def test_lock_screen_masks_entered_digits_and_highlights_the_dial():
    from timewarptv.config import UiConfig
    from timewarptv.overlay import _lock_ass

    ass = _lock_ass("LOCKED", UiConfig(), entered=2, dial=7, length=4)
    lines = ass.split("\n")

    assert sum(1 for line in lines if line.endswith("}*")) == 2
    assert sum(1 for line in lines if line.endswith("}7")) == 1


def test_status_replaces_the_help_line():
    from timewarptv.config import UiConfig
    from timewarptv.overlay import _lock_ass

    ass = _lock_ass("LOCKED", UiConfig(), entered=0, dial=0, length=4,
                    status="INCORRECT - TRY AGAIN")

    assert "INCORRECT - TRY AGAIN" in ass
    assert "CHOOSE" not in ass


def test_text_glow_has_a_dark_edge_not_a_green_halo():
    """The old 4px blurred *green* border smeared text over bright video."""
    from timewarptv.config import UiConfig
    from timewarptv.overlay import _hex_to_ass, _style

    ui = UiConfig()
    tags = _style(ui, size=40)

    assert rf"\3c{_hex_to_ass(ui.dim_color)}" in tags
    assert r"\blur4" not in tags


# -- "now playing" caption --------------------------------------------------------


def _caption_lines(caption):
    from timewarptv.config import UiConfig
    from timewarptv.overlay import _caption_ass

    return _caption_ass(caption, UiConfig())


def test_caption_is_right_aligned_and_sits_on_the_logo():
    import re

    from timewarptv.overlay import _IX1, _LOGO_TOP
    from timewarptv.titles import NowPlaying

    lines = _caption_lines(NowPlaying("Batman Beyond", "S03 E05  Out of the Past"))

    assert len(lines) == 2
    for line in lines:
        x, y = map(int, re.search(r"\\pos\((\d+),(\d+)\)", line).groups())
        assert line.startswith(r"{\an3") and x == _IX1 and y < _LOGO_TOP
    # the name above the detail
    ys = [int(re.search(r",(\d+)\)", line).group(1)) for line in lines]
    assert lines[-1].endswith("Batman Beyond") and ys[-1] < ys[0]


def test_caption_without_a_detail_is_one_line():
    from timewarptv.titles import NowPlaying

    assert len(_caption_lines(NowPlaying("Wee Sing Together"))) == 1


def test_caption_clears_the_volume_bar():
    """Vol +/- right after a channel change shows both at once."""
    from timewarptv.overlay import _BAR_ROW_TOP, _LOGO_TOP

    assert _LOGO_TOP < _BAR_ROW_TOP - 62   # caption sits above the logo, so above "Volume"


# -- standby screensaver ----------------------------------------------------------


class _CountingPlayer:
    """A MockPlayer that also counts how often each overlay is re-sent."""

    def __init__(self):
        from timewarptv.player import MockPlayer

        self.inner = MockPlayer()
        self.sends = {}

    def set_overlay(self, overlay_id, ass, w, h):
        self.sends[overlay_id] = self.sends.get(overlay_id, 0) + 1
        self.inner.set_overlay(overlay_id, ass, w, h)

    def clear_overlay(self, overlay_id):
        self.inner.clear_overlay(overlay_id)

    @property
    def overlays(self):
        return self.inner.overlays


def _saver_manager(tmp_path):
    player, clock = _CountingPlayer(), FakeClock()
    return OverlayManager(player, _config(tmp_path), clock=clock), player, clock


def test_standby_is_the_moving_logo_not_static_text(tmp_path):
    om, player, clock = _saver_manager(tmp_path)
    om.show_standby()
    first = player.overlays[3]

    clock.advance(1.0)
    om.tick()

    assert r"\p1" in first and "STANDBY" not in first   # the logo artwork
    assert player.overlays[3] != first                  # ...and it has moved
    assert 5 in player.overlays                         # the corner wordmark


def test_only_the_logo_is_resent_every_frame(tmp_path):
    om, player, clock = _saver_manager(tmp_path)
    om.show_standby()
    for _ in range(25 * 10):                            # ten seconds of frames
        clock.advance(1 / 25)
        om.tick()

    assert player.sends[3] > 200                        # animating
    assert player.sends[5] == 1                         # wordmark drawn once


def test_animation_only_asks_for_fast_ticks_while_running(tmp_path):
    om, _, _ = _saver_manager(tmp_path)
    assert om.frame_interval is None

    om.show_standby()
    assert om.frame_interval == pytest.approx(1 / 25)

    om.clear_standby()
    assert om.frame_interval is None


def test_leaving_standby_removes_logo_and_wordmark(tmp_path):
    om, player, clock = _saver_manager(tmp_path)
    om.show_standby()
    om.clear_standby()
    clock.advance(1.0)
    om.tick()                                           # must not redraw

    assert 3 not in player.overlays and 5 not in player.overlays
