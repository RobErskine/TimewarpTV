"""Channels: the folders of episodes and how they decide what to play.

A :class:`Channel` wraps one show (a folder of episode files) and knows how to
answer two questions:

* "I just tuned in - what should I play?" (:meth:`Channel.tune_in`)
* "The episode ended - what's next?" (:meth:`Channel.advance`)

and, for the remote's skip buttons, "give me another one" (:meth:`Channel.skip`)
and "go back to the one before" (:meth:`Channel.back`).

The answer depends on the configured ``tune_in`` mode (see ``config.py``):
random, resume, or broadcast. :class:`ChannelLineup` holds all the channels and
provides the up/down/by-number navigation a remote needs.
"""

from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import AbstractSet, Dict, List, Optional, Sequence

# Patterns for pulling a season number out of a file/folder path.
_SEASON_PATTERNS = (
    re.compile(r"s(\d{1,2})[ ._-]?e\d{1,3}", re.IGNORECASE),   # S06E01, s6e1
    re.compile(r"\bseason[ ._-]*(\d{1,2})\b", re.IGNORECASE),  # Season 6
    re.compile(r"\b(\d{1,2})x\d{1,3}\b"),                       # 6x01
)

from .config import BreaksConfig, ChannelConfig, Config
from .playlist import ShuffleBag
from .probe import DEFAULT_EPISODE_SECONDS, probe_duration
from .state import load_state

log = logging.getLogger(__name__)

# How many episodes back the remote's "previous" button can reach, per channel.
_HISTORY_LIMIT = 50


@dataclass(frozen=True)
class PlayRequest:
    """An instruction to the player: play ``path`` starting at ``start`` sec."""

    path: Path
    start: float = 0.0
    # True for a between-episode break clip (commercial/bathroom/snack/stretch)
    # - kept out of the resume position and the episode count.
    is_break: bool = False
    # True when this request is the viewer's remembered "resume" position, so
    # the app can offer a "press OK to start over" banner.
    resumed: bool = False


def detect_season(text: str) -> Optional[int]:
    """Best-effort extraction of a season number from a path/filename."""
    for pattern in _SEASON_PATTERNS:
        match = pattern.search(text)
        if match:
            return int(match.group(1))
    return None


def scan_episodes(
    root: Path,
    extensions: Sequence[str],
    *,
    recursive: bool = True,
    exclude: Sequence[str] = (),
    exclude_seasons: AbstractSet[int] = frozenset(),
) -> List[Path]:
    """Return a sorted list of episode files under ``root``.

    Sorting is natural-ish (case-insensitive by full path) so that, in the rare
    cases we present episodes in order, they are at least stable. Hidden files
    and typical sidecar files are ignored.

    ``exclude`` is a list of case-insensitive glob patterns; any episode whose
    relative path or filename matches one is dropped. ``exclude_seasons`` drops
    episodes whose detected season number is in the set.
    """
    if not root.exists():
        log.warning("channel folder does not exist: %s", root)
        return []
    exts = {e.lower() for e in extensions}
    patterns = [p.lower() for p in exclude]
    walker = root.rglob("*") if recursive else root.glob("*")
    episodes = [
        p
        for p in walker
        if p.is_file()
        and p.suffix.lower() in exts
        and not p.name.startswith(".")
        and not _is_excluded(p, root, patterns, exclude_seasons)
    ]
    episodes.sort(key=lambda p: str(p).lower())
    return episodes


def _is_excluded(
    path: Path,
    root: Path,
    patterns: Sequence[str],
    exclude_seasons: AbstractSet[int],
) -> bool:
    import fnmatch

    try:
        rel = path.relative_to(root).as_posix().lower()
    except ValueError:  # pragma: no cover - path always under root here
        rel = path.name.lower()
    name = path.name.lower()
    for pat in patterns:
        if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(name, pat):
            return True
    if exclude_seasons:
        season = detect_season(rel)
        if season is not None and season in exclude_seasons:
            return True
    return False


class BroadcastSchedule:
    """A never-ending, always-running shuffled running order for a channel.

    Given episode durations and a fixed start epoch, it can report exactly what
    "would be airing" at any wall-clock moment - the illusion that the station
    kept broadcasting while nobody was watching. The running order is a single
    shuffle that loops forever.
    """

    def __init__(
        self,
        episodes: Sequence[Path],
        durations: Sequence[float],
        *,
        epoch: float,
        rng: random.Random,
    ) -> None:
        if len(episodes) != len(durations):
            raise ValueError("episodes and durations must be the same length")
        order = list(range(len(episodes)))
        rng.shuffle(order)
        self._episodes = [episodes[i] for i in order]
        self._durations = [max(1.0, float(durations[i])) for i in order]
        self._epoch = epoch
        self._cycle = sum(self._durations)

    def at(self, when: float) -> PlayRequest:
        """What is airing at wall-clock time ``when`` (and how far into it)."""
        elapsed = (when - self._epoch) % self._cycle
        for path, dur in zip(self._episodes, self._durations):
            if elapsed < dur:
                return PlayRequest(path=path, start=elapsed)
            elapsed -= dur
        # Floating point rounding safety net.
        return PlayRequest(path=self._episodes[-1], start=0.0)


