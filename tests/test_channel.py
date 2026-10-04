import random

from timewarptv.channel import (
    BroadcastSchedule,
    Channel,
    ChannelLineup,
    build_lineup,
    detect_season,
    scan_episodes,
)
from timewarptv.config import BreaksConfig, ChannelConfig, config_from_dict
from tests.helpers import make_show


def _channel(tmp_path, name="arthur", episodes=4, *, ch_cfg=None, **kw):
    folder = make_show(tmp_path, name, episodes)
    cfg = ch_cfg or ChannelConfig(number=kw.pop("number", 3), name=name, path=folder)
    eps = scan_episodes(folder, [".mp4"])
    return Channel(cfg, eps, rng=random.Random(0), **kw)


def test_scan_episodes_sorted_and_filtered(tmp_path):
    folder = make_show(tmp_path, "arthur", 3)
    (folder / "notes.txt").write_text("nope")
    (folder / ".DS_Store").write_bytes(b"")
    eps = scan_episodes(folder, [".mp4"])
    assert [p.name for p in eps] == [
        "arthur_ep01.mp4",
        "arthur_ep02.mp4",
        "arthur_ep03.mp4",
    ]


def test_detect_season():
    assert detect_season("Arthur S06E01.mp4") == 6
    assert detect_season("arthur.s6e12.mkv") == 6
    assert detect_season("Season 12/ep03.mp4") == 12
    assert detect_season("Arthur 6x05.mp4") == 6
    assert detect_season("Arthurs Perfect Christmas.mp4") is None


def test_scan_exclude_globs(tmp_path):
    folder = tmp_path / "arthur"
    (folder / "Season 1").mkdir(parents=True)
    (folder / "Specials").mkdir(parents=True)
    (folder / "Season 1" / "S01E01.mp4").write_bytes(b"")
    (folder / "Specials" / "Arthur Special.mp4").write_bytes(b"")
    eps = scan_episodes(folder, [".mp4"], exclude=["*special*"])
    names = [p.name for p in eps]
    assert names == ["S01E01.mp4"]


def test_scan_exclude_seasons(tmp_path):
    folder = tmp_path / "arthur"
    folder.mkdir()
    for s in (1, 5, 6, 7, 25):
        (folder / f"Arthur S{s:02d}E01.mp4").write_bytes(b"")
    eps = scan_episodes(folder, [".mp4"], exclude_seasons=set(range(6, 26)))
    seasons = sorted(detect_season(p.name) for p in eps)
    assert seasons == [1, 5]  # 6..25 removed


def test_build_lineup_applies_channel_excludes(tmp_path):
    folder = tmp_path / "arthur"
    folder.mkdir()
    (folder / "Arthur S01E01.mp4").write_bytes(b"")
    (folder / "Arthur S06E01.mp4").write_bytes(b"")
    (folder / "Arthur Special.mp4").write_bytes(b"")
    cfg = config_from_dict(
        {
            "channels": [
                {
                    "number": 3,
                    "name": "Arthur",
                    "path": str(folder),
                    "exclude": ["*special*"],
                    "exclude_seasons": ["6-25"],
                }
            ]
        }
    )
    lineup = build_lineup(cfg)
    eps = list(lineup)[0].episodes
    assert [p.name for p in eps] == ["Arthur S01E01.mp4"]


def test_scan_recursive(tmp_path):
    base = tmp_path / "show"
    (base / "season1").mkdir(parents=True)
    (base / "season2").mkdir(parents=True)
    (base / "season1" / "a.mp4").write_bytes(b"")
    (base / "season2" / "b.mp4").write_bytes(b"")
    assert len(scan_episodes(base, [".mp4"], recursive=True)) == 2
    assert len(scan_episodes(base, [".mp4"], recursive=False)) == 0


def test_tune_in_random_plays_from_start(tmp_path):
    ch = _channel(tmp_path, tune_in="random")
    req = ch.tune_in()
    assert req is not None
    assert req.start == 0.0
    assert req.path in ch.episodes


def test_advance_continues_shuffle(tmp_path):
    ch = _channel(tmp_path, episodes=4, tune_in="random")
    seen = {ch.tune_in().path}
    for _ in range(3):
        seen.add(ch.advance().path)
    assert len(seen) == 4  # every episode shown before repeats


