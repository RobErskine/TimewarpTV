"""Command-line entry point: ``timewarptv`` (or ``python -m timewarptv``)."""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import replace
from pathlib import Path
from typing import List, Optional

from . import __version__
from .channel import build_lineup
from .config import Config, ConfigError, load_config

log = logging.getLogger("timewarptv")

# Places we look for a config file when one isn't given explicitly.
_DEFAULT_CONFIG_LOCATIONS = (
    Path("config.yaml"),
    # The media drive, where an installed box keeps its config (see README).
    Path("/media/timewarptv/config.yaml"),
    Path.home() / ".config" / "timewarptv" / "config.yaml",
    Path("/etc/timewarptv/config.yaml"),
)


def _find_config(explicit: Optional[str]) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    for candidate in _DEFAULT_CONFIG_LOCATIONS:
        if candidate.is_file():
            return candidate
    raise ConfigError(
        "no config file found. Pass --config PATH or create config.yaml "
        "(see config.example.yaml)."
    )


def _cmd_check(config: Config) -> int:
    """Validate the config and print the resulting channel lineup."""
    # Surface bad key_overrides here so typos are caught before running.
    from .input.keymap import parse_key_overrides

    try:
        overrides = parse_key_overrides(config.input_options.get("key_overrides"))
    except ValueError as exc:
        print(f"configuration error: {exc}")
        return 2

    lineup = build_lineup(config)
    print(f"TimewarpTV v{__version__} - configuration OK")
    print(f"tune-in mode: {config.tune_in}")
    if overrides:
        print(f"key overrides: {len(overrides)} configured")
    print(f"channels ({len(lineup)}):")
    total = 0
    for channel in lineup:
        count = len(channel.episodes)
        total += count
        flag = "" if count else "   <-- NO EPISODES FOUND"
        tags = []
        if channel.config.passcode is not None:
            tags.append("locked")
        if channel.tune_in_mode == "resume":
            tags.append("resume")
        if channel.break_info is not None:
            n_clips, every = channel.break_info
            tags.append(f"breaks: {n_clips} clips every {every}")
        suffix = f"  ({', '.join(tags)})" if tags else ""
        print(f"  CH {channel.number:>3}  {channel.name:<28} {count:>4} episodes{flag}{suffix}")
    print(f"total episodes: {total}")
    return 0 if total > 0 else 1


def _list_audio_devices() -> int:
    """Print mpv's available audio output devices, one 'name  -  description' per line."""
    try:
        import mpv  # type: ignore
    except ImportError:
        print("python-mpv/libmpv not installed; on the Pi try: mpv --audio-device=help")
        return 1
    try:
        player = mpv.MPV(vo="null", idle=True)
        devices = player.audio_device_list or []
        print("Available audio devices (use the 'name' in config.yaml -> audio_device):\n")
        for dev in devices:
            name = dev.get("name", "?")
            desc = dev.get("description", "")
            print(f"  {name}\n      {desc}")
        print("\nFor a TV, pick an HDMI one (\"vc4hdmi\" on a Raspberry Pi), e.g.\n"
              "  audio_device: \"alsa/default:CARD=vc4hdmi\"")
        player.terminate()
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"could not list audio devices via libmpv ({exc}).")
        print("Try on the Pi instead: mpv --audio-device=help")
        return 1


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="timewarptv",
        description="TimewarpTV - a retro TV media player for a Raspberry Pi.",
    )
    parser.add_argument("-c", "--config", help="path to the YAML config file")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run without real hardware (mock player + keyboard/stdin control)",
    )
    parser.add_argument(
        "--windowed",
        action="store_true",
        help="run mpv in a window instead of fullscreen (handy on a dev machine)",
    )
    parser.add_argument(
        "--hotplug",
        action="store_true",
        help="appliance mode: wait for the media drive (and its config) if it is "
        "missing, and exit so the service restarts with a fresh scan whenever the "
        "drive is unplugged or its config is edited",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate the config, list channels/episodes, and exit",
    )
    parser.add_argument(
        "--make-guide",
        action="store_true",
        help="render the channel-1 guide card from the config, into "
        "<config folder>/01-guide/welcome.mp4, and exit",
    )
    parser.add_argument(
        "--generate-assets",
        action="store_true",
        help="generate the static/colour-bars filler clips and exit",
    )
    parser.add_argument(
        "--list-audio",
        action="store_true",
        help="list available audio output devices (for the 'audio_device' setting) and exit",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="logging verbosity (default: INFO)",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.generate_assets:
        from .static_gen import DEFAULT_ASSETS_DIR, main as gen_main

        return gen_main(["--assets-dir", str(DEFAULT_ASSETS_DIR)])

    if args.list_audio:
        return _list_audio_devices()

    if args.hotplug and args.config and not args.check:
        from .app import wait_for_media

        wait_for_media(
            Path(args.config).expanduser(),
            fullscreen=not args.windowed,
            dry_run=args.dry_run,
        )

    try:
        config_path = _find_config(args.config)
        log.info("loading config: %s", config_path)
        config = load_config(config_path)
    except ConfigError as exc:
        log.error("%s", exc)
        return 2

    if args.windowed:
        config = replace(config, fullscreen=False)

    if args.check:
        return _cmd_check(config)

    if args.make_guide:
        from .guide_gen import GUIDE_FILENAME, generate_guide

        out = config_path.resolve().parent / "01-guide" / GUIDE_FILENAME
        generate_guide(config, out)
        print(f"wrote {out}")
        return 0

    from .app import run_from_config

    try:
        run_from_config(
            config,
            dry_run=args.dry_run,
            config_path=config_path,
            hotplug=args.hotplug,
        )
    except RuntimeError as exc:
        log.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
