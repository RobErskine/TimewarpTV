"""The video player abstraction.

The application talks to an abstract :class:`Player`; two implementations exist:

* :class:`MpvPlayer` - the real thing, backed by libmpv (via the ``python-mpv``
  package). This is what runs on the Raspberry Pi against the TV.
* :class:`MockPlayer` - a no-op player that records what it was asked to do and
  lets tests/dev drive "the episode ended" by hand. This lets the entire app be
  exercised on a laptop with no display, no libmpv, and no media files.

Keeping this boundary thin (load / stop / volume / a couple of OSD hooks) means
the interesting logic in ``app.py`` never has to know which one it is using.
"""

from __future__ import annotations

import logging
import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# First argument of the script-message that video-window keys send back to
# us, so mpv's own messages and any other client's can be told apart.
_WINDOW_KEY_PREFIX = "timewarptv"

# mpv property holding the height of the video as the decoder produced it.
# Deliberately not "video-out-params/h", which is the height after filters -
# with force_4_3 that is always 720 and would tell us nothing about the source.
_HEIGHT_PROPERTY = "video-params/h"


def _shader_for_height(
    shader: Optional[str], max_height: int, height: Optional[int]
) -> Optional[str]:
    """The ``glsl-shaders`` value suiting a source this tall ("" = no shader).

    Anything taller than ``max_height`` is treated as a modern HD feature: the
    CRT effect is both wrong for it and expensive, so it is dropped for the
    duration of that item.

    Returns ``None`` for "no opinion", which the caller must treat as "change
    nothing". mpv reports the height as null for a moment on every file change;
    answering that with a real value would flick the effect on at the start of
    each HD film, just before the height arrived and turned it off again.
    """
    if not shader:
        return ""
    if not max_height:
        return shader
    if not height:
        return None
    return "" if height > max_height else shader

# Reason strings passed to the "playback finished" callback.
END_EOF = "eof"        # the file played to its natural end -> roll next episode
END_ERROR = "error"    # the file failed to play -> skip to next episode
END_STOPPED = "stopped"  # we stopped it on purpose (channel change) -> ignore


class Player(ABC):
    """Minimal video-player interface used by the application."""

    #: Called when playback of the current item finishes. Receives one of the
    #: END_* reason strings. Set by the application before playing anything.
    on_end: Optional[Callable[[str], None]] = None

    #: Called when a key is pressed in the player's own video window. Receives
    #: an action name (``"volume_up"``). Only players that own a window and can
    #: report keys back (see :class:`MpvIpcPlayer`) ever call this.
    on_key: Optional[Callable[[str], None]] = None

    @abstractmethod
    def play(self, path: Path, *, start: float = 0.0) -> None:
        """Begin playing ``path`` from ``start`` seconds in."""

    @abstractmethod
    def play_loop(self, path: Path) -> None:
        """Play ``path`` on an endless loop (used for the static/no-signal clip)."""

    def play_transition(
        self,
        static_path: Path,
        target_path: Path,
        *,
        start: float = 0.0,
        static_seconds: float = 0.5,
    ) -> None:
        """Show a brief static burst, then the target episode.

        The default implementation just plays the target; players that can
        preload (see :class:`MpvPlayer`) override this to make the switch
        near-instant.
        """
        self.play(target_path, start=start)

    def preload_next(self, target_path: Path, *, start: float = 0.0) -> None:
        """Begin loading ``target_path`` in the background while the CURRENT item
        keeps playing. Call :meth:`commit_switch` to cut over once it's ready.

        The default implementation has no way to preload, so it just plays the
        target immediately; :class:`MpvPlayer` overrides it.
        """
        self.play(target_path, start=start)

    def commit_switch(self) -> None:
        """Switch to the item queued by :meth:`preload_next` (no-op by default)."""

    @abstractmethod
    def stop(self) -> None:
        """Stop playback and show a blank screen."""

    @abstractmethod
    def set_volume(self, volume: int) -> None:
        """Set the volume (0-100)."""

    @abstractmethod
    def set_mute(self, muted: bool) -> None: ...

    @abstractmethod
    def get_time_pos(self) -> Optional[float]:
        """Current playback position in seconds, or None if nothing is playing."""

    @abstractmethod
    def show_text(self, text: str, duration: float) -> None:
        """Show a plain OSD message for ``duration`` seconds."""

    @abstractmethod
    def set_overlay(self, overlay_id: int, ass: str, res_x: int, res_y: int) -> None:
        """Draw an ASS overlay with the given id (replacing any previous one)."""

    @abstractmethod
    def clear_overlay(self, overlay_id: int) -> None:
        """Remove a previously drawn overlay."""

    @abstractmethod
    def close(self) -> None:
        """Release resources."""


