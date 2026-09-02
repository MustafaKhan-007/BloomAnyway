"""Drip-fed course modules: which ones a buyer can open, and when.

The schedule runs from each buyer's own purchase date, so a module the owner
adds months after launch still reaches everyone who bought earlier — already
unlocked for them if their own schedule has passed it.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from ..models import utcnow


def unlock_at(started_at: datetime, number: int, interval_days: int) -> datetime:
    """When module ``number`` (1-based) opens for a buyer who started then."""
    steps = max(0, int(number or 1) - 1)
    return started_at + timedelta(days=steps * max(1, int(interval_days or 1)))


def schedule_start(product, started_at: datetime | None) -> datetime | None:
    """Where this product's schedule counts from for one buyer.

    Normally each buyer's own purchase, so a course bought today starts today.
    A product with a release date on it runs off the calendar instead: module
    one opens that day for everybody, and the rest follow from there, which is
    what a launch announced for a date needs. Buying afterwards then opens
    whatever has already been released rather than starting the wait again.
    """
    fixed = getattr(product, "drip_starts_at", None) if product is not None else None
    return fixed or started_at


def module_rows(product, started_at, now=None) -> list[dict]:
    """This product's modules with their file and lock state for one buyer.

    ``started_at`` is the buyer's purchase time; ``None`` (unknown, e.g. an old
    import) unlocks everything rather than taking content away.
    """
    rows = product.modules() if product is not None else []
    if not rows:
        return []
    anchor = schedule_start(product, started_at)
    dripped = product.is_dripped() and anchor is not None
    now = now or utcnow()
    days = product.drip_days()
    for row in rows:
        opens = unlock_at(anchor, row["number"], days) if dripped else None
        unlocked = opens is None or opens <= now
        row["unlocked"] = unlocked
        row["unlock_at"] = None if unlocked else opens
        row["unlock_display"] = "" if unlocked else opens.strftime("%b %d, %Y")
        row["days_away"] = 0 if unlocked else max(1, (opens - now).days + 1)
    return rows


def asset_unlocked(product, asset, started_at, now=None) -> bool:
    """False only for a file pinned to a module this buyer hasn't reached yet."""
    number = getattr(asset, "module_index", None)
    anchor = schedule_start(product, started_at)
    if not number or product is None or not product.is_dripped() or anchor is None:
        return True
    return unlock_at(anchor, number, product.drip_days()) <= (now or utcnow())


def next_locked(rows: list[dict]) -> dict | None:
    """The next module still to open, or None when the buyer has them all."""
    for row in rows:
        if not row.get("unlocked"):
            return row
    return None
