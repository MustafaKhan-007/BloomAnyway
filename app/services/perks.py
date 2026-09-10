"""Free membership time that comes with buying certain products.

The grant is derived from the buyer's purchases rather than stored on the
account, so it ends on its own when the months run out and disappears with a
refund — no expiry job, and nothing to clean up.
"""
from __future__ import annotations

import logging
from calendar import monthrange
from datetime import datetime

from ..models import Product, ShopPurchase, higher_membership, utcnow

log = logging.getLogger(__name__)


def add_months(start: datetime, months: int) -> datetime:
    """``start`` plus whole calendar months, clamped to the month's length."""
    months = max(0, int(months or 0))
    total = start.month - 1 + months
    year = start.year + total // 12
    month = total % 12 + 1
    day = min(start.day, monthrange(year, month)[1])
    return start.replace(year=year, month=month, day=day)


def perk_products() -> list[Product]:
    """Catalogue products that hand out free membership months.

    Cached for the request: reconciling a page full of members would otherwise
    re-read the same short list once per person.
    """
    try:
        from flask import g, has_app_context
        cached = getattr(g, "_perk_products", None) if has_app_context() else None
    except Exception:
        cached = None
    if cached is not None:
        return cached
    from ..extensions import db

    rows = (Product.query
            .filter(Product.perk_membership_tier.isnot(None),
                    db.or_(Product.perk_membership_months > 0,
                           Product.perk_ends_at.isnot(None)))
            .all())
    out = [p for p in rows if p.has_perk()]
    try:
        from flask import g, has_app_context
        if has_app_context():
            g._perk_products = out
    except Exception:
        pass
    return out


def purchase_has_perk(purchase: ShopPurchase) -> bool:
    products = perk_products()
    return bool(products) and _match(purchase, products) is not None


def _match(purchase: ShopPurchase, products: list[Product]) -> Product | None:
    """Same purchase → product rules the library uses, without the queries."""
    for raw in (purchase.variant_id, purchase.product_id):
        key = (raw or "").strip()
        if not key:
            continue
        for product in products:
            if key in ((product.stripe_price_id or "").strip(),
                       (product.ls_variant_id or "").strip()):
                return product
    name = (purchase.product_name or "").strip().lower()
    if name:
        for product in products:
            if (product.title or "").strip().lower() == name:
                return product
    return None


def perk_state(user) -> dict:
    """The membership perk this buyer holds right now.

    ``{"tier": "creator" | "", "until": datetime | None, "expired": bool,
    "starts": datetime | None}``. ``expired`` marks someone whose perk has run
    out and needs dropping back to whatever they actually pay for; ``starts``
    is a perk they've bought that hasn't opened yet, which is waiting rather
    than either of those.
    """
    out = {"tier": "", "until": None, "expired": False, "starts": None}
    if user is None or not getattr(user, "id", None):
        return out

    # Reconcile runs on ordinary page loads, so look up the handful of products
    # that carry a perk once and match purchases against them in memory.
    products = perk_products()
    if not products:
        return out

    now = utcnow()
    purchases = (ShopPurchase.query
                 .filter(ShopPurchase.user_id == user.id,
                         ShopPurchase.status.in_(("linked", "removed")))
                 .all())
    best = "none"
    for purchase in purchases:
        product = _match(purchase, products)
        if product is None:
            continue
        starts, until = product.perk_window(purchase.purchased_at or now)
        if until <= now:
            out["expired"] = True
            continue
        if starts > now:
            # Bought, paid for, and not open yet. Nothing to grant today and
            # nothing to take away either — it comes on by itself on the day.
            if out["starts"] is None or starts < out["starts"]:
                out["starts"] = starts
            continue
        best = higher_membership(best, product.perk_tier())
        if out["until"] is None or until > out["until"]:
            out["until"] = until

    if best != "none":
        out["tier"] = best
        out["expired"] = False
    else:
        out["until"] = None
    return out


