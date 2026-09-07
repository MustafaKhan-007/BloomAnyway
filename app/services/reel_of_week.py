"""Reel of the Week: member entries for the home page spotlight.

Creator and Full Bloom members put forward one reel a week, provided it has
picked up at least a hundred shares. The owner features one of them; Monday
clears the rest. Weeks run on Atlanta's clock, same as reel reviews.
"""
import logging
import time
from datetime import date

from flask import current_app

from ..extensions import db
from ..models import ReelSubmission, utcnow
from .reel_reviews import atlanta_today, is_instagram_reel_url, week_monday
from .settings import set_setting

log = logging.getLogger(__name__)

#: a reel has to have travelled this far before it can be put forward
MIN_SHARES = 100

_SWEEP_GAP_SEC = 3600
_last_sweep_mono = 0.0

__all__ = [
    "MIN_SHARES",
    "atlanta_today",
    "clear_featured",
    "current_week_key",
    "feature",
    "featured_submission",
    "is_instagram_reel_url",
    "maybe_sweep",
    "purge_old_submissions",
    "submission_for",
    "sweep_old_weeks",
    "week_submissions",
]


def current_week_key() -> date:
    return week_monday(atlanta_today())


def submission_for(user_id: int, week: date | None = None) -> ReelSubmission | None:
    week = week or current_week_key()
    return ReelSubmission.query.filter_by(user_id=user_id, week_key=week).first()


def week_submissions(week: date | None = None):
    """This week's entries, the featured one first, then most shares."""
    week = week or current_week_key()
    return (ReelSubmission.query
            .filter_by(week_key=week)
            .order_by(ReelSubmission.featured.desc(),
                      ReelSubmission.share_count.desc(),
                      ReelSubmission.created_at.asc())
            .all())


def featured_submission(week: date | None = None) -> ReelSubmission | None:
    week = week or current_week_key()
    return ReelSubmission.query.filter_by(week_key=week, featured=True).first()


def feature(submission: ReelSubmission) -> None:
    """Put this entry on the home page, replacing whatever was there.

    The spotlight still reads from site settings, so featuring writes through
    to them — that keeps the hand-typed fallback working unchanged.
    """
    for other in week_submissions(submission.week_key):
        other.featured = (other.id == submission.id)
    submission.featured = True
    # These rows are cleared out on Monday. Having been on the home page is
    # something Creator of the Month looks at, so the fact is kept on the
    # member — the first time it happened, which is what "at least once" needs.
    if submission.author is not None and submission.author.reel_featured_at is None:
        submission.author.reel_featured_at = utcnow()
    who = submission.author.public_name() if submission.author else ""
    set_setting("reel_url", submission.reel_url)
    if who:
        set_setting("reel_description", f"By {who} · {submission.share_count:,} shares")
    db.session.commit()


def clear_featured(week: date | None = None) -> None:
    """Take this week's pick off the home page."""
    for row in week_submissions(week):
        row.featured = False
    set_setting("reel_url", "")
    set_setting("reel_description", "")
    db.session.commit()


def purge_old_submissions(before: date | None = None) -> int:
    """Drop entries from finished weeks and their raw uploads.

    The featured reel already lives in site settings by then, so nothing on
    the home page depends on these rows surviving.
    """
    before = before or current_week_key()
    stale = (ReelSubmission.query
             .filter(ReelSubmission.week_key < before)
             .all())
    if not stale:
        return 0
    from .videos import delete_stored

    store = current_app.config["VIDEO_STORAGE_DIR"]
    for row in stale:
        if row.disk_name:
            delete_stored(store, row.disk_name)
        db.session.delete(row)
    db.session.commit()
    log.info("Reel of the week: cleared %s entries from past weeks.", len(stale))
    return len(stale)


def sweep_old_weeks() -> dict:
    """Monday's clear-out for both reel queues."""
    from . import reel_reviews

    return {
        "reel_reviews": reel_reviews.purge_old_applications(),
        "reel_of_week": purge_old_submissions(),
    }


def maybe_sweep() -> dict:
    """Hourly-at-most clear-out, safe to call from any request."""
    global _last_sweep_mono
    now_mono = time.monotonic()
    if (now_mono - _last_sweep_mono) < _SWEEP_GAP_SEC:
        return {}
    _last_sweep_mono = now_mono
    try:
        return sweep_old_weeks()
    except Exception:
        log.exception("reel queues: weekly clear-out failed")
        try:
            db.session.rollback()
        except Exception:
            pass
        return {}