class MpvPlayer(Player):
    """A :class:`Player` backed by libmpv, tuned for a Raspberry Pi + TV."""

    def __init__(
        self,
        *,
        fullscreen: bool = True,
        hwdec: str = "auto-safe",
        glsl_shaders: Optional[str] = None,
        fonts_dir: Optional[Path] = None,
        force_4_3: bool = True,
        audio_device: Optional[str] = None,
        extra_options: Optional[dict] = None,
        crt_max_height: int = 0,
    ) -> None:
        try:
            import mpv  # type: ignore
        except ImportError as exc:  # pragma: no cover - only on machines w/o libmpv
            raise RuntimeError(
                "python-mpv/libmpv is not installed. On the Raspberry Pi run "
                "`scripts/install.sh` or `pip install .[pi]` and ensure libmpv "
                "is present (`sudo apt install libmpv2 mpv`)."
            ) from exc

        # Make our bundled retro font discoverable by libass (used for the OSD
        # overlays) by dropping it into mpv's config "fonts" directory.
        if fonts_dir is not None:
            _install_fonts_for_mpv(fonts_dir)

        options = dict(
            # We drive the OSD ourselves, so disable mpv's own on-screen
            # controller and default keybindings.
            osc=False,
            input_default_bindings=False,
            input_vo_keyboard=False,
            # Keep a window alive even with nothing playing so the screen never
            # drops to a console/desktop between episodes or on an empty channel.
            idle="yes",
            force_window="yes",
            # keep-open=yes means a file that reaches its end PAUSES on the last
            # frame and sets the "eof-reached" property instead of silently
            # unloading. We watch that property to roll the next episode. This
            # avoids a nasty race: replacing a file (on a channel change) also
            # fires an "end-file" event for the outgoing file, and its reason is
            # unreliable across mpv versions - reacting to it caused episodes to
            # be skipped or the picture to hang. "eof-reached" only ever trips on
            # a genuine end-of-file, so it is the robust signal.
            keep_open="yes",
            # Preload the next playlist entry while the current one plays. This
            # is what makes channel changes near-instant: during the ~0.5s of
            # static, mpv is already opening/decoding the episode, so it appears
            # the moment the static ends (see play_transition).
            prefetch_playlist="yes",
            fullscreen=fullscreen,
            # Hardware decode + a sensible video output for the Pi. gpu with the
            # drm context works headless on the Pi 4; libmpv falls back sanely.
            hwdec=hwdec,
            # 4:3 shows should be pillarboxed (not stretched) inside the frame.
            keepaspect="yes",
            video_unscaled="no",
            # Hide the mouse cursor - this is a TV, not a computer.
            cursor_autohide="always",
            # A pleasant, readable OSD font size relative to the window.
            osd_font_size=40,
        )
        if audio_device:
            # Force audio to a specific output (e.g. HDMI) instead of mpv's
            # default (which can pick the 3.5mm jack on a Raspberry Pi).
            options["audio_device"] = audio_device
        if glsl_shaders:
            # CRT curvature/rounding/vignette/scanlines. Applied globally (always
            # on) so a newly-loaded episode is never shown for a frame or two
            # without the effect on a channel change.
            options["glsl_shaders"] = glsl_shaders
        if force_4_3:
            # Fit ANY source into a 4:3 raster (letterboxing 16:9 with black
            # bars), so every show - and the static/colour-bar clips - appears in
            # the same 4:3 tube-TV frame. mpv then pillarboxes that 4:3 image on
            # a 16:9 TV, and the CRT shader curves it.
            options["vf"] = (
                "lavfi=[scale=960:720:force_original_aspect_ratio=decrease,"
                "pad=960:720:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1]"
            )
        if extra_options:
            options.update(extra_options)

        self._mpv = mpv.MPV(**options)
        self._closed = False
        # True while a looping filler clip (static / colour bars) is showing, so
        # its (non-)ending never advances the channel.
        self._suppress = True
        self._shader = glsl_shaders or ""
        self._crt_max_height = crt_max_height
        self._crt_applied = self._shader

        @self._mpv.property_observer(_HEIGHT_PROPERTY)
        def _on_height(_name, value):  # pragma: no cover - needs libmpv + media
            self._apply_crt(value)

        @self._mpv.property_observer("eof-reached")
        def _on_eof(_name, value):  # pragma: no cover - needs libmpv + media
            if value and not self._suppress and self.on_end is not None:
                try:
                    self.on_end(END_EOF)
                except Exception:  # noqa: BLE001 - never let a callback kill mpv
                    log.exception("error in on_end (eof) callback")

        @self._mpv.event_callback("end-file")
        def _on_end_file(event):  # pragma: no cover - needs libmpv + media
            # We only care about *errors* here (e.g. a corrupt/missing file) so
            # we can skip to the next episode. Natural ends are handled by the
            # eof-reached observer above; intentional stops/replacements are
            # ignored.
            if self._suppress:
                return
            if _extract_end_reason(event) == END_ERROR and self.on_end is not None:
                try:
                    self.on_end(END_ERROR)
                except Exception:  # noqa: BLE001
                    log.exception("error in on_end (error) callback")

    def _apply_crt(self, height) -> None:  # pragma: no cover - needs libmpv
        """Match the CRT shader to the height of whatever just loaded."""
        wanted = _shader_for_height(self._shader, self._crt_max_height, height)
        if wanted is None or wanted == self._crt_applied:
            return
        try:
            self._mpv.command("set", "glsl-shaders", wanted)
        except Exception:  # noqa: BLE001 - cosmetic; never interrupt playback
            log.debug("could not change glsl-shaders", exc_info=True)
            return
        self._crt_applied = wanted
        log.info("CRT effect %s (source height %s)", "off" if not wanted else "on", height)

    # -- playback -----------------------------------------------------------
    def play(self, path: Path, *, start: float = 0.0) -> None:
        # Enable end detection only for real content.
        self._suppress = False
        try:
            self._mpv.loop_file = "no"
            if start and start > 0:
                # start is an mpv per-file option; +N seeks N seconds in.
                self._mpv.loadfile(str(path), "replace", start=f"+{start:.3f}")
            else:
                self._mpv.loadfile(str(path), "replace")
            self._mpv.pause = False  # keep-open can leave us paused; force play
        except Exception:  # noqa: BLE001
            log.exception("failed to play %s", path)
            if self.on_end is not None:
                self.on_end(END_ERROR)

    def play_loop(self, path: Path) -> None:
        self._suppress = True  # a looping clip should never trigger "next"
        try:
            self._mpv.loop_file = "inf"
            self._mpv.loadfile(str(path), "replace")
            self._mpv.pause = False
        except Exception:  # noqa: BLE001
            log.exception("failed to loop %s", path)

    def play_transition(
        self,
        static_path: Path,
        target_path: Path,
        *,
        start: float = 0.0,
        static_seconds: float = 0.5,
    ) -> None:
        # Build a 2-entry playlist: [static (cut to static_seconds), episode].
        # mpv plays the static burst and, thanks to prefetch-playlist, has the
        # episode ready to show the instant the static ends. keep-open=yes only
        # holds the LAST entry, so eof-reached (which advances the channel) only
        # ever trips for the episode - never the static.
        self._suppress = False
        try:
            self._mpv.loop_file = "no"
            self._mpv.loadfile(
                str(static_path), "replace", end=f"{max(0.05, static_seconds):.3f}"
            )
            if start and start > 0:
                self._mpv.loadfile(str(target_path), "append", start=f"+{start:.3f}")
            else:
                self._mpv.loadfile(str(target_path), "append")
            self._mpv.pause = False
        except Exception:  # noqa: BLE001
            log.exception("failed transition to %s", target_path)
            self.play(target_path, start=start)

    def preload_next(self, target_path: Path, *, start: float = 0.0) -> None:
        # Keep the currently-playing item on screen and append the target as a
        # second playlist entry. With prefetch-playlist=yes, mpv opens/decodes it
        # in the background while the current show keeps playing, so commit_switch
        # can cut over near-instantly (no frozen frame).
        self._suppress = True  # ignore the outgoing show's own eof during the bridge
        try:
            self._mpv.command("playlist-clear")  # drop any earlier pending append
            if start and start > 0:
                self._mpv.loadfile(str(target_path), "append", start=f"+{start:.3f}")
            else:
                self._mpv.loadfile(str(target_path), "append")
        except Exception:  # noqa: BLE001
            log.exception("failed to preload %s", target_path)
            self.play(target_path, start=start)

    def commit_switch(self) -> None:
        self._suppress = False
        try:
            self._mpv.command("playlist-next", "force")  # jump to the prefetched item
            self._mpv.command("playlist-clear")          # keep only the new current
            self._mpv.pause = False
        except Exception:  # noqa: BLE001
            log.debug("commit_switch failed", exc_info=True)

    def stop(self) -> None:
        self._suppress = True
        try:
            self._mpv.command("stop")
        except Exception:  # noqa: BLE001 - stopping should never crash us
            log.debug("mpv stop failed", exc_info=True)

    # -- audio --------------------------------------------------------------
    def set_volume(self, volume: int) -> None:
        try:
            self._mpv.volume = max(0, min(100, int(volume)))
        except Exception:  # noqa: BLE001
            log.debug("could not set volume", exc_info=True)

    def set_mute(self, muted: bool) -> None:
        try:
            self._mpv.mute = bool(muted)
        except Exception:  # noqa: BLE001
            log.debug("could not set mute", exc_info=True)

    def get_time_pos(self) -> Optional[float]:
        try:
            pos = self._mpv.time_pos
            return float(pos) if pos is not None else None
        except Exception:  # noqa: BLE001
            return None

    # -- OSD ----------------------------------------------------------------
    def show_text(self, text: str, duration: float) -> None:
        try:
            self._mpv.command("show-text", text, int(duration * 1000))
        except Exception:  # noqa: BLE001
            log.debug("show-text failed", exc_info=True)

    def set_overlay(self, overlay_id: int, ass: str, res_x: int, res_y: int) -> None:
        try:
            # osd-overlay positional args: id, format, data, res_x, res_y.
            # (Trailing z/hidden/compute_bounds use their defaults.)
            self._mpv.command(
                "osd-overlay", overlay_id, "ass-events", ass, res_x, res_y
            )
        except Exception:  # noqa: BLE001
            # Fall back to a plain message so the viewer still gets feedback.
            log.debug("osd-overlay failed, falling back to show-text", exc_info=True)
            self.show_text(_strip_ass(ass), 3.0)

    def clear_overlay(self, overlay_id: int) -> None:
        try:
            self._mpv.command("osd-overlay", overlay_id, "none", "")
        except Exception:  # noqa: BLE001
            log.debug("clearing overlay failed", exc_info=True)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._mpv.terminate()
        except Exception:  # noqa: BLE001
            log.debug("mpv terminate failed", exc_info=True)


