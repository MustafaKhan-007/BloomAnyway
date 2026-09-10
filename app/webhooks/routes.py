"""Stripe webhook receiver.

Verifies Stripe-Signature, maps events to fulfillment, idempotent on payment id.
"""
import logging

from flask import request

from ..extensions import db
from ..services import stripe_pay as pay
from . import bp

log = logging.getLogger(__name__)

#: Stripe namespaces that carry, or take away, money. Anything here that we
#: don't handle is worth saying out loud rather than answering 200 in silence.
MONEY_PREFIXES = ("payment_intent.", "charge.", "checkout.session.",
                  "invoice.", "customer.subscription.")


def _is_about_money(event_type: str) -> bool:
    return any((event_type or "").startswith(p) for p in MONEY_PREFIXES)


def _remember(**pairs) -> None:
    """Write down what Stripe just did, for the Studio dashboard to show.

    Never at the cost of the event itself: a settings table that will not
    write is not a reason to drop a sale on the floor.
    """
    from ..services.settings import set_setting

    try:
        for key, value in pairs.items():
            set_setting(key, value)
    except Exception:
        db.session.rollback()
        log.exception("stripe webhook: could not record %s", sorted(pairs))


def _note_arrival(event_type: str, *, handled: bool) -> None:
    from ..models import utcnow

    fields = {
        "stripe_last_webhook_at": utcnow().isoformat(timespec="seconds"),
        "stripe_last_webhook_type": event_type or "unknown",
    }
    if handled:
        # Something came through that this site reads, so the endpoint is
        # being heard and whatever it sent before that we couldn't is no
        # longer what stands between a sale and the dashboard.
        fields["stripe_ignored_event_at"] = ""
        fields["stripe_ignored_event_type"] = ""
    _remember(**fields)


def _note_ignored(event_type: str) -> None:
    from ..models import utcnow

    _remember(stripe_ignored_event_at=utcnow().isoformat(timespec="seconds"),
              stripe_ignored_event_type=event_type or "unknown")

HANDLED = {
    # Primary fulfillment path — fires for $0 / 100% off checkouts too
    # (payment_status paid or no_payment_required; no PaymentIntent required).
    "checkout.session.completed",
    "checkout.session.async_payment_succeeded",
    # The same purchase, announced the other two ways Stripe announces it.
    # Which of the three an endpoint sends is a checkbox in the Stripe
    # dashboard, and an endpoint set up on the payment events rather than the
    # checkout one used to mean every sale was visible in Stripe and invisible
    # here. All three key on the PaymentIntent, so they land on one order.
    "payment_intent.succeeded",
    "charge.succeeded",
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
    _note_arrival(event_type, handled=event_type in HANDLED)
    if event_type not in HANDLED:
        if _is_about_money(event_type):
            # Loudly, and where the owner will see it. An endpoint pointed at
            # events nothing here reads is the one failure that looks like
            # nothing at all: Stripe shows a row of 200s, the site shows no
            # sale, and neither of them mentions the other.
            log.warning(
                "stripe webhook: %s carries money and nothing here reads it — "
                "add it to the endpoint's events in Stripe, or tell us to "
                "handle it", event_type,
            )
            _note_ignored(event_type)
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
