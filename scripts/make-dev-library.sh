#!/usr/bin/env bash
#
# Build a fake, fully-labelled media library + config.dev.yaml so the whole
# box can be exercised on a laptop - shuffle, breaks, resume, locked channels
# - before any real media is copied onto a drive.
#
# Every clip is synthesized with ffmpeg: colour bars, a distinct tone, and the
# channel/show name burned into the picture via drawtext, so you can tell at a
# glance what's playing. Clips are short (20s) on purpose: a break block that
# would take 45 minutes to reach on real TV arrives in under a minute here.
#
# Usage:
#   ./scripts/make-dev-library.sh              # ~40 short synthetic clips
#   ./scripts/make-dev-library.sh --with-bunny # ...plus one real long movie
#   ./scripts/make-dev-library.sh --force      # regenerate even if present
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MEDIA_DIR="${REPO_DIR}/dev-media"
FONT="${REPO_DIR}/timewarptv/assets/fonts/VT323-Regular.ttf"
CONFIG_OUT="${REPO_DIR}/config.dev.yaml"

WITH_BUNNY=0
FORCE=0
for arg in "$@"; do
  case "$arg" in
    --with-bunny) WITH_BUNNY=1 ;;
    --force) FORCE=1 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "error: ffmpeg not found. Install it first: brew install ffmpeg (Mac) or sudo apt install ffmpeg (Linux)." >&2
  exit 1
fi
if [[ ! -f "$FONT" ]]; then
  echo "error: bundled font not found at $FONT (is this run from a checkout of TimewarpTV?)" >&2
  exit 1
fi

# Some ffmpeg builds (Homebrew's default formula, as of ffmpeg 7+, is one of
# them) are compiled without libfreetype and have no 'drawtext' filter at all -
# not fixable by reinstalling, only by a custom build. Fall back to rendering
# the label as a transparent PNG with Python/Pillow (using the same bundled
# font) and compositing it with ffmpeg's 'overlay' filter instead, which needs
# no font-rendering library. If even that isn't available, skip the on-screen
# text - TimewarpTV's own channel banner still shows what's playing, so
# labels are cosmetic either way.
HAVE_DRAWTEXT=1
if ! ffmpeg -hide_banner -filters 2>/dev/null | grep -qi '\bdrawtext\b'; then
  HAVE_DRAWTEXT=0
fi

HAVE_PIL=0
if [[ "$HAVE_DRAWTEXT" -eq 0 ]]; then
  if python3 -c "import PIL" >/dev/null 2>&1; then
    HAVE_PIL=1
  else
    echo "==> ffmpeg has no 'drawtext' filter here; installing Pillow to render labels instead" >&2
    if python3 -m pip install --quiet Pillow >/dev/null 2>&1; then
      HAVE_PIL=1
    fi
  fi
  if [[ "$HAVE_PIL" -eq 1 ]]; then
    echo "warning: this ffmpeg has no 'drawtext' filter - labels will be rendered via" >&2
    echo "         Pillow + ffmpeg's 'overlay' filter instead (a bit slower, same result)." >&2
  else
    echo "warning: this ffmpeg has no 'drawtext' filter, and Pillow could not be installed" >&2
    echo "         (pip install pillow) - clips will have no on-screen text. TimewarpTV's" >&2
    echo "         own channel banner still shows what's playing, so this is cosmetic." >&2
  fi
fi

DURATION=20   # seconds per synthetic clip - short on purpose, see header
WIDTH=640
HEIGHT=480
FPS=25
LABEL_DIR="$(mktemp -d)"
trap 'rm -rf "$LABEL_DIR"' EXIT