class MockPlayer(Player):
    """A headless stand-in that records commands - for tests and dev mode."""

    def __init__(self, *, verbose: bool = False) -> None:
        self.verbose = verbose
        self.current: Optional[Path] = None
        self.looping: Optional[Path] = None
        self.volume: int = 0
        self.muted: bool = False
        self.time_pos: float = 0.0
        self.closed = False
        # Recorded history, handy for assertions in tests.
        self.played: List[Tuple[Path, float]] = []
        self.transitions: List[Tuple[Path, Path, float]] = []
        self.preloaded: Optional[Tuple[Path, float]] = None
        self.messages: List[Tuple[str, float]] = []
        self.overlays: dict[int, str] = {}
        self.stops = 0

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"[player] {msg}")

    def play(self, path: Path, *, start: float = 0.0) -> None:
        self.current = path
        self.looping = None
        self.time_pos = start
        self.played.append((path, start))
        self._log(f"PLAY {path} @ {start:.1f}s")

    def play_loop(self, path: Path) -> None:
        self.looping = path
        self.current = path
        self._log(f"LOOP {path}")

    def play_transition(
        self,
        static_path: Path,
        target_path: Path,
        *,
        start: float = 0.0,
        static_seconds: float = 0.5,
    ) -> None:
        self.transitions.append((static_path, target_path, start))
        # The episode is what ends up playing (static is momentary).
        self.current = target_path
        self.looping = None
        self.time_pos = start
        self.played.append((target_path, start))
        self._log(f"TRANSITION static={static_path} -> {target_path} @ {start:.1f}s")

    def preload_next(self, target_path: Path, *, start: float = 0.0) -> None:
        # The current item keeps "playing"; the target is queued, not shown yet.
        self.preloaded = (target_path, start)
        self._log(f"PRELOAD {target_path} @ {start:.1f}s (current keeps playing)")

    def commit_switch(self) -> None:
        if self.preloaded is None:
            return
        target, start = self.preloaded
        self.preloaded = None
        self.current = target
        self.looping = None
        self.time_pos = start
        self.played.append((target, start))
        self._log(f"COMMIT SWITCH -> {target} @ {start:.1f}s")

    def stop(self) -> None:
        self.current = None
        self.looping = None
        self.preloaded = None
        self.stops += 1
        self._log("STOP")

    def set_volume(self, volume: int) -> None:
        self.volume = max(0, min(100, int(volume)))
        self._log(f"VOLUME {self.volume}")

    def set_mute(self, muted: bool) -> None:
        self.muted = bool(muted)
        self._log(f"MUTE {self.muted}")

    def get_time_pos(self) -> Optional[float]:
        return self.time_pos if self.current is not None else None

    def show_text(self, text: str, duration: float) -> None:
        self.messages.append((text, duration))
        self._log(f"TEXT {text!r} ({duration}s)")

    def set_overlay(self, overlay_id: int, ass: str, res_x: int, res_y: int) -> None:
        self.overlays[overlay_id] = ass
        self._log(f"OVERLAY {overlay_id}")

    def clear_overlay(self, overlay_id: int) -> None:
        self.overlays.pop(overlay_id, None)
        self._log(f"CLEAR OVERLAY {overlay_id}")

    def close(self) -> None:
        self.closed = True
        self._log("CLOSE")

    # -- test/dev helper ----------------------------------------------------
    def finish_current(self, reason: str = END_EOF) -> None:
        """Simulate the current episode ending, triggering ``on_end``."""
        self.current = None
        if self.on_end is not None:
            self.on_end(reason)


