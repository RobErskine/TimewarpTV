"""The remote receiver (a Flirc) must work whenever it's plugged in - at boot,
after boot, or after being pulled out and put back. evdev is Linux-only, so a
fake stands in; its devices are real OS pipes so select() behaves for real."""

from __future__ import annotations

import os
import sys
import time
import types
from queue import Empty, Queue

import pytest

import timewarptv.input.keyboard as keyboard
from timewarptv.actions import Action

EV_KEY, KEY_ENTER, KEY_UP = 1, 28, 103


class _Event:
    def __init__(self, code):
        self.type, self.code, self.value = EV_KEY, code, 1


class _Device:
    def __init__(self, path):
        self.path, self.name = path, "flirc.tv flirc Keyboard"
        self.fd, self._w = os.pipe()
        self._pending, self.gone = [], False

    def capabilities(self):
        return {EV_KEY: [KEY_ENTER, KEY_UP]}

    def read(self):
        if self.gone:
            raise OSError("No such device")
        os.read(self.fd, 1024)
        events, self._pending = self._pending, []
        return events

    def press(self, code):
        self._pending.append(_Event(code))
        os.write(self._w, b"x")

    def unplug(self):
        self.gone = True
        os.write(self._w, b"x")  # wake the reader, which then finds it gone

    def grab(self):
        pass

    def close(self):
        for fd in (self.fd, self._w):
            try:
                os.close(fd)
            except OSError:
                pass


@pytest.fixture
def usb(monkeypatch):
    """/dev/input as the fake evdev sees it: plug() and unplug() devices."""
    plugged, opened = {}, {}

    def open_device(path):
        if path not in plugged:
            raise OSError("No such device")
        dev = _Device(path)
        opened[path] = dev
        return dev

    fake = types.ModuleType("evdev")
    fake.list_devices = lambda: list(plugged)
    fake.InputDevice = open_device
    fake.ecodes = types.SimpleNamespace(EV_KEY=EV_KEY, KEY={KEY_ENTER: "KEY_ENTER", KEY_UP: "KEY_UP"})
    monkeypatch.setitem(sys.modules, "evdev", fake)
    monkeypatch.setattr(keyboard, "RESCAN_SECONDS", 0.05, raising=False)

    class Bus:
        def plug(self, path="/dev/input/event2"):
            plugged[path] = True

        def unplug(self, path="/dev/input/event2"):
            plugged.pop(path, None)
            if path in opened:
                opened[path].unplug()

        def device(self, path="/dev/input/event2"):
            return opened.get(path)

    return Bus()


def _started(queue):
    backend = keyboard.KeyboardBackend()
    backend.start(queue)
    return backend


def _press_and_expect(bus, queue, code, action, timeout=2.0):
    """Press until the backend has the device open and delivers the event."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        dev = bus.device()
        if dev is not None and not dev.gone:
            dev.press(code)
            try:
                return queue.get(timeout=0.2).action == action
            except Empty:
                pass
        time.sleep(0.05)
    return False


def test_a_remote_plugged_in_after_start_up_works(usb):
    queue = Queue()
    backend = _started(queue)                     # nothing plugged in yet
    try:
        time.sleep(0.2)
        usb.plug()
        assert _press_and_expect(usb, queue, KEY_ENTER, Action.ENTER)
    finally:
        backend.stop()


def test_a_remote_pulled_out_and_put_back_works_again(usb):
    queue = Queue()
    usb.plug()
    backend = _started(queue)
    try:
        assert _press_and_expect(usb, queue, KEY_UP, Action.CHANNEL_UP)

        usb.unplug()                              # e.g. to reprogram it on a Mac
        time.sleep(0.2)
        usb.plug()

        assert _press_and_expect(usb, queue, KEY_ENTER, Action.ENTER)
    finally:
        backend.stop()
