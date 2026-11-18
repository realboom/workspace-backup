"""
Retention for backup folders: delete timestamp-named backup folders older than N days.

Used by both backup notebooks, and only AFTER a successful backup, so a failing job never
deletes the history it would be needed to restore from.

Safety rules:
  * Only folders named like a backup timestamp (YYYYMMDDTHHMMSSZ) are considered — anything
    else in the volume is never touched.
  * The folder written by the current run (`keep`) is never deleted.
  * retention_days = 0 disables pruning.
"""

from __future__ import annotations

import datetime as dt
import re
import shutil
from pathlib import Path

STAMP = re.compile(r"^\d{8}T\d{6}Z$")
MAX_RETENTION_DAYS = 365


def parse_retention(raw: str) -> int:
    try:
        days = int(str(raw).strip() or "0")
    except ValueError:
        raise ValueError(f"retention_days must be a whole number of days (got '{raw}').")
    if not 0 <= days <= MAX_RETENTION_DAYS:
        raise ValueError(f"retention_days must be between 0 (off) and {MAX_RETENTION_DAYS} (got {days}).")
    return days


def prune(parent: Path, retention_days: int, keep: str | None = None, now: dt.datetime | None = None) -> list[str]:
    """Delete <parent>/<timestamp> folders older than retention_days. Returns the folders deleted."""
    if retention_days <= 0 or not parent.is_dir():
        return []
    cutoff = (now or dt.datetime.now(dt.timezone.utc)) - dt.timedelta(days=retention_days)
    deleted = []
    for child in sorted(parent.iterdir()):
        if not child.is_dir() or not STAMP.match(child.name) or child.name == keep:
            continue
        taken = dt.datetime.strptime(child.name, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc)
        if taken < cutoff:
            shutil.rmtree(child)
            deleted.append(str(child))
    return deleted
