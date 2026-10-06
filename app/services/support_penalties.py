"""Booking a seat and not turning up, and what happens if it keeps happening.

A peer support session is eight seats and somebody's evening. A seat taken
and not used is one a member who would have come couldn't have, and a host
who doesn't open the room wastes everybody who did. Neither is malice —
mostly it is a day that got away from somebody — so the first one costs
nothing at all beyond being told.

After that it escalates, and the ladder is deliberately slow at the bottom
and steep at the top: the second miss is a day, which is barely a punishment
and mostly a message, and only somebody missing again and again reaches the
weeks. Nobody is ever shut out for good.

**Cancelling is always free.** Leaving a session sets the seat to
``cancelled`` and a host pulling out cancels the whole meeting, so neither is
ever settled as a no-show. Freeing a seat in time is the behaviour this is
trying to produce; punishing it would be perverse.

**Only group sessions count.** A paid 1:1 that somebody misses has already
cost them the fee, and taking the groups away on top is charging twice for
one mistake.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from ..extensions import db
from ..models import SupportGroupNoShow, User, utcnow

log = logging.getLogger(__name__)

#: Days locked out after each miss, counted from the miss that earned it.
#: The first is nought — a warning and nothing else. Past the end of the
#: list it stays at the last value: the ladder has a top, and somebody who
#: keeps missing is a conversation for the owner to have, not a number to
#: keep doubling.
PENALTY_DAYS = (0, 1, 2, 5, 10, 20, 30)

#: How long a miss is counted for. One in March and one in November is not a
#: pattern, and a ladder with no memory limit means a member who had a bad
#: fortnight a year ago is still paying for it.
MEMORY_DAYS = 180

#: Kinds of session a miss is counted on. 1:1s are paid and are left out.
COUNTED_KINDS = ("peer", "facilitator")


def penalty_days(count: int) -> int:
    """Days out for somebody's ``count``-th miss (1 for their first)."""
    if count <= 0:
        return 0
    if count <= len(PENALTY_DAYS):
        return PENALTY_DAYS[count - 1]
    return PENALTY_DAYS[-1]


def _memory_floor():
    return utcnow() - timedelta(days=MEMORY_DAYS)


def active_no_shows(user: User | int) -> list[SupportGroupNoShow]:
    """Every miss still being counted against them, oldest first."""
    user_id = getattr(user, "id", user)
    if not user_id:
        return []
    return (SupportGroupNoShow.query
            .filter(SupportGroupNoShow.user_id == user_id,
                    SupportGroupNoShow.forgiven_at.is_(None),
                    SupportGroupNoShow.created_at >= _memory_floor())
            .order_by(SupportGroupNoShow.created_at.asc())
            .all())


def strike_count(user: User | int) -> int:
    return len(active_no_shows(user))


def blocked_until(user: User | int):
    """When they can use support groups again, or None if they can now.

    Read off the last miss rather than stored on the account, so forgiving
    one in Studio lifts the time-out in the same breath and there is no
    second copy of the truth to fall out of step.
    """
    rows = active_no_shows(user)
    if not rows:
        return None
    days = penalty_days(len(rows))
    if days <= 0:
        return None
    until = rows[-1].created_at + timedelta(days=days)
    return until if until > utcnow() else None


def is_blocked(user: User | int) -> bool:
    return blocked_until(user) is not None


def state(user: User | int) -> dict:
    """Everything a page needs to say where somebody stands."""
    rows = active_no_shows(user)
    until = blocked_until(user)
    count = len(rows)
    return {
        "misses": count,
        "blocked": until is not None,
        "until": until,
        "days": penalty_days(count),
        # What the *next* one would cost, for the warning after the first.
        "next_days": penalty_days(count + 1),
        "last_at": rows[-1].created_at if rows else None,
    }


def block_message(user: User | int) -> str | None:
    """Why they can't, in words, or None if they can."""
    from .timefmt import format_local

    until = blocked_until(user)
    if until is None:
        return None
    when = format_local(until, "%b %d at %I:%M %p")
    return (
        "You booked a support session and didn't come, so support groups are "
        f"paused for you until {when}. Cancelling a seat you can't use is "
        "always free and never counts."
    )


# --- recording -----------------------------------------------------------