def _extract_end_reason(event) -> str:  # pragma: no cover - libmpv specific
    """Normalise the many shapes of a python-mpv end-file event into a reason."""
    reason = None
    try:
        data = getattr(event, "data", event)
        if isinstance(data, dict):
            reason = data.get("reason")
        else:
            reason = getattr(data, "reason", None)
    except Exception:  # noqa: BLE001
        reason = None
    reason = str(reason).lower() if reason is not None else ""
    if "eof" in reason:
        return END_EOF
    if "error" in reason:
        return END_ERROR
    if "stop" in reason or "quit" in reason:
        return END_STOPPED
    # Unknown/redirect reasons: treat as a natural end so the channel keeps going.
    return END_EOF


def _install_fonts_for_mpv(fonts_dir: Path) -> None:
    """Copy bundled .ttf fonts into mpv's config 'fonts' dir so libass finds them.

    mpv automatically loads any fonts placed in ``<mpv config dir>/fonts``, which
    is the most reliable way to make our retro OSD font available to the ASS
    overlays without touching the system-wide fontconfig setup.
    """
    import os
    import shutil

    if not fonts_dir.is_dir():
        return
    config_home = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    dest = Path(config_home) / "mpv" / "fonts"
    try:
        dest.mkdir(parents=True, exist_ok=True)
        for ttf in fonts_dir.glob("*.ttf"):
            target = dest / ttf.name
            if not target.exists():
                shutil.copy2(ttf, target)
    except OSError:
        log.debug("could not install bundled fonts for mpv", exc_info=True)


