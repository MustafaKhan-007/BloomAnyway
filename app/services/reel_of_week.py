"""Reel of the Week: member entries for the home page spotlight.

Creator and Full Bloom members put forward one reel a round, provided it has
picked up at least a hundred shares. A round is the wait for a pick: it opens
on Monday and again the moment the owner features one, so the reel that goes
up next week — or later the same week — is open to everybody rather than to
whoever happened not to have entered yet. Weeks run on Atlanta's clock, same
as reel reviews, and Monday clears the entries out.
"""
import logging
import time
from datetime import date

from ..extensions import db
from ..models import ReelSubmission, utcnow
from .reel_reviews import atlanta_today, is_instagram_reel_url, week_monday
from .settings import get_setting, set_setting

log = logging.getLogger(__name__)

#: a reel has to have travelled this far before it can be put forward
MIN_SHARES = 100

#: which week the round counter below belongs to, and how far it has got
_ROUND_WEEK_KEY = "reel_round_week"
_ROUND_NO_KEY = "reel_round_no"

_SWEEP_GAP_SEC = 3600
_last_sweep_mono = 0.0

__all__ = [
    "MIN_SHARES",
    "atlanta_today",
    "clear_featured",
    "current_round",
    "current_week_key",
    "feature",
    "featured_submission",
    "is_instagram_reel_url",
    "maybe_sweep",
    "open_next_round",
    "purge_old_submissions",
    "round_submissions",
    "submission_for",
    "sweep_old_weeks",
    "week_submissions",
]


def current_week_key() -> date:
    return week_monday(atlanta_today())


def current_round(week: date | None = None) -> int:
    """Which round of this week is open — 0 until a reel has been featured.

    Read-only: a new week starts at 0 by saying so rather than by writing
    anything, so no page load has to be the one that notices Monday.
    """
    week = week or current_week_key()
    if (get_setting(_ROUND_WEEK_KEY) or "") != week.isoformat():
        return 0
    try:
        return max(0, int(get_setting(_ROUND_NO_KEY) or 0))
    except (TypeError, ValueError):
        return 0


def open_next_round(week: date | None = None) -> int:
    """Close the round that just produced a pick and open the one after it."""
    week = week or current_week_key()
    nxt = current_round(week) + 1
    set_setting(_ROUND_WEEK_KEY, week.isoformat())
    set_setting(_ROUND_NO_KEY, str(nxt))
    log.info("Reel of the week: round %s open for the week of %s.", nxt, week)
    return nxt


def submission_for(user_id: int, week: date | None = None,
                   round_no: int | None = None) -> ReelSubmission | None:
    """This member's entry in the round that is open (or one named)."""
    week = week or current_week_key()
    if round_no is None:
        round_no = current_round(week)
    return ReelSubmission.query.filter_by(
        user_id=user_id, week_key=week, round_key=round_no).first()


def week_submissions(week: date | None = None):
    """Everything entered this week, the featured one first, then most shares.

    Every round of it: the owner is choosing from what the week has brought
    in, and an entry that lost one round is still a reel she can put up.
    """
    week = week or current_week_key()
    return (ReelSubmission.query
            .filter_by(week_key=week)
            .order_by(ReelSubmission.featured.desc(),
                      ReelSubmission.share_count.desc(),
                      ReelSubmission.created_at.asc())
            .all())


def round_submissions(week: date | None = None, round_no: int | None = None):
    """Just the entries waiting on the round that is open."""
    week = week or current_week_key()
    if round_no is None:
        round_no = current_round(week)
    return [row for row in week_submissions(week) if row.round_key == round_no]


def featured_submission(week: date | None = None) -> ReelSubmission | None:
    week = week or current_week_key()
    return ReelSubmission.query.filter_by(week_key=week, featured=True).first()


def feature(submission: ReelSubmission) -> None:
    """Put this entry on the home page, replacing whatever was there.

    Choosing one ends the round it was entered in and opens the next, so
    everybody can put a reel forward for the spotlight after this one — the
    member who just won, and the ones who entered and didn't. Waiting for
    Monday instead left the rest of the week closed to everybody who had
    already had their go.

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
    # Last, with the pick safely recorded: from here everybody may enter again.
    open_next_round(submission.week_key)


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
    from . import reel_uploads

    for row in stale:
        if row.disk_name:
            reel_uploads.delete(row.disk_name)
        db.session.delete(row)
    db.session.commit()
    log.info("Reel of the week: cleared %s entries from past weeks.", len(stale))
    return len(stale)


def sweep_old_weeks() -> dict:
    """Monday's clear-out for both reel queues, and for the disk beneath them.

    Purging the rows only frees the files those rows still knew about. What
    is left over is everything else the week put on the disk: slices from an
    upload nobody finished, and files whose row went another way — an account
    closed, a review deleted. Nothing ever removed those, so they were the
    one part of a reel that outlived its week, every week, forever.
    """
    from . import reel_reviews, reel_uploads

    cleared = {
        "reel_reviews": reel_reviews.purge_old_applications(),
        "reel_of_week": purge_old_submissions(),
    }
    cleared["parts"] = reel_uploads.sweep_parts()
    cleared["orphan_files"] = reel_uploads.sweep_orphans()
    return cleared


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
