"""The 2-Month Creator Challenge: the welcome that goes with the receipt.

Buying the challenge is joining something that starts, so a receipt on its own
leaves somebody holding proof of payment and no idea what happens next. Which
course counts as the challenge is :mod:`catalog`'s business; sending the hello,
once, is this module's — from a payment coming in, or from the owner sending it
by hand to somebody the payment path missed.
"""
from __future__ import annotations

import logging

from ..extensions import db
from ..models import Order, Product, utcnow

log = logging.getLogger(__name__)


def course() -> Product | None:
    """The course the challenge is sold as here, if it is sold here."""
    from .catalog import challenge_product

    return challenge_product()


def _claim(order: Order | None) -> bool:
    """Take this order's welcome, so two paths can't both send it.

    An order with nothing to claim against — a payment that never landed here,
    or one the owner is making good by hand — is left to the caller's judgement.
    """
    if order is None:
        return True
    if getattr(order, "welcome_sent_at", None) is not None:
        return False
    order.welcome_sent_at = utcnow()
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        log.exception("challenge: could not claim the welcome for %s",
                      getattr(order, "ls_order_id", None))
        return False
    return True


def welcome_already_sent(order: Order | None) -> bool:
    return bool(order is not None
                and getattr(order, "welcome_sent_at", None) is not None)


def send_welcome(email: str, *, product: Product | None = None,
                 order: Order | None = None, name: str = "") -> bool:
    """Send the challenge welcome (#30) to one buyer. True if it went out.

    Claimed against the order first, so a webhook arriving twice — or the owner
    sending it by hand after one already went — doesn't welcome anybody twice.
    """
    from .mailer import send_challenge_welcome

    address = (email or "").strip()
    if "@" not in address:
        return False
    if not _claim(order):
        log.info("challenge: welcome for %s already went out",
                 getattr(order, "ls_order_id", None))
        return False

    when = getattr(order, "created_at", None)
    title = (name or "").strip() or (product.title if product is not None else "")
    try:
        return send_challenge_welcome(
            address,
            product_name=title,
            order_id=getattr(order, "ls_order_id", "") or "",
            order_date=when.strftime("%b %d, %Y") if when else "",
            perk=(product.perk_summary().replace(", free", "")
                  if product is not None and product.has_perk() else ""),
            description=(product.receipt_blurb() if product is not None else ""),
        )
    except Exception:
        log.exception("challenge: welcome email failed for %s", address)
        return False


def last_purchase(email: str) -> Order | None:
    """The most recent challenge order for an address, if there is one."""
    from sqlalchemy import func

    from .catalog import counts_as_challenge

    address = (email or "").strip().lower()
    if "@" not in address:
        return None
    orders = (Order.query
              .filter(func.lower(Order.buyer_email) == address,
                      Order.status == "paid")
              .order_by(Order.created_at.desc(), Order.id.desc())
              .limit(40)
              .all())
    for order in orders:
        product = order.product if order.product_id else None
        if counts_as_challenge(product, product.title if product else ""):
            return order
    return None
