"""Mailer with two transports.

1. Brevo HTTP API (``BREVO_API_KEY``) — preferred on hosts that block outbound
   SMTP ports (e.g. Render free tier). Templates/customization live in Brevo.
2. Plain SMTP (``SMTP_HOST`` etc.) — any relay, only when Brevo is unset.

When neither is configured (local dev) emails are printed to the console so the
auth flows are testable without a mail account.
"""
import logging
import os
import re
import smtplib
from email.message import EmailMessage
from html import escape

import requests
from flask import current_app

log = logging.getLogger(__name__)

BREVO_SEND_URL = "https://api.brevo.com/v3/smtp/email"

# Most recent send failure (human-readable). Cleared on success.
_last_error = ""


def last_send_error() -> str:
    return _last_error


def _set_error(message: str) -> None:
    global _last_error
    _last_error = (message or "").strip()


def _strip_env_quotes(value: str) -> str:
    """Render/dashboard pastes often wrap secrets in quotes — strip them."""
    v = (value or "").strip().lstrip("\ufeff")
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1].strip()
    return v


def _brevo_api_key() -> str:
    """Normalize the Brevo API key (env first, then app config)."""
    raw = os.environ.get("BREVO_API_KEY")
    if raw is None or not str(raw).strip():
        raw = current_app.config.get("BREVO_API_KEY") or ""
    key = _strip_env_quotes(str(raw))
    key = re.sub(r"\s+", "", key)
    lower = key.lower()
    if lower.startswith("bearer"):
        key = key[6:].lstrip(":").strip()
        lower = key.lower()
    for prefix in ("api-key:", "apikey:", "x-api-key:"):
        if lower.startswith(prefix):
            key = key[len(prefix):].strip()
            break
    return key


def _mail_from() -> str:
    raw = os.environ.get("MAIL_FROM")
    if raw is None or not str(raw).strip():
        raw = current_app.config.get("MAIL_FROM") or ""
    return _strip_env_quotes(str(raw))


#: Verified Brevo senders an owner may write as, keyed for form values, each
#: with the Brevo template a reply from that address goes out on. Automated
#: mail (welcomes, receipts, session notices) ignores all of this and keeps
#: using MAIL_FROM — only mail a person composes gets to pick a face.
SENDERS: tuple[tuple[str, str, str, str], ...] = (
    ("support", "Customer Support", "bloomsupport@bloomanyway.online",
     "BREVO_TEMPLATE_CUSTOMER_SUPPORT"),
    ("ayesha", "Ayesha", "ayesha@bloomanyway.online",
     "BREVO_TEMPLATE_REPLY_HEALING"),
    ("saman", "Saman", "saman@bloomanyway.online",
     "BREVO_TEMPLATE_REPLY_CREATOR"),
    # No template of its own — the plain house style is the point here.
    ("noreply", "Bloom Anyway", "noreply@bloomanyway.online",
     "BREVO_TEMPLATE_CUSTOMER_SUPPORT"),
)
DEFAULT_SENDER_KEY = "support"
_REPLY_TEMPLATE_DEFAULTS = {
    "BREVO_TEMPLATE_CUSTOMER_SUPPORT": 20,
    "BREVO_TEMPLATE_REPLY_CREATOR": 21,
    "BREVO_TEMPLATE_REPLY_HEALING": 22,
}


def _sender_row(key: str | None) -> tuple[str, str, str, str] | None:
    wanted = (key or "").strip().lower()
    for row in SENDERS:
        if row[0] == wanted:
            return row
    return None


def reply_template_for(key: str | None) -> int | None:
    """The Brevo template a reply from this address should use.

    None means no template is configured, and the caller should fall back to
    a plain text send rather than post an empty design.
    """
    row = _sender_row(key) or _sender_row(DEFAULT_SENDER_KEY)
    if row is None:
        return None
    return _int_config(row[3], _REPLY_TEMPLATE_DEFAULTS[row[3]]) or None


def sender_choices() -> list[dict]:
    """The sender list for a Studio dropdown."""
    return [{"key": key, "name": name, "email": email,
             "label": f"{name} <{email}>",
             "template": _int_config(cfg, _REPLY_TEMPLATE_DEFAULTS[cfg])}
            for key, name, email, cfg in SENDERS]


def sender_from(key: str | None) -> str | None:
    """``Name <email>`` for a known sender key, else None.

    Returning None rather than guessing keeps an unrecognised key from
    silently sending as somebody else.
    """
    row = _sender_row(key)
    return f"{row[1]} <{row[2]}>" if row else None


