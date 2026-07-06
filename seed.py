"""Idempotent seed script.

- Creates/promotes the admin: ADMIN_EMAIL (env var) with ADMIN_PASSWORD
  (env var; a strong password is generated and printed if unset).
- Loads data/quotes_seed.json, skipping quotes whose text already exists
  (case-insensitive).
- Creates starter FAQ items and legal page stubs if none exist.

Run after `flask db upgrade`:  python seed.py
"""
import json
import os
import secrets
import sys
from pathlib import Path

from app import create_app
from app.extensions import db
from app.models import FaqItem, Page, Quote, User, utcnow

SEED_FILE = Path(__file__).parent / "data" / "quotes_seed.json"

STARTER_FAQS = [
    ("How do I get my files after buying?",
     "The moment your payment goes through, Lemon Squeezy emails you a receipt "
     "with your download links. Check spam if it's shy. You can always re-send "
     "them from [your orders page](https://app.lemonsqueezy.com/my-orders).", 0),
    ("Do I need an account here to buy?",
     "No. Checkout works without one. An account just adds the daily check-in, "
     "streaks, and saved quotes \u2014 it's free.", 1),
    ("What's your refund policy?",
     "See the [refund policy](/refunds) page. Short version: I'd rather you be "
     "honest with me than stuck with something that isn't helping.", 2),
    ("Is this therapy?",
     "No \u2014 and it doesn't pretend to be. These are practical courses and "
     "notebooks. If you're in crisis, please reach out to a professional or a "
     "local helpline first. This will be here after.", 3),
]

LEGAL_STUBS = {
    "privacy": ("Privacy Policy",
                "*TODO: legal review.*\n\nWe collect the minimum needed to run this site: "
                "your email if you create an account or join the letter, and order records "
                "delivered by our payment provider (Lemon Squeezy), who is the merchant of "
                "record. We never see or store card details. No tracking cookies, no ad pixels."),
    "terms": ("Terms of Service",
              "*TODO: legal review.*\n\nDigital products are licensed for personal use. "
              "Payments, taxes and delivery are handled by Lemon Squeezy as merchant of record."),
    "refunds": ("Refund Policy",
                "*TODO: legal review.*\n\nIf something isn't working for you, reply to your "
                "receipt email within 14 days and we'll make it right."),
}


def seed():
    app = create_app()
    with app.app_context():
        # 1. admin user (email + password, pre-verified)
        admin_email = os.environ.get("ADMIN_EMAIL", "").strip().lower()
        admin_password = os.environ.get("ADMIN_PASSWORD", "").strip()
        if admin_email:
            user = User.query.filter_by(email=admin_email).first()
            generated = False
            if not admin_password and (user is None or not user.password_hash):
                admin_password = secrets.token_urlsafe(12)
                generated = True
            if user is None:
                user = User(email=admin_email, is_admin=True, email_verified_at=utcnow())
                user.set_password(admin_password)
                db.session.add(user)
                print(f"Created admin user {admin_email}")
            else:
                user.is_admin = True
                user.email_verified_at = user.email_verified_at or utcnow()
                if admin_password:
                    user.set_password(admin_password)
                print(f"Updated admin {admin_email}")
            if admin_password:
                if generated:
                    print(f"Admin password (generated \u2014 save it now): {admin_password}")
                else:
                    print("Admin password set from ADMIN_PASSWORD.")
        else:
            print("ADMIN_EMAIL not set \u2014 skipping admin creation.", file=sys.stderr)

        # 2. quotes (idempotent on lowercase text)
        payload = json.loads(SEED_FILE.read_text(encoding="utf-8"))
        existing = {q.text.strip().lower() for q in Quote.query.all()}
        added = 0
        for row in payload["quotes"]:
            key = row["text"].strip().lower()
            if key in existing:
                continue
            db.session.add(Quote(text=row["text"], author=row.get("author"),
                                 category=row["category"], active=True))
            existing.add(key)
            added += 1
        print(f"Quotes: added {added}, skipped {len(payload['quotes']) - added} existing")

        # 3. starter FAQ
        if FaqItem.query.count() == 0:
            for question, answer, order in STARTER_FAQS:
                db.session.add(FaqItem(question=question, answer_md=answer, sort_order=order))
            print(f"Added {len(STARTER_FAQS)} starter FAQ items")

        # 4. legal page stubs
        for slug, (title, body) in LEGAL_STUBS.items():
            if Page.query.filter_by(slug=slug).first() is None:
                db.session.add(Page(slug=slug, title=title, body_md=body))
                print(f"Created page stub: {slug}")

        db.session.commit()
        print("Seed complete.")


if __name__ == "__main__":
    seed()
