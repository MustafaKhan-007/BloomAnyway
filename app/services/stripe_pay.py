"""Stripe: Checkout Sessions, webhook verification, order fulfillment."""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
from typing import Any
from urllib.parse import urlparse, urlunparse

import stripe
from flask import current_app

from ..extensions import db
from ..models import Order, Product

log = logging.getLogger(__name__)


class StripeError(RuntimeError):
    pass


def _secret_key() -> str:
    return (current_app.config.get("STRIPE_SECRET_KEY") or "").strip()


def _webhook_secret() -> str:
    return (current_app.config.get("STRIPE_WEBHOOK_SECRET") or "").strip()


def configured() -> bool:
    return bool(_secret_key())


def _configure_stripe() -> None:
    key = _secret_key()
    if not key:
        raise StripeError("Stripe is not configured (missing STRIPE_SECRET_KEY).")
    stripe.api_key = key


def _cancel_url_from_success(success_url: str) -> str:
    """Best-effort cancel URL (same origin, drop purchase flags)."""
    try:
        parts = urlparse(success_url)
        path = parts.path or "/"
        if path.startswith("/account"):
            path = "/membership" if "membership" in (parts.query or "") else "/courses"
        if "/membership" in success_url or "tier" in (parts.query or ""):
            path = "/membership"
        return urlunparse((parts.scheme, parts.netloc, path, "", "", ""))
    except Exception:
        return success_url


def create_checkout_session(
    *,
    product_id: str,
    return_url: str,
    customer_email: str | None = None,
    customer_name: str | None = None,
    metadata: dict | None = None,
    quantity: int = 1,
) -> str:
    """Create a Stripe Checkout Session and return the hosted URL.

    ``product_id`` is a Stripe Price id (``price_…``).
    Memberships use ``mode=subscription``; courses/guides use ``mode=payment``.
    """
    _configure_stripe()
    price_id = (product_id or "").strip()
    if not price_id:
        raise StripeError("Missing Stripe price id.")

    meta = {str(k): str(v) for k, v in (metadata or {}).items() if v is not None}
    meta["price_id"] = price_id
    kind = (meta.get("kind") or "").strip().lower()
    mode = "subscription" if kind == "membership" else "payment"

    success = (return_url or "").strip()
    if "session_id=" not in success:
        joiner = "&" if "?" in success else "?"
        success = f"{success}{joiner}session_id={{CHECKOUT_SESSION_ID}}"

    params: dict[str, Any] = {
        "mode": mode,
        "line_items": [{"price": price_id, "quantity": max(1, int(quantity or 1))}],
        "success_url": success,
        "cancel_url": _cancel_url_from_success(return_url),
        "metadata": meta,
        "allow_promotion_codes": True,
    }
    if customer_email:
        params["customer_email"] = customer_email.strip().lower()
    if customer_name:
        params["metadata"]["customer_name"] = customer_name.strip()[:120]
    if mode == "subscription":
        params["subscription_data"] = {"metadata": meta}

    try:
        session = stripe.checkout.Session.create(**params)
    except Exception as exc:
        log.warning("stripe checkout failed: %s", exc)
        raise StripeError(_friendly_checkout_error(exc, price_id)) from exc

    url = getattr(session, "url", None)
    if not url:
        raise StripeError("Stripe returned no checkout URL.")
    return str(url)


def _friendly_checkout_error(exc: Exception, price_id: str) -> str:
    """Turn opaque Stripe failures into something an owner can act on."""
    msg = str(exc or "").strip()
    low = msg.lower()
    pid = (price_id or "").strip()

    if pid and not pid.startswith("price_"):
        return (
            "Checkout needs a Stripe Price ID (starts with price_...), "
            "not a Product ID. In Stripe: Product -> open the price -> copy its ID, "
            "then paste that into Studio."
        )
    if "no such price" in low or ("no such" in low and "price" in low):
        return (
            "Stripe doesn't recognize that Price ID. Check it in the Stripe Dashboard, "
            "and make sure you're using test keys with test prices (or live with live)."
        )
    if "no such product" in low:
        return (
            "That looks like a Product ID. Create/open a Price under the product in Stripe "
            "and paste the price_... ID into Studio instead."
        )
    if "invalid api key" in low or "invalid api_key" in low:
        return "Stripe rejected the secret key. Double-check STRIPE_SECRET_KEY on Render."
    if "mode" in low and ("subscription" in low or "recurring" in low
                          or "one.time" in low or "one-time" in low):
        return (
            "That Price's billing type doesn't match this checkout "
            "(courses need a one-time price; memberships need a recurring price)."
        )
    if msg:
        short = msg.split("\n", 1)[0].strip()
        if len(short) > 180:
            short = short[:177] + "..."
        return f"Checkout could not be started: {short}"
    return "Checkout could not be started. Try again in a moment."


def sign_webhook(secret: str, payload: bytes, timestamp: int | None = None) -> str:
    """Build a Stripe-Signature header value (for tests)."""
    ts = int(timestamp if timestamp is not None else time.time())
    signed = f"{ts}.{payload.decode('utf-8')}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return f"t={ts},v1={digest}"


def verify_webhook_signature(raw_body: bytes, headers: dict, secret: str | None = None) -> bool:
    """Verify Stripe-Signature header. Returns True when valid."""
    secret = secret if secret is not None else _webhook_secret()
    if not secret:
        return False
    sig_header = (
        headers.get("stripe-signature")
        or headers.get("Stripe-Signature")
        or ""
    ).strip()
    if not sig_header:
        return False
    try:
        parts = {}
        for piece in sig_header.split(","):
            k, _, v = piece.partition("=")
            parts.setdefault(k.strip(), []).append(v.strip())
        ts = int((parts.get("t") or [""])[0])
        if abs(int(time.time()) - ts) > 300 and not current_app.config.get("TESTING"):
            return False
        signed = f"{ts}.{raw_body.decode('utf-8')}".encode("utf-8")
        expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
        for candidate in parts.get("v1") or []:
            if hmac.compare_digest(expected, candidate):
                return True
    except Exception:
        return False
    return False


def construct_event(raw_body: bytes, headers: dict, secret: str | None = None):
    """Parse and verify a Stripe webhook into an Event-like dict."""
    secret = secret if secret is not None else _webhook_secret()
    sig_header = (
        headers.get("stripe-signature")
        or headers.get("Stripe-Signature")
        or ""
    ).strip()
    if not secret or not sig_header:
        raise StripeError("Missing webhook secret or signature.")
    if not verify_webhook_signature(raw_body, headers, secret=secret):
        raise StripeError("Invalid Stripe webhook signature.")
    import json
    payload = json.loads(raw_body.decode("utf-8"))
    if not isinstance(payload, dict):
        raise StripeError("Invalid webhook payload.")
    return payload


def _product_for_price_id(price_id: str | None) -> Product | None:
    if not price_id:
        return None
    key = str(price_id).strip()
    if not key:
        return None
    row = Product.query.filter_by(stripe_price_id=key).first()
    if row:
        return row
    return Product.query.filter_by(ls_variant_id=key).first()


def _product_from_metadata(data: dict) -> Product | None:
    meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    slug = str((meta or {}).get("slug") or "").strip()
    if not slug:
        return None
    return Product.query.filter_by(slug=slug).first()


def _resolve_product(data: dict, price_id: str | None) -> Product | None:
    return _product_for_price_id(price_id) or _product_from_metadata(data)


def _price_id_from_membership_meta(meta: dict | None) -> str | None:
    """Resolve Stripe price id from membership checkout metadata (tier + billing)."""
    if not isinstance(meta, dict):
        return None
    tier = str(meta.get("tier") or "").strip().lower()
    if tier not in ("healing", "creator", "full_bloom"):
        return None
    from ..models import MembershipPlan
    plan = MembershipPlan.query.filter_by(tier=tier).first()
    if plan is None:
        return None
    billing = str(meta.get("billing") or "monthly").strip().lower()
    if billing in ("year", "yearly", "annual"):
        billing = "annual"
    else:
        billing = "monthly"
    return plan.payment_product_id(billing)


def _first_price_id(data: dict) -> str | None:
    meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    if isinstance(meta, dict):
        pid = meta.get("price_id") or meta.get("product_id")
        if pid:
            return str(pid)
        tier_pid = _price_id_from_membership_meta(meta)
        if tier_pid:
            return tier_pid
    cart = data.get("product_cart") or data.get("line_items") or []
    if isinstance(cart, list) and cart:
        first = cart[0] or {}
        if isinstance(first, dict):
            pid = first.get("price") or first.get("product_id") or first.get("id")
            if isinstance(pid, dict):
                pid = pid.get("id")
            if pid:
                return str(pid)
    product = _product_from_metadata(data)
    if product and (product.stripe_price_id or "").strip():
        return product.stripe_price_id.strip()
    return None


def _buyer_email(data: dict) -> str:
    customer = data.get("customer") or {}
    if isinstance(customer, dict):
        email = customer.get("email") or ""
        if email:
            return str(email).strip().lower()
    details = data.get("customer_details") or {}
    if isinstance(details, dict) and details.get("email"):
        return str(details["email"]).strip().lower()
    billing = data.get("billing_details") or {}
    if isinstance(billing, dict) and billing.get("email"):
        return str(billing["email"]).strip().lower()
    for key in ("customer_email", "email", "billing_email"):
        raw = data.get(key)
        if raw:
            return str(raw).strip().lower()
    return ""


def _amount_cents(data: dict) -> int:
    for key in ("amount_total", "total_amount", "amount"):
        raw = data.get(key)
        if raw is None:
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            continue
    return 0


def _as_dict(obj) -> dict:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "to_dict_recursive"):
        return obj.to_dict_recursive()
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    try:
        return dict(obj)
    except Exception:
        return {}


def _stripe_id(value) -> str | None:
    """Normalize a Stripe id that may arrive as a string or expanded object."""
    if value is None:
        return None
    if isinstance(value, str):
        key = value.strip()
        return key or None
    if isinstance(value, dict):
        return _stripe_id(value.get("id"))
    return _stripe_id(getattr(value, "id", None))


def _session_should_fulfill(session: dict) -> bool:
    """True when a Checkout Session should grant access (incl. $0 / 100% off)."""
    status = (session.get("status") or "").strip().lower()
    if status and status not in ("complete", "completed"):
        return False
    ps = (session.get("payment_status") or "").strip().lower()
    if ps in ("paid", "no_payment_required"):
        return True
    # Fully discounted sessions sometimes omit payment_status; still fulfill.
    try:
        amount = int(session.get("amount_total") if session.get("amount_total") is not None else -1)
    except (TypeError, ValueError):
        amount = -1
    if amount == 0 and (not status or status in ("complete", "completed")):
        return True
    return False


def enrich_checkout_session(obj: dict) -> dict:
    """Pull full session from Stripe when webhook payload is missing price/email.

    Checkout webhooks omit ``line_items``; $0 / 100% off sessions also omit
    ``payment_intent``. If metadata is thin, retrieve the session so we can still
    grant membership / course access.
    """
    if not isinstance(obj, dict):
        return {}
    try:
        from flask import has_app_context
        if not has_app_context():
            return obj
        if current_app.config.get("TESTING") or not configured():
            return obj
    except RuntimeError:
        return obj
    meta = obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {}
    has_price = bool(
        (meta or {}).get("price_id")
        or _price_id_from_membership_meta(meta)
        or _first_price_id({"metadata": meta, "product_cart": [],
                            "line_items": obj.get("line_items")})
    )
    has_email = bool(_buyer_email(obj))
    if has_price and has_email:
        return obj
    sid = _stripe_id(obj.get("id"))
    if not sid:
        return obj
    try:
        _configure_stripe()
        full = stripe.checkout.Session.retrieve(sid, expand=["line_items"])
        merged = _as_dict(full)
        # Prefer webhook metadata when Stripe retrieve returns empty metadata.
        wh_meta = meta or {}
        full_meta = merged.get("metadata") if isinstance(merged.get("metadata"), dict) else {}
        if wh_meta and not full_meta:
            merged["metadata"] = dict(wh_meta)
        elif wh_meta and full_meta:
            combined = dict(full_meta)
            combined.update({k: v for k, v in wh_meta.items() if v})
            merged["metadata"] = combined
        log.info(
            "stripe: hydrated checkout session %s (had_price=%s had_email=%s)",
            sid, has_price, has_email,
        )
        return merged
    except Exception:
        log.exception("stripe: failed to hydrate checkout session %s", sid)
        return obj


