"""Mailer with two transports.

1. SMTP (``SMTP_HOST`` / ``SMTP_USER`` / ``SMTP_PASSWORD``) — preferred when
   configured. Use this for Brevo's SMTP wizard (``smtp-relay.brevo.com``).
2. Brevo HTTP API (``BREVO_API_KEY`` starting with ``xkeysib-``) — used when
   SMTP is not configured. Works on hosts that block outbound SMTP ports.

If ``BREVO_API_KEY`` is a Brevo *SMTP* key (``xsmtpsib-…``), it is treated as
SMTP credentials for ``smtp-relay.brevo.com`` (not the HTTP API).

When neither is configured (local dev) emails are printed to the console.
"""
import logging
import re
import smtplib
from email.message import EmailMessage
from html import escape

import requests
from flask import current_app

log = logging.getLogger(__name__)

BREVO_SMTP_HOST = "smtp-relay.brevo.com"

# Most recent send failure (human-readable). Cleared on success.
_last_error = ""


def last_send_error() -> str:
    return _last_error


def _set_error(message: str) -> None:
    global _last_error
    _last_error = (message or "").strip()


def _strip_env_quotes(value: str) -> str:
    """Render/dashboard pastes often wrap secrets in quotes — strip them."""
    v = (value or "").strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1].strip()
    return v


def _brevo_api_key() -> str:
    """Normalize the Brevo key (strip whitespace / quotes / Bearer prefix)."""
    key = _strip_env_quotes(current_app.config.get("BREVO_API_KEY") or "")
    if key.lower().startswith("bearer "):
        key = key[7:].strip()
    return key


def _parse_from(mail_from: str) -> dict:
    """Split 'Name <addr@x.com>' into Brevo's {"name": ..., "email": ...}."""
    mail_from = _strip_env_quotes(mail_from or "")
    match = re.match(r"^\s*(.*?)\s*<([^>]+)>\s*$", mail_from)
    if match:
        name, email = match.groups()
        return {"name": (name or "Bloom Anyway").strip() or "Bloom Anyway",
                "email": email.strip()}
    if "@" in mail_from:
        return {"name": "Bloom Anyway", "email": mail_from}
    return {"name": "Bloom Anyway", "email": mail_from}


def _resolve_smtp() -> dict | None:
    """Return SMTP settings if ready, else None.

    Sources (first match wins):
    1. Explicit SMTP_HOST + SMTP_USER + SMTP_PASSWORD
    2. Brevo SMTP key in BREVO_API_KEY (xsmtpsib-…) → smtp-relay.brevo.com
    """
    cfg = current_app.config
    host = _strip_env_quotes(cfg.get("SMTP_HOST") or "")
    user = _strip_env_quotes(cfg.get("SMTP_USER") or "")
    password = _strip_env_quotes(cfg.get("SMTP_PASSWORD") or "")
    try:
        port = int(cfg.get("SMTP_PORT") or 587)
    except (TypeError, ValueError):
        port = 587
    key = _brevo_api_key()

    if host and user and password:
        return {"host": host, "port": port, "user": user, "password": password}

    if key.lower().startswith("xsmtpsib-"):
        sender = _parse_from(cfg.get("MAIL_FROM") or "")
        login = user or sender.get("email") or ""
        if not login or "@" not in login:
            _set_error(
                "Brevo SMTP key detected, but SMTP_USER is missing. "
                "Set SMTP_USER to the login shown in Brevo → SMTP & API "
                "(usually your Brevo account email)."
            )
            return None
        return {
            "host": host or BREVO_SMTP_HOST,
            "port": port,
            "user": login,
            "password": key,
        }
    return None


