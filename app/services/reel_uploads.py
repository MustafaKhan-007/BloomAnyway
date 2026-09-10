"""Raw reel videos: where they live, how they get here, when they go.

Members put a raw video up with every reel-review request and every Reel of
the Week entry. These are the largest files the site takes from anybody who
isn't the owner, and unlike a course video they are wanted for about a week:
the owner watches one, writes the review, and the file has done its job.

So they get a folder of their own on the media disk rather than sharing the
one Content Hub videos sit in. That is what makes the weekly sweep at the
bottom of this file safe to write — everything in this folder is a raw reel,
so a file nothing points at any more is a file nobody will ever want.

Big ones arrive a slice at a time, the same way Studio sends a lesson video,
because Cloudflare Free refuses a request body much over 100 MB however
generous the cap here is.
"""
from __future__ import annotations

import logging
import os
import secrets
import time

from flask import current_app

from ..extensions import db
from ..models import ReelReviewApplication, ReelSubmission
from .videos import EXT_MIME, VideoError, process_video, sniff_stored

log = logging.getLogger(__name__)

#: What one request may carry. Cloudflare Free rejects a body much past
#: 100 MB whatever ``REEL_RAW_MAX_MB`` says, so anything larger has to come
#: through the chunked uploader below. A browser running our JavaScript never
#: reaches this; a browser without it gets told to trim the file.
SINGLE_MAX_BYTES = 90 * 1024 * 1024

#: How long a half-finished upload is kept before it counts as abandoned.
PART_KEEP_HOURS = 24

#: How long a file with nothing pointing at it is left alone before the sweep
#: takes it. An upload that has landed but whose row is a moment from being
#: written looks exactly like an orphan, and a day's grace costs nothing.
ORPHAN_GRACE_HOURS = 24


def storage_dir() -> str:
    return current_app.config["REEL_RAW_DIR"]


def legacy_dir() -> str:
    """Where raw reels were kept before they had a folder of their own."""
    return current_app.config.get("VIDEO_STORAGE_DIR") or ""


def parts_dir() -> str:
    return os.path.join(storage_dir(), "parts")


def max_upload_bytes() -> int:
    return current_app.config["REEL_RAW_MAX_MB"] * 1024 * 1024


def chunk_bytes() -> int:
    return current_app.config["REEL_CHUNK_MB"] * 1024 * 1024


def max_upload_label() -> str:
    """The ceiling as a person would say it: "90 MB", or "2 GB"."""
    mb = current_app.config["REEL_RAW_MAX_MB"]
    if mb >= 1024 and mb % 1024 == 0:
        return f"{mb // 1024} GB"
    if mb >= 1024:
        return f"{mb / 1024:.1f} GB"
    return f"{mb} MB"


def locate(disk_name: str) -> tuple[str, str] | None:
    """(directory, filename) of a stored raw reel, or None if it's gone.

    Uploads from before these moved into their own folder are still sitting
    in the Content Hub video directory, so both are looked in. Nothing was
    migrated: a raw reel outlives its week by days, not months.
    """
    safe = os.path.basename(disk_name or "")
    if not safe:
        return None
    for folder in (storage_dir(), legacy_dir()):
        if folder and os.path.isfile(os.path.join(folder, safe)):
            return os.path.abspath(folder), safe
    return None


def delete(disk_name: str) -> None:
    """Remove a stored raw reel from wherever it turns out to be."""
    found = locate(disk_name)
    if found is None:
        return
    _safe_remove(os.path.join(found[0], found[1]))


def _safe_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def store(file_storage) -> tuple[str, str, str, int]:
    """Save a raw reel arriving whole in the request it was posted with."""
    return process_video(file_storage, storage_dir(), SINGLE_MAX_BYTES)


# --- uploads that arrive a slice at a time -----------------------------------
# The browser cuts the file up and posts the pieces; we append each to a part
# file. No slice needs to reach the same worker as the last, because the state
# of an upload is the file itself.


def _part_path(upload_id: str) -> str:
    safe = os.path.basename(upload_id or "")
    if not safe:
        raise VideoError("That upload has expired \u2014 start it again.")
    return os.path.join(parts_dir(), safe)


def begin_upload(filename: str, declared_size: int) -> str:
    """Reserve a part file for an upload about to start, and return its id."""
    ext = os.path.splitext(os.path.basename(filename or ""))[1].lower()
    if ext not in EXT_MIME:
        raise VideoError("Please upload an MP4, MOV, WEBM or OGG video.")
    if declared_size <= 0:
        raise VideoError("That file was empty.")
    if declared_size > max_upload_bytes():
        raise VideoError(
            f"That video is over {max_upload_label()} \u2014 "
            "please trim or compress it.")
    os.makedirs(parts_dir(), exist_ok=True)
    # Somebody starting an upload is as good a moment as any to clear away the
    # ones nobody finished, and it needs no scheduler to happen.
    try:
        sweep_parts()
    except Exception:
        log.exception("reel uploads: could not sweep abandoned parts")
    upload_id = secrets.token_hex(16) + ext
    open(_part_path(upload_id), "wb").close()
    return upload_id