def invoice_subscription_id(invoice) -> str:
    """The subscription an invoice was raised for, across API versions.

    Stripe's 2025-03-31 "Basil" version removed ``invoice.subscription`` and
    moved it under ``parent.subscription_details``. Reading only the old field
    leaves us not knowing which subscription a renewal belongs to, which is far
    from harmless: a payment we can't tie to a subscription looks like it
    belongs to some *other* one, and the new-membership path then cancels the
    subscription that was just paid for.
    """
    inv = _as_dict(invoice)
    direct = _stripe_id(inv.get("subscription"))
    if direct:
        return direct
    parent = inv.get("parent")
    if isinstance(parent, dict):
        details = parent.get("subscription_details")
        if isinstance(details, dict):
            found = _stripe_id(details.get("subscription"))
            if found:
                return found
    details = inv.get("subscription_details")
    if isinstance(details, dict):
        found = _stripe_id(details.get("subscription"))
        if found:
            return found
    # Last resort: the line items carry it too, under their own parent.
    for line in ((inv.get("lines") or {}).get("data") or []):
        line_d = _as_dict(line)
        found = _stripe_id(line_d.get("subscription"))
        if found:
            return found
        line_parent = line_d.get("parent")
        if isinstance(line_parent, dict):
            item = line_parent.get("subscription_item_details")
            if isinstance(item, dict):
                found = _stripe_id(item.get("subscription"))
                if found:
                    return found
    return ""


def _invoice_subscription_metadata(invoice) -> dict:
    """Subscription metadata carried on an invoice (Basil moved this too).

    This is where ``tier`` lives on a renewal, so losing it means falling back
    to a price-id lookup for something we were told outright.
    """
    inv = _as_dict(invoice)
    parent = inv.get("parent")
    if isinstance(parent, dict):
        details = parent.get("subscription_details")
        if isinstance(details, dict) and isinstance(details.get("metadata"), dict):
            return dict(details["metadata"])
    details = inv.get("subscription_details")
    if isinstance(details, dict) and isinstance(details.get("metadata"), dict):
        return dict(details["metadata"])
    return {}


def _session_to_payment_data(session) -> dict:
    """Normalize a Checkout Session into our fulfillment shape."""
    session = _as_dict(session)
    meta = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
    price_id = (meta or {}).get("price_id")
    if not price_id:
        price_id = _price_id_from_membership_meta(meta)
    if not price_id:
        items = session.get("line_items") or {}
        items = _as_dict(items)
        data_items = items.get("data") if isinstance(items, dict) else items
        if isinstance(data_items, list) and data_items:
            first = _as_dict(data_items[0])
            price = first.get("price")
            if isinstance(price, dict):
                price_id = price.get("id")
            elif price:
                price_id = price
    email = _buyer_email(session)
    # $0 / 100% off: no PaymentIntent — fall back to subscription or session id.
    payment_id = (
        _stripe_id(session.get("payment_intent"))
        or _stripe_id(session.get("subscription"))
        or _stripe_id(session.get("id"))
    )
    meta_out = dict(meta or {})
    sub_id = _stripe_id(session.get("subscription"))
    if sub_id:
        meta_out["subscription_id"] = sub_id
    return {
        "payment_id": str(payment_id) if payment_id else "",
        "total_amount": session.get("amount_total") or 0,
        "currency": (session.get("currency") or "usd").upper(),
        "customer": {"email": email},
        "customer_email": email,
        "customer_details": session.get("customer_details") or {},
        "product_cart": [{"product_id": str(price_id), "quantity": 1}] if price_id else [],
        "metadata": meta_out,
        "payment_status": session.get("payment_status"),
        "mode": session.get("mode"),
        "id": _stripe_id(session.get("id")) or session.get("id"),
    }


def stripe_event_to_internal(event_type: str, obj: dict) -> tuple[str | None, dict]:
    """Map a Stripe event type + object into (internal_event, payment_data)."""
    if event_type in (
        "checkout.session.completed",
        "checkout.session.async_payment_succeeded",
    ):
        obj = enrich_checkout_session(obj)
        data = _session_to_payment_data(obj)
        if not _session_should_fulfill(obj):
            log.info(
                "stripe: skip checkout session %s (status=%s payment_status=%s amount=%s)",
                obj.get("id"), obj.get("status"), obj.get("payment_status"),
                obj.get("amount_total"),
            )
            return None, data
        if not data.get("product_cart"):
            log.warning(
                "stripe: checkout session %s has no price_id/line items "
                "(metadata=%s) — membership may not apply",
                obj.get("id"), obj.get("metadata"),
            )
        return "payment.succeeded", data
    if event_type == "invoice.paid":
        # Subscription first invoice can be $0 with a 100% promo — still grant access.
        meta = obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {}
        lines = (obj.get("lines") or {}).get("data") or []
        price_id = (meta or {}).get("price_id")
        if not price_id and lines:
            price = (lines[0].get("price") or {})
            price_id = price.get("id") if isinstance(price, dict) else None
            if not price_id and isinstance(lines[0].get("pricing"), dict):
                # newer invoice line shape
                price_details = (lines[0].get("pricing") or {}).get("price_details") or {}
                price_id = price_details.get("price")
        email = ""
        cust = obj.get("customer_email")
        if cust:
            email = str(cust).strip().lower()
        if not email:
            email = _buyer_email(obj)
        sub_id = invoice_subscription_id(obj)
        payment_id = (
            _stripe_id(obj.get("payment_intent"))
            or sub_id
            or _stripe_id(obj.get("id"))
        )
        amount = obj.get("amount_paid")
        if amount is None:
            amount = obj.get("total") or 0
        meta_out = dict(_invoice_subscription_metadata(obj))
        meta_out.update(meta or {})
        if sub_id:
            meta_out["subscription_id"] = sub_id
        reason = (obj.get("billing_reason") or "").strip()
        if reason:
            meta_out["billing_reason"] = reason
        return "payment.succeeded", {
            "payment_id": str(payment_id) if payment_id else "",
            "total_amount": amount or 0,
            "currency": (obj.get("currency") or "usd").upper(),
            "customer": {"email": email},
            "customer_email": email,
            "product_cart": [{"product_id": str(price_id), "quantity": 1}] if price_id else [],
            "metadata": meta_out,
        }
    if event_type == "invoice.payment_failed":
        meta = obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {}
        lines = (obj.get("lines") or {}).get("data") or []
        price_id = None
        if lines:
            price = (lines[0].get("price") or {})
            price_id = price.get("id") if isinstance(price, dict) else None
        email = ""
        cust = obj.get("customer_email")
        if cust:
            email = str(cust).strip().lower()
        sub_id = invoice_subscription_id(obj)
        meta_out = dict(meta or {})
        if sub_id:
            meta_out["subscription_id"] = sub_id
        return "payment.failed", {
            "payment_id": str(
                _stripe_id(obj.get("payment_intent")) or _stripe_id(obj.get("id")) or ""
            ),
            "total_amount": obj.get("amount_due") or 0,
            "currency": (obj.get("currency") or "usd").upper(),
            "customer": {"email": email},
            "product_cart": [{"product_id": price_id, "quantity": 1}] if price_id else [],
            "metadata": meta_out,
        }
    if event_type in ("charge.refunded", "charge.refund.updated"):
        pi = _stripe_id(obj.get("payment_intent"))
        return "payment.refunded", {
            "payment_id": str(pi or _stripe_id(obj.get("id")) or ""),
            "total_amount": obj.get("amount_refunded") or obj.get("amount") or 0,
            "currency": (obj.get("currency") or "usd").upper(),
            "customer": {"email": _buyer_email(obj)},
            "product_cart": [],
            "metadata": obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {},
        }
    if event_type == "customer.subscription.deleted":
        # Handled specially in the webhook route via handle_subscription_deleted
        # (must end the original paid membership orders, not invent a sub_ order).
        return None, {}
    return None, {}


def fulfill_checkout_session_id(session_id: str) -> Order | None:
    """Retrieve a Checkout Session by id and fulfill it (webhook backup / $0 codes)."""
    sid = (session_id or "").strip()
    if not sid or not configured():
        return None
    if current_app.config.get("TESTING"):
        return None
    _configure_stripe()
    try:
        full = stripe.checkout.Session.retrieve(sid, expand=["line_items"])
    except Exception as exc:
        log.warning("stripe: could not retrieve session %s: %s", sid, exc)
        return None
    session = _as_dict(full)
    if not _session_should_fulfill(session):
        log.info(
            "stripe: session %s not ready to fulfill (status=%s payment_status=%s)",
            sid, session.get("status"), session.get("payment_status"),
        )
        return None
    data = _session_to_payment_data(session)
    if not data.get("payment_id"):
        return None
    return handle_payment_event("payment.succeeded", data)


def sync_recent_payments(*, days: int = 60, max_pages: int = 3) -> dict:
    """Pull recent completed Checkout Sessions and fulfill any missing locally."""
    if not configured():
        return {"ok": False, "error": "not_configured", "imported": 0, "checked": 0}
    if current_app.config.get("TESTING"):
        return {"ok": True, "imported": 0, "checked": 0, "errors": 0, "skipped": "testing"}

    from datetime import datetime, timedelta, timezone

    _configure_stripe()
    since = datetime.now(timezone.utc) - timedelta(days=max(1, int(days or 60)))
    created_gte = int(since.timestamp())
    checked = 0
    imported = 0
    errors = 0

    starting_after = None
    for _ in range(max(1, int(max_pages or 1))):
        try:
            kwargs: dict[str, Any] = {
                "limit": 50,
                "created": {"gte": created_gte},
                "status": "complete",
            }
            if starting_after:
                kwargs["starting_after"] = starting_after
            page = stripe.checkout.Session.list(**kwargs)
        except Exception as exc:
            log.warning("stripe sync list failed: %s", exc)
            return {
                "ok": False,
                "error": str(exc),
                "imported": imported,
                "checked": checked,
            }
        items = list(page.data or [])
        if not items:
            break
        for session in items:
            checked += 1
            starting_after = session.id
            try:
                full = stripe.checkout.Session.retrieve(
                    session.id, expand=["line_items"]
                )
                data = _session_to_payment_data(full)
                payment_id = data.get("payment_id")
                if not payment_id:
                    continue
                before = Order.query.filter_by(ls_order_id=str(payment_id)).first()
                was_new = before is None or before.status != "paid"
                if not _session_should_fulfill(_as_dict(full)):
                    continue
                handle_payment_event("payment.succeeded", data)
                db.session.commit()
                if was_new:
                    imported += 1
            except Exception:
                errors += 1
                db.session.rollback()
                log.exception("stripe sync: failed to fulfill %s", session.id)
        if len(items) < 50:
            break

    log.info(
        "stripe sync: checked=%s imported=%s errors=%s",
        checked, imported, errors,
    )
    return {
        "ok": True,
        "imported": imported,
        "checked": checked,
        "errors": errors,
    }


def start_background_sync(*, days: int = 60, max_pages: int = 2) -> bool:
    """Kick off ``sync_recent_payments`` off the request thread.

    Opening Studio used to wait on two pages of Stripe API calls whenever the
    throttle expired, which is seconds of blank screen for the owner. The page
    no longer waits: the pull happens behind it and shows up on the next load.
    """
    if not configured():
        return False
    from .background import run_in_background
    return run_in_background(
        "stripe-sync", sync_recent_payments, days=days, max_pages=max_pages)


_last_cancel_sweep_mono = 0.0
_CANCEL_SWEEP_GAP_SEC = 60 * 60


def maybe_sweep_cancel_flags() -> bool:
    """Hourly-at-most cancel-flag refresh, off the request thread."""
    global _last_cancel_sweep_mono
    if not configured():
        return False
    now_mono = time.monotonic()
    if (now_mono - _last_cancel_sweep_mono) < _CANCEL_SWEEP_GAP_SEC:
        return False
    _last_cancel_sweep_mono = now_mono
    from .background import run_in_background
    return run_in_background("stripe-cancel-sweep", sweep_cancel_flags)


