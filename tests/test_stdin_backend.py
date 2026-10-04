"""Terminal-reading tests for the stdin dev backend, driven over a real pty.

These exist because the interesting bugs here live in the *reading*, not in
the key map (tests/test_keymap.py already covers that). A terminal delivers an
arrow key as one three-byte burst, ``ESC [ A``, and any reader that leaves the
tail of that burst sitting in a buffer sees only a bare ESC - which the key map
turns into QUIT. So the box would shut down when you pressed "channel up".
"""

from __future__ import annotations

import os
import sys
import time
from queue import Empty, Queue

import pytest

from timewarptv.actions import Action
from timewarptv.input.stdin_backend import StdinBackend

pytestmark = pytest.mark.skipif(
    sys.platform.startswith("win"), reason="needs a POSIX pty"
)


@pytest.fixture
def terminal(monkeypatch):
    """Yield (write_keys, events) wired to a pty with the backend attached.

    ``sys.stdin`` is replaced by a genuine buffered text stream over the pty,
    not a stub, so that the buffering behaviour the backend has to cope with is
    the real thing.
    """
    master, slave = os.openpty()
    monkeypatch.setattr(sys, "stdin", open(slave, "r"))
    queue: "Queue" = Queue()
    backend = StdinBackend()
    backend.start(queue)
    time.sleep(0.2)  # let the reader thread reach cbreak mode

    def write_keys(data: bytes) -> None:
        os.write(master, data)

    def events(count: int, timeout: float = 2.0):
        out = []
        for _ in range(count):
            try:
                out.append(queue.get(timeout=timeout))
            except Empty:
                break
        return out

    try:
        yield write_keys, events
    finally:
        backend.stop()
        sys.stdin.close()  # owns and closes the pty slave
        os.close(master)


def test_arrow_key_burst_is_not_read_as_escape(terminal):
    """The whole ESC [ A must be consumed as one arrow, not as ESC + junk."""
    write_keys, events = terminal
    write_keys(b"\x1b[A")

    got = events(1)
    assert [e.action for e in got] == [Action.CHANNEL_UP]


def test_all_four_arrows_back_to_back(terminal):
    write_keys, events = terminal
    write_keys(b"\x1b[A\x1b[B\x1b[C\x1b[D")

    got = events(4)
    assert [e.action for e in got] == [
        Action.CHANNEL_UP,
        Action.CHANNEL_DOWN,
        Action.VOLUME_UP,
        Action.VOLUME_DOWN,
    ]


def test_plain_keys_still_work(terminal):
    write_keys, events = terminal
    write_keys(b"m")
    write_keys(b"7")

    got = events(2)
    assert [e.action for e in got] == [Action.MUTE, Action.DIGIT]
    assert got[1].value == 7


def test_bare_escape_still_quits(terminal):
    """A lone ESC (no sequence follows) keeps its old meaning: quit."""
    write_keys, events = terminal
    write_keys(b"\x1b")

    got = events(1)
    assert [e.action for e in got] == [Action.QUIT]
