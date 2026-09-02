"""Homepage features: new Content Hub items, Product of the Day, top products."""
from __future__ import annotations

from datetime import timedelta

from flask import url_for
from sqlalchemy.orm import joinedload

from ..models import MarketplaceListing, ReelReview, Video, utcnow

#: How long something new stays on the home page. Each item runs its own clock,
#: so an afternoon reel review doesn't cut a morning tip's day short.
DROP_HOURS = 24

#: Most cards the hero strip will carry at once, newest first. Reel reviews are
#: capped at one a day and tips are published by hand, so this is headroom
#: rather than a limit anyone should meet.
MAX_DROPS = 4


def content_hub_drops(limit: int = MAX_DROPS) -> list[dict]:
    """Everything that landed in the Content Hub in the last day, newest first.

    A written tip and a reel review published the same day both show, and each
    leaves 24 hours after it went up rather than when the newest one does.
    """
    since = utcnow() - timedelta(hours=DROP_HOURS)
    drops = []
    for tip in (Video.query
                .filter(Video.published.is_(True), Video.created_at >= since)
                .order_by(Video.created_at.desc())
                .limit(limit).all()):
        drops.append({
            "kind": "tip",
            "label": "New in the Content Hub",
            "title": tip.title,
            "at": tip.created_at,
            "url": url_for("main.watch", video_id=tip.id),
        })
    for review in (ReelReview.query
                   .filter(ReelReview.published.is_(True),
                           ReelReview.created_at >= since)
                   .order_by(ReelReview.created_at.desc())
                   .limit(limit).all()):
        drops.append({
            "kind": "reel",
            "label": "New reel review",
            "title": review.title,
            "at": review.created_at,
            "url": url_for("main.reel_review", review_id=review.id),
        })
    drops.sort(key=lambda drop: drop["at"], reverse=True)
    return drops[:limit]


def content_hub_groups(limit: int = MAX_DROPS) -> list[dict]:
    """The same drops, gathered by what they are.

    Three things landing on one day used to be three full-width bars saying
    "New in the Content Hub" twice over. Gathered like this the strip is two
    rows at the very most, however busy the day, and each title is still its
    own link.
    """
    groups: dict[str, dict] = {}
    for drop in content_hub_drops(limit):
        group = groups.get(drop["kind"])
        if group is None:
            group = {"kind": drop["kind"], "label": drop["label"],
                     "at": drop["at"], "items": []}
            groups[drop["kind"]] = group
        group["items"].append({"title": drop["title"], "url": drop["url"]})
        group["at"] = max(group["at"], drop["at"])
    return sorted(groups.values(), key=lambda g: g["at"], reverse=True)


def _active_product_listings():
    return (
        MarketplaceListing.query
        .options(joinedload(MarketplaceListing.author),
                 joinedload(MarketplaceListing.images))
        .filter_by(active=True, kind="product")
        .all()
    )


def product_of_the_day() -> MarketplaceListing | None:
    """Stable daily pick from active Showcase digital products.

    Prefers Creator-member listings (eligibility perk); falls back to any
    active product listing so the section can still fill.
    """
    listings = _active_product_listings()
    if not listings:
        return None
    creators = [
        ln for ln in listings
        if ln.author and ln.author.has_feature("spotlight")
    ]
    pool = creators or listings
    # Sort for a stable order, then pick by day-of-year.
    pool = sorted(pool, key=lambda ln: (ln.id,))
    idx = utcnow().toordinal() % len(pool)
    return pool[idx]


def top_products(limit: int = 6) -> list[MarketplaceListing]:
    """Most-clicked active digital products, preferring the last 30 days."""
    since = utcnow() - timedelta(days=30)
    recent = (
        MarketplaceListing.query
        .options(joinedload(MarketplaceListing.author),
                 joinedload(MarketplaceListing.images))
        .filter_by(active=True, kind="product")
        .filter(MarketplaceListing.created_at >= since)
        .order_by(MarketplaceListing.clicks.desc(),
                  MarketplaceListing.created_at.desc())
        .limit(limit)
        .all()
    )
    if len(recent) >= limit:
        return recent

    seen = {ln.id for ln in recent}
    filler = (
        MarketplaceListing.query
        .options(joinedload(MarketplaceListing.author),
                 joinedload(MarketplaceListing.images))
        .filter_by(active=True, kind="product")
        .order_by(MarketplaceListing.clicks.desc(),
                  MarketplaceListing.created_at.desc())
        .limit(limit * 2)
        .all()
    )
    out = list(recent)
    for ln in filler:
        if ln.id in seen:
            continue
        out.append(ln)
        seen.add(ln.id)
        if len(out) >= limit:
            break
    return out
