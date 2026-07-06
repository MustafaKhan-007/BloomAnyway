"""Idempotent seed script — content only, never credentials.

- Loads data/quotes_seed.json, skipping quotes whose text already exists
  (case-insensitive).
- Creates starter FAQ items and legal page stubs if none exist.

The owner/admin account is created in the browser at /setup (one-time page,
locks itself after the owner's first sign-in). Passwords are never written
by this script, so password changes always survive redeploys.

Run after `flask db upgrade`:  python seed.py
"""
import json
from pathlib import Path

from app import create_app
from app.extensions import db
from app.models import FaqItem, Page, Quote

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
        # 1. quotes (idempotent on lowercase text)
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

        # 2. starter FAQ
        if FaqItem.query.count() == 0:
            for question, answer, order in STARTER_FAQS:
                db.session.add(FaqItem(question=question, answer_md=answer, sort_order=order))
            print(f"Added {len(STARTER_FAQS)} starter FAQ items")

        # 3. legal page stubs
        for slug, (title, body) in LEGAL_STUBS.items():
            if Page.query.filter_by(slug=slug).first() is None:
                db.session.add(Page(slug=slug, title=title, body_md=body))
                print(f"Created page stub: {slug}")

        db.session.commit()
        print("Seed complete.")


if __name__ == "__main__":
    seed()
