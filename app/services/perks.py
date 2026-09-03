"""Free membership time that comes with buying certain products.

The grant is derived from the buyer's purchases rather than stored on the
account, so it ends on its own when the months run out and disappears with a
refund — no expiry job, and nothing to clean up.
"""
from __future__ import annotations

from calendar import monthrange
from datetime import datetime

from ..models import (MEMBERSHIP_LABELS, Product, ShopPurchase,
                      higher_membership, utcnow)


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
    rows = (Product.query
            .filter(Product.perk_membership_tier.isnot(None),
                    Product.perk_membership_months > 0)
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

    ``{"tier": "creator" | "", "until": datetime | None, "expired": bool}``.
    ``expired`` marks someone whose perk has run out and needs dropping back
    to whatever they actually pay for.
    """
    out = {"tier": "", "until": None, "expired": False}
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
        until = add_months(purchase.purchased_at or now, product.perk_months())
        if until <= now:
            out["expired"] = True
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


def perk_summary_for(purchase) -> str:
    """"3 months of Creator membership" for what this purchase carried, or ""."""
    product = _match(purchase, perk_products()) if purchase is not None else None
    if product is None or not product.has_perk():
        return ""
    months = product.perk_months()
    label = MEMBERSHIP_LABELS.get(product.perk_tier(), product.perk_tier())
    return f"{months} month{'' if months == 1 else 's'} of {label} membership"


def announce(user, purchase) -> bool:
    """Tell a buyer their purchase carried free membership months. Once.

    Nothing said so at the time: the tier simply went up, and the only place
    it was written down was the membership card on their account, which
    somebody who has just bought a guide has no reason to open.

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
    held = getattr(user, "membership", None) or "none"
    if higher_membership(held, product.perk_tier()) == held:
        return False
    until = add_months(purchase.purchased_at or utcnow(), product.perk_months())
    if until <= utcnow():
        return False

    body = (f"“{product.title}” came with {perk_summary_for(purchase)} — "
            f"it is on your account now, until "
            f"{until.strftime('%b %d, %Y')}.")[:300]
    # Linking a purchase happens more than once — at checkout, at signup, on a
    # webhook retry — and each one runs through here.
    already = (Notification.query
               .filter_by(user_id=user.id, kind="membership", body=body)
               .first())
    if already is not None:
        return False
    notify(user.id, kind="membership", body=body, url="/account")
    return True


def perk_end_display(user) -> str:
    """Human end date for an active perk, or empty."""
    state = perk_state(user)
    if not state["tier"] or state["until"] is None:
        return ""
    try:
        return state["until"].strftime("%b %d, %Y")
    except Exception:
        return ""