def _claim_membership_welcome(email: str, tier: str, order: Order) -> bool:
    """Return True once per email+tier — claim before sending to stop duplicates.

    checkout.session.completed and invoice.paid often use different payment ids,
    so order-level send_receipt alone is not enough.
    """
    from sqlalchemy import func

    from ..models import utcnow

    email_l = (email or "").strip().lower()
    tier_l = (tier or "").strip().lower()
    if not email_l or "@" not in email_l or tier_l not in ("healing", "creator", "full_bloom"):
        return False
    if getattr(order, "welcome_sent_at", None) is not None:
        return False

    prior = (
        Order.query
        .filter(
            func.lower(Order.buyer_email) == email_l,
            Order.membership_tier == tier_l,
            Order.welcome_sent_at.isnot(None),
        )
        .first()
    )
    # The email match breaks once an account is closed (we scrub the address off
    # their orders), so also check the subscription: a renewal of a membership we
    # already welcomed is never a new membership.
    sub_id = (getattr(order, "stripe_subscription_id", None) or "").strip()
    if prior is None and sub_id:
        prior = (
            Order.query
            .filter(
                Order.stripe_subscription_id == sub_id,
                Order.welcome_sent_at.isnot(None),
            )
            .first()
        )
    if prior is not None:
        return False

    # Claim immediately so a parallel webhook with another payment_id skips.
    order.welcome_sent_at = utcnow()
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        log.exception(
            "stripe: failed claiming membership welcome for %s (%s)",
            email_l, tier_l,
        )
        return False
    return True


def upsert_order_from_payment(
    *,
    payment_id: str,
    product_id: str | None,
    buyer_email: str,
    total_cents: int,
    currency: str,
    status: str,
    gift_to: str | None = None,
    membership_tier: str | None = None,
    subscription_id: str | None = None,
) -> Order:
    """Insert or update an order; idempotent on payment_id (stored as ls_order_id)."""
    from .privacy import is_closed_account_email

    order = Order.query.filter_by(ls_order_id=str(payment_id)).first()
    if order is None:
        order = Order(ls_order_id=str(payment_id))
        db.session.add(order)
    if product_id and str(product_id).strip():
        order.ls_variant_id = str(product_id).strip()
    email_norm = (buyer_email or "").strip().lower()
    usable = (email_norm and "@" in email_norm
              and not email_norm.endswith("@invalid")
              and not is_closed_account_email(email_norm))
    if is_closed_account_email(order.buyer_email) and not (
            usable and _user_for_email(email_norm)):
        # This row was scrubbed when the buyer deleted their account. Re-running
        # fulfillment (a late webhook, a dashboard sync) must not put their
        # address back — unless they have since signed up again, in which case
        # the row is theirs once more and leaving it detached hides a
        # membership they are paying for.
        pass
    elif usable:
        order.buyer_email = email_norm
    elif not order.buyer_email:
        order.buyer_email = email_norm or "unknown@invalid"
    sub_ref = (subscription_id or "").strip()
    if sub_ref and not order.stripe_subscription_id:
        order.stripe_subscription_id = sub_ref[:80]
    if gift_to:
        order.gift_to_email = gift_to.strip().lower()
    order.total_cents = int(total_cents or 0)
    order.currency = (currency or "USD").upper()[:3]
    order.status = status

    tier = (membership_tier or "").strip().lower()
    if tier not in ("healing", "creator", "full_bloom"):
        from .memberships import tier_for_price_id
        tier = tier_for_price_id(order.ls_variant_id) or ""
    if tier in ("healing", "creator", "full_bloom"):
        order.membership_tier = tier

    if order.ls_variant_id and order.product_id is None:
        from .shop_purchases import is_addon_checkout
        if not is_addon_checkout(variant_id=order.ls_variant_id):
            product = _product_for_price_id(order.ls_variant_id)
            if product:
                order.product_id = product.id
            else:
                log.warning(
                    "stripe: no product with stripe_price_id=%s (payment %s)",
                    order.ls_variant_id, order.ls_order_id,
                )
    from .memberships import apply_from_order
    apply_from_order(order)
    return order


def _live_user_from_meta(meta) -> "User | None":
    """The live account named by a payment/subscription's ``buyer_user_id``.

    Membership checkouts stamp the buyer's account id into metadata (and, via
    ``subscription_data``, onto the subscription so renewals carry it). Matching
    on that id — not just whatever email Stripe happens to hold — is what keeps a
    live member from being mistaken for a deleted account after an email change.
    """
    from ..models import User

    if not isinstance(meta, dict):
        return None
    raw = str((meta or {}).get("buyer_user_id") or "").strip()
    if not raw.isdigit():
        return None
    user = db.session.get(User, int(raw))
    if user is not None and user.deleted_at is None:
        return user
    return None


def _subscription_buyer_user_id(sub_id: str) -> str:
    """buyer_user_id stamped on a Stripe subscription's metadata (empty if none)."""
    sid = (sub_id or "").strip()
    if not sid.startswith("sub_") or not configured():
        return ""
    if current_app.config.get("TESTING"):
        return ""
    try:
        _configure_stripe()
        sub = _as_dict(stripe.Subscription.retrieve(sid))
    except Exception:
        log.exception("stripe: could not read subscription %s for its buyer id", sid)
        return ""
    meta = sub.get("metadata") if isinstance(sub.get("metadata"), dict) else {}
    return str((meta or {}).get("buyer_user_id") or "").strip()


def _ensure_subscription_buyer_meta(sub_id: str, user_id: int) -> None:
    """Stamp buyer_user_id onto a live subscription that predates it.

    Subscriptions bought before this link existed can still be tied to their
    account here, so a later renewal is never mistaken for a deleted account's
    charge and cancelled. Best-effort: a failure just leaves things as they were.
    """
    sid = (sub_id or "").strip()
    if not sid.startswith("sub_") or not configured():
        return
    if current_app.config.get("TESTING"):
        return
    try:
        _configure_stripe()
        sub = _as_dict(stripe.Subscription.retrieve(sid))
        meta = sub.get("metadata") if isinstance(sub.get("metadata"), dict) else {}
        if str((meta or {}).get("buyer_user_id") or "").strip():
            return
        new_meta = dict(meta or {})
        new_meta["buyer_user_id"] = str(user_id)
        stripe.Subscription.modify(sid, metadata=new_meta)
        log.info("stripe: stamped buyer_user_id=%s onto subscription %s", user_id, sid)
    except Exception:
        log.exception("stripe: could not stamp buyer id onto subscription %s", sid)


def _membership_is_orphaned(order: Order, sub_id: str | None,
                            payer_email: str | None = None,
                            meta: dict | None = None) -> bool:
    """True only when nobody holds the account this payment is paying for.

    Closing an account scrubs the buyer email off their orders, so a charge that
    keeps arriving afterwards is usually billing for an account that no longer
    exists. Usually — people come back. Someone who deletes their account and
    signs up again (or just changes their email) still owns that subscription,
    and cancelling it then cancels a membership they are using and paying for.

    So a live account always wins, and we look hard for one — by the buyer id
    carried on the payment/subscription first, then by the paying email — before
    writing anything off. Being unsure counts as not orphaned: the worst case
    here is a charge the owner has to refund, against cancelling a paying member
    by mistake and scrubbing their order off their account.
    """
    from ..models import User
    from .privacy import is_closed_account_email

    # 1) The buyer's own account id, stamped on this payment, is the firmest
    #    signal there is — an email change can't shake it loose.
    if _live_user_from_meta(meta) is not None:
        return False

    # 2) A live account for the paying email wins too.
    payer = (payer_email or "").strip().lower()
    if payer and not is_closed_account_email(payer) and _user_for_email(payer):
        return False

    key = (sub_id or getattr(order, "stripe_subscription_id", None) or "").strip()

    # 3) Ask the subscription itself who it belongs to before writing anyone off:
    #    it may name its buyer's account id (bought after this fix), or bill an
    #    email that now holds an account again (a returning member).
    if key:
        buyer_uid = _subscription_buyer_user_id(key)
        if buyer_uid.isdigit():
            u = db.session.get(User, int(buyer_uid))
            if u is not None and u.deleted_at is None:
                return False
        live = _subscription_payer_email(key)
        if live and _user_for_email(live):
            log.info(
                "stripe: subscription %s bills %s who holds an account — "
                "not orphaned", key, live,
            )
            return False

    # 4) Only now, with no live owner found anywhere, is a scrubbed order (or a
    #    subscription whose orders were all scrubbed) an orphaned charge.
    if is_closed_account_email(getattr(order, "buyer_email", None)):
        return True
    if not key:
        return False
    rows = Order.query.filter(Order.stripe_subscription_id == key).all()
    if not any(is_closed_account_email(row.buyer_email) for row in rows):
        return False
    return True


def _subscription_payer_email(sub_id: str) -> str:
    """Who Stripe currently bills for this subscription (empty when unknown)."""
    sid = (sub_id or "").strip()
    if not sid.startswith("sub_") or not configured():
        return ""
    if current_app.config.get("TESTING"):
        return ""
    try:
        _configure_stripe()
        sub = _as_dict(stripe.Subscription.retrieve(sid, expand=["customer"]))
    except Exception:
        log.exception("stripe: could not read subscription %s to find its payer", sid)
        return ""
    return _email_from_subscription(sub)


def _reclaim_subscription_orders(email_norm: str, sub_id: str | None) -> int:
    """Give a subscription's scrubbed paid orders back to a live account.

    Once a payment has been written off as orphaned its row is detached from
    every account, and it stays detached — so the tier it pays for never lands
    and the member looks unpaid forever. Finding a live owner undoes that.
    """
    from .privacy import is_closed_account_email

    key = (sub_id or "").strip()
    email = (email_norm or "").strip().lower()
    if not key or not email or is_closed_account_email(email):
        return 0
    rows = (Order.query
            .filter(Order.stripe_subscription_id == key,
                    Order.status == "paid")
            .all())
    fixed = 0
    for row in rows:
        if is_closed_account_email(row.buyer_email):
            row.buyer_email = email
            fixed += 1
    if fixed:
        log.warning(
            "stripe: reattached %s paid order(s) on subscription %s to %s — "
            "they had been written off as a deleted account's",
            fixed, key, email,
        )
    return fixed


def _handle_orphan_membership_payment(order: Order, sub_id: str | None,
                                      payer_email: str | None = None,
                                      meta: dict | None = None) -> None:
    """Stop billing a deleted account, and tell the owner it happened."""
    from ..models import User
    from .privacy import CLOSED_ACCOUNT_EMAIL, is_closed_account_email

    key = (sub_id or getattr(order, "stripe_subscription_id", None) or "").strip()
    # Last line of defence before we cancel someone's billing. Cancelling a
    # paying member is far worse than leaving a deleted account's charge for
    # the owner to refund by hand, so anything that looks alive stops us.
    live = _live_user_from_meta(meta)
    if live is None and key:
        buyer_uid = _subscription_buyer_user_id(key)
        if buyer_uid.isdigit():
            candidate = db.session.get(User, int(buyer_uid))
            if candidate is not None and candidate.deleted_at is None:
                live = candidate
    if live is not None:
        log.error(
            "stripe: refused to write off payment %s — account %s (%s) is live "
            "(subscription %s)", order.ls_order_id, live.id, live.email,
            key or "unknown",
        )
        return
    payer = (payer_email or getattr(order, "buyer_email", None) or "").strip().lower()
    if not (payer and not is_closed_account_email(payer)):
        # The event didn't name a payer, so ask Stripe rather than act blind.
        payer = _subscription_payer_email(key)
    if payer and not is_closed_account_email(payer) and _user_for_email(payer):
        log.error(
            "stripe: refused to write off payment %s — %s holds a live account "
            "(subscription %s)", order.ls_order_id, payer, key or "unknown",
        )
        return
    # A renewal arrives as a brand new order row, which would otherwise carry the
    # address we deleted for them.
    if not is_closed_account_email(order.buyer_email):
        order.buyer_email = CLOSED_ACCOUNT_EMAIL
    log.warning(
        "stripe: membership payment %s belongs to a closed account "
        "(subscription %s) — no welcome sent",
        order.ls_order_id, key or "unknown",
    )
    cancelled = False
    if key and configured() and not current_app.config.get("TESTING"):
        try:
            cancelled = _cancel_stripe_subscription_now(
                key, "payment for an account that was deleted")
        except Exception:
            log.exception("stripe: orphan cancel failed for %s", key)
    if cancelled:
        body = (
            f"Subscription {key} was still charging after the member deleted "
            "their account. We cancelled it just now.\n\n"
            f"Payment {order.ls_order_id} may need refunding — that's your call."
        )
    else:
        body = (
            f"Payment {order.ls_order_id} came in for a membership whose account "
            "was deleted"
            + (f" (subscription {key})." if key else ".")
            + "\n\nWe could not cancel it automatically. Please cancel it in "
            "Stripe so they stop being charged."
        )
    try:
        from .mailer import send_billing_alert
        send_billing_alert("Deleted account was charged again", body)
    except Exception:
        log.exception(
            "stripe: could not alert owner about orphan payment %s",
            order.ls_order_id,
        )


