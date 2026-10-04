"""Resume-position persistence: survives a power cut.

Saved to ``state_file`` as ``{"<channel number>": {"path": ..., "position": ...}}``
so a "resume" tune-in channel (see ``config.py``) can pick up where a movie left
off even after the box loses power and boots fresh - the whole point of the box
working unattended at a relative's house. Every failure (missing file, a
read-only mount, corrupt JSON) is logged and ignored: resume just degrades to
in-memory-only for that run, it never crashes the box.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)


def load_state(path: Optional[Path]) -> Dict[int, Dict[str, Any]]:
    """Load previously-saved resume positions, keyed by channel number."""
    if path is None or not path.is_file():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
        return {int(k): v for k, v in raw.items()}
    except Exception:  # noqa: BLE001
        log.warning("could not read resume state from %s", path, exc_info=True)
        return {}


def save_state(path: Optional[Path], data: Dict[int, Dict[str, Any]]) -> None:
    """Persist resume positions. Writes via a temp file + rename to avoid a
    half-written file surviving an unclean shutdown."""
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump({str(k): v for k, v in data.items()}, fh)
        tmp.replace(path)
    except Exception:  # noqa: BLE001
        log.warning("could not write resume state to %s", path, exc_info=True)


__all__ = ["load_state", "save_state"]