def append_chunk(upload_id: str, data: bytes) -> int:
    """Append one slice. Returns how many bytes have arrived so far."""
    path = _part_path(upload_id)
    if not os.path.isfile(path):
        raise VideoError("That upload has expired \u2014 start it again.")
    if os.path.getsize(path) + len(data) > max_upload_bytes():
        _safe_remove(path)
        raise VideoError(
            f"That video is over {max_upload_label()} \u2014 "
            "please trim or compress it.")
    with open(path, "ab") as fh:
        fh.write(data)
    return os.path.getsize(path)


def abort_upload(upload_id: str) -> None:
    try:
        _safe_remove(_part_path(upload_id))
    except VideoError:
        pass


def finish_upload(upload_id: str, filename: str) -> tuple[str, str, str, int]:
    """Turn a completed part file into a stored raw reel.

    Returns the same four things a whole-request upload does, so the entry
    forms don't care which way the file got here.
    """
    path = _part_path(upload_id)
    if not os.path.isfile(path):
        raise VideoError("That upload has expired \u2014 start it again.")
    size = os.path.getsize(path)
    if not size:
        _safe_remove(path)
        raise VideoError("That file was empty.")

    name = os.path.basename(filename or "")[:255]
    ext = os.path.splitext(name)[1].lower()
    if ext not in EXT_MIME:
        _safe_remove(path)
        raise VideoError("Please upload an MP4, MOV, WEBM or OGG video.")
    # A slice going missing is how a large upload ends up as something that
    # won't play, and this is the last place to catch it.
    if not sniff_stored(path, ext):
        _safe_remove(path)
        raise VideoError("That didn't look like a valid video file.")

    os.makedirs(storage_dir(), exist_ok=True)
    disk_name = secrets.token_hex(16) + ext
    try:
        os.replace(path, os.path.join(storage_dir(), disk_name))
    except OSError:
        _safe_remove(path)
        raise VideoError(
            "We couldn't save that upload just now \u2014 please try again.")
    return disk_name, EXT_MIME[ext], name, size


def claim(form, files) -> tuple[str, str, str, int] | None:
    """The raw video for an entry, however it got here. None if none came.

    A large file is already on the disk by the time the form is posted, and
    the form carries the id of what landed. A small one — or one from a
    browser that never ran our JavaScript — still rides along with the post.
    """
    upload_id = (form.get("upload_id") or "").strip()
    if upload_id:
        return finish_upload(upload_id, form.get("upload_name") or "")
    upload = files.get("raw_video")
    if not upload or not upload.filename:
        return None
    return store(upload)


# --- the weekly clear-out ----------------------------------------------------


def sweep_parts(older_than_hours: int = PART_KEEP_HOURS) -> int:
    """Clear out slices from uploads nobody finished."""
    return _sweep_folder(parts_dir(), older_than_hours, keep=set(),
                         what="slices from uploads nobody finished")


def referenced_names() -> set[str]:
    """Every raw reel file some row still points at."""
    names: set[str] = set()
    for model in (ReelReviewApplication, ReelSubmission):
        for (disk_name,) in db.session.query(model.disk_name).filter(
                model.disk_name.isnot(None)).all():
            if disk_name:
                names.add(os.path.basename(disk_name))
    return names


def sweep_orphans(older_than_hours: int = ORPHAN_GRACE_HOURS) -> int:
    """Delete raw reels no row points at any more.

    Purging the rows is only half of it. A file whose row went another way —
    an account closed, a review deleted, a commit that failed after the
    upload had already landed — is one nothing will ever ask for again, and
    before this nothing ever removed it. On a disk sized for the library that
    exists, a season of those is the difference between room and none.

    Only ever run over the reel folder. Sharing a folder with the owner's own
    videos would make this a sweep that could take one of those by mistake.
    """
    folder = storage_dir()
    if not folder or os.path.abspath(folder) == os.path.abspath(legacy_dir() or ""):
        return 0
    return _sweep_folder(folder, older_than_hours, keep=referenced_names(),
                         what="raw reels nothing points at any more")


def _sweep_folder(folder: str, older_than_hours: int, *, keep: set[str],
                  what: str) -> int:
    if not folder or not os.path.isdir(folder):
        return 0
    cutoff = time.time() - max(1, int(older_than_hours)) * 3600
    cleared = 0
    for name in os.listdir(folder):
        if name in keep:
            continue
        path = os.path.join(folder, name)
        try:
            if not os.path.isfile(path) or os.path.getmtime(path) >= cutoff:
                continue
            os.remove(path)
            cleared += 1
        except OSError:
            continue
    if cleared:
        log.info("reel uploads: cleared %s %s", cleared, what)
    return cleared
