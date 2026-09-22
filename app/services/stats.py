"""Dashboard statistics, computed from the local database only."""
import logging
from datetime import date, datetime, time, timedelta

from sqlalchemy import func, or_
from sqlalchemy.orm import joinedload

from ..extensions import db
from ..models import (COLLECTED_ORDER_STATUSES, ForumPost, MarketplaceListing,
                      MembershipPlan, Order, PageView, Product, ShopPurchase,
                      SiteFeedback, User, Video, VisitEvent)
from . import support_groups as sg_svc

log = logging.getLogger(__name__)

#: Who counts as a member here. Studio's stand-in accounts are furniture, not
#: people, so they stay out of every number an owner reads for a decision.
REAL_MEMBER = (User.deleted_at.is_(None), User.is_demo.is_(False))


def _dt(day: date) -> datetime:
    return datetime.combine(day, time.min)


def _money(cents: int, currency: str = "USD") -> str:
    symbol = {"USD": "$", "EUR": "\u20ac", "GBP": "\u00a3"}.get(currency, currency + " ")
    return f"{symbol}{(cents or 0) / 100:,.0f}"


def _is_stripe_ish_id(value: str) -> bool:
    v = (value or "").strip()
    return v.startswith(("price_", "prod_", "pdt_", "cs_", "pi_", "sub_"))


def _exclude_test_sales(query):
    """Drop buys of owners-only test products.

    An owner buying their own test product is a dry run, not trade, so it must
    not show up as revenue. Orders point at a product either by foreign key or
    by the Stripe price id, and both routes have to be covered. The NULL checks
    matter: ``NOT IN`` on a NULL column is NULL, which would silently throw
    away every membership order along with the test ones.
    """
    rows = Product.query.filter(Product.test_mode.is_(True)).all()
    if not rows:
        return query
    ids = [r.id for r in rows]
    # Every price the dry run has ever been charged at, not just today's —
    # a test product whose price was changed would otherwise start counting
    # its older rehearsals as takings.
    prices = sorted({key for r in rows for key in r.price_keys()})
    query = query.filter(or_(Order.product_id.is_(None),
                             Order.product_id.notin_(ids)))
    if prices:
        query = query.filter(or_(Order.ls_variant_id.is_(None),
                                 Order.ls_variant_id.notin_(prices)))
    return query


def _chart_title_maps(orders: list[Order]) -> tuple[dict[str, str], dict[str, str]]:
    """Resolve Stripe/Lemon ids → human titles for chart filters.

    Returns (variant_id → title, payment_id → title).
    """
    variants = {
        (o.ls_variant_id or "").strip()
        for o in orders
        if (o.ls_variant_id or "").strip() and not (o.product_id and o.product)
    }
    payment_ids = {
        (o.ls_order_id or "").strip()
        for o in orders
        if (o.ls_order_id or "").strip() and not (o.product_id and o.product)
    }
    by_variant: dict[str, str] = {}
    by_payment: dict[str, str] = {}
    if not variants and not payment_ids:
        return by_variant, by_payment

    if variants:
        # Read whole rather than queried by id: an order can name a price the
        # product has since stopped selling at, and those live in a JSON list
        # that no ``IN`` clause can reach. The catalogue is small.
        for p in Product.query.all():
            for key in p.price_keys():
                if key in variants and p.title:
                    by_variant.setdefault(key, p.title)

        plans = (
            MembershipPlan.query
            .filter(or_(
                MembershipPlan.stripe_price_id.in_(variants),
                MembershipPlan.stripe_price_id_annual.in_(variants),
                MembershipPlan.ls_variant_id.in_(variants),
            ))
            .all()
        )
        for plan in plans:
            label = (plan.name or "").strip() or f"{plan.tier} membership".replace("_", " ").title()
            for key in (
                (plan.stripe_price_id or "").strip(),
                (plan.stripe_price_id_annual or "").strip(),
                (plan.ls_variant_id or "").strip(),
            ):
                if key and key in variants and key not in by_variant:
                    by_variant[key] = label

    if payment_ids:
        shops = (
            ShopPurchase.query
            .filter(ShopPurchase.lemon_squeezy_order_id.in_(payment_ids))
            .all()
        )
        generic = {"", "course purchase", "shop purchase"}
        for row in shops:
            name = (row.product_name or "").strip()
            if not name or name.lower() in generic:
                continue
            pid = (row.lemon_squeezy_order_id or "").strip()
            if pid:
                by_payment[pid] = name
            vid = (row.variant_id or row.product_id or "").strip()
            if vid and vid in variants and vid not in by_variant:
                by_variant[vid] = name

    return by_variant, by_payment