def _brevo_error_hint(status: int, body: str) -> str:
    """Turn a Brevo HTTP failure into a short owner-facing hint."""
    text = (body or "").lower()
    if status == 401:
        return (
            "Brevo rejected the API key (401). Use an API key (xkeysib-…), "
            "not an SMTP key, and authorize Render's outbound IP in Brevo "
            "Security — or turn off IP restriction for that key."
        )
    if status == 403:
        return (
            "Brevo forbade the send (403). Check that transactional email is "
            "enabled and the sender/domain are verified for this Brevo account."
        )
    if status == 400 and ("sender" in text or "from" in text):
        return (
            "Brevo rejected the sender. MAIL_FROM must be an exact verified "
            "sender address on this Brevo account (domain authenticated)."
        )
    if status == 400:
        return f"Brevo rejected the email (400): {(body or '')[:240]}"
    return f"Brevo error {status}: {(body or '')[:240]}"


def _send_via_brevo(to: str, subject: str, text_body: str) -> bool:
    """Send through Brevo HTTP API. Prefer plain text; also include tiny HTML."""
    key = _brevo_api_key()
    if not key:
        _set_error("BREVO_API_KEY is empty.")
        return False
    if key.lower().startswith("xsmtpsib-"):
        # Should have been routed to SMTP by send_email; keep as safety net.
        log.error("Brevo: BREVO_API_KEY is an SMTP key — use SMTP transport.")
        _set_error(
            "BREVO_API_KEY is an SMTP key (xsmtpsib-…). Set SMTP_HOST="
            f"{BREVO_SMTP_HOST}, SMTP_USER=your Brevo login, and put this key "
            "in SMTP_PASSWORD (or leave it in BREVO_API_KEY with SMTP_USER set)."
        )
        return False

    sender = _parse_from(current_app.config.get("MAIL_FROM") or "")
    if not sender.get("email") or "@" not in sender["email"]:
        log.error("Brevo: MAIL_FROM is missing a real email address (got %r). "
                  "Set MAIL_FROM to a sender verified in your Brevo account.",
                  current_app.config.get("MAIL_FROM"))
        _set_error("MAIL_FROM must be a real verified email, e.g. "
                   "Bloom Anyway <hello@yourdomain.com>.")
        return False
    if sender["email"].endswith("@localhost"):
        log.error("Brevo: MAIL_FROM still uses @localhost — set it to a verified sender.")
        _set_error("MAIL_FROM still uses @localhost — set a verified Brevo sender.")
        return False

    html_body = (
        "<pre style=\"font-family:ui-monospace,monospace;white-space:pre-wrap;"
        "font-size:15px;line-height:1.5;\">"
        f"{escape(text_body)}</pre>"
    )
    payload = {
        "sender": sender,
        "to": [{"email": to}],
        "subject": subject,
        "textContent": text_body,
        "htmlContent": html_body,
    }
    headers = {
        "api-key": key,
        "accept": "application/json",
        "content-type": "application/json",
    }
    try:
        resp = requests.post(
            "https://api.brevo.com/v3/smtp/email",
            json=payload,
            headers=headers,
            timeout=20,
        )
        if resp.status_code in (200, 201, 202):
            log.info("Brevo API: sent to %s (status %s)", to, resp.status_code)
            _set_error("")
            return True

        hint = _brevo_error_hint(resp.status_code, resp.text)
        log.error("Brevo API rejected email to %s: %s %s", to, resp.status_code, resp.text)

        if resp.status_code == 400 and "name" in payload["sender"]:
            payload["sender"] = {"email": sender["email"]}
            resp2 = requests.post(
                "https://api.brevo.com/v3/smtp/email",
                json=payload,
                headers=headers,
                timeout=20,
            )
            if resp2.status_code in (200, 201, 202):
                log.info("Brevo API: sent to %s on retry (status %s)", to, resp2.status_code)
                _set_error("")
                return True
            hint = _brevo_error_hint(resp2.status_code, resp2.text)
            log.error("Brevo API retry failed for %s: %s %s", to, resp2.status_code, resp2.text)

        _set_error(hint)
        return False
    except Exception as exc:
        log.exception("Failed to reach Brevo API for email to %s", to)
        _set_error(f"Could not reach Brevo ({exc.__class__.__name__}).")
        return False