def test_start_offset_fixed(tmp_path):
    ch = _channel(tmp_path, tune_in="random", start_offset_min=5.0, start_offset_max=5.0)
    assert ch.tune_in().start == 5.0
    assert ch.advance().start == 5.0


def test_start_offset_range(tmp_path):
    ch = _channel(tmp_path, tune_in="random", start_offset_min=6.0, start_offset_max=10.0)
    starts = [ch.tune_in().start for _ in range(20)] + [ch.advance().start for _ in range(20)]
    assert all(6.0 <= s <= 10.0 for s in starts)
    assert len(set(round(s, 3) for s in starts)) > 1  # actually varies


def test_resume_mode_remembers_position(tmp_path):
    ch = _channel(tmp_path, tune_in="resume")
    first = ch.tune_in()
    ch.remember(first.path, 123.5)
    again = ch.tune_in()
    assert again.path == first.path
    assert again.start == 123.5


def test_empty_channel_returns_none(tmp_path):
    folder = tmp_path / "empty"
    folder.mkdir()
    from timewarptv.config import ChannelConfig

    ch = Channel(ChannelConfig(number=9, name="Empty", path=folder), [])
    assert ch.is_empty
    assert ch.tune_in() is None
    assert ch.advance() is None


def test_broadcast_schedule_positions():
    from pathlib import Path

    eps = [Path("a.mp4"), Path("b.mp4"), Path("c.mp4")]
    durs = [100.0, 200.0, 300.0]
    sched = BroadcastSchedule(eps, durs, epoch=0.0, rng=random.Random(0))
    # At t=0 we are at the start of the first item in the (shuffled) order.
    first = sched.at(0.0)
    assert first.start == 0.0
    # The schedule is a loop of total length 600s; t=600 == t=0.
    assert sched.at(600.0).path == first.path
    # 50s into the cycle we should still be within the first item, offset 50.
    assert sched.at(50.0).start == 50.0


def test_broadcast_tune_in_uses_real_time(tmp_path, monkeypatch):
    # Force probe_duration to a known value so we don't need ffprobe/real media.
    import timewarptv.channel as channel_mod

    monkeypatch.setattr(channel_mod, "probe_duration", lambda p: 60.0)
    ch = _channel(tmp_path, episodes=3, tune_in="broadcast")
    # Two tune-ins at different times should generally land at different offsets.
    r1 = ch.tune_in(now=0.0)
    r2 = ch.tune_in(now=30.0)
    assert r1.start == 0.0
    assert r2.start == 30.0


def test_lineup_navigation(tmp_path):
    for n in ("a", "b", "c"):
        make_show(tmp_path, n, 1)
    cfg = config_from_dict(
        {
            "shuffle_seed": 1,
            "channels": [
                {"number": 2, "name": "A", "path": str(tmp_path / "a")},
                {"number": 4, "name": "B", "path": str(tmp_path / "b")},
                {"number": 7, "name": "C", "path": str(tmp_path / "c")},
            ],
        }
    )
    lineup = build_lineup(cfg)
    assert lineup.numbers == [2, 4, 7]
    assert lineup.current.number == 2
    assert lineup.up().number == 4
    assert lineup.up().number == 7
    assert lineup.up().number == 2  # wraps
    assert lineup.down().number == 7  # wraps back
    assert lineup.select_number(4).number == 4
    assert lineup.select_number(99) is None
    assert lineup.has_number(7)


def test_lineup_sorted_by_number(tmp_path):
    for n in ("a", "b"):
        make_show(tmp_path, n, 1)
    cfg = config_from_dict(
        {
            "channels": [
                {"number": 9, "name": "Nine", "path": str(tmp_path / "a")},
                {"number": 3, "name": "Three", "path": str(tmp_path / "b")},
            ]
        }
    )
    lineup = build_lineup(cfg)
    assert lineup.numbers == [3, 9]


# -- passcode lock ----------------------------------------------------------
def test_locked_channel_tune_in_returns_none(tmp_path):
    folder = make_show(tmp_path, "adultswim", 3)
    ch_cfg = ChannelConfig(number=9, name="Adult Swim", path=folder, passcode="1997")
    ch = _channel(tmp_path, "adultswim", 3, ch_cfg=ch_cfg)
    assert ch.locked is True
    assert ch.tune_in() is None