def _strip_ass(ass: str) -> str:  # pragma: no cover - trivial
    """Very small ASS-tag stripper for the show-text fallback path."""
    import re

    text = re.sub(r"\{[^}]*\}", "", ass)
    text = text.replace("\\N", " ").replace("\\n", " ")
    return text.strip()



class MpvIpcPlayer(Player):
    """A :class:`Player` that drives the ``mpv`` **binary** over its JSON IPC socket.

    Why this exists: on macOS, libmpv (which :class:`MpvPlayer` uses) cannot open
    a window from a plain Python process. It needs a Cocoa event loop running on
    the main thread, which a CLI Python script does not provide - the symptom is
    audio playing with no picture at all. The real ``mpv`` binary runs its own
    event loop, so spawning it and talking to it over a unix socket works
    everywhere, at the cost of one extra process.

    Two deliberate simplifications versus :class:`MpvPlayer`:

    * ``play_transition`` falls back to the base-class behaviour (cut straight to
      the episode, no static burst), since per-file options in ``loadfile``
      changed shape across mpv versions and this backend is for dev machines.
    * playback position comes from an observed ``time-pos`` property rather than
      a synchronous query, so it is up to a moment stale - which is fine for the
      resume feature's purposes.
    """

    def __init__(
        self,
        *,
        fullscreen: bool = True,
        hwdec: str = "auto-safe",
        glsl_shaders: Optional[str] = None,
        fonts_dir: Optional[Path] = None,
        force_4_3: bool = True,
        audio_device: Optional[str] = None,
        extra_options: Optional[dict] = None,
        mpv_binary: str = "mpv",
        connect_timeout: float = 10.0,
        window_keys: Optional[Dict[str, str]] = None,
        crt_max_height: int = 0,
    ) -> None:
        import shutil
        import socket
        import subprocess
        import tempfile
        import threading

        if shutil.which(mpv_binary) is None:
            raise RuntimeError(
                f"the '{mpv_binary}' binary was not found. Install it with "
                "`brew install mpv` (macOS) or `sudo apt install mpv` (Linux)."
            )
        if fonts_dir is not None:
            _install_fonts_for_mpv(fonts_dir)

        self._sock_dir = tempfile.mkdtemp(prefix="timewarptv-mpv-")
        self._socket_path = str(Path(self._sock_dir) / "mpv.sock")

        args = [
            mpv_binary,
            f"--input-ipc-server={self._socket_path}",
            # Same behaviour as the libmpv backend - see MpvPlayer for why each
            # of these matters (keep-open in particular is what makes
            # "eof-reached" the reliable end-of-episode signal).
            "--idle=yes",
            "--force-window=yes",
            "--keep-open=yes",
            "--prefetch-playlist=yes",
            "--osc=no",
            "--input-default-bindings=no",
            # Keys typed at the video window are wanted (see _bind_window_keys),
            # but only ours: mpv's own bindings stay off, so 'q' does not close
            # the window behind the application's back.
            f"--input-vo-keyboard={'yes' if window_keys else 'no'}",
            # The terminal belongs to the application (its stdin backend reads
            # the dev keyboard there). Leave it alone: two readers on one
            # terminal race for every keystroke.
            "--input-terminal=no",
            "--keepaspect=yes",
            "--video-unscaled=no",
            "--cursor-autohide=always",
            "--osd-font-size=40",
            f"--hwdec={hwdec}",
            f"--fullscreen={'yes' if fullscreen else 'no'}",
        ]
        if audio_device:
            args.append(f"--audio-device={audio_device}")
        if glsl_shaders:
            args.append(f"--glsl-shaders={glsl_shaders}")
        if force_4_3:
            args.append(
                "--vf=lavfi=[scale=960:720:force_original_aspect_ratio=decrease,"
                "pad=960:720:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1]"
            )
        for key, value in (extra_options or {}).items():
            args.append(f"--{str(key).replace('_', '-')}={value}")

        log.info("starting mpv: %s", " ".join(args))
        self._proc = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        self._sock = self._connect(socket, connect_timeout)
        self._closed = False
        self._suppress = True
        self._time_pos: Optional[float] = None
        self._send_lock = threading.Lock()
        self._buffer = b""

        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

        self._shader = glsl_shaders or ""
        self._crt_max_height = crt_max_height
        self._crt_applied = self._shader

        # Property ids are arbitrary; we only ever match on the property name.
        self._command("observe_property", 1, "eof-reached")
        self._command("observe_property", 2, "time-pos")
        self._command("observe_property", 3, _HEIGHT_PROPERTY)
        self._bind_window_keys(window_keys)

    # -- plumbing -----------------------------------------------------------
    def _connect(self, socket_mod, timeout: float):
        import time as _time

        deadline = _time.monotonic() + timeout
        while _time.monotonic() < deadline:
            if self._proc.poll() is not None:
                raise RuntimeError(
                    f"mpv exited (code {self._proc.returncode}) before its IPC "
                    "socket appeared"
                )
            try:
                sock = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
                sock.connect(self._socket_path)
                return sock
            except OSError:
                _time.sleep(0.05)
        self._proc.terminate()
        raise RuntimeError(
            f"timed out waiting for mpv's IPC socket at {self._socket_path}"
        )

    def _apply_crt(self, height) -> None:
        """Match the CRT shader to the height of whatever just loaded."""
        wanted = _shader_for_height(self._shader, self._crt_max_height, height)
        if wanted is None or wanted == self._crt_applied:
            return
        self._command("set_property", "glsl-shaders", wanted)
        self._crt_applied = wanted
        log.info("CRT effect %s (source height %s)", "off" if not wanted else "on", height)

    def _bind_window_keys(self, window_keys: Optional[Dict[str, str]]) -> None:
        """Make the video window report its keystrokes back over the IPC socket.

        Each key is bound to ``script-message timewarptv <action>``, which mpv
        echoes to us as a ``client-message`` event. That is what makes the
        remote keys work while the *video* window has focus rather than the
        terminal - on a laptop the video window is the one you naturally click.
        """
        for key, action in (window_keys or {}).items():
            self._command(
                "keybind", key, f"script-message {_WINDOW_KEY_PREFIX} {action}"
            )

    def _command(self, *args) -> None:
        """Fire a command at mpv. Errors are logged, never raised at the caller."""
        import json

        if self._closed:
            return
        payload = json.dumps({"command": list(args)}) + "\n"
        try:
            with self._send_lock:
                self._sock.sendall(payload.encode("utf-8"))
        except OSError:
            log.debug("mpv IPC send failed: %s", args, exc_info=True)

    def _set(self, prop: str, value) -> None:
        self._command("set_property", prop, value)

    def _read_loop(self) -> None:  # pragma: no cover - needs a live mpv
        import json

        while not self._closed:
            try:
                chunk = self._sock.recv(65536)
            except OSError:
                break
            if not chunk:
                break
            self._buffer += chunk
            while b"\n" in self._buffer:
                line, self._buffer = self._buffer.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line.decode("utf-8", "replace"))
                except ValueError:
                    continue
                self._handle_message(msg)

    def _handle_message(self, msg: dict) -> None:  # pragma: no cover - live mpv
        event = msg.get("event")
        if event == "client-message":
            args = msg.get("args") or []
            if len(args) >= 2 and args[0] == _WINDOW_KEY_PREFIX and self.on_key:
                try:
                    self.on_key(str(args[1]))
                except Exception:  # noqa: BLE001 - never let a callback kill us
                    log.exception("error in on_key callback")
            return
        if event != "property-change":
            return
        name, data = msg.get("name"), msg.get("data")
        if name == _HEIGHT_PROPERTY:
            self._apply_crt(data)
            return
        if name == "time-pos":
            self._time_pos = float(data) if isinstance(data, (int, float)) else None
        elif name == "eof-reached":
            if data and not self._suppress and self.on_end is not None:
                try:
                    self.on_end(END_EOF)
                except Exception:  # noqa: BLE001 - never let a callback kill us
                    log.exception("error in on_end (eof) callback")

    def _apply_start(self, start: float) -> None:
        """Set the start offset applied to the *next* file mpv loads.

        Done as a property rather than a per-file loadfile option because the
        shape of loadfile's options argument changed across mpv versions.
        """
        self._set("start", f"+{start:.3f}" if start and start > 0 else "0")

    # -- playback -----------------------------------------------------------
    def play(self, path: Path, *, start: float = 0.0) -> None:
        self._suppress = False
        self._set("loop-file", "no")
        self._apply_start(start)
        self._command("loadfile", str(path), "replace")
        self._set("pause", False)

    def play_loop(self, path: Path) -> None:
        self._suppress = True  # a looping clip should never trigger "next"
        self._set("loop-file", "inf")
        self._apply_start(0.0)
        self._command("loadfile", str(path), "replace")
        self._set("pause", False)

    def preload_next(self, target_path: Path, *, start: float = 0.0) -> None:
        # Keep the current item on screen; queue the target as a second entry.
        self._suppress = True  # ignore the outgoing show's eof during the bridge
        self._set("loop-file", "no")
        self._command("playlist-clear")
        self._apply_start(start)
        self._command("loadfile", str(target_path), "append")

    def commit_switch(self) -> None:
        self._suppress = False
        self._command("playlist-next", "force")
        self._command("playlist-clear")
        self._set("pause", False)

    def stop(self) -> None:
        self._suppress = True
        self._command("stop")

    # -- audio --------------------------------------------------------------
    def set_volume(self, volume: int) -> None:
        self._set("volume", max(0, min(100, int(volume))))

    def set_mute(self, muted: bool) -> None:
        self._set("mute", bool(muted))

    def get_time_pos(self) -> Optional[float]:
        return self._time_pos

    # -- OSD ----------------------------------------------------------------
    def show_text(self, text: str, duration: float) -> None:
        self._command("show-text", text, int(duration * 1000))

    def set_overlay(self, overlay_id: int, ass: str, res_x: int, res_y: int) -> None:
        self._command("osd-overlay", overlay_id, "ass-events", ass, res_x, res_y)

    def clear_overlay(self, overlay_id: int) -> None:
        self._command("osd-overlay", overlay_id, "none", "")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._command("quit")
        except Exception:  # noqa: BLE001
            pass
        try:
            self._sock.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._proc.wait(timeout=3)
        except Exception:  # noqa: BLE001
            try:
                self._proc.kill()
            except Exception:  # noqa: BLE001
                pass
        import shutil

        shutil.rmtree(self._sock_dir, ignore_errors=True)