class Channel:
    """A single TV channel backed by a folder of episodes."""

    def __init__(
        self,
        config: ChannelConfig,
        episodes: Sequence[Path],
        *,
        tune_in: str = "random",
        start_offset_min: float = 0.0,
        start_offset_max: Optional[float] = None,
        rng: Optional[random.Random] = None,
        breaks: Optional[BreaksConfig] = None,
        break_clips: Sequence[Path] = (),
    ) -> None:
        self.config = config
        self.episodes: List[Path] = list(episodes)
        self.tune_in_mode = tune_in
        # Start each episode a random number of seconds in (within this range) so
        # the picture appears already "in the show" and channel switches land at
        # varied points instead of always the same spot.
        self.start_offset_min = max(0.0, start_offset_min)
        self.start_offset_max = (
            self.start_offset_min
            if start_offset_max is None
            else max(self.start_offset_min, start_offset_max)
        )
        self._rng = rng or random.Random()
        self._bag: Optional[ShuffleBag[Path]] = (
            ShuffleBag(self.episodes, self._rng) if self.episodes else None
        )
        # Resume state (used by the "resume" tune-in mode).
        self._resume_path: Optional[Path] = None
        self._resume_position: float = 0.0
        # Broadcast schedule (built lazily on first use in "broadcast" mode).
        self._broadcast: Optional[BroadcastSchedule] = None
        # Passcode lock (see config.ChannelConfig.passcode /
        # docs/decisions/0001-passcode-instead-of-time-gate.md).
        self.locked: bool = config.passcode is not None
        # Between-episode break blocks.
        self._breaks = breaks
        self._break_bag: Optional[ShuffleBag[Path]] = (
            ShuffleBag(list(break_clips), self._rng) if break_clips else None
        )
        self._break_queue: List[PlayRequest] = []
        self._episodes_since_break = 0
        self._last_was_break = False
        # Episodes this channel has shown, oldest first, for the remote's skip
        # buttons. _cursor points at what is on now; "previous" steps it back
        # and "next" steps it forward again before drawing anything new - the
        # same back/forward behaviour as shuffle on a music player.
        self._history: List[Path] = []
        self._cursor = -1

    # -- identity -----------------------------------------------------------
    @property
    def number(self) -> int:
        return self.config.number

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def is_empty(self) -> bool:
        return not self.episodes

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Channel {self.number} {self.name!r} ({len(self.episodes)} eps)>"

    # -- playback selection -------------------------------------------------
    def _start_offset(self) -> float:
        if self.start_offset_max > self.start_offset_min:
            return self._rng.uniform(self.start_offset_min, self.start_offset_max)
        return self.start_offset_min

    def _next_shuffled(self) -> PlayRequest:
        assert self._bag is not None
        return PlayRequest(path=self._bag.next(), start=self._start_offset())

    def tune_in(self, *, now: Optional[float] = None) -> Optional[PlayRequest]:
        """Decide what to play the instant a viewer switches to this channel."""
        # Flipping to a channel never lands you mid-break, and cancels any
        # in-progress passcode entry state the app was tracking for it.
        self._break_queue = []
        self._last_was_break = False

        if self.is_empty or self.locked:
            return None
        now = time.time() if now is None else now

        request: Optional[PlayRequest] = None
        if self.tune_in_mode == "resume" and self._resume_path is not None:
            request = PlayRequest(
                path=self._resume_path, start=self._resume_position, resumed=True
            )
        elif self.tune_in_mode == "broadcast":
            schedule = self._ensure_broadcast(epoch=now)
            if schedule is not None:
                request = schedule.at(now)
        if request is None:
            # Random mode - or broadcast whose schedule could not be built.
            request = self._next_shuffled()
        self._record(request.path)
        return request

    def advance(self) -> Optional[PlayRequest]:
        """Decide what to play when the current episode ends naturally."""
        if self.is_empty:
            return None

        # Serve any queued break clips before anything else.
        if self._break_queue:
            self._last_was_break = True
            return self._break_queue.pop(0)

        # Only count actual episodes toward the break threshold - not the
        # break clip that may have just finished.
        if not self._last_was_break:
            self._episodes_since_break += 1
            if self._breaks is not None and self._episodes_since_break >= self._breaks.every:
                self._episodes_since_break = 0
                self._fill_break_queue()
                if self._break_queue:
                    self._last_was_break = True
                    return self._break_queue.pop(0)

        if self.tune_in_mode == "broadcast" and self._broadcast is not None:
            # Roll straight into whatever airs next in the running order.
            request = self._broadcast.at(time.time())
        else:
            request = self._next_shuffled()

        self._last_was_break = False
        self._record(request.path)
        self._track_resume(request)
        return request

    # -- the remote's skip buttons ------------------------------------------
    def skip(self) -> Optional[PlayRequest]:
        """"Not this one": leave the current episode for another.

        Steps forward through episodes already seen if the viewer went back,
        otherwise draws a fresh one from the shuffle. A skip is not a natural
        end, so it never triggers or counts toward a break block, and pressing
        it during a break abandons the rest of the break.
        """
        if not self._skippable():
            return None
        if self._cursor < len(self._history) - 1:
            self._cursor += 1
            request = PlayRequest(self._history[self._cursor], start=self._start_offset())
        else:
            if len(self.episodes) < 2:
                return None  # nothing else to show; don't restart the only one
            request = self._next_shuffled()
            if self._history and request.path == self._history[self._cursor]:
                # Only possible when the current episode didn't come from the
                # bag (a resumed film): draw once more rather than restart it.
                request = self._next_shuffled()
            self._record(request.path)
        self._leave_break()
        self._track_resume(request)
        return request

    def back(self) -> Optional[PlayRequest]:
        """Return to the episode shown before this one on this channel.

        During a break, "before this one" is the episode the break followed -
        the viewer is asking for the show back, not for the one before it.
        """
        if not self._skippable():
            return None
        if self._last_was_break and self._cursor >= 0:
            target = self._cursor
        elif self._cursor > 0:
            target = self._cursor - 1
        else:
            return None
        self._cursor = target
        self._leave_break()
        request = PlayRequest(self._history[target], start=self._start_offset())
        self._track_resume(request)
        return request

    def _skippable(self) -> bool:
        # A broadcast channel is "live": its position comes from the clock, so
        # a skip would just be undone the next time you tuned in. Better that
        # the button does nothing than something that silently reverts.
        return not (self.is_empty or self.locked or self.tune_in_mode == "broadcast")

    def _leave_break(self) -> None:
        self._break_queue = []
        self._last_was_break = False

    def _record(self, path: Path) -> None:
        """Note ``path`` as what is on now. Playing something new after going
        back discards the forward history, as in a web browser."""
        if 0 <= self._cursor < len(self._history) and self._history[self._cursor] == path:
            return
        del self._history[self._cursor + 1 :]
        self._history.append(path)
        if len(self._history) > _HISTORY_LIMIT:
            del self._history[0]
        self._cursor = len(self._history) - 1

    def _track_resume(self, request: PlayRequest) -> None:
        if self.tune_in_mode == "resume":
            # Whatever just started is what a flip-away-and-back should resume
            # - not a stale position from the last time the channel changed.
            # See docs/decisions: "advance() must clear the stale resume
            # position" bug fix.
            self._resume_path = request.path
            self._resume_position = request.start

    def _fill_break_queue(self) -> None:
        if self._break_bag is None or self._breaks is None:
            return
        count = self._breaks.count_min
        if self._breaks.count_max > self._breaks.count_min:
            count = self._rng.randint(self._breaks.count_min, self._breaks.count_max)
        self._break_queue = [
            PlayRequest(path=self._break_bag.next(), start=0.0, is_break=True)
            for _ in range(count)
        ]

    def remember(self, path: Path, position: float) -> None:
        """Record where the viewer left off (for the "resume" mode)."""
        self._resume_path = path
        self._resume_position = max(0.0, position)

    def forget_resume(self) -> None:
        """Discard the remembered position (viewer chose to start over)."""
        self._resume_path = None
        self._resume_position = 0.0

    @property
    def resume_snapshot(self) -> Optional[tuple[Path, float]]:
        """The remembered (path, position), or None if nothing is remembered."""
        if self._resume_path is None:
            return None
        return (self._resume_path, self._resume_position)

    # -- passcode lock --------------------------------------------------------
    def unlock(self, code: str) -> bool:
        """Try to unlock with ``code``. Returns whether it succeeded."""
        if self.config.passcode is None:
            self.locked = False
            return True
        if code == self.config.passcode:
            self.locked = False
            return True
        return False

    def relock(self) -> None:
        if self.config.passcode is not None:
            self.locked = True

    # -- introspection (used by `--check`) -------------------------------------
    @property
    def break_info(self) -> Optional[tuple[int, int]]:
        """(clip_count, every_n_episodes) if breaks are configured, else None."""
        if self._breaks is None:
            return None
        count = len(self._break_bag) if self._break_bag else 0
        return (count, self._breaks.every)

    # -- broadcast schedule -------------------------------------------------
    def _ensure_broadcast(self, *, epoch: float) -> Optional[BroadcastSchedule]:
        if self._broadcast is not None:
            return self._broadcast
        if self.is_empty:
            return None
        durations: List[float] = []
        for path in self.episodes:
            dur = probe_duration(path)
            durations.append(dur if dur else DEFAULT_EPISODE_SECONDS)
        # Use a channel-stable epoch offset so different channels are out of
        # phase with each other, but keep it deterministic per run.
        self._broadcast = BroadcastSchedule(
            self.episodes, durations, epoch=epoch, rng=self._rng
        )
        return self._broadcast


