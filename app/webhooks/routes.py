"""Stripe webhook receiver.

Verifies Stripe-Signature, maps events to fulfillment, idempotent on payment id.
"""
import logging

from flask import request

from ..extensions import db
from ..services import stripe_pay as pay
from . import bp

log = logging.getLogger(__name__)

HANDLED = {
    # Primary fulfillment path — fires for $0 / 100% off checkouts too
    # (payment_status paid or no_payment_required; no PaymentIntent required).
    "checkout.session.completed",
    "checkout.session.async_payment_succeeded",
    "invoice.paid",
    "invoice.payment_failed",
    "charge.refunded",
    "charge.refund.updated",
    "customer.subscription.deleted",
    # Cancel-at-period-end is only ever announced here — without it a member who
    # cancels in Stripe's portal still reads as a paying member in Studio. It
    # also carries the states where dunning gives up (unpaid / paused), which
    # never produce a delete and so are the only warning we get.
    "customer.subscription.updated",
    # A chargeback never refunds, so nothing else takes the access back.
    "charge.dispute.created",
    "charge.dispute.closed",
    # Neither paid nor failed: the bank wants the cardholder to confirm.
    "invoice.payment_action_required",
    # Memberships are matched by email, so a member editing theirs in Stripe's
    # billing portal silently detaches their own subscription.
    "customer.updated",
}


@bp.route("/stripe", methods=["POST"])
def stripe_webhook():
    raw = request.get_data()
    headers = {k.lower(): v for k, v in request.headers.items()}
    try:
        event = pay.construct_event(raw, headers)
    except pay.StripeError as exc:
        log.warning(
            "stripe webhook: signature rejected (ip=%s detail=%s) — "
            "confirm STRIPE_WEBHOOK_SECRET matches the www endpoint signing secret",
            request.remote_addr, exc,
        )
        return {
            "error": "invalid signature",
            "hint": "STRIPE_WEBHOOK_SECRET must match this endpoint's whsec_ in Stripe",
        }, 401
    except Exception:
        log.exception("stripe webhook: could not parse event")
        return {"error": "invalid payload"}, 400

    event_type = (event.get("type") or "").strip()
    if event_type not in HANDLED:
        return {"status": "ignored", "event": event_type}, 200

    obj = (event.get("data") or {}).get("object") or {}
    if not isinstance(obj, dict):
        return {"error": "invalid payload"}, 400

    # Period-end cancel: revoke local membership when Stripe actually deletes the sub.
    if event_type == "customer.subscription.deleted":
        try:
            result = pay.handle_subscription_deleted(obj)
            db.session.commit()
            log.info(
                "stripe webhook: subscription.deleted email=%s orders_ended=%s",
                result.get("email"), result.get("orders_ended"),
            )
            return {"status": "ok", "event": event_type}, 200
        except Exception:
            db.session.rollback()
            log.exception("stripe webhook: failed to process %s", event_type)
            return {"error": "processing failed"}, 500

    # Scheduled cancel, a resume, or dunning giving up.
    if event_type == "customer.subscription.updated":
        try:
            result = pay.apply_subscription_cancel_state(obj)
            db.session.commit()
            log.info(
                "stripe webhook: subscription.updated email=%s canceling=%s "
                "ended=%s changed=%s",
                result.get("email"), result.get("canceling"),
                result.get("ended"), result.get("changed"),
            )
            return {"status": "ok", "event": event_type}, 200
        except Exception:
            db.session.rollback()
            log.exception("stripe webhook: failed to process %s", event_type)
            return {"error": "processing failed"}, 500

    # Chargebacks, stalled authentication, and billing-email changes: each one
    # is a way the money can stop without any payment event ever arriving.
    if event_type in ("charge.dispute.created", "charge.dispute.closed",
                      "invoice.payment_action_required", "customer.updated"):
        try:
            if event_type == "customer.updated":
                result = pay.handle_customer_updated(event)
            elif event_type == "invoice.payment_action_required":
                result = pay.handle_payment_action_required(obj)
            else:
                result = pay.handle_dispute(
                    obj, opened=event_type.endswith("created"))
            db.session.commit()
            log.info("stripe webhook: %s → %s", event_type, result)
            return {"status": "ok", "event": event_type}, 200
        except Exception:
            db.session.rollback()
            log.exception("stripe webhook: failed to process %s", event_type)
            return {"error": "processing failed"}, 500

    internal, data = pay.stripe_event_to_internal(event_type, obj)
    if not internal:
        log.info(
            "stripe webhook: ignored %s (id=%s payment_status=%s amount=%s)",
            event_type,
            (obj.get("id") if isinstance(obj, dict) else None),
            (obj.get("payment_status") if isinstance(obj, dict) else None),
            (obj.get("amount_total") if isinstance(obj, dict) else None),
        )
        return {"status": "ignored", "event": event_type}, 200

    try:
        pay.handle_payment_event(internal, data)
        db.session.commit()
        log.info(
            "stripe webhook: %s → %s (payment %s email=%s price=%s amount=%s)",
            event_type, internal,
            data.get("payment_id") or data.get("id"),
            data.get("customer_email") or (data.get("customer") or {}).get("email"),
            (data.get("product_cart") or [{}])[0].get("product_id")
            if data.get("product_cart") else None,
            data.get("total_amount"),
        )
        return {"status": "ok"}, 200
    except Exception:
        db.session.rollback()
        log.exception("stripe webhook: failed to process %s", event_type)
        return {"error": "processing failed"}, 500


# Retired providers — fail loudly so old dashboard URLs are noticed.
@bp.route("/dodo", methods=["POST"])
@bp.route("/dodopayments", methods=["POST"])
def dodo_retired():
    return {
        "error": "Dodo Payments webhooks are retired. Use /webhooks/stripe.",
    }, 410


@bp.route("/lemonsqueezy", methods=["POST"])
def lemonsqueezy_retired():
    return {
        "error": "Lemon Squeezy webhooks are retired. Use /webhooks/stripe.",
    }, 410
