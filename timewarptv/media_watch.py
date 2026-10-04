"""Notice the media drive going away, or its config changing, while on the air.

The box is meant to be carried between rooms and houses, and new shows are
added by taking the drive to another computer. Neither should ever need a
terminal. So when running as an appliance (``--hotplug``) the app watches the
things it built its line-up from - the config file and the channel folders -
and when any of them vanishes or the config is edited, it simply stops.
systemd restarts it a few seconds later, and a fresh start re-reads the config
and re-scans every folder. That is the whole re-index mechanism: no partial
reload, no second code path, just the start-up that already works.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

log = logging.getLogger(__name__)


class MediaWatch:
    """Cheap, rate-limited check of whether the line-up is still valid."""

    def __init__(
        self,
        config_path: Path,
        folders: Iterable[Path],
        *,
        interval: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config_path = Path(config_path)
        self._config_mtime = _mtime(self._config_path)
        # Only folders that existed at start-up: a channel that was already
        # missing is not news, and must not cause a restart loop.
        self._folders = [Path(f) for f in folders if Path(f).is_dir()]
        self._interval = interval
        self._clock = clock
        self._next_check = clock() + interval

    def changed(self) -> Optional[str]:
        """Why the line-up is stale, or None. At most one real check per interval."""
        now = self._clock()
        if now < self._next_check:
            return None
        self._next_check = now + self._interval

        mtime = _mtime(self._config_path)
        if mtime is None:
            return f"config file is gone ({self._config_path}) - drive unplugged?"
        if mtime != self._config_mtime:
            return f"config file changed ({self._config_path})"
        for folder in self._folders:
            if not folder.is_dir():
                return f"channel folder is gone ({folder})"
        return None


def wait_for_file(
    path: Path,
    *,
    poll: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
    on_first_miss: Optional[Callable[[], None]] = None,
) -> None:
    """Block until ``path`` exists. ``on_first_miss`` runs once if it didn't."""
    path = Path(path)
    if path.is_file():
        return
    log.info("waiting for %s (is the media drive connected?)", path)
    if on_first_miss is not None:
        on_first_miss()
    while not path.is_file():
        sleep(poll)
    log.info("found %s", path)


def _mtime(path: Path) -> Optional[float]:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


__all__ = ["MediaWatch", "wait_for_file"]