# _render_label_png <out_png> <width> <height> <line1> [<line2>]
# Renders 1-2 centred lines of text onto a transparent PNG using the bundled
# font - no libfreetype/fontconfig needed in ffmpeg, since ffmpeg only has to
# composite a pre-rendered image (the 'overlay' filter is always available).
_render_label_png() {
  local out_png="$1" width="$2" height="$3" line1="$4" line2="${5:-}"
  python3 - "$out_png" "$width" "$height" "$FONT" "$line1" "$line2" <<'PYEOF'
import sys
from PIL import Image, ImageDraw, ImageFont

out_png, width, height, font_path, line1, line2 = sys.argv[1:7]
width, height = int(width), int(height)
img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
draw = ImageDraw.Draw(img)


def centered(text, size, y):
    if not text:
        return
    font = ImageFont.truetype(font_path, size)
    x0, y0, x1, y1 = draw.textbbox((0, 0), text, font=font)
    w, h = x1 - x0, y1 - y0
    x = (width - w) // 2
    pad = 10
    draw.rectangle([x - pad, y - pad, x + w + pad, y + h + pad], fill=(0, 0, 0, 140))
    draw.text((x - x0, y - y0), text, font=font, fill=(255, 255, 255, 255))


centered(line1, 36, height // 2 - 60)
centered(line2, 28, height // 2 + 10)
img.save(out_png)
PYEOF
}

# make_clip <out_path> <label-line1> <label-line2> <tone-hz>
make_clip() {
  local out="$1" line1="$2" line2="$3" hz="$4"
  if [[ -f "$out" && "$FORCE" -eq 0 ]]; then
    return
  fi
  mkdir -p "$(dirname "$out")"
  if [[ "$HAVE_DRAWTEXT" -eq 1 ]]; then
    local vf="drawtext=fontfile='${FONT}':text='${line1}':fontcolor=white:fontsize=36:x=(w-text_w)/2:y=(h/2)-60:box=1:boxcolor=black@0.5:boxborderw=10"
    vf+=",drawtext=fontfile='${FONT}':text='${line2}':fontcolor=white:fontsize=28:x=(w-text_w)/2:y=(h/2)+10:box=1:boxcolor=black@0.5:boxborderw=10"
    ffmpeg -y -loglevel error \
      -f lavfi -i "smptebars=s=${WIDTH}x${HEIGHT}:r=${FPS}:d=${DURATION}" \
      -f lavfi -i "sine=frequency=${hz}:duration=${DURATION}:sample_rate=48000" \
      -vf "$vf" \
      -af "volume=0.15" \
      -c:v libx264 -preset veryfast -pix_fmt yuv420p \
      -c:a aac -b:a 96k -shortest \
      "$out"
  elif [[ "$HAVE_PIL" -eq 1 ]]; then
    local png="${LABEL_DIR}/clip-$$-${RANDOM}.png"
    _render_label_png "$png" "$WIDTH" "$HEIGHT" "$line1" "$line2"
    ffmpeg -y -loglevel error \
      -f lavfi -i "smptebars=s=${WIDTH}x${HEIGHT}:r=${FPS}:d=${DURATION}" \
      -f lavfi -i "sine=frequency=${hz}:duration=${DURATION}:sample_rate=48000" \
      -loop 1 -i "$png" \
      -filter_complex "[0:v][2:v]overlay=0:0[v]" \
      -map "[v]" -map "1:a" \
      -af "volume=0.15" \
      -c:v libx264 -preset veryfast -pix_fmt yuv420p \
      -c:a aac -b:a 96k -shortest \
      "$out"
    rm -f "$png"
  else
    ffmpeg -y -loglevel error \
      -f lavfi -i "smptebars=s=${WIDTH}x${HEIGHT}:r=${FPS}:d=${DURATION}" \
      -f lavfi -i "sine=frequency=${hz}:duration=${DURATION}:sample_rate=48000" \
      -af "volume=0.15" \
      -c:v libx264 -preset veryfast -pix_fmt yuv420p \
      -c:a aac -b:a 96k -shortest \
      "$out"
  fi
  echo "  wrote $out"
}

# make_card <out_path> <big-text> <hz>  - a break-time card (green, no bars)
make_card() {
  local out="$1" text="$2" hz="$3"
  if [[ -f "$out" && "$FORCE" -eq 0 ]]; then
    return
  fi
  mkdir -p "$(dirname "$out")"
  if [[ "$HAVE_DRAWTEXT" -eq 1 ]]; then
    local vf="drawtext=fontfile='${FONT}':text='${text}':fontcolor=white:fontsize=48:x=(w-text_w)/2:y=(h-text_h)/2"
    ffmpeg -y -loglevel error \
      -f lavfi -i "color=c=0x123B18:s=${WIDTH}x${HEIGHT}:r=${FPS}:d=8" \
      -f lavfi -i "sine=frequency=${hz}:duration=8:sample_rate=48000" \
      -vf "$vf" \
      -af "volume=0.1" \
      -c:v libx264 -preset veryfast -pix_fmt yuv420p \
      -c:a aac -b:a 96k -shortest \
      "$out"
  elif [[ "$HAVE_PIL" -eq 1 ]]; then
    local png="${LABEL_DIR}/card-$$-${RANDOM}.png"
    _render_label_png "$png" "$WIDTH" "$HEIGHT" "$text" ""
    ffmpeg -y -loglevel error \
      -f lavfi -i "color=c=0x123B18:s=${WIDTH}x${HEIGHT}:r=${FPS}:d=8" \
      -f lavfi -i "sine=frequency=${hz}:duration=8:sample_rate=48000" \
      -loop 1 -i "$png" \
      -filter_complex "[0:v][2:v]overlay=0:0[v]" \
      -map "[v]" -map "1:a" \
      -af "volume=0.1" \
      -c:v libx264 -preset veryfast -pix_fmt yuv420p \
      -c:a aac -b:a 96k -shortest \
      "$out"
    rm -f "$png"
  else
    ffmpeg -y -loglevel error \
      -f lavfi -i "color=c=0x123B18:s=${WIDTH}x${HEIGHT}:r=${FPS}:d=8" \
      -f lavfi -i "sine=frequency=${hz}:duration=8:sample_rate=48000" \
      -af "volume=0.1" \
      -c:v libx264 -preset veryfast -pix_fmt yuv420p \
      -c:a aac -b:a 96k -shortest \
      "$out"
  fi
  echo "  wrote $out"
}

echo "==> Generating fake channel library in ${MEDIA_DIR}"

declare -a CHANNELS=(
  "02-baby:Wee Sing:CH 02 BABY TV"
  "03-toddler:Mickey Mouse Clubhouse:CH 03 PLAYHOUSE"
  "04-kids:Rugrats:CH 04 KIDS"
  "05-y7:Ren and Stimpy:CH 05 Y7"
  "06-kid-movies:Kid Movie Night:CH 06 KID MOVIES"
  "09-adult-swim:Aqua Teen Hunger Force:CH 09 ADULT SWIM"
  "10-movies:Grownup Movie Night:CH 10 MOVIES"
)

hz=220
for entry in "${CHANNELS[@]}"; do
  IFS=":" read -r folder show label <<< "$entry"
  hz=$((hz + 40))
  for i in 1 2 3 4 5; do
    ep=$(printf "S01E%02d" "$i")
    make_clip "${MEDIA_DIR}/${folder}/${show}/${ep}.mp4" "$label" "${show} ${ep}" "$hz"
  done
done

echo "==> Generating break-time cards in ${MEDIA_DIR}/breaks"
make_card "${MEDIA_DIR}/breaks/bathroom-break.mp4" "BATHROOM BREAK" 330
make_card "${MEDIA_DIR}/breaks/snack-time.mp4" "SNACK TIME" 392
make_card "${MEDIA_DIR}/breaks/stretch.mp4" "STRETCH TIME" 440
make_card "${MEDIA_DIR}/breaks/water-break.mp4" "WATER BREAK" 494
make_card "${MEDIA_DIR}/breaks/old-ad-1.mp4" "(pretend commercial)" 523
make_card "${MEDIA_DIR}/breaks/old-ad-2.mp4" "(pretend commercial)" 587

if [[ "$WITH_BUNNY" -eq 1 ]]; then
  BUNNY_OUT="${MEDIA_DIR}/10-movies/Grownup Movie Night/Big Buck Bunny.mp4"
  if [[ -f "$BUNNY_OUT" && "$FORCE" -eq 0 ]]; then
    echo "==> Big Buck Bunny already present, skipping download"
  else
    echo "==> Downloading Big Buck Bunny (real long-form video, for a genuine decode/resume test)"
    mkdir -p "$(dirname "$BUNNY_OUT")"
    # From the Internet Archive: download.blender.org now answers scripted
    # downloads with a bot-check page. --fail so an error page is never saved
    # as "Big Buck Bunny.mp4" (it would sit in the library as a broken episode).
    curl -L --fail --progress-bar -A "TimewarpTV-dev/1.0" \
      "https://archive.org/download/BigBuckBunny_124/Content/big_buck_bunny_720p_surround.mp4" \
      -o "$BUNNY_OUT" || { rm -f "$BUNNY_OUT"; echo "warning: Big Buck Bunny download failed - skipped" >&2; }
  fi
fi

echo "==> Writing ${CONFIG_OUT}"
cat > "$CONFIG_OUT" <<EOF
# Desktop dev config, generated by scripts/make-dev-library.sh.
# Run with:  timewarptv --config config.dev.yaml --windowed
media_root: ${MEDIA_DIR}
state_file: ${MEDIA_DIR}/.timewarptv-state.json

tune_in: random
start_offset: [2, 4]
scan_recursive: true

breaks:
  path: "breaks"
  every: 2
  count: [1, 2]

input:
  keyboard: false
  cec: false
  stdin: true

channels:
  - { number: 2,  name: "Baby TV",    path: "02-baby", breaks: false }
  - { number: 3,  name: "Playhouse",  path: "03-toddler" }
  - { number: 4,  name: "Kids",       path: "04-kids" }
  - { number: 5,  name: "Y7",         path: "05-y7" }
  - { number: 6,  name: "Kid Movies", path: "06-kid-movies", tune_in: resume, start_offset: 0 }
  - number: 9
    name: "Adult Swim"
    path: "09-adult-swim"
    passcode: "1997"
    locked_message: "ADULT SWIM - LOCKED"
  - number: 10
    name: "Movies"
    path: "10-movies"
    tune_in: resume
    start_offset: 0
    passcode: "1997"
EOF

echo "==> Done."
echo ""
echo "Try it:"
echo "  timewarptv --config config.dev.yaml --windowed"
echo ""
echo "Remote controls (they work in the video window and in this terminal):"
echo "  up/down arrow    channel up/down      left/right arrow  volume down/up"
echo "  0-9              type a channel       enter             confirm / OK"
echo "  m                mute                 p                 power/standby"
echo "  q / ctrl-c       quit"
