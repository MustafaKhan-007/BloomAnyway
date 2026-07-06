"""SMTP mailer. Works with Resend, Postmark or any SMTP relay.

When SMTP_HOST is unset (local dev) emails are printed to the console so the
magic-link flow is testable without a mail account.
"""
import logging
import smtplib
from email.message import EmailMessage

from flask import current_app

log = logging.getLogger(__name__)

_BRAND_WRAPPER = """\
<!doctype html>
<html>
  <body style="margin:0;padding:0;background:#FAF5EE;font-family:'Nunito Sans',Verdana,sans-serif;color:#2B2622;">
    <div style="max-width:560px;margin:0 auto;padding:24px;">
      <div style="background:#7A2E62;color:#FAF5EE;border-radius:16px 16px 0 0;padding:20px 28px;
                  font-size:18px;font-weight:700;letter-spacing:0.02em;">First Light</div>
      <div style="background:#ffffff;border-radius:0 0 16px 16px;padding:28px;line-height:1.7;font-size:16px;">
        {body}
      </div>
      <p style="color:#6B6159;font-size:13px;text-align:center;margin-top:16px;">
        Sent with care. If this wasn't for you, you can simply ignore it.
      </p>
    </div>
  </body>
</html>"""

_BUTTON = """\
<p style="text-align:center;margin:28px 0;">
  <a href="{url}" style="background:#EFA733;color:#2B2622;text-decoration:none;font-weight:700;
     padding:14px 32px;border-radius:999px;display:inline-block;">{label}</a>
</p>"""


def send_email(to: str, subject: str, text_body: str, html_body: str | None = None) -> bool:
    cfg = current_app.config
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg["MAIL_FROM"]
    msg["To"] = to
    msg.set_content(text_body)
    if html_body:
        msg.add_alternative(_BRAND_WRAPPER.format(body=html_body), subtype="html")

    if not cfg["SMTP_HOST"]:
        log.warning("SMTP not configured; printing email to console.")
        print("\n===== EMAIL (console fallback) =====")
        print(f"To: {to}\nSubject: {subject}\n\n{text_body}")
        print("====================================\n")
        return True

    try:
        with smtplib.SMTP(cfg["SMTP_HOST"], cfg["SMTP_PORT"], timeout=15) as server:
            server.starttls()
            if cfg["SMTP_USER"]:
                server.login(cfg["SMTP_USER"], cfg["SMTP_PASSWORD"])
            server.send_message(msg)
        return True
    except Exception:
        log.exception("Failed to send email to %s", to)
        return False


_CODE_BOX = """\
<p style="text-align:center;margin:28px 0;">
  <span style="display:inline-block;background:#FBE8C8;color:#2B2622;font-size:32px;
        font-weight:700;letter-spacing:8px;padding:16px 28px;border-radius:14px;
        font-family:Consolas,monospace;">{code}</span>
</p>"""


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
        f"It expires in {minutes} minutes. "
        "If you didn't request it, you can safely ignore this email."
    )
    html = (
        f"<p>{intro}</p>"
        + _CODE_BOX.format(code=code)
        + f"<p style='color:#6B6159;font-size:14px;'>It expires in {minutes} minutes. "
        "If you didn't request it, you can safely ignore this email.</p>"
    )
    return send_email(to, subject, text, html)


def send_contact_notification(name: str, email: str, body: str) -> bool:
    admin = current_app.config["ADMIN_EMAIL"]
    if not admin:
        log.warning("ADMIN_EMAIL not set; contact message stored but not emailed.")
        return False
    text = f"New message from the contact form.\n\nFrom: {name} <{email}>\n\n{body}"
    html = (
        f"<p><strong>New message from the contact form.</strong></p>"
        f"<p>From: {name} &lt;{email}&gt;</p>"
        f"<blockquote style='border-left:3px solid #C9928A;margin:0;padding:8px 16px;'>"
        f"{body}</blockquote>"
    )
    return send_email(admin, f"Contact form: {name}", text, html)