def test_unlock_with_wrong_code_stays_locked(tmp_path):
    folder = make_show(tmp_path, "adultswim", 3)
    ch_cfg = ChannelConfig(number=9, name="Adult Swim", path=folder, passcode="1997")
    ch = _channel(tmp_path, "adultswim", 3, ch_cfg=ch_cfg)
    assert ch.unlock("0000") is False
    assert ch.locked is True
    assert ch.tune_in() is None


def test_unlock_with_correct_code_plays(tmp_path):
    folder = make_show(tmp_path, "adultswim", 3)
    ch_cfg = ChannelConfig(number=9, name="Adult Swim", path=folder, passcode="1997")
    ch = _channel(tmp_path, "adultswim", 3, ch_cfg=ch_cfg)
    assert ch.unlock("1997") is True
    assert ch.locked is False
    assert ch.tune_in() is not None


def test_relock_locks_again(tmp_path):
    folder = make_show(tmp_path, "adultswim", 3)
    ch_cfg = ChannelConfig(number=9, name="Adult Swim", path=folder, passcode="1997")
    ch = _channel(tmp_path, "adultswim", 3, ch_cfg=ch_cfg)
    ch.unlock("1997")
    ch.relock()
    assert ch.locked is True
    assert ch.tune_in() is None


def test_unlocked_channel_ignores_relock(tmp_path):
    ch = _channel(tmp_path)  # no passcode configured
    ch.relock()
    assert ch.locked is False
    assert ch.tune_in() is not None


def test_locked_channel_stays_in_lineup(tmp_path):
    make_show(tmp_path, "a", 1)
    make_show(tmp_path, "b", 1)
    cfg = config_from_dict(
        {
            "channels": [
                {"number": 2, "name": "A", "path": str(tmp_path / "a")},
                {"number": 9, "name": "B", "path": str(tmp_path / "b"), "passcode": "1234"},
            ]
        }
    )
    lineup = build_lineup(cfg)
    assert lineup.numbers == [2, 9]
    assert lineup.up().number == 9
    assert lineup.select_number(9) is not None
    assert lineup.select_number(9).tune_in() is None  # still locked


# -- per-channel tune_in / start_offset override -----------------------------
def test_build_lineup_honours_per_channel_tune_in_and_offset(tmp_path):
    make_show(tmp_path, "a", 2)
    make_show(tmp_path, "b", 2)
    cfg = config_from_dict(
        {
            "tune_in": "random",
            "start_offset": [6, 10],
            "channels": [
                {
                    "number": 2,
                    "name": "A",
                    "path": str(tmp_path / "a"),
                    "tune_in": "resume",
                    "start_offset": 0,
                },
                {"number": 3, "name": "B", "path": str(tmp_path / "b")},
            ],
        }
    )
    lineup = build_lineup(cfg)
    a = lineup.select_number(2)
    b = lineup.select_number(3)
    assert a.tune_in_mode == "resume"
    assert (a.start_offset_min, a.start_offset_max) == (0.0, 0.0)
    assert b.tune_in_mode == "random"
    assert (b.start_offset_min, b.start_offset_max) == (6.0, 10.0)


# -- resume bug fix: advance() must not leave a stale resume position --------
def test_advance_tracks_new_episode_on_resume_channel(tmp_path):
    ch = _channel(tmp_path, episodes=4, tune_in="resume")
    first = ch.tune_in()
    assert first.resumed is False  # nothing remembered yet
    # Simulate watching to the end without an explicit remember() (which only
    # happens on a channel change in the app) - advance() must track the new
    # episode itself, not leave the resume pointer stale.
    second = ch.advance()
    assert ch.resume_snapshot == (second.path, second.start)
    again = ch.tune_in()
    assert again.path == second.path
    assert again.resumed is True


def test_forget_resume_clears_position(tmp_path):
    ch = _channel(tmp_path, tune_in="resume")
    first = ch.tune_in()
    ch.remember(first.path, 99.0)
    ch.forget_resume()
    assert ch.resume_snapshot is None
    fresh = ch.tune_in()
    assert fresh.resumed is False


