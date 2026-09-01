"""Grant / revoke membership tiers from purchases.

Source of truth (in order):
1. ``orders.membership_tier`` on paid membership orders (set at checkout)
2. Stripe price id → ``MembershipPlan``
3. Stripe product id → ``MembershipPlan``
4. Known Stripe product names (Healing/Creator/Full Bloom Membership Monthly|Annual)
5. Checkout / subscription metadata ``tier``
"""
import logging
import re

from sqlalchemy import func, or_

from ..models import (MEMBERSHIP_RANK, MEMBERSHIPS, MembershipPlan, Order,
                      User, higher_membership, utcnow)

log = logging.getLogger(__name__)

_PAID_TIERS = ("healing", "creator", "full_bloom")


# --- "memberships under maintenance" allowlist --------------------------------
# Memberships are disabled for everyone except admins and the accounts an owner
# lists in Studio. Everyone else (including signed-out visitors) sees a simple
# "under maintenance" page and can't view, buy, or manage a membership.

def membership_access_emails() -> set[str]:
    """Lowercased emails allowed to use memberships while under maintenance."""
    from .settings import get_setting
    raw = get_setting("membership_access_emails") or ""
    out: set[str] = set()
    for line in raw.replace(",", "\n").splitlines():
        e = line.strip().lower()
        if e:
            out.add(e)
    return out


def can_use_memberships(user) -> bool:
    """True for admins and allowlisted accounts; False for everyone else."""
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_admin", False):
        return True
    email = (getattr(user, "email", "") or "").strip().lower()
    return bool(email) and email in membership_access_emails()

# Exact Stripe product names used in the dashboard (normalized lowercase).
_STRIPE_PRODUCT_NAME_TIERS = {
    "full bloom membership (annual)": "full_bloom",
    "full bloom membership (monthly)": "full_bloom",
    "creator membership (annual)": "creator",
    "creator membership (monthly)": "creator",
    "healing membership (annual)": "healing",
    "healing membership (monthly)": "healing",
}


