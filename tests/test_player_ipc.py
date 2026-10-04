"""Message parsing for the IPC (subprocess mpv) player.

Only the parsing is covered here - everything else in this backend needs a
live mpv. The one thing worth pinning down is the shape of the message the
video window sends back when a key is pressed in it, because a wrong prefix or
argument position fails silently: keys would simply stop working.
"""

from __future__ import annotations

from timewarptv.player import MpvIpcPlayer


def _bare_player(on_key=None):
    """An instance with no mpv behind it - enough to parse one message."""
    player = object.__new__(MpvIpcPlayer)
    player.on_key = on_key
    return player


def test_window_key_message_is_forwarded():
    seen = []
    player = _bare_player(seen.append)

    player._handle_message(
        {"event": "client-message", "args": ["timewarptv", "volume_up"]}
    )

    assert seen == ["volume_up"]


def test_client_message_from_another_client_is_ignored():
    seen = []
    player = _bare_player(seen.append)

    player._handle_message({"event": "client-message", "args": ["osc", "volume_up"]})
    player._handle_message({"event": "client-message", "args": ["timewarptv"]})

    assert seen == []


def test_window_key_without_a_listener_does_not_raise():
    _bare_player(None)._handle_message(
        {"event": "client-message", "args": ["timewarptv", "mute"]}
    )


def test_a_failing_callback_does_not_kill_the_reader():
    """The IPC reader thread is the only source of end-of-episode events."""

    def boom(_name):
        raise RuntimeError("callback blew up")

    _bare_player(boom)._handle_message(
        {"event": "client-message", "args": ["timewarptv", "mute"]}
    )


# -- CRT auto-off for HD sources --------------------------------------------
# The effect sells "old show on a tube TV". A 1080p film is neither, and the
# shader costs GPU time a Pi 3 does not have spare, so it is dropped for the
# duration of that item and restored afterwards.

SHADER = "/tmp/crt.glsl"


def test_sd_source_keeps_the_shader():
    from timewarptv.player import _shader_for_height

    assert _shader_for_height(SHADER, 720, 480) == SHADER
    assert _shader_for_height(SHADER, 720, 360) == SHADER


def test_720p_kids_shows_keep_the_shader():
    """28 shows in the real library are 1280x720; they are still period TV."""
    from timewarptv.player import _shader_for_height

    assert _shader_for_height(SHADER, 720, 720) == SHADER


def test_hd_feature_drops_the_shader():
    from timewarptv.player import _shader_for_height

    for height in (800, 804, 816, 1040, 1080):
        assert _shader_for_height(SHADER, 720, height) == ""


def test_unknown_height_has_no_opinion():
    """mpv reports a null height on every file change; that must change nothing.

    Answering it with a real value flicked the effect back on for a moment at
    the start of each HD film, before the height arrived and turned it off.
    """
    from timewarptv.player import _shader_for_height

    assert _shader_for_height(SHADER, 720, None) is None
    assert _shader_for_height(SHADER, 720, 0) is None


def test_zero_threshold_means_always_on():
    from timewarptv.player import _shader_for_height

    assert _shader_for_height(SHADER, 0, 1080) == SHADER


def test_no_shader_configured_stays_no_shader():
    from timewarptv.player import _shader_for_height

    assert _shader_for_height(None, 720, 480) == ""
    assert _shader_for_height("", 720, 1080) == ""


def test_apply_crt_only_talks_to_mpv_when_the_answer_changes():
    """Every height tick would otherwise re-set the property many times a file."""
    from timewarptv.player import MpvIpcPlayer

    sent = []
    player = object.__new__(MpvIpcPlayer)
    player._shader = SHADER
    player._crt_max_height = 720
    player._crt_applied = SHADER
    player._command = lambda *a: sent.append(a)

    player._apply_crt(480)          # still SD - nothing to do
    assert sent == []

    player._apply_crt(1080)         # HD - turn it off, once
    player._apply_crt(1080)
    assert sent == [("set_property", "glsl-shaders", "")]

    player._apply_crt(None)         # file change: no opinion, stay off
    assert sent == [("set_property", "glsl-shaders", "")]

    player._apply_crt(480)          # back to SD - turn it on again
    assert sent[-1] == ("set_property", "glsl-shaders", SHADER)
