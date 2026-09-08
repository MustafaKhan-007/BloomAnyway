"""Idempotent seed script — content only, never credentials.

- Loads data/quotes_seed.json, skipping quotes whose text already exists
  (case-insensitive).
- Creates starter FAQ items and legal page stubs if none exist.

It no longer invents community members or conversations. The forums fill up
with real people now, and a deploy has no business writing posts.

The owner/admin account is created in the browser at /setup (one-time page,
locks itself after the owner's first sign-in). Passwords are never written
by this script, so password changes always survive redeploys.

Run after `flask db upgrade`:  python seed.py
"""
import json
from pathlib import Path

from app import create_app
from app.extensions import db
from app.models import (FaqItem, ForumCategory, ForumTag, MembershipPlan, Page,
                        Quote)
from app.services.legal_copy import LEGAL_COPY_VERSION
from app.services.legal_copy import PRIVACY as _PRIVACY
from app.services.legal_copy import REFUNDS as _REFUNDS
from app.services.legal_copy import TERMS as _TERMS
from app.services.settings import get_setting, set_setting

SEED_FILE = Path(__file__).parent / "data" / "quotes_seed.json"

# Two forums, each with topic tags used for reader filters and author labels.
FORUMS = [
    {"slug": "building", "name": "Building", "accent": "#f0a202", "sort": 0,
     "description": "Growth, goals, and the brave work of creating a life and a craft.",
     "tags": [("content", "Content & Creating"), ("starting-over", "Starting Over"),
              ("work-money", "Work & Money"), ("wins", "Small Wins")]},
    {"slug": "healing", "name": "Healing", "accent": "#7b6cf6", "sort": 1,
     "description": "Room to process, grieve, vent, and find your footing again.",
     "tags": [("venting", "The Vent"), ("divorce-custody", "Divorce & Custody"),
              ("grief", "Grief & Loss"), ("confidence", "Confidence")]},
]
# categories seeded by the previous version, now folded into tags above
RETIRED_CATEGORY_SLUGS = ["venting", "divorce-custody", "content-creation",
                          "starting-over", "wins"]

STARTER_FAQS = [
    ("How do I get my files after buying?",
     "When payment clears, Stripe emails you a receipt. Purchases linked "
     "to your account email also appear under My space → Courses & guides.", 0),
    ("Do I need an account here to buy?",
     "No. Checkout works without one. An account just adds saved quotes, the "
     "community forums, and course picks made for you \u2014 it's free.", 1),
    ("What's your refund policy?",
     "Refunds are only applicable for duplicate purchases. Guides are "
     "non-refundable \u2014 a guide opens the moment you pay and is yours to "
     "keep. Read the full note on the [refund policy](/refunds) page. Either "
     "way, if something has gone wrong, tell me before you tell your bank.", 2),
    ("Is this therapy?",
     "No \u2014 and it doesn't pretend to be. These are practical courses and "
     "notebooks. If you're in crisis, please reach out to a professional or a "
     "local helpline first. This will be here after.", 3),
]

# Membership plans, created inactive. Set a price + Stripe price ID
# in Studio -> Plans, then flip them Live. Buying one auto-upgrades the buyer.
MEMBERSHIP_PLANS = [
    {"tier": "healing", "name": "Healing membership",
     "tagline": "Belong to the whole community.", "period": "month", "sort_order": 1},
    {"tier": "creator", "name": "Creator membership",
     "tagline": "Everything, plus the tools to be seen.", "period": "month", "sort_order": 2},
]

