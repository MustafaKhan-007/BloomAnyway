"""Account closure / data-minimization helpers."""
from __future__ import annotations

import logging

from sqlalchemy import func, or_

from ..extensions import db
from ..models import (
    CheckIn,
    CoachingIntake,
    ContentReport,
    CourseProgress,
    Follow,
    ForumComment,
    ForumCommentLike,
    ForumPost,
    ForumPostLike,
    JournalEntry,
    MarketplaceListing,
    Notification,
    Order,
    QuoteFavorite,
    ReelReviewApplication,
    ReelSubmission,
    ShopPurchase,
    SiteFeedback,
    SupportGroupApplication,
    SupportGroupMeeting,
    SupportGroupTopicAlert,
    User,
    VerificationCode,
    VisitEvent,
    utcnow,
)

log = logging.getLogger(__name__)

_PAID_TIERS = frozenset({"healing", "creator", "full_bloom"})

# Shared placeholder so forum posts/comments keep a valid author after hard-delete.
FORMER_MEMBER_EMAIL = "former-member@invalid.local"
#: stand-in for rows that arrive after the account they belong to was deleted
CLOSED_ACCOUNT_EMAIL = "closed+orphan@invalid.local"


def _scrub_email_token(user_id: int, row_id: int) -> str:
    return f"closed+{user_id}.{row_id}@invalid.local"


def is_closed_account_email(email: str | None) -> bool:
    """True for the placeholder we leave on rows after an account is deleted.

    Fulfillment uses this to keep scrubbed rows scrubbed and to recognise a
    payment that belongs to someone who no longer has an account.
    """
    value = (email or "").strip().lower()
    return value.startswith("closed+") and value.endswith("@invalid.local")


def _former_member() -> User:
    """Return (or create) the single tombstone account for deleted authors."""
    row = User.query.filter_by(email=FORMER_MEMBER_EMAIL).first()
    if row is not None:
        # Keep it inert: never log in, never show as a real member.
        dirty = False
        if row.deleted_at is None:
            row.deleted_at = utcnow()
            dirty = True
        if row.display_name != "Former member":
            row.display_name = "Former member"
            dirty = True
        if row.password_hash is not None:
            row.password_hash = None
            dirty = True
        if (row.membership or "none") != "none":
            row.membership = "none"
            dirty = True
        if dirty:
            db.session.flush()
        return row

    row = User(
        email=FORMER_MEMBER_EMAIL,
        display_name="Former member",
        membership="none",
        deleted_at=utcnow(),
        password_hash=None,
        email_verified_at=None,
    )
    db.session.add(row)
    db.session.flush()
    return row


