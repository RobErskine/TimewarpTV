"""File paths -> on-screen captions. Examples mirror naming styles seen in a
real library; the names themselves are made up."""

from __future__ import annotations

from pathlib import Path

import pytest

from timewarptv.titles import NowPlaying, describe

ROOT = Path("/media/tv/05-kids")


def cap(rel):
    return describe(ROOT / rel, ROOT)


@pytest.mark.parametrize("rel, title, detail", [
    # Plain "Show SxxEyy Episode title"
    ("Show Name 1997 Season 1/Show Name S01E01 Home Is Where It Is.mkv",
     "Show Name", "S01 E01  Home Is Where It Is"),
    # Release name: dots for spaces, junk after the title
    ("Space.Show.S01.1080p/Space.Show.S01E09-Jamming.with.Friends.1080p.BRRIP.x265.mkv",
     "Space Show", "S01 E09  Jamming with Friends"),
    # " - S01 E09 - " with spaces, and junk in brackets
    ("Duck Show (1991-1992) - Complete/Season 1 (1991-92)/"
     "Duck Show - S01 E09 - Battle of Wits (480p - Web-DL).mp4",
     "Duck Show", "S01 E09  Battle of Wits"),
    # A multi-episode file
    ("Kid Spies/Season 1/Kid Spies - S01 E01-E03 (720p - Web-DL).mp4",
     "Kid Spies", "S01 E01-E03"),
    # Abbreviation in the file; the real name only on a folder further up
    ("Science Guy Show S01-05/Season 3/SGS S03E03 Plants.avi",
     "Science Guy Show", "S03 E03  Plants"),
    # Abbreviated folder too: keep climbing
    ("Hunger Force Show S01-S11 (2000-) + Movie (2007)/HFS S01 (360p re-dvdrip)/HFS S01E01 Robot.mp4",
     "Hunger Force Show", "S01 E01  Robot"),
    # "Season 2, Episode 10 - ..." spelled out; show from a "Season Two" folder
    ("Future Hero (Collection) (1999)/Future Hero Season Two (2000)/Season 2, Episode 10 - Mind Games.avi",
     "Future Hero", "S02 E10  Mind Games"),
    # Only an episode number; the season comes from a "Book 1" folder
    ("Airbender Tale/Book 1; Water/Ep. 01 - The Boy in the Ice.mp4",
     "Airbender Tale", "S01 E01  The Boy in the Ice"),
    # 1x05 style
    ("Old Sitcom/Old Sitcom 2x05 The Move.avi", "Old Sitcom", "S02 E05  The Move"),
    # An odd "X01" (unaired extra) marker, with a bracketed note
    ("Hunger Force Show/HFS S05 (360p)/HFS S05X01 Boston [Unaired].mp4",
     "Hunger Force Show", "S05 X01  Boston"),
])
def test_episodes(rel, title, detail):
    assert cap(rel) == NowPlaying(title, detail)


@pytest.mark.parametrize("rel, title, year", [
    ("A Knights Story (2001)/A Knights Story (2001).mp4", "A Knights Story", "2001"),
    ("3 Ninja Kids (1992)/3.Ninja.Kids.1992.720p.WEB-DL.AAC2.0.H264-GROUP.mkv", "3 Ninja Kids", "1992"),
    # A number in the title must not be taken for the year
    ("28 Nights Later (2002)/28 Nights Later (2002).mp4", "28 Nights Later", "2002"),
    ("Runner 2049 (2017)/Runner 2049 (2017).mp4", "Runner 2049", "2017"),
    # A title that IS a year
    ("1917 (2019)/1917 (2019).mp4", "1917", "2019"),
    # Year only on the folder (Plex layout with a bare file name)
    ("The Matrix Thing (1999)/movie.mp4", "The Matrix Thing", "1999"),
    # No year anywhere: an all-lowercase slug is title-cased
    ("Sing Along/sing-along_grandpa-magical-toys.mp4", "Sing Along Grandpa Magical Toys", ""),
    # "Episode 4" with a year is still a film, not S?? E04
    ("Star Tales Episode 4 (1977)/Star Tales Episode 4 (1977).mp4", "Star Tales Episode 4", "1977"),
])
def test_movies(rel, title, year):
    assert cap(rel) == NowPlaying(title, year)