def _send_via_smtp(to: str, subject: str, text_body: str,
                   html_body: str | None = None,
                   smtp: dict | None = None) -> bool:
    cfg = current_app.config
    smtp = smtp or _resolve_smtp()
    if not smtp:
        return False

    mail_from = _strip_env_quotes(cfg.get("MAIL_FROM") or "")
    if not mail_from or "@localhost" in mail_from.lower():
        _set_error("MAIL_FROM must be a verified Brevo sender before SMTP can send.")
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = mail_from
    msg["To"] = to
    msg.set_content(text_body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    else:
        msg.add_alternative(
            "<pre style=\"font-family:ui-monospace,monospace;white-space:pre-wrap;"
            f"font-size:15px;line-height:1.5;\">{escape(text_body)}</pre>",
            subtype="html",
        )

    host, port = smtp["host"], int(smtp["port"])
    try:
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=20)
        else:
            server = smtplib.SMTP(host, port, timeout=20)
            server.ehlo()
            server.starttls()
            server.ehlo()
        with server:
            server.login(smtp["user"], smtp["password"])
            server.send_message(msg)
        log.info("SMTP: sent to %s via %s:%s", to, host, port)
        _set_error("")
        return True
    except smtplib.SMTPAuthenticationError:
        log.exception("SMTP auth failed for %s via %s", to, host)
        _set_error(
            "SMTP login failed. Check SMTP_USER (Brevo SMTP login) and "
            "SMTP_PASSWORD (Brevo SMTP key, xsmtpsib-…)."
        )
        return False
    except (smtplib.SMTPException, OSError, TimeoutError) as exc:
        log.exception("Failed to send email to %s via SMTP %s:%s", to, host, port)
        _set_error(
            f"SMTP send failed ({exc.__class__.__name__}). On Render free tier, "
            "outbound SMTP is often blocked — use a paid instance or the Brevo "
            "HTTP API key (xkeysib-…) instead."
        )
        return False


def send_email(to: str, subject: str, text_body: str, html_body: str | None = None) -> bool:
    """Send email. Prefers SMTP when configured; else Brevo HTTP API; else console."""
    to = (to or "").strip()
    if not to:
        _set_error("Missing recipient email.")
        return False

    _set_error("")
    smtp = _resolve_smtp()
    if smtp is None and last_send_error():
        # _resolve_smtp already set a specific error (e.g. missing SMTP_USER).
        return False
    if smtp:
        return _send_via_smtp(to, subject, text_body, html_body, smtp=smtp)

    key = _brevo_api_key()
    if key:
        return _send_via_brevo(to, subject, text_body)

    log.warning("No email transport configured; printing email to console.")
    print("\n===== EMAIL (console fallback) =====")
    print(f"To: {to}\nSubject: {subject}\n\n{text_body}")
    print("====================================\n")
    _set_error("")
    return True


def send_verification_code(to: str, code: str, purpose: str) -> bool:
    minutes = current_app.config["CODE_MAX_AGE_MINUTES"]
    if purpose == "reset":
        subject = "Your password reset code"
        intro = "Here's your code to reset your password:"
    else:
        subject = "Your confirmation code"
        intro = "Welcome. Here's your code to confirm your email:"
    text = (
        f"{intro}\n\n    {code}\n\n"
        f"It expires in {minutes} minutes.\n"
        "If you didn't request it, you can safely ignore this email.\n\n"
        "— Bloom Anyway"
    )
    return send_email(to, subject, text)


def send_contact_notification(name: str, email: str, body: str) -> bool:
    from ..models import User
    owner = (User.query.filter_by(is_admin=True)
             .filter(User.deleted_at.is_(None)).order_by(User.id).first())
    admin = owner.email if owner else None
    if not admin:
        log.warning("No owner account to notify; contact message stored but not emailed.")
        _set_error("No owner account email to notify.")
        return False
    text = f"New message from the contact form.\n\nFrom: {name} <{email}>\n\n{body}"
    return send_email(admin, f"Contact form: {name}", text)