def _clear_membership_history(email: str, *, user_id: int) -> dict:
    """Cancel Stripe billing and detach local purchase history from this email.

    Re-signing up with the same address must start Free: paid Orders and shop
    rows must not still match the email for ``purchased_tier`` / link-pending.
    """
    email_norm = (email or "").strip().lower()
    out = {
        "stripe_ok": True,
        "stripe_cancelled": 0,
        "orders_ended": 0,
        "orders_scrubbed": 0,
        "shop_scrubbed": 0,
        "still_billing": [],
        "errors": [],
    }
    from .demo_accounts import is_demo_address

    if (not email_norm or "@" not in email_norm
            or email_norm.endswith("@invalid.local")
            or is_demo_address(email_norm)):
        return out

    # 1. Cancel live Stripe memberships immediately (also ends matching Orders).
    try:
        from . import stripe_pay as pay
        if pay.configured():
            result = pay.cancel_membership_subscriptions(
                email_norm, at_period_end=False, reason="account closed",
            )
            out["stripe_ok"] = bool(result.get("ok"))
            out["stripe_cancelled"] = len(result.get("cancelled") or [])
            out["orders_ended"] = int(result.get("orders_ended") or 0)
            if not result.get("ok"):
                errors = list(result.get("errors") or ["stripe_cancel_incomplete"])
                out["errors"].extend(errors)
                out["still_billing"] = [
                    err.split(":", 1)[1] for err in errors
                    if isinstance(err, str) and err.startswith("cancel_failed:")
                ]
                log.warning(
                    "close_account: Stripe cancel incomplete for user %s: %s",
                    user_id, result.get("errors"),
                )
            else:
                log.info(
                    "close_account: cancelled %s subscription(s) for user %s",
                    out["stripe_cancelled"], user_id,
                )
    except Exception:
        out["stripe_ok"] = False
        out["errors"].append("stripe_cancel_exception")
        log.exception(
            "close_account: Stripe cancel failed for user %s (%s)",
            user_id, email_norm,
        )

    # 2. End any remaining paid membership Orders (price ids missing / sync lag).
    try:
        from .memberships import tier_for_price_id
    except Exception:
        tier_for_price_id = lambda _pid: None  # noqa: E731

    orders = (
        Order.query
        .filter(
            (func.lower(Order.buyer_email) == email_norm)
            | (func.lower(Order.gift_to_email) == email_norm)
        )
        .all()
    )
    for order in orders:
        buyer_match = (order.buyer_email or "").strip().lower() == email_norm
        gift_match = (order.gift_to_email or "").strip().lower() == email_norm
        if buyer_match:
            tier = (order.membership_tier or "").strip().lower()
            if tier not in _PAID_TIERS:
                tier = (tier_for_price_id(order.ls_variant_id) or "").strip().lower()
            if tier in _PAID_TIERS and order.status == "paid":
                order.status = "ended"
                out["orders_ended"] += 1
            # Detach from the real email so reconcile cannot revive access on re-signup.
            order.buyer_email = _scrub_email_token(user_id, order.id)
        if gift_match:
            order.gift_to_email = None
        out["orders_scrubbed"] += 1

    # 3. Detach shop / course purchases so they don't auto-link on re-signup.
    shops = (
        ShopPurchase.query
        .filter(func.lower(ShopPurchase.customer_email) == email_norm)
        .all()
    )
    for row in shops:
        row.customer_email = _scrub_email_token(user_id, row.id)
        row.user_id = None
        if row.status == "linked":
            row.status = "pending_link"
        out["shop_scrubbed"] += 1

    return out


def _detach_and_purge_user_rows(user: User, *, tombstone_id: int) -> None:
    """Clear FKs / personal rows so the users row can be hard-deleted."""
    uid = user.id
    if uid == tombstone_id:
        raise ValueError("refusing to purge the former-member tombstone")

    # Community: keep threads, hide them, attribute to tombstone.
    ForumPost.query.filter_by(user_id=uid).update(
        {"hidden": True, "user_id": tombstone_id}, synchronize_session=False,
    )
    ForumComment.query.filter_by(user_id=uid).update(
        {"hidden": True, "user_id": tombstone_id}, synchronize_session=False,
    )
    ContentReport.query.filter_by(reporter_id=uid).update(
        {"reporter_id": tombstone_id}, synchronize_session=False,
    )

    # Likes / social / personal data — drop entirely.
    ForumPostLike.query.filter_by(user_id=uid).delete(synchronize_session=False)
    ForumCommentLike.query.filter_by(user_id=uid).delete(synchronize_session=False)
    Follow.query.filter(
        or_(Follow.follower_id == uid, Follow.following_id == uid),
    ).delete(synchronize_session=False)
    Notification.query.filter_by(user_id=uid).delete(synchronize_session=False)
    Notification.query.filter_by(actor_id=uid).update(
        {"actor_id": None}, synchronize_session=False,
    )
    QuoteFavorite.query.filter_by(user_id=uid).delete(synchronize_session=False)
    CheckIn.query.filter_by(user_id=uid).delete(synchronize_session=False)
    JournalEntry.query.filter_by(user_id=uid).delete(synchronize_session=False)
    VerificationCode.query.filter_by(user_id=uid).delete(synchronize_session=False)
    CourseProgress.query.filter_by(user_id=uid).delete(synchronize_session=False)
    CoachingIntake.query.filter_by(user_id=uid).delete(synchronize_session=False)
    SupportGroupApplication.query.filter_by(user_id=uid).delete(
        synchronize_session=False,
    )
    SupportGroupTopicAlert.query.filter_by(user_id=uid).delete(
        synchronize_session=False,
    )
    SupportGroupMeeting.query.filter_by(scheduled_by_user_id=uid).update(
        {"scheduled_by_user_id": None}, synchronize_session=False,
    )

    # Marketplace listings + images (cascade delete-orphan on relationship).
    for listing in MarketplaceListing.query.filter_by(user_id=uid).all():
        db.session.delete(listing)

    # Reel apps: keep published reviews by moving the application to tombstone.
    ReelReviewApplication.query.filter_by(user_id=uid).update(
        {"user_id": tombstone_id}, synchronize_session=False,
    )

    # Reel of the Week entries go entirely — a featured reel already lives in
    # site settings, so nothing on the home page depends on the row.
    for entry in ReelSubmission.query.filter_by(user_id=uid).all():
        if entry.disk_name:
            try:
                from flask import current_app
                from .videos import delete_stored
                delete_stored(current_app.config["VIDEO_STORAGE_DIR"],
                              entry.disk_name)
            except Exception:
                log.exception("close_account: could not remove reel upload")
        db.session.delete(entry)

    # Nullable analytics / feedback links.
    ShopPurchase.query.filter_by(user_id=uid).update(
        {"user_id": None}, synchronize_session=False,
    )
    VisitEvent.query.filter_by(user_id=uid).update(
        {"user_id": None}, synchronize_session=False,
    )
    SiteFeedback.query.filter_by(user_id=uid).update(
        {"user_id": None}, synchronize_session=False,
    )


