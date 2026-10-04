"""Keyboard / USB / IR remote input via Linux evdev.

Most cheap "media remotes" (and IR remotes bridged through a USB receiver or
LIRC's uinput) show up to Linux as ordinary keyboard-like input devices. This
backend reads their key-down events straight from ``/dev/input/event*`` using
the ``evdev`` package - no X server or desktop required, which is exactly what
we want on a headless Pi wired to a TV.
"""

from __future__ import annotations

import logging
import select
import time
from typing import Dict, List, Optional, Sequence

from ..actions import InputEvent
from .base import InputBackend
from .keymap import evdev_key_to_event

log = logging.getLogger(__name__)

# How often to look for remotes/keyboards plugged in since start-up.
RESCAN_SECONDS = 2.0

# Key-event values reported by evdev: 0=up, 1=down, 2=autorepeat.
_KEY_DOWN = 1
_KEY_REPEAT = 2


class KeyboardBackend(InputBackend):
    """Reads remote/keyboard events from evdev input devices."""

    name = "keyboard"

    def __init__(
        self,
        *,
        device_paths: Optional[Sequence[str]] = None,
        name_filter: Optional[str] = None,
        grab: bool = False,
        allow_repeat: bool = True,
        overrides: Optional[Dict[str, Optional[InputEvent]]] = None,
    ) -> None:
        super().__init__()
        self._device_paths = list(device_paths) if device_paths else None
        self._name_filter = name_filter.lower() if name_filter else None
        self._grab = grab
        self._allow_repeat = allow_repeat
        # Per-key action overrides from config (key name -> InputEvent or None).
        self._overrides = dict(overrides or {})
        self._devices: List = []
        self._warned: set = set()  # paths already warned about, so rescans stay quiet

    def _lookup(self, key_name: str) -> Optional[InputEvent]:
        """Config overrides win over the built-in defaults."""
        if key_name in self._overrides:
            return self._overrides[key_name]  # may be None (explicitly unbound)
        return evdev_key_to_event(key_name)

    @staticmethod
    def is_available() -> bool:
        try:
            import evdev  # noqa: F401
        except ImportError:
            return False
        return True

    def _open_devices(self, skip: frozenset = frozenset()):
        """Open every usable key-sending device not already open (``skip``)."""
        import evdev
        from evdev import ecodes

        paths = self._device_paths or evdev.list_devices()
        devices = []
        for path in paths:
            if path in skip:
                continue
            try:
                dev = evdev.InputDevice(path)
            except (OSError, PermissionError) as exc:
                if path not in self._warned:
                    log.warning("cannot open input device %s: %s", path, exc)
                    self._warned.add(path)
                continue
            caps = dev.capabilities()
            if ecodes.EV_KEY not in caps:
                dev.close()
                continue
            if self._name_filter and self._name_filter not in (dev.name or "").lower():
                dev.close()
                continue
            if self._grab:
                try:
                    dev.grab()
                except OSError:
                    log.warning("could not grab %s (continuing ungrabbed)", dev.name)
            log.info("listening to input device: %s (%s)", dev.name, path)
            devices.append(dev)
        return devices

    def _run(self) -> None:
        if not self.is_available():
            log.error("evdev is not installed; keyboard/remote input disabled")
            return
        from evdev import ecodes

        # Devices are looked for again every couple of seconds, not just at
        # start-up: a remote receiver plugged in after the box is on, pulled out
        # and put back, or reset by a USB brown-out must start working again by
        # itself - otherwise the remote is dead until the next reboot.
        fd_to_device: Dict = {}
        next_scan = 0.0
        waiting_logged = False
        while not self.stopping:
            now = time.monotonic()
            if now >= next_scan:
                known = frozenset(getattr(d, "path", None) for d in fd_to_device.values())
                for dev in self._open_devices(skip=known):
                    fd_to_device[dev.fd] = dev
                self._devices = list(fd_to_device.values())
                next_scan = now + RESCAN_SECONDS
                if not fd_to_device and not waiting_logged:
                    log.warning("no remote or keyboard found yet; will keep looking")
                    waiting_logged = True
                elif fd_to_device:
                    waiting_logged = False
            if not fd_to_device:
                self._stop.wait(0.5)
                continue
            try:
                r, _, _ = select.select(list(fd_to_device), [], [], 0.5)
            except (OSError, ValueError):
                # A device vanished between the scan and the select; drop any
                # that are no longer readable and carry on.
                for fd, dev in list(fd_to_device.items()):
                    if getattr(dev, "fd", -1) < 0:
                        fd_to_device.pop(fd, None)
                continue
            for fd in r:
                dev = fd_to_device.get(fd)
                if dev is None:
                    continue
                try:
                    for event in dev.read():
                        if event.type != ecodes.EV_KEY:
                            continue
                        self._handle_key_event(event)
                except OSError:
                    log.warning("input device %s disappeared", getattr(dev, "path", "?"))
                    fd_to_device.pop(fd, None)
                    try:
                        dev.close()
                    except Exception:  # noqa: BLE001
                        pass

    def _handle_key_event(self, event) -> None:
        from evdev import ecodes

        if event.value == _KEY_DOWN:
            pass
        elif event.value == _KEY_REPEAT and self._allow_repeat:
            pass
        else:
            return  # key-up, or repeats when disabled

        key_name = _code_to_name(ecodes.KEY, event.code)
        if key_name is None:
            return
        input_event = self._lookup(key_name)
        if input_event is None:
            return
        # Only volume/channel keys should auto-repeat when held; ignore repeats
        # for digits, enter, power, etc. so a held button doesn't misbehave.
        from ..actions import Action

        if event.value == _KEY_REPEAT and input_event.action not in (
            Action.VOLUME_UP,
            Action.VOLUME_DOWN,
            Action.CHANNEL_UP,
            Action.CHANNEL_DOWN,
        ):
            return
        self.emit(input_event)

    def _close(self) -> None:
        for dev in self._devices:
            try:
                if self._grab:
                    dev.ungrab()
            except OSError:
                pass
            try:
                dev.close()
            except OSError:
                pass
        self._devices = []


def _code_to_name(key_table, code: int) -> Optional[str]:
    """evdev's KEY table maps a code to a name or a list of aliases."""
    name = key_table.get(code)
    if name is None:
        return None
    if isinstance(name, (list, tuple)):
        return name[0] if name else None
    return name


__all__ = ["KeyboardBackend"]