def default_player_backend() -> str:
    """Which real player backend suits this machine: ``"ipc"`` or ``"libmpv"``.

    macOS gets the subprocess/IPC backend because libmpv cannot open its own
    window from a plain Python process there (audio plays, no picture). Every
    other platform uses libmpv, which needs no extra process.
    """
    return "ipc" if sys.platform == "darwin" else "libmpv"


def create_player(
    backend: str = "auto",
    *,
    window_keys: Optional[Dict[str, str]] = None,
    **kwargs,
) -> Player:
    """Build the real player for ``backend`` (``auto``/``libmpv``/``ipc``).

    ``window_keys`` (mpv key name -> action name) makes the video window accept
    the remote keys itself. Only the IPC backend can do this; libmpv shares the
    process with the evdev input backend the Pi uses, where taking the same key
    twice would double every button press.
    """
    chosen = default_player_backend() if backend == "auto" else backend
    if chosen == "ipc":
        return MpvIpcPlayer(window_keys=window_keys, **kwargs)
    if chosen == "libmpv":
        return MpvPlayer(**kwargs)
    raise ValueError(f"unknown player backend: {backend!r}")

__all__ = [
    "Player",
    "MpvPlayer",
    "MpvIpcPlayer",
    "MockPlayer",
    "create_player",
    "default_player_backend",
    "END_EOF",
    "END_ERROR",
    "END_STOPPED",
]