def _order_chart_series(order: Order, by_variant: dict[str, str],
                        by_payment: dict[str, str]) -> tuple[str, str]:
    """Return (series_key, display_title) for one paid order."""
    if order.product_id and order.product and (order.product.title or "").strip():
        return f"p{order.product_id}", order.product.title.strip()

    variant = (order.ls_variant_id or "").strip()
    payment = (order.ls_order_id or "").strip()
    title = (
        (by_variant.get(variant) if variant else None)
        or (by_payment.get(payment) if payment else None)
    )
    if variant and title:
        return f"v{variant}", title
    if variant and _is_stripe_ish_id(variant):
        return f"v{variant}", "Unmatched product"
    if variant:
        return f"v{variant}", variant
    if title:
        return f"pay{payment}" if payment else "other", title
    return "other", "Other / unmatched"


def payment_insights(days: int = 30) -> dict:
    """Main payment numbers for Studio (not a full ledger)."""
    start = _dt(date.today() - timedelta(days=days - 1))
    revenue, count = (
        _exclude_test_sales(
            db.session.query(func.coalesce(func.sum(Order.total_cents), 0),
                             func.count(Order.id))
            .filter(Order.status.in_(COLLECTED_ORDER_STATUSES),
                    Order.created_at >= start))
        .one()
    )
    # Top products by order count in window
    top_rows = (
        db.session.query(Product.title, func.count(Order.id))
        .join(Order, Order.product_id == Product.id)
        .filter(Order.status.in_(COLLECTED_ORDER_STATUSES),
                Order.created_at >= start,
                Product.test_mode.is_(False))
        .group_by(Product.title)
        .order_by(func.count(Order.id).desc())
        .limit(3)
        .all()
    )
    sources = (
        db.session.query(VisitEvent.source, func.count(VisitEvent.id))
        .filter(VisitEvent.created_at >= start)
        .group_by(VisitEvent.source)
        .order_by(func.count(VisitEvent.id).desc())
        .limit(5)
        .all()
    )
    return {
        "revenue": _money(revenue) if count else None,
        "revenue_note": f"{count} paid order{'s' if count != 1 else ''} in {days} days",
        "orders_30d": count,
        "top_products": [{"title": t, "orders": n} for t, n in top_rows],
        "traffic_sources": [{"source": s, "count": n} for s, n in sources],
    }


def _membership_price_tiers() -> dict[str, str]:
    """Map every membership plan's Stripe/Lemon price id → its tier.

    Built once so an order whose ``membership_tier`` was never stamped (an older
    row, a renewal invoice) can still be recognised as a subscription by the
    price it was billed on — monthly *and* annual ids both point at the tier.
    """
    from .memberships import _PAID_TIERS
    out: dict[str, str] = {}
    for plan in MembershipPlan.query.all():
        if plan.tier not in _PAID_TIERS:
            continue
        for key in (plan.stripe_price_id, plan.stripe_price_id_annual,
                    plan.ls_variant_id):
            k = (key or "").strip()
            if k:
                out[k] = plan.tier
    return out