LEGAL_STUBS = {
    "privacy": ("Privacy Policy", _PRIVACY),
    "terms": ("Terms of Service", _TERMS),
    "refunds": ("Refund Policy", _REFUNDS),
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

        # 3. legal pages — create missing, refresh TODO stubs, or force-sync
        #    when LEGAL_COPY_VERSION bumps (so deploy picks up Terms/Refunds).
        legal_version = get_setting("legal_copy_version", "")
        force_legal = legal_version != LEGAL_COPY_VERSION
        for slug, (title, body) in LEGAL_STUBS.items():
            page = Page.query.filter_by(slug=slug).first()
            if page is None:
                db.session.add(Page(slug=slug, title=title, body_md=body))
                print(f"Created page: {slug}")
            elif force_legal or (page.body_md and "*TODO: legal review.*" in page.body_md):
                page.title = title
                page.body_md = body
                print(f"Refreshed legal page: {slug}")
        if force_legal:
            set_setting("legal_copy_version", LEGAL_COPY_VERSION)
            print(f"Legal copy version → {LEGAL_COPY_VERSION}")

        # 4. forums + topic tags (idempotent). Retire the old single-topic
        #    categories once they're empty — they live on as tags now.
        removed = 0
        for slug in RETIRED_CATEGORY_SLUGS:
            old = ForumCategory.query.filter_by(slug=slug).first()
            if old and old.posts.count() == 0:
                db.session.delete(old)
                removed += 1
        if removed:
            print(f"Retired {removed} old forum categories")

        cat_added = tag_added = 0
        for f in FORUMS:
            cat = ForumCategory.query.filter_by(slug=f["slug"]).first()
            if cat is None:
                cat = ForumCategory(slug=f["slug"])
                db.session.add(cat)
                cat_added += 1
            cat.name = f["name"]
            cat.description = f["description"]
            cat.accent = f["accent"]
            cat.sort_order = f["sort"]
            db.session.flush()
            for order, (tslug, tname) in enumerate(f["tags"]):
                if ForumTag.query.filter_by(category_id=cat.id, slug=tslug).first() is None:
                    db.session.add(ForumTag(category_id=cat.id, slug=tslug,
                                            name=tname, sort_order=order))
                    tag_added += 1
        if cat_added or tag_added:
            print(f"Forums: added {cat_added} categories, {tag_added} tags")

        # 5. membership plans (inactive; owner sets a price + Stripe price id, then goes Live)
        mem_added = 0
        for m in MEMBERSHIP_PLANS:
            if MembershipPlan.query.filter_by(tier=m["tier"]).first() is None:
                db.session.add(MembershipPlan(
                    tier=m["tier"], name=m["name"], tagline=m["tagline"],
                    period=m["period"], sort_order=m["sort_order"], active=False))
                mem_added += 1
        if mem_added:
            print(f"Added {mem_added} membership plans")

        # 6. Remove leftover mock Courses & Guides rows (if any)
        from app.services.catalog import remove_demo_catalog
        cat_removed = remove_demo_catalog()
        if cat_removed:
            print(f"Removed {cat_removed} demo catalogue products")

        # 7. brand rename (First Light → Bloom Anyway) if the stored title
        #    is still a legacy name. Custom titles the owner set are left alone.
        from app.services.settings import ensure_brand_title, ensure_support_email
        if ensure_brand_title():
            print("Site title updated to Bloom Anyway")

        # 7a. Public customer support address, seeded once so a deliberate
        #     clear in Studio isn't undone on the next run. Also moves a site
        #     off an address we no longer use.
        from app.services.settings import DEFAULTS as SITE_DEFAULTS
        if ensure_support_email():
            print(f"Public support email set to {SITE_DEFAULTS['contact_email']}")

        # 7b. Founder launch window — banner on /membership through end of Sept 2026
        #     (Sept has 30 days; active while founder_price_ends >= today).
        FOUNDER_ENDS = "2026-09-30"
        set_setting("founder_price_ends", FOUNDER_ENDS)
        print(f"Founder pricing ends → {FOUNDER_ENDS}")

        # 8. Backfill mime types for stored images. Avatar/thumbnail bytes are
        #    deferred columns now, and "do they have one?" is answered from the
        #    mime — old rows saved before the mime was recorded need it filled.
        patched = 0
        for sql in (
            "UPDATE users SET avatar_mime = 'image/jpeg' "
            "WHERE avatar_data IS NOT NULL AND (avatar_mime IS NULL OR avatar_mime = '')",
            "UPDATE users SET avatar_anim_mime = 'image/gif' "
            "WHERE avatar_anim_data IS NOT NULL "
            "AND (avatar_anim_mime IS NULL OR avatar_anim_mime = '')",
            "UPDATE videos SET thumb_mime = 'image/jpeg' "
            "WHERE thumb_data IS NOT NULL AND (thumb_mime IS NULL OR thumb_mime = '')",
        ):
            patched += db.session.execute(db.text(sql)).rowcount or 0
        if patched:
            print(f"Backfilled {patched} image mime type(s)")

        db.session.commit()
        print("Seed complete.")


if __name__ == "__main__":
    seed()
