"""Saman (and future Ayesha) 1:1 intake: questionnaire, availability, fulfillment."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func
from sqlalchemy.orm import joinedload

from ..extensions import db
from ..models import (
    CoachAvailability,
    CoachingIntake,
    SupportGroupApplication,
    SupportGroupMeeting,
    User,
    utcnow,
)
from . import support_groups as sg_svc
from .timefmt import format_local, normalize_timezone, to_local

log = logging.getLogger(__name__)

COACH_LABELS = {"saman": "Saman", "ayesha": "Ayesha"}
WEEKDAY_LABELS = (
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
)
SLOT_HORIZON_DAYS = 28
SLOT_STEP_MINUTES = 60

# All questionnaire fields — every answer is required before checkout.
SAMAN_QUESTIONS: tuple[dict, ...] = (
    {
        "key": "instagram_handle",
        "section": "About you",
        "label": "What's your Instagram handle (or main platform)?",
        "input": "text",
        "placeholder": "@you or TikTok / YouTube handle",
    },
    {
        "key": "posting_duration",
        "section": "About you",
        "label": "How long have you been posting content?",
        "input": "text",
        "placeholder": "e.g. 3 months, 2 years",
    },
    {
        "key": "follower_count",
        "section": "About you",
        "label": "What's your current follower count?",
        "input": "text",
        "placeholder": "e.g. 1.2k",
    },
    {
        "key": "posting_focus",
        "section": "About you",
        "label": (
            "Do you post part-time around a job/kids, or is this your main focus?"
        ),
        "input": "textarea",
        "placeholder": "A short sentence is perfect",
    },
    {
        "key": "niche",
        "section": "Niche & goals",
        "label": (
            "What's your niche, or what are you hoping to talk about? "
            "(If you're not sure yet, say so — that's okay!)"
        ),
        "input": "textarea",
    },
    {
        "key": "goal_3_months",
        "section": "Niche & goals",
        "label": (
            "What's your #1 goal for the next 3 months? "
            "(e.g. grow followers, get consistent, start monetizing, land brand deals)"
        ),
        "input": "textarea",
    },
    {
        "key": "why_build",
        "section": "Niche & goals",
        "label": "Why do you want to build this? (personal story, income goal, etc.)",
        "input": "textarea",
    },
    {
        "key": "days_per_week",
        "section": "Current content habits",
        "label": "How many days a week do you currently post?",
        "input": "text",
        "placeholder": "e.g. 3–4",
    },
    {
        "key": "content_type",
        "section": "Current content habits",
        "label": (
            "What type of content do you usually make? "
            "(talking head, storytelling, faceless, educational, etc.)"
        ),
        "input": "textarea",
    },
    {
        "key": "best_post",
        "section": "Current content habits",
        "label": (
            "What's your best-performing post or reel so far, if any? "
            "(link if you have one)"
        ),
        "input": "textarea",
    },
    {
        "key": "hardest_part",
        "section": "Current content habits",
        "label": "What's felt hardest or most confusing so far?",
        "input": "textarea",
    },
    {
        "key": "making_money",
        "section": "Monetization",
        "label": (
            "Are you currently making any money from content? "
            "If yes, how (affiliate, UGC, products, brand deals)?"
        ),
        "input": "textarea",
    },
    {
        "key": "product_to_sell",
        "section": "Monetization",
        "label": (
            "Do you have a product, service, or skill you'd eventually want to sell?"
        ),
        "input": "textarea",
    },
    {
        "key": "biggest_question",
        "section": "The call itself",
        "label": "What's the single biggest question you want answered on our call?",
        "input": "textarea",
    },
    {
        "key": "already_tried",
        "section": "The call itself",
        "label": "Is there anything you've already tried that didn't work?",
        "input": "textarea",
    },
)

#: Ayesha's is the healing side of the same booking: shorter, and leading with
#: tick-boxes because naming what you're in the middle of is hard to type cold.
AYESHA_QUESTIONS: tuple[dict, ...] = (
    {
        "key": "going_through",
        "section": "Where you're at",
        "label": "What are you currently going through?",
        "input": "checkboxes",
        "options": ("Divorce", "Custody / co-parenting challenges",
                    "Separation", "Anxiety"),
        "other": True,
    },
    {
        "key": "hoping_for",
        "section": "Where you're at",
        "label": "What's the main thing you're hoping to get from this call?",
        "input": "checkboxes",
        "options": ("Guidance / advice", "Someone to listen",
                    "Practical next steps"),
        "other": True,
    },
    {
        "key": "discuss",
        "section": "The call itself",
        "label": "Briefly explain what you would like to discuss in this call.",
        "input": "textarea",
    },
    {
        "key": "before_we_talk",
        "section": "The call itself",
        "label": (
            "Is there anything specific you'd like me to know before we talk?"
        ),
        "input": "textarea",
        # Asked, not demanded — "anything specific" invites "no" as an answer,
        # and making someone type that to reach checkout is a toll.
        "optional": True,
    },
)

DISCLAIMER_KEYS = (
    "disclaimer_identity",
    "disclaimer_conduct",
    "disclaimer_recording",
)


def coach_label(coach: str) -> str:
    key = (coach or "").strip().casefold()
    return COACH_LABELS.get(key, (coach or "").strip().title() or "Coach")


def normalize_coach(coach: str) -> str | None:
    key = (coach or "").strip().casefold()
    return key if key in COACH_LABELS else None


QUESTIONS_BY_COACH: dict[str, tuple[dict, ...]] = {
    "saman": SAMAN_QUESTIONS,
    "ayesha": AYESHA_QUESTIONS,
}


def questions_for(coach: str) -> tuple[dict, ...]:
    return QUESTIONS_BY_COACH.get(normalize_coach(coach) or "", ())


def _answer_from_form(form, q: dict) -> str:
    """One answer, whatever shape the question is asked in.

    Tick-boxes come back as a list plus a free-text "Other"; both collapse to
    one line so an answer is a string wherever it is stored or shown.
    """
    if q.get("input") != "checkboxes":
        return (form.get(q["key"]) or "").strip()
    getlist = getattr(form, "getlist", None)
    picked = [str(v).strip() for v in (getlist(q["key"]) if getlist else [])]
    picked = [v for v in picked if v in q.get("options", ())]
    if q.get("other"):
        other = (form.get(f"{q['key']}_other") or "").strip()
        if other:
            picked.append(other[:200])
    return ", ".join(picked)


def parse_answers(form, coach: str) -> tuple[dict | None, str | None]:
    """Validate required questionnaire + disclaimers from a form mapping."""
    qs = questions_for(coach)
    if not qs:
        return None, "That coaching intake isn’t available yet."
    answers: dict[str, str] = {}
    for q in qs:
        raw = _answer_from_form(form, q)
        if not raw:
            if q.get("optional"):
                continue
            return None, f"Please answer: {q['label']}"
        if len(raw) > 2000:
            return None, "One of your answers is a bit long — keep each under 2000 characters."
        answers[q["key"]] = raw
    for key in DISCLAIMER_KEYS:
        if not form.get(key):
            return None, "Please confirm all the session guidelines before continuing."
        answers[key] = "yes"
    return answers, None


def list_availability(coach: str) -> list[CoachAvailability]:
    key = normalize_coach(coach)
    if not key:
        return []
    return (
        CoachAvailability.query
        .filter_by(coach=key, active=True)
        .order_by(CoachAvailability.weekday, CoachAvailability.start_minute)
        .all()
    )


def add_availability(
    coach: str,
    *,
    weekday: int,
    start_minute: int,
    end_minute: int,
    tz_name: str | None,
) -> tuple[CoachAvailability | None, str | None]:
    key = normalize_coach(coach)
    if not key:
        return None, "Unknown coach."
    try:
        weekday = int(weekday)
        start_minute = int(start_minute)
        end_minute = int(end_minute)
    except (TypeError, ValueError):
        return None, "Pick a valid day and time range."
    if weekday < 0 or weekday > 6:
        return None, "Pick a weekday."
    if not (0 <= start_minute < 24 * 60 and 0 < end_minute <= 24 * 60):
        return None, "Pick a valid time range."
    if end_minute - start_minute < SLOT_STEP_MINUTES:
        return None, f"Windows need to be at least {SLOT_STEP_MINUTES} minutes."
    tz = normalize_timezone(tz_name)
    if not tz:
        return None, "Pick a valid timezone from the list."
    row = CoachAvailability(
        coach=key,
        weekday=weekday,
        start_minute=start_minute,
        end_minute=end_minute,
        timezone=tz,
        active=True,
        created_at=utcnow(),
    )
    db.session.add(row)
    db.session.commit()
    return row, None


#: The hours a coach can be marked free. Slots are hourly, so an hour is the
#: smallest thing worth ticking.
DAY_HOURS = tuple(range(24))


def week_grid(coach: str) -> dict[int, set[int]]:
    """Which hours are already marked free, weekday → set of start hours.

    Turns saved windows back into the ticks that made them, so the editor opens
    on what is there rather than on nothing.
    """
    grid: dict[int, set[int]] = {day: set() for day in range(7)}
    for win in list_availability(coach):
        start = max(0, win.start_minute // 60)
        end = min(24, -(-win.end_minute // 60))  # round up to the hour
        for hour in range(start, end):
            grid[win.weekday].add(hour)
    return grid


def week_timezone(coach: str, fallback: str | None = None) -> str:
    """The timezone the saved week is written in, for the editor to open on."""
    rows = list_availability(coach)
    if rows:
        return rows[0].timezone or normalize_timezone(fallback) or "UTC"
    return normalize_timezone(fallback) or "UTC"


def _merge_hours(hours) -> list[tuple[int, int]]:
    """Ticked hours → the fewest contiguous ranges that cover them."""
    out: list[tuple[int, int]] = []
    for hour in sorted(set(hours)):
        if out and out[-1][1] == hour * 60:
            out[-1] = (out[-1][0], (hour + 1) * 60)
        else:
            out.append((hour * 60, (hour + 1) * 60))
    return out


def set_week_availability(
    coach: str,
    picks: dict[int, list[int]] | dict[int, set[int]],
    *,
    tz_name: str | None,
) -> tuple[int, str | None]:
    """Replace a coach's whole week in one go. Returns (windows saved, error).

    Saving the week as a whole is what lets the editor be a week: there is no
    add-one-then-add-another, and unticking is how you remove. Contiguous hours
    are merged so a morning is one window rather than four.
    """
    key = normalize_coach(coach)
    if not key:
        return 0, "Unknown coach."
    tz = normalize_timezone(tz_name)
    if not tz:
        return 0, "Pick a valid timezone from the list."

    ranges: list[tuple[int, int, int]] = []
    for day, hours in (picks or {}).items():
        try:
            day = int(day)
        except (TypeError, ValueError):
            continue
        if not 0 <= day <= 6:
            continue
        clean = {int(h) for h in hours if str(h).strip().isdigit()}
        clean = {h for h in clean if 0 <= h <= 23}
        for start, end in _merge_hours(clean):
            ranges.append((day, start, end))

    (CoachAvailability.query
     .filter_by(coach=key)
     .delete(synchronize_session=False))
    for day, start, end in ranges:
        db.session.add(CoachAvailability(
            coach=key, weekday=day, start_minute=start, end_minute=end,
            timezone=tz, active=True, created_at=utcnow(),
        ))
    db.session.commit()
    return len(ranges), None


def remove_availability(row_id: int, coach: str | None = None) -> str | None:
    row = db.session.get(CoachAvailability, row_id)
    if row is None:
        return "That window isn’t there anymore."
    if coach and row.coach != normalize_coach(coach):
        return "That window isn’t there anymore."
    db.session.delete(row)
    db.session.commit()
    return None


def _booked_starts(coach: str) -> set[datetime]:
    """UTC starts already taken by open/paid intakes or scheduled 1:1s for this coach."""
    key = normalize_coach(coach)
    label = coach_label(key or "")
    taken: set[datetime] = set()
    if not key:
        return taken

    for when, in (
        db.session.query(CoachingIntake.scheduled_at)
        .filter(
            CoachingIntake.coach == key,
            CoachingIntake.status.in_(("pending_payment", "paid", "scheduled")),
            CoachingIntake.scheduled_at >= utcnow(),
        )
        .all()
    ):
        if when:
            taken.add(when.replace(microsecond=0))

    meetings = (
        SupportGroupMeeting.query
        .filter(
            SupportGroupMeeting.kind == "one_on_one",
            SupportGroupMeeting.status.in_(("draft", "scheduled")),
            SupportGroupMeeting.scheduled_at.isnot(None),
            SupportGroupMeeting.scheduled_at >= utcnow(),
        )
        .all()
    )
    for m in meetings:
        notes = (m.notes or "").strip().casefold()
        if notes == label.casefold() or notes == key:
            taken.add(m.scheduled_at.replace(microsecond=0))
    return taken


def _expire_stale_pending(coach: str | None = None) -> None:
    """Release slots held by abandoned checkouts (pending > 2 hours)."""
    cutoff = utcnow() - timedelta(hours=2)
    try:
        q = CoachingIntake.query.filter(
            CoachingIntake.status == "pending_payment",
            CoachingIntake.created_at < cutoff,
        )
        if coach:
            key = normalize_coach(coach)
            if key:
                q = q.filter_by(coach=key)
        rows = q.all()
    except Exception:
        db.session.rollback()
        return
    if not rows:
        return
    for row in rows:
        row.status = "expired"
    db.session.commit()


def open_slots(
    coach: str,
    *,
    horizon_days: int = SLOT_HORIZON_DAYS,
    viewer_tz: str | None = None,
) -> list[dict]:
    """Generate bookable 60-minute slots from weekly availability."""
    key = normalize_coach(coach)
    _expire_stale_pending(key)
    windows = list_availability(key or "")
    if not windows:
        return []

    taken = _booked_starts(key or "")
    duration = sg_svc.ONE_ON_ONE_DURATION_MINUTES
    now = utcnow().replace(microsecond=0)
    view_tz = normalize_timezone(viewer_tz) or "UTC"
    out: list[dict] = []
    seen: set[datetime] = set()

    today = date.today()
    for offset in range(horizon_days + 1):
        day = today + timedelta(days=offset)
        py_weekday = day.weekday()  # Mon=0
        for win in windows:
            if win.weekday != py_weekday:
                continue
            try:
                coach_tz = ZoneInfo(win.timezone or "UTC")
            except Exception:
                coach_tz = ZoneInfo("UTC")
            cursor = win.start_minute
            while cursor + duration <= win.end_minute:
                local_dt = datetime(
                    day.year, day.month, day.day,
                    cursor // 60, cursor % 60,
                    tzinfo=coach_tz,
                )
                utc_dt = local_dt.astimezone(timezone.utc).replace(tzinfo=None)
                utc_dt = utc_dt.replace(microsecond=0)
                cursor += SLOT_STEP_MINUTES
                if utc_dt <= now + timedelta(hours=2):
                    continue
                if utc_dt in taken or utc_dt in seen:
                    continue
                seen.add(utc_dt)
                local_view = to_local(utc_dt, view_tz) or utc_dt
                out.append({
                    "utc": utc_dt.isoformat(timespec="seconds"),
                    "label": format_local(
                        utc_dt, "%a %b %d · %I:%M %p", tz_name=view_tz,
                    ) or utc_dt.isoformat(),
                    "date_key": local_view.strftime("%Y-%m-%d"),
                    "time_label": local_view.strftime("%I:%M %p").lstrip("0"),
                    "day_label": local_view.strftime("%a %b %d"),
                    "tz": view_tz,
                })

    out.sort(key=lambda s: s["utc"])
    return out


def parse_slot_utc(raw: str) -> datetime | None:
    s = (raw or "").strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1]
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.replace(microsecond=0)


def slot_still_open(coach: str, when: datetime) -> bool:
    when = when.replace(microsecond=0)
    for slot in open_slots(coach, horizon_days=SLOT_HORIZON_DAYS):
        if slot["utc"] == when.isoformat(timespec="seconds"):
            return True
    return False


def create_pending_intake(
    user: User,
    *,
    coach: str,
    answers: dict,
    slot_utc: datetime,
) -> tuple[CoachingIntake | None, str | None]:
    key = normalize_coach(coach)
    if not key:
        return None, "Unknown coach."
    if not questions_for(key):
        return None, "That coaching intake isn’t available yet."
    if slot_utc is None or not slot_still_open(key, slot_utc):
        return None, "That time isn’t available — pick another slot."

    # Drop stale pending intakes for this user/coach so they don't block slots.
    stale = (
        CoachingIntake.query
        .filter_by(user_id=user.id, coach=key, status="pending_payment")
        .all()
    )
    for row in stale:
        row.status = "cancelled"

    intake = CoachingIntake(
        user_id=user.id,
        coach=key,
        scheduled_at=slot_utc.replace(microsecond=0),
        status="pending_payment",
        created_at=utcnow(),
    )
    intake.set_answers(answers)
    db.session.add(intake)
    db.session.commit()
    return intake, None


def intake_for_meeting(meeting_id: int) -> CoachingIntake | None:
    return (
        CoachingIntake.query
        .filter_by(meeting_id=meeting_id)
        .order_by(CoachingIntake.id.desc())
        .first()
    )


def studio_intakes(coach: str | None = None, limit: int = 40) -> list[CoachingIntake]:
    q = (
        CoachingIntake.query
        .options(
            joinedload(CoachingIntake.member),
            joinedload(CoachingIntake.meeting),
        )
        .filter(CoachingIntake.status.in_(("paid", "scheduled", "pending_payment")))
        .order_by(CoachingIntake.created_at.desc())
    )
    if coach:
        key = normalize_coach(coach)
        if key:
            q = q.filter_by(coach=key)
    return q.limit(limit).all()


def answer_rows(intake: CoachingIntake) -> list[dict]:
    """Ordered label/value pairs for Studio expandable display."""
    data = intake.answers()
    rows = []
    for q in questions_for(intake.coach):
        val = (data.get(q["key"]) or "").strip()
        if not val:
            continue
        rows.append({
            "section": q["section"],
            "label": q["label"],
            "value": val,
        })
    return rows


def _studio_host() -> User | None:
    return (
        User.query
        .filter_by(is_admin=True, deleted_at=None)
        .order_by(User.id.asc())
        .first()
    )


def fulfill_intake(intake_id: int, *, buyer_email: str | None = None) -> str | None:
    """After Stripe pays: create Daily 1:1, seat member, mark intake scheduled."""
    intake = db.session.get(CoachingIntake, int(intake_id))
    if intake is None:
        return "Intake not found."
    if intake.status == "scheduled" and intake.meeting_id:
        return None
    if intake.status not in ("pending_payment", "paid"):
        return f"Intake is {intake.status}."

    member = db.session.get(User, intake.user_id)
    if member is None or member.deleted_at is not None:
        # Fall back to buyer email if the account was recreated.
        email = (buyer_email or "").strip().lower()
        if email:
            member = (
                User.query
                .filter(func.lower(User.email) == email, User.deleted_at.is_(None))
                .first()
            )
    if member is None:
        return "Member account not found for this booking."

    host = _studio_host()
    label = coach_label(intake.coach)
    when = intake.scheduled_at
    if when is None or when <= utcnow():
        intake.status = "paid"
        db.session.commit()
        return "Chosen slot is no longer in the future — mark paid for Studio to reschedule."

    # A second run picks up the session the first one started rather than
    # opening another. Retrying is normal — the room Daily wouldn't make, the
    # webhook that came twice — and a member should never end up with two.
    meeting = (db.session.get(SupportGroupMeeting, intake.meeting_id)
               if intake.meeting_id else None)
    if meeting is not None and (meeting.status or "") == "cancelled":
        meeting = None
    if meeting is None:
        meeting = SupportGroupMeeting(
            circle_id=None,
            capacity=sg_svc.ONE_ON_ONE_CAP,
            kind="one_on_one",
            scheduled_by_user_id=host.id if host else None,
            status="draft",
            notes=label,
            created_at=utcnow(),
        )
        db.session.add(meeting)
        db.session.flush()

        if host is not None:
            db.session.add(SupportGroupApplication(
                user_id=host.id,
                circle_id=None,
                meeting_id=meeting.id,
                message="",
                status="selected",
                created_at=utcnow(),
            ))
        if host is None or member.id != host.id:
            db.session.add(SupportGroupApplication(
                user_id=member.id,
                circle_id=None,
                meeting_id=meeting.id,
                message="",
                status="selected",
                created_at=utcnow(),
            ))
    intake.status = "paid"
    intake.meeting_id = meeting.id
    db.session.commit()

    err = sg_svc.schedule_meeting(meeting, scheduled_at=when, owner=host)
    if err:
        log.error("coaching intake %s schedule failed: %s", intake.id, err)
        intake.status = "paid"
        db.session.commit()
        # Payment succeeded — still confirm to the member even if Daily room
        # creation lagged (Studio can finish the room from the paid intake).
        try:
            sg_svc.notify_paid_one_on_one_pending(meeting, member=member)
        except Exception:
            log.exception(
                "coaching intake %s pending notify failed", intake.id,
            )
        return err

    intake.status = "scheduled"
    db.session.commit()
    return None


def unfinished_for_user(user, limit: int = 5) -> list[CoachingIntake]:
    """Sessions this member paid for that never got as far as a room."""
    if user is None or not getattr(user, "id", None):
        return []
    return (CoachingIntake.query
            .filter(CoachingIntake.user_id == user.id,
                    CoachingIntake.status == "paid")
            .order_by(CoachingIntake.created_at.desc())
            .limit(max(1, int(limit)))
            .all())


def finish_unscheduled(user, limit: int = 5) -> int:
    """Finish anything already paid for, and say how many that came to.

    Money can land without the booking ever finishing — a webhook that never
    arrived, a room Daily wouldn't make — and the member is left holding a
    charge and silence. Rather than wait for someone to notice, the next time
    they come back the session is made and the confirmation goes out.
    """
    done = 0
    for intake in unfinished_for_user(user, limit=limit):
        try:
            if fulfill_intake(intake.id) is None:
                done += 1
        except Exception:
            log.exception("coaching: could not finish intake %s", intake.id)
            db.session.rollback()
    return done


def fulfill_from_payment_metadata(meta: dict, *, buyer_email: str | None = None) -> None:
    raw = (meta or {}).get("coaching_intake_id") or (meta or {}).get("intake_id")
    if not raw:
        return
    try:
        intake_id = int(raw)
    except (TypeError, ValueError):
        return
    try:
        err = fulfill_intake(intake_id, buyer_email=buyer_email)
        if err:
            log.warning("coaching fulfill intake %s: %s", intake_id, err)
    except Exception:
        log.exception("coaching fulfill failed for intake %s", intake_id)


def minutes_to_hhmm(minute: int) -> str:
    h, m = divmod(int(minute), 60)
    return f"{h:02d}:{m:02d}"


def hhmm_to_minutes(raw: str) -> int | None:
    s = (raw or "").strip()
    if not s or ":" not in s:
        return None
    try:
        h_s, m_s = s.split(":", 1)
        h, m = int(h_s), int(m_s[:2])
    except (TypeError, ValueError):
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return h * 60 + m
