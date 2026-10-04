"""The television itself: the state machine that ties everything together.

:class:`TVApp` owns the channel lineup, the player, the overlays and the input
queue, and turns remote-control actions into TV behaviour: changing channels
(with a burst of static and a channel banner), adjusting and muting the volume,
direct channel entry by number, an info banner, a "last channel" jump, and a
standby/off mode. When an episode ends it automatically rolls into the next one
on that channel's shuffle, so the box never stops "broadcasting".

The class is written to be testable without a display: pass it a
:class:`~timewarptv.player.MockPlayer` and a fake clock and you can single-step
the whole thing (see ``step`` / ``handle_event`` / ``process_pending``).
"""

from __future__ import annotations

import logging
import queue
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

from .actions import Action, InputEvent
from .channel import Channel, ChannelLineup, PlayRequest, build_lineup
from .config import Config
from .input.keymap import MPV_WINDOW_KEYS, action_from_name
from .input.manager import InputManager, create_backends
from .guide_gen import GUIDE_FILENAME
from .media_watch import MediaWatch
from .overlay import OverlayManager
from .player import END_EOF, END_ERROR, MockPlayer, Player
from .state import save_state
from .titles import NowPlaying, describe
from .static_gen import (
    COLORBARS_FILENAME,
    DEFAULT_ASSETS_DIR,
    GLITCH_FILENAME,
    STATIC_FILENAME,
)

# How long the "RESUMING - PRESS OK TO START OVER" banner stays live and
# listening for an OK/ENTER press before it just... resumes.
RESUME_OFFER_SECONDS = 6.0

# The power-off press (volume-down at zero) must come at least this long after
# the previous volume-down, so a held, auto-repeating button can't trigger it.
POWER_OFF_PAUSE_SECONDS = 1.0

log = logging.getLogger(__name__)