# -- break blocks -------------------------------------------------------------
def _breaks_channel(tmp_path, *, every=2, count=(1, 1), n_episodes=6, n_breaks=4):
    folder = make_show(tmp_path, "show", n_episodes)
    breaks_folder = make_show(tmp_path, "breaks", n_breaks)
    eps = scan_episodes(folder, [".mp4"])
    clips = scan_episodes(breaks_folder, [".mp4"])
    ch_cfg = ChannelConfig(number=4, name="Kids", path=folder)
    breaks_cfg = BreaksConfig(path=breaks_folder, every=every, count_min=count[0], count_max=count[1])
    return Channel(ch_cfg, eps, rng=random.Random(0), breaks=breaks_cfg, break_clips=clips)


def test_break_fires_after_n_episodes(tmp_path):
    ch = _breaks_channel(tmp_path, every=2)
    ch.tune_in()
    r1 = ch.advance()  # episode 1 of 2 -> not a break yet
    assert r1.is_break is False
    r2 = ch.advance()  # episode 2 of 2 -> break fires
    assert r2.is_break is True
    r3 = ch.advance()  # break clip ended -> back to a normal episode
    assert r3.is_break is False


def test_break_count_multiple_clips_then_episode(tmp_path):
    ch = _breaks_channel(tmp_path, every=1, count=(2, 2))
    ch.tune_in()
    r1 = ch.advance()  # 1 episode elapsed -> break of 2 clips queued
    assert r1.is_break is True
    r2 = ch.advance()  # second queued break clip
    assert r2.is_break is True
    r3 = ch.advance()  # queue drained -> normal episode
    assert r3.is_break is False


def test_break_clips_start_at_zero(tmp_path):
    ch = _breaks_channel(tmp_path, every=1)
    ch.tune_in()
    r = ch.advance()
    assert r.is_break is True
    assert r.start == 0.0


def test_tune_in_clears_partial_break_queue(tmp_path):
    ch = _breaks_channel(tmp_path, every=1, count=(3, 3))
    ch.tune_in()
    ch.advance()  # queues a 3-clip break, returns the first
    assert ch._break_queue  # still has 2 queued
    req = ch.tune_in()
    assert req.is_break is False
    assert ch._break_queue == []


def test_break_clips_all_play_before_repeat(tmp_path):
    ch = _breaks_channel(tmp_path, every=1, count=(1, 1), n_episodes=8, n_breaks=3)
    ch.tune_in()
    seen = set()
    for _ in range(3):
        r = ch.advance()  # break clip
        assert r.is_break is True
        seen.add(r.path)
        ch.advance()  # back to a normal episode
    assert len(seen) == 3  # every break clip aired once before any repeat


def test_no_breaks_configured_never_fires(tmp_path):
    ch = _channel(tmp_path, episodes=6, tune_in="random")
    ch.tune_in()
    for _ in range(10):
        assert ch.advance().is_break is False


def test_breaks_disabled_via_effective_breaks(tmp_path):
    from timewarptv.channel import _effective_breaks

    breaks_cfg = BreaksConfig(path=tmp_path, every=1)
    disabled_ch_cfg = ChannelConfig(
        number=2, name="Baby", path=tmp_path, breaks_disabled=True
    )
    assert _effective_breaks(breaks_cfg, disabled_ch_cfg) is None
    plain_ch_cfg = ChannelConfig(number=3, name="Kids", path=tmp_path)
    assert _effective_breaks(breaks_cfg, plain_ch_cfg) is breaks_cfg


# -- skip buttons (◀ / ▶) ------------------------------------------------------
# ▶ means "not this one"; ◀ means "the one before". Back/forward through what
# was shown, like shuffle on a music player; fresh draws only at the end.


def test_skip_gives_a_different_episode(tmp_path):
    ch = _channel(tmp_path)
    first = ch.tune_in().path

    second = ch.skip().path

    assert second != first


def test_back_returns_to_the_previous_episode(tmp_path):
    ch = _channel(tmp_path)
    first = ch.tune_in().path
    ch.skip()

    assert ch.back().path == first