def _norm_name(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def tier_from_stripe_product_name(name: str | None) -> str | None:
    """Map a Stripe product name to a tier when it matches the known catalog."""
    key = _norm_name(name)
    if not key:
        return None
    if key in _STRIPE_PRODUCT_NAME_TIERS:
        return _STRIPE_PRODUCT_NAME_TIERS[key]
    # Tolerate missing "Membership" or swapped punctuation.
    compact = key.replace("—", "-").replace("–", "-")
    for label, tier in _STRIPE_PRODUCT_NAME_TIERS.items():
        if compact == label:
            return tier
    # Last resort: clear "[Tier] Membership" without billing suffix.
    for needle, tier in (
        ("full bloom membership", "full_bloom"),
        ("creator membership", "creator"),
        ("healing membership", "healing"),
    ):
        if compact.startswith(needle):
            return tier
    return None


def _plan_for_product_id(product_id):
    """Match a Stripe *price* id (or legacy variant) to a MembershipPlan."""
    if not product_id:
        return None
    key = str(product_id).strip()
    if not key:
        return None
    matches = (MembershipPlan.query
               .filter(or_(MembershipPlan.stripe_price_id == key,
                           MembershipPlan.stripe_price_id_annual == key,
                           MembershipPlan.ls_variant_id == key))
               .all())
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    by_tier = {p.tier: p for p in matches}
    log.error(
        "membership: price %s matches multiple plans %s — fix Studio price ids",
        key, [p.tier for p in matches],
    )
    halves = [p for p in matches if p.tier in ("healing", "creator")]
    if len(halves) == 1:
        return halves[0]
    return by_tier.get("full_bloom") or matches[0]


def _plan_for_stripe_product_id(stripe_product_id):
    """Match a Stripe *product* id (prod_…) to a MembershipPlan."""
    if not stripe_product_id:
        return None
    key = str(stripe_product_id).strip()
    if not key:
        return None
    matches = (MembershipPlan.query
               .filter(or_(MembershipPlan.stripe_product_id == key,
                           MembershipPlan.stripe_product_id_annual == key))
               .all())
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    log.error(
        "membership: Stripe product %s matches multiple plans %s",
        key, [p.tier for p in matches],
    )
    return matches[0]


def tier_for_price_id(price_id: str | None) -> str | None:
    """Map a Stripe price id to a membership tier via MembershipPlan."""
    plan = _plan_for_product_id(price_id)
    if plan and plan.tier in _PAID_TIERS:
        return plan.tier
    return None


def tier_for_stripe_product(
    product_id: str | None = None,
    product_name: str | None = None,
) -> str | None:
    """Resolve tier from Studio product id and/or known Stripe product name."""
    plan = _plan_for_stripe_product_id(product_id)
    if plan and plan.tier in _PAID_TIERS:
        return plan.tier
    return tier_from_stripe_product_name(product_name)


def purchased_tier(email: str) -> str:
    """Highest membership tier this email owns via paid membership orders."""
    if not email:
        return "none"
    orders = (Order.query
              .filter(Order.status == "paid",
                      func.lower(Order.buyer_email) == email.strip().lower())
              .all())
    best = "none"
    for order in orders:
        tier = (order.membership_tier or "").strip().lower()
        if tier not in _PAID_TIERS:
            tier = tier_for_price_id(order.ls_variant_id) or ""
            if tier in _PAID_TIERS and not order.membership_tier:
                order.membership_tier = tier
        if tier in _PAID_TIERS:
            best = higher_membership(best, tier)
    return best


def manual_tier(user: User) -> str:
    """The tier an owner set by hand in Studio, or "" when following billing."""
    tier = (getattr(user, "membership_manual", None) or "").strip().lower()
    return tier if tier in MEMBERSHIPS else ""


def billing_tier(email: str) -> str:
    """Tier that Stripe / paid orders would grant this email right now."""
    live = None
    try:
        from .stripe_pay import active_membership_tier_from_stripe
        live = active_membership_tier_from_stripe(email)
    except Exception:
        log.exception("membership: stripe live lookup failed for %s", email)
    if live is not None:
        return live
    return purchased_tier(email)


def end_local_membership_orders(email: str) -> int:
    """Mark this email's paid membership Orders ended. Caller commits."""
    email_norm = (email or "").strip().lower()
    if not email_norm:
        return 0
    ended = 0
    orders = (Order.query
              .filter(Order.status == "paid",
                      func.lower(Order.buyer_email) == email_norm)
              .all())
    for order in orders:
        tier = (order.membership_tier or "").strip().lower()
        if tier not in _PAID_TIERS:
            tier = tier_for_price_id(order.ls_variant_id) or ""
        if tier in _PAID_TIERS:
            order.status = "ended"
            ended += 1
    return ended


def revoke_paid_membership(email: str) -> dict:
    """Cancel Stripe membership billing and end paid membership orders.

    Without this a Studio downgrade is undone by the next reconcile, because
    the live subscription (or paid order) still grants the old tier.
    """
    out = {"cancelled": 0, "orders_ended": 0, "errors": []}
    email_norm = (email or "").strip().lower()
    if not email_norm or "@" not in email_norm or email_norm.endswith("@invalid.local"):
        out["errors"].append("no_billable_email")
        return out
    try:
        from . import stripe_pay as pay
        if pay.configured():
            result = pay.cancel_membership_subscriptions(
                email_norm, at_period_end=False,
            )
            out["cancelled"] = len(result.get("cancelled") or [])
            out["orders_ended"] = int(result.get("orders_ended") or 0)
            # Report every problem Stripe mentioned, not only the ones that
            # flipped ``ok`` — e.g. "no_membership_prices" leaves ok True while
            # cancelling nothing at all.
            out["errors"].extend(result.get("errors") or [])
            if not result.get("ok") and not out["errors"]:
                out["errors"].append("stripe_cancel_incomplete")
    except Exception:
        out["errors"].append("stripe_cancel_exception")
        log.exception("membership: Stripe cancel failed for %s", email_norm)
    # Local fallback: covers plans with no price id configured in Studio.
    out["orders_ended"] += end_local_membership_orders(email_norm)
    if not out["cancelled"] and not out["orders_ended"] and not out["errors"]:
        # Billing says this member is paying, yet there was nothing to stop:
        # the subscription lives under another customer/email in Stripe.
        out["errors"].append("billing_not_found")
    return out


def set_manual_tier(user: User, tier: str) -> dict:
    """Apply an owner-chosen tier from Studio → Members. Caller commits.

    Downgrades also stop the billing behind the old tier, and the choice is
    remembered so Stripe sync can't put the member back where they were.
    """
    out = {"ok": False, "revoked": False, "cancelled": 0,
           "orders_ended": 0, "errors": []}
    if user is None or tier not in MEMBERSHIPS or user.is_admin:
        return out

    billing = billing_tier(user.email)
    if tier != billing and MEMBERSHIP_RANK.get(tier, 0) <= MEMBERSHIP_RANK.get(billing, 0):
        info = revoke_paid_membership(user.email)
        out["revoked"] = bool(info["cancelled"] or info["orders_ended"])
        out["cancelled"] = info["cancelled"]
        out["orders_ended"] = info["orders_ended"]
        out["errors"] = info["errors"]

    # Record the override against everything that would otherwise grant a tier
    # — billing *before* the cancel, plus any free membership perk from a
    # product they bought. Assuming the cancel worked and dropping the override
    # here is what let a lingering Stripe subscription (or a replayed webhook)
    # hand the old tier straight back on the next sync. Only a tier that
    # already matches what they'd get anyway means "follow billing".
    from .perks import perk_state
    granted = higher_membership(billing, perk_state(user)["tier"] or "none")
    user.membership_manual = None if tier == granted else tier
    user.membership_manual_at = None if user.membership_manual is None else utcnow()
    user.membership = tier
    user.membership_cancel_at = None
    from .listings import enforce_listing_limits
    enforce_listing_limits(user)
    out["ok"] = True
    log.info("membership: studio set user %s -> %s (granted=%s, manual=%s)",
             user.id, tier, granted, user.membership_manual or "-")
    return out


#: distinguishes "caller already asked Stripe" from "Stripe said nothing"
_ASK_STRIPE = object()


def reconcile_user(user: User, downgrade: bool = False,
                   live_tier=_ASK_STRIPE) -> bool:
    """Sync a user's membership column from Stripe / paid orders.

    A tier set by hand in Studio wins until the member pays again. Otherwise
    prefer live Stripe (price/product → plan), else paid local orders, and
    keep whichever is better than a free membership perk from a product they
    bought. Never touches the owner. The caller commits.
    """
    if user is None:
        return False
    if user.is_admin:
        # Owners already rank as Full Bloom through effective_membership(), so
        # stamping the column adds nothing — and it used to leave a removed
        # co-owner sitting on a paid tier they never bought.
        return False
    if user.is_demo:
        # Stand-in accounts have no address Stripe could ever know, so asking
        # would only ever demote the tier the owner picked for them.
        return False

    manual = manual_tier(user)
    if manual:
        if (user.membership or "none") == manual:
            return False
        user.membership = manual
        log.info("membership: user %s kept on studio tier %s", user.id, manual)
        from .listings import enforce_listing_limits
        enforce_listing_limits(user)
        return True

    if live_tier is _ASK_STRIPE:
        live = None
        try:
            from .stripe_pay import active_membership_tier_from_stripe
            live = active_membership_tier_from_stripe(user.email)
        except Exception:
            log.exception("membership: stripe live sync failed for user %s", user.id)
            live = None
    else:
        live = live_tier

    purchased = purchased_tier(user.email)
    current = user.membership or "none"

    from .perks import perk_state
    perk = perk_state(user)

    if live is not None:
        base = live
    elif purchased != "none":
        base = purchased
    elif downgrade or perk["expired"]:
        # Nothing is paying for this tier any more, and a perk that has run
        # out must not leave them parked on it.
        base = "none"
    else:
        base = current
    new = higher_membership(base, perk["tier"] or "none")

    if new != current:
        user.membership = new
        log.info("membership: user %s %s -> %s (live=%s purchased=%s perk=%s)",
                 user.id, current, new, live, purchased, perk["tier"] or "-")
        from .listings import enforce_listing_limits
        enforce_listing_limits(user)
        return True
    return False


def reconcile_email(email: str, downgrade: bool = False) -> bool:
    """Reconcile the account matching an email (if one exists). Caller commits."""
    if not email:
        return False
    user = (User.query
            .filter(func.lower(User.email) == email.strip().lower(),
                    User.deleted_at.is_(None))
            .first())
    return reconcile_user(user, downgrade=downgrade)


def apply_from_order(order: Order) -> None:
    """After an order changes, grant/revoke membership if it is a membership order."""
    if not order:
        return
    tier = (order.membership_tier or "").strip().lower()
    if tier not in _PAID_TIERS:
        tier = tier_for_price_id(order.ls_variant_id) or ""
        if tier in _PAID_TIERS:
            order.membership_tier = tier
    if tier not in _PAID_TIERS:
        return
    if order.status == "paid":
        # A fresh payment replaces whatever an owner set by hand in Studio —
        # but a webhook replayed for an older payment must not.
        clear_manual_tier(order.buyer_email,
                          paid_at=getattr(order, "created_at", None))
    reconcile_email(order.buyer_email, downgrade=(order.status == "refunded"))


def clear_manual_tier(email: str, paid_at=None) -> bool:
    """Drop the Studio override for this email so billing decides again.

    ``paid_at`` is when the payment behind the change was recorded. A payment
    older than the Studio choice (a retried/late webhook for the membership the
    owner just revoked) leaves the override in place.
    """
    if not email:
        return False
    user = (User.query
            .filter(func.lower(User.email) == email.strip().lower(),
                    User.deleted_at.is_(None))
            .first())
    if user is None or not manual_tier(user):
        return False
    set_at = getattr(user, "membership_manual_at", None)
    if paid_at is not None and set_at is not None and paid_at <= set_at:
        log.info("membership: kept studio tier for user %s (payment %s predates "
                 "the studio change %s)", user.id, paid_at, set_at)
        return False
    user.membership_manual = None
    user.membership_manual_at = None
    log.info("membership: cleared studio tier for user %s after payment", user.id)
    return True


# --- what the tiers look like before there is an account ----------------------
# Sign-up and sign-in show the plans so someone can see what they are joining,
# but nothing there is buyable: Stripe tells us who paid by email address, so a
# membership bought before the account exists has nowhere to land.

PREVIEW_SUMMARIES = {
    "none": "Quotes, badges, courses & guides, and Content Hub free picks.",
    "healing": ("Healing community, healing support and Ayesha 1:1, "
                "one Showcase listing, healing Content Tips."),
    "creator": ("Building community, Content Hub, reels & spotlight, "
                "five Showcase listings, creator support and Saman 1:1."),
    "full_bloom": "Everything in both Healing and Creator.",
}

_PREVIEW_FALLBACK_NAMES = {
    "healing": "Healing membership",
    "creator": "Creator membership",
    "full_bloom": "Full Bloom membership",
}


def plan_preview() -> list[dict]:
    """The tiers as something to read rather than something to buy.

    Never raises: this goes on the sign-in page, and a plan lookup that fails
    should cost a teaser, not the way back into the site.
    """
    try:
        plans = {p.tier: p for p in MembershipPlan.query.all()}
    except Exception:
        log.exception("membership: could not load plans for the sign-in preview")
        return []
    rows = [{"tier": "none", "name": "Free", "price": "$0 / forever",
             "summary": PREVIEW_SUMMARIES["none"]}]
    for tier, fallback in _PREVIEW_FALLBACK_NAMES.items():
        plan = plans.get(tier)
        price = plan.price_display() if plan and plan.price_cents is not None else ""
        rows.append({
            "tier": tier,
            "name": (plan.name if plan else "") or fallback,
            "price": f"{price} / month" if price else "Coming soon",
            "summary": PREVIEW_SUMMARIES[tier],
        })
    return rows
