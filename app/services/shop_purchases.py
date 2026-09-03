"""Digital purchase fulfillment for My Space (Stripe)."""
from datetime import datetime

from sqlalchemy import func, or_

from ..extensions import db
from ..models import MembershipPlan, ShopPurchase, User, utcnow


def _norm_email(email: str) -> str:
    return (email or "").strip().lower()


def is_membership_variant(variant_id) -> bool:
    """True when this product id is a membership plan (not a course/guide)."""
    if variant_id is None:
        return False
    key = str(variant_id).strip()
    if not key:
        return False
    return (MembershipPlan.query
            .filter(or_(MembershipPlan.stripe_price_id == key,
                        MembershipPlan.stripe_price_id_annual == key,
                        MembershipPlan.ls_variant_id == key))
            .first()) is not None


def is_addon_checkout(
    *,
    variant_id=None,
    product_id=None,
    metadata: dict | None = None,
) -> bool:
    """True for facilitator / founder 1:1 add-ons (not catalogue courses)."""
    meta = metadata if isinstance(metadata, dict) else {}
    addon = str(meta.get("addon") or "").strip().lower()
    if addon in ("facilitator", "ayesha", "saman"):
        return True
    slug = str(meta.get("slug") or "").strip().lower()
    if slug.startswith("addon-"):
        return True
    kind = str(meta.get("kind") or "").strip().lower()
    if kind == "addon":
        return True

    from .settings import get_setting

    keys = []
    for raw in (variant_id, product_id):
        key = str(raw or "").strip()
        if key and key not in keys:
            keys.append(key)
    if not keys:
        return False
    addon_prices = {
        (get_setting("facilitator_stripe_price_id") or "").strip(),
        (get_setting("ayesha_stripe_price_id") or "").strip(),
        (get_setting("saman_stripe_price_id") or "").strip(),
    }
    addon_prices.discard("")
    return any(k in addon_prices for k in keys)


def sync_membership_perk(purchase, *, downgrade: bool = False) -> bool:
    """Re-apply the buyer's tier when a purchase carrying a perk changes.

    Some products hand out free membership months, so linking (or refunding)
    one can change the tier its buyer should be on. Ordinary purchases are
    left alone — a reconcile costs a Stripe lookup.
    """
    from .perks import purchase_has_perk

    user_id = getattr(purchase, "user_id", None)
    if not user_id or not purchase_has_perk(purchase):
        return False
    from .memberships import reconcile_user

    # The purchase row is usually still pending in this session.
    db.session.flush()
    user = db.session.get(User, user_id)
    if user is None:
        return False
    return reconcile_user(user, downgrade=downgrade)


def upsert_shop_purchase(
    *,
    lemon_squeezy_order_id: str,
    customer_email: str,
    product_name: str | None = None,
    product_id: str | None = None,
    variant_id: str | None = None,
    download_url: str | None = None,
    purchased_at: datetime | None = None,
    refunded: bool = False,
) -> ShopPurchase | None:
    """Create or update a shop purchase. Idempotent on lemon_squeezy_order_id.

    Skips membership-plan variants (those stay on the Order + membership path).
    Returns None when the variant is a membership (no ShopPurchase row).
    """
    order_id = str(lemon_squeezy_order_id or "").strip()
    if not order_id:
        raise ValueError("lemon_squeezy_order_id is required")

    email = _norm_email(customer_email)
    if not email:
        raise ValueError("customer_email is required")

    row = ShopPurchase.query.filter_by(lemon_squeezy_order_id=order_id).first()

    # Membership plans stay on the Order + membership path — never create a
    # ShopPurchase for them. Still allow refunds to mark an existing row.
    if is_membership_variant(variant_id) and row is None:
        return None
    # Facilitator / 1:1 add-ons are sessions, not library guides.
    if (
        is_addon_checkout(variant_id=variant_id, product_id=product_id)
        and row is None
        and not refunded
    ):
        return None

    if refunded:
        if row is None:
            # Refund before we ever saw the order — record it as refunded.
            row = ShopPurchase(
                lemon_squeezy_order_id=order_id,
                customer_email=email,
                product_name=(product_name or "").strip()[:200] or "Shop purchase",
                product_id=str(product_id).strip()[:80] if product_id else None,
                variant_id=str(variant_id).strip()[:80] if variant_id else None,
                download_url=(download_url or "").strip()[:1000] or None,
                purchased_at=purchased_at or utcnow(),
                status="refunded",
            )
            db.session.add(row)
        else:
            row.status = "refunded"
        sync_membership_perk(row, downgrade=True)
        return row

    # Idempotency: keep an existing non-refunded row, but still try to link
    # pending purchases and refresh the display name when we learn more.
    if row is not None:
        if product_name:
            cleaned = (product_name or "").strip()[:200]
            if cleaned:
                row.product_name = cleaned
        if row.status not in ("refunded", "removed") and (
                row.user_id is None or row.status == "pending_link"):
            user = (User.query
                    .filter(func.lower(User.email) == email, User.deleted_at.is_(None))
                    .first())
            if user:
                row.user_id = user.id
                row.status = "linked"
                sync_membership_perk(row)
        return row

    user = (User.query
            .filter(func.lower(User.email) == email, User.deleted_at.is_(None))
            .first())
    row = ShopPurchase(
        lemon_squeezy_order_id=order_id,
        customer_email=email,
        user_id=user.id if user else None,
        product_name=(product_name or "").strip()[:200] or "Shop purchase",
        product_id=str(product_id).strip()[:80] if product_id else None,
        variant_id=str(variant_id).strip()[:80] if variant_id else None,
        download_url=(download_url or "").strip()[:1000] or None,
        purchased_at=purchased_at or utcnow(),
        status="linked" if user else "pending_link",
    )
    db.session.add(row)
    if user:
        sync_membership_perk(row)
    return row


