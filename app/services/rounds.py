"""Running the same product more than once.

A round is a set of dates and nothing else — see :class:`ProductRound`. This
is the handful of things Studio does to them: start another one, write the
dates on it, count who is on it, and take an empty one away again.

The first round of a product is copied from the product's own dates, so
turning a product into one that runs in rounds changes nothing for anybody:
the calendar the round is on is the calendar the product was already on.
"""
from __future__ import annotations

from sqlalchemy import func, or_

from ..extensions import db
from ..models import DRIP_MODES, Product, ProductRound, ShopPurchase


class RoundError(ValueError):
    """Something Studio should say out loud rather than do quietly."""


def add(product: Product) -> ProductRound:
    """Start the next run of this product, on a copy of the current dates.

    Copied rather than empty because a second run of something is almost
    always the first one moved along a few weeks — and an empty round would
    silently mean "no schedule, no perk, never comes off sale", which is
    never what starting one is for.

    It arrives in draft. Nothing about the product page changes until it is
    made live, so the dates can be worked out at leisure.
    """
    source = product.rounds[-1] if product.rounds else product
    row = ProductRound(
        product=product,
        number=product.next_round_number(),
        status="draft",
        drip_mode=source.drip_mode_key(),
        drip_interval_days=source.drip_days(),
        drip_starts_at=source.drip_starts_at,
        off_shelf_at=source.off_shelf_at,
        perk_membership_months=source.perk_months(),
        perk_starts_at=source.perk_starts_at,
        perk_ends_at=source.perk_ends_at,
    )
    row.set_module_timing(source.module_timing())
    db.session.add(row)
    db.session.flush()
    return row


def buyers(row: ProductRound | None) -> int:
    """How many people are on this round's calendar."""
    if row is None or not row.id:
        return 0
    return (ShopPurchase.query
            .filter(ShopPurchase.round_id == row.id,
                    ShopPurchase.status != "refunded")
            .count())


def unstamped(product: Product) -> int:
    """How many buyers are on the product's own dates rather than a round's.

    Everybody who bought before the product was ever run in rounds. They are
    left where they are on purpose: the dates they bought against are the
    product's, and moving them onto a round they never saw would change when
    their modules open. Worth a line in Studio so it is not a surprise that
    changing the product's own dates still moves somebody.
    """
    keys = product.price_keys()
    title = (product.title or "").strip()
    matches = []
    if keys:
        matches.append(ShopPurchase.product_id.in_(keys))
        matches.append(ShopPurchase.variant_id.in_(keys))
    if title:
        matches.append(func.lower(ShopPurchase.product_name) == title.lower())
    if not matches:
        return 0
    return (ShopPurchase.query
            .filter(ShopPurchase.round_id.is_(None),
                    ShopPurchase.status != "refunded",
                    or_(*matches))
            .count())


def remove(row: ProductRound) -> None:
    """Take a round away, as long as nobody is on it.

    Somebody who bought into a round has their dates on it. Deleting it
    would drop them back onto the product's own, which is a schedule they
    never agreed to and, for a perk, months they may not have had. Closing
    a round is what ``off_shelf_at`` is for; this is only for one created
    by mistake.
    """
    held = buyers(row)
    if held:
        raise RoundError(
            f"{held} {'person has' if held == 1 else 'people have'} bought into "
            f"{row.label()}, and their dates are on it. Set a date for it to "
            f"come off the shelves instead — they keep what they paid for.")
    db.session.delete(row)


def set_live(row: ProductRound, live: bool) -> None:
    """Offer this round to new buyers, or stop offering it."""
    row.status = "live" if live else "draft"


def apply_fields(row: ProductRound, form, owner_tz, notes: list[str]) -> None:
    """Write a round's dates from the Studio form.

    Reads the same field names the product form uses for the same dates, so
    there is one way to type a date into Studio rather than two. Anything
    that doesn't parse is left off and said out loud in ``notes`` — dates
    are the whole of what a round is, and one quietly dropped is a module
    that never opens.
    """
    from .timefmt import parse_owner_parts

    row.title = (form.get("title") or "").strip()[:120] or None

    def moment(field: str, fallback: str, whats_lost: str):
        day = (form.get(f"{field}_date") or "").strip()
        if not day:
            return None
        when = parse_owner_parts(
            day, (form.get(f"{field}_time") or "").strip() or fallback, owner_tz)
        if when is None:
            notes.append(whats_lost)
        return when

    row.off_shelf_at = moment(
        "off_shelf", "23:59",
        "That last-day-on-sale date didn't look right, so this round has no "
        "end on it.")

    # The perk and module blocks are only on the page when the product has
    # something for them to say — a tier to give away, modules to space out.
    # A form that didn't carry them isn't an owner clearing them, so what is
    # already written down is left alone.
    if row.product.perk_tier():
        row.perk_starts_at = (None if form.get("perk_start_on_buy") else moment(
            "perk_starts", "09:00",
            "That membership start date didn't look right, so it starts when "
            "they buy."))
        row.perk_ends_at = moment(
            "perk_ends", "23:59",
            "That membership end date didn't look right, so the months are "
            "what buyers get.")
        if (row.perk_ends_at is not None and row.perk_starts_at is not None
                and row.perk_ends_at <= row.perk_starts_at):
            row.perk_ends_at = None
            notes.append("The membership can't end before it starts, so that "
                         "end date was left off.")
        try:
            months = max(0, min(60, int(
                (form.get("perk_months") or "0").strip() or 0)))
        except ValueError:
            months = 0
        row.perk_membership_months = (
            months if months >= 1 or row.perk_ends_at is not None else 1)
    else:
        row.perk_membership_months = 0

    if "drip_mode" not in form:
        return
    mode = (form.get("drip_mode") or "").strip().lower()
    row.drip_mode = mode if mode in DRIP_MODES else "interval"
    try:
        row.drip_interval_days = max(1, min(365, int(
            (form.get("drip_interval_days") or "7").strip() or 7)))
    except ValueError:
        row.drip_interval_days = 7
    row.drip_starts_at = moment(
        "drip_starts", "09:00",
        "That date for module 1 didn't look right, so the modules will open "
        "from each buyer's own start instead.")

    timing = []
    for i in range(1, len(row.product.curriculum()) + 1):
        day = (form.get(f"mod{i}_release_date") or "").strip()
        when = None
        if day:
            when = parse_owner_parts(
                day, (form.get(f"mod{i}_release_time") or "").strip() or "09:00",
                owner_tz)
            if when is None:
                notes.append(f"Module {i}'s date didn't look right, so it comes "
                             f"out with the module above it.")
        timing.append({
            "release_at": when.isoformat() if when is not None else "",
            "gap_days": (form.get(f"mod{i}_gap_days") or "0").strip(),
        })
    row.set_module_timing(timing)