def test_back_then_skip_goes_forward_again_not_somewhere_new(tmp_path):
    ch = _channel(tmp_path, episodes=8)
    ch.tune_in()
    second = ch.skip().path
    ch.back()

    assert ch.skip().path == second


def test_back_at_the_start_of_history_does_nothing(tmp_path):
    ch = _channel(tmp_path)
    ch.tune_in()

    assert ch.back() is None


def test_natural_advance_is_part_of_the_history(tmp_path):
    ch = _channel(tmp_path)
    ch.tune_in()
    finished = ch.advance().path
    ch.advance()

    assert ch.back().path == finished


def test_new_episode_after_going_back_drops_the_forward_history(tmp_path):
    """Same as a web browser: go back, then somewhere new, and 'forward' is gone."""
    ch = _channel(tmp_path, episodes=8)
    ch.tune_in()
    abandoned = ch.skip().path
    ch.back()
    ch.advance()                     # the episode ended; a fresh one started

    assert abandoned not in ch._history
    assert ch._cursor == len(ch._history) - 1   # nothing left "forward"


def test_skip_does_nothing_on_a_single_episode_channel(tmp_path):
    """The guide card: ▶ must not restart the only thing on the channel."""
    ch = _channel(tmp_path, episodes=1)
    ch.tune_in()

    assert ch.skip() is None
    assert ch.back() is None


def test_skip_does_nothing_on_locked_or_empty_channels(tmp_path):
    locked = _channel(
        tmp_path,
        ch_cfg=ChannelConfig(number=9, name="AS", path=make_show(tmp_path, "as", 4), passcode="1997"),
    )
    assert locked.skip() is None and locked.back() is None

    empty = Channel(ChannelConfig(number=5, name="E", path=tmp_path), [], rng=random.Random(0))
    assert empty.skip() is None and empty.back() is None


def test_skip_does_nothing_on_broadcast_channels(tmp_path):
    """A 'live' channel's position comes from the clock; a skip would revert."""
    ch = _channel(tmp_path, tune_in="broadcast")
    ch.tune_in(now=1000.0)

    assert ch.skip() is None
    assert ch.back() is None


def test_skip_never_triggers_or_counts_toward_a_break(tmp_path):
    ch = _breaks_channel(tmp_path, every=2)
    ch.tune_in()
    for _ in range(5):
        assert ch.skip().is_break is False

    # The very next natural end is only the first episode toward the break.
    assert ch.advance().is_break is False


def test_skip_during_a_break_abandons_the_rest_of_it(tmp_path):
    # every=2, so a fresh break can't follow the very next episode: if the next
    # natural end yields a break clip, it can only be a leftover from this one.
    ch = _breaks_channel(tmp_path, every=2, count=(3, 3))
    ch.tune_in()
    assert ch.advance().is_break is False
    assert ch.advance().is_break is True   # a three-clip break has started

    assert ch.skip().is_break is False
    assert ch.advance().is_break is False  # the other two clips are gone


def test_back_during_a_break_returns_to_the_show_it_interrupted(tmp_path):
    ch = _breaks_channel(tmp_path, every=1)
    before_break = ch.tune_in().path
    assert ch.advance().is_break is True

    assert ch.back().path == before_break


def test_skip_on_a_resume_channel_is_what_you_resume(tmp_path):
    """Flip away after a skip and back: resume the film you skipped TO."""
    ch = _channel(tmp_path, tune_in="resume")
    ch.tune_in()
    skipped_to = ch.skip().path

    assert ch.tune_in().path == skipped_to


def test_skip_past_a_resumed_film_draws_something_else(tmp_path):
    ch = _channel(tmp_path, tune_in="resume")
    resumed = ch.episodes[0]
    ch.remember(resumed, 1234.0)
    assert ch.tune_in().path == resumed

    for _ in range(20):                    # every draw, not just a lucky one
        ch.remember(resumed, 1234.0)
        ch._history, ch._cursor = [], -1
        ch.tune_in()
        assert ch.skip().path != resumed


def test_history_is_bounded(tmp_path):
    from timewarptv.channel import _HISTORY_LIMIT

    ch = _channel(tmp_path, episodes=4)
    ch.tune_in()
    for _ in range(_HISTORY_LIMIT + 30):
        ch.skip()

    assert len(ch._history) == _HISTORY_LIMIT
