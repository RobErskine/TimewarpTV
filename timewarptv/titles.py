"""Turn a video file's path into what a TV would caption it with.

There is no metadata to go on - the file and folder names are all there is -
and real libraries name things every way at once: Plex style
(``Trap (2024)/Trap (2024).mp4``), raw release names
(``Show.Name.S01E09-Title.1080p.WEB-DL.x264-GROUP.mkv``), abbreviations with the
real name only on the folder (``BNTSG S01E01 Flight.avi`` in
``Bill Nye the Science Guy S01-05/Season 1/``), spelled-out markers
(``Season 2, Episode 10 - Mind Games``), and seasons that only exist as a
folder (``Book 1; Water/Ep. 01 - The Boy in the Iceberg``).

So this is a small set of rules, applied in order, each one cheap and
predictable. It never raises: the worst case is a tidied-up file name.

* An **episode** (a season/episode marker in the file name) is captioned with
  the show's name and ``S01 E09`` plus the episode's own title, if it has one.
  The show's name comes from the text before the marker, unless that's missing
  or an abbreviation, in which case the nearest folder that names it is used.
* Anything else is a **movie**: its title, and its year if the name has one
  (from the file, or failing that its folder, as in Plex's layout).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Tuple


@dataclass(frozen=True)
class NowPlaying:
    """A caption: the show or movie, and a detail line (episode, or year)."""

    title: str
    detail: str = ""


# -- episode markers --------------------------------------------------------
# Each finds the marker in a file name and says where it sits, so the text
# before it can name the show and the text after it can name the episode.
_SXXEYY = re.compile(
    r"\bS(?P<season>\d{1,2})[\s._-]*(?P<kind>[EXM])(?P<ep>\d{1,3})"
    r"(?P<sub>[EX]\d{1,3})?(?:\s*-\s*E?(?P<ep2>\d{1,3}))?(?![\d])",
    re.IGNORECASE,
)
_SEASON_EPISODE_WORDS = re.compile(
    r"\bSeason\s*(?P<season>\d{1,2})\W{0,3}\s*Episode\s*(?P<ep>\d{1,3})\b",
    re.IGNORECASE,
)
_NXNN = re.compile(r"\b(?P<season>\d{1,2})x(?P<ep>\d{2,3})\b", re.IGNORECASE)
# "Robotboy 101 Dog Ra": season digit + two-digit episode. Only trusted when a
# folder names the same season, so "Pitch Perfect 2" or "Area 51" never match.
_NNN = re.compile(r"(?<![\d.])\b(?P<season>[1-9])(?P<ep>\d{2})\b(?![\d.])")
_EP_ONLY = re.compile(r"\b(?:Ep(?:isode)?)\.?\s*(?P<ep>\d{1,3})\b", re.IGNORECASE)

# Season named by a folder: "Season 3", "Season Two (2000)", "Book 1; Water", "S02".
_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}
_FOLDER_SEASON = re.compile(
    r"\b(?:Season|Series|Book)\s*(?P<n>\d{1,2}|" + "|".join(_WORD_NUMBERS) + r")\b"
    r"|\bS(?P<s>\d{1,2})\b",
    re.IGNORECASE,
)

# -- tidying names ------------------------------------------------------------
# Release-name debris. A name is cut at the first of these: whatever follows a
# resolution or a source tag is never part of the title.
_JUNK = re.compile(
    r"^(?:\d{3,4}p|4k|uhd|hdr\d*|"
    r"web|web-?dl|web-?rip|re-?webrip|dvd|dvd-?rip|re-?dvd-?rip|re-?dvrip|"
    r"blu-?ray|br-?rip|bd-?rip|hd-?rip|hdtv|tv-?rip|vhs|(?:re-?)?vhs-?rip|"
    r"[xh]\.?26[45]|hevc|avc|xvid|divx|aac[\d.]*|ac3|dts|opus\d*|"
    r"amzn|nf|dsnp|dnsp|hmax|hulu|atvp|"
    r"complete|proper|repack|internal|dual|multi|subbed|dubbed|"
    r"rarbg|yify|yts)$",
    re.IGNORECASE,
)
# In a folder name, a season/range marker ends the show's name: "Show S01-S05",
# "Show Season Two (2000)", "Book 1; Water".
_SEASONISH = re.compile(
    r"^(?:S\d{1,2}(?:[EXM]\d{1,3})*(?:-S?\d{1,2})?|Season|Series|Book|Seasons|Vol\.?|Volume|"
    r"Extras|Xtras|Specials|Bonus|Featurettes)$",
    re.IGNORECASE,
)
_YEAR = re.compile(r"(?<!\d)(19[2-9]\d|20[0-4]\d)(?!\d)")
# "BNTSG", "ATHF", "SGCtC": a short single word that's mostly capitals.
_ABBREVIATION = re.compile(r"^(?=(?:[^A-Z]*[A-Z]){2})[A-Za-z]{2,6}$")
_EDGE_PUNCT = " -_,;:.()[]"
MAX_TITLE = 34
MAX_DETAIL = 40


def describe(path: Path, root: Optional[Path] = None) -> NowPlaying:
    """Caption the file at ``path``; ``root`` is its channel's folder."""
    path = Path(path)
    folders = _folders_between(path, root)
    stem = path.stem

    found = _find_episode(stem, folders)
    if found is not None:
        season, label, before, after = found
        if season is None:
            season = _season_from_folders(folders)
        show = _show_name(before, folders) or _tidy(stem)
        episode = _tidy(after, cut_seasons=False)
        prefix = f"S{season:02d} {label}" if season is not None else label
        detail = f"{prefix}  {episode}" if episode else prefix
        return NowPlaying(_fit(show, MAX_TITLE), _fit(detail, MAX_DETAIL))

    title, year = _title_and_year(stem)
    if folders and (len(title) <= 3 or _ABBREVIATION.match(title)):
        folder_title, folder_year = _title_and_year(folders[0])
        if folder_title:
            title, year = folder_title, year or folder_year
    if year is None and folders:
        folder_title, folder_year = _title_and_year(folders[0])
        if folder_year is not None:
            title, year = folder_title or title, folder_year
    title = title or _tidy(stem) or stem
    return NowPlaying(_fit(title, MAX_TITLE), str(year) if year else "")