def handle_payment_event(event_type: str, data: dict) -> Order | None:
    """Fulfill or refund from a normalized payment payload."""
    from datetime import timedelta

    from sqlalchemy import func

    from ..models import User, utcnow
    from .memberships import _plan_for_product_id, tier_for_price_id
    from .privacy import is_closed_account_email

    payment_id = (
        data.get("payment_id")
        or data.get("id")
        or (data.get("payment") or {}).get("payment_id")
    )
    if not payment_id:
        raise ValueError("payment_id missing from webhook data")

    product_id = _first_price_id(data)
    email = _buyer_email(data)
    currency = (data.get("currency") or "USD").upper()
    meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    gift_to = (meta or {}).get("gift_to") or (meta or {}).get("giftTo")
    # Stripe tells us when an invoice continues an existing subscription rather
    # than starting one. Only a first payment can be a new membership.
    renewal = str((meta or {}).get("billing_reason") or "").strip().lower() in (
        "subscription_cycle", "subscription_update", "subscription_threshold",
    )

    if event_type in ("payment.succeeded", "payment.processing"):
        if event_type != "payment.succeeded":
            return None
        status = "paid"
    elif event_type == "payment.failed":
        status = "failed"
    elif event_type in ("payment.cancelled", "refund.succeeded", "payment.refunded"):
        status = "refunded"
    else:
        return None

    if not email and status in ("paid", "failed"):
        if status == "paid":
            raise ValueError("customer email missing from paid payment")
        log.warning("stripe: payment.failed missing customer email (%s)", payment_id)
        return None

    prior = Order.query.filter_by(ls_order_id=str(payment_id)).first()
    send_receipt = status == "paid" and (prior is None or prior.status != "paid")
    send_decline = (
        status == "failed"
        and (prior is None or prior.status != "failed")
    )

    meta_tier = str((meta or {}).get("tier") or "").strip().lower()
    grant_tier = meta_tier if meta_tier in ("healing", "creator", "full_bloom") else None
    if not grant_tier:
        grant_tier = tier_for_price_id(product_id)
    plan = _plan_for_product_id(product_id) if product_id else None
    if plan is None and grant_tier:
        from ..models import MembershipPlan
        plan = MembershipPlan.query.filter_by(tier=grant_tier).first()

    prev_tier = "none"
    buyer = None
    if email:
        buyer = (User.query
                 .filter(func.lower(User.email) == email.strip().lower(),
                         User.deleted_at.is_(None))
                 .first())
    if buyer is None:
        # The paying email may not match the account (email change, stale Stripe
        # customer). The buyer id carried on the payment resolves it firmly.
        buyer = _live_user_from_meta(meta)
    if buyer is not None:
        prev_tier = buyer.effective_membership()

    order = upsert_order_from_payment(
        payment_id=str(payment_id),
        product_id=str(product_id) if product_id else None,
        buyer_email=email or "unknown@invalid",
        total_cents=_amount_cents(data),
        currency=currency,
        status=status,
        gift_to=gift_to,
        membership_tier=grant_tier,
        subscription_id=(meta or {}).get("subscription_id"),
    )

    is_membership = bool(plan and plan.tier in ("healing", "creator", "full_bloom"))
    orphaned = False
    if status == "paid" and is_membership:
        sub_ref = (meta or {}).get("subscription_id")
        orphaned = _membership_is_orphaned(
            order, sub_ref, payer_email=email, meta=meta)
        # send_receipt marks a payment we haven't fulfilled before, so replaying
        # old payments through a sync can't spam the owner with the same alert.
        if orphaned and send_receipt:
            _handle_orphan_membership_payment(
                order, sub_ref, payer_email=email, meta=meta)
        elif not orphaned and buyer is not None:
            # Someone who changed their email (or deleted their account and came
            # back) owns this payment. Attribute it to the account we now know is
            # live — Stripe may have billed a different address — so the tier they
            # paid for actually lands, and hand back anything written off earlier.
            reclaim_email = (buyer.email or email or "").strip().lower()
            sub_key = sub_ref or getattr(order, "stripe_subscription_id", None)
            if (reclaim_email
                    and (order.buyer_email or "").strip().lower() != reclaim_email
                    and not is_closed_account_email(order.buyer_email)):
                order.buyer_email = reclaim_email
            _reclaim_subscription_orders(reclaim_email, sub_key)
            from .memberships import reconcile_email
            reconcile_email(reclaim_email)
            # Stamp the account id onto an older subscription that lacks it, so
            # its next renewal is tied to the account instead of guessed by email.
            if sub_key:
                _ensure_subscription_buyer_meta(sub_key, buyer.id)
        elif renewal and buyer is None:
            # Subscriptions predating the subscription_id column can't be tied
            # back to a closed account, so leave a trail rather than guessing.
            log.warning(
                "stripe: membership renewal %s has no account behind it "
                "(subscription %s)",
                order.ls_order_id, (meta or {}).get("subscription_id") or "unknown",
            )

    # New membership purchase replaces any prior membership immediately
    # (cancel old Stripe sub + revoke local access, keep only this order).
    if (status == "paid" and is_membership and not orphaned
            and order.buyer_email and "@" in order.buyer_email):
        keep_sub = (meta or {}).get("subscription_id") or ""
        if not keep_sub:
            keep_sub = _subscription_id_from_payment_ref(str(payment_id or "")) or ""
        try:
            replace_other_memberships(
                order.buyer_email,
                keep_order_id=str(order.ls_order_id or payment_id),
                keep_subscription_id=keep_sub or None,
            )
        except Exception:
            log.exception(
                "stripe: failed replacing prior memberships for %s",
                order.buyer_email,
            )

    from .shop_purchases import is_addon_checkout, upsert_shop_purchase
    addon_checkout = is_addon_checkout(
        variant_id=product_id,
        product_id=product_id,
        metadata=meta,
    )

    product = None if addon_checkout else _resolve_product(data, product_id)
    if product and order.product_id is None:
        order.product_id = product.id
        if not order.ls_variant_id and (product.stripe_price_id or "").strip():
            order.ls_variant_id = product.stripe_price_id.strip()

    name = (
        (product.title if product else None)
        or (meta or {}).get("product_name")
        or (plan.name if plan else None)
        or "Course purchase"
    )

    # For a closed account, keep the scrubbed address rather than re-recording
    # the real one on a fresh row.
    record_email = (
        order.buyer_email
        if is_closed_account_email(order.buyer_email)
        else (email or order.buyer_email)
    )

    if status == "paid" and not addon_checkout:
        upsert_shop_purchase(
            lemon_squeezy_order_id=str(payment_id),
            customer_email=record_email,
            product_name=name,
            product_id=str(product_id) if product_id else None,
            variant_id=str(product_id) if product_id else None,
            download_url=None,
            refunded=False,
        )
    elif status == "refunded" and not addon_checkout:
        upsert_shop_purchase(
            lemon_squeezy_order_id=str(payment_id),
            customer_email=record_email,
            product_name=name,
            product_id=str(product_id) if product_id else None,
            variant_id=str(product_id) if product_id else None,
            download_url=None,
            refunded=True,
        )

    # Product checkouts only — memberships get their tier welcome email, not the receipt.
    if (send_receipt and order.buyer_email and "@" in order.buyer_email
            and not addon_checkout and plan is None):
        try:
            from .mailer import send_order_receipt
            when = order.created_at
            order_date = when.strftime("%b %d, %Y") if when else ""
            send_order_receipt(
                order.buyer_email,
                order_id=order.ls_order_id,
                product_name=name,
                amount=order.total_display(),
                order_date=order_date,
            )
        except Exception:
            log.exception("Order receipt email failed for %s", order.ls_order_id)

    if (send_receipt and is_membership and not orphaned
            and order.buyer_email and "@" in order.buyer_email):
        # Already on this tier (or higher) before this fulfill — renewals / no-ops.
        already = (
            (plan.tier == "healing" and prev_tier in ("healing", "creator", "full_bloom"))
            or (plan.tier == "creator" and prev_tier in ("creator", "full_bloom"))
            or (plan.tier == "full_bloom" and prev_tier == "full_bloom")
        )
        # Durable claim: one welcome per email+tier across checkout + invoice ids.
        if (not already and not renewal
                and _claim_membership_welcome(order.buyer_email, plan.tier, order)):
            try:
                from .mailer import (
                    send_creator_welcome,
                    send_full_bloom_welcome,
                    send_healing_welcome,
                )
                key = str(product_id or "").strip()
                annual_id = (plan.stripe_price_id_annual or "").strip()
                is_annual = bool(annual_id and key == annual_id)
                if is_annual:
                    billing_interval = "annually"
                    plan_price = plan.annual_price_display() or order.total_display()
                else:
                    billing_interval = "monthly"
                    plan_price = plan.price_display() or order.total_display()
                if plan.tier == "healing":
                    trial_days = 14
                    sender = send_healing_welcome
                elif plan.tier == "creator":
                    trial_days = 7
                    sender = send_creator_welcome
                else:
                    trial_days = 7
                    sender = send_full_bloom_welcome
                trial_end = (utcnow() + timedelta(days=trial_days)).strftime("%b %d, %Y")
                sender(
                    order.buyer_email,
                    trial_end_date=trial_end,
                    plan_price=plan_price,
                    billing_interval=billing_interval,
                )
            except Exception:
                log.exception(
                    "%s welcome email failed for %s",
                    plan.tier.title(), order.ls_order_id,
                )

    if send_decline and plan and plan.tier in ("healing", "creator", "full_bloom"):
        to = (email or order.buyer_email or "").strip()
        try:
            grace = int(current_app.config.get("MEMBERSHIP_GRACE_DAYS") or 5)
        except (TypeError, ValueError):
            grace = 5
        if to and "@" in to:
            try:
                from .mailer import send_card_declined
                plan_name = plan.name or f"{plan.tier.replace('_', ' ').title()} membership"
                send_card_declined(
                    to,
                    plan_name=plan_name,
                    grace_days=grace,
                )
            except Exception:
                log.exception("Card-declined email failed for %s", payment_id)
        sub_id = (meta or {}).get("subscription_id") or ""
        if not sub_id:
            sub_id = _subscription_id_from_payment_ref(str(payment_id or "")) or ""
        if sub_id and not current_app.config.get("TESTING"):
            try:
                _schedule_membership_grace_cancel(sub_id, grace)
            except Exception:
                log.exception(
                    "stripe: grace cancel schedule failed for %s", sub_id,
                )

    if (status == "paid" and plan and plan.tier in ("healing", "creator", "full_bloom")
            and not current_app.config.get("TESTING")):
        sub_id = (meta or {}).get("subscription_id") or ""
        if not sub_id:
            sub_id = _subscription_id_from_payment_ref(str(payment_id or "")) or ""
        if sub_id:
            try:
                _clear_membership_grace_cancel(sub_id)
            except Exception:
                log.exception(
                    "stripe: clear grace cancel failed for %s", sub_id,
                )

    if status == "paid":
        try:
            from .coaching_intake import fulfill_from_payment_metadata
            fulfill_from_payment_metadata(
                meta or {},
                buyer_email=email or getattr(order, "buyer_email", None),
            )
        except Exception:
            log.exception(
                "stripe: coaching intake fulfill failed for payment %s", payment_id,
            )

    return order


def _membership_price_ids() -> set[str]:
    from ..models import MembershipPlan
    ids: set[str] = set()
    for plan in MembershipPlan.query.all():
        for raw in (
            plan.stripe_price_id,
            plan.stripe_price_id_annual,
            plan.ls_variant_id,
        ):
            key = (raw or "").strip()
            if key:
                ids.add(key)
    return ids


def _membership_product_ids() -> set[str]:
    from ..models import MembershipPlan
    ids: set[str] = set()
    for plan in MembershipPlan.query.all():
        for raw in (plan.stripe_product_id, plan.stripe_product_id_annual):
            key = (raw or "").strip()
            if key:
                ids.add(key)
    return ids