def _parse_mail_from(raw: str) -> tuple[str, str]:
    """Return (display_name, email) from ``Name <email>`` or bare email."""
    value = (raw or "").strip()
    match = re.match(r"^(.*?)\s*<([^>]+)>\s*$", value)
    if match:
        name = match.group(1).strip().strip('"').strip("'")
        email = match.group(2).strip()
        return name or "Bloom Anyway", email
    return "Bloom Anyway", value


def _brevo_error_hint(status: int, body: str) -> str:
    """Turn a Brevo HTTP failure into a short owner-facing hint."""
    text = (body or "").lower()
    if status == 401 or "unauthorized" in text or "key not found" in text \
            or "invalid" in text and "key" in text:
        return (
            "Brevo rejected the API key (401). Set BREVO_API_KEY to a key "
            "from Brevo → SMTP & API → API Keys."
        )
    if status == 403:
        return (
            "Brevo forbade the send (403). Check your Brevo plan/limits and "
            "that the sender for MAIL_FROM is verified."
        )
    if status == 400 or "sender" in text or "from" in text:
        return (
            "Brevo rejected the sender or payload. MAIL_FROM must use a "
            "verified sender/domain in Brevo. "
            f"Details: {(body or '')[:220]}"
        )
    if status == 429:
        return "Brevo rate limit hit — wait a minute and try again."
    return f"Brevo error {status}: {(body or '')[:240]}"


def _as_brevo_attachments(attachments) -> list[dict]:
    """Files in the shape Brevo wants: a name and the bytes, base64'd."""
    import base64

    out = []
    for item in attachments or []:
        data = item.get("data")
        if not data:
            continue
        out.append({
            "name": (item.get("name") or "attachment").strip() or "attachment",
            "content": base64.b64encode(data).decode("ascii"),
        })
    return out


def _send_via_brevo(to: str, subject: str, text_body: str,
                    html_body: str | None = None,
                    template_id: int | None = None,
                    params: dict | None = None,
                    sender: str | None = None,
                    attachments=None) -> bool:
    """Send through Brevo's transactional HTTP API."""
    key = _brevo_api_key()
    if not key:
        _set_error("BREVO_API_KEY is empty on the server. Set it in Render and redeploy.")
        return False

    mail_from = (sender or "").strip() or _mail_from()
    name, email = _parse_mail_from(mail_from)
    if not email or "@" not in email or email.lower().endswith("@localhost"):
        log.error("Brevo: sender is missing a real email address (got %r).", mail_from)
        _set_error(
            "The From address must be a real Brevo-verified sender, "
            "e.g. Bloom Anyway <hello@yourdomain.com>."
        )
        return False

    payload = {
        "sender": {"name": name, "email": email},
        "to": [{"email": to}],
    }
    if template_id:
        payload["templateId"] = int(template_id)
        if params:
            payload["params"] = params
        # Subject in the Brevo template wins unless we override
        if subject:
            payload["subject"] = subject
    else:
        if not html_body:
            html_body = (
                "<pre style=\"font-family:ui-monospace,monospace;white-space:pre-wrap;"
                "font-size:15px;line-height:1.5;\">"
                f"{escape(text_body)}</pre>"
            )
        payload["subject"] = subject
        payload["textContent"] = text_body
        payload["htmlContent"] = html_body
    files = _as_brevo_attachments(attachments)
    if files:
        payload["attachment"] = files
    try:
        resp = requests.post(
            BREVO_SEND_URL,
            json=payload,
            headers={
                "api-key": key,
                "accept": "application/json",
                "content-type": "application/json",
            },
            timeout=20,
        )
        if resp.status_code in (200, 201, 202):
            log.info("Brevo: sent to %s (status %s)", to, resp.status_code)
            _set_error("")
            return True
        hint = _brevo_error_hint(resp.status_code, resp.text)
        log.error("Brevo rejected email to %s: %s %s", to, resp.status_code, resp.text)
        _set_error(hint)
        return False
    except Exception as exc:
        log.exception("Failed to reach Brevo API for email to %s", to)
        _set_error(f"Could not reach Brevo ({exc.__class__.__name__}).")
        return False


def _send_via_smtp(to: str, msg: EmailMessage) -> bool:
    cfg = current_app.config
    try:
        if int(cfg["SMTP_PORT"]) == 465:
            server = smtplib.SMTP_SSL(cfg["SMTP_HOST"], cfg["SMTP_PORT"], timeout=15)
        else:
            server = smtplib.SMTP(cfg["SMTP_HOST"], cfg["SMTP_PORT"], timeout=15)
            server.starttls()
        with server:
            if cfg["SMTP_USER"]:
                server.login(cfg["SMTP_USER"], cfg["SMTP_PASSWORD"])
            server.send_message(msg)
        _set_error("")
        return True
    except Exception as exc:
        log.exception("Failed to send email to %s via SMTP", to)
        _set_error(f"SMTP send failed ({exc.__class__.__name__}).")
        return False