class ChannelLineup:
    """An ordered set of channels with remote-style navigation."""

    def __init__(self, channels: Sequence[Channel]) -> None:
        if not channels:
            raise ValueError("a lineup needs at least one channel")
        # Present channels in ascending channel-number order, like a real tuner.
        self._channels: List[Channel] = sorted(channels, key=lambda c: c.number)
        self._by_number: Dict[int, Channel] = {c.number: c for c in self._channels}
        self._index = 0

    def __len__(self) -> int:
        return len(self._channels)

    def __iter__(self):
        return iter(self._channels)

    @property
    def current(self) -> Channel:
        return self._channels[self._index]

    @property
    def numbers(self) -> List[int]:
        return [c.number for c in self._channels]

    def has_number(self, number: int) -> bool:
        return number in self._by_number

    def index_of(self, number: int) -> Optional[int]:
        for i, ch in enumerate(self._channels):
            if ch.number == number:
                return i
        return None

    def up(self) -> Channel:
        self._index = (self._index + 1) % len(self._channels)
        return self.current

    def down(self) -> Channel:
        self._index = (self._index - 1) % len(self._channels)
        return self.current

    def select_number(self, number: int) -> Optional[Channel]:
        idx = self.index_of(number)
        if idx is None:
            return None
        self._index = idx
        return self.current

    def select_index(self, index: int) -> Channel:
        self._index = index % len(self._channels)
        return self.current