def _subscription_id_from_payment_ref(payment_id: str) -> str | None:
    """Resolve a stored order id (sub_ / cs_ / pi_) to a Stripe subscription id."""
    key = (payment_id or "").strip()
    if not key or not configured():
        return None
    if key.startswith("sub_"):
        return key
    _configure_stripe()
    try:
        if key.startswith("cs_"):
            session = _as_dict(stripe.checkout.Session.retrieve(key))
            return _stripe_id(session.get("subscription"))
        if key.startswith("in_"):
            return invoice_subscription_id(stripe.Invoice.retrieve(key)) or None
        if key.startswith("pi_"):
            pi = _as_dict(stripe.PaymentIntent.retrieve(key))
            invoice_id = _stripe_id(pi.get("invoice"))
            if not invoice_id:
                return None
            return invoice_subscription_id(stripe.Invoice.retrieve(invoice_id)) or None
    except Exception:
        log.exception("stripe: could not resolve subscription from %s", key)
    return None


def _order_subscription_id(order) -> str | None:
    """Subscription behind an Order, from the stored id before asking Stripe."""
    stored = str(getattr(order, "stripe_subscription_id", "") or "").strip()
    if stored:
        return stored
    oid = str(getattr(order, "ls_order_id", "") or "").strip()
    return _subscription_id_from_payment_ref(oid) if oid else None


#: Cancels the member asked for. Everything else is the site deciding on its
#: own that someone should stop being billed, which is the decision worth
#: hearing about — twice now it has been wrong.
_ASKED_FOR_CANCELS = ("member cancelled", "owner cancelled", "account closed",
                      "member switched to another plan")


def _cancel_stripe_subscription_now(sub_id: str, reason: str = "unspecified") -> bool:
    """Immediately cancel a Stripe subscription (no remaining access period).

    ``reason`` is written into Stripe's own cancellation record and, unless the
    cancel was asked for, emailed to the owner. Cancelling someone's billing is
    the most damaging thing this app does by itself, and until now it did it
    without leaving anything behind that said which code path decided to.
    """
    sid = (sub_id or "").strip()
    if not sid.startswith("sub_"):
        return False
    _configure_stripe()
    try:
        details = {"comment": f"Bloom Anyway: {reason}"[:500]}
        try:
            if hasattr(stripe.Subscription, "cancel"):
                stripe.Subscription.cancel(sid, cancellation_details=details)
            else:
                stripe.Subscription.delete(sid)
        except TypeError:
            # Older SDK signature — the cancel still matters more than the note.
            stripe.Subscription.cancel(sid)
        log.warning("stripe: CANCELLED subscription %s — reason: %s", sid, reason)
        if reason not in _ASKED_FOR_CANCELS:
            _tell_owner_we_cancelled(sid, reason)
        return True
    except Exception as exc:
        msg = str(exc).lower()
        if "already been canceled" in msg or "no such subscription" in msg:
            log.info("stripe: subscription %s already gone (%s)", sid, exc)
            return True
        log.exception("stripe: failed to cancel subscription %s", sid)
        return False


def _tell_owner_we_cancelled(sub_id: str, reason: str) -> None:
    """Say out loud that the site stopped someone's billing, and why."""
    try:
        from .mailer import send_billing_alert
        send_billing_alert(
            "The site cancelled a subscription",
            f"Subscription {sub_id} was cancelled by Bloom Anyway itself, not "
            f"by the member and not from the Stripe dashboard.\n\n"
            f"Reason: {reason}\n\n"
            "If that member should still be paying, this is a bug — send this "
            "message on. The same reason is written on the subscription in "
            "Stripe under Cancellation details.",
        )
    except Exception:
        log.exception("stripe: could not report an automatic cancel of %s", sub_id)


def _schedule_subscription_cancel(sub_id: str) -> dict | None:
    """Cancel at period end. Returns {id, period_end} or None on failure."""
    sid = (sub_id or "").strip()
    if not sid.startswith("sub_"):
        return None
    _configure_stripe()
    try:
        sub = stripe.Subscription.modify(sid, cancel_at_period_end=True)
        sub_d = _as_dict(sub)
        period_end = sub_d.get("current_period_end") or sub_d.get("cancel_at")
        try:
            period_end = int(period_end) if period_end is not None else None
        except (TypeError, ValueError):
            period_end = None
        log.info(
            "stripe: scheduled cancel_at_period_end for %s (period_end=%s)",
            sid, period_end,
        )
        return {"id": sid, "period_end": period_end}
    except Exception as exc:
        msg = str(exc).lower()
        if "already been canceled" in msg or "no such subscription" in msg:
            log.info("stripe: subscription %s already gone (%s)", sid, exc)
            return {"id": sid, "period_end": None}
        log.exception("stripe: failed to schedule cancel for %s", sid)
        return None


def _schedule_membership_grace_cancel(sub_id: str, grace_days: int) -> int | None:
    """After a failed renewal, cancel the sub at now+grace_days (access until then).

    Does not shorten an earlier voluntary cancel (cancel_at_period_end) or an
    already-scheduled cancel_at. Returns the unix cancel timestamp, or None.
    """
    sid = (sub_id or "").strip()
    if not sid.startswith("sub_") or not configured():
        return None
    try:
        days = max(1, int(grace_days))
    except (TypeError, ValueError):
        days = 5
    _configure_stripe()
    try:
        sub_d = _as_dict(stripe.Subscription.retrieve(sid))
        if sub_d.get("cancel_at_period_end"):
            existing = sub_d.get("cancel_at") or sub_d.get("current_period_end")
            try:
                return int(existing) if existing is not None else None
            except (TypeError, ValueError):
                return None
        target = int(time.time()) + days * 86400
        existing = sub_d.get("cancel_at")
        try:
            existing_i = int(existing) if existing is not None else None
        except (TypeError, ValueError):
            existing_i = None
        if existing_i and existing_i <= target:
            log.info(
                "stripe: grace cancel already set for %s at %s", sid, existing_i,
            )
            return existing_i
        stripe.Subscription.modify(sid, cancel_at=target)
        log.info(
            "stripe: grace cancel_at=%s (%s days) for %s",
            target, days, sid,
        )
        return target
    except Exception as exc:
        msg = str(exc).lower()
        if "already been canceled" in msg or "no such subscription" in msg:
            log.info("stripe: subscription %s already gone (%s)", sid, exc)
            return None
        log.exception("stripe: failed to schedule grace cancel for %s", sid)
        return None


def _clear_membership_grace_cancel(sub_id: str) -> bool:
    """Clear a grace ``cancel_at`` after a successful renewal (not voluntary cancel)."""
    sid = (sub_id or "").strip()
    if not sid.startswith("sub_") or not configured():
        return False
    _configure_stripe()
    try:
        sub_d = _as_dict(stripe.Subscription.retrieve(sid))
        if sub_d.get("cancel_at_period_end"):
            return False
        if not sub_d.get("cancel_at"):
            return False
        stripe.Subscription.modify(sid, cancel_at="")
        log.info("stripe: cleared grace cancel_at for %s after successful payment", sid)
        return True
    except Exception as exc:
        msg = str(exc).lower()
        if "already been canceled" in msg or "no such subscription" in msg:
            return False
        log.exception("stripe: failed to clear grace cancel for %s", sid)
        return False


def replace_other_memberships(
    email: str,
    *,
    keep_order_id: str,
    keep_subscription_id: str | None = None,
) -> dict:
    """On a new membership payment: end every other membership immediately.

    Cancels other Stripe membership subscriptions now, marks their paid Orders
    refunded (access revoked), keeps ``keep_order_id`` / ``keep_subscription_id``,
    clears cancel-at flags, and reconciles the member to the new plan only.
    """
    from sqlalchemy import func

    from .memberships import reconcile_email

    email_norm = (email or "").strip().lower()
    keep_oid = str(keep_order_id or "").strip()
    result: dict[str, Any] = {
        "ok": True,
        "cancelled": [],
        "errors": [],
        "orders_ended": 0,
    }
    if not email_norm or not keep_oid:
        result["ok"] = False
        result["errors"].append("missing_email_or_order")
        return result

    price_ids = _membership_price_ids()
    if not price_ids:
        return result

    keep_sub = (keep_subscription_id or "").strip()
    if not keep_sub:
        keep_sub = _subscription_id_from_payment_ref(keep_oid) or ""
    # Without knowing which subscription this payment is for, "every membership
    # subscription except the new one" is every membership subscription they
    # have, the one they just bought included. Local orders can still be tidied
    # — the order being kept goes on granting the tier — but nothing may be
    # cancelled in Stripe on a guess.
    blind = not keep_sub
    if blind:
        log.error(
            "stripe: cancelling nothing for %s — could not tell which "
            "subscription payment %s belongs to", email_norm, keep_oid,
        )
        result["ok"] = False
        result["errors"].append("unknown_subscription")

    paid = (
        Order.query
        .filter(
            func.lower(Order.buyer_email) == email_norm,
            Order.status == "paid",
            Order.ls_variant_id.in_(list(price_ids)),
        )
        .all()
    )

    cancel_subs: set[str] = set()
    ended = 0
    for order in paid:
        oid = str(order.ls_order_id or "").strip()
        if oid and oid == keep_oid:
            continue
        sid = _order_subscription_id(order)
        if sid and not blind and sid != keep_sub:
            cancel_subs.add(sid)
        order.status = "ended"
        ended += 1

    # Also cancel any live Stripe membership sub that isn't the new one
    # (covers orphans not linked to a local Order id).
    if not blind and configured() and not current_app.config.get("TESTING"):
        for sid in _membership_subscription_ids_for_email(email_norm, price_ids):
            if sid and sid != keep_sub:
                cancel_subs.add(sid)

    for sid in sorted(cancel_subs):
        if current_app.config.get("TESTING") or not configured():
            result["cancelled"].append(sid)
            continue
        if _cancel_stripe_subscription_now(
                sid, f"replaced by a new membership ({keep_sub})"):
            result["cancelled"].append(sid)
        else:
            result["ok"] = False
            result["errors"].append(f"cancel_failed:{sid}")

    result["orders_ended"] = ended
    if ended or result["cancelled"]:
        clear_cancel_at_for_email(email_norm)
        reconcile_email(email_norm, downgrade=True)
        log.info(
            "stripe: membership switch for %s ended %s prior order(s), "
            "cancelled %s sub(s); kept order=%s sub=%s",
            email_norm, ended, len(result["cancelled"]), keep_oid, keep_sub or "-",
        )
    return result


def cancel_membership_subscriptions(
    email: str,
    *,
    at_period_end: bool = True,
    reason: str = "owner cancelled",
) -> dict:
    """Cancel Stripe membership subscriptions for this email.

    Member self-cancel (``at_period_end=True``): schedules cancel at the end of
    the paid period, keeps local paid Orders so access continues until Stripe
    fires ``customer.subscription.deleted``.

    Studio revoke (``at_period_end=False``): cancels immediately and marks
    matching paid Orders refunded so access drops now.
    """
    email_norm = (email or "").strip().lower()
    result: dict[str, Any] = {
        "ok": True,
        "cancelled": [],
        "errors": [],
        "orders_ended": 0,
        "period_end": None,
        "at_period_end": bool(at_period_end),
    }
    if not email_norm:
        result["ok"] = False
        result["errors"].append("missing_email")
        return result

    price_ids = _membership_price_ids()
    if not price_ids:
        result["errors"].append("no_membership_prices")
        return result

    from sqlalchemy import func

    paid = (
        Order.query
        .filter(
            func.lower(Order.buyer_email) == email_norm,
            Order.status == "paid",
            Order.ls_variant_id.in_(list(price_ids)),
        )
        .all()
    )

    sub_ids: set[str] = set()
    for order in paid:
        sid = _subscription_id_from_payment_ref(order.ls_order_id or "")
        if sid:
            sub_ids.add(sid)

    if configured() and not current_app.config.get("TESTING"):
        try:
            _configure_stripe()
            customers_data = []
            try:
                safe = email_norm.replace("\\", "\\\\").replace("'", "\\'")
                found = stripe.Customer.search(
                    query=f"email:'{safe}'", limit=20,
                )
                customers_data = list(found.data or [])
            except Exception:
                listed = stripe.Customer.list(limit=100)
                customers_data = [
                    c for c in list(listed.data or [])
                    if (getattr(c, "email", None) or "").strip().lower() == email_norm
                ]
            for cust in customers_data:
                cust_id = _stripe_id(cust) or getattr(cust, "id", None)
                if not cust_id:
                    continue
                for status in ("active", "trialing", "past_due"):
                    page = stripe.Subscription.list(
                        customer=cust_id, status=status, limit=20,
                    )
                    for sub in list(page.data or []):
                        sub_d = _as_dict(sub)
                        items = (sub_d.get("items") or {}).get("data") or []
                        matched = False
                        for item in items:
                            item_d = _as_dict(item)
                            pid = _stripe_id(item_d.get("price"))
                            if pid and pid in price_ids:
                                matched = True
                                break
                        if matched:
                            sid = _stripe_id(sub_d.get("id"))
                            if sid:
                                sub_ids.add(sid)
        except Exception as exc:
            log.exception("stripe: list subscriptions failed for %s", email_norm)
            result["errors"].append(str(exc))

    period_ends: list[int] = []
    for sid in sorted(sub_ids):
        if current_app.config.get("TESTING") or not configured():
            result["cancelled"].append(sid)
            continue
        if at_period_end:
            info = _schedule_subscription_cancel(sid)
            if info:
                result["cancelled"].append(sid)
                pe = info.get("period_end")
                if isinstance(pe, int) and pe > 0:
                    period_ends.append(pe)
            else:
                result["ok"] = False
                result["errors"].append(f"cancel_failed:{sid}")
        else:
            if _cancel_stripe_subscription_now(sid, reason):
                result["cancelled"].append(sid)
            else:
                result["ok"] = False
                result["errors"].append(f"cancel_failed:{sid}")

    if period_ends:
        result["period_end"] = max(period_ends)

    # Immediate revoke only — period-end keeps Orders paid until subscription.deleted.
    if not at_period_end:
        ended = _end_paid_membership_orders(email_norm, price_ids)
        result["orders_ended"] = ended
        if ended:
            log.info(
                "stripe: ended %s membership order(s) for %s after immediate cancel",
                ended, email_norm,
            )
    return result


