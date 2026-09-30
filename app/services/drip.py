"""Drip-fed course modules: which ones a buyer can open, and when.

The schedule runs from each buyer's own purchase date, so a module the owner
adds months after launch still reaches everyone who bought earlier — already
unlocked for them if their own schedule has passed it.

Which calendar a buyer is on is a separate question from which modules exist.
A product sold in rounds has one set of modules and a set of dates per round,
and everything here takes both: ``product`` for what the modules are, and a
``schedule`` for when they open. Leave the schedule out and it is the
product's own dates, which is every product not run in rounds.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from ..models import utcnow


def unlock_at(started_at: datetime, number: int, interval_days: int) -> datetime:
    """When module ``number`` (1-based) opens for a buyer who started then."""
    steps = max(0, int(number or 1) - 1)
    return started_at + timedelta(days=steps * max(1, int(interval_days or 1)))


def dates_of(product, schedule=None):
    """Whose calendar to read: the one handed in, or the product's own."""
    if schedule is not None:
        return schedule
    if product is None:
        return None
    try:
        return product.schedule()
    except AttributeError:
        return product


def schedule_start(schedule, started_at: datetime | None) -> datetime | None:
    """Where this schedule counts from for one buyer.

    Normally each buyer's own purchase, so a course bought today starts today.
    A schedule with a release date on it runs off the calendar instead: module
    one opens that day for everybody, and the rest follow from there, which is
    what a launch announced for a date needs. Buying afterwards then opens
    whatever has already been released rather than starting the wait again.
    """
    fixed = getattr(schedule, "drip_starts_at", None) if schedule is not None else None
    return fixed or started_at


def _parse_release(text) -> datetime | None:
    try:
        return datetime.fromisoformat(str(text)) if text else None
    except ValueError:
        return None


def unlock_times(schedule, anchor: datetime | None) -> list[datetime | None]:
    """When each module opens, in order, for a buyer anchored at ``anchor``.

    Three ways to space them, chosen per product:

    * ``interval`` — the same gap between every one.
    * ``dates`` — each module has its own day on the calendar, the same for
      everybody. A module left without one comes out with the module before
      it, which is how "these two land together" is written.
    * ``gaps`` — each module waits its own number of days after the one
      before, counted from the buyer's own start.

    A later module never opens before an earlier one, whatever is typed in.
    """
    rows = schedule.module_timing() if schedule is not None else []
    if not rows:
        return []
    mode = schedule.drip_mode_key()

    raw: list[datetime | None] = []
    if mode == "dates":
        carried = None
        for row in rows:
            carried = _parse_release(row.get("release_at")) or carried
            raw.append(carried)
    elif anchor is None:
        raw = [None] * len(rows)
    elif mode == "gaps":
        when = anchor
        for i, row in enumerate(rows):
            if i:
                when = when + timedelta(days=int(row.get("gap_days") or 0))
            raw.append(when)
    else:
        days = schedule.drip_days()
        raw = [anchor + timedelta(days=i * days) for i in range(len(rows))]

    out: list[datetime | None] = []
    latest: datetime | None = None
    for opens in raw:
        if opens is not None:
            latest = opens if latest is None else max(latest, opens)
        out.append(latest)
    return out


def public_steps(product, now=None, schedule=None) -> list[dict]:
    """What each module's opening is, told to somebody who hasn't bought yet.

    There is no purchase to count from, so a schedule pinned to the calendar
    comes back as dates and one that runs from the purchase comes back as the
    day it lands on — day 1 being the moment they buy. A date already gone by
    is day 1 as well: it opens as soon as they buy, which is the truth for
    anybody reading the page now.
    """
    dates = dates_of(product, schedule)
    rows = dates.module_timing() if dates is not None else []
    if not rows:
        return []
    now = now or utcnow()
    times = unlock_times(dates, getattr(dates, "drip_starts_at", None))
    mode = dates.drip_mode_key()

    day = 1
    steps: list[dict] = []
    for i, row in enumerate(rows):
        if mode == "gaps" and i:
            day += max(0, int(row.get("gap_days") or 0))
        elif mode == "interval" and i:
            day = i * dates.drip_days() + 1
        opens = times[i] if i < len(times) else None
        if opens is not None and opens > now:
            steps.append({"when": opens, "day": None})
        else:
            steps.append({"when": None, "day": 1 if opens is not None else day})
    return steps


def reads_it_whole(viewer) -> bool:
    """True for an owner reading their own shelf.

    A schedule is for buyers. An owner is the person who has to check the
    course works, and waiting a fortnight to see whether module three opens is
    not checking anything. Asking to see the site as a member puts the wait
    back, since that is what that switch is for.
    """
    if viewer is None or not getattr(viewer, "is_admin", False):
        return False
    try:
        return not viewer.preview_tier()
    except Exception:
        return True


def module_rows(product, started_at, now=None, viewer=None,
                schedule=None) -> list[dict]:
    """This product's modules with their file and lock state for one buyer.

    ``started_at`` is the buyer's purchase time; ``None`` (unknown, e.g. an old
    import) unlocks everything rather than taking content away. ``schedule``
    is the round they bought into, when the product is sold in rounds.
    """
    rows = product.modules() if product is not None else []
    if not rows:
        return []
    dates = dates_of(product, schedule)
    anchor = schedule_start(dates, started_at)
    dripped = product.is_dripped() and not reads_it_whole(viewer) and (
        anchor is not None or dates.drip_mode_key() == "dates")
    now = now or utcnow()
    times = unlock_times(dates, anchor) if dripped else []
    for row in rows:
        i = row["number"] - 1
        opens = times[i] if 0 <= i < len(times) else None
        unlocked = opens is None or opens <= now
        row["unlocked"] = unlocked
        row["unlock_at"] = None if unlocked else opens
        row["unlock_display"] = "" if unlocked else opens.strftime("%b %d, %Y")
        row["days_away"] = 0 if unlocked else max(1, (opens - now).days + 1)
    return rows


def asset_unlocked(product, asset, started_at, now=None, viewer=None,
                   schedule=None) -> bool:
    """False only for a file pinned to a module this buyer hasn't reached yet."""
    number = getattr(asset, "module_index", None)
    if not number or product is None or not product.is_dripped():
        return True
    if reads_it_whole(viewer):
        return True
    dates = dates_of(product, schedule)
    anchor = schedule_start(dates, started_at)
    if anchor is None and dates.drip_mode_key() != "dates":
        return True
    times = unlock_times(dates, anchor)
    if number > len(times):
        return True
    opens = times[number - 1]
    return opens is None or opens <= (now or utcnow())


def next_locked(rows: list[dict]) -> dict | None:
    """The next module still to open, or None when the buyer has them all."""
    for row in rows:
        if not row.get("unlocked"):
            return row
    return None
