"""Home-page spotlight: Creator of the Month and Reel of the Week.

Both slots are the owner's own pick — Creator of the Month goes to the Creator
member who turned up most in the comments this month, out of those who put an
Instagram link on their Bloom Anyway profile, and Reel of the Week is whatever
reel she found that week. Neither is an application queue; that's Reel reviews,
which is a separate feature.

Each slot carries a run-until date so a stale card doesn't sit on the home page
forever, and owners get a Bloom Anyway notification a day before one runs out.
"""
import logging
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func

from ..extensions import db
from ..models import ForumComment, ForumPost, User
from .settings import get_setting, set_setting
from .social import instagram_from_links, instagram_profile_url
from .timefmt import normalize_timezone, viewer_timezone

log = logging.getLogger(__name__)

#: how long a fresh pick runs for by default
CREATOR_RUN_DAYS = 30
REEL_RUN_DAYS = 7
#: how long before the end date owners get the heads-up
NOTICE_DAYS = 1

_SWEEP_GAP_SEC = 3600
_last_sweep_mono = 0.0


def _parse_date(raw: str | None) -> date | None:
    text = (raw or "").strip()[:10]
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def default_end(kind: str, start: date | None = None) -> date:
    """The date a freshly saved slot should run until."""
    days = CREATOR_RUN_DAYS if kind == "creator" else REEL_RUN_DAYS
    return (start or date.today()) + timedelta(days=days)


def slot_state(kind: str, today: date | None = None) -> dict:
    """Status of one spotlight slot for Studio and the expiry sweep."""
    today = today or date.today()
    if kind == "creator":
        label = "Creator of the month"
        who = (get_setting("creator_name") or "").strip()
        ends = _parse_date(get_setting("creator_expires"))
    else:
        label = "Reel of the week"
        who = (get_setting("reel_url") or "").strip()
        ends = _parse_date(get_setting("reel_expires"))
    days_left = (ends - today).days if ends else None
    return {
        "kind": kind,
        "label": label,
        "filled": bool(who),
        "subject": who,
        "ends": ends,
        "days_left": days_left,
        "expired": bool(ends and days_left is not None and days_left < 0),
        "ending_soon": bool(ends and days_left is not None
                            and 0 <= days_left <= NOTICE_DAYS),
    }


def spotlight_slots(today: date | None = None) -> list[dict]:
    return [slot_state("creator", today), slot_state("reel", today)]


# --- how much of the month somebody spent in the comments --------------------

def _owner_tz() -> str:
    """The zone the person reading Studio keeps, or UTC off a request."""
    try:
        return viewer_timezone()
    except Exception:
        return "UTC"


def month_window(tz_name: str | None = None,
                 now: datetime | None = None) -> tuple[datetime, datetime, date]:
    """``(start, end, first_day)`` — this calendar month, as stored UTC.

    A month starts and ends on the owner's own calendar, not on the server's,
    so a comment left late on the 31st in Los Angeles still belongs to the
    month she watched it arrive in. Comment timestamps are naive UTC, so the
    two ends come back the same way, ready to compare.
    """
    zone = ZoneInfo(normalize_timezone(tz_name) or _owner_tz())
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    first = moment.astimezone(zone).replace(day=1, hour=0, minute=0, second=0,
                                            microsecond=0)
    following = (first + timedelta(days=32)).replace(day=1)

    def stored(when: datetime) -> datetime:
        return when.astimezone(timezone.utc).replace(tzinfo=None)

    return stored(first), stored(following), first.date()


def month_of_record(tz_name: str | None = None,
                    now: datetime | None = None) -> date:
    """The first of the month the tally covers, on the owner's calendar."""
    return month_window(tz_name, now)[2]


def comment_tally(user_ids, tz_name: str | None = None,
                  now: datetime | None = None) -> dict[int, dict]:
    """How many comments each of these members left this month, and when last.

    Only comments still standing on a readable post are counted. One the author
    or an owner deleted is gone from the table outright; one hidden by
    moderation, by a report, or by an account closing is skipped here; and a
    comment under a hidden post counts for nothing because nobody can read it.
    So no amount of posting-then-deleting moves the number.
    """
    ids = {int(i) for i in user_ids}
    if not ids:
        return {}
    start, end, _first = month_window(tz_name, now)
    rows = (db.session.query(ForumComment.user_id,
                             func.count(ForumComment.id),
                             func.max(ForumComment.created_at))
            .join(ForumPost, ForumPost.id == ForumComment.post_id)
            .filter(ForumComment.user_id.in_(ids),
                    ForumComment.hidden.is_(False),
                    ForumPost.hidden.is_(False),
                    ForumComment.created_at >= start,
                    ForumComment.created_at < end)
            .group_by(ForumComment.user_id)
            .all())
    return {int(uid): {"comments": int(n or 0), "last_at": last}
            for uid, n, last in rows}


