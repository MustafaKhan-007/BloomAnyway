"""Bundles: one price at the counter, several products on the shelf.

A bundle is an ordinary catalogue product with its own Stripe price and a list
of other products inside it. Paying for it is one payment and one
``ShopPurchase``; what this does is turn that into a purchase per product held
inside, so every one of them opens in My Space exactly as it would have if it
had been bought on its own — its own reading progress, its own drip schedule,
its own free months if it carries any.

Those child rows carry the parent's order id with the product pinned on the
end, which is what makes all of this safe to run twice: a webhook replayed, a
buyer landing back from Stripe, an owner adding a product to a bundle that has
already sold. Nothing is created that is already there, and a refund or a
withdrawal can find everything one payment handed over.
"""
import logging

from sqlalchemy import or_

from ..extensions import db
from ..models import Product, ShopPurchase

log = logging.getLogger(__name__)

#: Between the payment's own id and the product it opened. Not a character
#: Stripe puts in an id, so nothing else can look like one of these.
CHILD_MARK = "#p"


def child_order_id(order_id: str, product_id: int) -> str:
    return f"{str(order_id or '').strip()}{CHILD_MARK}{int(product_id)}"


def contents(product: Product | None) -> list[Product]:
    """What a bundle hands over — nothing, unless it is ticked as a bundle.

    A bundle inside a bundle is left out rather than followed: Studio won't
    offer one, and reading a chain of them is not worth the loop it needs.
    """
    if product is None or not product.has_type("bundle"):
        return []
    return [p for p in product.bundle_products()
            if p.id != product.id and not p.has_type("bundle")]


def granted_from(purchase: ShopPurchase | None):
    """Every purchase row this one opened, if it was a bundle."""
    if purchase is None:
        return []
    order_id = (purchase.lemon_squeezy_order_id or "").strip()
    if not order_id or CHILD_MARK in order_id:
        return []
    return (ShopPurchase.query
            .filter(ShopPurchase.lemon_squeezy_order_id.startswith(
                order_id + CHILD_MARK, autoescape=True))
            .order_by(ShopPurchase.id)
            .all())


def grant_contents(purchase: ShopPurchase | None) -> list[ShopPurchase]:
    """Put each product inside a bundle purchase onto the buyer's shelf.

    Runs on the row the payment made. If that row isn't a bundle, or the
    bundle is empty, nothing happens. The caller commits.
    """
    from . import course_reader as reader_svc
    from .shop_purchases import sync_membership_perk

    if purchase is None or purchase.status not in ("linked", "pending_link",
                                                   "removed"):
        return []
    order_id = (purchase.lemon_squeezy_order_id or "").strip()
    if not order_id or CHILD_MARK in order_id:
        return []
    held = contents(reader_svc.catalog_product_for_purchase(purchase))
    if not held:
        return []

    already = {row.lemon_squeezy_order_id for row in granted_from(purchase)}
    made = []
    for product in held:
        child_id = child_order_id(order_id, product.id)
        if child_id in already:
            continue
        row = ShopPurchase(
            lemon_squeezy_order_id=child_id,
            customer_email=purchase.customer_email,
            user_id=purchase.user_id,
            product_name=(product.title or "").strip()[:200] or "Shop purchase",
            # Matched back to the catalogue the same way any purchase is, so
            # the reader, the drip schedule and the perks need to know nothing
            # about bundles.
            product_id=(product.stripe_price_id or "").strip()[:80] or None,
            variant_id=(product.stripe_price_id or "").strip()[:80] or None,
            # The schedule on a dripped course counts from here, so it counts
            # from when the bundle was paid for, not from when it was opened.
            purchased_at=purchase.purchased_at,
            # Putting the bundle away is a buyer tidying their own shelf, not
            # a reason for what came inside it to arrive already hidden.
            status="linked" if purchase.user_id else "pending_link",
        )
        db.session.add(row)
        made.append(row)
    if not made:
        return []
    db.session.flush()
    for row in made:
        if row.user_id:
            sync_membership_perk(row)
    log.info("bundle: purchase %s opened %s product(s) for %s",
             order_id, len(made), purchase.customer_email)
    return made


def backfill(product: Product | None) -> int:
    """Hand a bundle's contents to everyone who has already bought it.

    For the owner adding a product to a bundle that has been selling for a
    month: without this, only the next buyer would get it, and the ones who
    paid first would have the smaller bundle for good. The caller commits.
    """
    if product is None or not contents(product):
        return 0
    keys = [k for k in ((product.stripe_price_id or "").strip(),
                        (product.ls_variant_id or "").strip()) if k]
    matches = [ShopPurchase.product_name == product.title]
    if keys:
        matches.append(ShopPurchase.variant_id.in_(keys))
        matches.append(ShopPurchase.product_id.in_(keys))
    rows = (ShopPurchase.query
            .filter(ShopPurchase.status.in_(("linked", "pending_link", "removed")),
                    or_(*matches))
            .all())
    opened = 0
    for row in rows:
        if CHILD_MARK in (row.lemon_squeezy_order_id or ""):
            continue
        opened += len(grant_contents(row))
    return opened
