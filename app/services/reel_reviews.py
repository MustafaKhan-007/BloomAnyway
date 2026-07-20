"""Weekly reel-review lottery helpers."""
from datetime import date, timedelta
import random

from ..extensions import db
from ..models import ReelReviewApplication


def week_monday(d: date | None = None) -> date:
    """Return the Monday that starts the ISO week containing ``d``."""
    d = d or date.today()
    return d - timedelta(days=d.weekday())


def current_week_key() -> date:
    return week_monday()


def application_for(user_id: int, week: date | None = None) -> ReelReviewApplication | None:
    week = week or current_week_key()
    return ReelReviewApplication.query.filter_by(user_id=user_id, week_key=week).first()


def week_applicants(week: date | None = None):
    week = week or current_week_key()
    # Selected winner first, then by entry time
    return (ReelReviewApplication.query
            .filter_by(week_key=week)
            .order_by(ReelReviewApplication.selected.desc(),
                      ReelReviewApplication.created_at.asc())
            .all())


def pick_random_applicant(week: date | None = None) -> ReelReviewApplication | None:
    """Choose one random applicant for the week and mark them selected.

    Clears prior ``selected`` flags for that week first.
    """
    apps = week_applicants(week)
    if not apps:
        return None
    week = week or current_week_key()
    for a in apps:
        a.selected = False
    chosen = random.choice(apps)
    chosen.selected = True
    db.session.commit()
    return chosen


def is_instagram_reel_url(url: str) -> bool:
    u = (url or "").strip().lower()
    return ("instagram.com/" in u) and ("/reel/" in u or "/reels/" in u)
