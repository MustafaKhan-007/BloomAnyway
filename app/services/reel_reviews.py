"""Weekly reel-review queue.

Members enter one reel a week. Owners review one a day — seven over the week —
and the slate is wiped every Monday so a fresh round starts clean.

Weeks and days run on Atlanta's clock, not the server's, so "Monday" and
"today" mean the same thing to the owner wherever the box happens to live.
"""
import logging
import random
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from ..extensions import db
from ..models import ReelReview, ReelReviewApplication

log = logging.getLogger(__name__)

#: Atlanta is US Eastern; the zone handles daylight saving for us.
ATLANTA_TZ = ZoneInfo("America/New_York")

#: one review a day, so a full week is seven
REVIEWS_PER_WEEK = 7


def atlanta_today() -> date:
    """Today's date in Atlanta."""
    return datetime.now(ATLANTA_TZ).date()


def week_monday(d: date | None = None) -> date:
    """Return the Monday that starts the week containing ``d``."""
    d = d or atlanta_today()
    return d - timedelta(days=d.weekday())


def current_week_key() -> date:
    return week_monday()


def week_range_label(week: date | None = None) -> str:
    """A week said as the span it is: "Sep 7 – 13", or "Sep 28 – Oct 4".

    Naming only the Monday reads as a stale date to anybody looking at the
    page on a Thursday — the question it got asked was why the page was
    talking about the 7th when today was the 10th. The span answers that
    without anyone having to know which day a week starts on here.
    """
    week = week or current_week_key()
    end = week + timedelta(days=6)
    if week.month == end.month:
        return f"{week.strftime('%b')} {week.day} \u2013 {end.day}"
    return (f"{week.strftime('%b')} {week.day} \u2013 "
            f"{end.strftime('%b')} {end.day}")


def application_for(user_id: int, week: date | None = None) -> ReelReviewApplication | None:
    week = week or current_week_key()
    return ReelReviewApplication.query.filter_by(user_id=user_id, week_key=week).first()


def week_applicants(week: date | None = None):
    week = week or current_week_key()
    # Reviewed entries first, then by entry time.
    return (ReelReviewApplication.query
            .filter_by(week_key=week)
            .order_by(ReelReviewApplication.selected.desc(),
                      ReelReviewApplication.created_at.asc())
            .all())


def waiting_applicants(week: date | None = None):
    """This week's entries that haven't been reviewed yet."""
    return [a for a in week_applicants(week) if a.review is None]


def published_reviews_for_week(week: date | None = None):
    """Every live review drawn from this week's entries, newest first."""
    week = week or current_week_key()
    return (ReelReview.query
            .join(ReelReviewApplication,
                  ReelReview.application_id == ReelReviewApplication.id)
            .filter(ReelReviewApplication.week_key == week,
                    ReelReview.published.is_(True))
            .order_by(ReelReview.created_at.desc())
            .all())


def review_on(day: date | None = None) -> ReelReview | None:
    """The review already published on that Atlanta day, if any."""
    day = day or atlanta_today()
    return (ReelReview.query
            .filter(ReelReview.review_date == day,
                    ReelReview.published.is_(True))
            .first())


def day_is_done(day: date | None = None) -> bool:
    """True once today's one review has gone out."""
    return review_on(day) is not None


def week_progress(week: date | None = None) -> dict:
    """How far through the week's seven reviews the owner is."""
    week = week or current_week_key()
    done = len(published_reviews_for_week(week))
    return {
        "done": done,
        "target": REVIEWS_PER_WEEK,
        "left": max(0, REVIEWS_PER_WEEK - done),
        "waiting": len(waiting_applicants(week)),
        "today_done": day_is_done(),
    }


def pick_random_applicant(week: date | None = None) -> ReelReviewApplication | None:
    """Choose a random entry that hasn't been reviewed yet and flag it.

    Only one entry carries the flag at a time — it marks who is up next, not
    who has won, so picking again simply moves it.
    """
    week = week or current_week_key()
    waiting = waiting_applicants(week)
    if not waiting:
        return None
    for a in week_applicants(week):
        if a.review is None:
            a.selected = False
    chosen = random.choice(waiting)
    chosen.selected = True
    db.session.commit()
    return chosen


def is_instagram_reel_url(url: str) -> bool:
    u = (url or "").strip().lower()
    return ("instagram.com/" in u) and ("/reel/" in u or "/reels/" in u)


def purge_old_applications(before: date | None = None) -> int:
    """Drop unreviewed entries from finished weeks, and their video files.

    Entries that were reviewed stay put — a published review points back at
    them — but nothing keeps an unreviewed reel around once its week is over,
    and the raw uploads are large.
    """
    before = before or current_week_key()
    stale = (ReelReviewApplication.query
             .filter(ReelReviewApplication.week_key < before)
             .all())
    from . import reel_uploads

    dropped = 0
    for app_row in stale:
        if app_row.review is not None:
            # Keep the row, but the raw upload has done its job.
            if app_row.disk_name:
                reel_uploads.delete(app_row.disk_name)
                app_row.disk_name = None
            # Uploads from before these went to disk are bytes in a Postgres
            # column. Only the file was ever released, so a reviewed one from
            # back then sat in the database for good — and travelled with
            # every backup taken since.
            app_row.data = None
            app_row.size = 0
            continue
        if app_row.disk_name:
            reel_uploads.delete(app_row.disk_name)
        db.session.delete(app_row)
        dropped += 1
    if stale:
        db.session.commit()
    if dropped:
        log.info("Reel reviews: cleared %s unreviewed entries from past weeks.", dropped)
    return dropped
