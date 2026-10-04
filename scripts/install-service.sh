#!/usr/bin/env bash
#
# Turn the Pi into an appliance: boot straight into the TV, mount the media
# drive by its label (including when it's plugged in later), and re-scan the
# library by itself whenever the drive comes back from another computer.
#
# Usage:
#   ./scripts/install-service.sh [MEDIA_PATH] [DRIVE_LABEL]
#
#   MEDIA_PATH   where the drive is mounted     (default: /media/nostalgiabox)
#   DRIVE_LABEL  the drive's volume label       (default: WARPMEDIA)
#
# The drive is matched by LABEL, not by UUID, so a second drive formatted with
# the same name (say, a copy of the library for another house) just works.
# The label must not contain spaces.
#
# Safe to re-run: it replaces its own fstab entry, udev rule and service.
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE="${REPO_DIR}/scripts/nostalgiabox.service"
TARGET="/etc/systemd/system/nostalgiabox.service"
UDEV_RULE="/etc/udev/rules.d/99-nostalgiabox-media.rules"
FSTAB_MARKER="# nostalgiabox media drive (managed by scripts/install-service.sh)"
MEDIA_PATH="${1:-/media/nostalgiabox}"
DRIVE_LABEL="${2:-WARPMEDIA}"

RUN_USER="${SUDO_USER:-$USER}"
RUN_UID="$(id -u "${RUN_USER}")"
RUN_GID="$(id -g "${RUN_USER}")"
RUN_HOME="$(getent passwd "${RUN_USER}" | cut -d: -f6)"

if [[ ! -x "${REPO_DIR}/.venv/bin/timewarptv" ]]; then
  echo "error: ${REPO_DIR}/.venv/bin/timewarptv not found." >&2
  echo "Run ./scripts/install.sh first." >&2
  exit 1
fi
if [[ "${DRIVE_LABEL}" =~ [[:space:]] ]]; then
  echo "error: drive label '${DRIVE_LABEL}' contains a space; rename the drive." >&2
  exit 1
fi

MOUNT_UNIT="$(systemd-escape --path --suffix=mount "${MEDIA_PATH}")"

# --- 1. Mount the drive by label -------------------------------------------
# exFAT (the format that works on both Mac and Pi) has no Unix permissions, so
# ownership is set at mount time. If the drive is plugged in right now, use its
# real filesystem type; otherwise assume exFAT.
FSTYPE="exfat"
DEVICE="$(sudo blkid -L "${DRIVE_LABEL}" 2>/dev/null || true)"
if [[ -n "${DEVICE}" ]]; then
  FSTYPE="$(sudo blkid -s TYPE -o value "${DEVICE}")"
  echo "==> Found drive '${DRIVE_LABEL}' at ${DEVICE} (${FSTYPE})"
else
  echo "==> Drive '${DRIVE_LABEL}' is not plugged in right now; assuming exFAT"
fi
case "${FSTYPE}" in
  exfat|vfat|ntfs|ntfs3)
    OPTS="nofail,noatime,uid=${RUN_UID},gid=${RUN_GID},umask=022,x-systemd.device-timeout=10s" ;;
  *)
    OPTS="nofail,noatime,x-systemd.device-timeout=10s" ;;
esac

echo "==> Adding the drive to /etc/fstab (backup: /etc/fstab.nostalgiabox.bak)"
sudo mkdir -p "${MEDIA_PATH}"
sudo cp /etc/fstab /etc/fstab.nostalgiabox.bak
fstab_with_media_entry() {
  # Everything except a previous run's entry (the marker line and the line
  # after it), then the fresh entry. Exact string match - no regex surprises.
  awk -v m="${FSTAB_MARKER}" 'skip { skip = 0; next } $0 == m { skip = 1; next } { print }' "$1"
  printf '%s\nLABEL=%s  %s  %s  %s  0  0\n' \
    "${FSTAB_MARKER}" "${DRIVE_LABEL}" "${MEDIA_PATH}" "${FSTYPE}" "${OPTS}"
}
fstab_new="$(mktemp)"
fstab_with_media_entry /etc/fstab > "${fstab_new}"
sudo tee /etc/fstab < "${fstab_new}" > /dev/null
rm -f "${fstab_new}"