def resume_membership_subscriptions(email: str) -> dict:
    """Undo cancel-at-period-end on membership subscriptions (keep renewing)."""
    email_norm = (email or "").strip().lower()
    result: dict[str, Any] = {
        "ok": True,
        "resumed": [],
        "errors": [],
    }
    if not email_norm:
        result["ok"] = False
        result["errors"].append("missing_email")
        return result

    price_ids = _membership_price_ids()
    if not price_ids:
        result["errors"].append("no_membership_prices")
        return result

    sub_ids = _membership_subscription_ids_for_email(email_norm, price_ids)
    if current_app.config.get("TESTING") or not configured():
        result["resumed"] = sorted(sub_ids)
        return result

    _configure_stripe()
    for sid in sorted(sub_ids):
        try:
            sub_d = _as_dict(stripe.Subscription.retrieve(sid))
            if not sub_d.get("cancel_at_period_end") and not sub_d.get("cancel_at"):
                result["resumed"].append(sid)
                continue
            stripe.Subscription.modify(sid, cancel_at_period_end=False)
            # Clear a grace cancel_at if one was set (voluntary cancel uses period end).
            try:
                refreshed = _as_dict(stripe.Subscription.retrieve(sid))
                if refreshed.get("cancel_at"):
                    stripe.Subscription.modify(sid, cancel_at="")
            except Exception:
                pass
            result["resumed"].append(sid)
            log.info("stripe: resumed membership subscription %s for %s", sid, email_norm)
        except Exception as exc:
            msg = str(exc).lower()
            if "already been canceled" in msg or "no such subscription" in msg:
                result["errors"].append(f"gone:{sid}")
                continue
            log.exception("stripe: failed to resume %s", sid)
            result["ok"] = False
            result["errors"].append(f"resume_failed:{sid}")
    return result


def membership_cancel_status(email: str) -> dict:
    """Live Stripe view of cancel-at-period-end for this email's memberships.

    Returns ``{canceling, period_end, access_end}``.
    """
    email_norm = (email or "").strip().lower()
    out = {"canceling": False, "period_end": None, "access_end": ""}
    if not email_norm or not configured() or current_app.config.get("TESTING"):
        return out
    price_ids = _membership_price_ids()
    if not price_ids:
        return out
    period_ends: list[int] = []
    for sid in _membership_subscription_ids_for_email(email_norm, price_ids):
        try:
            _configure_stripe()
            sub_d = _as_dict(stripe.Subscription.retrieve(sid))
            status = (sub_d.get("status") or "").strip()
            if status not in ("active", "trialing", "past_due"):
                continue
            if not (sub_d.get("cancel_at_period_end") or sub_d.get("cancel_at")):
                continue
            pe = sub_d.get("cancel_at") or sub_d.get("current_period_end")
            try:
                pe_i = int(pe) if pe is not None else None
            except (TypeError, ValueError):
                pe_i = None
            if pe_i:
                period_ends.append(pe_i)
            out["canceling"] = True
        except Exception:
            log.exception("stripe: cancel status failed for %s", sid)
    if period_ends:
        out["period_end"] = max(period_ends)
        out["access_end"] = format_access_end_date(out["period_end"])
    return out


def resolve_membership_tier(
    price_id: str | None,
    *,
    metadata: dict | None = None,
    product_name: str | None = None,
    nickname: str | None = None,
    stripe_product_id: str | None = None,
    fetch_stripe_labels: bool = False,
) -> str | None:
    """Resolve healing/creator/full_bloom for a checkout / subscription.

    Priority:
    1. Checkout metadata ``tier``
    2. Studio plan matched by Stripe price id
    3. Studio plan matched by Stripe product id
    4. Known Stripe product names (Healing/Creator/Full Bloom Membership …)
    """
    from .memberships import tier_for_price_id, tier_for_stripe_product

    meta = metadata if isinstance(metadata, dict) else {}
    meta_tier = str((meta or {}).get("tier") or "").strip().lower()
    if meta_tier in ("healing", "creator", "full_bloom"):
        return meta_tier

    plan_tier = tier_for_price_id(price_id)
    if plan_tier:
        return plan_tier

    pname = (product_name or "").strip()
    nick = (nickname or "").strip()
    prod_id = (stripe_product_id or "").strip() or None
    if fetch_stripe_labels and price_id and not (pname or prod_id):
        prod_id, pname, nick = _stripe_price_product_info(price_id)

    return tier_for_stripe_product(prod_id, pname or nick)


#: Add-on prices are set once and read on every page that lists them, so a
#: short in-process cache keeps a Stripe round trip out of the render.
_PRICE_TEXT_CACHE: dict[str, tuple[float, str]] = {}
_PRICE_TEXT_TTL = 600.0


def format_price_amount(cents, currency: str) -> str:
    """Money the way the rest of the catalogue writes it."""
    if cents is None:
        return ""
    try:
        amount = int(cents)
    except (TypeError, ValueError):
        return ""
    code = (currency or "usd").upper()[:3]
    symbol = {"USD": "$", "EUR": "\u20ac", "GBP": "\u00a3"}.get(code, code + " ")
    value = amount / 100
    return (f"{symbol}{value:,.0f}" if amount % 100 == 0
            else f"{symbol}{value:,.2f}")


def price_display(price_id: str | None) -> str:
    """What Stripe charges for one price id, formatted. Empty when unknown.

    Never raises and never leaves a page waiting: if Stripe can't be reached
    the caller just falls back to whatever it said before there was a price.
    """
    pid = (price_id or "").strip()
    if not pid or not configured() or current_app.config.get("TESTING"):
        return ""
    now = time.monotonic()
    cached = _PRICE_TEXT_CACHE.get(pid)
    if cached and (now - cached[0]) < _PRICE_TEXT_TTL:
        return cached[1]
    try:
        _configure_stripe()
        price = _as_dict(stripe.Price.retrieve(pid))
    except Exception:
        # Cache the miss too, or a mistyped id costs a Stripe call per view.
        log.warning("stripe: could not read price %s", pid, exc_info=True)
        _PRICE_TEXT_CACHE[pid] = (now, "")
        return ""
    text = format_price_amount(price.get("unit_amount"), price.get("currency"))
    recurring = price.get("recurring")
    if text and isinstance(recurring, dict) and recurring.get("interval"):
        count = recurring.get("interval_count") or 1
        unit = str(recurring.get("interval"))
        text += f" / {unit}" if count == 1 else f" / {count} {unit}s"
    _PRICE_TEXT_CACHE[pid] = (now, text)
    return text


#: The bookable extras, and the Studio setting holding each one's price id.
ADDON_PRICE_SETTINGS = {
    "facilitator": "facilitator_stripe_price_id",
    "ayesha": "ayesha_stripe_price_id",
    "saman": "saman_stripe_price_id",
}


def addon_prices() -> dict[str, str]:
    """Formatted price per add-on, empty string where there isn't one to show."""
    from .settings import get_setting

    out = {}
    for kind, key in ADDON_PRICE_SETTINGS.items():
        try:
            out[kind] = price_display(get_setting(key))
        except Exception:
            log.exception("stripe: could not price add-on %s", kind)
            out[kind] = ""
    return out


def _stripe_price_product_info(price_id: str | None) -> tuple[str | None, str, str]:
    """Return (product_id, product_name, nickname) for a Stripe price."""
    pid = (price_id or "").strip()
    if not pid or not configured() or current_app.config.get("TESTING"):
        return None, "", ""
    try:
        _configure_stripe()
        price = stripe.Price.retrieve(pid, expand=["product"])
        price_d = _as_dict(price)
        nick = str(price_d.get("nickname") or "")
        prod = price_d.get("product")
        if isinstance(prod, dict):
            return (
                _stripe_id(prod.get("id")) or None,
                str(prod.get("name") or ""),
                nick,
            )
        if isinstance(prod, str) and prod.startswith("prod_"):
            p = _as_dict(stripe.Product.retrieve(prod))
            return prod, str(p.get("name") or ""), nick
        return None, "", nick
    except Exception:
        log.warning("stripe: could not load product for price %s", pid, exc_info=True)
        return None, "", ""


def _tier_from_live_subscription(sub_d: dict) -> tuple[str | None, str | None]:
    """Resolve (tier, price_id) for one Stripe subscription dict."""
    meta = sub_d.get("metadata") if isinstance(sub_d.get("metadata"), dict) else {}
    items = (sub_d.get("items") or {}).get("data") or []
    price_id = None
    price_nick = ""
    product_name = ""
    stripe_product_id = None
    for item in items:
        item_d = _as_dict(item)
        pid = _stripe_id(item_d.get("price"))
        if not pid:
            continue
        price_id = pid
        price_obj = item_d.get("price")
        if isinstance(price_obj, dict):
            price_nick = str(price_obj.get("nickname") or "")
            prod = price_obj.get("product")
            if isinstance(prod, dict):
                product_name = str(prod.get("name") or "")
                stripe_product_id = _stripe_id(prod.get("id"))
            elif isinstance(prod, str) and prod.startswith("prod_"):
                stripe_product_id = prod
        break

    tier = resolve_membership_tier(
        price_id,
        metadata=meta,
        product_name=product_name,
        nickname=price_nick,
        stripe_product_id=stripe_product_id,
        fetch_stripe_labels=not (product_name or stripe_product_id),
    )
    return tier, price_id