def _alert_owner_still_billing(email: str, user_id: int, info: dict) -> None:
    """Email the owner when a closed account may still be billing in Stripe.

    Deletion goes ahead either way, but the owner has to know so the card stops
    being charged; a log line alone is how people end up paying for an account
    that no longer exists.
    """
    sub_ids = [s for s in (info.get("still_billing") or []) if s]
    lines = [
        f"Account #{user_id} was deleted, but we could not confirm their "
        "Stripe membership was cancelled.",
        "",
    ]
    if sub_ids:
        lines.append("Cancel these subscriptions in Stripe:")
        lines.extend(f"  {sid}" for sid in sub_ids)
    else:
        # No id to go on, so the owner needs the address to find the customer.
        lines.append(
            f"We could not list their subscriptions. Search Stripe for {email} "
            "and cancel anything still active."
        )
    lines += ["", f"Details: {info.get('errors')}"]
    try:
        from .mailer import send_billing_alert
        send_billing_alert("Deleted account may still be billing", "\n".join(lines))
    except Exception:
        log.exception("close_account: could not alert owner for user %s", user_id)


def close_account(user: User) -> None:
    """Cancel billing, scrub purchase history, and hard-delete the user row.

    Forum posts/comments stay (hidden) under a shared Former member account so
    threads remain intact. Re-signing up with the same email starts Free.
    """
    if user is None:
        return
    uid = user.id
    email = (user.email or "").strip()

    if (email or "").strip().lower() == FORMER_MEMBER_EMAIL:
        log.warning("close_account: refusing to delete former-member tombstone")
        return
    if user.is_admin:
        log.warning("close_account: refusing to delete admin user %s", uid)
        return

    # Cancel memberships / clear purchase history while we still have the email.
    clear_info = _clear_membership_history(email, user_id=uid)
    if clear_info.get("errors"):
        log.warning(
            "close_account: membership cleanup warnings for user %s: %s",
            uid, clear_info.get("errors"),
        )
        _alert_owner_still_billing(email, uid, clear_info)

    try:
        from .listings import enforce_listing_limits
        user.membership = "none"
        enforce_listing_limits(user)
    except Exception:
        log.exception("close_account: listing limit enforce failed for user %s", uid)

    tombstone = _former_member()
    _detach_and_purge_user_rows(user, tombstone_id=tombstone.id)

    db.session.delete(user)
    db.session.commit()
    log.info("close_account: hard-deleted user %s (%s)", uid, email or "?")