def months_bought_since(user, when: datetime | None) -> bool:
    """Did this buyer pay for membership months after ``when``?

    A tier an owner sets by hand in Studio outranks billing, and it outranked
    perk months too — including months bought long afterwards, which left the
    buyer paying for a membership that never arrived. Money that lands after
    the decision answers it; money that came before does not. ``when`` of
    ``None`` (a choice made before we recorded the date) counts as before.
    """
    if user is None or not getattr(user, "id", None):
        return False
    products = perk_products()
    if not products:
        return False
    now = utcnow()
    purchases = (ShopPurchase.query
                 .filter(ShopPurchase.user_id == user.id,
                         ShopPurchase.status.in_(("linked", "removed")))
                 .all())
    for purchase in purchases:
        product = _match(purchase, products)
        if product is None:
            continue
        bought = purchase.purchased_at or now
        if when is not None and bought <= when:
            continue
        # Months that have already run out are not worth reopening.
        if product.perk_window(bought)[1] <= now:
            continue
        return True
    return False


def perk_summary_for(purchase) -> str:
    """"3 months of Creator membership" for what this purchase carried, or ""."""
    product = _match(purchase, perk_products()) if purchase is not None else None
    if product is None or not product.has_perk():
        return ""
    return product.perk_offer()


def announce(user, purchase, *, held_before: str | None = None) -> bool:
    """Tell a buyer their purchase carried free membership months. Once.

    Nothing said so at the time: the tier simply went up, and the only place
    it was written down was the membership card on their account, which
    somebody who has just bought a guide has no reason to open.

    Said after the tier has been worked out, and it reports what actually
    happened: months that are on the account now, or a perk with a date still
    to come. ``held_before`` is what they held before that ran, since by now
    the column already says otherwise. A perk that was bought and then not
    granted says nothing at all — a buyer told they have something they
    haven't got is worse off than one who was told nothing.

    Skipped for a buyer already on that tier or better, where the months
    change nothing they can see today.
    """
    from ..models import Notification
    from .social_graph import notify

    if user is None or purchase is None or not getattr(user, "id", None):
        return False
    product = _match(purchase, perk_products())
    if product is None or not product.has_perk():
        return False
    tier = product.perk_tier()
    held = (held_before if held_before is not None
            else getattr(user, "membership", None)) or "none"
    if higher_membership(held, tier) == held:
        return False
    now = utcnow()
    starts, until = product.perk_window(purchase.purchased_at or now)
    if until <= now:
        return False

    holds_now = getattr(user, "membership", None) or "none"
    if starts > now:
        when = f"it starts on {starts.strftime('%b %d, %Y')} and runs until"
    elif higher_membership(holds_now, tier) == holds_now:
        when = "it is on your account now, until"
    else:
        log.warning(
            "perk: user %s bought %s for %s months of %s and is still on %s "
            "— saying nothing rather than promising it",
            user.id, product.id, product.perk_months(), tier, holds_now,
        )
        return False
    body = (f"“{product.title}” came with {perk_summary_for(purchase)} — "
            f"{when} {until.strftime('%b %d, %Y')}.")[:300]
    # Linking a purchase happens more than once — at checkout, at signup, on a
    # webhook retry — and each one runs through here.
    already = (Notification.query
               .filter_by(user_id=user.id, kind="membership", body=body)
               .first())
    if already is not None:
        return False
    notify(user.id, kind="membership", body=body, url="/account")
    return True


def perk_display(user) -> dict:
    """What to tell a member about their perk: when it ends, or when it opens.

    ``{"until": "Dec 31, 2026", "from": ""}`` — one or the other, since a perk
    they are holding has nothing to wait for and one they are waiting on isn't
    running yet.
    """
    from .timefmt import format_local

    out = {"until": "", "from": ""}
    state = perk_state(user)
    try:
        if state["tier"] and state["until"] is not None:
            out["until"] = format_local(state["until"], "%b %d, %Y")
        elif state["starts"] is not None:
            out["from"] = format_local(state["starts"], "%b %d, %Y")
    except Exception:
        pass
    return out


def perk_end_display(user) -> str:
    """Human end date for an active perk, or empty."""
    return perk_display(user)["until"]
