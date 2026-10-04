#!/usr/bin/env bash
#
# Build the TimewarpTV drive from a Plex-style library (one folder per show /
# movie), reorganised into one folder per channel as listed in
# scripts/library.tsv.
#
# Only video files are copied. After the copy every video on the drive is
# checked with ffprobe; a file whose video stops before its declared end (an
# interrupted download - it would die mid-episode) is deleted from the drive
# and listed in <dest>/incomplete-files.txt. The source is never modified.
#
# Usage:
#   ./scripts/build-library.sh --dry-run "/Volumes/backup/Plex Media" /Volumes/WARPMEDIA
#   ./scripts/build-library.sh           "/Volumes/backup/Plex Media" /Volumes/WARPMEDIA
#
# --dry-run copies nothing: it checks the manifest, then runs the same
# completeness check against the source and prints what would be copied and
# what would be dropped.
#
# Re-running is incremental (rsync skips files already on the drive). Files are
# never removed from the drive except by the completeness check, so if a show
# moves to another channel in the manifest, delete its old folder by hand - the
# script warns about folders it doesn't expect.
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MANIFEST="${REPO_DIR}/scripts/library.tsv"
if [[ ! -f "$MANIFEST" ]]; then
  echo "error: no manifest at scripts/library.tsv" >&2
  echo "       copy scripts/library.example.tsv to scripts/library.tsv and list your shows" >&2
  exit 1
fi

# Must match DEFAULT_VIDEO_EXTENSIONS in timewarptv/config.py.
VIDEO_EXTS="mp4 mkv avi m4v mov webm mpg mpeg ts"

DRY_RUN=0
ARGS=()
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -*) echo "unknown option: $arg" >&2; exit 2 ;;
    *) ARGS+=("$arg") ;;
  esac