# -- episode ------------------------------------------------------------------
def _find_episode(
    stem: str, folders: List[str]
) -> Optional[Tuple[Optional[int], str, str, str]]:
    """(season, "E09" label, text before the marker, text after) or None."""
    text = _separators(stem)
    m = _SXXEYY.search(text)
    if m:
        kind = m.group("kind").upper()
        label = f"{kind}{int(m.group('ep')):02d}"
        if m.group("sub"):
            label += f" {m.group('sub').upper()}"
        if m.group("ep2"):
            label += f"-E{int(m.group('ep2')):02d}"
        return int(m.group("season")), label, text[: m.start()], text[m.end():]
    # The weaker markers below only count when there's no year about: films
    # carry years ("DCAU 11X01 Chase Me (2003)" is a short, not season 11),
    # episodes rarely do. The explicit S01E01 form above is trusted regardless.
    has_year = bool(_YEAR.search(text))
    for pattern in (_SEASON_EPISODE_WORDS, _NXNN):
        m = pattern.search(text)
        if m and (pattern is _SEASON_EPISODE_WORDS or not has_year):
            label = f"E{int(m.group('ep')):02d}"
            return int(m.group("season")), label, text[: m.start()], text[m.end():]
    m = _NNN.search(text)
    if m and _season_from_folders(folders) == int(m.group("season")):
        label = f"E{int(m.group('ep')):02d}"
        return int(m.group("season")), label, text[: m.start()], text[m.end():]
    # A bare "Ep. 01" names an episode only when there's no year about: a
    # film like "Star Wars Episode 4 (1977)" is still a film.
    m = _EP_ONLY.search(text)
    if m and not has_year:
        return None, f"E{int(m.group('ep')):02d}", text[: m.start()], text[m.end():]
    return None


def _season_from_folders(folders: Iterable[str]) -> Optional[int]:
    for name in folders:
        m = _FOLDER_SEASON.search(name)
        if m:
            raw = (m.group("n") or m.group("s")).lower()
            return _WORD_NUMBERS.get(raw) or int(raw)
    return None


