"""The appliance's re-index trigger: notice the drive leaving or changing.

The box is carried between rooms and new shows are added on another computer,
so "the line-up is stale" has to be detected without anyone at a terminal.
"""

from __future__ import annotations

import os

from timewarptv.media_watch import MediaWatch, wait_for_file
from tests.helpers import FakeClock


def _drive(tmp_path):
    (tmp_path / "02-playhouse").mkdir()
    (tmp_path / "05-kids").mkdir()
    cfg = tmp_path / "config.yaml"
    cfg.write_text("channels: []\n")
    return cfg, [tmp_path / "02-playhouse", tmp_path / "05-kids"]


def test_quiet_while_nothing_changes(tmp_path):
    cfg, folders = _drive(tmp_path)
    clock = FakeClock()
    watch = MediaWatch(cfg, folders, clock=clock)

    for _ in range(5):
        clock.advance(10)
        assert watch.changed() is None


def test_drive_unplugged_is_noticed(tmp_path):
    cfg, folders = _drive(tmp_path)
    clock = FakeClock()
    watch = MediaWatch(cfg, folders, clock=clock)

    cfg.unlink()                          # what an unmount looks like to us
    clock.advance(10)

    assert "gone" in watch.changed()


def test_config_edited_is_noticed(tmp_path):
    cfg, folders = _drive(tmp_path)
    clock = FakeClock()
    watch = MediaWatch(cfg, folders, clock=clock)

    stat = cfg.stat()
    os.utime(cfg, (stat.st_atime, stat.st_mtime + 60))
    clock.advance(10)

    assert "changed" in watch.changed()


def test_channel_folder_removed_is_noticed(tmp_path):
    cfg, folders = _drive(tmp_path)
    clock = FakeClock()
    watch = MediaWatch(cfg, folders, clock=clock)

    folders[1].rmdir()
    clock.advance(10)

    assert "05-kids" in watch.changed()


def test_a_folder_missing_from_the_start_is_not_news(tmp_path):
    """Otherwise a typo'd channel path would restart the box every 5 seconds."""
    cfg, folders = _drive(tmp_path)
    clock = FakeClock()
    watch = MediaWatch(cfg, folders + [tmp_path / "99-typo"], clock=clock)

    clock.advance(10)

    assert watch.changed() is None


def test_checks_are_rate_limited(tmp_path):
    """Called every main-loop step (10x a second); must not stat that often."""
    cfg, folders = _drive(tmp_path)
    clock = FakeClock()
    watch = MediaWatch(cfg, folders, clock=clock, interval=5.0)
    cfg.unlink()

    clock.advance(1)
    assert watch.changed() is None        # too soon to look
    clock.advance(5)
    assert watch.changed() is not None


def test_wait_for_file_returns_at_once_when_present(tmp_path):
    cfg, _ = _drive(tmp_path)
    shown = []

    wait_for_file(cfg, sleep=lambda s: None, on_first_miss=lambda: shown.append(1))

    assert shown == []                    # no "connect the drive" card


def test_wait_for_file_shows_the_card_once_then_waits(tmp_path):
    target = tmp_path / "config.yaml"
    shown, polls = [], []

    def sleep(_seconds):
        polls.append(1)
        if len(polls) == 3:               # "plug the drive in" on the 3rd poll
            target.write_text("channels: []\n")

    wait_for_file(target, sleep=sleep, on_first_miss=lambda: shown.append(1))

    assert shown == [1]
    assert len(polls) == 3
