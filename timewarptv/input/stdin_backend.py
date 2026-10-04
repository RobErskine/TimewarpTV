"""Developer keyboard input over stdin (raw terminal mode).

This backend has nothing to do with the Pi/TV; it exists so you can drive the
whole application from a terminal while developing - arrow keys, digits, ``+``/
``-`` for volume, ``m`` to mute, ``i`` for info, ``q`` to quit. It puts the
terminal into cbreak mode so single keystrokes come through immediately, and
restores it on exit.
"""

from __future__ import annotations

import logging
import os
import select
import sys
from typing import Optional

from .base import InputBackend
from .keymap import stdin_char_to_event, stdin_escape_to_event

log = logging.getLogger(__name__)


class StdinBackend(InputBackend):
    """Reads single keystrokes from a terminal for local development."""

    name = "stdin"

    def __init__(self) -> None:
        super().__init__()
        self._fd: Optional[int] = None
        self._old_settings = None
        self._pending = ""

    @staticmethod
    def is_available() -> bool:
        try:
            return sys.stdin.isatty()
        except (ValueError, OSError):
            return False

    def _run(self) -> None:
        if not self.is_available():
            log.info("stdin is not a TTY; keyboard (stdin) input disabled")
            return
        import termios
        import tty

        self._fd = sys.stdin.fileno()
        self._old_settings = termios.tcgetattr(self._fd)
        tty.setcbreak(self._fd)
        log.info("stdin input active (arrows=chan/vol, digits, m, i, l, p, q)")
        try:
            while not self.stopping:
                ch = self._next_char(0.3)
                if ch:
                    self._handle_char(ch)
        finally:
            self._restore()

    def _handle_char(self, ch: str) -> None:
        if ch == "\x1b":  # ESC - could be a bare ESC or an arrow-key sequence
            seq = self._read_escape_sequence()
            if seq:
                event = stdin_escape_to_event(seq)
                if event is not None:
                    self.emit(event)
                return
            # Bare ESC -> quit
        event = stdin_char_to_event(ch)
        if event is not None:
            self.emit(event)

    def _read_escape_sequence(self) -> str:
        """After an ESC, non-blockingly grab up to two more chars (e.g. ``[A``)."""
        seq = ""
        for _ in range(2):
            ch = self._next_char(0.02)
            if not ch:
                break
            seq += ch
        return seq

    def _next_char(self, timeout: float) -> str:
        """Return one keystroke, or "" if none arrives within ``timeout``.

        Reads straight from the file descriptor with :func:`os.read` rather
        than from ``sys.stdin``. Buffered text I/O would swallow the whole
        burst: an arrow key arrives as the three bytes ``ESC [ A`` at once,
        ``sys.stdin.read(1)`` pulls all three into Python's own buffer, and the
        following ``select()`` on the descriptor then reports "nothing there".
        The arrow would look like a bare ESC - which means quit.
        """
        if not self._pending:
            fd = self._fd
            if fd is None:
                return ""
            r, _, _ = select.select([fd], [], [], timeout)
            if not r:
                return ""
            try:
                data = os.read(fd, 64)
            except OSError:
                return ""
            if not data:
                return ""
            self._pending = data.decode("utf-8", "replace")
        ch, self._pending = self._pending[0], self._pending[1:]
        return ch

    def _close(self) -> None:
        self._restore()

    def _restore(self) -> None:
        if self._fd is not None and self._old_settings is not None:
            import termios

            try:
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old_settings)
            except (termios.error, OSError):
                pass
            self._old_settings = None


__all__ = ["StdinBackend"]