def _show_name(before_marker: str, folders: List[str]) -> str:
    """The show: named before the marker, else by the nearest folder that does."""
    name = _tidy(before_marker)
    if name and not _ABBREVIATION.match(name):
        return name
    for folder in folders:
        candidate = _tidy(folder)
        if candidate and not _ABBREVIATION.match(candidate):
            return candidate
    return name


# -- movie ----------------------------------------------------------------------
def _title_and_year(name: str) -> Tuple[str, Optional[int]]:
    """Split "Title (1999) 1080p..." into ("Title", 1999). Takes the LAST year,
    so "2001 A Space Odyssey (1968)" and "Blade Runner 2049 (2017)" work."""
    words = _cut_junk(_separators(name).split())
    text = " ".join(words)
    years = list(_YEAR.finditer(text))
    if not years:
        return _tidy(name), None
    last = years[-1]
    title = _tidy(text[: last.start()])
    if not title:  # the whole name is a year, e.g. "1917 (2019)" -> "1917"
        title = _tidy(text[last.end():]) or last.group(1)
    return title, int(last.group(1))


# -- tidying --------------------------------------------------------------------
def _separators(text: str) -> str:
    """Dots/underscores (and, in all-lowercase names, hyphens) to spaces."""
    text = re.sub(r"\[[^\]]*\]|\{[^}]*\}", " ", text)  # [TAGS], {tags}
    text = text.replace("_", " ")
    if " " not in text.strip():  # a release name: dots are the spaces
        text = text.replace(".", " ")
    if text == text.lower() and re.fullmatch(r"[a-z0-9 .'-]+", text.strip() or "x"):
        text = text.replace("-", " ")
    return re.sub(r"\s+", " ", text).strip()


def _cut_junk(words: List[str], *, cut_seasons: bool = False) -> List[str]:
    out = []
    for word in words:
        bare = word.strip("()[],;-")
        if _JUNK.match(bare) or (cut_seasons and _SEASONISH.match(bare)):
            break
        out.append(word)
    return out


def _tidy(text: str, *, cut_seasons: bool = True) -> str:
    """A clean display name: junk cut off, year-only brackets dropped."""
    words = _cut_junk(_separators(text).split(), cut_seasons=cut_seasons)
    text = " ".join(words)
    # Cutting at the junk can leave a bracket open - "Boo to You (1996" - so an
    # unclosed one goes, with whatever it had started.
    if text.count("(") > text.count(")"):
        text = text[: text.rfind("(")]
    # Brackets holding only a year or year range go, as do one-word tags like
    # "(Grp)"; others ("(Part 1)", "(Kids NEXT Door)") keep their words.
    text = re.sub(r"\(\s*(?:19|20)\d\d(?:\s*-\s*(?:(?:19|20)\d\d)?)?\s*\)", " ", text)
    text = re.sub(r"\(\s*[A-Za-z]{1,6}\s*\)", " ", text)
    # A collection's running number: "DCAU 02 Batman Mask of the Phantasm".
    text = re.sub(r"^\s*[A-Z]{2,6}\s+\d{2}(?:[XE]\d{2})?\s+(?=\S)", "", text)
    text = text.replace("(", " ").replace(")", " ")
    text = re.sub(r"\s+", " ", text).strip(_EDGE_PUNCT)
    if text and text == text.lower():
        text = " ".join(w[:1].upper() + w[1:] for w in text.split())
    # Library sort order back to how it's said: "Oblongs, The" -> "The Oblongs".
    m = re.fullmatch(r"(.+), (The|A|An)", text)
    return f"{m.group(2)} {m.group(1)}" if m else text


def _fit(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3].rstrip(_EDGE_PUNCT) + "..."


def _folders_between(path: Path, root: Optional[Path]) -> List[str]:
    """Folder names from the file up to (not including) the channel folder."""
    parents = list(path.parents)
    names = []
    for parent in parents:
        if root is not None and parent == Path(root):
            break
        if parent == parent.parent:
            break
        names.append(parent.name)
    if root is None:
        names = names[:2]  # without a root, don't wander up the whole disk
    return names


__all__ = ["NowPlaying", "describe"]
