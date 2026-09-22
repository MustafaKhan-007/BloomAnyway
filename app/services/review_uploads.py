"""The owner's video on a reel review: how it gets here, and where it lands.

A review can carry a video of the owner talking a member through their reel.
It was the last upload in Studio that still had to arrive whole, and a screen
recording of any length is past the roughly 100 MB a Cloudflare Free request
body may carry — so the owner could write the review, attach the video, press
Publish, and be told only that the file was too large. Raising the cap does
nothing: the refusal happens at the edge, before the app is reached.

So it comes up a slice at a time, the same road a lesson video and a member's
raw reel already take. The finished file lands beside the Content Hub videos
in ``VIDEO_STORAGE_DIR`` — the mounted disk on Render — which is where
``/watch/reviews/<id>/stream`` reads it from and where ``delete_stored`` looks
when a review is deleted.

Deliberately *not* ``REEL_RAW_DIR``: everything in that folder is swept every
week, and ``reel_uploads.referenced_names`` only knows about the members' raw
entries. A review video parked there would be deleted out from under a live
page within days of going up.
"""
from __future__ import annotations

import logging
import os
import secrets
import time

from flask import current_app

from .videos import EXT_MIME, VideoError, process_video, sniff_stored

log = logging.getLogger(__name__)

#: What one request may carry, for the no-JavaScript fallback. Cloudflare Free
#: rejects a body much past 100 MB whatever ``REVIEW_UPLOAD_MAX_MB`` says, so
#: anything larger has to come through the chunked road below. A browser
#: running our JavaScript never reaches this.
SINGLE_MAX_BYTES = 90 * 1024 * 1024

#: How long a half-finished upload is kept before it counts as abandoned.
PART_KEEP_HOURS = 24


def storage_dir() -> str:
    """Where a finished review video lives — with the Content Hub videos."""
    return current_app.config["VIDEO_STORAGE_DIR"]


def parts_dir() -> str:
    """Slices land in a folder of their own, so a half-sent file is never
    mistaken for a finished one by anything reading the video directory."""
    return os.path.join(storage_dir(), "review_parts")


def max_upload_bytes() -> int:
    return current_app.config["REVIEW_UPLOAD_MAX_MB"] * 1024 * 1024


def chunk_bytes() -> int:
    return current_app.config["REVIEW_CHUNK_MB"] * 1024 * 1024


def max_upload_label() -> str:
    """The ceiling as a person would say it: "90 MB", or "2 GB"."""
    mb = current_app.config["REVIEW_UPLOAD_MAX_MB"]
    if mb >= 1024 and mb % 1024 == 0:
        return f"{mb // 1024} GB"
    if mb >= 1024:
        return f"{mb / 1024:.1f} GB"
    return f"{mb} MB"


def _safe_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def store(file_storage) -> tuple[str, str, str, int]:
    """Save a review video arriving whole in the request it was posted with."""
    return process_video(file_storage, storage_dir(), SINGLE_MAX_BYTES)


# --- uploads that arrive a slice at a time -----------------------------------
# The browser cuts the file up and posts the pieces; we append each to a part
# file. No slice needs to reach the same worker as the last, because the state
# of an upload is the file itself.


def _part_path(upload_id: str) -> str:
    safe = os.path.basename(upload_id or "")
    if not safe:
        raise VideoError("That upload has expired — start it again.")
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
            f"That video is over {max_upload_label()} — "
            "please trim or compress it.")
    os.makedirs(parts_dir(), exist_ok=True)
    # Starting an upload is as good a moment as any to clear away the ones
    # nobody finished, and it needs no scheduler to happen.
    try:
        sweep_parts()
    except Exception:
        log.exception("review uploads: could not sweep abandoned parts")
    upload_id = secrets.token_hex(16) + ext
    open(_part_path(upload_id), "wb").close()
    return upload_id


def append_chunk(upload_id: str, data: bytes) -> int:
    """Append one slice. Returns how many bytes have arrived so far."""
    path = _part_path(upload_id)
    if not os.path.isfile(path):
        raise VideoError("That upload has expired — start it again.")
    if os.path.getsize(path) + len(data) > max_upload_bytes():
        _safe_remove(path)
        raise VideoError(
            f"That video is over {max_upload_label()} — "
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
    """Turn a completed part file into a stored review video.

    Returns the same four things a whole-request upload does, so the publish
    form doesn't care which way the file got here.
    """
    path = _part_path(upload_id)
    if not os.path.isfile(path):
        raise VideoError("That upload has expired — start it again.")
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
            "We couldn't save that upload just now — please try again.")
    return disk_name, EXT_MIME[ext], name, size


def claim(form, files) -> tuple[str, str, str, int] | None:
    """The review video for this publish, however it got here. None if none came.

    A large file is already on the disk by the time the form is posted, and
    the form carries the id of what landed. A small one — or one from a
    browser that never ran our JavaScript — still rides along with the post.
    """
    upload_id = (form.get("review_upload_id") or "").strip()
    if upload_id:
        return finish_upload(upload_id, form.get("review_upload_name") or "")
    upload = files.get("review_video")
    if not upload or not upload.filename:
        return None
    return store(upload)


def sweep_parts(older_than_hours: int = PART_KEEP_HOURS) -> int:
    """Clear out slices from uploads nobody finished.

    Only ever the parts folder. The videos themselves sit one level up, and a
    sweep that could reach those would take the library with it.
    """
    folder = parts_dir()
    if not folder or not os.path.isdir(folder):
        return 0
    cutoff = time.time() - max(1, int(older_than_hours)) * 3600
    cleared = 0
    for name in os.listdir(folder):
        path = os.path.join(folder, name)
        try:
            if not os.path.isfile(path) or os.path.getmtime(path) >= cutoff:
                continue
            os.remove(path)
            cleared += 1
        except OSError:
            continue
    if cleared:
        log.info("review uploads: cleared %s slices from uploads nobody "
                 "finished", cleared)
    return cleared