def record_no_show(seat, meeting) -> SupportGroupNoShow | None:
    """Note one missed seat. Caller commits. None if it doesn't count.

    Idempotent on the seat: the sweep that settles finished sessions runs
    off whatever request arrives and two can reach the same one.
    """
    if seat is None or meeting is None:
        return None
    if (meeting.kind or "peer") not in COUNTED_KINDS:
        return None
    user_id = getattr(seat, "user_id", None)
    if not user_id:
        return None
    # An owner hosting is working, not attending; and the member who is also
    # an owner is not somebody this is for.
    member = db.session.get(User, user_id)
    if member is None or member.is_admin or member.deleted_at is not None:
        return None
    existing = (SupportGroupNoShow.query
                .filter_by(application_id=seat.id).first())
    if existing is not None:
        return None
    row = SupportGroupNoShow(
        user_id=user_id,
        meeting_id=meeting.id,
        application_id=seat.id,
        role=("host" if meeting.scheduled_by_user_id == user_id else "seat"),
        created_at=utcnow(),
    )
    db.session.add(row)
    return row


def tell_them(user_id: int, meeting) -> None:
    """A bell saying what happened and what it cost. Caller commits.

    Said at the moment it is counted rather than discovered later from a
    form that won't submit — somebody who doesn't know they are in a
    time-out just thinks the site is broken.
    """
    from .social_graph import notify

    count = strike_count(user_id)
    days = penalty_days(count)
    topic = ""
    try:
        from .support_groups import meeting_display_title
        topic = meeting_display_title(meeting) if meeting else ""
    except Exception:
        topic = ""
    where = f" ({topic})" if topic else ""
    if days <= 0:
        body = (f"You missed a support session you booked{where}. No harm "
                "done — but cancel next time if you can't make it, or "
                "support groups pause for a day.")
    elif days == 1:
        body = (f"You missed a support session you booked{where}. Support "
                "groups are paused for you for a day.")
    else:
        body = (f"You missed a support session you booked{where}. Support "
                f"groups are paused for you for {days} days.")
    notify(user_id, kind="support_group", body=body,
           url="/support-groups")


def record_for_meeting(meeting) -> int:
    """Count every missed seat on a session that has just finished.

    Caller commits. Returns how many were counted.
    """
    from .support_groups import meeting_seats

    if meeting is None or (meeting.kind or "peer") not in COUNTED_KINDS:
        return 0
    counted = 0
    for seat in meeting_seats(meeting, include_attended=True):
        if seat.status != "no_show" or seat.joined_at is not None:
            continue
        row = record_no_show(seat, meeting)
        if row is None:
            continue
        counted += 1
        try:
            db.session.flush()        # so the new row counts in the tally
            tell_them(seat.user_id, meeting)
        except Exception:
            log.exception("support penalties: could not tell user %s",
                          seat.user_id)
    return counted


# --- the owner's side ------------------------------------------------------

def forgive(row: SupportGroupNoShow, by: User | None) -> bool:
    """Let somebody off one miss. Caller commits.

    Lifts the time-out immediately where it was the last one, because the
    block is read off what is still counted.
    """
    if row is None or row.forgiven_at is not None:
        return False
    row.forgiven_at = utcnow()
    row.forgiven_by_id = getattr(by, "id", None)
    return True


def forgive_all(user: User | int, by: User | None) -> int:
    """Wipe somebody's slate. Caller commits."""
    rows = active_no_shows(user)
    for row in rows:
        forgive(row, by)
    return len(rows)


def recent(limit: int = 40) -> list[SupportGroupNoShow]:
    """The latest misses, for Studio to show."""
    return (SupportGroupNoShow.query
            .order_by(SupportGroupNoShow.created_at.desc())
            .limit(limit).all())


def blocked_members() -> list[dict]:
    """Everyone currently in a time-out, worst first."""
    rows = (SupportGroupNoShow.query
            .filter(SupportGroupNoShow.forgiven_at.is_(None),
                    SupportGroupNoShow.created_at >= _memory_floor())
            .all())
    out = []
    for user_id in {r.user_id for r in rows}:
        until = blocked_until(user_id)
        if until is None:
            continue
        member = db.session.get(User, user_id)
        if member is None:
            continue
        out.append({"user": member, "until": until,
                    "misses": strike_count(user_id)})
    out.sort(key=lambda row: row["until"], reverse=True)
    return out