class TVApp:
    """The retro-TV application state machine."""

    def __init__(
        self,
        config: Config,
        player: Player,
        input_manager: InputManager,
        *,
        overlay: Optional[OverlayManager] = None,
        clock: Callable[[], float] = time.monotonic,
        assets_dir: Optional[Path] = None,
        media_watch: Optional[MediaWatch] = None,
    ) -> None:
        self.config = config
        self.player = player
        self.input = input_manager
        # Set when running as an appliance (--hotplug): stop, and let systemd
        # restart us with a fresh scan, when the drive goes away or changes.
        self._media_watch = media_watch
        self.overlay = overlay or OverlayManager(player, config, clock=clock)
        self._clock = clock

        self.lineup: ChannelLineup = build_lineup(config)

        # Runtime state.
        self.volume = config.initial_volume
        self.muted = False
        self._last_volume_down = float("-inf")
        # When the current still screen (guide, lock screen, colour bars) started
        # being looked at without a button press; None while the picture moves.
        self._still_since: Optional[float] = None
        self.standby = False
        self.powered_off = False
        self._playing_path: Optional[Path] = None
        self._playing_is_break = False
        self._last_channel_number: Optional[int] = None
        self._running = False

        # Direct channel entry ("type 1 then 2 -> channel 12").
        self._digit_buffer = ""
        self._digit_deadline = 0.0
        self._digit_entry_timeout = 2.0

        # Passcode entry for a locked channel. None = not currently entering a
        # code; "" or more = the digits typed so far. While this is set, DIGIT
        # and ENTER events go here instead of the channel-number entry above.
        self._code_buffer: Optional[str] = None
        # The digit currently showing on the lock screen's dial. A remote with
        # no number pad (the Argon) turns it with ◀ / ▶ and locks it in with OK.
        self._dial = 0

        # The "RESUMING - PRESS OK TO START OVER" banner window: an ENTER press
        # before this deadline discards the resume position and starts fresh.
        self._resume_offer_until: Optional[float] = None

        # Pending "bridge" switch: keep the old show playing until this deadline,
        # then cut to the channel that was preloaded. The channel banner is shown
        # at the moment of the cut-over, not when the button is pressed.
        self._switch_deadline: Optional[float] = None
        # (number, name, show_channel_bug keyword arguments)
        self._pending_banner: Optional[tuple[int, str, dict]] = None

        # Playback-finished events from the player (may arrive on any thread).
        self._ended: "queue.Queue[str]" = queue.Queue()
        self.player.on_end = self._ended.put
        # Keys pressed in the player's own video window arrive the same way as
        # any other remote: on the input queue. Players without a window of
        # their own simply never call this.
        self.player.on_key = self._on_window_key

        # Filler assets.
        self._assets_dir = assets_dir or config.assets_dir or DEFAULT_ASSETS_DIR
        self._colorbars_path = self._resolve_asset(COLORBARS_FILENAME)
        # The channel-change transition clip depends on the configured effect.
        self._transition_path = self._resolve_transition_asset()

    # -- construction -------------------------------------------------------
    @classmethod
    def from_config(
        cls,
        config: Config,
        *,
        player: Optional[Player] = None,
        input_manager: Optional[InputManager] = None,
        dry_run: bool = False,
        assets_dir: Optional[Path] = None,
        media_watch: Optional[MediaWatch] = None,
    ) -> "TVApp":
        """Build a fully wired app, creating real hardware backends by default.

        ``dry_run`` swaps in a :class:`MockPlayer` and disables all real input
        backends (a stdin backend is added if a TTY is available), which is how
        the box can be exercised on a development machine.
        """
        if input_manager is None:
            if dry_run:
                backends = create_backends({"keyboard": False, "cec": False, "stdin": True})
            else:
                backends = create_backends(config.input_options)
            input_manager = InputManager(backends)

        if player is None:
            if dry_run:
                player = MockPlayer(verbose=True)
            else:
                from .crt import write_shader
                from .player import create_player

                assets = assets_dir or config.assets_dir or DEFAULT_ASSETS_DIR
                shader_path = write_shader(config.crt)
                player = create_player(
                    config.player_backend,
                    fullscreen=config.fullscreen,
                    hwdec=config.hwdec,
                    glsl_shaders=str(shader_path) if shader_path else None,
                    fonts_dir=assets / "fonts",
                    force_4_3=config.force_4_3,
                    audio_device=config.audio_device,
                    window_keys=_window_keys_for(input_manager),
                    crt_max_height=config.crt.max_height,
                )

        return cls(
            config, player, input_manager, assets_dir=assets_dir, media_watch=media_watch
        )

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        """Power on: set volume, start input, and tune to the first channel."""
        self.player.set_volume(self.volume)
        self.player.set_mute(self.muted)
        self.input.start()
        self._select_start_channel()
        self.tune_current(show_static=False)

    def run(self) -> None:
        """Run the blocking main loop until a QUIT action is received."""
        self.start()
        self._running = True
        log.info("TimewarpTV is on the air. %d channels.", len(self.lineup))
        try:
            while self._running:
                # Faster while something is animating (the standby screensaver).
                self.step(block=True, timeout=self.overlay.frame_interval or 0.1)
        except KeyboardInterrupt:  # pragma: no cover - interactive convenience
            log.info("interrupted; shutting down")
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        self._running = False
        self._remember_position()
        try:
            self.overlay.clear_all()
        except Exception:  # noqa: BLE001
            pass
        self.input.stop()
        self.player.close()

    # -- main-loop step (small and testable) --------------------------------
    def step(self, *, block: bool = False, timeout: float = 0.1) -> None:
        """Advance the state machine by one iteration.

        Handles overlay expiry, channel-entry timeouts, finished episodes, and
        at most one queued input event.
        """
        now = self._clock()
        if self._media_watch is not None:
            reason = self._media_watch.changed()
            if reason is not None:
                log.info("media changed: %s - stopping so a restart re-indexes", reason)
                self._running = False
                return
        self.overlay.tick()
        self._maybe_commit_switch(now)
        self._maybe_commit_digits(now)
        self._maybe_expire_resume_offer(now)
        self._maybe_idle_standby(now)
        self._drain_playback_events()

        event = self.input.get(timeout=timeout if block else 0.0)
        if event is not None:
            self.handle_event(event)

    def _maybe_commit_switch(self, now: float) -> None:
        """Cut over to the preloaded channel once the bridge window has elapsed."""
        if self._switch_deadline is not None and now >= self._switch_deadline:
            self._switch_deadline = None
            self.player.commit_switch()
            # Flash the channel banner right as the picture actually changes.
            if self._pending_banner is not None:
                number, name, banner = self._pending_banner
                self.overlay.show_channel_bug(number, name, **banner)
                self._pending_banner = None

    def _maybe_expire_resume_offer(self, now: float) -> None:
        if self._resume_offer_until is not None and now >= self._resume_offer_until:
            self._resume_offer_until = None

    def _on_window_key(self, action_name: str) -> None:
        """Turn a key from the player's video window into a queued input event."""
        try:
            event = action_from_name(action_name)
        except ValueError:
            log.debug("ignoring unknown window key action %r", action_name)
            return
        if event is not None:
            self.input.put(event)

    # -- input handling -----------------------------------------------------
    def handle_event(self, event: InputEvent) -> None:
        action = event.action
        self._still_since = None  # someone's here: restart the burn-in count

        if action == Action.QUIT:
            self._running = False
            return
        if action == Action.POWER:
            self._toggle_standby()
            return

        # While in standby, ignore everything except POWER/QUIT (handled above).
        if self.standby:
            return

        # A locked channel's lock screen is showing: it's a combination lock.
        # ◀ / ▶ turn the dial, OK locks in its digit; typed digits (keyboard,
        # TV remote over CEC) go straight in. Channel-nav cancels entry and lets
        # the surf-away happen normally (falls through below).
        if self._code_buffer is not None:
            if action == Action.DIGIT:
                self._push_code_digit(event.value or 0)
                return
            if action == Action.ENTER:
                self._push_code_digit(self._dial)
                return
            if action in (Action.NEXT_EPISODE, Action.PREVIOUS_EPISODE):
                self._turn_dial(1 if action == Action.NEXT_EPISODE else -1)
                return
            if action in (Action.CHANNEL_UP, Action.CHANNEL_DOWN, Action.LAST_CHANNEL):
                self._code_buffer = None

        # The "RESUMING..." banner is up and listening for an OK press.
        if action == Action.ENTER and self._resume_offer_until is not None:
            self._resume_offer_until = None
            self._restart_without_resume()
            return

        handlers = {
            Action.CHANNEL_UP: self._channel_up,
            Action.CHANNEL_DOWN: self._channel_down,
            Action.VOLUME_UP: self._volume_up,
            Action.VOLUME_DOWN: self._volume_down,
            Action.MUTE: self._toggle_mute,
            Action.INFO: self._show_info,
            Action.LAST_CHANNEL: self._jump_last_channel,
            Action.HOME: self._jump_home,
            Action.NEXT_EPISODE: lambda: self._skip_episode(forward=True),
            Action.PREVIOUS_EPISODE: lambda: self._skip_episode(forward=False),
            Action.ENTER: self._confirm_digits,
        }
        if action == Action.DIGIT:
            self._push_digit(event.value or 0)
        else:
            handler = handlers.get(action)
            if handler is not None:
                handler()

    # -- channel changing ---------------------------------------------------
    def _channel_up(self) -> None:
        self._remember_position()
        self._last_channel_number = self.lineup.current.number
        self.lineup.up()
        self.tune_current()

    def _channel_down(self) -> None:
        self._remember_position()
        self._last_channel_number = self.lineup.current.number
        self.lineup.down()
        self.tune_current()

    def _jump_last_channel(self) -> None:
        if self._last_channel_number is None:
            return
        target = self._last_channel_number
        if not self.lineup.has_number(target):
            return
        self._remember_position()
        self._last_channel_number = self.lineup.current.number
        self.lineup.select_number(target)
        self.tune_current()

    def _jump_home(self) -> None:
        """The remote's HOME button: go straight to the welcome / guide channel.

        Falls back to the channel the box boots on, so a config that only sets
        ``start_channel`` still has a working HOME button.
        """
        target = self.config.home_channel
        if target is None:
            target = self.config.start_channel
        if target is None:
            return
        self.select_channel_number(target)

    def select_channel_number(self, number: int) -> bool:
        """Tune directly to a channel number. Returns False if it doesn't exist."""
        if not self.lineup.has_number(number):
            self.overlay.show_message(f"CH {number:02d}  -  NO CHANNEL")
            return False
        if number == self.lineup.current.number:
            self._show_info()
            return True
        self._remember_position()
        self._last_channel_number = self.lineup.current.number
        self.lineup.select_number(number)
        self.tune_current()
        return True

    def tune_current(self, *, show_static: bool = True) -> None:
        """Tune into the currently selected channel."""
        channel = self.lineup.current
        self.overlay.clear_standby()
        # Whatever was up in the middle of the screen belonged to the channel
        # we're leaving - above all a lock screen, which never times out.
        self.overlay.clear_message()
        self._code_buffer = None
        self._resume_offer_until = None
        # Only the channel on screen can be unlocked: leaving a locked channel
        # locks it again, so coming back always asks for the code.
        for other in self.lineup:
            if other is not channel:
                other.relock()

        request = channel.tune_in()
        self._pending_banner = None

        if request is None:
            # No episodes, or the channel is passcode-locked: show a slate.
            self.overlay.show_channel_bug(channel.number, channel.name)
            self._show_no_signal(channel)
            return

        subtitle: Optional[str] = None
        if request.resumed:
            subtitle = "RESUMING - PRESS OK TO START OVER"
            self._resume_offer_until = self._clock() + RESUME_OFFER_SECONDS

        self._present(channel, request, show_static=show_static, subtitle=subtitle)

    def _skip_episode(self, *, forward: bool) -> None:
        """The remote's ◀ / ▶: the previous or next episode on this channel.

        Presented exactly like a channel change - same bridge or transition, same
        banner - so it feels like the same television. Anything the channel
        can't skip (locked, empty, broadcast, a lone episode) is a quiet no-op.
        """
        channel = self.lineup.current
        request = channel.skip() if forward else channel.back()
        if request is None:
            return
        self._resume_offer_until = None
        self._pending_banner = None
        self._present(channel, request)

    def _present(
        self,
        channel: Channel,
        request: PlayRequest,
        *,
        show_static: bool = True,
        subtitle: Optional[str] = None,
    ) -> None:
        """Put ``request`` on screen, using the configured changeover style."""
        banner = {"subtitle": subtitle, "caption": self._caption_for(channel, request)}
        if not show_static:
            # Not a channel change (first tune / waking from standby): play now.
            self._switch_deadline = None
            self.overlay.show_channel_bug(channel.number, channel.name, **banner)
            self._play_request(request)
        elif self._transition_path is not None:
            # Transition clip (glitch/static) + preloaded episode.
            self._switch_deadline = None
            self.overlay.show_channel_bug(channel.number, channel.name, **banner)
            self._playing_path = request.path
            self._playing_is_break = request.is_break
            self.player.play_transition(
                self._transition_path,
                request.path,
                start=request.start,
                static_seconds=self.config.transition_duration,
            )
        elif self.config.bridge_seconds > 0 and self._playing_path is not None:
            # No transition effect: keep the current show playing while the next
            # channel preloads, then cut over (no frozen frame). The banner is
            # shown at the cut-over (see _maybe_commit_switch), not right now.
            self._playing_path = request.path
            self._playing_is_break = request.is_break
            self.player.preload_next(request.path, start=request.start)
            self._switch_deadline = self._clock() + self.config.bridge_seconds
            self._pending_banner = (channel.number, channel.name, banner)
        else:
            self._switch_deadline = None
            self.overlay.show_channel_bug(channel.number, channel.name, **banner)
            self._play_request(request)

    def _play_request(self, request: PlayRequest) -> None:
        self._playing_path = request.path
        self._playing_is_break = request.is_break
        self.player.play(request.path, start=request.start)

    def _show_no_signal(self, channel: Channel) -> None:
        self._switch_deadline = None
        self._pending_banner = None
        self._playing_path = None
        self._playing_is_break = False
        if self._colorbars_path is not None:
            self.player.play_loop(self._colorbars_path)
        else:
            self.player.stop()
        if channel.locked:
            self._code_buffer = ""
            self._dial = 0
            self._show_lock_screen()
        else:
            self.overlay.show_message(
                f"CH {channel.number:02d}  {channel.name}  -  NO SIGNAL", duration=6.0
            )

    # -- volume -------------------------------------------------------------
    def _volume_up(self) -> None:
        self._set_volume(self.volume + self.config.volume_step, unmute=True)

    def _volume_down(self) -> None:
        # One press below zero cleanly powers off the box (safe to unplug) - but
        # only a deliberate press, made after a pause. Volume keys auto-repeat
        # when held, and without the pause, holding Vol- from 70 ran through
        # zero and shut the box down in about a second.
        now = self._clock()
        previous, self._last_volume_down = self._last_volume_down, now
        if (
            self.config.power_off_on_min_volume
            and not self.muted
            and self.volume <= 0
            and now - previous >= POWER_OFF_PAUSE_SECONDS
        ):
            self._power_off()
            return
        self._set_volume(self.volume - self.config.volume_step, unmute=True)

    def _set_volume(self, value: int, *, unmute: bool = False) -> None:
        self.volume = max(0, min(100, value))
        if unmute and self.muted:
            self.muted = False
            self.player.set_mute(False)
        self.player.set_volume(self.volume)
        self.overlay.show_volume(self.volume, self.muted)

    def _power_off(self) -> None:
        """Cleanly shut the Pi down so it's safe to unplug."""
        log.info("powering off (volume floor)")
        self.powered_off = True
        self._switch_deadline = None
        self._pending_banner = None
        self._relock_all()
        self._save_state()
        try:
            self.overlay.clear_all()
            self.overlay.show_message("GOODBYE", duration=0)
            self.player.stop()
        except Exception:  # noqa: BLE001
            pass
        self._run_power_off_command()
        self._running = False  # exit the main loop

    def _run_power_off_command(self) -> None:
        command = list(self.config.power_off_command)
        if not command:
            return  # disabled / test mode
        try:
            subprocess.Popen(command)
        except Exception:  # noqa: BLE001
            log.exception("power-off command failed: %s", command)

    def _toggle_mute(self) -> None:
        self.muted = not self.muted
        self.player.set_mute(self.muted)
        self.overlay.show_volume(self.volume, self.muted)

    # -- info / standby -----------------------------------------------------
    def _show_info(self) -> None:
        channel = self.lineup.current
        caption = None
        if self._playing_path is not None and not self._playing_is_break:
            caption = self._caption_for(channel, PlayRequest(path=self._playing_path))
        self.overlay.show_channel_bug(channel.number, channel.name, caption=caption)

    def _caption_for(self, channel: Channel, request: PlayRequest) -> Optional[NowPlaying]:
        """What to caption the bottom-right with: the show and episode, or the
        film and year - worked out from the file's name (see titles.py). None
        for break clips and the guide card, which aren't programmes."""
        if not self.config.ui.now_playing or request.is_break:
            return None
        if request.path.name == GUIDE_FILENAME:
            return None
        return describe(request.path, channel.config.path)

    def _toggle_standby(self) -> None:
        self.standby = not self.standby
        if self.standby:
            self._remember_position()
            self._switch_deadline = None
            self._pending_banner = None
            self._code_buffer = None
            self._relock_all()
            self.player.stop()
            self.overlay.clear_all()
            self.overlay.show_standby()
        else:
            self.overlay.clear_standby()
            self.tune_current(show_static=False)

    def _on_still_screen(self) -> bool:
        """Is the picture one that never changes? The guide card, or the colour
        bars behind a lock screen or an empty channel's NO SIGNAL."""
        if self.standby or self.powered_off:
            return False
        if self._playing_path is None:
            return True  # the colour-bars slate (locked or empty channel)
        return self._playing_path.name == GUIDE_FILENAME

    def _maybe_idle_standby(self, now: float) -> None:
        """Burn-in guard: a still screen left alone too long goes to standby,
        where the screensaver keeps everything moving."""
        limit = self.config.idle_standby_minutes * 60.0
        if limit <= 0 or not self._on_still_screen():
            self._still_since = None
            return
        if self._still_since is None:
            self._still_since = now
        elif now - self._still_since >= limit:
            log.info(
                "still screen for %.0f min with no input - standby (burn-in guard)",
                self.config.idle_standby_minutes,
            )
            self._still_since = None
            self._toggle_standby()

    def _relock_all(self) -> None:
        """Re-lock every passcode-gated channel (standby/power-off fail-safe)."""
        for channel in self.lineup:
            channel.relock()

    # -- direct channel entry ----------------------------------------------
    def _push_digit(self, digit: int) -> None:
        self._digit_buffer = (self._digit_buffer + str(digit))[-3:]
        self._digit_deadline = self._clock() + self._digit_entry_timeout
        self.overlay.show_message(f"CH {self._digit_buffer}_", duration=self._digit_entry_timeout)

    def _confirm_digits(self) -> None:
        if not self._digit_buffer:
            return
        number = int(self._digit_buffer)
        self._digit_buffer = ""
        self._digit_deadline = 0.0
        self.select_channel_number(number)

    def _maybe_commit_digits(self, now: float) -> None:
        if self._digit_buffer and now >= self._digit_deadline:
            self._confirm_digits()

    # -- passcode entry -------------------------------------------------------
    def _code_length(self) -> int:
        return len(self.lineup.current.config.passcode or "0000")

    def _push_code_digit(self, digit: int) -> None:
        length = self._code_length()
        self._code_buffer = ((self._code_buffer or "") + str(digit))[-length:]
        self._dial = 0  # each new digit starts from 0
        if len(self._code_buffer) >= length:
            self._submit_code()
        else:
            self._show_lock_screen()

    def _turn_dial(self, step: int) -> None:
        self._dial = (self._dial + step) % 10  # wraps: ◀ from 0 gives 9
        self._show_lock_screen()

    def _submit_code(self) -> None:
        channel = self.lineup.current
        code = self._code_buffer or ""
        self._code_buffer = None
        if channel.unlock(code):
            self.tune_current(show_static=False)
        else:
            self._code_buffer = ""
            self._dial = 0
            self._show_lock_screen(status="INCORRECT - TRY AGAIN")

    def _show_lock_screen(self, status: Optional[str] = None) -> None:
        """The combination lock: digits entered (masked), the dial, and help.

        Stays up until the channel unlocks or the viewer surfs away - including
        after a wrong code, which says so in place of the help line rather than
        flashing and leaving bare colour bars behind.
        """
        channel = self.lineup.current
        title = channel.config.locked_message or (
            f"CH {channel.number:02d}  {channel.name}  -  LOCKED"
        )
        self.overlay.show_lock(
            title,
            entered=len(self._code_buffer or ""),
            dial=self._dial,
            length=self._code_length(),
            status=status,
        )

    # -- resume banner ----------------------------------------------------
    def _restart_without_resume(self) -> None:
        self.lineup.current.forget_resume()
        self.tune_current(show_static=False)

    # -- playback-finished handling ----------------------------------------
    def _drain_playback_events(self) -> None:
        advanced = False
        while True:
            try:
                reason = self._ended.get_nowait()
            except queue.Empty:
                break
            # Coalesce: only advance once even if several events queued up.
            if reason in (END_EOF, END_ERROR) and not advanced and not self.standby:
                self._advance_current()
                advanced = True

    def _advance_current(self) -> None:
        request = self.lineup.current.advance()
        if request is None:
            self._show_no_signal(self.lineup.current)
        else:
            self._play_request(request)

    # -- helpers ------------------------------------------------------------
    def _remember_position(self) -> None:
        channel = self.lineup.current
        if channel.tune_in_mode != "resume" or self._playing_path is None:
            return
        if self._playing_is_break:
            # A break clip is not the show itself - never resume into one.
            return
        pos = self.player.get_time_pos()
        if pos is not None:
            channel.remember(self._playing_path, pos)
            self._save_state()

    def _save_state(self) -> None:
        if self.config.state_file is None:
            return
        data = {}
        for channel in self.lineup:
            snapshot = channel.resume_snapshot
            if snapshot is not None:
                path, pos = snapshot
                data[channel.number] = {"path": str(path), "position": pos}
        save_state(self.config.state_file, data)

    def _select_start_channel(self) -> None:
        if self.config.start_channel is not None and self.lineup.has_number(
            self.config.start_channel
        ):
            self.lineup.select_number(self.config.start_channel)

    def _resolve_asset(self, filename: str) -> Optional[Path]:
        path = self._assets_dir / filename
        return path if path.is_file() else None

    def _resolve_transition_asset(self) -> Optional[Path]:
        effect = self.config.transition_effect
        if effect == "none":
            return None
        filename = GLITCH_FILENAME if effect == "glitch" else STATIC_FILENAME
        return self._resolve_asset(filename)


