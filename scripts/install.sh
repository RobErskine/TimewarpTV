#!/usr/bin/env bash
#
# TimewarpTV installer for Raspberry Pi OS Lite (Trixie; Bookworm works too).
#
# Installs system + Python dependencies, generates the filler assets, and
# optionally installs a systemd service so the box boots straight into "TV mode".
#
# Usage:
#   ./scripts/install.sh              # install deps + assets
#   ./scripts/install.sh --service    # ...and install & enable the systemd unit
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_SERVICE=0
for arg in "$@"; do
  case "$arg" in
    --service) INSTALL_SERVICE=1 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

echo "==> Installing system packages (mpv/libmpv, ffmpeg, cec-utils, python)"
sudo apt-get update
sudo apt-get install -y \
  mpv libmpv2 \
  ffmpeg \
  cec-utils \
  exfatprogs \
  python3 python3-pip python3-venv \
  python3-evdev

echo "==> Creating a virtual environment in ${REPO_DIR}/.venv"
python3 -m venv --system-site-packages "${REPO_DIR}/.venv"
# shellcheck source=/dev/null
source "${REPO_DIR}/.venv/bin/activate"

echo "==> Installing TimewarpTV and Python dependencies"
pip install --upgrade pip
# Editable install so that a plain `git pull` picks up code updates without
# needing to reinstall (just restart the service afterwards).
# The package was once called "nostalgiabox"; drop that registration so an
# updated box doesn't keep a stale one pointing at a folder that's gone.
pip uninstall -y -q nostalgiabox 2>/dev/null || true
pip install -e "${REPO_DIR}[pi]"

echo "==> Generating filler assets (static + colour bars)"
python -m timewarptv.static_gen || echo "   (asset generation skipped/failed - box still works)"

echo "==> Installing the retro OSD font (VT323)"
# TimewarpTV also copies this into mpv's font dir at runtime, but installing it
# system-wide makes it available everywhere (and to fontconfig).
mkdir -p "${HOME}/.local/share/fonts" "${HOME}/.config/mpv/fonts"
if compgen -G "${REPO_DIR}/timewarptv/assets/fonts/*.ttf" > /dev/null; then
  cp "${REPO_DIR}"/timewarptv/assets/fonts/*.ttf "${HOME}/.local/share/fonts/" || true
  cp "${REPO_DIR}"/timewarptv/assets/fonts/*.ttf "${HOME}/.config/mpv/fonts/" || true
  command -v fc-cache > /dev/null && fc-cache -f "${HOME}/.local/share/fonts" || true
fi

# The config lives on the media drive, not here (see README, Part E). Check it
# if the drive is already mounted; otherwise say so rather than validating a
# sample config whose example folders can't exist.
echo "==> Putting the timewarptv command on the PATH"
# It lives in the virtual environment; link it so `timewarptv --check` works
# from any shell, without activating anything.
sudo ln -sf "${REPO_DIR}/.venv/bin/timewarptv" /usr/local/bin/timewarptv

DRIVE_CONFIG="/media/nostalgiabox/config.yaml"
if [[ -f "${DRIVE_CONFIG}" ]]; then
  echo "==> Checking the library on the drive"
  timewarptv --check --config "${DRIVE_CONFIG}" || \
    echo "   (fix ${DRIVE_CONFIG}, then re-run the check below)"
else
  echo "==> Skipping the library check: the media drive isn't mounted yet"
  echo "    (install-service.sh mounts it - then run the check below)"
fi

if [[ "${INSTALL_SERVICE}" -eq 1 ]]; then
  echo "==> Installing systemd service"
  "${REPO_DIR}/scripts/install-service.sh"
fi

cat <<EOF

==> Done!

Next steps:
  1. Put your library and its config.yaml on the media drive (see README),
     plug the drive into the Pi.
  2. Turn the Pi into an appliance - boot to TV, mount the drive by label,
     re-scan whenever it's plugged back in:
         ./scripts/install-service.sh
  3. Check what it found:
         timewarptv --check

Enjoy TimewarpTV!
EOF
