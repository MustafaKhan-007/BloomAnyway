"""Peer-led support circles (Daily.co) — member schedule/join; admin for facilitator."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from flask import url_for
from sqlalchemy import func, or_
from sqlalchemy.orm import joinedload

from ..extensions import db
from ..models import (SUPPORT_CIRCLE_SEED, SupportGroupApplication,
                      SupportGroupCircle, SupportGroupMeeting,
                      SupportGroupTopicAlert, User, utcnow)
from .mailer import (
    absolute_url,
    owner_emails,
    send_facilitator_booked,
    send_facilitator_cancelled,
    send_one_on_one_booked,
    send_one_on_one_cancelled,
    send_styled_email,
    send_support_group_booked,
    send_support_group_host_cancelled,
    send_support_group_left,
    send_support_group_reminder,
)
from .social_graph import notify
from .timefmt import (format_local, local_day_bounds, normalize_timezone,
                      parse_owner_parts, to_local)
from . import daily as daily_svc

log = logging.getLogger(__name__)

_last_sweep_mono = 0.0
_SWEEP_GAP_SEC = 60

PEER_MEETING_CAP = 8
FACILITATOR_MEETING_CAP = 8
ONE_ON_ONE_CAP = 2
MAX_OPEN_SESSIONS_PER_CIRCLE = 4
#: Hosting is a day's work, so a member takes one day at a time. Sitting in
#: somebody else's session costs nothing and is not counted at all.
PEER_SESSIONS_HOSTED_PER_DAY = 1
FACILITATOR_DURATION_MINUTES = 60
ONE_ON_ONE_DURATION_MINUTES = 60


def peer_meeting_minutes() -> int:
    return daily_svc.meeting_duration_minutes()


def meeting_duration_minutes(meeting: SupportGroupMeeting | None = None) -> int:
    """Live-window length for a meeting (peer default, 60m for facilitator/1:1)."""
    kind = ((meeting.kind if meeting else None) or "peer").strip().lower()
    if kind == "facilitator":
        return FACILITATOR_DURATION_MINUTES
    if kind == "one_on_one":
        return ONE_ON_ONE_DURATION_MINUTES
    return peer_meeting_minutes()


def meeting_max_participants(meeting: SupportGroupMeeting | None = None) -> int:
    if meeting is not None and meeting.capacity:
        try:
            return max(2, min(int(meeting.capacity), 50))
        except (TypeError, ValueError):
            pass
    kind = ((meeting.kind if meeting else None) or "peer").strip().lower()
    if kind == "one_on_one":
        return ONE_ON_ONE_CAP
    if kind == "facilitator":
        return FACILITATOR_MEETING_CAP
    return PEER_MEETING_CAP


def meeting_phase(meeting: SupportGroupMeeting, *, now: datetime | None = None
                  ) -> str:
    """Return ``waiting``, ``live``, ``ended``, or ``unavailable``."""
    if meeting is None or meeting.status != "scheduled" or not meeting.scheduled_at:
        return "unavailable"
    now = now or utcnow()
    start = meeting.scheduled_at
    end = start + timedelta(minutes=meeting_duration_minutes(meeting))
    if now < start:
        return "waiting"
    if now >= end:
        return "ended"
    return "live"


def expire_past_meetings(now: datetime | None = None) -> int:
    """Mark scheduled sessions complete once their live window has ended.

    Studio "Open meetings" previously kept every past peer session forever
    because nothing flipped ``scheduled`` → ``completed`` after the room closed.
    """
    now = now or utcnow()
    # Pull anything that has already started; filter by per-meeting duration.
    rows = (
        SupportGroupMeeting.query
        .filter(
            SupportGroupMeeting.status == "scheduled",
            SupportGroupMeeting.scheduled_at.isnot(None),
            SupportGroupMeeting.scheduled_at <= now,
        )
        .all()
    )
    if not rows:
        return 0
    closed = 0
    for meeting in rows:
        end = meeting.scheduled_at + timedelta(
            minutes=meeting_duration_minutes(meeting),
        )
        if end >= now:
            continue
        room_name = (meeting.zoom_meeting_id or "").strip()
        meeting.status = "completed"
        settle_seats(meeting)
        closed += 1
        if room_name:
            try:
                daily_svc.delete_room(room_name)
            except Exception:
                log.exception("Failed to delete Daily room %s after expire", room_name)
    if closed:
        db.session.commit()
        log.info("support groups: auto-completed %s past meeting(s)", closed)
    return closed


def ensure_circles() -> list[SupportGroupCircle]:
    """Seed / backfill the catalogue from SUPPORT_CIRCLE_SEED."""
    existing = {
        c.slug: c for c in SupportGroupCircle.query.all()
    }
    changed = False
    for i, (slug, track, title, blurb, cap, meets, icon) in enumerate(SUPPORT_CIRCLE_SEED):
        if slug in existing:
            continue
        db.session.add(SupportGroupCircle(
            slug=slug, track=track, title=title, blurb=blurb,
            capacity=cap, meets_label=meets, icon=icon,
            sort_order=(i + 1) * 10, active=True,
        ))
        changed = True
    if changed:
        db.session.commit()
    return (SupportGroupCircle.query
            .order_by(SupportGroupCircle.sort_order.asc()).all())


def is_custom_circle(circle: SupportGroupCircle | None) -> bool:
    if circle is None:
        return False
    return (circle.slug or "").startswith("custom-")


def normalize_custom_topic(raw: str | None) -> str:
    """Collapse whitespace for display + overlap matching."""
    text = " ".join((raw or "").strip().split())
    return text[:80]


def custom_topic_key(raw: str | None) -> str:
    return normalize_custom_topic(raw).casefold()


def meeting_display_title(meeting: SupportGroupMeeting) -> str:
    """Circle title, custom topic, facilitator label, or 1:1 coach name."""
    kind = (meeting.kind or "peer").strip().lower()
    if kind == "one_on_one":
        coach = normalize_custom_topic(meeting.notes) or "a founder"
        return f"1:1 with {coach}"
    if kind == "facilitator":
        topic = normalize_custom_topic(meeting.notes)
        if topic:
            return topic
        circle = meeting.circle
        if circle:
            return circle.title
        return "Facilitator session"
    circle = meeting.circle
    base = circle.title if circle else "support group"
    if is_custom_circle(circle):
        topic = normalize_custom_topic(meeting.notes)
        if topic:
            return f"Custom: {topic}"
    return base


def peer_session_time_conflict(
    circle_id: int,
    when: datetime,
    *,
    topic_key: str | None = None,
    exclude_meeting_id: int | None = None,
) -> SupportGroupMeeting | None:
    """Return an overlapping scheduled peer meeting for this topic, if any.

    Fixed topics: any overlapping session on the same circle.
    Custom circles: only when the normalized custom topic name also matches.
    """
    duration = timedelta(minutes=peer_meeting_minutes())
    end = when + duration
    q = (SupportGroupMeeting.query
         .filter(
             SupportGroupMeeting.circle_id == circle_id,
             SupportGroupMeeting.status == "scheduled",
             SupportGroupMeeting.scheduled_at.isnot(None),
             SupportGroupMeeting.kind == "peer",
         ))
    if exclude_meeting_id:
        q = q.filter(SupportGroupMeeting.id != exclude_meeting_id)
    for other in q.all():
        start = other.scheduled_at
        if start is None:
            continue
        other_end = start + duration
        if when >= other_end or end <= start:
            continue
        if topic_key is None:
            return other
        if custom_topic_key(other.notes) == topic_key:
            return other
    return None


def circles_by_track(track: str | None = None) -> list[SupportGroupCircle]:
    ensure_circles()
    q = SupportGroupCircle.query.filter_by(active=True)
    if track:
        q = q.filter_by(track=track)
    return q.order_by(SupportGroupCircle.sort_order.asc()).all()


def get_circle(circle_id: int | None = None, *, slug: str | None = None
               ) -> SupportGroupCircle | None:
    ensure_circles()
    if circle_id:
        return db.session.get(SupportGroupCircle, circle_id)
    if slug:
        return SupportGroupCircle.query.filter_by(slug=slug).first()
    return None


def user_can_access_circle(user: User | None, circle: SupportGroupCircle) -> bool:
    if not user or not getattr(user, "is_authenticated", True):
        return False
    if not user.is_member() and not user.is_owner_view():
        return False
    if circle.track == "building":
        return user.has_feature("support_creator")
    if circle.track == "healing":
        return user.has_feature("support_healing")
    return False


def meeting_seat_count(meeting: SupportGroupMeeting) -> int:
    return (SupportGroupApplication.query
            .filter_by(meeting_id=meeting.id, status="selected")
            .count())


def seat_counts(meetings) -> dict[int, int]:
    """Taken seats for several meetings in one query (0 for empty ones)."""
    ids = [m.id for m in meetings]
    if not ids:
        return {}
    rows = (db.session.query(SupportGroupApplication.meeting_id,
                             func.count(SupportGroupApplication.id))
            .filter(SupportGroupApplication.meeting_id.in_(ids),
                    SupportGroupApplication.status == "selected")
            .group_by(SupportGroupApplication.meeting_id)
            .all())
    taken = dict(rows)
    return {mid: int(taken.get(mid, 0)) for mid in ids}


def meeting_spots_left(meeting: SupportGroupMeeting) -> int:
    cap = int(meeting.capacity or PEER_MEETING_CAP)
    return max(0, cap - meeting_seat_count(meeting))


def _spots_left(meeting: SupportGroupMeeting, taken: int) -> int:
    return max(0, int(meeting.capacity or PEER_MEETING_CAP) - taken)


def open_peer_sessions_by_circle(circle_ids) -> dict[int, list[SupportGroupMeeting]]:
    """Scheduled peer sessions for several topics at once, soonest first."""
    ids = list(circle_ids)
    if not ids:
        return {}
    now = utcnow()
    rows = (SupportGroupMeeting.query
            .options(joinedload(SupportGroupMeeting.circle),
                     joinedload(SupportGroupMeeting.host))
            .filter(
                SupportGroupMeeting.circle_id.in_(ids),
                SupportGroupMeeting.kind == "peer",
                SupportGroupMeeting.status == "scheduled",
                SupportGroupMeeting.scheduled_at.isnot(None),
                SupportGroupMeeting.scheduled_at > now,
            )
            .order_by(SupportGroupMeeting.scheduled_at.asc())
            .all())
    out: dict[int, list[SupportGroupMeeting]] = {cid: [] for cid in ids}
    for m in rows:
        out.setdefault(m.circle_id, []).append(m)
    return out


def open_facilitator_sessions(limit: int = 20) -> list[SupportGroupMeeting]:
    """Upcoming Studio-scheduled facilitator sessions (Daily rooms)."""
    now = utcnow()
    return (SupportGroupMeeting.query
            .options(joinedload(SupportGroupMeeting.circle),
                     joinedload(SupportGroupMeeting.host))
            .filter(
                SupportGroupMeeting.kind == "facilitator",
                SupportGroupMeeting.status == "scheduled",
                SupportGroupMeeting.scheduled_at.isnot(None),
                SupportGroupMeeting.scheduled_at > now,
            )
            .order_by(SupportGroupMeeting.scheduled_at.asc())
            .limit(limit)
            .all())


def member_hosted_session_count(circle_id: int) -> int:
    """Upcoming sessions in this topic that a member put up.

    What the four-a-topic cap counts. An owner's own sessions sit outside it
    both ways: no number of them stops her adding another, and no number of
    them takes the topic away from the members who host in it.
    """
    return (SupportGroupMeeting.query
            .outerjoin(User, SupportGroupMeeting.scheduled_by_user_id == User.id)
            .filter(
                SupportGroupMeeting.circle_id == circle_id,
                SupportGroupMeeting.kind == "peer",
                SupportGroupMeeting.status == "scheduled",
                SupportGroupMeeting.scheduled_at.isnot(None),
                SupportGroupMeeting.scheduled_at > utcnow(),
                or_(User.id.is_(None), User.is_admin.is_(False)),
            )
            .count())


def user_selected_on_meeting(user_id: int, meeting_id: int
                             ) -> SupportGroupApplication | None:
    return (SupportGroupApplication.query
            .filter_by(user_id=user_id, meeting_id=meeting_id, status="selected")
            .first())


def sessions_hosted_on(user_id: int, when: datetime,
                       tz_name: str | None = None) -> list[SupportGroupMeeting]:
    """Peer sessions this person already hosts on the day ``when`` falls on.

    Their day, not the server's. They pick times on the clock in their
    settings, and an evening in Karachi is the small hours of the next day in
    UTC — counted the other way, a member would lose a Tuesday they never used.
    """
    start, end = local_day_bounds(when, tz_name)
    return (SupportGroupMeeting.query
            .filter(
                SupportGroupMeeting.scheduled_by_user_id == user_id,
                SupportGroupMeeting.kind == "peer",
                SupportGroupMeeting.status.in_(("draft", "scheduled")),
                SupportGroupMeeting.scheduled_at.isnot(None),
                SupportGroupMeeting.scheduled_at >= start,
                SupportGroupMeeting.scheduled_at < end,
            )
            .order_by(SupportGroupMeeting.scheduled_at.asc())
            .all())


def can_schedule_peer(user: User) -> tuple[bool, str | None]:
    """Whether this person may host at all. Which day is a separate question.

    The day is settled when they name one, in :func:`schedule_peer_session`,
    because somebody with a session on Tuesday is free on Wednesday and a page
    that took the form away would only be wrong.
    """
    if not user or not user.is_member():
        return False, "Support groups are for Healing, Creator, and Full Bloom members."
    return True, None


def circle_stats() -> list[dict]:
    """Per-circle cards for the public page + Studio overview.

    Every circle's sessions and every session's seat count come from one query
    each — counting seats per meeting was the single most expensive thing on
    the Studio dashboard.
    """
    circles = circles_by_track()
    by_circle = open_peer_sessions_by_circle([c.id for c in circles])
    taken = seat_counts([m for rows in by_circle.values() for m in rows])
    out = []
    for c in circles:
        sessions = by_circle.get(c.id, [])
        seats = {m.id: taken.get(m.id, 0) for m in sessions}
        spots = {m.id: _spots_left(m, seats[m.id]) for m in sessions}
        open_n = len(sessions)
        joinable = sum(1 for n in spots.values() if n > 0)
        seated = sum(seats.values())
        # Hosts came back with the sessions, so the members' share of them is
        # counted here rather than asked for again per circle.
        member_hosted = sum(1 for m in sessions
                            if not (m.host is not None and m.host.is_admin))
        out.append({
            "circle": c,
            "open_sessions": open_n,
            "joinable_sessions": joinable,
            "member_sessions": member_hosted,
            "sessions_full": member_hosted >= MAX_OPEN_SESSIONS_PER_CIRCLE,
            "sessions": sessions,
            "session_seats": seats,
            "session_spots": spots,
            "seated": seated,
        })
    return out


def upcoming_for_user(user: User, limit: int = 12) -> list[SupportGroupApplication]:
    """Selected seats on upcoming peer (or facilitator) sessions."""
    rows = (SupportGroupApplication.query
            .options(joinedload(SupportGroupApplication.meeting)
                     .joinedload(SupportGroupMeeting.circle),
                     joinedload(SupportGroupApplication.circle))
            .filter_by(user_id=user.id, status="selected")
            .order_by(SupportGroupApplication.created_at.desc())
            .limit(40)
            .all())
    out = []
    now = utcnow()
    for row in rows:
        m = row.meeting
        if m is None or m.status != "scheduled" or not m.scheduled_at:
            continue
        if m.scheduled_at <= now:
            continue
        out.append(row)
        if len(out) >= limit:
            break
    out.sort(key=lambda r: r.meeting.scheduled_at or now)
    return out


def open_meetings():
    expire_past_meetings()
    return (SupportGroupMeeting.query
            .options(joinedload(SupportGroupMeeting.circle),
                     joinedload(SupportGroupMeeting.host))
            .filter(SupportGroupMeeting.status.in_(("draft", "scheduled")))
            .order_by(SupportGroupMeeting.created_at.desc())
            .all())


def recent_meetings(limit: int = 20):
    return (SupportGroupMeeting.query
            .options(joinedload(SupportGroupMeeting.circle),
                     joinedload(SupportGroupMeeting.host))
            .filter(SupportGroupMeeting.status.in_(("completed", "cancelled")))
            .order_by(SupportGroupMeeting.created_at.desc())
            .limit(limit)
            .all())


#: Every seat somebody took, whatever became of it. ``selected`` while the
#: session is still to come or running, then ``attended`` or ``no_show`` once
#: it has finished and each seat has been settled against the room.
SEATED_STATUSES = ("selected", "attended", "no_show")


def meeting_seats(meeting: SupportGroupMeeting, *, include_attended: bool = False):
    """Seats on a meeting. Live seats by default; finishing settles them.

    ``include_attended`` is for anything looking back at a session that has
    already run — a finished meeting has no ``selected`` seats left, so a
    caller after the fact that only asks for those finds nobody.
    """
    statuses = SEATED_STATUSES if include_attended else ("selected",)
    return (SupportGroupApplication.query
            .options(joinedload(SupportGroupApplication.author))
            .filter(SupportGroupApplication.meeting_id == meeting.id,
                    SupportGroupApplication.status.in_(statuses))
            .order_by(SupportGroupApplication.created_at.asc())
            .all())


def seats_for_meetings(meetings, *, include_past: bool = False) -> dict[int, list]:
    """Seat rows for several meetings in one query, keyed by meeting id."""
    ids = [m.id for m in meetings if m is not None]
    if not ids:
        return {}
    statuses = SEATED_STATUSES if include_past else ("selected",)
    rows = (SupportGroupApplication.query
            .options(joinedload(SupportGroupApplication.author))
            .filter(SupportGroupApplication.meeting_id.in_(ids),
                    SupportGroupApplication.status.in_(statuses))
            .order_by(SupportGroupApplication.created_at.asc())
            .all())
    out: dict[int, list] = {mid: [] for mid in ids}
    for row in rows:
        out.setdefault(row.meeting_id, []).append(row)
    return out


def mark_joined(meeting: SupportGroupMeeting, user: User) -> bool:
    """Remember that somebody was in the room. True the first time.

    Stamped as the room is handed over, which is the only moment the site can
    honestly say anybody turned up. How long they stayed is their business:
    ducking in for five minutes of a session you booked is still showing up,
    and the alternative is a stopwatch nobody asked for.
    """
    seat = user_selected_on_meeting(getattr(user, "id", 0), meeting.id)
    if seat is None or seat.joined_at is not None:
        return False
    seat.joined_at = utcnow()
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        log.exception("Could not record %s joining meeting %s",
                      getattr(user, "id", None), meeting.id)
        return False
    return True


def settle_seats(meeting: SupportGroupMeeting) -> None:
    """Close every live seat on a finished session against who was in the room.

    Caller commits.
    """
    for seat in meeting_seats(meeting):
        seat.status = "attended" if seat.joined_at is not None else "no_show"


def turnout(meeting: SupportGroupMeeting, seats=None) -> dict:
    """Who came, out of who booked, for a session that has already run.

    ``tracked`` is false for sessions that finished before the site started
    noting arrivals: every seat on those was marked attended on the way out,
    and there is no honest way to say now who was really there.
    """
    rows = seats if seats is not None else meeting_seats(meeting,
                                                         include_attended=True)
    joined = sum(1 for seat in rows if seat.joined_at is not None)
    missed = sum(1 for seat in rows if seat.status == "no_show")
    return {"joined": joined, "booked": len(rows),
            "tracked": bool(joined or missed or not rows)}


def wrap_peers(meeting: SupportGroupMeeting, viewer: User) -> list[User]:
    """Other seated members shown on the post-session wrap page.

    Seats from the whole session, not only live ones: the wrap page is reached
    after the session is over, and by then every seat has been settled.

    The Studio account that hosts a 1:1 is left off. It is seated to make the
    room, and putting it here would hand the member a profile card and a
    report button for a name they never booked.
    """
    one_on_one = (meeting.kind or "").strip().lower() == "one_on_one"
    peers = []
    for seat in meeting_seats(meeting, include_attended=True):
        user = seat.author
        if not user or user.deleted_at or user.id == viewer.id:
            continue
        if one_on_one and user.id == meeting.scheduled_by_user_id:
            continue
        peers.append(user)
    return peers


def user_topic_alert_ids(user_id: int) -> set[int]:
    rows = (SupportGroupTopicAlert.query
            .filter_by(user_id=user_id)
            .all())
    return {r.circle_id for r in rows}


def toggle_topic_alert(user: User, circle_id: int
                       ) -> tuple[bool | None, str | None]:
    """Subscribe/unsubscribe. Returns (now_on, error)."""
    circle = get_circle(circle_id)
    if circle is None or not circle.active:
        return None, "That topic isn’t available."
    if not user_can_access_circle(user, circle):
        return None, "That topic isn’t included in your plan."

    row = (SupportGroupTopicAlert.query
           .filter_by(user_id=user.id, circle_id=circle.id)
           .first())
    if row:
        db.session.delete(row)
        db.session.commit()
        return False, None

    db.session.add(SupportGroupTopicAlert(
        user_id=user.id, circle_id=circle.id, created_at=utcnow(),
    ))
    db.session.commit()
    return True, None


def _meeting_room_url(meeting: SupportGroupMeeting) -> str:
    try:
        return url_for("main.support_session_room", meeting_id=meeting.id)
    except RuntimeError:
        return f"/support-groups/meetings/{meeting.id}/room"


def _studio_sessions_url() -> str:
    """Where Studio finishes a session, reachable off a request too."""
    try:
        return url_for("admin.support_groups")
    except RuntimeError:
        return "/admin/support-groups"


def _circle_browse_url(circle_id: int | None) -> str:
    try:
        base = url_for("main.support_groups_page")
    except RuntimeError:
        base = "/support-groups"
    if circle_id:
        return f"{base}#circle-{circle_id}"
    return base


def _member_safe_error(err: str) -> str:
    """Keep host-configuration wording out of a member's face.

    Video-room failures come back phrased for whoever runs the site ("set
    DAILY_API_KEY..."). A member can't act on that, and it reads like the site
    is broken on purpose.
    """
    text = (err or "").strip()
    if "DAILY_API_KEY" in text or "Daily" in text:
        return ("We couldn't open a video room for that time — the booking "
                "wasn't made. Try again in a minute, and let us know if it "
                "keeps happening.")
    return text or "Something went wrong scheduling that session."


def schedule_peer_session(
    user: User,
    *,
    circle_id: int,
    date_s: str,
    time_s: str,
    tz_name: str | None = None,
    topic_title: str | None = None,
) -> tuple[SupportGroupMeeting | None, str | None]:
    """Member schedules a peer support session for a topic."""
    ok, err = can_schedule_peer(user)
    if not ok:
        return None, err

    circle = get_circle(circle_id)
    if circle is None or not circle.active:
        return None, "Choose a support group topic."
    if not user_can_access_circle(user, circle):
        if circle.track == "building":
            return None, "Creator accountability groups aren’t included in your plan."
        return None, "Healing peer groups aren’t included in your plan."

    # Every cap below keeps one member from filling a topic on their own. An
    # owner running the place is not that member: they may put up as many
    # sessions as they like, in one topic, at whatever times suit them. An
    # owner previewing the site as a member is held to the member's rules.
    owner_view = bool(user.is_owner_view())

    if (not owner_view
            and member_hosted_session_count(circle.id)
            >= MAX_OPEN_SESSIONS_PER_CIRCLE):
        return None, (
            f"Members already have {MAX_OPEN_SESSIONS_PER_CIRCLE} sessions "
            f"coming up in {circle.title}. Join one of those, or try "
            "another topic."
        )

    zone = tz_name or getattr(user, "timezone", None)
    when = parse_owner_parts(date_s, time_s, zone)
    if when is None:
        return None, "Pick a date and time for the session."
    if when <= utcnow():
        return None, "Choose a time in the future."

    # One a day to host, for a member.
    if not owner_view:
        held = sessions_hosted_on(user.id, when, zone)
        if len(held) >= PEER_SESSIONS_HOSTED_PER_DAY:
            at = format_local(held[0].scheduled_at, "%b %d at %I:%M %p",
                              tz_name=zone or "UTC")
            return None, (
                f"You're already hosting a session that day — {at}. "
                "One a day is the limit for hosting; sit in as many of "
                "everybody else's as you like."
            )

    custom = is_custom_circle(circle)
    topic = normalize_custom_topic(topic_title) if custom else ""
    if custom and not topic:
        return None, "Name your custom topic (what this session is about)."
    topic_key = custom_topic_key(topic) if custom else None

    if not owner_view:
        conflict = peer_session_time_conflict(
            circle.id, when, topic_key=topic_key,
        )
        if conflict is not None:
            label = meeting_display_title(conflict) if custom else circle.title
            return None, (
                f"There’s already a {label} session at that time. "
                "Pick a different time, or join the existing one."
            )

    meeting = SupportGroupMeeting(
        circle_id=circle.id,
        capacity=PEER_MEETING_CAP,
        kind="peer",
        scheduled_by_user_id=user.id,
        status="draft",
        notes=topic if custom else None,
        created_at=utcnow(),
    )
    db.session.add(meeting)
    db.session.flush()

    seat = SupportGroupApplication(
        user_id=user.id,
        circle_id=circle.id,
        meeting_id=meeting.id,
        message="",
        status="selected",
        created_at=utcnow(),
    )
    db.session.add(seat)
    db.session.commit()

    err = schedule_meeting(meeting, scheduled_at=when, owner=user)
    if err:
        # The room never existed, so drop the draft outright rather than
        # leaving a cancelled shell in the member's history.
        log.warning("peer session room failed for circle %s: %s", circle.id, err)
        db.session.delete(seat)
        db.session.delete(meeting)
        db.session.commit()
        return None, _member_safe_error(err)
    return meeting, None


def schedule_studio_session(
    owner: User,
    *,
    kind: str,
    date_s: str,
    time_s: str,
    tz_name: str | None = None,
    title: str | None = None,
    coach: str | None = None,
    member_email: str | None = None,
) -> tuple[SupportGroupMeeting | None, str | None]:
    """Studio creates a facilitator or 1:1 session with a Daily.co room."""
    kind = (kind or "").strip().lower()
    if kind not in ("facilitator", "one_on_one"):
        return None, "Choose facilitator or 1:1."

    when = parse_owner_parts(date_s, time_s, tz_name or getattr(owner, "timezone", None))
    if when is None:
        return None, "Pick a date and time for the session."
    if when <= utcnow():
        return None, "Choose a time in the future."

    if kind == "facilitator":
        topic = normalize_custom_topic(title) or "Facilitator session"
        capacity = FACILITATOR_MEETING_CAP
        notes = topic
        guest = None
    else:
        coach_name = normalize_custom_topic(coach) or ""
        if coach_name.casefold() not in ("ayesha", "saman"):
            return None, "Choose Ayesha or Saman for the 1:1."
        coach_name = "Ayesha" if coach_name.casefold() == "ayesha" else "Saman"
        capacity = ONE_ON_ONE_CAP
        notes = coach_name
        email = (member_email or "").strip().lower()
        if not email or "@" not in email:
            return None, "Enter the member’s email so we can seat them."
        guest = (User.query
                 .filter(func.lower(User.email) == email,
                         User.deleted_at.is_(None))
                 .first())
        if guest is None:
            return None, f"No account found for {email}."

    meeting = SupportGroupMeeting(
        circle_id=None,
        capacity=capacity,
        kind=kind,
        scheduled_by_user_id=owner.id,
        status="draft",
        notes=notes,
        created_at=utcnow(),
    )
    db.session.add(meeting)
    db.session.flush()

    # Seat the Studio host (facilitator / coach) and optional 1:1 guest.
    db.session.add(SupportGroupApplication(
        user_id=owner.id,
        circle_id=None,
        meeting_id=meeting.id,
        message="",
        status="selected",
        created_at=utcnow(),
    ))
    if guest is not None and guest.id != owner.id:
        db.session.add(SupportGroupApplication(
            user_id=guest.id,
            circle_id=None,
            meeting_id=meeting.id,
            message="",
            status="selected",
            created_at=utcnow(),
        ))
    db.session.commit()

    err = schedule_meeting(meeting, scheduled_at=when, owner=owner)
    if err:
        cancel_meeting(meeting, owner=owner)
        return None, err
    return meeting, None


def meeting_lock_query(meeting_id: int):
    """The locking read used before seating someone.

    Split out from ``_lock_meeting`` so a test can check the lock is still
    asked for: SQLite drops ``FOR UPDATE`` silently, so running this proves
    nothing on its own.
    """
    return (SupportGroupMeeting.query
            .filter_by(id=meeting_id)
            .with_for_update())


def _lock_meeting(meeting_id: int) -> SupportGroupMeeting | None:
    """Re-read a meeting with its row locked, to hold the seat count still.

    SQLite ignores ``FOR UPDATE`` and doesn't need it — it serialises writers
    across the whole database. Postgres is where this earns its keep.
    """
    return meeting_lock_query(meeting_id).first()


def join_peer_session(user: User, meeting_id: int
                     ) -> tuple[SupportGroupApplication | None, str | None]:
    meeting = db.session.get(SupportGroupMeeting, meeting_id)
    if meeting is None or meeting.status != "scheduled":
        return None, "That session isn’t open to join."
    kind = (meeting.kind or "peer").strip().lower()
    if kind not in ("peer", "facilitator"):
        return None, "That session isn’t open to join."
    if kind == "peer":
        if not meeting.circle or not meeting.circle.active:
            return None, "That topic isn’t available."
        if not user_can_access_circle(user, meeting.circle):
            return None, "That session isn’t included in your plan."
    elif kind == "facilitator":
        if not user.is_owner_view() and not user.is_healing():
            return None, "Facilitator sessions are for Healing & Full Bloom members."
    if not meeting.scheduled_at or meeting.scheduled_at <= utcnow():
        return None, "That session has already started or ended."

    # Before the capacity check, so someone who already holds a seat is handed
    # it back rather than told the session is full.
    existing = user_selected_on_meeting(user.id, meeting.id)
    if existing:
        return existing, None

    # Nothing counts how many seats one person holds, here or anywhere: a seat
    # costs the room nothing and the member only their evening, so somebody
    # who wants three circles this week can sit in three.
    #
    # From here the seat count has to hold still. Counting free seats and then
    # inserting is a race: two people clicking at the same moment both count
    # the same empty seat before either has taken it, and both get in.
    locked = _lock_meeting(meeting.id)
    if locked is not None:
        meeting = locked
    if meeting.status != "scheduled":
        return None, "That session isn’t open to join."
    if meeting_spots_left(meeting) <= 0:
        cap = meeting.capacity or meeting_max_participants(meeting)
        return None, f"That session is full ({cap} seats max)."

    row = SupportGroupApplication(
        user_id=user.id,
        circle_id=meeting.circle_id,
        meeting_id=meeting.id,
        message="",
        status="selected",
        created_at=utcnow(),
    )
    db.session.add(row)
    db.session.commit()
    _notify_joiner(meeting, user)
    return row, None


def leave_peer_session(user: User, meeting_id: int) -> str | None:
    meeting = db.session.get(SupportGroupMeeting, meeting_id)
    if meeting is None:
        return "Session not found."
    row = user_selected_on_meeting(user.id, meeting.id)
    if row is None:
        return "You're not in that session."

    # Host leaving cancels the whole peer session.
    if meeting.scheduled_by_user_id == user.id and meeting.kind == "peer":
        cancel_meeting(meeting, owner=user)
        return None

    topic = _circle_name(meeting)
    day, _time = _session_date_and_time(user, meeting.scheduled_at)
    row.status = "cancelled"
    db.session.commit()
    try:
        send_support_group_left(
            user.email,
            group_topic=topic,
            session_date=day,
        )
    except Exception:
        log.exception("Support-group leave email failed for user %s", user.id)
    return None


#: A 1:1 cancelled with less notice than this is not refundable.
ONE_ON_ONE_REFUND_HOURS = 24


def one_on_one_refundable(meeting: SupportGroupMeeting,
                          now: datetime | None = None) -> bool:
    """True when there's still at least a day before the session starts."""
    if meeting is None or not meeting.scheduled_at:
        return False
    now = now or utcnow()
    return meeting.scheduled_at - now >= timedelta(hours=ONE_ON_ONE_REFUND_HOURS)


def cancel_one_on_one(user: User, meeting_id: int) -> tuple[str | None, bool]:
    """Member cancels their own 1:1. Returns ``(error, refundable)``.

    A 1:1 has one member in it, so dropping the seat ends the session: the
    room goes away and the founder's slot frees up. Refunds are the owner's
    to issue, so the alert says plainly whether one is owed.
    """
    meeting = db.session.get(SupportGroupMeeting, meeting_id)
    if meeting is None or (meeting.kind or "").strip().lower() != "one_on_one":
        return "Session not found.", False
    seat = user_selected_on_meeting(user.id, meeting.id)
    if seat is None:
        return "That isn't your session.", False
    if meeting.status != "scheduled":
        return "That session is already cancelled.", False
    if meeting.scheduled_at and meeting.scheduled_at <= utcnow():
        return "That session has already started — reach out to us instead.", False

    refundable = one_on_one_refundable(meeting)
    coach = normalize_custom_topic(meeting.notes) or "a founder"
    day, at_time = _session_date_and_time(user, meeting.scheduled_at)
    room_name = (meeting.zoom_meeting_id or "").strip()

    meeting.status = "cancelled"
    seat.status = "cancelled"
    try:
        from .coaching_intake import intake_for_meeting
        intake = intake_for_meeting(meeting.id)
        if intake is not None and intake.status in ("paid", "scheduled",
                                                    "pending_payment"):
            intake.status = "cancelled"
    except Exception:
        log.exception("Failed syncing cancelled intake for meeting %s", meeting.id)
    db.session.commit()

    if room_name:
        try:
            daily_svc.delete_room(room_name)
        except Exception:
            log.exception("Failed to delete Daily room %s", room_name)

    from .social_graph import notify_owners
    money = ("A refund is due — they cancelled more than "
             f"{ONE_ON_ONE_REFUND_HOURS} hours ahead."
             if refundable else
             f"No refund is due — cancelled inside {ONE_ON_ONE_REFUND_HOURS} hours.")
    notify_owners(
        kind="support_group_alert",
        body=f"{user.public_name()} cancelled their 1:1 with {coach} "
             f"on {day} at {at_time}. {money}"[:300],
        url=url_for("admin.support_groups"),
        actor_id=user.id,
    )
    db.session.commit()

    # Template #18 tells people the founder cancelled and a refund is coming,
    # which is wrong both ways here, so this one is written out in full.
    if refundable:
        money_line = (
            f"Because you cancelled more than {ONE_ON_ONE_REFUND_HOURS} hours "
            "ahead, your payment is being refunded to the card you used. "
            "It usually lands within 5-10 business days."
        )
    else:
        money_line = (
            f"This one was inside the {ONE_ON_ONE_REFUND_HOURS}-hour window, "
            "so it isn't refundable — that time was being held for you."
        )
    try:
        send_styled_email(
            user.email,
            subject=f"Your 1:1 with {coach} is cancelled",
            preview=f"Your session on {day} is cancelled.",
            header="1:1 coaching",
            title=f"Your 1:1 with {coach} is cancelled",
            body=(f"You cancelled your session on {day} at {at_time}. "
                  f"{money_line}\n\n"
                  "Whenever you're ready, you can book a new time."),
            button_text="Book another time",
            button_url=_circle_browse_url(None) + "#coaching",
        )
    except Exception:
        log.exception("1:1 cancel email failed for user %s", user.id)
    return None, refundable


def user_one_on_one(user: User, meeting_id: int) -> SupportGroupMeeting | None:
    """The member's own upcoming 1:1, or ``None``."""
    meeting = db.session.get(SupportGroupMeeting, meeting_id)
    if meeting is None or (meeting.kind or "").strip().lower() != "one_on_one":
        return None
    if meeting.status != "scheduled":
        return None
    if user_selected_on_meeting(user.id, meeting.id) is None:
        return None
    return meeting


def meeting_coach_key(meeting: SupportGroupMeeting) -> str | None:
    """``saman`` / ``ayesha`` from the linked intake or the meeting notes."""
    from .coaching_intake import intake_for_meeting, normalize_coach
    intake = intake_for_meeting(meeting.id)
    if intake is not None:
        key = normalize_coach(intake.coach)
        if key:
            return key
    return normalize_coach(meeting.notes or "")


def request_one_on_one_reschedule(
    user: User, meeting_id: int, slot_utc: datetime | None,
) -> str | None:
    """Move a member's 1:1 onto an open coach slot. Returns an error or ``None``."""
    from .coaching_intake import slot_still_open
    from .social_graph import notify_owners
    from .timefmt import format_local

    meeting = user_one_on_one(user, meeting_id)
    if meeting is None:
        return "That isn't your session."
    if meeting.scheduled_at and meeting.scheduled_at <= utcnow():
        return "That session has already started — reach out to us instead."
    if slot_utc is None:
        return "Pick an available date and time."
    slot_utc = slot_utc.replace(microsecond=0)
    if slot_utc <= utcnow():
        return "Pick a time that hasn't passed."
    coach = meeting_coach_key(meeting)
    if not coach:
        return "We couldn't tell whose calendar that 1:1 belongs to."
    if not slot_still_open(coach, slot_utc):
        return "That time isn’t available — pick another slot."

    old_when = meeting.scheduled_at
    meeting.scheduled_at = slot_utc
    try:
        from .coaching_intake import intake_for_meeting
        intake = intake_for_meeting(meeting.id)
        if intake is not None:
            intake.scheduled_at = slot_utc
            if intake.status in ("pending_payment", "paid"):
                intake.status = "scheduled"
    except Exception:
        log.exception("Failed syncing intake time for meeting %s", meeting.id)

    if meeting.zoom_meeting_id:
        try:
            updated = daily_svc.update_room(
                meeting.zoom_meeting_id,
                scheduled_at=slot_utc,
                duration_minutes=ONE_ON_ONE_DURATION_MINUTES,
                max_participants=meeting.capacity or ONE_ON_ONE_CAP,
                enable_cloud_recording=True,
            )
            if updated is not None and updated.room_url:
                meeting.zoom_url = updated.room_url
        except Exception:
            log.exception("Failed updating Daily room for rescheduled 1:1 %s",
                          meeting.id)
    db.session.commit()

    from .coaching_intake import coach_label
    coach_name = coach_label(coach)
    member_tz = normalize_timezone(getattr(user, "timezone", None)) or "UTC"
    new_label = format_local(slot_utc, "%a %b %d · %I:%M %p", tz_name=member_tz) or slot_utc.isoformat()
    old_label = (
        format_local(old_when, "%a %b %d · %I:%M %p", tz_name=member_tz)
        if old_when else "the previous time"
    )
    notify_owners(
        kind="support_group_alert",
        body=(f"{user.public_name()} requested a new time for their 1:1 with "
              f"{coach_name}: {old_label} → {new_label}.")[:300],
        url=url_for("admin.support_groups"),
        actor_id=user.id,
    )
    notify(
        user.id, kind="support_group",
        body=f"Your 1:1 with {coach_name} is now {new_label}.",
        url=url_for("main.manage_sessions"),
    )
    db.session.commit()
    try:
        send_styled_email(
            user.email,
            subject=f"Your 1:1 with {coach_name} was updated",
            preview=f"New time: {new_label}.",
            header="1:1 coaching",
            title=f"Your 1:1 with {coach_name} is moved",
            body=(f"You asked for a new time, and it's booked.\n\n"
                  f"Was: {old_label}\n"
                  f"Now: {new_label}\n\n"
                  "Join from Upcoming sessions in My space when it’s time."),
            button_text="Open your sessions",
            button_url=url_for("main.manage_sessions", _external=True),
        )
    except Exception:
        log.exception("1:1 reschedule email failed for user %s", user.id)
    return None


def _notify_joiner(meeting: SupportGroupMeeting, user: User) -> None:
    room = _meeting_room_url(meeting)
    group = _circle_name(meeting)
    when = _when_for(user, meeting.scheduled_at)
    note = f"You're in for {group} — {when}."
    notify(user.id, kind="support_group", body=note[:300],
           actor_id=meeting.scheduled_by_user_id, url=room)
    _send_booked_email(meeting, user)
    db.session.commit()


def schedule_meeting(meeting: SupportGroupMeeting, *, scheduled_at: datetime,
                     owner: User | None = None) -> str | None:
    if scheduled_at is None:
        return "Pick a date and time."
    if scheduled_at <= utcnow():
        return "Choose a time in the future."

    # Also block overlapping times when rescheduling an existing peer meeting —
    # for a member. Whoever is doing it here is the same person the caps were
    # checked against, and an owner is free to run two side by side.
    by_owner = bool(owner is not None and owner.is_owner_view())
    if not by_owner and (meeting.kind or "peer") == "peer" and meeting.circle_id:
        topic_key = (
            custom_topic_key(meeting.notes)
            if is_custom_circle(meeting.circle) else None
        )
        conflict = peer_session_time_conflict(
            meeting.circle_id,
            scheduled_at,
            topic_key=topic_key,
            exclude_meeting_id=meeting.id,
        )
        if conflict is not None:
            return (
                "There’s already a session for this topic at that time. "
                "Pick a different time."
            )

    title = meeting_display_title(meeting)
    topic = f"Bloom Anyway — {title}"
    duration = meeting_duration_minutes(meeting)
    max_part = meeting_max_participants(meeting)
    cloud_rec = (meeting.kind or "").strip().lower() == "one_on_one"
    try:
        if meeting.zoom_meeting_id:
            updated = daily_svc.update_room(
                meeting.zoom_meeting_id,
                scheduled_at=scheduled_at,
                duration_minutes=duration,
                max_participants=max_part,
                enable_cloud_recording=cloud_rec,
            )
            if updated is None:
                info = daily_svc.create_room(
                    topic=topic,
                    scheduled_at=scheduled_at,
                    duration_minutes=duration,
                    max_participants=max_part,
                    enable_cloud_recording=cloud_rec,
                )
                meeting.zoom_meeting_id = info.room_name
                meeting.zoom_url = info.room_url
            else:
                if updated.room_url:
                    meeting.zoom_url = updated.room_url
                meeting.zoom_meeting_id = updated.room_name or meeting.zoom_meeting_id
        else:
            info = daily_svc.create_room(
                topic=topic,
                scheduled_at=scheduled_at,
                duration_minutes=duration,
                max_participants=max_part,
                enable_cloud_recording=cloud_rec,
            )
            meeting.zoom_meeting_id = info.room_name
            meeting.zoom_url = info.room_url
    except daily_svc.DailyError as exc:
        return str(exc)

    if not (meeting.zoom_url or "").strip():
        return "Daily.co did not return a room URL."

    was_scheduled = meeting.status == "scheduled" and meeting.booked_notified_at
    meeting.scheduled_at = scheduled_at
    meeting.status = "scheduled"
    # Keep linked coaching intake slot in sync with the meeting.
    try:
        from .coaching_intake import intake_for_meeting
        intake = intake_for_meeting(meeting.id)
        if intake is not None:
            intake.scheduled_at = scheduled_at
            if intake.status in ("pending_payment", "paid"):
                intake.status = "scheduled"
    except Exception:
        log.exception("Failed syncing coaching intake for meeting %s", meeting.id)
    db.session.commit()
    if was_scheduled:
        _notify_seats(meeting, kind="updated", actor_id=getattr(owner, "id", None))
    else:
        _notify_seats(meeting, kind="booked", actor_id=getattr(owner, "id", None))
        meeting.booked_notified_at = utcnow()
        db.session.commit()
        if (meeting.kind or "peer") == "peer":
            notify_topic_watchers(meeting, actor_id=getattr(owner, "id", None))
    return None


def cancel_meeting(meeting: SupportGroupMeeting, *,
                   owner: User | None = None) -> None:
    seats = meeting_seats(meeting)
    was_live = meeting.status == "scheduled" and bool(meeting.booked_notified_at)
    room_name = (meeting.zoom_meeting_id or "").strip()
    meeting.status = "cancelled"
    for row in seats:
        row.status = "cancelled"
    try:
        from .coaching_intake import intake_for_meeting
        intake = intake_for_meeting(meeting.id)
        if intake is not None and intake.status in ("paid", "scheduled", "pending_payment"):
            intake.status = "cancelled"
    except Exception:
        log.exception("Failed syncing cancelled intake for meeting %s", meeting.id)
    db.session.commit()
    if room_name:
        try:
            daily_svc.delete_room(room_name)
        except Exception:
            log.exception("Failed to delete Daily room %s", room_name)
    if seats:
        _notify_seats(
            meeting,
            kind="cancelled" if was_live else "cancelled_draft",
            actor_id=getattr(owner, "id", None),
            seats=seats,
        )


def complete_meeting(meeting: SupportGroupMeeting) -> None:
    meeting.status = "completed"
    settle_seats(meeting)
    db.session.commit()


def notify_topic_watchers(meeting: SupportGroupMeeting,
                          *, actor_id: int | None = None) -> int:
    """Fan out to members who tapped Notify me on this topic."""
    if not meeting.circle_id:
        return 0
    seated_ids = {
        s.user_id for s in SupportGroupApplication.query
        .filter_by(meeting_id=meeting.id, status="selected").all()
    }
    alerts = (SupportGroupTopicAlert.query
              .options(joinedload(SupportGroupTopicAlert.author))
              .filter_by(circle_id=meeting.circle_id)
              .all())
    group = _circle_name(meeting)
    browse = _circle_browse_url(meeting.circle_id)
    sent = 0
    for alert in alerts:
        user = alert.author
        if not user or user.deleted_at:
            continue
        if user.id in seated_ids:
            continue
        when = _when_for(user, meeting.scheduled_at)
        note = f"New {group} session scheduled — {when}."
        notify(
            user.id,
            kind="support_group_alert",
            body=note[:300],
            actor_id=actor_id,
            url=browse,
        )
        sent += 1
    if sent:
        db.session.commit()
    return sent


def _when_for(user: User, dt: datetime | None) -> str:
    tz = normalize_timezone(getattr(user, "timezone", None)) or "UTC"
    stamp = format_local(dt, "%A, %b %d, %Y at %I:%M %p", tz_name=tz)
    return f"{stamp} ({tz})" if stamp else "the scheduled time"


def _reminder_day_and_timing(
    user: User, scheduled_at: datetime | None, *, now: datetime | None = None,
) -> tuple[str | None, str]:
    """Return (day_word, timing) for reminder copy in the member's timezone.

    day_word is ``today``, ``tomorrow``, or None. timing is a short phrase like
    ``starting soon`` / ``later today`` / ``in about 20 hours``.
    """
    tz = normalize_timezone(getattr(user, "timezone", None)) or "UTC"
    return _day_word_for_tz(tz, scheduled_at, now=now)


def _day_word_for_tz(
    tz: str, scheduled_at: datetime | None, *, now: datetime | None = None,
) -> tuple[str | None, str]:
    """The same, for a clock rather than an account."""
    now = now or utcnow()
    if scheduled_at is None:
        return None, "coming up"

    start = scheduled_at
    if start.tzinfo is not None:
        start = start.astimezone(timezone.utc).replace(tzinfo=None)
    ref = now
    if ref.tzinfo is not None:
        ref = ref.astimezone(timezone.utc).replace(tzinfo=None)

    local_now = to_local(ref, tz)
    local_start = to_local(start, tz)
    delta = start - ref
    secs = max(0, int(delta.total_seconds()))

    if local_now is not None and local_start is not None:
        day_delta = (local_start.date() - local_now.date()).days
        if day_delta <= 0:
            day_word = "today"
            timing = "starting soon" if secs <= 90 * 60 else "later today"
            return day_word, timing
        if day_delta == 1:
            hours = max(1, int(round(secs / 3600)))
            return "tomorrow", (
                f"in about {hours} hour{'s' if hours != 1 else ''}"
            )

    if secs <= 90 * 60:
        return None, "starting soon"
    hours = max(1, int(round(secs / 3600)))
    return None, f"in about {hours} hour{'s' if hours != 1 else ''}"


def host_display_name(meeting: SupportGroupMeeting) -> str:
    """Who is hosting, as the person who booked should see it.

    A 1:1 is sold as an hour with Saman or with Ayesha. The Studio account
    makes the room either way, so its own handle in that place tells a member
    they're meeting a stranger they never booked.
    """
    if (meeting.kind or "").strip().lower() == "one_on_one":
        coach = normalize_custom_topic(meeting.notes)
        if coach:
            return coach
    host = meeting.host
    if host is None and meeting.scheduled_by_user_id:
        host = db.session.get(User, meeting.scheduled_by_user_id)
    if not host or host.deleted_at:
        return "a member"
    return (host.public_name() or host.first_name() or "a member").strip() or "a member"


def seat_display_name(meeting: SupportGroupMeeting, user: User) -> str:
    """A name for the roster and for the video tile, the coach included."""
    if ((meeting.kind or "").strip().lower() == "one_on_one"
            and meeting.scheduled_by_user_id
            and user.id == meeting.scheduled_by_user_id):
        return host_display_name(meeting)
    return user.public_name() or "Member"


def _session_date_and_time(user: User, scheduled_at: datetime | None
                           ) -> tuple[str, str]:
    """Date + time strings in the member's timezone for booking emails."""
    tz = normalize_timezone(getattr(user, "timezone", None)) or "UTC"
    return _session_stamp(tz, scheduled_at)


def _session_stamp(tz: str, scheduled_at: datetime | None) -> tuple[str, str]:
    """The same date and time strings, for a clock rather than an account."""
    if scheduled_at is None:
        return "—", "—"
    day = format_local(scheduled_at, "%A, %b %d, %Y", tz_name=tz) or "—"
    time_s = format_local(scheduled_at, "%I:%M %p", tz_name=tz) or "—"
    if time_s and time_s != "—":
        time_s = f"{time_s} ({tz})"
    return day, time_s


def _circle_name(meeting: SupportGroupMeeting) -> str:
    return meeting_display_title(meeting)


def _facilitator_amount_for(email: str) -> str:
    """Best-effort paid amount from a recent facilitator Stripe order."""
    return _addon_amount_for(
        email,
        setting_key="facilitator_stripe_price_id",
        fallback="",
    )


def _one_on_one_amount_for(email: str, coach: str) -> str:
    """Paid amount from Ayesha/Saman Stripe orders when available."""
    coach_key = (coach or "").strip().casefold()
    if coach_key == "saman":
        setting = "saman_stripe_price_id"
    else:
        setting = "ayesha_stripe_price_id"
    return _addon_amount_for(email, setting_key=setting, fallback="")


def _addon_amount_for(email: str, *, setting_key: str, fallback: str) -> str:
    from ..models import Order
    from .settings import get_setting

    price_id = (get_setting(setting_key) or "").strip()
    email_norm = (email or "").strip().lower()
    if email_norm and price_id:
        order = (
            Order.query
            .filter(
                func.lower(Order.buyer_email) == email_norm,
                Order.status == "paid",
                Order.ls_variant_id == price_id,
            )
            .order_by(Order.created_at.desc())
            .first()
        )
        if order is not None:
            try:
                shown = order.total_display()
                if shown:
                    return shown
            except Exception:
                pass
    return fallback


def _send_booked_email(meeting: SupportGroupMeeting, user: User) -> None:
    kind = (meeting.kind or "peer").strip().lower()
    # Studio host is seated but didn't book as a member.
    if (kind in ("facilitator", "one_on_one")
            and meeting.scheduled_by_user_id
            and user.id == meeting.scheduled_by_user_id):
        return
    day, time_s = _session_date_and_time(user, meeting.scheduled_at)
    try:
        if kind == "facilitator":
            send_facilitator_booked(
                user.email,
                session_date=day,
                session_time=time_s,
                amount=_facilitator_amount_for(user.email),
            )
            return
        if kind == "one_on_one":
            coach = normalize_custom_topic(meeting.notes) or "a founder"
            send_one_on_one_booked(
                user.email,
                coach_name=coach,
                session_date=day,
                session_time=time_s,
                amount=_one_on_one_amount_for(user.email, coach),
                button_url=_meeting_room_url(meeting) or _circle_browse_url(meeting.circle_id),
            )
            return
        room = _meeting_room_url(meeting)
        send_support_group_booked(
            user.email,
            group_topic=_circle_name(meeting),
            host_name=host_display_name(meeting),
            session_date=day,
            session_time=time_s,
            button_url=room,
        )
    except Exception:
        log.exception("Support-group booking email failed for user %s", user.id)


COACH_EMAIL_SETTINGS = {
    "saman": "saman_coach_email",
    "ayesha": "ayesha_coach_email",
}


def coach_reminder_addresses(coach: str) -> tuple[list[str], bool]:
    """Where a founder's own 1:1 reminder goes, and whether the owners got it.

    Each founder's address is set in Studio. Without one the reminder goes to
    the owners rather than nowhere — an hour somebody paid for shouldn't go
    unattended because a field was left blank.
    """
    from .settings import get_setting

    key = (coach or "").strip().casefold()
    setting = COACH_EMAIL_SETTINGS.get(key)
    address = (get_setting(setting) or "").strip() if setting else ""
    if address:
        return [address], False
    return owner_emails(), True


def _account_for_email(address: str) -> User | None:
    email = (address or "").strip().lower()
    if not email:
        return None
    return (User.query
            .filter(func.lower(User.email) == email, User.deleted_at.is_(None))
            .first())


def booking_member(meeting: SupportGroupMeeting) -> User | None:
    """Whoever booked the hour — the seat that isn't the Studio host."""
    for row in meeting_seats(meeting):
        user = row.author
        if not user or user.deleted_at:
            continue
        if (meeting.scheduled_by_user_id
                and user.id == meeting.scheduled_by_user_id):
            continue
        return user
    return None


def remind_coach(meeting: SupportGroupMeeting) -> int:
    """Tell whoever is taking a 1:1 that it's coming, and who booked it.

    The member has had a reminder since the beginning; the founder on the
    other side of the call had nothing but the Studio page to remember to
    look at. Sent the day before with the rest of them, so it's one sweep and
    one claim rather than a second thing to keep in step.
    """
    if (meeting.kind or "").strip().lower() != "one_on_one":
        return 0
    coach = normalize_custom_topic(meeting.notes) or ""
    addresses, to_owners = coach_reminder_addresses(coach)
    if not addresses:
        return 0

    member = booking_member(meeting)
    who = member.public_name() if member is not None else "A member"
    label = coach or "a founder"
    room = _meeting_room_url(meeting)
    studio = absolute_url(_studio_sessions_url())
    sent = 0
    told: set[int] = set()
    for address in addresses:
        account = _account_for_email(address)
        tz = normalize_timezone(getattr(account, "timezone", None)) or "UTC"
        day, time_s = _session_stamp(tz, meeting.scheduled_at)
        # An hour booked this morning for this evening is not "tomorrow".
        day_word, _timing = _day_word_for_tz(tz, meeting.scheduled_at)
        soon = day_word or "coming up"
        heading = (f"{label}'s 1:1" if to_owners else "Your 1:1")
        body = (
            f"{who} booked an hour with {label}.\n\n"
            f"Date: {day}\n"
            f"Time: {time_s}\n\n"
            "What they wrote on the booking form is in Studio under Support "
            f"groups: {studio}"
        )
        try:
            send_styled_email(
                address,
                subject=f"{heading} is {soon} — {who}",
                preview=f"{who}, {day} at {time_s}.",
                header="Session reminder",
                title=f"{heading} with {who} is {soon}",
                body=body,
                button_text="Open the room",
                button_url=room,
            )
            sent += 1
        except Exception:
            log.exception("1:1 coach reminder email failed for %s", address)
        if account is not None and account.id not in told:
            told.add(account.id)
            notify(
                account.id,
                kind="support_group",
                body=(f"{heading} with {who} is {soon} — {day}, "
                      f"{time_s}.")[:300],
                url=room,
            )
    if told:
        db.session.commit()
    return sent


def _send_reminder_email(meeting: SupportGroupMeeting, user: User) -> None:
    room = _meeting_room_url(meeting)
    day, time_s = _session_date_and_time(user, meeting.scheduled_at)
    try:
        send_support_group_reminder(
            user.email,
            group_topic=_circle_name(meeting),
            host_name=host_display_name(meeting),
            session_date=day,
            session_time=time_s,
            button_url=room,
        )
    except Exception:
        log.exception("Support-group reminder email failed for user %s", user.id)


def _send_host_cancelled_email(meeting: SupportGroupMeeting, user: User) -> None:
    kind = (meeting.kind or "peer").strip().lower()
    if (kind in ("facilitator", "one_on_one")
            and meeting.scheduled_by_user_id
            and user.id == meeting.scheduled_by_user_id):
        return
    day, _time = _session_date_and_time(user, meeting.scheduled_at)
    try:
        if kind == "facilitator":
            send_facilitator_cancelled(
                user.email,
                session_date=day,
                amount=_facilitator_amount_for(user.email),
            )
            return
        if kind == "one_on_one":
            coach = normalize_custom_topic(meeting.notes) or "a founder"
            send_one_on_one_cancelled(
                user.email,
                coach_name=coach,
                session_date=day,
                amount=_one_on_one_amount_for(user.email, coach),
            )
            return
        send_support_group_host_cancelled(
            user.email,
            group_topic=_circle_name(meeting),
            session_date=day,
            button_url=_circle_browse_url(meeting.circle_id),
        )
    except Exception:
        log.exception(
            "Support-group host-cancel email failed for user %s", user.id,
        )


def _send_updated_email(meeting: SupportGroupMeeting, user: User) -> None:
    """Notify a member that their session time changed (general template #10 for 1:1)."""
    kind = (meeting.kind or "peer").strip().lower()
    if (kind in ("facilitator", "one_on_one")
            and meeting.scheduled_by_user_id
            and user.id == meeting.scheduled_by_user_id):
        return
    group = _circle_name(meeting)
    when = _when_for(user, meeting.scheduled_at)
    room = _meeting_room_url(meeting)
    browse = _circle_browse_url(meeting.circle_id)
    button_url = room if room else browse
    try:
        if kind == "one_on_one":
            send_styled_email(
                user.email,
                subject=f"{group} time updated",
                preview=f"Your session was moved to {when}.",
                header="Session update",
                title=f"Your {group} was rescheduled",
                body=(
                    f"Your private session time changed.\n\n"
                    f"New time: {when}\n\n"
                    "Join from Support Groups in Bloom Anyway when it’s time."
                ),
                button_text="Open Support Groups",
                button_url=button_url,
            )
            return
        seats = meeting_seats(meeting)
        others = max(0, len(seats) - 1)
        send_styled_email(
            user.email,
            subject=f"{group} time updated",
            preview=f"Your session was moved to {when}.",
            header="Session update",
            title=f"Your {group} was rescheduled",
            body=(
                f"Your session details changed.\n\n"
                f"New time: {when}\n"
                f"Others in the circle: {others}\n\n"
                "Join from Support Groups in Bloom Anyway when it’s time."
            ),
            button_text="Open session",
            button_url=button_url,
        )
    except Exception:
        log.exception("Support-group update email failed for user %s", user.id)


def _notify_seats(meeting: SupportGroupMeeting, *, kind: str,
                  actor_id: int | None = None,
                  seats: list[SupportGroupApplication] | None = None) -> None:
    seats = seats if seats is not None else meeting_seats(meeting)
    others = max(0, len(seats) - 1)
    room = _meeting_room_url(meeting)
    group = _circle_name(meeting)
    browse = _circle_browse_url(meeting.circle_id)
    meeting_kind = (meeting.kind or "peer").strip().lower()

    for row in seats:
        user = row.author
        if not user or user.deleted_at:
            continue
        # Studio host is seated on facilitator/1:1 but isn't the booking member.
        if (meeting_kind in ("facilitator", "one_on_one")
                and meeting.scheduled_by_user_id
                and user.id == meeting.scheduled_by_user_id):
            continue
        when = _when_for(user, meeting.scheduled_at)
        join_url = room if kind in ("booked", "updated", "reminder") else browse
        if kind == "booked":
            if meeting_kind == "one_on_one":
                note = f"Your {group} is booked — {when}."
            elif meeting_kind == "facilitator":
                note = f"You're booked for {group} — {when}."
            else:
                note = (
                    f"You're booked for {group} with "
                    f"{others} other{'s' if others != 1 else ''} — {when}."
                )
        elif kind == "updated":
            note = f"Your {group} was rescheduled — {when}."
        elif kind == "cancelled":
            note = f"Your {group} meeting was cancelled."
        elif kind == "cancelled_draft":
            note = f"The {group} session you were seated for was cancelled."
        elif kind == "reminder":
            day_word, _timing = _reminder_day_and_timing(
                user, meeting.scheduled_at)
            if day_word:
                note = f"Reminder: {group} {day_word} — {when}."
            else:
                note = f"Reminder: {group} — {when}."
        else:
            continue

        notify(user.id, kind="support_group", body=note[:300],
               actor_id=actor_id, url=join_url)
        if kind == "booked":
            _send_booked_email(meeting, user)
            continue
        if kind == "updated":
            _send_updated_email(meeting, user)
            continue
        if kind == "reminder":
            _send_reminder_email(meeting, user)
            continue
        if kind in ("cancelled", "cancelled_draft"):
            _send_host_cancelled_email(meeting, user)
            continue
    db.session.commit()


def notify_paid_one_on_one_pending(
    meeting: SupportGroupMeeting, *, member: User | None = None,
) -> None:
    """Confirm payment when Daily room creation failed (Studio finishes later)."""
    if meeting is None:
        return
    seats = meeting_seats(meeting)
    users: list[User] = []
    if member is not None and not getattr(member, "deleted_at", None):
        users.append(member)
    for row in seats:
        user = row.author
        if not user or user.deleted_at:
            continue
        if meeting.scheduled_by_user_id and user.id == meeting.scheduled_by_user_id:
            continue
        if any(u.id == user.id for u in users):
            continue
        users.append(user)
    group = _circle_name(meeting)
    browse = _circle_browse_url(meeting.circle_id)
    for user in users:
        when = _when_for(user, meeting.scheduled_at)
        note = (
            f"Payment received for {group} — {when}. "
            "We’ll confirm your room in Studio shortly."
        )
        notify(
            user.id,
            kind="support_group",
            body=note[:300],
            actor_id=meeting.scheduled_by_user_id,
            url=browse,
        )
        _send_booked_email(meeting, user)
    # Somebody has paid for an hour that doesn't exist yet, and only Studio can
    # finish it. Waiting for the owner to happen to look is how that hour
    # arrives with nothing behind it.
    try:
        from .social_graph import notify_owners

        who = users[0].public_name() if users else "A member"
        notify_owners(
            kind="support_group_alert",
            body=(f"{who} paid for {group}, but the room isn't made yet — "
                  "confirm it from Support groups.")[:300],
            url=_studio_sessions_url(),
        )
    except Exception:
        log.exception("Could not alert owners about unmade 1:1 %s", meeting.id)
    if users:
        db.session.commit()


def due_reminders(now: datetime | None = None):
    now = now or utcnow()
    window_end = now + timedelta(hours=24)
    return (SupportGroupMeeting.query
            .filter(
                SupportGroupMeeting.status == "scheduled",
                SupportGroupMeeting.reminded_at.is_(None),
                SupportGroupMeeting.scheduled_at.isnot(None),
                SupportGroupMeeting.scheduled_at > now,
                SupportGroupMeeting.scheduled_at <= window_end,
            )
            .all())


def _claim_reminder(meeting: SupportGroupMeeting) -> bool:
    """Take this session's reminder. True only for whoever gets there first.

    Both workers run this sweep, and both of them read the same unreminded
    session before either has stamped it — which is a second email and a
    second bell for every seat. Stamping first, in one statement that only
    matches while the stamp is still empty, is what keeps it to one. A send
    that then fails loses the reminder rather than repeating it for the
    people who already had theirs.
    """
    table = SupportGroupMeeting.__table__
    done = db.session.execute(
        table.update()
        .where(table.c.id == meeting.id, table.c.reminded_at.is_(None))
        .values(reminded_at=utcnow())
    )
    db.session.commit()
    return bool(done.rowcount)


def dispatch_due_reminders(now: datetime | None = None) -> int:
    meetings = due_reminders(now=now)
    sent = 0
    for meeting in meetings:
        if not _claim_reminder(meeting):
            continue
        _notify_seats(meeting, kind="reminder")
        remind_coach(meeting)
        db.session.commit()
        sent += 1
    return sent


def _sweep_reminders() -> int:
    try:
        expire_past_meetings()
        return dispatch_due_reminders()
    except Exception:
        log.exception("Support-group reminder sweep failed")
        try:
            db.session.rollback()
        except Exception:
            pass
        return 0


def maybe_sweep_reminders(force: bool = False) -> int:
    """Throttled reminder sweep, run behind the response.

    Each due session emails every seated member in turn, so doing this inline
    made one unlucky page load wait on the whole batch.
    """
    global _last_sweep_mono
    now_mono = time.monotonic()
    if not force and (now_mono - _last_sweep_mono) < _SWEEP_GAP_SEC:
        return 0
    _last_sweep_mono = now_mono
    from .background import run_in_background
    run_in_background("support-group-reminders", _sweep_reminders)
    return 0