def _list_customer_membership_subs(email_norm: str) -> list[dict] | None:
    """Active/trialing/past_due membership subscriptions for this email.

    Returns a list (possibly empty) on success, or ``None`` if Stripe could
    not be queried (caller must not treat that as “no membership”).
    """
    if not email_norm or not configured() or current_app.config.get("TESTING"):
        return None
    price_ids = _membership_price_ids()
    product_ids = _membership_product_ids()
    out: list[dict] = []
    try:
        _configure_stripe()
        customers_data = []
        try:
            safe = email_norm.replace("\\", "\\\\").replace("'", "\\'")
            found = stripe.Customer.search(query=f"email:'{safe}'", limit=20)
            customers_data = list(found.data or [])
        except Exception:
            # Filter server-side. Paging the whole customer list and matching
            # locally silently misses anyone past the first page, and a member
            # we fail to find here reads as "not paying" and loses their tier.
            listed = stripe.Customer.list(email=email_norm, limit=20)
            customers_data = list(listed.data or [])
        for cust in customers_data:
            cust_id = _stripe_id(cust) or getattr(cust, "id", None)
            if not cust_id:
                continue
            for status in ("active", "trialing", "past_due"):
                # No expand: subscription items already carry the full price,
                # and Stripe rejects any expand string deeper than four
                # properties — "data.items.data.price.product" is five, so it
                # used to 400 and take the whole live lookup down with it.
                page = stripe.Subscription.list(
                    customer=cust_id, status=status, limit=20,
                )
                for sub in list(page.data or []):
                    sub_d = _as_dict(sub)
                    items = (sub_d.get("items") or {}).get("data") or []
                    matched = False
                    for item in items:
                        item_d = _as_dict(item)
                        pid = _stripe_id(item_d.get("price"))
                        if pid and pid in price_ids:
                            matched = True
                            break
                        price_obj = item_d.get("price")
                        if isinstance(price_obj, dict):
                            prod = price_obj.get("product")
                            prod_id = (
                                _stripe_id(prod.get("id"))
                                if isinstance(prod, dict) else _stripe_id(prod)
                            )
                            if prod_id and prod_id in product_ids:
                                matched = True
                                break
                    if matched:
                        out.append(sub_d)
                        continue
                    # Still accept when product name / metadata maps to a plan.
                    tier, _ = _tier_from_live_subscription(sub_d)
                    if tier in ("healing", "creator", "full_bloom"):
                        out.append(sub_d)
        return out
    except Exception:
        log.exception("stripe: list live memberships failed for %s", email_norm)
        return None


def active_membership_tier_from_stripe(email: str) -> str | None:
    """Live Stripe membership tier for this email, or None if Stripe unusable.

    Returns ``none`` / ``healing`` / ``creator`` / ``full_bloom`` when Stripe
    answered successfully (including no active memberships → ``none``).
    Returns ``None`` when Stripe isn't configured or the lookup failed so the
    caller should fall back to local Orders.
    """
    email_norm = (email or "").strip().lower()
    if not email_norm or not configured() or current_app.config.get("TESTING"):
        return None

    try:
        subs = _list_customer_membership_subs(email_norm)
    except Exception:
        log.exception("stripe: active membership lookup failed for %s", email_norm)
        return None
    if subs is None:
        return None

    if not subs:
        try:
            _heal_local_membership_orders(
                email_norm, keep_subscription_ids=set(), keep_price_ids=set(),
            )
        except Exception:
            log.exception("stripe: heal local membership orders failed for %s", email_norm)
        log.info("stripe: live membership for %s → none (0 active subs)", email_norm)
        return "none"

    ranked = []
    for sub_d in subs:
        tier, price_id = _tier_from_live_subscription(sub_d)
        sid = _stripe_id(sub_d.get("id"))
        created = int(sub_d.get("created") or 0)
        if tier not in ("healing", "creator", "full_bloom"):
            continue
        ranked.append((tier, created, sid, price_id, sub_d))

    if not ranked:
        log.info("stripe: live membership for %s → none (subs not mapped)", email_norm)
        return "none"

    def _rank(row):
        t, created, _sid, _pid, _sub = row
        rank = {"full_bloom": 2, "creator": 1, "healing": 1, "none": 0}.get(t, 0)
        return (rank, created)

    ranked.sort(key=_rank, reverse=True)
    best_tier, _created, keep_sid, keep_price, _sub = ranked[0]
    keep_subs = {keep_sid} if keep_sid else set()
    keep_prices = {keep_price} if keep_price else set()

    # Duplicates are reported, never cancelled here. This function answers
    # "what tier is this person on", and gets called on ordinary reads — a
    # reconcile, a page load, the Studio audit. Cancelling from a lookup means
    # billing can stop at a moment nobody asked for anything, which is
    # impossible to reason about from the outside. A real replacement is
    # handled where one actually happens, at the new payment.
    extras = [sid for _t, _c, sid, _pid, _sub in ranked[1:]
              if sid and sid != keep_sid]
    if extras:
        log.warning(
            "stripe: %s has %s membership subscriptions at once (keeping %s, "
            "also live: %s) — not cancelling from a lookup",
            email_norm, len(ranked), keep_sid, ", ".join(extras),
        )
        try:
            from .mailer import send_billing_alert
            send_billing_alert(
                "A member has more than one membership subscription",
                f"{email_norm} is being billed for {len(ranked)} memberships at "
                f"once.\n\nTheir tier here follows {keep_sid}. Also live: "
                + ", ".join(extras)
                + "\n\nNothing was cancelled automatically. Cancel the extras in "
                "Stripe once you've checked which one they meant to keep.",
            )
        except Exception:
            log.exception("stripe: could not report duplicate subscriptions")

    try:
        _heal_local_membership_orders(
            email_norm,
            keep_subscription_ids=keep_subs,
            keep_price_ids=keep_prices,
        )
    except Exception:
        log.exception("stripe: heal local membership orders failed for %s", email_norm)

    log.info(
        "stripe: live membership for %s → %s (%s active sub(s))",
        email_norm, best_tier, len(ranked),
    )
    return best_tier


def _heal_local_membership_orders(
    email_norm: str,
    *,
    keep_subscription_ids: set[str],
    keep_price_ids: set[str],
) -> int:
    """End local paid membership orders that don't match live Stripe subs."""
    from sqlalchemy import func

    prices = _membership_price_ids()
    if not email_norm or not prices:
        return 0
    paid = (
        Order.query
        .filter(
            func.lower(Order.buyer_email) == email_norm,
            Order.status == "paid",
            Order.ls_variant_id.in_(list(prices)),
        )
        .all()
    )
    ended = 0
    for order in paid:
        variant = str(order.ls_variant_id or "").strip()
        sid = _order_subscription_id(order)
        if sid and sid in keep_subscription_ids:
            continue
        if (not sid) and variant and variant in keep_price_ids:
            continue
        # Live Creator (etc.) exists — drop other local paid membership rows.
        # Or no live membership — drop all local membership rows.
        order.status = "ended"
        ended += 1
    if ended:
        log.info(
            "stripe: healed %s stale membership order(s) for %s",
            ended, email_norm,
        )
    return ended


def _membership_subscription_ids_for_email(
    email_norm: str, price_ids: set[str] | None = None,
) -> set[str]:
    """Collect Stripe subscription ids for this email that use membership prices."""
    from sqlalchemy import func

    prices = price_ids or _membership_price_ids()
    sub_ids: set[str] = set()
    if not email_norm or not prices:
        return sub_ids

    paid = (
        Order.query
        .filter(
            func.lower(Order.buyer_email) == email_norm,
            Order.status == "paid",
            Order.ls_variant_id.in_(list(prices)),
        )
        .all()
    )
    for order in paid:
        sid = _subscription_id_from_payment_ref(order.ls_order_id or "")
        if sid:
            sub_ids.add(sid)

    if not configured() or current_app.config.get("TESTING"):
        return sub_ids

    try:
        _configure_stripe()
        customers_data = []
        try:
            safe = email_norm.replace("\\", "\\\\").replace("'", "\\'")
            found = stripe.Customer.search(
                query=f"email:'{safe}'", limit=20,
            )
            customers_data = list(found.data or [])
        except Exception:
            listed = stripe.Customer.list(limit=100)
            customers_data = [
                c for c in list(listed.data or [])
                if (getattr(c, "email", None) or "").strip().lower() == email_norm
            ]
        for cust in customers_data:
            cust_id = _stripe_id(cust) or getattr(cust, "id", None)
            if not cust_id:
                continue
            for status in ("active", "trialing", "past_due"):
                page = stripe.Subscription.list(
                    customer=cust_id, status=status, limit=20,
                )
                for sub in list(page.data or []):
                    sub_d = _as_dict(sub)
                    items = (sub_d.get("items") or {}).get("data") or []
                    matched = False
                    for item in items:
                        item_d = _as_dict(item)
                        pid = _stripe_id(item_d.get("price"))
                        if pid and pid in prices:
                            matched = True
                            break
                    if matched:
                        sid = _stripe_id(sub_d.get("id"))
                        if sid:
                            sub_ids.add(sid)
    except Exception:
        log.exception("stripe: list membership subscriptions failed for %s", email_norm)
    return sub_ids


def apply_cancel_at_to_user(user, period_end: int | None) -> None:
    """Persist scheduled access-end on the user (caller commits)."""
    if user is None:
        return
    from datetime import datetime, timedelta, timezone

    from ..models import utcnow

    if period_end:
        try:
            user.membership_cancel_at = datetime.fromtimestamp(
                int(period_end), tz=timezone.utc,
            ).replace(tzinfo=None)
            return
        except (TypeError, ValueError, OSError):
            pass
    # Fallback: mark canceling with a far-enough placeholder so UI flips;
    # Stripe webhook still revokes at the real period end.
    if user.membership_cancel_at is None:
        user.membership_cancel_at = utcnow() + timedelta(days=31)


def clear_cancel_at_for_email(email: str) -> None:
    """Clear membership_cancel_at after resume or subscription end."""
    from sqlalchemy import func

    from ..models import User

    email_norm = (email or "").strip().lower()
    if not email_norm:
        return
    user = (
        User.query
        .filter(func.lower(User.email) == email_norm, User.deleted_at.is_(None))
        .first()
    )
    if user is not None and user.membership_cancel_at is not None:
        user.membership_cancel_at = None


def _end_paid_membership_orders(
    email_norm: str,
    price_ids: set[str] | None = None,
    *,
    only_subscription_id: str | None = None,
    except_order_id: str | None = None,
) -> int:
    """End paid membership Orders and reconcile the member's tier.

    ``only_subscription_id``: only end orders that resolve to that Stripe sub
    (used when a subscription is deleted so a plan-switch cannot revoke the
    new membership that shares the same price).
    ``except_order_id``: leave this order paid (plan-switch keep).
    """
    from sqlalchemy import func

    from .memberships import reconcile_email

    prices = price_ids or _membership_price_ids()
    if not email_norm or not prices:
        return 0
    keep = str(except_order_id or "").strip()
    only_sub = str(only_subscription_id or "").strip()
    paid = (
        Order.query
        .filter(
            func.lower(Order.buyer_email) == email_norm,
            Order.status == "paid",
            Order.ls_variant_id.in_(list(prices)),
        )
        .all()
    )
    ended = 0
    for order in paid:
        oid = str(order.ls_order_id or "").strip()
        if keep and oid == keep:
            continue
        if only_sub and _order_subscription_id(order) != only_sub:
            continue
        order.status = "ended"
        ended += 1
    if ended:
        reconcile_email(email_norm, downgrade=True)
    return ended


def _email_from_subscription(sub: dict) -> str:
    """Best-effort buyer email from a subscription object."""
    email = (sub.get("customer_email") or "").strip().lower()
    if email and "@" in email:
        return email
    cust = sub.get("customer")
    if isinstance(cust, str) and cust.startswith("cus_") and configured():
        try:
            _configure_stripe()
            c = stripe.Customer.retrieve(cust)
            email = (getattr(c, "email", None) or "").strip().lower()
            if email and "@" in email:
                return email
        except Exception:
            log.exception("stripe: could not load customer %s for subscription end", cust)
    elif isinstance(cust, dict):
        email = (cust.get("email") or "").strip().lower()
        if email and "@" in email:
            return email
    return ""


def _user_for_email(email: str):
    from sqlalchemy import func

    from ..models import User

    email_norm = (email or "").strip().lower()
    if not email_norm:
        return None
    return (User.query
            .filter(func.lower(User.email) == email_norm,
                    User.deleted_at.is_(None))
            .first())


def _subscription_is_membership(sub: dict) -> bool:
    """True when any line on this subscription is one of our membership prices."""
    prices = _membership_price_ids()
    products = _membership_product_ids()
    if not prices and not products:
        return False
    for item in ((sub.get("items") or {}).get("data") or []):
        item_d = _as_dict(item)
        pid = _stripe_id(item_d.get("price"))
        if pid and pid in prices:
            return True
        price_obj = item_d.get("price")
        if isinstance(price_obj, dict):
            prod_id = _stripe_id(price_obj.get("product"))
            if prod_id and prod_id in products:
                return True
    return False


def _cancel_end_from_subscription(sub: dict) -> int | None:
    """Unix time this subscription's access ends, when a cancel is scheduled."""
    if not (sub.get("cancel_at_period_end") or sub.get("cancel_at")):
        return None
    raw = sub.get("cancel_at") or sub.get("current_period_end")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