# fstab alone only mounts at boot. This rule also mounts the drive when it's
# plugged in while the Pi is already on.
echo "==> Installing udev rule ${UDEV_RULE} (mount on plug-in)"
sudo tee "${UDEV_RULE}" > /dev/null <<EOF
# TimewarpTV: mount the media drive whenever it is plugged in.
ACTION=="add", SUBSYSTEM=="block", ENV{ID_FS_LABEL}=="${DRIVE_LABEL}", ENV{SYSTEMD_WANTS}+="${MOUNT_UNIT}"
EOF
sudo udevadm control --reload-rules

# --- 2. The TV service ----------------------------------------------------------
echo "==> Rendering service unit for user '${RUN_USER}' (media at ${MEDIA_PATH})"
tmp="$(mktemp)"
sed \
  -e "s|__USER__|${RUN_USER}|g" \
  -e "s|__UID__|${RUN_UID}|g" \
  -e "s|__HOME__|${RUN_HOME}|g" \
  -e "s|__REPO_DIR__|${REPO_DIR}|g" \
  -e "s|__MEDIA_PATH__|${MEDIA_PATH}|g" \
  -e "s|__MEDIA_MOUNT_UNIT__|${MOUNT_UNIT}|g" \
  "${TEMPLATE}" > "${tmp}"
# install, not cp: mktemp files are private (0600), and a unit file should be
# world-readable (0644) so `systemctl cat` works for anyone.
sudo install -m 0644 "${tmp}" "${TARGET}"
rm -f "${tmp}"

echo "==> Allowing '${RUN_USER}' to power off without a password (for the"
echo "    volume-down-past-zero shutdown)"
sudo tee /etc/sudoers.d/nostalgiabox-poweroff > /dev/null <<EOF
${RUN_USER} ALL=(root) NOPASSWD: /sbin/poweroff, /usr/sbin/poweroff, /sbin/shutdown, /usr/sbin/shutdown, /usr/bin/systemctl poweroff
EOF
sudo chmod 440 /etc/sudoers.d/nostalgiabox-poweroff

echo "==> Disabling the network-wait boot stall (this box runs fully offline)"
sudo systemctl disable NetworkManager-wait-online.service 2>/dev/null || true
sudo systemctl mask NetworkManager-wait-online.service 2>/dev/null || true

echo "==> Enabling and starting everything"
sudo systemctl daemon-reload
sudo systemctl start "${MOUNT_UNIT}" 2>/dev/null || true
sudo systemctl enable nostalgiabox.service
sudo systemctl restart nostalgiabox.service

if findmnt -rn "${MEDIA_PATH}" > /dev/null; then
  MOUNTED="yes - $(findmnt -rno SOURCE,FSTYPE "${MEDIA_PATH}")"
else
  MOUNTED="no (plug it in; it will mount and the TV will start by itself)"
fi
if [[ -f "${MEDIA_PATH}/config.yaml" ]]; then
  CONFIG="found"
else
  CONFIG="NOT FOUND - put config.yaml in the root of the drive"
fi

cat <<EOF

==> Done. The TV now starts on power-up and looks after the drive itself.

  Drive mounted at ${MEDIA_PATH}:  ${MOUNTED}
  ${MEDIA_PATH}/config.yaml:       ${CONFIG}

Handy commands:
  systemctl status nostalgiabox          # is it running?
  journalctl -u nostalgiabox -b          # this boot's logs
  sudo systemctl stop nostalgiabox       # stop the TV (e.g. to test by hand)
  sudo systemctl disable nostalgiabox    # don't start on boot
EOF