def test_long_names_are_shortened_to_fit_the_screen():
    np = cap("Show/Show S01E01 De-Zanitized, The Monkey Song, Nighty-Night Toon, Encore.mp4")

    assert len(np.detail) <= 40 and np.detail.endswith("...")


def test_never_raises_on_odd_names():
    for rel in ("x.mp4", ".mp4", "----.mkv", "S01E01.mp4", "(2001).mp4", "a/b/c/d/e.mp4"):
        assert isinstance(cap(rel), NowPlaying)


def test_file_directly_in_the_channel_folder():
    assert cap("Cartoon Show S02E03 Pilot.mp4") == NowPlaying("Cartoon Show", "S02 E03  Pilot")


@pytest.mark.parametrize("rel, title, detail", [
    # "M" = a movie within a series' run, sometimes with extras numbered after it
    ("Bear Show S01-S04 + Movies/TNA of Bear Show S04M02 Boo to You (1996 360p re-vhsrip).mp4",
     "TNA of Bear Show", "S04 M02  Boo to You"),
    ("Hunger Force Show/HFS Xtras/HFS S04M01X03 Deleted Scene.mp4",
     "Hunger Force Show", "S04 M01 X03  Deleted Scene"),
    # "Show 101 Title" - trusted only because the folder says Season 1
    ("Robot Kid [Tag]/Season 1/Robot Kid 101 Dog Day - War and Peace [Tag].avi",
     "Robot Kid", "S01 E01  Dog Day - War and Peace"),
    # ...and a one-word release tag in brackets is dropped
    ("Jungle Family - All/Jungle Family Season 1/Jungle Family 102 Dinner With Darwin (Grp) [xx].avi",
     "Jungle Family", "S01 E02  Dinner With Darwin"),
    # A mixed-case abbreviation ("SGCtC") climbs to the folder too
    ("Ghost Talk Show S01-S08 (1994-)/GTsS S01 (360p re-dvdrip)/GTsS S01E02 Gilligan.mp4",
     "Ghost Talk Show", "S01 E02  Gilligan"),
])
def test_more_episode_styles(rel, title, detail):
    assert cap(rel) == NowPlaying(title, detail)


def test_a_three_digit_number_without_a_matching_season_folder_is_not_an_episode():
    assert cap("Movies/Area 101 (1999).mp4") == NowPlaying("Area 101", "1999")
    assert cap("Jungle Family - Movies/Jungle Family 192 Rugrats Go Wild.avi").detail == ""


@pytest.mark.parametrize("rel, title, year", [
    # A file name that says nothing; the folder names the film
    ("PUPPET TREASURE ISLAND[DUAL AUDIO]/ti.mp4", "PUPPET TREASURE ISLAND", ""),
    # A collection's running number is not part of the title
    ("Hero Animated (1992-2014)/DCAU 02 Hero Mask of the Phantom (1993 360p re-dvdrip).mp4",
     "Hero Mask of the Phantom", "1993"),
    # Library sort order is put back
    ("Oblongs, The/Oblongs, The.avi", "The Oblongs", ""),
])
def test_more_movie_styles(rel, title, year):
    assert cap(rel) == NowPlaying(title, year)


def test_a_numbered_short_in_a_collection_is_a_film_not_season_eleven():
    assert cap("Hero Animated (1992-2014)/DCAU 11X01 Chase Me (2003).mp4") == NowPlaying("Chase Me", "2003")


def test_a_folder_named_by_its_marker_is_skipped_for_the_show_name():
    np = cap("Hunger Force Show S01-S11/HFS S04M01 (360p)/HFS S04M01 The Movie (2007 360p re-webrip).mp4")
    assert np == NowPlaying("Hunger Force Show", "S04 M01  The Movie")


def test_a_dash_delimited_episode_number_wins_over_a_year():
    """'Series - E03 - Title (1942)': numbered shorts in a collection. The
    dashes make it unambiguous, unlike 'Star Tales Episode 4 (1977)'."""
    np = cap("Cartoon Collection (1930-1969)/Cartoon Collection - E03 - Hold the Lion, Please (1942) (1080p x265).mkv")

    assert np == NowPlaying("Cartoon Collection", "E03  Hold the Lion, Please")