done
if [[ ${#ARGS[@]} -ne 2 ]]; then
  echo "usage: $0 [--dry-run] <source-root> <dest-root>" >&2
  exit 2
fi
SRC="${ARGS[0]%/}"
DEST="${ARGS[1]%/}"

for tool in ffprobe awk find; do
  command -v "$tool" >/dev/null 2>&1 || { echo "error: $tool not found" >&2; exit 1; }
done
if [[ "$DRY_RUN" -eq 0 ]] && ! command -v rsync >/dev/null 2>&1; then
  echo "error: rsync not found" >&2; exit 1
fi
[[ -d "$SRC" ]] || { echo "error: source not found or not readable: $SRC" >&2; exit 1; }
# The destination must already exist: an unmounted drive would otherwise mean
# writing 400 GB into /Volumes on the boot disk.
if [[ "$DRY_RUN" -eq 0 && ! -d "$DEST" ]]; then
  echo "error: destination not found: $DEST (is the drive mounted?)" >&2; exit 1
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# --- helpers ------------------------------------------------------------------

file_size() {  # GNU stat first: on Linux, `stat -f` means something else
  stat -c %s "$1" 2>/dev/null || stat -f %z "$1"
}

# tag <channel>: prefix each stdin line with "<channel> TAB".
tag() {
  local l
  while IFS= read -r l; do printf '%s\t%s\n' "$1" "$l"; done
}

# strip <prefix>: remove a literal leading prefix from each stdin line.
strip() {
  local l
  while IFS= read -r l; do printf '%s\n' "${l#"$1"}"; done
}

# find(1) arguments matching a video file (case-insensitive, no dotfiles).
FIND_VIDEO=( -type f ! -name '.*' \( )
sep=()
for ext in $VIDEO_EXTS; do FIND_VIDEO+=( "${sep[@]+"${sep[@]}"}" -iname "*.${ext}" ); sep=( -o ); done
FIND_VIDEO+=( \) )

# rsync filter: every directory, video files only, no dotfiles (._* AppleDouble
# files would otherwise match *.mp4). Extensions become [mM][pP]4 so the match
# is case-insensitive.
RSYNC_FILTER=( --exclude='.*' --include='*/' )
for ext in $VIDEO_EXTS; do
  pat=""
  for (( i = 0; i < ${#ext}; i++ )); do
    c="${ext:i:1}"
    case "$c" in
      [a-z]) pat+="[${c}$(printf '%s' "$c" | tr a-z A-Z)]" ;;
      *) pat+="$c" ;;
    esac
  done
  RSYNC_FILTER+=( --include="*.${pat}" )
done
RSYNC_FILTER+=( --exclude='*' )

# probe_file <file>: sets DUR (seconds) and REASON (empty if the file plays to
# the end, else why not). Reads the declared duration, then asks for any video
# packet in the last few seconds. A download cut short keeps a valid header (so
# the full duration is still reported) but has no packets near the end. Both
# pts and dts are read: AVI reports pts as N/A.
probe_file() {
  local f="$1" start last
  REASON=""
  DUR=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$f" 2>/dev/null </dev/null) || true
  case "$DUR" in ''|N/A) DUR=0; REASON="no readable video header"; return ;; esac
  start=$(awk -v d="$DUR" 'BEGIN { s = d - 5; printf "%d", (s < 0 ? 0 : s) }')
  last=$(ffprobe -v error -select_streams v:0 -read_intervals "${start}%+10" \
           -show_entries packet=pts_time,dts_time -of csv=p=0 "$f" 2>/dev/null </dev/null \
         | tr ',' '\n' | grep -v -x -e 'N/A' -e '' | tail -n 1) || true
  if [[ -z "$last" ]]; then
    REASON=$(awk -v d="$DUR" 'BEGIN { printf "video stops early (header says %.0f min)", d / 60 }')
  fi
}

# --- read + check the manifest -------------------------------------------------

CHANNELS=()
PATHS=()
line_no=0
bad_rows=0
while IFS= read -r line || [[ -n "$line" ]]; do
  line_no=$((line_no + 1))
  case "$line" in ''|'#'*) continue ;; esac
  ch="${line%%$'\t'*}"
  path="${line#*$'\t'}"
  if [[ "$ch" == "$line" || -z "$path" ]]; then
    echo "error: ${MANIFEST}:${line_no}: expected <channel> TAB <path>" >&2
    bad_rows=$((bad_rows + 1)); continue
  fi
  if [[ "$ch" != SKIP && ! "$ch" =~ ^[0-9]{2}-[a-z0-9-]+$ ]]; then
    echo "error: ${MANIFEST}:${line_no}: bad channel folder '$ch'" >&2
    bad_rows=$((bad_rows + 1)); continue
  fi
  CHANNELS+=("$ch")
  PATHS+=("$path")
done < "$MANIFEST"
[[ "$bad_rows" -eq 0 ]] || exit 1

echo "==> Checking ${MANIFEST##*/} covers the source library"
printf '%s\n' "${PATHS[@]}" | LC_ALL=C sort > "$WORK/claimed.txt"
for top in "TV Shows" "Movies"; do
  [[ -d "$SRC/$top" ]] || { echo "error: $SRC/$top not found" >&2; exit 1; }
  find "$SRC/$top" -mindepth 1 -maxdepth 1 ! -name '.*' | strip "$SRC/"
done | LC_ALL=C sort > "$WORK/on-disk.txt"

dupes=$(LC_ALL=C uniq -d "$WORK/claimed.txt")
missing=$(LC_ALL=C comm -13 "$WORK/on-disk.txt" "$WORK/claimed.txt")
unclaimed=$(LC_ALL=C comm -23 "$WORK/on-disk.txt" "$WORK/claimed.txt")
problems=0
if [[ -n "$dupes" ]]; then
  echo "error: listed more than once in the manifest:" >&2; echo "$dupes" | sed 's/^/    /' >&2; problems=1
fi
if [[ -n "$missing" ]]; then
  echo "error: in the manifest but not in the source (typo or renamed?):" >&2; echo "$missing" | sed 's/^/    /' >&2; problems=1
fi
if [[ -n "$unclaimed" ]]; then
  echo "error: in the source but not in the manifest - add a row or a SKIP:" >&2; echo "$unclaimed" | sed 's/^/    /' >&2; problems=1
fi
[[ "$problems" -eq 0 ]] || exit 1
echo "    ok: $(wc -l < "$WORK/on-disk.txt" | tr -d ' ') folders, each claimed once"

# --- copy -----------------------------------------------------------------------

# $WORK/files.tsv collects "<channel> TAB <file to check>": source files in a dry
# run, drive files after a real copy.
: > "$WORK/files.tsv"
total=${#PATHS[@]}
for (( i = 0; i < total; i++ )); do
  ch="${CHANNELS[$i]}"; path="${PATHS[$i]}"
  [[ "$ch" == SKIP ]] && continue
  src="$SRC/$path"
  name="${path##*/}"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    find "$src" "${FIND_VIDEO[@]}" | tag "$ch" >> "$WORK/files.tsv"
  else
    echo "==> [$((i + 1))/${total}] ${ch} <- ${name}"
    mkdir -p "$DEST/$ch"
    if [[ -d "$src" ]]; then
      rsync -rt "${RSYNC_FILTER[@]}" "$src/" "$DEST/$ch/$name/"
    else
      rsync -t "$src" "$DEST/$ch/"
    fi
  fi
done

if [[ "$DRY_RUN" -eq 0 ]]; then
  # rsync creates every directory, including ones that held only subtitles etc.
  for ch in $(printf '%s\n' "${CHANNELS[@]}" | grep -v -x SKIP | sort -u); do
    find "$DEST/$ch" -mindepth 1 -type d -empty -delete
    find "$DEST/$ch" "${FIND_VIDEO[@]}" | tag "$ch" >> "$WORK/files.tsv"
  done
  mkdir -p "$DEST/breaks"

  # Folders on the drive that the manifest doesn't produce (e.g. a show that
  # moved channel). Left alone - just reported.
  for (( i = 0; i < total; i++ )); do
    [[ "${CHANNELS[$i]}" == SKIP ]] || echo "${CHANNELS[$i]}/${PATHS[$i]##*/}"
  done | LC_ALL=C sort > "$WORK/expected.txt"
  find "$DEST" -mindepth 2 -maxdepth 2 ! -name '.*' -path "$DEST/[0-9][0-9]-*" \
    | strip "$DEST/" | LC_ALL=C sort > "$WORK/present.txt"
  stray=$(LC_ALL=C comm -13 "$WORK/expected.txt" "$WORK/present.txt")
  if [[ -n "$stray" ]]; then
    echo "warning: on the drive but not in the manifest (delete by hand if unwanted):" >&2
    echo "$stray" | sed 's/^/    /' >&2
  fi
fi

# --- completeness check ---------------------------------------------------------

n_files=$(wc -l < "$WORK/files.tsv" | tr -d ' ')
where="the drive"; [[ "$DRY_RUN" -eq 1 ]] && where="the source"
echo "==> Checking ${n_files} videos on ${where} for incomplete files (ffprobe, ~0.3 s each)"
: > "$WORK/bad.txt"
: > "$WORK/sizes.tsv"
n=0
while IFS=$'\t' read -r ch f; do
  n=$((n + 1))
  (( n % 100 == 0 )) && echo "    ${n}/${n_files}"
  probe_file "$f"
  if [[ -n "$REASON" ]]; then
    rel="${f#"$DEST/"}"; rel="${rel#"$SRC/"}"
    printf '%s\t%s\n' "$REASON" "$rel" >> "$WORK/bad.txt"
    [[ "$DRY_RUN" -eq 0 ]] && rm -f "$f"
  else
    printf '%s\t%s\t%s\n' "$ch" "$(file_size "$f")" "$DUR" >> "$WORK/sizes.tsv"
  fi
done < "$WORK/files.tsv"

n_bad=$(wc -l < "$WORK/bad.txt" | tr -d ' ')
if [[ "$n_bad" -gt 0 ]]; then
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "==> ${n_bad} incomplete file(s) would be dropped:"
    sed 's/^/    /' "$WORK/bad.txt"
  else
    report="$DEST/incomplete-files.txt"
    {
      echo "# Removed from this drive by build-library.sh on $(date '+%Y-%m-%d %H:%M')."
      echo "# The source copies are untouched. Re-download these, then run the script again."
      cat "$WORK/bad.txt"
    } > "$report"
    find "$DEST" -mindepth 2 -type d -empty -path "$DEST/[0-9][0-9]-*" -delete
    echo "==> Removed ${n_bad} incomplete file(s); list in ${report}"
  fi
elif [[ "$DRY_RUN" -eq 0 ]]; then
  rm -f "$DEST/incomplete-files.txt"
fi

# --- summary ------------------------------------------------------------------

{
  echo ""
  if [[ "$DRY_RUN" -eq 1 ]]; then echo "Would copy:"; else echo "On the drive ($(date '+%Y-%m-%d')):"; fi
  awk -F'\t' '
    { gb[$1] += $2 / 1073741824; h[$1] += $3 / 3600; n[$1]++
      tgb += $2 / 1073741824; th += $3 / 3600; tn++ }
    END {
      for (c in n) printf "    %-15s %7.1f GB %6d files %7.0f hours\n", c, gb[c], n[c], h[c] | "sort"
      close("sort")
      printf "    %-15s %7.1f GB %6d files %7.0f hours\n", "TOTAL", tgb, tn, th
    }' "$WORK/sizes.tsv"
  if [[ "$n_bad" -gt 0 ]]; then echo "    (${n_bad} incomplete file(s) left out)"; fi
} > "$WORK/summary.txt"
cat "$WORK/summary.txt"
# Keep the numbers on the drive (e.g. for a printed channel guide).
[[ "$DRY_RUN" -eq 0 ]] && cp "$WORK/summary.txt" "$DEST/library-summary.txt"
exit 0