def _effective_breaks(
    global_breaks: Optional[BreaksConfig], ch_cfg: ChannelConfig
) -> Optional[BreaksConfig]:
    if ch_cfg.breaks_disabled:
        return None
    if ch_cfg.breaks is not None:
        return ch_cfg.breaks
    return global_breaks


def build_lineup(config: Config, *, rng: Optional[random.Random] = None) -> ChannelLineup:
    """Scan every configured channel folder and build the full lineup."""
    base_rng = rng or random.Random(config.shuffle_seed)
    saved_state = load_state(config.state_file)
    channels: List[Channel] = []
    for i, ch_cfg in enumerate(config.channels):
        episodes = scan_episodes(
            ch_cfg.path,
            config.video_extensions,
            recursive=config.scan_recursive,
            exclude=ch_cfg.exclude,
            exclude_seasons=ch_cfg.exclude_seasons,
        )
        if not episodes:
            log.warning(
                "channel %s (%s) has no playable episodes in %s",
                ch_cfg.number, ch_cfg.name, ch_cfg.path,
            )
        # Give each channel its own RNG stream so they shuffle independently
        # but reproducibly when a seed is configured.
        if config.shuffle_seed is not None:
            # Derive a distinct-but-deterministic integer seed per channel.
            ch_rng = random.Random(hash((config.shuffle_seed, ch_cfg.number, i)) & 0xFFFFFFFF)
        else:
            ch_rng = random.Random()

        breaks_cfg = _effective_breaks(config.breaks, ch_cfg)
        break_clips: List[Path] = []
        if breaks_cfg is not None and breaks_cfg.path is not None:
            break_clips = scan_episodes(breaks_cfg.path, config.video_extensions, recursive=True)
            if not break_clips:
                log.warning(
                    "channel %s (%s) has breaks configured but no clips in %s",
                    ch_cfg.number, ch_cfg.name, breaks_cfg.path,
                )

        lo, hi = ch_cfg.start_offset or (config.start_offset_min, config.start_offset_max)

        channel = Channel(
            ch_cfg,
            episodes,
            tune_in=ch_cfg.tune_in or config.tune_in,
            start_offset_min=lo,
            start_offset_max=hi,
            rng=ch_rng,
            breaks=breaks_cfg,
            break_clips=break_clips,
        )

        saved = saved_state.get(ch_cfg.number)
        if saved:
            saved_path = Path(str(saved.get("path", "")))
            if saved_path.is_file():
                channel.remember(saved_path, float(saved.get("position", 0.0)))

        channels.append(channel)
    return ChannelLineup(channels)


__all__ = [
    "Channel",
    "ChannelLineup",
    "PlayRequest",
    "BroadcastSchedule",
    "scan_episodes",
    "detect_season",
    "build_lineup",
]