def link_pending_purchases(user: User) -> int:
    """Attach pending shop purchases for this email. Returns how many were linked."""
    if user is None or not user.email:
        return 0
    email = _norm_email(user.email)
    pending = (ShopPurchase.query
               .filter(func.lower(ShopPurchase.customer_email) == email,
                       ShopPurchase.status == "pending_link")
               .all())
    for row in pending:
        row.user_id = user.id
        row.status = "linked"
    for row in pending:
        # A guest checkout can carry a free membership perk with it.
        if sync_membership_perk(row):
            break
    return len(pending)


def _library_dedupe_key(purchase: ShopPurchase) -> str:
    """Stable key so repeated checkouts of the same guide collapse in the library."""
    from . import course_reader as reader_svc
    prod = reader_svc.catalog_product_for_purchase(purchase)
    if prod is not None:
        return f"p:{prod.id}"
    variant = (purchase.variant_id or purchase.product_id or "").strip().lower()
    if variant:
        return f"v:{variant}"
    name = (purchase.product_name or "").strip().lower()
    if name:
        return f"n:{name}"
    return f"id:{purchase.id}"


def dedupe_library_purchases(rows: list[ShopPurchase]) -> list[ShopPurchase]:
    """Keep the newest purchase per catalogue product / title."""
    seen: set[str] = set()
    out: list[ShopPurchase] = []
    for row in rows:
        key = _library_dedupe_key(row)
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def collapse_duplicate_purchases(user: User) -> int:
    """Mark older same-product library rows as removed. Returns how many were hidden."""
    if user is None or not getattr(user, "id", None):
        return 0
    rows = (ShopPurchase.query
            .filter_by(user_id=user.id, status="linked")
            .order_by(ShopPurchase.purchased_at.desc(), ShopPurchase.id.desc())
            .all())
    seen: set[str] = set()
    hidden = 0
    for row in rows:
        key = _library_dedupe_key(row)
        if key in seen:
            row.status = "removed"
            hidden += 1
        else:
            seen.add(key)
    return hidden


def linked_purchases_for(user: User, *, dedupe: bool = False):
    """Shop purchases shown in My Space (linked only)."""
    if user is None or not getattr(user, "is_authenticated", False):
        return []
    rows = (ShopPurchase.query
            .filter_by(user_id=user.id, status="linked")
            .order_by(ShopPurchase.purchased_at.desc(), ShopPurchase.id.desc())
            .all())
    if dedupe:
        return dedupe_library_purchases(rows)
    return rows


def library_purchases_for(user: User, *, dedupe: bool = False):
    """Everything to show in My Space, including what was removed from it.

    A purchase someone removed stays on the shelf, marked, rather than
    vanishing — nothing was refunded and it is still theirs, so a list it has
    silently dropped out of is a worse answer than one that says so.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return []
    rows = (ShopPurchase.query
            .filter(ShopPurchase.user_id == user.id,
                    ShopPurchase.status.in_(("linked", "removed")))
            .order_by(ShopPurchase.purchased_at.desc(), ShopPurchase.id.desc())
            .all())
    if dedupe:
        return dedupe_library_purchases(rows)
    return rows


def remove_from_library(user: User, purchase_id: int) -> bool:
    """Mark a purchase as put away (owner only). Does not refund Stripe."""
    if user is None or not getattr(user, "id", None):
        return False
    row = db.session.get(ShopPurchase, purchase_id)
    if row is None or row.user_id != user.id or row.status != "linked":
        return False
    row.status = "removed"
    return True


def restore_to_library(user: User, purchase_id: int) -> bool:
    """Undo a removal, so the buyer can open it again."""
    if user is None or not getattr(user, "id", None):
        return False
    row = db.session.get(ShopPurchase, purchase_id)
    if row is None or row.user_id != user.id or row.status != "removed":
        return False
    row.status = "linked"
    return True