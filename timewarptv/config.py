"""Configuration loading and validation.

The whole box is described by a single YAML file (see ``config.example.yaml``).
This module turns that file into validated :class:`Config` /
:class:`ChannelConfig` objects and fills in sensible defaults so a minimal
config still produces a working television.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional


# Video containers we consider "an episode" when scanning a channel folder.
DEFAULT_VIDEO_EXTENSIONS: tuple[str, ...] = (
    ".mp4", ".mkv", ".avi", ".m4v", ".mov", ".webm", ".mpg", ".mpeg", ".ts",
)


class ConfigError(Exception):
    """Raised when the configuration file is missing or invalid."""


# How a channel behaves the moment you tune into it.
#   random    - start a fresh random episode from the beginning (the default,
#               and what most people picture: flip to the channel, a show
#               starts). Episodes keep rolling on a shuffle after that.
#   resume    - remember where you were on that channel and pick up there,
#               so flipping away and back does not restart the episode.
#   broadcast - the channel behaves like a real station that is "always on":
#               a fixed shuffled running order advances in real time whether
#               or not anyone is watching, so you tune in partway through
#               whatever "would" be airing right now.
TUNE_IN_MODES = ("random", "resume", "broadcast")

# Effect shown briefly while changing channels.
#   glitch - a short burst of digital corruption (default)
#   static - classic analog snow
#   none   - cut straight to the next channel
TRANSITION_EFFECTS = ("glitch", "static", "none")

# Which real video player backend to use.
#   auto   - libmpv, except on macOS where libmpv cannot open its own window
#            from a plain Python process (audio plays, no picture); there it
#            drives the mpv binary over a JSON IPC socket instead.
#   libmpv - always use libmpv (one process, needs python-mpv + libmpv)
#   ipc    - always drive the mpv binary as a subprocess
PLAYER_BACKENDS = ("auto", "libmpv", "ipc")


@dataclass(frozen=True)
class UiConfig:
    """Look of the on-screen overlays (the green digital TV readouts)."""

    font: str = "VT323"             # bundled retro terminal font (OFL)
    color: str = "#4DFF5A"          # bright CRT phosphor green
    dim_color: str = "#123B18"      # unlit volume segment / dot colour
    glow: bool = True               # soft glow around text for that CRT bloom
    logo: bool = True               # corner mark on the channel banner / volume bar
    brand: str = "TIME WARP TV"     # station name, shown on the welcome channel
    now_playing: bool = True        # caption the show/episode or film/year, bottom-right


@dataclass(frozen=True)
class CrtConfig:
    """The CRT picture effect applied to the 4:3 video via a GLSL shader."""

    # Tuned to read as a tube TV without fighting the picture: the shape and
    # the scanlines are there, but a kid can still see the corners of the show.
    # Roughly double every number below for the heavy, unmistakable version.
    enabled: bool = True
    # The effect sells "old show on a tube TV", which is wrong for a modern HD
    # film - and it costs GPU time exactly where a Pi has least to spare. Above
    # this source height the shader is switched off for that item and back on
    # afterwards. 0 disables the rule (always on). 720 keeps it for period TV,
    # including 720p kids' shows, and drops it for 1080p features.
    max_height: int = 720
    curvature: float = 0.045        # barrel "bulge" amount (0 = perfectly flat)
    corner_radius: float = 0.032    # rounded-corner size (fraction of screen)
    vignette: float = 0.11          # darkening toward the edges
    scanlines: bool = True
    scanline_intensity: float = 0.045


@dataclass(frozen=True)
class BreaksConfig:
    """Between-episode break clips: old commercials, or bathroom/snack/stretch cards.

    Fired only when an episode reaches a natural end - never mid-episode. See
    docs/decisions/0004-breaks-between-episodes-only.md.
    """

    path: Optional[Path] = None
    every: int = 1              # a break block after every N episodes
    count_min: int = 1          # clips per break block
    count_max: int = 1


@dataclass(frozen=True)
class ChannelConfig:
    """A single television channel backed by a folder of episodes."""

    number: int
    name: str
    path: Path
    shuffle: bool = True
    # Episodes to leave out. `exclude` is a list of case-insensitive glob
    # patterns matched against each file's path (and name); `exclude_seasons` is
    # a set of season numbers detected from the path (e.g. S06E01, "Season 6").
    exclude: tuple[str, ...] = ()
    exclude_seasons: frozenset[int] = frozenset()
    # A 4-digit-style passcode gate (see docs/decisions/0001). None = unlocked
    # channel, reachable by anyone. Digits only, 1-8 chars.
    passcode: Optional[str] = None
    locked_message: Optional[str] = None
    # Per-channel override of the global `tune_in` mode / start_offset. None
    # means "use the global setting".
    tune_in: Optional[str] = None
    start_offset: Optional[tuple[float, float]] = None
    # Per-channel break-block override: a BreaksConfig overrides the global
    # one entirely; breaks_disabled=True turns breaks off for this channel
    # regardless of the global setting (`breaks: false` in YAML).
    breaks: Optional[BreaksConfig] = None
    breaks_disabled: bool = False

    def __post_init__(self) -> None:
        if self.number < 0:
            raise ConfigError(f"channel number must be >= 0, got {self.number}")
        if not self.name:
            raise ConfigError(f"channel {self.number} is missing a name")


@dataclass(frozen=True)
class Config:
    """Top-level configuration for the whole TV."""

    channels: List[ChannelConfig]
    video_extensions: tuple[str, ...] = DEFAULT_VIDEO_EXTENSIONS
    tune_in: str = "random"
    start_channel: Optional[int] = None
    # Where the remote's HOME button goes (the welcome screen / channel guide).
    # None = use start_channel, so a box that boots onto the guide needs no
    # extra setting.
    home_channel: Optional[int] = None

    # Presentation / "feel" of the TV.
    force_4_3: bool = False                # if true, letterbox everything to 4:3;
                                          #   default keeps each show's own aspect
    # Start each episode a random number of seconds in (between min and max), so
    # channel switches land at varied points in the show.
    start_offset_min: float = 6.0
    start_offset_max: float = 10.0
    transition_effect: str = "none"       # channel-change effect: none|glitch|static
    transition_duration: float = 0.4      # length of the channel-change effect
    # When there's no transition effect, keep the current show playing this many
    # seconds while the next channel preloads, then cut over (avoids a frozen
    # frame on channel change). 0 = switch immediately.
    bridge_seconds: float = 0.8
    channel_bug_seconds: float = 4.0      # how long the channel banner lingers
    # Burn-in guard: after this many minutes on a still screen - the guide
    # card, a locked channel's lock screen, an empty channel's colour bars -
    # with no remote presses, go to standby (the moving screensaver). Pressing
    # anything restarts the count; ordinary shows never trigger it. 0 = never.
    idle_standby_minutes: float = 10.0
    osd_duration: float = 2.0             # how long volume/message overlays linger
    ui: UiConfig = field(default_factory=UiConfig)
    crt: CrtConfig = field(default_factory=CrtConfig)

    # Window / decode (mainly useful on a desktop dev machine, or to work
    # around a weak GPU on a Raspberry Pi 3 - see Part H/Pi-3 notes in the
    # README).
    fullscreen: bool = True
    hwdec: str = "auto-safe"
    # Which real player to drive: "auto" picks the subprocess/IPC mpv backend on
    # macOS (libmpv cannot open a window from a plain Python process there) and
    # libmpv everywhere else. Force one with "libmpv" or "ipc".
    player_backend: str = "auto"

    # Between-episode break blocks (old commercials / bathroom-snack-stretch
    # cards). None = no breaks anywhere unless a channel sets its own.
    breaks: Optional[BreaksConfig] = None

    # Where "resume" tune-in state is persisted so it survives a power cut.
    # None = resume only lasts for the current process (in-memory).
    state_file: Optional[Path] = None

    # Audio.
    initial_volume: int = 70              # 0-100
    volume_step: int = 5
    audio_device: Optional[str] = None    # mpv audio device (e.g. HDMI); None = auto
    # Press volume-down once more when already at 0 to cleanly power off the Pi
    # (so it's safe to unplug). The command run to shut down:
    power_off_on_min_volume: bool = True
    power_off_command: tuple[str, ...] = ("sudo", "poweroff")

    # Playback.
    scan_recursive: bool = True           # look in sub-folders for episodes
    shuffle_seed: Optional[int] = None    # set for deterministic ordering (tests)

    # Assets (generated by scripts/install.sh via timewarptv.static_gen).
    assets_dir: Optional[Path] = None

    # Options for the input backends (see input/manager.create_backends).
    input_options: Mapping[str, Any] = field(default_factory=dict)

    def channel_numbers(self) -> List[int]:
        return [c.number for c in self.channels]

    def with_channels(self, channels: List[ChannelConfig]) -> "Config":
        return replace(self, channels=channels)


def _as_path(value: Any, base: Optional[Path]) -> Path:
    p = Path(os.path.expanduser(str(value)))
    if not p.is_absolute() and base is not None:
        p = (base / p)
    return p


def _discover_channels(
    media_root: Path,
    *,
    start_number: int,
    default_shuffle: bool,
) -> List[ChannelConfig]:
    """Turn every immediate sub-folder of ``media_root`` into a channel.

    This is the "just drop show folders on the SD card" workflow: a folder
    called ``Dragon Tales`` becomes a channel named "Dragon Tales". Channels
    are numbered sequentially starting at ``start_number`` in alphabetical
    order of the folder name.
    """
    if not media_root.is_dir():
        raise ConfigError(f"media_root does not exist or is not a directory: {media_root}")

    subdirs = sorted(
        (p for p in media_root.iterdir() if p.is_dir() and not p.name.startswith(".")),
        key=lambda p: p.name.lower(),
    )
    channels: List[ChannelConfig] = []
    for offset, folder in enumerate(subdirs):
        channels.append(
            ChannelConfig(
                number=start_number + offset,
                name=_prettify_name(folder.name),
                path=folder,
                shuffle=default_shuffle,
            )
        )
    return channels


def _prettify_name(folder_name: str) -> str:
    """Turn a folder name like ``dragon_tales`` into ``Dragon Tales``."""
    cleaned = folder_name.replace("_", " ").replace("-", " ").strip()
    cleaned = " ".join(cleaned.split())
    return cleaned.title() if cleaned.islower() else cleaned


def _parse_channels(
    raw: Any,
    base: Optional[Path],
    default_shuffle: bool,
    global_breaks: Optional[BreaksConfig],
) -> List[ChannelConfig]:
    if not isinstance(raw, list):
        raise ConfigError("'channels' must be a list")
    channels: List[ChannelConfig] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ConfigError(f"channel #{i} must be a mapping, got {type(entry).__name__}")
        if "path" not in entry:
            raise ConfigError(f"channel #{i} is missing required key 'path'")
        number = entry.get("number", i + 2)  # old TVs often started around ch. 2
        name = entry.get("name") or _prettify_name(Path(str(entry["path"])).name)

        ch_tune_in: Optional[str] = None
        if entry.get("tune_in") is not None:
            ch_tune_in = str(entry["tune_in"]).lower()
            if ch_tune_in not in TUNE_IN_MODES:
                raise ConfigError(
                    f"channel #{i} 'tune_in' must be one of {TUNE_IN_MODES}, got '{entry['tune_in']}'"
                )

        has_offset_keys = any(
            k in entry for k in ("start_offset", "start_offset_min", "start_offset_max")
        )
        ch_start_offset = _offset_range(entry) if has_offset_keys else None

        breaks_raw = entry.get("breaks")
        breaks_disabled = breaks_raw is False
        ch_breaks = (
            _parse_breaks(breaks_raw, base, default=global_breaks)
            if isinstance(breaks_raw, dict)
            else None
        )
        if breaks_raw is not None and breaks_raw is not False and not isinstance(breaks_raw, dict):
            raise ConfigError(f"channel #{i} 'breaks' must be a mapping or 'false'")

        locked_message = entry.get("locked_message")

        channels.append(
            ChannelConfig(
                number=int(number),
                name=str(name),
                path=_as_path(entry["path"], base),
                shuffle=bool(entry.get("shuffle", default_shuffle)),
                exclude=_parse_str_list(entry.get("exclude"), "exclude"),
                exclude_seasons=_parse_seasons(entry.get("exclude_seasons")),
                passcode=_parse_passcode(entry.get("passcode"), i),
                locked_message=str(locked_message) if locked_message else None,
                tune_in=ch_tune_in,
                start_offset=ch_start_offset,
                breaks=ch_breaks,
                breaks_disabled=breaks_disabled,
            )
        )
    return channels


def _parse_passcode(raw: Any, index: int) -> Optional[str]:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s.isdigit() or not (1 <= len(s) <= 8):
        raise ConfigError(f"channel #{index} 'passcode' must be 1-8 digits, got '{raw}'")
    return s


def _parse_breaks(
    raw: Any, base: Optional[Path], *, default: Optional[BreaksConfig] = None
) -> Optional[BreaksConfig]:
    if raw is None or raw is False:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("'breaks' must be a mapping")
    base_cfg = default or BreaksConfig()

    path_raw = raw.get("path")
    path = _as_path(path_raw, base) if path_raw else base_cfg.path

    every = _clamp_int(raw.get("every", base_cfg.every), 1, 1000, "breaks.every")

    count_raw = raw.get("count", [base_cfg.count_min, base_cfg.count_max])
    if isinstance(count_raw, (list, tuple)):
        if not count_raw:
            raise ConfigError("'breaks.count' list cannot be empty")
        cmin = _clamp_int(count_raw[0], 1, 20, "breaks.count")
        cmax = _clamp_int(count_raw[1] if len(count_raw) > 1 else count_raw[0], 1, 20, "breaks.count")
    else:
        cmin = cmax = _clamp_int(count_raw, 1, 20, "breaks.count")

    return BreaksConfig(path=path, every=every, count_min=cmin, count_max=max(cmin, cmax))


def _parse_str_list(raw: Any, name: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (raw,)
    if isinstance(raw, list):
        return tuple(str(x) for x in raw)
    raise ConfigError(f"'{name}' must be a string or a list of strings")


def _parse_seasons(raw: Any) -> frozenset[int]:
    """Parse season numbers from an int, a 'start-end' range, or a list of those."""
    if raw is None:
        return frozenset()
    items = raw if isinstance(raw, list) else [raw]
    seasons: set[int] = set()
    for item in items:
        if isinstance(item, int):
            seasons.add(item)
        elif isinstance(item, str) and "-" in item:
            lo_s, hi_s = item.split("-", 1)
            try:
                lo, hi = int(lo_s), int(hi_s)
            except ValueError as exc:
                raise ConfigError(f"invalid season range '{item}'") from exc
            seasons.update(range(min(lo, hi), max(lo, hi) + 1))
        else:
            try:
                seasons.add(int(item))
            except (TypeError, ValueError) as exc:
                raise ConfigError(f"invalid season number '{item}'") from exc
    return frozenset(seasons)


def config_from_dict(data: Dict[str, Any], *, base_dir: Optional[Path] = None) -> Config:
    """Build a :class:`Config` from an already-parsed mapping.

    ``base_dir`` is used to resolve relative paths (normally the directory the
    config file lives in).
    """
    if not isinstance(data, dict):
        raise ConfigError("configuration root must be a mapping")

    default_shuffle = bool(data.get("shuffle", True))

    exts = data.get("video_extensions")
    if exts is None:
        extensions = DEFAULT_VIDEO_EXTENSIONS
    else:
        if not isinstance(exts, list) or not exts:
            raise ConfigError("'video_extensions' must be a non-empty list")
        extensions = tuple(e if e.startswith(".") else f".{e}" for e in (s.lower() for s in exts))

    media_root_raw = data.get("media_root")
    media_root = _as_path(media_root_raw, base_dir) if media_root_raw else None

    breaks_raw = data.get("breaks")
    if breaks_raw is not None and breaks_raw is not False and not isinstance(breaks_raw, dict):
        raise ConfigError("'breaks' must be a mapping or 'false'")
    global_breaks = _parse_breaks(breaks_raw, media_root or base_dir)

    if "channels" in data:
        channels = _parse_channels(
            data["channels"], media_root or base_dir, default_shuffle, global_breaks
        )
    elif media_root is not None:
        channels = _discover_channels(
            media_root,
            start_number=int(data.get("first_channel_number", 2)),
            default_shuffle=default_shuffle,
        )
    else:
        raise ConfigError("configuration must define either 'channels' or 'media_root'")

    if not channels:
        raise ConfigError("no channels found - check 'channels' or the folders under 'media_root'")

    _ensure_unique_numbers(channels)

    tune_in = str(data.get("tune_in", "random")).lower()
    if tune_in not in TUNE_IN_MODES:
        raise ConfigError(f"'tune_in' must be one of {TUNE_IN_MODES}, got '{tune_in}'")

    assets_dir_raw = data.get("assets_dir")
    assets_dir = _as_path(assets_dir_raw, base_dir) if assets_dir_raw else None

    start_channel = data.get("start_channel")
    start_channel = int(start_channel) if start_channel is not None else None

    home_channel = data.get("home_channel")
    home_channel = int(home_channel) if home_channel is not None else None

    initial_volume = _clamp_int(data.get("initial_volume", 70), 0, 100, "initial_volume")
    volume_step = _clamp_int(data.get("volume_step", 5), 1, 100, "volume_step")
    audio_device = data.get("audio_device")
    audio_device = str(audio_device) if audio_device else None

    poff_raw = data.get("power_off_command", ["sudo", "poweroff"])
    if isinstance(poff_raw, str):
        power_off_command = tuple(poff_raw.split())
    elif isinstance(poff_raw, list):
        power_off_command = tuple(str(x) for x in poff_raw)
    else:
        raise ConfigError("'power_off_command' must be a string or list of strings")

    state_file_raw = data.get("state_file")
    state_file = _as_path(state_file_raw, base_dir) if state_file_raw else None

    return Config(
        channels=channels,
        video_extensions=extensions,
        tune_in=tune_in,
        start_channel=start_channel,
        home_channel=home_channel,
        force_4_3=bool(data.get("force_4_3", False)),
        start_offset_min=_offset_range(data)[0],
        start_offset_max=_offset_range(data)[1],
        transition_effect=_valid_transition(data.get("transition", "none")),
        transition_duration=_clamp_float(data.get("transition_duration", 0.4), 0.0, 10.0, "transition_duration"),
        bridge_seconds=_clamp_float(data.get("bridge_seconds", 0.8), 0.0, 10.0, "bridge_seconds"),
        channel_bug_seconds=_clamp_float(data.get("channel_bug_seconds", 4.0), 0.0, 60.0, "channel_bug_seconds"),
        idle_standby_minutes=_clamp_float(
            data.get("idle_standby_minutes", 10.0), 0.0, 24 * 60.0, "idle_standby_minutes"
        ),
        osd_duration=_clamp_float(data.get("osd_duration", 2.0), 0.0, 60.0, "osd_duration"),
        ui=_parse_ui(data.get("ui")),
        crt=_parse_crt(data.get("crt")),
        fullscreen=bool(data.get("fullscreen", True)),
        hwdec=str(data.get("hwdec", "auto-safe")),
        player_backend=_valid_player_backend(data.get("player_backend", "auto")),
        breaks=global_breaks,
        state_file=state_file,
        initial_volume=initial_volume,
        volume_step=volume_step,
        audio_device=audio_device,
        power_off_on_min_volume=bool(data.get("power_off_on_min_volume", True)),
        power_off_command=power_off_command,
        scan_recursive=bool(data.get("scan_recursive", True)),
        shuffle_seed=(int(data["shuffle_seed"]) if data.get("shuffle_seed") is not None else None),
        assets_dir=assets_dir,
        input_options=dict(data.get("input") or {}),
    )


def load_config(path: os.PathLike | str) -> Config:
    """Load and validate a YAML configuration file."""
    import yaml  # imported lazily so importing the package is cheap

    cfg_path = Path(path).expanduser()
    if not cfg_path.is_file():
        raise ConfigError(f"configuration file not found: {cfg_path}")
    try:
        with cfg_path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except yaml.YAMLError as exc:  # pragma: no cover - passthrough of parser error
        raise ConfigError(f"could not parse YAML in {cfg_path}: {exc}") from exc

    return config_from_dict(data, base_dir=cfg_path.parent)


def _parse_ui(raw: Any) -> UiConfig:
    if raw is None:
        return UiConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'ui' must be a mapping")
    defaults = UiConfig()
    return UiConfig(
        font=str(raw.get("font", defaults.font)),
        color=_valid_color(raw.get("color", defaults.color), "ui.color"),
        dim_color=_valid_color(raw.get("dim_color", defaults.dim_color), "ui.dim_color"),
        glow=bool(raw.get("glow", defaults.glow)),
        logo=bool(raw.get("logo", defaults.logo)),
        brand=str(raw.get("brand", defaults.brand)),
        now_playing=bool(raw.get("now_playing", defaults.now_playing)),
    )


def _parse_crt(raw: Any) -> CrtConfig:
    if raw is None:
        return CrtConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'crt' must be a mapping")
    d = CrtConfig()
    return CrtConfig(
        enabled=bool(raw.get("enabled", d.enabled)),
        max_height=_clamp_int(raw.get("max_height", d.max_height), 0, 4320, "crt.max_height"),
        curvature=_clamp_float(raw.get("curvature", d.curvature), 0.0, 0.5, "crt.curvature"),
        corner_radius=_clamp_float(raw.get("corner_radius", d.corner_radius), 0.0, 0.3, "crt.corner_radius"),
        vignette=_clamp_float(raw.get("vignette", d.vignette), 0.0, 1.0, "crt.vignette"),
        scanlines=bool(raw.get("scanlines", d.scanlines)),
        scanline_intensity=_clamp_float(
            raw.get("scanline_intensity", d.scanline_intensity), 0.0, 1.0, "crt.scanline_intensity"
        ),
    )


def _offset_range(data: Dict[str, Any]) -> tuple[float, float]:
    """Resolve the (min, max) start-offset seconds from the config.

    Accepts ``start_offset`` as a single number or a ``[min, max]`` list, or
    explicit ``start_offset_min`` / ``start_offset_max`` keys.
    """
    if "start_offset_min" in data or "start_offset_max" in data:
        lo = _clamp_float(data.get("start_offset_min", 0.0), 0.0, 3600.0, "start_offset_min")
        hi = _clamp_float(data.get("start_offset_max", lo), 0.0, 3600.0, "start_offset_max")
    else:
        raw = data.get("start_offset", [6.0, 10.0])
        if isinstance(raw, (list, tuple)):
            if not raw:
                raise ConfigError("'start_offset' list cannot be empty")
            lo = _clamp_float(raw[0], 0.0, 3600.0, "start_offset")
            hi = _clamp_float(raw[1] if len(raw) > 1 else raw[0], 0.0, 3600.0, "start_offset")
        else:
            lo = hi = _clamp_float(raw, 0.0, 3600.0, "start_offset")
    return (lo, max(lo, hi))


def _valid_player_backend(value: Any) -> str:
    s = str(value).strip().lower()
    if s not in PLAYER_BACKENDS:
        raise ConfigError(
            f"'player_backend' must be one of {PLAYER_BACKENDS}, got '{value}'"
        )
    return s


def _valid_transition(value: Any) -> str:
    s = str(value).strip().lower()
    if s not in TRANSITION_EFFECTS:
        raise ConfigError(f"'transition' must be one of {TRANSITION_EFFECTS}, got '{value}'")
    return s


def _valid_color(value: Any, name: str) -> str:
    """Validate a ``#RRGGBB`` hex colour string."""
    import re

    s = str(value).strip()
    if not re.fullmatch(r"#?[0-9a-fA-F]{6}", s):
        raise ConfigError(f"'{name}' must be a hex colour like '#4DFF5A', got '{value}'")
    return s if s.startswith("#") else f"#{s}"


def _ensure_unique_numbers(channels: List[ChannelConfig]) -> None:
    seen: Dict[int, str] = {}
    for ch in channels:
        if ch.number in seen:
            raise ConfigError(
                f"duplicate channel number {ch.number} used by "
                f"'{seen[ch.number]}' and '{ch.name}'"
            )
        seen[ch.number] = ch.name


def _clamp_int(value: Any, lo: int, hi: int, name: str) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"'{name}' must be an integer") from exc
    return max(lo, min(hi, n))


def _clamp_float(value: Any, lo: float, hi: float, name: str) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"'{name}' must be a number") from exc
    return max(lo, min(hi, n))


__all__ = [
    "Config",
    "ChannelConfig",
    "UiConfig",
    "CrtConfig",
    "BreaksConfig",
    "ConfigError",
    "load_config",
    "config_from_dict",
    "DEFAULT_VIDEO_EXTENSIONS",
    "TUNE_IN_MODES",
    "TRANSITION_EFFECTS",
    "PLAYER_BACKENDS",
]