def _standing(row: dict) -> tuple:
    """Sort key: busiest first, and a tie goes to whoever got there first.

    Two people who both left nine comments finished level, but one of them was
    done sooner — that last comment is the moment they reached the number, so
    the earlier one leads.
    """
    return (-row["comments"],
            row["last_comment_at"] or datetime.max,
            (row["name"] or "").casefold())


# --- who can be featured -----------------------------------------------------

def eligible_creators(tz_name: str | None = None,
                      now: datetime | None = None) -> list[dict]:
    """Creator-tier members, in the order this month's comments put them.

    Linking Instagram on the Bloom Anyway profile is what makes someone
    pickable, so the list comes back ranked and :func:`eligible_split` cuts it
    into the ones who can be featured today and the ones who need that link.
    """
    rows = (User.query
            .filter(User.deleted_at.is_(None),
                    User.is_admin.is_(False),
                    User.membership.in_(("creator", "full_bloom")))
            .order_by(User.display_name, User.username)
            .all())
    tally = comment_tally([u.id for u in rows], tz_name, now)
    out = []
    for u in rows:
        handle = instagram_from_links(u.links())
        seen = tally.get(u.id) or {}
        out.append({
            "user_id": u.id,
            "name": u.public_name(),
            "email": u.email,
            "username": u.username or "",
            "tier": u.membership_label(),
            "handle": handle,
            "profile_url": instagram_profile_url(handle) if handle else "",
            "bio": (u.bio or "").strip(),
            "has_photo": bool(u.avatar_mime or (u.avatar_url or "").strip()),
            "comments": int(seen.get("comments") or 0),
            "last_comment_at": seen.get("last_at"),
        })
    out.sort(key=_standing)
    return out


def eligible_split(tz_name: str | None = None,
                   now: datetime | None = None) -> tuple[list[dict], list[dict]]:
    """``(ready, missing_instagram)`` — the two halves, each busiest first."""
    ready, missing = [], []
    for row in eligible_creators(tz_name, now):
        (ready if row["handle"] else missing).append(row)
    return ready, missing


def pick_top_commenter(tz_name: str | None = None,
                       now: datetime | None = None) -> dict | None:
    """Whoever showed up most in the comments this month, or ``None``.

    Nobody is returned when the month's comments are all from people who can't
    be featured yet — an empty month has no winner to hand the card to.
    """
    ready, _missing = eligible_split(tz_name, now)
    leader = ready[0] if ready else None
    if not leader or not leader["comments"]:
        return None
    return leader


def candidate(user_id: int) -> dict | None:
    for row in eligible_creators():
        if row["user_id"] == int(user_id):
            return row
    return None


# --- expiry notices ----------------------------------------------------------

def _notified_key(kind: str) -> str:
    return f"spotlight_{kind}_notified"


def mark_slot_saved(kind: str, *, filled: bool, end: date | None) -> None:
    """Record a slot's run-until date and re-arm its expiry notice."""
    key = "creator_expires" if kind == "creator" else "reel_expires"
    set_setting(key, end.isoformat() if (filled and end) else "")
    set_setting(_notified_key(kind), "")


def sweep_expiry_notices(today: date | None = None) -> int:
    """Notify owners a day before a spotlight slot runs out."""
    from flask import url_for

    from ..extensions import db
    from .social_graph import notify_owners

    try:
        href = url_for("admin.spotlight")
    except RuntimeError:
        href = "/admin/spotlight"   # sweep can run without a request (cron/CLI)
    today = today or date.today()
    sent = 0
    for slot in spotlight_slots(today):
        if not slot["filled"] or not slot["ends"]:
            continue
        if not (slot["ending_soon"] or slot["expired"]):
            continue
        stamp = slot["ends"].isoformat()
        if (get_setting(_notified_key(slot["kind"])) or "").strip() == stamp:
            continue
        if slot["expired"]:
            when = "has run out"
        elif slot["days_left"] == 0:
            when = "runs out today"
        else:
            when = "runs out tomorrow"
        who = slot["subject"]
        if slot["kind"] == "creator":
            tail = f" — {who} has been up all month." if who else ""
        else:
            tail = " — time to pick this week's reel."
        notify_owners(
            kind="spotlight_expiry",
            body=f"{slot['label']} {when}{tail}",
            url=href,
        )
        set_setting(_notified_key(slot["kind"]), stamp)
        sent += 1
    if sent:
        db.session.commit()
    return sent


def maybe_sweep() -> int:
    """Hourly-at-most expiry check, safe to call from any request."""
    global _last_sweep_mono
    now_mono = time.monotonic()
    if (now_mono - _last_sweep_mono) < _SWEEP_GAP_SEC:
        return 0
    _last_sweep_mono = now_mono
    try:
        return sweep_expiry_notices()
    except Exception:
        log.exception("spotlight: expiry sweep failed")
        from ..extensions import db
        try:
            db.session.rollback()
        except Exception:
            pass
        return 0
