# 0003: Try HDMI-CEC before buying a Flirc

**Decision:** Recommend testing the existing HDMI-CEC input backend (`timewarptv/input/cec.py`,
already implemented, needs only `cec-utils` installed) with the in-laws' TV remote before buying a
Flirc USB receiver for the Argon IR remote.

**Why:** The Pi has no built-in IR receiver, so the Argon remote needs a receiver of some kind. CEC
costs nothing and needs no extra hardware if the TV supports it (most TVs since ~2010 do — Anynet+,
SimpLink, BRAVIA Sync). The Flirc (~$25) is the reliable fallback and is what the project's README
is written around. Both backends can be enabled simultaneously, so buying the Flirc later doesn't
waste the CEC setup.

**Alternatives considered:** a bare IR receiver on GPIO (`dtoverlay=gpio-ir`) — cheapest (~$2) but
needs wiring and a custom kernel keymap; not supported out of the box by this project.

**Date:** 2026-09-04