def send_email(to: str, subject: str, text_body: str, html_body: str | None = None,
               template_id: int | None = None, params: dict | None = None,
               sender: str | None = None, attachments=None) -> bool:
    """Send email. Prefer Brevo; fall back to SMTP; else console in local dev.

    ``sender`` overrides MAIL_FROM for this one send and must already be a
    verified Brevo sender — see :func:`sender_from`.

    ``attachments`` is a list of ``{"name": ..., "data": bytes}``.
    """
    cfg = current_app.config
    to = (to or "").strip()
    if not to:
        _set_error("Missing recipient email.")
        return False
    # Studio's stand-in accounts carry an address that can't receive anything.
    # Catching it here covers every caller rather than each one remembering.
    from .demo_accounts import is_demo_address

    if is_demo_address(to):
        log.debug("Skipping email to stand-in account %s", to)
        _set_error("")
        return False

    if _brevo_api_key():
        return _send_via_brevo(
            to, subject, text_body,
            html_body=html_body,
            template_id=template_id,
            params=params,
            sender=sender,
            attachments=attachments,
        )

    if not cfg["SMTP_HOST"]:
        log.warning("No email transport configured; printing email to console.")
        print("\n===== EMAIL (console fallback) =====")
        print(f"From: {sender or _mail_from()}\nTo: {to}\nSubject: {subject}\n\n{text_body}")
        if template_id:
            print(f"(Brevo template #{template_id} params={params!r})")
        for item in attachments or []:
            print(f"(attached {item.get('name')}, "
                  f"{len(item.get('data') or b'')} bytes)")
        print("====================================\n")
        _set_error("")
        return True

    msg = EmailMessage()
    msg["Subject"] = subject
    # Most SMTP relays refuse to send as an arbitrary address, so the fallback
    # transport stays on MAIL_FROM and puts the chosen face in Reply-To.
    msg["From"] = _mail_from()
    if sender and sender.strip() != _mail_from():
        msg["Reply-To"] = sender
    msg["To"] = to
    msg.set_content(text_body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    for item in attachments or []:
        data = item.get("data")
        if not data:
            continue
        msg.add_attachment(
            data, maintype="application", subtype="pdf",
            filename=(item.get("name") or "attachment.pdf"),
        )
    return _send_via_smtp(to, msg)


def _int_config(key: str, default: int = 0) -> int:
    raw = current_app.config.get(key, default)
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


def _public_href(path: str = "/") -> str:
    """Absolute URL for email CTAs."""
    path = path if (path or "").startswith("/") else f"/{path or ''}"
    try:
        from flask import has_request_context, request
        if has_request_context() and request.url_root:
            return request.url_root.rstrip("/") + path
    except Exception:
        pass
    try:
        from flask import url_for, has_app_context
        if has_app_context():
            if path == "/" or path == "":
                return url_for("main.index", _external=True)
    except Exception:
        pass
    base = (current_app.config.get("PUBLIC_BASE_URL") or "https://www.bloomanyway.online").rstrip("/")
    return base + path


def send_styled_email(
    to: str,
    *,
    subject: str,
    preview: str,
    header: str,
    title: str,
    body: str,
    button_text: str,
    button_url: str,
    extra_params: dict | None = None,
) -> bool:
    """Send via Brevo general template (#10).

    Params: HEADER, TITLE, BODY, BUTTON_TEXT, BUTTON_URL.
    ``preview`` is kept for callers / plain-text fallback only.
    """
    _ = preview  # reserved for plain-text / future template fields
    text = (
        f"{title}\n\n{body}\n\n"
        f"{button_text}: {button_url}\n\n"
        "— Bloom Anyway"
    )
    template_id = _int_config("BREVO_TEMPLATE_GENERAL", 10) or None
    if not template_id:
        return send_email(to, subject, text)

    params = {
        "HEADER": header,
        "TITLE": title,
        "BODY": body,
        "BUTTON_TEXT": button_text,
        "BUTTON_URL": button_url,
    }
    if extra_params:
        params.update({k: v for k, v in extra_params.items() if v is not None})
    return send_email(
        to, subject, text, template_id=template_id, params=params,
    )


def send_customer_support_email(
    to: str,
    *,
    subject: str,
    preview: str,
    header: str,
    title: str,
    body: str,
    sender: str | None = None,
    sender_key: str | None = None,
    extra_params: dict | None = None,
) -> bool:
    """Send a reply an owner wrote, on the template that address uses.

    Customer support goes out on #20, Saman's creator address on #21 and
    Ayesha's healing address on #22. All three take the same parameters:
    SUBJECT, PREVIEW, HEADER, TITLE, BODY. No call-to-action button — an
    answer to someone's question is the point, not a nudge elsewhere.
    """
    text = f"{title}\n\n{body}\n\n— Bloom Anyway"
    template_id = reply_template_for(sender_key or DEFAULT_SENDER_KEY)
    if not template_id:
        return send_email(to, subject, text, sender=sender)

    params = {
        "SUBJECT": subject,
        "PREVIEW": preview,
        "HEADER": header,
        "TITLE": title,
        "BODY": body,
    }
    if extra_params:
        params.update({k: v for k, v in extra_params.items() if v is not None})
    return send_email(to, subject, text, template_id=template_id, params=params,
                      sender=sender)


def send_verification_code(to: str, code: str, purpose: str) -> bool:
    minutes = current_app.config["CODE_MAX_AGE_MINUTES"]
    if purpose == "reset":
        subject = "Your password reset code"
        intro = "Here's your code to reset your password:"
        # No dedicated reset template — use general (#10).
        return send_styled_email(
            to,
            subject=subject,
            preview=f"Your reset code: {code}",
            header="Bloom Anyway",
            title="Password reset code",
            body=(
                f"{intro}\n\n{code}\n\n"
                f"It expires in {minutes} minutes.\n"
                "If you didn't request it, you can safely ignore this email."
            ),
            button_text="Reset password",
            button_url=_public_href("/reset-password"),
        )

    subject = "Your confirmation code"
    intro = "Welcome. Here's your code to confirm your email:"
    text = (
        f"{intro}\n\n    {code}\n\n"
        f"It expires in {minutes} minutes.\n"
        "If you didn't request it, you can safely ignore this email.\n\n"
        "— Bloom Anyway"
    )

    template_id = _int_config("BREVO_TEMPLATE_CONFIRM", 3) or None
    if not template_id:
        return send_email(to, subject, text)

    # Brevo #3: {{ params.CODE }}
    return send_email(
        to, subject, text,
        template_id=template_id,
        params={"CODE": code},
    )


def send_welcome_email(to: str, *, first_name: str | None = None) -> bool:
    """Send the Brevo welcome template (#2) after the account is fully created/verified."""
    template_id = _int_config("BREVO_TEMPLATE_WELCOME", 2) or None
    name = (first_name or "").strip()
    text = (
        "Welcome to Bloom Anyway.\n\n"
        "Your account is ready — take a breath, look around, and bloom at your own pace.\n\n"
        "— Bloom Anyway"
    )
    if not template_id:
        return send_email(to, "Welcome to Bloom Anyway", text)

    # Brevo #2: {{ params.VORNAME }}
    return send_email(
        to,
        "Welcome to Bloom Anyway",
        text,
        template_id=template_id,
        params={"VORNAME": name},
    )


def send_order_receipt(
    to: str,
    *,
    order_id: str,
    product_name: str,
    amount: str,
    order_date: str,
    attachments=None,
    perk: str = "",
    description: str = "",
) -> bool:
    """Send Brevo order-receipt template (#4) after a successful product purchase.

    ``attachments`` are the readable files that come with it, so a guide lands
    in the buyer's inbox rather than only waiting in their library. ``perk`` is
    the free membership time the product carries, if any: it is added to the
    account there and then, and the receipt is where somebody would look.
    ``description`` is what the owner wrote for this product's own receipt —
    what they just bought, in their words, rather than only its name.
    """
    template_id = _int_config("BREVO_TEMPLATE_RECEIPT", 4) or None
    oid = str(order_id or "").strip()
    product = (product_name or "").strip() or "Purchase"
    paid = (amount or "").strip() or "—"
    when = (order_date or "").strip() or "—"
    attached = [a.get("name") for a in (attachments or []) if a.get("data")]
    came_with = (
        "\n\nAttached: " + ", ".join(n for n in attached if n)
        + "\nIt's in your library too, at any time."
        if attached else ""
    )
    included = (perk or "").strip()
    membership = (
        f"\n\nThis one comes with {included}, already on your account — "
        "sign in with this address and it's there."
        if included else ""
    )
    blurb = " ".join((description or "").split())
    text = (
        "Thanks for your purchase on Bloom Anyway.\n\n"
        f"Order #: {oid}\n"
        f"Item: {product}\n"
        + (f"{blurb}\n" if blurb else "")
        + f"Amount paid: {paid}\n"
        f"Date: {when}"
        f"{came_with}{membership}\n\n"
        "— Bloom Anyway"
    )
    if not template_id:
        return send_email(to, "Your Bloom Anyway receipt", text,
                          attachments=attachments)

    params = {
        "ORDER_ID": oid,
        "PRODUCT_NAME": product,
        "AMOUNT": paid,
        "ORDER_DATE": when,
        "ATTACHED": ", ".join(n for n in attached if n),
        "MEMBERSHIP_INCLUDED": included,
        "PRODUCT_DESCRIPTION": blurb,
    }
    return send_email(
        to,
        "Your Bloom Anyway receipt",
        text,
        template_id=template_id,
        params=params,
        attachments=attachments,
    )


def send_healing_welcome(
    to: str,
    *,
    trial_end_date: str = "",
    plan_price: str,
    billing_interval: str,
) -> bool:
    """Send Brevo template (#5) when someone newly joins Healing membership."""
    return _send_membership_welcome(
        to,
        tier="healing",
        subject="Welcome to Healing membership",
        config_key="BREVO_TEMPLATE_HEALING",
        default_id=5,
        trial_end_date=trial_end_date,
        plan_price=plan_price,
        billing_interval=billing_interval,
    )


def send_creator_welcome(
    to: str,
    *,
    trial_end_date: str = "",
    plan_price: str,
    billing_interval: str,
) -> bool:
    """Send Brevo template (#6) when someone newly joins Creator membership."""
    return _send_membership_welcome(
        to,
        tier="creator",
        subject="Welcome to Creator membership",
        config_key="BREVO_TEMPLATE_CREATOR",
        default_id=6,
        trial_end_date=trial_end_date,
        plan_price=plan_price,
        billing_interval=billing_interval,
    )


def send_full_bloom_welcome(
    to: str,
    *,
    trial_end_date: str = "",
    plan_price: str,
    billing_interval: str,
) -> bool:
    """Send Brevo template (#19) when someone newly joins Full Bloom membership."""
    return _send_membership_welcome(
        to,
        tier="full_bloom",
        subject="Welcome to Full Bloom membership",
        config_key="BREVO_TEMPLATE_FULL_BLOOM",
        default_id=19,
        trial_end_date=trial_end_date,
        plan_price=plan_price,
        billing_interval=billing_interval,
    )


def _send_membership_welcome(
    to: str,
    *,
    tier: str,
    subject: str,
    config_key: str,
    default_id: int,
    trial_end_date: str,
    plan_price: str,
    billing_interval: str,
) -> bool:
    """Healing #5 / Creator #6 / Full Bloom #19 — PLAN_PRICE, BILLING_INTERVAL."""
    template_id = _int_config(config_key, default_id) or None
    trial = (trial_end_date or "").strip() or "—"
    price = (plan_price or "").strip() or "—"
    interval = (billing_interval or "").strip() or "monthly"
    label = {
        "healing": "Healing",
        "creator": "Creator",
        "full_bloom": "Full Bloom",
    }.get(tier, "membership")
    text = (
        f"Welcome to {label} membership on Bloom Anyway.\n\n"
        f"Plan price: {price}\n"
        f"Billing: {interval}\n"
    )
    if trial and trial != "—":
        text += f"Trial ends: {trial}\n"
    text += "\n— Bloom Anyway"
    if not template_id:
        return send_email(to, subject, text)

    params = {
        "PLAN_PRICE": price,
        "BILLING_INTERVAL": interval,
    }
    return send_email(to, subject, text, template_id=template_id, params=params)


def send_card_declined(
    to: str,
    *,
    plan_name: str,
    grace_days: str | int,
) -> bool:
    """Send Brevo template (#7) when a membership renewal card is declined."""
    template_id = _int_config("BREVO_TEMPLATE_CARD_DECLINED", 7) or None
    plan = (plan_name or "").strip() or "your membership"
    days = str(grace_days).strip() or "5"
    text = (
        "We couldn't charge the card on file for your Bloom Anyway membership.\n\n"
        f"Plan: {plan}\n"
        f"Your access is still active for now. Update your payment method within "
        f"{days} day(s). After that, your access will be revoked and your "
        f"membership will be cancelled.\n\n"
        "— Bloom Anyway"
    )
    if not template_id:
        return send_email(to, "Update your payment method", text)

    params = {
        "PLAN_NAME": plan,
        "GRACE_DAYS": days,
    }
    return send_email(
        to,
        "Update your payment method",
        text,
        template_id=template_id,
        params=params,
    )


def send_membership_cancelled(
    to: str,
    *,
    plan_name: str,
    access_end_date: str,
) -> bool:
    """Send Brevo template (#8) when a member cancels their subscription."""
    template_id = _int_config("BREVO_TEMPLATE_CANCEL", 8) or None
    plan = (plan_name or "").strip() or "your membership"
    ends = (access_end_date or "").strip() or "—"
    text = (
        "Your Bloom Anyway membership has been cancelled.\n\n"
        f"Plan: {plan}\n"
        f"You keep access until {ends}. On that date your membership access "
        f"will be revoked and paid features will end.\n\n"
        "You will not be charged again.\n\n"
        "— Bloom Anyway"
    )
    if not template_id:
        return send_email(to, "Your membership is cancelled", text)

    params = {
        "PLAN_NAME": plan,
        "ACCESS_END_DATE": ends,
    }
    return send_email(
        to,
        "Your membership is cancelled",
        text,
        template_id=template_id,
        params=params,
    )


def send_newsletter_welcome(to: str) -> bool:
    """Send Brevo template (#9) when someone joins the Sunday letter list."""
    template_id = _int_config("BREVO_TEMPLATE_NEWSLETTER", 9) or None
    text = (
        "You're in — welcome to the Bloom Anyway Sunday letter.\n\n"
        "One small step, every Sunday.\n\n"
        "— Bloom Anyway"
    )
    if not template_id:
        return send_email(to, "You're on the Sunday letter", text)

    return send_email(
        to,
        "You're on the Sunday letter",
        text,
        template_id=template_id,
        params={},
    )


def send_support_group_booked(
    to: str,
    *,
    group_topic: str,
    host_name: str = "",
    session_date: str,
    session_time: str,
    button_url: str = "",
) -> bool:
    """Send Brevo template (#11) when a peer support-group seat is saved.

    Params: GROUP_TOPIC, SESSION_DATE, SESSION_TIME.
    """
    template_id = _int_config("BREVO_TEMPLATE_SUPPORT_BOOKED", 11) or None
    topic = (group_topic or "").strip() or "your support session"
    host = (host_name or "").strip() or "a member"
    day = (session_date or "").strip() or "—"
    time_s = (session_time or "").strip() or "—"
    url = (button_url or "").strip() or _public_href("/support-groups")
    if url.startswith("/"):
        url = _public_href(url)
    subject = "Your seat is saved"
    text = (
        "Your seat is saved.\n\n"
        f"You're booked into {topic} — hosted by {host}.\n"
        f"• {day} at {time_s}\n"
        "• 30 minutes, up to 8 women, in-site video\n\n"
        "We'll send a reminder 24 hours before, with the link to join.\n\n"
        f"Open session: {url}\n\n"
        "— Bloom Anyway"
    )
    if not template_id:
        return send_email(to, subject, text)

    params = {
        "GROUP_TOPIC": topic,
        "SESSION_DATE": day,
        "SESSION_TIME": time_s,
    }
    return send_email(
        to,
        subject,
        text,
        template_id=template_id,
        params=params,
    )


def send_support_group_left(
    to: str,
    *,
    group_topic: str,
    session_date: str,
) -> bool:
    """Send Brevo template (#12) when a member leaves an upcoming session."""
    template_id = _int_config("BREVO_TEMPLATE_SUPPORT_LEFT", 12) or None
    topic = (group_topic or "").strip() or "your support session"
    day = (session_date or "").strip() or "—"
    subject = "Your seat has been released"
    text = (
        "Your seat has been released.\n\n"
        f"This confirms you're no longer booked into {topic} on {day}. "
        "Your seat has opened up for someone else who needs it. "
        "No explanation needed — come back whenever it's the right time.\n\n"
        "— Bloom Anyway"
    )
    if not template_id:
        return send_email(to, subject, text)

    params = {
        "GROUP_TOPIC": topic,
        "SESSION_DATE": day,
    }
    return send_email(
        to,
        subject,
        text,
        template_id=template_id,
        params=params,
    )


def send_support_group_reminder(
    to: str,
    *,
    group_topic: str,
    host_name: str,
    session_date: str,
    session_time: str,
    button_url: str,
) -> bool:
    """Send Brevo template (#13) ~24 hours before a support session."""
    template_id = _int_config("BREVO_TEMPLATE_SUPPORT_REMINDER", 13) or None
    topic = (group_topic or "").strip() or "your support session"
    host = (host_name or "").strip() or "a member"
    day = (session_date or "").strip() or "—"
    time_s = (session_time or "").strip() or "—"
    url = (button_url or "").strip() or _public_href("/support-groups")
    if url.startswith("/"):
        url = _public_href(url)
    subject = "Your session is tomorrow"
    text = (
        "Your session is tomorrow.\n\n"
        f"{topic} with {host} is tomorrow, {day} at {time_s}.\n\n"
        "No need to prepare anything. Just show up as you are — "
        "that's the whole point.\n\n"
        f"Join: {url}\n\n"
        "— Bloom Anyway"
    )
    if not template_id:
        return send_email(to, subject, text)

    params = {
        "GROUP_TOPIC": topic,
        "HOST_NAME": host,
        "SESSION_DATE": day,
        "SESSION_TIME": time_s,
        "BUTTON_URL": url,
    }
    return send_email(
        to,
        subject,
        text,
        template_id=template_id,
        params=params,
    )


def send_support_group_host_cancelled(
    to: str,
    *,
    group_topic: str,
    session_date: str,
    button_url: str = "",
) -> bool:
    """Send Brevo template (#14) when a host cancels a peer support session.

    Params: GROUP_TOPIC, SESSION_DATE.
    """
    template_id = _int_config("BREVO_TEMPLATE_SUPPORT_HOST_CANCEL", 14) or None
    topic = (group_topic or "").strip() or "your support session"
    day = (session_date or "").strip() or "—"
    url = (button_url or "").strip() or _public_href("/support-groups")
    if url.startswith("/"):
        url = _public_href(url)
    subject = "This session won't be happening"
    text = (
        "This session won't be happening.\n\n"
        f"{topic} on {day} has been cancelled by the host.\n\n"
        "Nothing you need to do — no charge, no lost seat. Whenever you're "
        "ready, there are other sessions open to join, or you can host your own.\n\n"
        f"Find another session: {url}\n\n"
        "— Bloom Anyway"
    )
    if not template_id:
        return send_email(to, subject, text)

    params = {
        "GROUP_TOPIC": topic,
        "SESSION_DATE": day,
    }
    return send_email(
        to,
        subject,
        text,
        template_id=template_id,
        params=params,
    )


def send_facilitator_booked(
    to: str,
    *,
    session_date: str,
    session_time: str,
    amount: str,
) -> bool:
    """Send Brevo template (#15) when a facilitator-led session is booked."""
    template_id = _int_config("BREVO_TEMPLATE_FACILITATOR_BOOKED", 15) or None
    day = (session_date or "").strip() or "—"
    time_s = (session_time or "").strip() or "—"
    paid = (amount or "").strip() or "—"
    subject = "Your guided session is booked"
    text = (
        "Your guided session is booked.\n\n"
        "You're booked into a facilitator-led session — 60 minutes, "
        "professionally guided, 8 women max.\n\n"
        "We'll send a reminder 24 hours before, with the link to join.\n\n"
        f"Date: {day}\n"
        f"Time: {time_s}\n"
        f"Amount paid: {paid}\n\n"
        "— Bloom Anyway"
    )
    if not template_id:
        return send_email(to, subject, text)

    params = {
        "SESSION_DATE": day,
        "SESSION_TIME": time_s,
        "AMOUNT": paid,
    }
    return send_email(
        to,
        subject,
        text,
        template_id=template_id,
        params=params,
    )


def send_one_on_one_booked(
    to: str,
    *,
    coach_name: str,
    session_date: str,
    session_time: str,
    amount: str,
    button_url: str = "",
) -> bool:
    """Send Brevo template (#16) when a 1:1 with a founder is booked.

    Params: COACH_NAME, SESSION_DATE, SESSION_TIME, AMOUNT, BUTTON_URL.
    """
    template_id = _int_config("BREVO_TEMPLATE_ONE_ON_ONE_BOOKED", 16) or None
    coach = (coach_name or "").strip() or "a founder"
    day = (session_date or "").strip() or "—"
    time_s = (session_time or "").strip() or "—"
    paid = (amount or "").strip() or "—"
    url = (button_url or "").strip() or _public_href("/support-groups")
    if url.startswith("/"):
        url = _public_href(url)
    subject = f"Your 1:1 with {coach} is booked"
    text = (
        f"Your 1:1 with {coach} is booked.\n\n"
        f"Your private 60-minute session is set for {day} at {time_s}.\n\n"
        f"Date: {day}\n"
        f"Time: {time_s}\n"
        f"With: {coach}\n"
        f"Amount paid: {paid}\n\n"
        f"Join: {url}\n\n"
        "— Bloom Anyway"
    )
    if not template_id:
        return send_email(to, subject, text)

    params = {
        "COACH_NAME": coach,
        "SESSION_DATE": day,
        "SESSION_TIME": time_s,
        "AMOUNT": paid,
        "BUTTON_URL": url,
    }
    return send_email(
        to,
        subject,
        text,
        template_id=template_id,
        params=params,
    )


def send_facilitator_cancelled(
    to: str,
    *,
    session_date: str,
    amount: str,
) -> bool:
    """Send Brevo template (#17) when a facilitator-led session is cancelled."""
    template_id = _int_config("BREVO_TEMPLATE_FACILITATOR_CANCELLED", 17) or None
    day = (session_date or "").strip() or "—"
    paid = (amount or "").strip() or "—"
    subject = "Your guided session has been cancelled"
    text = (
        "Your guided session has been cancelled.\n\n"
        f"We're sorry — your facilitator-led session on {day} has had to be "
        f"cancelled.\n\n"
        f"{paid} will be refunded to your original payment method within "
        "5-10 business days. No action needed on your end.\n\n"
        "We'd love to have you at the next one when you're ready.\n\n"
        "— Bloom Anyway"
    )
    if not template_id:
        return send_email(to, subject, text)

    params = {
        "SESSION_DATE": day,
        "AMOUNT": paid,
    }
    return send_email(
        to,
        subject,
        text,
        template_id=template_id,
        params=params,
    )


def send_one_on_one_cancelled(
    to: str,
    *,
    coach_name: str,
    session_date: str,
    amount: str,
) -> bool:
    """Send Brevo template (#18) when a founder cancels a 1:1 session."""
    template_id = _int_config("BREVO_TEMPLATE_ONE_ON_ONE_CANCELLED", 18) or None
    coach = (coach_name or "").strip() or "a founder"
    day = (session_date or "").strip() or "—"
    paid = (amount or "").strip() or "—"
    subject = f"Your 1:1 with {coach} has been cancelled"
    text = (
        f"Your 1:1 with {coach} has been cancelled.\n\n"
        f"We're sorry — {coach} has had to cancel your session on {day}.\n\n"
        f"{paid} will be refunded to your original payment method within "
        "5-10 business days. No action needed on your end.\n\n"
        "Reach out whenever you're ready to find a new time — your spot "
        "matters, and this wasn't your fault.\n\n"
        "— Bloom Anyway"
    )
    if not template_id:
        return send_email(to, subject, text)

    params = {
        "COACH_NAME": coach,
        "SESSION_DATE": day,
        "AMOUNT": paid,
    }
    return send_email(
        to,
        subject,
        text,
        template_id=template_id,
        params=params,
    )


def owner_emails() -> list[str]:
    """Every active owner's address, de-duplicated, oldest account first.

    Anything addressed to "the owner" goes to all of them — a co-owner who
    never sees a contact message can't answer it.
    """
    from ..models import User
    rows = (User.query
            .filter(User.is_admin.is_(True), User.deleted_at.is_(None))
            .order_by(User.id)
            .all())
    seen: set[str] = set()
    out: list[str] = []
    for owner in rows:
        addr = (owner.email or "").strip().lower()
        if addr and "@" in addr and addr not in seen:
            seen.add(addr)
            out.append(addr)
    return out


def _owner_email() -> str | None:
    addrs = owner_emails()
    return addrs[0] if addrs else None


def _send_to_owners(what: str, **kwargs) -> bool:
    """Send one styled email to every owner. True when they all went out."""
    addrs = owner_emails()
    if not addrs:
        log.warning("No owner account to notify about %s", what)
        _set_error("No owner account email to notify.")
        return False
    sent = 0
    for addr in addrs:
        if send_styled_email(addr, **kwargs):
            sent += 1
        else:
            log.warning("Could not email owner %s about %s", addr, what)
    return sent == len(addrs)


def send_billing_alert(title: str, body: str) -> bool:
    """Tell the owners about billing that needs to be sorted out in Stripe.

    Used when we could not stop a subscription ourselves, so a silent failure
    can't leave someone being charged after they've left.
    """
    return _send_to_owners(
        f"billing: {title}",
        subject=f"Action needed: {title}",
        preview=title,
        header="Billing",
        title=title,
        body=body,
        button_text="Open Stripe",
        button_url="https://dashboard.stripe.com/subscriptions",
    )


def send_contact_notification(name: str, email: str, body: str) -> bool:
    return _send_to_owners(
        f"contact form from {name}",
        subject=f"Contact form: {name}",
        preview=f"New message from {name}",
        header="Contact form",
        title=f"Message from {name}",
        body=f"From: {name} <{email}>\n\n{body}",
        button_text="Open Studio inbox",
        button_url=_public_href("/admin/inbox?filter=messages"),
    )