def _annual_price_ids() -> set[str]:
    ids: set[str] = set()
    for plan in MembershipPlan.query.all():
        k = (plan.stripe_price_id_annual or "").strip()
        if k:
            ids.add(k)
    return ids


def revenue_split(days: int = 30) -> dict:
    """Split collected revenue into one-time products and memberships.

    Both halves add up to the total the ledger collected in the window. Amounts
    are summed from what was actually charged on each order, so founder vs
    regular pricing, the different tiers, and monthly vs annual all fall out of
    the real numbers rather than an assumed price. Memberships handed out as a
    free perk with a product never create a paid order, so they are naturally
    absent from subscription revenue — the money for that sale sits on the
    products side, where it was earned, and is counted once.
    """
    from ..models import MEMBERSHIP_LABELS

    start = _dt(date.today() - timedelta(days=days - 1))
    orders = _exclude_test_sales(
        Order.query.filter(Order.status.in_(COLLECTED_ORDER_STATUSES),
                           Order.created_at >= start)).all()

    price_tier = _membership_price_tiers()
    annual_ids = _annual_price_ids()

    total_cents = 0
    sub_cents = 0
    sub_count = 0
    prod_count = 0
    tier_cents: dict[str, int] = {}
    annual_cents = 0
    monthly_cents = 0
    for o in orders:
        amount = int(o.total_cents or 0)
        total_cents += amount
        variant = (o.ls_variant_id or "").strip()
        tier = (o.membership_tier or "").strip().lower()
        if tier not in ("healing", "creator", "full_bloom"):
            tier = price_tier.get(variant, "")
        if tier in ("healing", "creator", "full_bloom"):
            sub_cents += amount
            sub_count += 1
            tier_cents[tier] = tier_cents.get(tier, 0) + amount
            if variant and variant in annual_ids:
                annual_cents += amount
            else:
                monthly_cents += amount
        else:
            prod_count += 1
    product_cents = total_cents - sub_cents
    product_count = max(0, len(orders) - sub_count)

    tier_rows = [
        {"tier": t, "label": MEMBERSHIP_LABELS.get(t, t.replace("_", " ").title()),
         "amount": _money(tier_cents[t])}
        for t in ("healing", "creator", "full_bloom")
        if tier_cents.get(t)
    ]

    # Who currently holds a paid tier and why — so the free perks that bring in
    # no money are visible next to the members who are actually paying.
    members = {}
    try:
        from . import membership_audit
        counts = membership_audit.audit().get("counts", {})
        members = {
            "paying": int(counts.get("order", 0)),
            "perk": int(counts.get("perk", 0)),
            "comped": int(counts.get("manual", 0)) + int(counts.get("owner", 0)),
            "unexplained": int(counts.get("unexplained", 0)),
        }
    except Exception:
        log.exception("revenue_split: membership composition failed")

    return {
        "days": days,
        "total": _money(total_cents),
        "products": {
            "amount": _money(product_cents),
            "count": product_count,
        },
        "subscriptions": {
            "amount": _money(sub_cents),
            "count": sub_count,
            "by_tier": tier_rows,
            "annual": _money(annual_cents) if annual_cents else None,
            "monthly": _money(monthly_cents) if monthly_cents else None,
            "members": members,
        },
    }


