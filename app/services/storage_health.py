"""Is the course files disk the same one it was last time?

A disk that isn't really persistent — declared but never attached, or mounted
somewhere the app isn't writing — behaves exactly like a working one until the
next deploy, when it comes back empty. Nothing says so: uploads succeed, files
open, and then every one of them is missing at once with no explanation.

Leaving a marker on the disk and remembering it turns that into something the
app can state plainly, rather than an owner re-uploading into the same hole.
"""
from __future__ import annotations

import logging
import os
import secrets

from flask import current_app

log = logging.getLogger(__name__)

#: Written into the course files directory, and remembered in the database.
MARKER_NAME = ".storage-id"
SETTING_KEY = "course_storage_id"


def _marker_path() -> str:
    return os.path.join(current_app.config["COURSE_FILES_DIR"] or "",
                        MARKER_NAME)


def _read_marker() -> str:
    try:
        with open(_marker_path(), encoding="utf-8") as fh:
            return fh.read().strip()[:64]
    except OSError:
        return ""


def _write_marker(value: str) -> bool:
    try:
        folder = current_app.config["COURSE_FILES_DIR"] or ""
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(_marker_path(), "w", encoding="utf-8") as fh:
            fh.write(value)
        return True
    except OSError:
        log.exception("storage: could not write the disk marker")
        return False


#: How many files to look for before taking the answer as read. A library of
#: thousands doesn't need counting to know the disk under it has gone.
SCAN_LIMIT = 300


def check() -> dict:
    """How many uploaded files are missing, and whether the disk changed.

    ``missing`` is what the banner hangs on, because it stays true for as long
    as the problem does — an alarm that only fires on the one page load that
    catches the moment is an alarm nobody sees. ``swapped`` says the disk
    being written to now is not the one that held them, which is the
    difference between a stray file and storage that isn't persistent.
    """
    from ..extensions import db
    from ..models import ProductAsset
    from .settings import get_setting, set_setting

    folder = (current_app.config.get("COURSE_FILES_DIR") or "").strip()
    state = {"dir": folder, "swapped": False, "checked": False,
             "files": 0, "missing": 0, "in_database": not folder}
    if not folder:
        # Nothing to check: the bytes are in Postgres, which outlives the
        # container they were uploaded from.
        return state

    known = (get_setting(SETTING_KEY, "") or "").strip()
    found = _read_marker()
    state["checked"] = True
    try:
        rows = (ProductAsset.query
                .filter(ProductAsset.disk_name.isnot(None))
                .limit(SCAN_LIMIT).all())
    except Exception:
        return state
    state["files"] = len(rows)
    state["missing"] = sum(1 for row in rows if row.file_missing())

    # A marker that isn't the one we remember means a different disk. Worth
    # saying only when files were expected to be on it.
    if known and found != known and state["files"]:
        state["swapped"] = True
        log.error(
            "storage: %s is not the disk that held the files — %s of %s "
            "asset(s) point at bytes that are not there. Check that the "
            "persistent disk is mounted at this path.",
            folder, state["missing"], state["files"])

    fresh = found or secrets.token_hex(8)
    if not found:
        _write_marker(fresh)
    try:
        if fresh != known:
            set_setting(SETTING_KEY, fresh)
            db.session.commit()
    except Exception:
        db.session.rollback()
        log.exception("storage: could not remember the disk marker")
    return state