#: Subscription states that mean the money has stopped and is not coming back
#: on its own. ``past_due`` is deliberately absent — Stripe is still retrying.
DEAD_SUBSCRIPTION_STATUSES = ("canceled", "unpaid", "incomplete_expired", "paused")


def apply_subscription_cancel_state(sub: dict) -> dict:
    """Record (or clear) a scheduled cancel from one subscription. Caller commits.

    Stripe only tells us "this ends on the 14th" through
    ``customer.subscription.updated``. Without this, a member who cancels from
    Stripe's billing portal — or whom you cancel from the Stripe dashboard —
    keeps looking like a full-price member in Studio until the subscription
    finally deletes itself weeks later.
    """
    sub = sub if isinstance(sub, dict) else _as_dict(sub)
    out = {"email": "", "canceling": False, "changed": False, "ended": 0}
    if not _subscription_is_membership(sub):
        return out

    status = (sub.get("status") or "").strip().lower()
    email = _email_from_subscription(sub)
    out["email"] = email
    user = _user_for_email(email)
    if user is None:
        return out

    if status in DEAD_SUBSCRIPTION_STATUSES:
        # Dunning has given up, or billing is paused. Stripe may never delete
        # the subscription, so nothing else is coming to take the access back:
        # left alone this is a membership that stopped being paid for and
        # nobody noticed.
        sub_id = _stripe_id(sub.get("id")) or ""
        ended = _end_paid_membership_orders(
            email, only_subscription_id=sub_id or None)
        if user.membership_cancel_at is not None:
            user.membership_cancel_at = None
        out["ended"] = ended
        out["changed"] = True
        log.info("stripe: subscription %s is %s — ended %s membership order(s) "
                 "for %s", sub_id or "?", status, ended, email)
        return out

    ends_unix = _cancel_end_from_subscription(sub)
    scheduled = bool(ends_unix) and status in ("active", "trialing", "past_due")
    out["canceling"] = scheduled

    if scheduled:
        before = user.membership_cancel_at
        apply_cancel_at_to_user(user, ends_unix)
        out["changed"] = user.membership_cancel_at != before
    elif status in ("active", "trialing") and user.membership_cancel_at is not None:
        # They resumed — Stripe is billing them again.
        user.membership_cancel_at = None
        out["changed"] = True
    return out


def sweep_cancel_flags(*, max_pages: int = 10) -> dict:
    """Re-read every live membership subscription and fix the cancel flags.

    Backfills anyone who cancelled before the site started listening for it,
    and self-heals any webhook we missed. One listing call per status rather
    than one lookup per member.
    """
    out = {"checked": 0, "canceling": 0, "changed": 0, "ok": True}
    if not configured() or current_app.config.get("TESTING"):
        out["ok"] = False
        return out

    seen_canceling: set[str] = set()
    seen_live: set[str] = set()
    try:
        _configure_stripe()
        for status in ("active", "trialing", "past_due"):
            starting_after = None
            for _ in range(max_pages):
                page = stripe.Subscription.list(
                    status=status, limit=100,
                    expand=["data.customer"],
                    **({"starting_after": starting_after} if starting_after else {}),
                )
                rows = list(page.data or [])
                for sub in rows:
                    sub_d = _as_dict(sub)
                    res = apply_subscription_cancel_state(sub_d)
                    if not res["email"]:
                        continue
                    out["checked"] += 1
                    seen_live.add(res["email"])
                    if res["canceling"]:
                        seen_canceling.add(res["email"])
                        out["canceling"] += 1
                    if res["changed"]:
                        out["changed"] += 1
                if not rows or not getattr(page, "has_more", False):
                    break
                starting_after = _stripe_id(_as_dict(rows[-1]).get("id"))
                if not starting_after:
                    break
    except Exception:
        log.exception("stripe: cancel-flag sweep failed")
        db.session.rollback()
        out["ok"] = False
        return out

    # Anyone flagged locally whose subscription is live and no longer ending.
    stale = seen_live - seen_canceling
    if stale:
        from sqlalchemy import func

        from ..models import User
        rows = (User.query
                .filter(User.membership_cancel_at.isnot(None),
                        User.deleted_at.is_(None),
                        func.lower(User.email).in_(stale))
                .all())
        for user in rows:
            user.membership_cancel_at = None
            out["changed"] += 1

    db.session.commit()
    log.info("stripe: cancel sweep checked=%s canceling=%s changed=%s",
             out["checked"], out["canceling"], out["changed"])
    return out


def handle_subscription_deleted(sub: dict) -> dict:
    """When a membership subscription actually ends, revoke local access."""
    sub = sub if isinstance(sub, dict) else _as_dict(sub)
    email = _email_from_subscription(sub)
    sub_id = _stripe_id(sub.get("id")) or ""
    membership_prices = _membership_price_ids()
    item_prices: set[str] = set()
    items = (sub.get("items") or {}).get("data") or []
    for item in items:
        item_d = _as_dict(item)
        pid = _stripe_id(item_d.get("price"))
        if pid:
            item_prices.add(pid)
    matched = item_prices & membership_prices if item_prices else set()

    result = {"ok": True, "email": email, "orders_ended": 0, "membership": False}
    if item_prices and not matched:
        # Non-membership subscription ended — leave membership Orders alone.
        log.info(
            "stripe: subscription.deleted %s ignored (not a membership price)",
            sub_id or _stripe_id(sub.get("id")),
        )
        return result
    if not matched and not email:
        log.info(
            "stripe: subscription.deleted %s ignored (no email/membership price)",
            sub_id,
        )
        return result

    if not email:
        log.warning(
            "stripe: subscription.deleted %s has membership prices but no email",
            sub_id,
        )
        result["ok"] = False
        return result

    result["membership"] = True
    # Only end orders tied to this exact subscription so a plan switch (new
    # sub paid, old sub deleted) cannot revoke the new membership by price.
    if sub_id:
        ended = _end_paid_membership_orders(
            email, matched or membership_prices,
            only_subscription_id=sub_id,
        )
    else:
        ended = _end_paid_membership_orders(email, matched or membership_prices)
    result["orders_ended"] = ended
    clear_cancel_at_for_email(email)
    log.info(
        "stripe: subscription.deleted ended %s order(s) for %s (sub=%s)",
        ended, email, sub_id or "-",
    )
    return result


def _order_behind_charge(dispute: dict) -> Order | None:
    """The order a disputed charge paid for, by payment intent then charge id."""
    for key in (_stripe_id(dispute.get("payment_intent")),
                _stripe_id(dispute.get("charge"))):
        if key:
            row = Order.query.filter_by(ls_order_id=str(key)).first()
            if row is not None:
                return row
    # Subscription renewals are stored under the invoice, not the charge.
    pi = _stripe_id(dispute.get("payment_intent"))
    if pi and configured() and not current_app.config.get("TESTING"):
        sub_id = _subscription_id_from_payment_ref(pi)
        if sub_id:
            return (Order.query
                    .filter(Order.stripe_subscription_id == sub_id,
                            Order.status == "paid")
                    .order_by(Order.id.desc())
                    .first())
    return None


def handle_dispute(dispute: dict, *, opened: bool) -> dict:
    """A chargeback. Take the access back while the money is being pulled.

    Without this someone can subscribe, dispute the charge, and keep the
    membership: the payment never refunds, so nothing else revokes it. Winning
    the dispute puts the access back.
    """
    d = dispute if isinstance(dispute, dict) else _as_dict(dispute)
    from .memberships import reconcile_email

    status = (d.get("status") or "").strip().lower()
    out = {"ok": True, "order_id": None, "changed": False, "status": status}
    order = _order_behind_charge(d)
    email = (getattr(order, "buyer_email", None) or "").strip().lower()
    if order is not None:
        out["order_id"] = order.ls_order_id

    if opened:
        if order is not None and order.status == "paid":
            order.status = "refunded"
            out["changed"] = True
            if email:
                reconcile_email(email, downgrade=True)
        body = (
            f"A payment was disputed with Stripe ({d.get('reason') or 'no reason given'}).\n\n"
            + (f"Order {order.ls_order_id} for {email or 'an unknown buyer'} — "
               "access has been taken back while it is decided.\n"
               if order is not None
               else "We could not match it to an order here, so nothing was "
                    "changed on the site. Check Stripe.\n")
            + "\nRespond in Stripe before their deadline or it is lost by default."
        )
        title = "Payment disputed"
    else:
        won = status == "won"
        if won and order is not None and order.status == "refunded":
            order.status = "paid"
            out["changed"] = True
            if email:
                reconcile_email(email)
        title = "Dispute won" if won else "Dispute lost"
        body = (
            f"Stripe closed a dispute as {status or 'closed'}.\n\n"
            + (f"Order {order.ls_order_id} for {email or 'an unknown buyer'}: "
               + ("access has been put back." if won else "access stays revoked.")
               if order is not None else "No matching order here.")
        )

    log.warning("stripe: dispute %s (%s) order=%s changed=%s",
                "opened" if opened else "closed", status or "?",
                out["order_id"] or "-", out["changed"])
    try:
        from .mailer import send_billing_alert
        send_billing_alert(title, body)
    except Exception:
        log.exception("stripe: could not alert owner about a dispute")
    return out


def handle_payment_action_required(invoice: dict) -> dict:
    """A renewal is waiting on the cardholder to authenticate (3-D Secure).

    Neither paid nor failed, so on its own it is silence: the member keeps
    access, Stripe chases them, and nobody here knows why the money stopped.
    """
    inv = invoice if isinstance(invoice, dict) else _as_dict(invoice)
    email = (inv.get("customer_email") or "").strip().lower() or _buyer_email(inv)
    sub_id = invoice_subscription_id(inv)
    out = {"email": email, "subscription": sub_id}
    log.warning(
        "stripe: invoice %s needs the cardholder to authenticate "
        "(subscription %s, %s)", _stripe_id(inv.get("id")) or "?",
        sub_id or "-", email or "unknown",
    )
    try:
        from .mailer import send_billing_alert
        send_billing_alert(
            "A renewal is waiting on the member's bank",
            f"{email or 'A member'} has a renewal Stripe can't take yet — their "
            "bank wants them to confirm it.\n\n"
            f"Subscription: {sub_id or 'unknown'}\n\n"
            "Stripe emails them about it. If it stays unconfirmed the "
            "subscription eventually goes unpaid and the site drops their tier.",
        )
    except Exception:
        log.exception("stripe: could not alert owner about a pending authentication")
    return out


def handle_customer_updated(event: dict) -> dict:
    """Watch for a member changing the email Stripe bills them under.

    Everything here is matched to an account by email address, so a member who
    edits theirs in Stripe's billing portal quietly detaches their own
    membership: the next renewal arrives under an address no account holds.

    Deliberately does not move the account: anyone able to edit a Stripe
    customer could otherwise point it at someone else's membership.
    """
    ev = event if isinstance(event, dict) else _as_dict(event)
    obj = _as_dict((ev.get("data") or {}).get("object") or {})
    previous = _as_dict((ev.get("data") or {}).get("previous_attributes") or {})
    out = {"changed": False, "old": "", "new": ""}
    if "email" not in previous:
        return out
    old = (previous.get("email") or "").strip().lower()
    new = (obj.get("email") or "").strip().lower()
    if not old or old == new:
        return out
    out.update({"old": old, "new": new})
    if _user_for_email(new) is not None:
        return out  # the new address already has an account; nothing detaches
    if _user_for_email(old) is None:
        return out  # nobody here was using the old one either
    out["changed"] = True
    log.warning(
        "stripe: customer %s changed billing email %s → %s, which no account "
        "here holds", _stripe_id(obj.get("id")) or "?", old, new,
    )
    try:
        from .mailer import send_billing_alert
        send_billing_alert(
            "A member changed their billing email",
            f"{old} changed the email on their Stripe customer to {new}.\n\n"
            "Their Bloom Anyway account is still under the old address, and "
            "memberships are matched by email — so their next renewal will not "
            "reach their account.\n\n"
            "Either change it back in Stripe, or change their account email in "
            "Studio to match.",
        )
    except Exception:
        log.exception("stripe: could not alert owner about a billing email change")
    return out


def format_access_end_date(period_end: int | None) -> str:
    """Unix timestamp → human date for cancel emails / flashes."""
    from datetime import datetime, timezone

    if not period_end:
        return ""
    try:
        dt = datetime.fromtimestamp(int(period_end), tz=timezone.utc)
        return dt.strftime("%b %d, %Y")
    except (TypeError, ValueError, OSError):
        return ""