def dashboard_cards() -> dict:
    """Community metrics shown on the Studio dashboard."""
    today = date.today()
    posts_24h = db.session.query(func.count(ForumPost.id)).filter(
        ForumPost.created_at >= datetime.utcnow() - timedelta(hours=24)
    ).scalar() or 0
    posts_30d = db.session.query(func.count(ForumPost.id)).filter(
        ForumPost.created_at >= _dt(today - timedelta(days=29))
    ).scalar() or 0
    week_start = today - timedelta(days=today.weekday())
    posters_week = db.session.query(func.count(func.distinct(ForumPost.user_id))).filter(
        ForumPost.created_at >= _dt(week_start)
    ).scalar() or 0
    active_members = db.session.query(func.count(User.id)).filter(
        *REAL_MEMBER,
        User.membership.in_(("healing", "creator", "full_bloom")),
    ).scalar() or 0
    free_n = db.session.query(func.count(User.id)).filter(
        *REAL_MEMBER,
        User.membership == "none",
    ).scalar() or 0
    denom = max(active_members + free_n, 1)
    engagement = round(100 * posters_week / denom)
    avg_streak = db.session.query(func.avg(User.current_streak)).filter(
        *REAL_MEMBER,
        User.current_streak.isnot(None),
        User.current_streak > 0,
    ).scalar()
    new_week = db.session.query(func.count(User.id)).filter(
        *REAL_MEMBER,
        User.created_at >= _dt(today - timedelta(days=7)),
    ).scalar() or 0
    pay = payment_insights(30)
    return {
        "pay_split": revenue_split(30),
        "forum_posts": posts_30d,
        "forum_posts_24h": posts_24h,
        "engagement_pct": engagement,
        "posters_week": posters_week,
        "avg_streak": round(float(avg_streak or 0), 1),
        "new_members_week": new_week,
        "revenue": pay["revenue"],
        "revenue_note": pay["revenue_note"],
        "orders_30d": pay["orders_30d"],
        "top_products": pay["top_products"],
        "traffic_sources": pay["traffic_sources"],
    }


def signups_by_week(weeks: int = 12) -> dict:
    """Signups per week, bucketed in Python from one pass over the dates."""
    today = date.today()
    start = today - timedelta(weeks=weeks)
    end = start + timedelta(weeks=weeks)
    labels = [(start + timedelta(weeks=i)).isoformat() for i in range(weeks)]
    users = [0] * weeks
    rows = (db.session.query(User.created_at)
            .filter(User.created_at >= _dt(start), User.created_at < _dt(end))
            .all())
    for (created,) in rows:
        if not created:
            continue
        bucket = (created.date() - start).days // 7
        if 0 <= bucket < weeks:
            users[bucket] += 1
    return {"labels": labels, "users": users}


def membership_breakdown() -> dict:
    rows = dict(db.session.query(User.membership, func.count(User.id))
                .filter(*REAL_MEMBER).group_by(User.membership).all())
    return {
        "none": rows.get("none", 0),
        "healing": rows.get("healing", 0),
        "creator": rows.get("creator", 0),
        "full_bloom": rows.get("full_bloom", 0),
        "total": sum(rows.values()),
    }


def video_count() -> int:
    return db.session.query(func.count(Video.id)).scalar() or 0


def marketplace_counts() -> dict:
    active = db.session.query(func.count(MarketplaceListing.id)).filter(
        MarketplaceListing.active.is_(True)).scalar() or 0
    total = db.session.query(func.count(MarketplaceListing.id)).scalar() or 0
    return {"active": active, "total": total}


def most_visited(days: int = 7, limit: int = 10):
    start = date.today() - timedelta(days=days - 1)
    return db.session.query(
        PageView.path, func.sum(PageView.count).label("views")
    ).filter(PageView.date >= start).group_by(PageView.path).order_by(
        func.sum(PageView.count).desc()
    ).limit(limit).all()