def _window_keys_for(input_manager: InputManager) -> Optional[dict]:
    """The keys the video window should accept, or None if it should take none.

    A real remote or USB keyboard is read straight from its device by the
    evdev backend, whatever window has focus. Letting the video window take the
    same keys as well would act on every button press twice, so on a box with
    that backend running the window stays deaf.
    """
    if any(backend.name == "keyboard" for backend in input_manager.backends):
        return None
    return MPV_WINDOW_KEYS


def run_from_config(
    config: Config,
    *,
    dry_run: bool = False,
    config_path: Optional[Path] = None,
    hotplug: bool = False,
) -> None:
    """Convenience entry point used by the CLI.

    ``hotplug`` is appliance mode: watch the config file and channel folders,
    and return (so systemd restarts us with a fresh scan) when they change.
    """
    watch = None
    if hotplug and config_path is not None:
        watch = MediaWatch(config_path, [ch.path for ch in config.channels])
    app = TVApp.from_config(config, dry_run=dry_run, media_watch=watch)
    app.run()


def wait_for_media(config_path: Path, *, fullscreen: bool = True, dry_run: bool = False) -> None:
    """Appliance mode, drive not connected: show a "no signal" card and wait.

    Without this, a box switched on without its drive would fail to start and
    leave a login prompt on the TV. Instead it shows muted colour bars and a
    message, and carries on by itself the moment the drive is plugged in.
    """
    from .media_watch import wait_for_file

    if config_path.is_file():
        return
    shown: list = []

    def show_card() -> None:
        config = Config(channels=[], fullscreen=fullscreen)
        if dry_run:
            player: Player = MockPlayer(verbose=True)
        else:
            from .player import create_player

            player = create_player(
                config.player_backend,
                fullscreen=config.fullscreen,
                hwdec=config.hwdec,
                fonts_dir=DEFAULT_ASSETS_DIR / "fonts",
                force_4_3=config.force_4_3,
            )
        player.set_mute(True)  # the colour bars carry a 1 kHz tone
        colorbars = DEFAULT_ASSETS_DIR / COLORBARS_FILENAME
        if colorbars.is_file():
            player.play_loop(colorbars)
        OverlayManager(player, config).show_message(
            "NO SIGNAL\nCONNECT THE MEDIA DRIVE", duration=0
        )
        shown.append(player)

    try:
        wait_for_file(config_path, on_first_miss=show_card)
    finally:
        for player in shown:
            player.close()
    time.sleep(1.0)  # the drive has only just mounted; let it settle


__all__ = ["TVApp", "run_from_config", "wait_for_media"]
