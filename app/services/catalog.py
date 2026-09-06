"""Courses & Guides catalogue helpers.

Old mock-catalogue rows (fixed slugs from the initial layout) are removed if
still present. Do not delete by title heuristics — that would wipe real
products named with words like "test".
"""
from __future__ import annotations

import re

from ..extensions import db
from ..models import Product

# Slugs created by the old mock catalogue — delete these if still present.
DEMO_SLUGS = (
    "rebuild-workbook",
    "custody-with-confidence",
    "boundaries-blueprint",
    "healing-bundle",
    "50-hooks",
    "0-to-10k",
    "first-digital-product",
    "creator-bundle",
)


def _purge_product(product: Product) -> str:
    """Hard-delete a product; detach order/testimonial/progress links first."""
    from ..models import CourseProgress, Order, Testimonial
    from .product_covers import clear as clear_cover, clear_all_gallery

    (Order.query.filter_by(product_id=product.id)
     .update({Order.product_id: None}, synchronize_session=False))
    (Testimonial.query.filter_by(product_id=product.id)
     .update({Testimonial.product_id: None}, synchronize_session=False))
    (CourseProgress.query.filter_by(product_id=product.id)
     .update({CourseProgress.product_id: None}, synchronize_session=False))
    clear_cover(product.id)
    clear_all_gallery(product.id)
    # The product's own cascade clears its assets, extracts included. Naming
    # them here as well would delete each one down two paths at once.
    db.session.delete(product)
    return "deleted"


def remove_demo_catalog() -> int:
    """Delete leftover mock catalogue rows by known slug only."""
    removed = 0
    for slug in DEMO_SLUGS:
        product = Product.query.filter_by(slug=slug).first()
        if product is None:
            continue
        _purge_product(product)
        removed += 1
    if removed:
        db.session.flush()
    return removed


def slugify_title(title: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return (base or "product")[:140]


def unique_product_slug(title: str, *, exclude_id: int | None = None) -> str:
    """Build a unique product slug from a title."""
    base = slugify_title(title)
    slug = base
    n = 2
    while True:
        q = Product.query.filter_by(slug=slug)
        if exclude_id is not None:
            q = q.filter(Product.id != exclude_id)
        if q.first() is None:
            return slug
        slug = f"{base}-{n}"[:160]
        n += 1


# --- which product is the challenge ------------------------------------------
#
# There is one challenge running at a time, so this is one product, kept as a
# site setting rather than a column: Round 3 takes over from Round 2 by being
# ticked, and the old one lets go by itself.

CHALLENGE_SETTING = "challenge_product_id"


def challenge_product_id() -> int:
    """The product buying counts as joining the challenge, or ``0``."""
    from .settings import get_setting

    try:
        return int((get_setting(CHALLENGE_SETTING) or "").strip() or 0)
    except (TypeError, ValueError):
        return 0


def is_challenge(product: Product | None) -> bool:
    return bool(product is not None and product.id
                and product.id == challenge_product_id())


def challenge_product() -> Product | None:
    """The course the challenge is sold as, if it's sold here at all.

    Whatever Studio has marked, and failing that a published course that says
    challenge in its name — the landing page shouldn't keep sending people
    away to a store just because a tick box hasn't been found yet. Newest
    first, so Round 3 takes over from Round 2 on its own.
    """
    marked = db.session.get(Product, challenge_product_id() or 0)
    if marked is not None and marked.status == "published":
        return marked
    return (Product.query
            .filter(Product.status == "published",
                    Product.test_mode.is_(False),
                    db.or_(Product.slug.ilike("%challenge%"),
                           Product.title.ilike("%challenge%")))
            .order_by(Product.created_at.desc(), Product.id.desc())
            .first())


def set_challenge_product(product: Product, on: bool) -> None:
    """Mark this product as the challenge, or let it go.

    Unticking only clears the setting when it is this product that holds it,
    so saving any other product leaves the challenge where it is.
    """
    from .settings import set_setting

    if on:
        set_setting(CHALLENGE_SETTING, str(product.id))
    elif is_challenge(product):
        set_setting(CHALLENGE_SETTING, "")