def member_activity(limit: int = 12) -> list[dict]:
    """Recent purchases, signups, and traffic for the Studio activity feed."""
    out: list[dict] = []
    now = datetime.utcnow()

    paid = _exclude_test_sales(
        Order.query
        .options(joinedload(Order.product))
        .filter(Order.status.in_(COLLECTED_ORDER_STATUSES))
    ).order_by(Order.created_at.desc()).limit(max(8, limit)).all()
    # One lookup for every buyer, rather than one per order.
    buyer_emails = {(o.buyer_email or "").strip().lower()
                    for o in paid if "@" in (o.buyer_email or "")}
    names_by_email: dict[str, str] = {}
    if buyer_emails:
        for u in (User.query
                  .filter(func.lower(User.email).in_(buyer_emails),
                          User.deleted_at.is_(None))
                  .all()):
            names_by_email[(u.email or "").strip().lower()] = u.public_name()
    for o in paid:
        title = (o.product.title if o.product else None) or "a product"
        who = (o.buyer_email or "Someone").strip() or "Someone"
        display = names_by_email.get(who.lower(), who)
        amount = _money(o.total_cents or 0, o.currency or "USD")
        out.append({
            "kind": "purchase",
            "when": o.created_at,
            "name": display,
            "plan": "Purchase",
            "last_active": _relative(o.created_at, now),
            "posts": "—",
            "streak": "—",
            "summary": f"Purchased {title} · {amount}",
        })

    visits = (VisitEvent.query
              .options(joinedload(VisitEvent.user))
              .order_by(VisitEvent.created_at.desc())
              .limit(max(6, limit // 2))
              .all())
    for v in visits:
        who = "Someone"
        if v.user_id and v.user:
            who = v.user.public_name()
        path = v.path or "/"
        if path == "/":
            place = "the home page"
        elif path.startswith("/courses"):
            place = "Courses & Guides"
        elif path.startswith("/membership"):
            place = "Membership"
        else:
            place = path
        out.append({
            "kind": "visit",
            "when": v.created_at,
            "name": who,
            "plan": v.source,
            "last_active": _relative(v.created_at, now),
            "posts": "—",
            "streak": "—",
            "summary": f"Viewed {place} — arrived via {v.source}",
        })

    rows = (User.query
            .filter(*REAL_MEMBER)
            .order_by(User.created_at.desc())
            .limit(limit)
            .all())
    post_counts = dict(
        db.session.query(ForumPost.user_id, func.count(ForumPost.id))
        .filter(ForumPost.user_id.in_([u.id for u in rows] or [0]))
        .group_by(ForumPost.user_id)
        .all()
    )
    for u in rows:
        streak = int(getattr(u, "current_streak", 0) or 0)
        posts = post_counts.get(u.id, 0)
        last = getattr(u, "last_checkin_date", None)
        if isinstance(last, date) and not isinstance(last, datetime):
            last = datetime.combine(last, time.min)
        last = last or u.created_at
        out.append({
            "kind": "member",
            "when": u.created_at,
            "name": u.public_name(),
            "plan": u.membership_label(),
            "last_active": _relative(last, now),
            "posts": posts,
            "streak": streak,
            "summary": f"Joined as {u.membership_label()}",
        })

    out.sort(key=lambda r: r.get("when") or now, reverse=True)
    return out[:limit]


def purchases_over_time(days: int = 90) -> dict:
    """Daily paid-order counts for Studio charts, with per-product series."""
    days = max(7, min(int(days or 90), 180))
    today = date.today()
    start = today - timedelta(days=days - 1)
    labels = [(start + timedelta(days=i)).isoformat() for i in range(days)]
    label_index = {lab: i for i, lab in enumerate(labels)}

    paid = _exclude_test_sales(
        Order.query
        .options(joinedload(Order.product))
        .filter(Order.status.in_(COLLECTED_ORDER_STATUSES),
                Order.created_at >= _dt(start))).all()

    by_variant, by_payment = _chart_title_maps(paid)

    all_counts = [0] * days
    by_key: dict[str, list[int]] = {}
    products_meta: dict[str, str] = {}

    for o in paid:
        day = (o.created_at.date() if o.created_at else today)
        lab = day.isoformat()
        idx = label_index.get(lab)
        if idx is None:
            continue
        all_counts[idx] += 1
        key, title = _order_chart_series(o, by_variant, by_payment)
        if key not in by_key:
            by_key[key] = [0] * days
            products_meta[key] = title
        by_key[key][idx] += 1

    products = [
        {"key": k, "title": products_meta[k]}
        for k in sorted(products_meta.keys(), key=lambda x: products_meta[x].lower())
    ]
    return {
        "labels": labels,
        "all": all_counts,
        "by_product": by_key,
        "products": products,
        "total": sum(all_counts),
    }


def trending_product(window_days: int = 7) -> dict | None:
    """Product with the most paid sales in the recent window."""
    window_days = max(3, int(window_days or 7))
    start = _dt(date.today() - timedelta(days=window_days - 1))
    rows = (
        db.session.query(Product.title, func.count(Order.id).label("n"))
        .join(Order, Order.product_id == Product.id)
        .filter(Order.status.in_(COLLECTED_ORDER_STATUSES),
                Order.created_at >= start,
                Product.test_mode.is_(False))
        .group_by(Product.title)
        .order_by(func.count(Order.id).desc())
        .limit(1)
        .all()
    )
    if not rows:
        # Fall back to ShopPurchase names when Order.product_id wasn't linked.
        shop_rows = (
            db.session.query(ShopPurchase.product_name, func.count(ShopPurchase.id))
            .filter(
                ShopPurchase.status.in_(("linked", "pending_link")),
                ShopPurchase.purchased_at >= start,
                ShopPurchase.product_name.isnot(None),
                ShopPurchase.product_name != "",
                func.lower(ShopPurchase.product_name) != "course purchase",
                func.lower(ShopPurchase.product_name) != "shop purchase",
            )
            .group_by(ShopPurchase.product_name)
            .order_by(func.count(ShopPurchase.id).desc())
            .limit(1)
            .all()
        )
        if not shop_rows or not shop_rows[0][0]:
            return None
        title, n = shop_rows[0]
        return {
            "title": title,
            "sales": int(n),
            "window_days": window_days,
            "label": f"{title} is trending",
        }
    title, n = rows[0]
    return {
        "title": title,
        "sales": int(n),
        "window_days": window_days,
        "label": f"{title} is trending",
    }


def _relative(when, now=None) -> str:
    now = now or datetime.utcnow()
    if not when:
        return "—"
    if isinstance(when, date) and not isinstance(when, datetime):
        when = datetime.combine(when, time.min)
    days = (now - when).days
    if days <= 0:
        return "Today"
    if days == 1:
        return "Yesterday"
    return f"{days}d ago"


def showcase_performance(limit: int = 8) -> list[dict]:
    rows = (MarketplaceListing.query
            .options(joinedload(MarketplaceListing.author))
            .filter_by(active=True)
            .order_by(MarketplaceListing.clicks.desc())
            .limit(limit)
            .all())
    out = []
    for ln in rows:
        clicks = int(ln.clicks or 0)
        # Views aren't tracked separately; approximate with clicks * 4 + 1.
        views = max(clicks * 4, clicks, 1)
        ctr = round(100 * clicks / views, 1) if views else 0
        out.append({
            "member": ln.author.public_name() if ln.author else "Member",
            "title": ln.title,
            "views": views,
            "clicks": clicks,
            "ctr": ctr,
        })
    return out


def recent_feedback(limit: int = 6) -> list[dict]:
    rows = (SiteFeedback.query
            .order_by(SiteFeedback.created_at.desc())
            .limit(limit)
            .all())
    out = []
    for f in rows:
        out.append({
            "stars": f.stars,
            "body": (f.body or "")[:180],
            "kind": f.kind,
            "when": f.created_at,
        })
    return out


def support_occupancy() -> list[dict]:
    try:
        return sg_svc.circle_stats()
    except Exception:
        return []


def founder_days_remaining() -> int | None:
    from .settings import get_setting
    raw = (get_setting("founder_price_ends") or "").strip()
    if not raw:
        return None
    try:
        end = date.fromisoformat(raw[:10])
    except ValueError:
        return None
    return max(0, (end - date.today()).days)


