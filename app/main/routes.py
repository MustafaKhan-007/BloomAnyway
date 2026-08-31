"""Public pages."""
import logging
import os
import re
from datetime import date, datetime
from flask import (Response, abort, current_app, flash, jsonify, redirect,
                   render_template, request, send_file, url_for)
from flask_login import current_user, login_required

from ..extensions import db, limiter
from sqlalchemy import func
from sqlalchemy.orm import joinedload, selectinload

from ..models import (MARKETPLACE_KINDS, MARKETPLACE_KIND_LABELS,
                      MARKETPLACE_TAG_GROUPS, MARKETPLACE_TAG_MAX,
                      MARKETPLACE_TAGS, MOOD_KEYS, MOODS,
                      ContactMessage, FaqItem, JournalEntry,
                      ListingImage, MarketplaceListing, MembershipPlan,
                      Notification, Order, Page, Product, ProductAsset,
                      Quote, QuoteFavorite, ReelReview, ReelReviewApplication,
                      ReelSubmission,
                      ShopPurchase, Subscriber, User, Video, utcnow,
                      journal_prompt_map, random_journal_prompt,
                      sample_journal_prompts)
from ..services import quotes as quotes_service
from ..services import reel_of_week as rotw_svc
from ..services import reel_reviews as reel_svc
from ..services import settings as settings_service
from ..services.avatars import AvatarError, process_avatar
from ..services.badges import CATEGORIES, category_progress, earned_badges
from ..services.catalog import remove_demo_catalog
from ..services import stripe_pay as pay
from ..services.journey import build_journey_pdf
from ..services.mailer import send_contact_notification
from ..services.perks import perk_end_display
from ..services.recommend import INTENTS, valid_intent_keys
from ..services.listings import (ListingError, can_add_listing, listing_limit,
                                 process_listing_image)
from ..services.shop_purchases import (
    collapse_duplicate_purchases, linked_purchases_for, link_pending_purchases,
    remove_from_library,
)
from ..services.social import (instagram_embed_url, instagram_from_links,
                               instagram_handle, instagram_profile_url,
                               upsert_instagram_link)
from ..services.videos import VideoError, delete_stored, process_video
from . import bp

log = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

#: how many custom profile links a member may add
PROFILE_LINK_MAX = 5


def _collect_profile_links(form):
    """Read paired label/url inputs (link_label_N / link_url_N) into a clean list."""
    links = []
    for i in range(PROFILE_LINK_MAX):
        url = (form.get(f"link_url_{i}") or "").strip()[:300]
        label = (form.get(f"link_label_{i}") or "").strip()[:40]
        if not url:
            continue
        if not url.lower().startswith(("http://", "https://")):
            url = "https://" + url
        if not label:
            label = re.sub(r"^https?://(www\.)?", "", url).split("/")[0][:40]
        links.append({"label": label, "url": url})
    return links


def _valid_badge_choices(keys):
    """Keep up to 3 chosen badge categories the member has actually earned."""
    earned = {b["cat"] for b in earned_badges(current_user)}
    out = []
    for key in keys:
        if key in CATEGORIES and key in earned and key not in out:
            out.append(key)
        if len(out) >= 3:
            break
    return out

def _spotlight_context():
    """Creator of the Month + Reel of the Week, straight from site settings."""
    from ..services.site_images import get as get_site_image, public_path

    site = settings_service.all_settings()
    creator = None
    if (site.get("creator_name") or "").strip():
        raw_ig = (site.get("creator_instagram") or "").strip()
        handle = instagram_handle(raw_ig)
        profile = instagram_profile_url(handle) if handle else ""
        image = (site.get("creator_image_url") or "").strip()
        stored = get_site_image("creator")
        if stored is not None:
            # Prefer the uploaded Studio photo; bust browser cache on replace.
            stamp = ""
            if stored.updated_at:
                stamp = f"?v={int(stored.updated_at.timestamp())}"
            image = public_path("creator") + stamp
        creator = {
            "name": site["creator_name"].strip(),
            "instagram": profile or raw_ig,
            "handle": handle,
            "blurb": (site.get("creator_blurb") or "").strip(),
            "image": image,
        }
    reel = None
    reel_url = (site.get("reel_url") or "").strip()
    if reel_url:
        reel = {
            "url": reel_url,
            "embed": instagram_embed_url(reel_url),
            "description": (site.get("reel_description") or "").strip(),
        }
    return {"creator_of_month": creator, "reel_of_week": reel}


def _video_notice():
    """Newest published tip, only for the first day after it goes live.

    Ordering here is by date alone. Sorting by ``sort_order`` first (the hub's
    own order) meant one pinned older tip became "the latest" and swallowed the
    notice for everything published after it.
    """
    if not getattr(current_user, "is_authenticated", False):
        return None
    video = (Video.query.filter_by(published=True)
             .order_by(Video.created_at.desc()).first())
    if video is None or not video.created_at:
        return None
    # hide after ~24 hours
    age = utcnow() - video.created_at
    if age.total_seconds() > 24 * 3600:
        return None
    return video


@bp.route("/")
def index():
    from ..services import homepage as home_svc
    return render_template(
        "main/index.html",
        latest_video=_video_notice(),
        product_of_the_day=home_svc.product_of_the_day(),
        top_products=home_svc.top_products(6),
        **_spotlight_context(),
    )


HEALING_FILTERS = [
    ("all", "All"),
    ("workbook", "Workbooks"),
    ("course", "Courses"),
    ("audio", "Audio Guides"),
]
BUILDING_FILTERS = [
    ("all", "All"),
    ("guide", "Guides"),
    ("course", "Courses"),
    ("template", "Templates"),
]


def _sees_test_products() -> bool:
    """Test products are owner-only, everywhere they could show up."""
    return bool(current_user.is_authenticated and current_user.is_admin)


def _hide_test(query):
    if _sees_test_products():
        return query
    return query.filter(Product.test_mode.is_(False))


def _courses_lane(track: str, type_filter: str, sort: str):
    q = _hide_test(Product.query.filter_by(status="published", track=track)
                   .filter(Product.type != "bundle"))
    if type_filter and type_filter != "all":
        q = q.filter(Product.type == type_filter)
    rows = q.all()
    if sort == "price_asc":
        rows.sort(key=lambda p: (p.price_cents is None, p.price_cents or 0, p.sort_order, p.id))
    elif sort == "price_desc":
        rows.sort(key=lambda p: (p.price_cents is None, -(p.price_cents or 0), p.sort_order, p.id))
    else:
        rows.sort(key=lambda p: (p.sort_order, -(p.created_at.timestamp() if p.created_at else 0), p.id))
    return rows


@bp.route("/courses")
def courses():
    """On-site Courses & Guides catalogue (two founder lanes)."""
    try:
        if remove_demo_catalog():
            db.session.commit()
    except Exception:
        db.session.rollback()

    lane = (request.args.get("lane") or "").strip().lower()
    if lane not in ("healing", "building"):
        lane = None

    h_filter = (request.args.get("h") or "all").strip().lower()
    b_filter = (request.args.get("b") or "all").strip().lower()
    if h_filter not in {k for k, _ in HEALING_FILTERS}:
        h_filter = "all"
    if b_filter not in {k for k, _ in BUILDING_FILTERS}:
        b_filter = "all"
    sort = (request.args.get("sort") or "newest").strip().lower()
    if sort not in ("newest", "price_asc", "price_desc"):
        sort = "newest"

    # Always load both tracks so desktop can show them side-by-side.
    # `lane` is only a mobile focus / scroll hint.
    healing = _courses_lane("healing", h_filter, sort)
    building = _courses_lane("building", b_filter, sort)
    bundles = {
        "healing": _hide_test(Product.query.filter_by(
            status="published", track="healing", type="bundle")).first(),
        "building": _hide_test(Product.query.filter_by(
            status="published", track="building", type="bundle")).first(),
    }
    owned_purchases = {}
    if current_user.is_authenticated:
        from ..services import course_reader as reader_svc
        for purchase in linked_purchases_for(current_user):
            prod = reader_svc.catalog_product_for_purchase(purchase)
            if prod is not None:
                owned_purchases[prod.id] = purchase.id
    return render_template(
        "main/courses.html",
        healing=healing,
        building=building,
        bundles=bundles,
        healing_filters=HEALING_FILTERS,
        building_filters=BUILDING_FILTERS,
        h_filter=h_filter,
        b_filter=b_filter,
        sort=sort,
        lane=lane,
        owned_purchases=owned_purchases,
    )


@bp.route("/courses/<slug>")
def course_detail(slug):
    """Public product page — cover, copy, teasers, and buy / open CTA.

    Drafts are 404 for the public; owners can open them as a Studio preview.
    """
    try:
        if remove_demo_catalog():
            db.session.commit()
    except Exception:
        db.session.rollback()

    product = Product.query.filter_by(slug=slug).first_or_404()
    if not product.visible_to(current_user):
        abort(404)
    is_preview = False
    if product.status != "published":
        if not (current_user.is_authenticated and current_user.is_admin):
            abort(404)
        is_preview = True

    owned_purchase_id = None
    if current_user.is_authenticated and not is_preview:
        from ..services import course_reader as reader_svc
        for purchase in linked_purchases_for(current_user):
            prod = reader_svc.catalog_product_for_purchase(purchase)
            if prod and prod.id == product.id:
                owned_purchase_id = purchase.id
                break
    return render_template(
        "main/course_detail.html",
        product=product,
        owned_purchase_id=owned_purchase_id,
        is_preview=is_preview,
    )


@bp.route("/checkout/product/<slug>", methods=["GET", "POST"])
@limiter.limit("20 per minute")
def checkout_product(slug):
    product = Product.query.filter_by(slug=slug, status="published").first_or_404()
    if not product.visible_to(current_user):
        abort(404)
    pid = (product.stripe_price_id or "").strip()
    if not pid:
        flash("Checkout for this guide isn’t live yet — check back soon.", "info")
        return redirect(url_for("main.courses"))
    if not pay.configured():
        flash("Payments aren’t configured yet. Please try again later.", "error")
        return redirect(url_for("main.courses"))
    email = current_user.email if current_user.is_authenticated else None
    name = current_user.public_name() if current_user.is_authenticated else None
    try:
        url = pay.create_checkout_session(
            product_id=pid,
            return_url=url_for("main.account", tab="saved", purchased=1, _external=True),
            customer_email=email,
            customer_name=name,
            metadata={"slug": product.slug, "kind": "product"},
        )
    except pay.StripeError as exc:
        flash(str(exc), "error")
        return redirect(url_for("main.courses"))
    return redirect(url)


@bp.route("/checkout/membership/<tier>", methods=["GET", "POST"])
@limiter.limit("20 per minute")
def checkout_membership(tier):
    if tier not in ("healing", "creator", "full_bloom"):
        abort(404)
    # Stripe tells us who paid by email address, so a membership bought with
    # no account open has nowhere to land — and lands on a stranger's account
    # if they later sign up with that address. Make the account first.
    if not current_user.is_authenticated:
        flash("Make your account first, then join — a membership is tied to "
              "the account it's bought from.", "info")
        return redirect(url_for("auth.login", next=url_for("main.membership")))
    billing = (request.args.get("billing") or request.form.get("billing")
               or "monthly").strip().lower()
    if billing in ("year", "yearly", "annual"):
        billing = "annual"
    else:
        billing = "monthly"
    plan = MembershipPlan.query.filter_by(tier=tier, active=True).first()
    product_id = plan.payment_product_id(billing) if plan else None
    if plan is None or not product_id:
        flash("That membership isn’t available for checkout yet.", "info")
        return redirect(url_for("main.membership"))
    if not pay.configured():
        flash("Payments aren’t configured yet. Please try again later.", "error")
        return redirect(url_for("main.membership"))

    # Switching plans: cancel the current membership immediately, then send
    # them to Stripe Checkout for the new plan.
    if (current_user.is_authenticated
            and not current_user.is_admin
            and current_user.effective_membership() in ("healing", "creator", "full_bloom")
            and current_user.effective_membership() != tier):
        try:
            result = pay.cancel_membership_subscriptions(
                current_user.email, at_period_end=False,
            )
        except Exception:
            log.exception(
                "membership switch: cancel failed for user %s", current_user.id,
            )
            flash(
                "We couldn’t cancel your current membership. Please try again "
                "or cancel it from Settings first.",
                "error",
            )
            return redirect(url_for("main.membership"))
        current_user.membership_cancel_at = None
        from ..services.memberships import reconcile_user
        reconcile_user(current_user, downgrade=True)
        db.session.commit()
        if not result.get("ok"):
            log.warning(
                "membership switch: cancel reported errors for %s: %s",
                current_user.email, result.get("errors"),
            )
        flash(
            "Your previous membership was cancelled. Complete checkout to start "
            "the new plan.",
            "info",
        )

    email = current_user.email if current_user.is_authenticated else None
    name = current_user.public_name() if current_user.is_authenticated else None
    try:
        url = pay.create_checkout_session(
            product_id=product_id,
            return_url=url_for("main.account", purchased=1, _external=True),
            customer_email=email,
            customer_name=name,
            metadata={"tier": tier, "kind": "membership", "billing": billing},
        )
    except pay.StripeError as exc:
        flash(str(exc), "error")
        return redirect(url_for("main.membership"))
    return redirect(url)


#: fallback comparison matrix (used only if plan feature build fails)
MEMBERSHIP_MATRIX = [
    ("Buy courses & guides", True, True, True, True),
    ("Daily quotes & motivation", True, True, True, True),
    ("Earn & display badges", True, True, True, True),
    ("Healing community", False, True, False, True),
    ("Building / Creator community", False, False, True, True),
    ("Browse the Content Hub", True, True, True, True),
    ("Watch Content Hub tips", "Free picks", "Healing tips", True, True),
    ("Request a weekly reel review", False, False, True, True),
    ("Home-page spotlight eligibility", False, False, True, True),
    ("Showcase listings", False, "1 active", "5 active", "5 active"),
    ("Healing support groups / Ayesha 1:1", False, True, False, True),
    ("Creator support groups / Saman 1:1", False, False, True, True),
    ("Profile links", False, True, True, True),
    ("My Journey keepsake export", False, True, True, True),
]


def _safe_back_url(raw: str | None):
    """Same-origin path only (open-redirect safe)."""
    if not raw:
        return None
    raw = raw.strip()
    if raw.startswith("/") and not raw.startswith("//"):
        return raw
    try:
        from urllib.parse import urlparse
        here = urlparse(request.host_url)
        there = urlparse(raw)
        if there.netloc == here.netloc and there.path:
            return there.path + (f"?{there.query}" if there.query else "")
    except Exception:
        pass
    return None


def _sync_membership_cancel_flag(user) -> None:
    """Pull cancel-at-period-end from Stripe so the UI stays honest."""
    if user is None or not user.is_authenticated or user.is_admin:
        return
    if not user.is_member() or not pay.configured():
        return
    try:
        live = pay.membership_cancel_status(user.email)
    except Exception:
        log.exception("membership cancel sync failed for user %s", user.id)
        return
    changed = False
    if live.get("canceling"):
        pe = live.get("period_end")
        if pe:
            from datetime import datetime, timezone
            try:
                ends = datetime.fromtimestamp(int(pe), tz=timezone.utc).replace(tzinfo=None)
            except (TypeError, ValueError, OSError):
                ends = None
            if ends and user.membership_cancel_at != ends:
                user.membership_cancel_at = ends
                changed = True
        elif user.membership_cancel_at is None:
            pay.apply_cancel_at_to_user(user, None)
            changed = True
    elif user.membership_cancel_at is not None and not live.get("canceling"):
        # Stripe says renewing again (or sub gone) — clear stale local flag only
        # when they still have a paid membership (revoke webhook clears on end).
        user.membership_cancel_at = None
        changed = True
    if changed:
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            log.exception("membership cancel flag commit failed for user %s", user.id)


@bp.route("/membership")
def membership():
    from ..services.plan_features import build_membership_matrix

    if current_user.is_authenticated:
        from ..services.memberships import reconcile_user
        if reconcile_user(current_user):
            db.session.commit()
        _sync_membership_cancel_flag(current_user)

    all_plans = {p.tier: p for p in MembershipPlan.query.all()}
    current = (current_user.effective_membership()
               if current_user.is_authenticated else None)
    checkout = {"monthly": {}, "annual": {}}
    for tier, plan in all_plans.items():
        for billing in ("monthly", "annual"):
            if plan and plan.active and plan.is_buyable(billing):
                checkout[billing][tier] = url_for(
                    "main.checkout_membership", tier=tier, billing=billing)
            else:
                checkout[billing][tier] = None
    back_url = (_safe_back_url(request.args.get("next"))
                or _safe_back_url(request.referrer)
                or url_for("main.index"))
    if back_url.rstrip("/") == url_for("main.membership").rstrip("/"):
        back_url = url_for("main.index")
    # Friendly label from the path
    if back_url.startswith("/account"):
        back_label = "My space"
    elif back_url.startswith("/forums"):
        back_label = "the community"
    elif back_url.startswith("/watch"):
        back_label = "the Content Hub"
    elif back_url.startswith("/showcase") or back_url.startswith("/marketplace"):
        back_label = "Showcase"
    else:
        back_label = "where you were"
    try:
        matrix = build_membership_matrix(all_plans)
    except Exception:
        matrix = MEMBERSHIP_MATRIX
    from ..services import founder_pricing as founder_svc
    founder = founder_svc.public_state(all_plans)
    return render_template("main/membership.html",
                           plans=all_plans,
                           matrix=matrix, current=current,
                           checkout=checkout,
                           founder=founder,
                           back_url=back_url, back_label=back_label)


CHALLENGE_ENROLL_URL = "https://stan.store/hustlinmommazbiz/p/2-month-challenge"


@bp.route("/challenge")
def challenge():
    """2-month Creator Challenge landing."""
    return render_template(
        "main/challenge.html",
        challenge_enroll_url=CHALLENGE_ENROLL_URL,
    )


# --- marketplace (member adverts; we redirect out, we don't sell) ----------

MARKETPLACE_SORTS = {"popular": "Most popular", "new": "Newest"}


def _showcase_index():
    kind = request.args.get("kind")
    if kind not in MARKETPLACE_KINDS:
        kind = None
    q = (request.args.get("q") or "").strip()
    tag = (request.args.get("tag") or "").strip()
    location = (request.args.get("location") or "").strip()
    sort = request.args.get("sort", "popular")
    if sort not in MARKETPLACE_SORTS:
        sort = "popular"
    view = "list" if request.args.get("view") == "list" else "tiles"

    # One pass over active listings: filter, tag catalogue, and locations.
    # Authors and thumbnails come along for the ride — every card shows both,
    # and lazy-loading them meant two queries per listing on the page.
    active = (MarketplaceListing.query
              .options(joinedload(MarketplaceListing.author),
                       selectinload(MarketplaceListing.images))
              .filter_by(active=True)
              .all())
    used = set()
    locations = set()
    listings = []
    q_lower = q.lower()
    loc_lower = location.lower()
    for ln in active:
        tags = ln.tags()
        if kind is None or ln.kind == kind:
            used.update(tags)
        loc = (ln.location or "").strip()
        if ln.kind == "service" and loc:
            locations.add(loc)
        if kind and ln.kind != kind:
            continue
        if q_lower and q_lower not in (ln.title or "").lower() \
                and q_lower not in (ln.description or "").lower():
            continue
        if location and loc_lower not in (ln.location or "").lower():
            continue
        if tag and tag not in tags:
            continue
        listings.append(ln)

    if sort == "new":
        listings.sort(key=lambda ln: ln.created_at or datetime.min, reverse=True)
    else:
        listings.sort(key=lambda ln: (ln.clicks or 0, ln.created_at or datetime.min),
                      reverse=True)

    all_tags = list(MARKETPLACE_TAGS) + sorted(used - set(MARKETPLACE_TAGS))
    return render_template("marketplace/index.html", listings=listings,
                           kind=kind, kinds=MARKETPLACE_KIND_LABELS, q=q, tag=tag,
                           location=location, sort=sort, sorts=MARKETPLACE_SORTS,
                           view=view, all_tags=all_tags,
                           locations=sorted(locations, key=str.lower))


@bp.route("/showcase")
def showcase():
    return _showcase_index()


@bp.route("/marketplace")
def marketplace():
    return _showcase_index()


@bp.route("/marketplace/l/<int:listing_id>")
def listing_detail(listing_id):
    ln = db.session.get(MarketplaceListing, listing_id)
    if ln is None or not ln.active:
        abort(404)
    return render_template("marketplace/detail.html", listing=ln)


@bp.route("/marketplace/image/<int:image_id>")
def listing_image(image_id):
    img = db.session.get(ListingImage, image_id)
    if img is None:
        abort(404)
    resp = Response(bytes(img.data), mimetype=img.mime or "image/jpeg")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@bp.route("/marketplace/go/<int:listing_id>")
def listing_go(listing_id):
    ln = db.session.get(MarketplaceListing, listing_id)
    if ln is None or not ln.active:
        abort(404)
    url = ln.website_url or ""
    if not url.lower().startswith(("http://", "https://")):
        abort(404)
    ln.clicks = (ln.clicks or 0) + 1
    db.session.commit()
    return redirect(url)


@bp.route("/marketplace/mine")
@login_required
def my_listings():
    if not current_user.is_member():
        flash("The marketplace is a members' perk \u2014 join to advertise your "
              "products and services.", "info")
        return redirect(url_for("main.membership"))
    mine = (MarketplaceListing.query.filter_by(user_id=current_user.id)
            .order_by(MarketplaceListing.active.desc(),
                      MarketplaceListing.created_at.desc()).all())
    back_from = (request.args.get("from") or "").strip().lower()
    if back_from not in ("myspace", "showcase"):
        # Referrer hint when opened without an explicit from=
        ref = (request.referrer or "")
        if "/account" in ref or "/settings" in ref:
            back_from = "myspace"
        else:
            back_from = "showcase"
    return render_template("marketplace/mine.html", listings=mine,
                           limit=listing_limit(current_user),
                           can_add=can_add_listing(current_user),
                           back_from=back_from)


_TAG_LOOKUP = {t.lower(): t for t in MARKETPLACE_TAGS}


def _collect_listing_tags(form):
    """Pick tags from the checklist + optional custom comma list (capped)."""
    seen, out = set(), []
    for raw in form.getlist("tags"):
        key = (raw or "").strip().lower()
        if not key or key in seen:
            continue
        # prefer the curated spelling when it matches
        label = _TAG_LOOKUP.get(key) or (raw or "").strip()[:40]
        if not label:
            continue
        seen.add(key)
        out.append(label)
        if len(out) >= MARKETPLACE_TAG_MAX:
            return out
    for part in (form.get("tags_custom") or "").replace("\n", ",").split(","):
        t = part.strip()[:40]
        key = t.lower()
        if not t or key in seen:
            continue
        seen.add(key)
        out.append(_TAG_LOOKUP.get(key, t))
        if len(out) >= MARKETPLACE_TAG_MAX:
            break
    return out


@bp.route("/marketplace/new", methods=["GET", "POST"])
@bp.route("/marketplace/<int:listing_id>/edit", methods=["GET", "POST"])
@login_required
def listing_form(listing_id=None):
    if not current_user.is_member():
        flash("The marketplace is a members' perk \u2014 join to advertise here.", "info")
        return redirect(url_for("main.membership"))

    listing = None
    if listing_id:
        listing = db.session.get(MarketplaceListing, listing_id)
        if listing is None or listing.user_id != current_user.id:
            abort(404)

    if request.method == "POST":
        kind = request.form.get("kind")
        if kind not in MARKETPLACE_KINDS:
            kind = "product"
        title = (request.form.get("title") or "").strip()[:140]
        description = (request.form.get("description") or "").strip()
        website = (request.form.get("website_url") or "").strip()[:500]
        price = (request.form.get("price") or "").strip()[:80] or None
        location = (request.form.get("location") or "").strip()[:120] or None
        tags = _collect_listing_tags(request.form)

        errors = []
        if not title:
            errors.append("Give your listing a title.")
        if not website.lower().startswith(("http://", "https://")):
            if website and not website.startswith(("http://", "https://")):
                website = "https://" + website
            if not website:
                errors.append("Add the link where people can find it.")
        if kind == "product":
            location = None
        elif kind == "service" and not location:
            errors.append("Add a location for your service (city, region, or Remote).")

        # tier limit only matters when creating (or reactivating) a live listing
        if listing is None and not can_add_listing(current_user):
            lim = listing_limit(current_user)
            errors.append(
                f"Your plan allows {lim} active listing{'s' if lim != 1 else ''}. "
                "Upgrade to Creator or Full Bloom for up to 5 listings, or remove one first.")

        new_images = []
        if not errors:
            for f in request.files.getlist("images"):
                if f and f.filename:
                    try:
                        new_images.append(process_listing_image(f))
                    except ListingError as exc:
                        errors.append(str(exc))

        if errors:
            for e in errors:
                flash(e, "error")
        else:
            is_new = listing is None
            if listing is None:
                listing = MarketplaceListing(user_id=current_user.id)
                db.session.add(listing)
            listing.kind = kind
            listing.title = title
            listing.description = description
            listing.website_url = website
            listing.price = price
            listing.location = location
            listing.set_tags(tags)
            for img_id in request.form.getlist("remove_image"):
                img = db.session.get(ListingImage, int(img_id)) if img_id.isdigit() else None
                if img and img.listing_id == listing.id:
                    db.session.delete(img)
            start = len(listing.images)
            for i, (data, mime) in enumerate(new_images):
                listing.images.append(ListingImage(data=data, mime=mime, sort_order=start + i))
            db.session.flush()
            if is_new:
                from ..services.social_graph import notify_followers_of_listing
                notify_followers_of_listing(current_user, listing)
            db.session.commit()
            flash("Listing saved. It's live in the marketplace.", "success")
            back_from = (request.form.get("from") or request.args.get("from") or "").strip().lower()
            if back_from == "myspace":
                return redirect(url_for("main.my_listings", **{"from": "myspace"}))
            return redirect(url_for("main.my_listings"))

    chosen = set(listing.tags()) if listing else set()
    # keep any custom tags the listing already has, even if not in the catalogue
    custom_existing = [t for t in chosen if t not in MARKETPLACE_TAGS]
    back_from = (request.args.get("from") or "").strip().lower()
    if back_from not in ("myspace", "showcase"):
        back_from = "showcase"
    return render_template("marketplace/form.html", listing=listing,
                           kinds=MARKETPLACE_KIND_LABELS,
                           tag_groups=MARKETPLACE_TAG_GROUPS,
                           tag_max=MARKETPLACE_TAG_MAX,
                           chosen_tags=chosen,
                           tags_custom=", ".join(custom_existing),
                           back_from=back_from)


@bp.route("/marketplace/<int:listing_id>/delete", methods=["POST"])
@login_required
def listing_delete(listing_id):
    listing = db.session.get(MarketplaceListing, listing_id)
    if listing is None or listing.user_id != current_user.id:
        abort(404)
    db.session.delete(listing)
    db.session.commit()
    flash("Listing removed.", "success")
    back_from = (request.form.get("from") or request.args.get("from") or "").strip().lower()
    if back_from == "myspace":
        return redirect(url_for("main.my_listings", **{"from": "myspace"}))
    return redirect(url_for("main.my_listings"))


@bp.route("/about")
def about():
    page = Page.query.filter_by(slug="about").first()
    return render_template("main/about.html", page=page)


@bp.route("/quotes")
def quotes():
    today = date.today()
    # Visitors see only today's quote. The archive is a member perk, and it
    # only goes back as far as the day their account was created.
    if not current_user.is_authenticated:
        q = quotes_service.quote_for(today)
        recent = [(today, q)] if q else []
        return render_template("main/quotes.html", recent=recent, today=today,
                               favorite_ids=set())

    created = (current_user.created_at.date()
               if current_user.created_at else today)
    days = max(1, min((today - created).days + 1, 366))
    recent = quotes_service.recent_quotes(days, today=today)
    favorite_ids = {f.quote_id for f in current_user.favorites}
    return render_template("main/quotes.html", recent=recent, today=today,
                           favorite_ids=favorite_ids)


@bp.route("/quotes/<int:quote_id>/favorite", methods=["POST"])
@login_required
def toggle_favorite(quote_id):
    quote = db.session.get(Quote, quote_id) or abort(404)
    existing = QuoteFavorite.query.filter_by(user_id=current_user.id, quote_id=quote.id).first()
    if existing:
        db.session.delete(existing)
    else:
        db.session.add(QuoteFavorite(user_id=current_user.id, quote_id=quote.id))
    db.session.commit()
    return redirect(request.form.get("next") or url_for("main.quotes"))


@bp.route("/account")
@login_required
def account():
    # Attach any pending Stripe purchases that match this email (guest checkout → later signup).
    if link_pending_purchases(current_user):
        db.session.commit()
    # Keep tier in sync with paid membership orders (fixes stale Full Bloom, etc.).
    from ..services.memberships import reconcile_user
    if reconcile_user(current_user):
        db.session.commit()
    _sync_membership_cancel_flag(current_user)

    from ..services.timefmt import greeting as _greeting
    greeting = _greeting(current_user.first_name())

    tab = (request.args.get("tab") or "profile").strip().lower()
    if tab not in ("profile", "saved", "journal", "activity", "settings"):
        tab = "profile"
    if tab == "settings":
        return redirect(url_for("main.settings"))

    # After checkout return, fulfill the exact session (incl. $0 / 100% off)
    # and sync recent payments so My space updates even if the webhook lagged.
    session_id = (request.args.get("session_id") or "").strip()
    if (request.args.get("purchased") or session_id) and pay.configured():
        try:
            if session_id:
                pay.fulfill_checkout_session_id(session_id)
            pay.sync_recent_payments(days=14, max_pages=1)
            link_pending_purchases(current_user)
            db.session.commit()
        except Exception:
            log.exception("account: purchase sync after checkout failed")
            db.session.rollback()

    orders = (Order.query
              .filter(func.lower(Order.buyer_email) == current_user.email.lower())
              .order_by(Order.created_at.desc()).all())
    favorites = (db.session.query(Quote).join(QuoteFavorite)
                 .filter(QuoteFavorite.user_id == current_user.id)
                 .order_by(QuoteFavorite.created_at.desc()).all())
    journal = (JournalEntry.query.filter_by(user_id=current_user.id)
               .order_by(JournalEntry.day.desc(), JournalEntry.created_at.desc(),
                         JournalEntry.id.desc())
               .limit(80).all())
    notes = (Notification.query.filter_by(user_id=current_user.id)
             .order_by(Notification.created_at.desc()).limit(40).all())
    # Mark visible activity as read
    if tab == "activity":
        for n in notes:
            if n.read_at is None:
                n.read_at = utcnow()
        db.session.commit()
    from ..services.social_graph import follow_counts, unread_notification_count
    from ..services import support_groups as sg_svc
    from ..services.listings import active_listing_count, listing_limit
    from ..services.participation import (community_participation_count,
                                          participation_bar_pct)
    followers_n, following_n = follow_counts(current_user)
    my_listings = []
    if current_user.is_member():
        from ..models import MarketplaceListing
        my_listings = (MarketplaceListing.query
                       .filter_by(user_id=current_user.id, active=True)
                       .order_by(MarketplaceListing.created_at.desc())
                       .limit(5).all())
    today_entry = next((e for e in journal if e.day == date.today()), None)
    participation_n = community_participation_count(current_user)
    from ..services import course_reader as reader_svc
    # Collapse repeated test checkouts of the same guide into one library card.
    if collapse_duplicate_purchases(current_user):
        db.session.commit()
    shop_list = linked_purchases_for(current_user, dedupe=True)
    progress_by_purchase = reader_svc.progress_map_for(
        current_user.id, [p.id for p in shop_list])
    readable = {}
    purchase_catalog = {}
    for p in shop_list:
        prod = reader_svc.catalog_product_for_purchase(p)
        purchase_catalog[p.id] = prod
        readable[p.id] = bool(prod and prod.has_assets())
    return render_template(
        "main/account.html", greeting=greeting, orders=orders,
        favorites=favorites,
        shop_purchases=shop_list,
        course_progress=progress_by_purchase,
        course_readable=readable,
        purchase_catalog=purchase_catalog,
        premium=current_user.has_feature("journey_export"),
        plan_perks=(
            MembershipPlan.query.filter_by(
                tier=current_user.effective_membership()).first()
        ),
        perk_until=perk_end_display(current_user),
        active_tab=tab,
        journal_entries=journal,
        today_entry=today_entry,
        today_prompt=random_journal_prompt(),
        prompt_ideas=sample_journal_prompts(4),
        moods=MOODS,
        community_participation=participation_n,
        community_participation_pct=participation_bar_pct(participation_n),
        notifications=notes,
        unread_notes=unread_notification_count(current_user),
        followers_n=followers_n,
        following_n=following_n,
        my_listings=my_listings,
        listing_count=active_listing_count(current_user),
        listing_cap=listing_limit(current_user),
        support_upcoming=sg_svc.upcoming_for_user(current_user, limit=6),
    )


@bp.route("/account/notifications/read", methods=["POST"])
@login_required
def mark_notifications_read():
    """Mark all unread notifications as read (used by the nav bell)."""
    from ..services.social_graph import mark_notifications_read as mark_read
    n = mark_read(current_user)
    wants_json = (
        request.accept_mimetypes.best == "application/json"
        or request.headers.get("X-Requested-With") == "fetch"
        or request.is_json
    )
    if wants_json:
        return {"ok": True, "marked": n}
    return redirect(url_for("main.account", tab="activity"))


@bp.route("/account/shop/<int:purchase_id>/remove", methods=["POST"])
@login_required
def shop_remove(purchase_id):
    """Hide a library item (e.g. test checkouts / wrong product). Does not refund Stripe."""
    if remove_from_library(current_user, purchase_id):
        db.session.commit()
        flash("Removed from your library.", "success")
    else:
        flash("Could not remove that item.", "error")
    return redirect(url_for("main.account", tab="saved"))


@bp.route("/account/shop/<int:purchase_id>/download")
@login_required
def shop_download(purchase_id):
    """Serve a self-hosted shop file only to the purchaser who owns it."""
    purchase = db.session.get(ShopPurchase, purchase_id)
    if (purchase is None
            or purchase.user_id != current_user.id
            or purchase.status != "linked"
            or not purchase.file_key):
        abort(404)
    # file_key is a basename only — never allow path traversal
    key = os.path.basename(purchase.file_key.strip())
    if not key or key != purchase.file_key.strip():
        abort(404)
    directory = os.path.abspath(current_app.config["SHOP_FILES_DIR"])
    path = os.path.abspath(os.path.join(directory, key))
    if not path.startswith(directory + os.sep) and path != directory:
        abort(404)
    if not os.path.isfile(path):
        abort(404)
    return send_file(path, as_attachment=True, download_name=key)


@bp.route("/account/courses/<int:purchase_id>")
@login_required
def course_reader(purchase_id):
    """Themed on-site reader for a purchased course/guide."""
    from ..services import course_reader as reader_svc

    from ..services import drip as drip_svc

    purchase = reader_svc.owned_purchase(current_user, purchase_id)
    if purchase is None:
        abort(404)
    product = reader_svc.catalog_product_for_purchase(purchase)
    modules = drip_svc.module_rows(product, purchase.purchased_at)
    wanted_item = request.args.get("item", type=int)
    # Files with no module are open from day one, even on a drip schedule.
    extras = [a for a in (product.assets if product else []) if not a.module_index]
    chosen = reader_svc.open_module(modules, request.args.get("module", type=int))
    module_items = []
    picked_extra = next((a for a in extras if a.id == wanted_item), None)
    if picked_extra is not None:
        asset = picked_extra
        active_module = 0
    elif chosen is not None:
        module_items = chosen["contents"]
        asset = reader_svc.open_item(chosen, wanted_item)
        active_module = chosen["number"]
    else:
        # No module contents (or none ready yet) — fall back to the plain file.
        asset = reader_svc.general_asset(product)
        if asset is None and not any(m["contents"] for m in modules):
            asset = reader_svc.primary_asset(product)
        active_module = 0
    progress = reader_svc.get_progress(current_user.id, purchase.id)
    bookmarks = progress.bookmarks() if progress else []
    resuming = progress is not None and (progress.module_index or 0) == active_module
    download_url = None
    if asset:
        download_url = url_for(
            "main.course_file", purchase_id=purchase.id, asset_id=asset.id, download=1)
    elif purchase.download_url:
        download_url = purchase.download_url
    elif purchase.file_key:
        download_url = url_for("main.shop_download", purchase_id=purchase.id)
    return render_template(
        "main/course_reader.html",
        purchase=purchase,
        product=product,
        asset=asset,
        modules=modules,
        module_items=module_items,
        extras=extras,
        active_module=active_module,
        next_locked=drip_svc.next_locked(modules),
        progress=progress,
        bookmarks=bookmarks,
        download_url=download_url,
        start_page=(progress.current_page if resuming and progress.current_page else 1),
        start_percent=(progress.percent if resuming else 0),
    )


@bp.route("/account/courses/<int:purchase_id>/file/<int:asset_id>")
@login_required
def course_file(purchase_id, asset_id):
    """Stream a course file inline to the owner (PDF.js / media / etc.)."""
    from ..services import course_reader as reader_svc

    purchase = reader_svc.owned_purchase(current_user, purchase_id)
    if purchase is None:
        abort(404)
    from ..services import drip as drip_svc

    product = reader_svc.catalog_product_for_purchase(purchase)
    asset = db.session.get(ProductAsset, asset_id)
    if product is None or asset is None or asset.product_id != product.id:
        abort(404)
    if not drip_svc.asset_unlocked(product, asset, purchase.purchased_at):
        abort(404)
    from ..services import assets as asset_svc

    raw_name = (asset.filename or "file").replace('"', "")
    as_download = (request.args.get("download") or "").strip().lower() in ("1", "true", "yes")
    disposition = "attachment" if as_download else "inline"
    mime = asset.mime or "application/octet-stream"

    if asset.body is not None and not asset.disk_name:
        resp = Response(asset.body, mimetype=mime)
    elif asset.disk_name:
        path = asset_svc.disk_path(asset.disk_name)
        if not os.path.isfile(path):
            log.error("Course asset %s missing on disk: %s", asset.id, path)
            abort(404)
        # conditional=True turns on HTTP Range, so a lesson video can be
        # scrubbed instead of downloading in full before it plays.
        resp = send_file(path, mimetype=mime, conditional=True,
                         as_attachment=False, download_name=raw_name)
        resp.headers["Accept-Ranges"] = "bytes"
    else:  # uploaded before files moved to the disk
        resp = Response(bytes(asset.data or b""), mimetype=mime)
        resp.headers["Accept-Ranges"] = "none"
    resp.headers["Content-Disposition"] = f'{disposition}; filename="{raw_name}"'
    resp.headers["Cache-Control"] = "private, max-age=300"
    return resp


@bp.route("/account/courses/<int:purchase_id>/h5p/<int:asset_id>/<path:filename>")
@login_required
def course_h5p_file(purchase_id, asset_id, filename):
    """Serve extracted H5P package files to the purchase owner."""
    from ..services import course_reader as reader_svc

    purchase = reader_svc.owned_purchase(current_user, purchase_id)
    if purchase is None:
        abort(404)
    from ..services import drip as drip_svc

    product = reader_svc.catalog_product_for_purchase(purchase)
    asset = db.session.get(ProductAsset, asset_id)
    if (product is None or asset is None or asset.product_id != product.id
            or asset.kind != "h5p"):
        abort(404)
    if not drip_svc.asset_unlocked(product, asset, purchase.purchased_at):
        abort(404)
    try:
        reader_svc.ensure_h5p_extracted(asset)
    except Exception:
        log.exception("h5p extract failed for asset %s", asset_id)
        abort(500)
    path = reader_svc.safe_h5p_file(asset_id, filename)
    if path is None:
        abort(404)
    return send_file(path)


@bp.route("/account/courses/<int:purchase_id>/progress", methods=["POST"])
@login_required
@limiter.limit("120 per minute")
def course_progress(purchase_id):
    """Save reading position (page + percent) for resume later."""
    from ..services import course_reader as reader_svc

    purchase = reader_svc.owned_purchase(current_user, purchase_id)
    if purchase is None:
        abort(404)
    product = reader_svc.catalog_product_for_purchase(purchase)
    payload = request.get_json(silent=True) or {}
    try:
        page = int(payload.get("page") or payload.get("current_page") or 1)
        total = int(payload.get("total") or payload.get("total_pages") or 0)
    except (TypeError, ValueError):
        return {"ok": False, "error": "bad page"}, 400
    # Allow percent override for non-paged kinds (H5P).
    percent_raw = payload.get("percent")
    row = reader_svc.save_progress(
        user_id=current_user.id,
        purchase_id=purchase.id,
        product_id=product.id if product else None,
        current_page=page,
        total_pages=total,
        module_index=request.args.get("module", type=int),
    )
    if percent_raw is not None and (not total or total <= 1):
        try:
            row.percent = max(0, min(100, int(percent_raw)))
        except (TypeError, ValueError):
            pass
    db.session.commit()
    return {
        "ok": True,
        "page": row.current_page,
        "total": row.total_pages,
        "percent": row.percent,
    }


@bp.route("/account/courses/<int:purchase_id>/bookmarks", methods=["POST"])
@login_required
@limiter.limit("60 per minute")
def course_bookmarks(purchase_id):
    """Toggle a bookmarked page for this purchase."""
    from ..services import course_reader as reader_svc

    purchase = reader_svc.owned_purchase(current_user, purchase_id)
    if purchase is None:
        abort(404)
    product = reader_svc.catalog_product_for_purchase(purchase)
    payload = request.get_json(silent=True) or {}
    try:
        page = int(payload.get("page") or 1)
    except (TypeError, ValueError):
        return {"ok": False, "error": "bad page"}, 400
    row, bookmarked = reader_svc.toggle_bookmark(
        user_id=current_user.id,
        purchase_id=purchase.id,
        product_id=product.id if product else None,
        page=page,
    )
    db.session.commit()
    return {
        "ok": True,
        "page": page,
        "bookmarked": bookmarked,
        "bookmarks": row.bookmarks(),
    }


@bp.route("/media/site/<key>")
def site_image(key):
    """Serve an owner-uploaded site image (hero / story teaser)."""
    from ..services.site_images import get as get_site_image
    row = get_site_image(key)
    if row is None or not row.data:
        abort(404)
    resp = Response(row.data, mimetype=row.mime or "image/jpeg")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


def _test_product_hidden(product_id: int) -> bool:
    """Guessing an id shouldn't leak artwork from an owners-only product."""
    if _sees_test_products():
        return False
    product = db.session.get(Product, product_id)
    return product is not None and product.test_mode


@bp.route("/media/product-gallery/<int:product_id>/<path:filename>")
def product_gallery_image(product_id, filename):
    """Serve a product teaser / gallery image (DB-backed)."""
    from ..services.product_covers import gallery_bytes
    if _test_product_hidden(product_id):
        abort(404)
    row = gallery_bytes(product_id, filename)
    if row is None:
        abort(404)
    data, mime = row
    resp = Response(data, mimetype=mime or "image/jpeg")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@bp.route("/media/product-cover/<int:product_id>")
def product_cover(product_id):
    """Serve an optional product cover image (DB-backed)."""
    from ..services.product_covers import cover_bytes
    if _test_product_hidden(product_id):
        abort(404)
    row = cover_bytes(product_id)
    if row is None:
        abort(404)
    data, mime = row
    resp = Response(data, mimetype=mime or "image/jpeg")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@bp.route("/account/journey.pdf")
@login_required
def journey_pdf():
    if not current_user.has_feature("journey_export"):
        flash("The My Journey keepsake isn’t included in your plan.", "info")
        return redirect(url_for("main.account"))
    pdf_bytes = build_journey_pdf(current_user)
    stamp = date.today().isoformat()
    resp = Response(pdf_bytes, mimetype="application/pdf")
    resp.headers["Content-Disposition"] = f'attachment; filename="my-journey-{stamp}.pdf"'
    resp.headers["Cache-Control"] = "private, no-store"
    return resp


@bp.route("/account/settings")
@login_required
def settings():
    from ..services.memberships import reconcile_user
    if reconcile_user(current_user):
        db.session.commit()
    _sync_membership_cancel_flag(current_user)
    links = current_user.links()
    return render_template("main/settings.html", intents=INTENTS,
                           user_goals=set(current_user.goals()),
                           links=links,
                           link_max=PROFILE_LINK_MAX,
                           can_link=current_user.has_feature("profile_links"),
                           creator_instagram=instagram_from_links(links),
                           badge_progress=category_progress(current_user),
                           chosen_badges=set(current_user.displayed_badges()))


@bp.route("/account/membership/cancel", methods=["POST"])
@login_required
def cancel_membership():
    next_page = (request.form.get("next") or "").strip().lower()
    dest = url_for("main.membership") if next_page == "membership" else url_for("main.settings")

    if current_user.is_admin:
        flash("The owner account always keeps Full Bloom access.", "info")
        return redirect(dest)
    if current_user.membership == "none":
        flash("You're on the free plan already.", "info")
        return redirect(dest)
    if current_user.membership_is_canceling():
        flash(
            f"Your membership is already set to end on "
            f"{current_user.membership_access_end_display() or 'the billing period end'}.",
            "info",
        )
        return redirect(dest)

    from ..models import MembershipPlan
    from ..services.mailer import send_membership_cancelled

    tier = current_user.membership or "none"
    plan = MembershipPlan.query.filter_by(tier=tier).first()
    plan_name = (
        (plan.name if plan else None)
        or current_user.membership_label()
        or "Membership"
    )

    stripe_result = {"ok": True, "cancelled": [], "errors": [], "period_end": None}
    if pay.configured():
        try:
            # Keep plan access until the paid period ends; Stripe stops renewals.
            stripe_result = pay.cancel_membership_subscriptions(
                current_user.email, at_period_end=True,
            )
        except Exception:
            log.exception(
                "Stripe membership cancel failed for user %s", current_user.id,
            )
            stripe_result = {
                "ok": False,
                "cancelled": [],
                "errors": ["exception"],
                "period_end": None,
            }

    access_end = pay.format_access_end_date(stripe_result.get("period_end"))
    if not access_end:
        # Fall back to live Stripe status if the cancel response omitted the date.
        live = pay.membership_cancel_status(current_user.email)
        if live.get("period_end"):
            stripe_result["period_end"] = live["period_end"]
            access_end = live.get("access_end") or ""
    if not access_end:
        access_end = "the end of your billing period"

    if stripe_result.get("cancelled") or not pay.configured():
        pay.apply_cancel_at_to_user(current_user, stripe_result.get("period_end"))
        db.session.commit()
    elif stripe_result.get("ok") and not stripe_result.get("cancelled"):
        # No live Stripe sub found — treat as cancelled locally so UI updates.
        pay.apply_cancel_at_to_user(current_user, stripe_result.get("period_end"))
        db.session.commit()

    try:
        send_membership_cancelled(
            current_user.email,
            plan_name=plan_name,
            access_end_date=access_end,
        )
    except Exception:
        log.exception("Cancel email failed for user %s", current_user.id)

    if stripe_result.get("cancelled"):
        flash(
            f"Your membership is cancelled. You keep {plan_name} access until "
            f"{access_end}, then it will be revoked. You will not be charged again.",
            "success",
        )
    elif not pay.configured():
        flash(
            f"Your membership is cancelled. Access continues until {access_end}.",
            "success",
        )
    else:
        flash(
            "We couldn't confirm the cancel with Stripe yet. "
            "If renewals continue, contact us and we'll sort it out.",
            "error",
        )
    return redirect(dest)


@bp.route("/account/membership/resume", methods=["POST"])
@login_required
def resume_membership():
    next_page = (request.form.get("next") or "").strip().lower()
    dest = url_for("main.membership") if next_page == "membership" else url_for("main.settings")

    if current_user.is_admin:
        flash("The owner account always keeps Full Bloom access.", "info")
        return redirect(dest)
    if not current_user.is_member():
        flash("You're on the free plan — nothing to resume.", "info")
        return redirect(dest)
    if not current_user.membership_is_canceling():
        # Still try Stripe in case local flag is stale.
        live = pay.membership_cancel_status(current_user.email) if pay.configured() else {}
        if not live.get("canceling"):
            flash("Your membership isn't scheduled to end.", "info")
            return redirect(dest)

    result = {"ok": True, "resumed": [], "errors": []}
    if pay.configured():
        try:
            result = pay.resume_membership_subscriptions(current_user.email)
        except Exception:
            log.exception(
                "Stripe membership resume failed for user %s", current_user.id,
            )
            result = {"ok": False, "resumed": [], "errors": ["exception"]}

    if result.get("resumed") or (result.get("ok") and not result.get("errors")):
        current_user.membership_cancel_at = None
        db.session.commit()
        flash(
            "Welcome back — your membership will renew as usual. "
            "You won't lose access at the end of this period.",
            "success",
        )
    else:
        flash(
            "We couldn't resume your subscription with Stripe. "
            "Try again in a moment, or contact us for help.",
            "error",
        )
    return redirect(dest)


@bp.route("/account/checkin", methods=["POST"])
@login_required
def checkin():
    freshly = current_user.check_in()
    journal_body = (request.form.get("journal") or "").strip()[:4000]
    mood = (request.form.get("mood") or "").strip().lower()
    if mood not in MOOD_KEYS:
        mood = None
    prompt_key = (request.form.get("prompt") or "").strip()
    prompt_map = journal_prompt_map()
    if prompt_key not in prompt_map:
        prompt_key, _ = random_journal_prompt()
    today = date.today()
    saved_page = False
    if journal_body:
        # Each save becomes its own notebook page (never overwrite prior pages).
        entry = JournalEntry(
            user_id=current_user.id,
            day=today,
            prompt_key=prompt_key,
            prompt_label=prompt_map[prompt_key],
            body=journal_body,
            mood=mood,
        )
        db.session.add(entry)
        saved_page = True
    elif mood:
        entry = (
            JournalEntry.query
            .filter_by(user_id=current_user.id, day=today)
            .order_by(JournalEntry.created_at.desc(), JournalEntry.id.desc())
            .first()
        )
        if entry is None:
            entry = JournalEntry(
                user_id=current_user.id,
                day=today,
                prompt_key=prompt_key,
                prompt_label=prompt_map[prompt_key],
                mood=mood,
            )
            db.session.add(entry)
        else:
            entry.mood = mood
    db.session.commit()
    if saved_page:
        flash("Entry saved — here’s a fresh page.", "success")
    elif freshly:
        flash("You showed up today. That's the whole thing.", "success")
    elif mood:
        flash("Mood saved for today.", "success")
    else:
        flash("Already checked in today \u2014 see you tomorrow.", "info")
    next_url = request.form.get("next") or url_for("main.account", tab="journal")
    return redirect(next_url)


@bp.route("/u/<int:user_id>")
def profile(user_id):
    user = db.session.get(User, user_id)
    if user is None or user.deleted_at is not None:
        abort(404)
    from_post = request.args.get("from", type=int)
    from ..services.social_graph import follow_counts, is_following
    followers_n, following_n = follow_counts(user)
    following = (current_user.is_authenticated
                 and is_following(current_user, user))
    return render_template(
        "main/profile.html", profile_user=user, from_post=from_post,
        followers_n=followers_n, following_n=following_n, is_following=following)


@bp.route("/u/<int:user_id>/follow", methods=["POST"])
@login_required
def follow_user(user_id):
    target = db.session.get(User, user_id)
    if target is None or target.deleted_at is not None or target.id == current_user.id:
        abort(404)
    from ..services.social_graph import toggle_follow
    now_following = toggle_follow(current_user, target)
    db.session.commit()
    flash("Following — you'll hear when they post." if now_following
          else "Unfollowed.", "success")
    return redirect(request.form.get("next")
                    or url_for("main.profile", user_id=user_id))


@bp.route("/mentions/suggest")
def mention_suggest():
    """JSON autocomplete for @username tagging in the community.

    Always returns JSON (never an HTML login redirect) so the compose-box
    fetch handler can parse the response reliably.
    """
    if not current_user.is_authenticated:
        return jsonify([]), 401
    from ..services.social_graph import suggest_usernames
    q = request.args.get("q") or ""
    try:
        return jsonify(suggest_usernames(
            q, limit=8, exclude_id=current_user.id))
    except Exception:
        log.exception("mention suggest failed")
        return jsonify([])


@bp.route("/account/profile", methods=["POST"])
@login_required
def update_profile():
    from ..services.social_graph import (allocate_username, normalize_username,
                                         username_error)
    from sqlalchemy import func

    name = (request.form.get("display_name") or "").strip()[:80]
    bio = (request.form.get("bio") or "").strip()[:400]
    raw_user = normalize_username(request.form.get("username") or "")
    current_handle = (current_user.username or "").lower()
    if not raw_user:
        # Username is required for tagging — keep existing or allocate one.
        if not current_user.username:
            current_user.username = allocate_username(current_user.email)
    elif raw_user != current_handle:
        err = username_error(raw_user)
        if err:
            flash(err, "error")
            return redirect(url_for("main.settings"))
        clash = (User.query
                 .filter(func.lower(User.username) == raw_user,
                         User.id != current_user.id).first())
        if clash:
            flash("That @username is already taken.", "error")
            return redirect(url_for("main.settings"))
        current_user.username = raw_user
    current_user.display_name = name or None
    current_user.bio = bio or None
    current_user.default_anonymous = request.form.get("default_anonymous") == "1"
    current_user.set_goals(valid_intent_keys(request.form.getlist("goals")))
    # profile links are a Studio-toggled membership perk
    if current_user.has_feature("profile_links"):
        links = _collect_profile_links(request.form)
        # Creator-of-the-Month Instagram field (spotlight perk). Only touch
        # it when the dedicated input was submitted, so other profile saves
        # don't wipe a handle set earlier.
        if current_user.has_feature("spotlight") and "creator_instagram" in request.form:
            links = upsert_instagram_link(
                links, request.form.get("creator_instagram") or "",
                limit=PROFILE_LINK_MAX)
        current_user.set_links(links)
    current_user.set_displayed_badges(_valid_badge_choices(request.form.getlist("badges_display")))

    if request.form.get("remove_avatar") == "1":
        current_user.avatar_data = None
        current_user.avatar_mime = None
        current_user.avatar_anim_data = None
        current_user.avatar_anim_mime = None
        current_user.avatar_url = None

    upload = request.files.get("avatar_file")
    if upload and upload.filename:
        try:
            data, mime, anim, anim_mime = process_avatar(upload)
            current_user.avatar_data = data
            current_user.avatar_mime = mime
            current_user.avatar_anim_data = anim
            current_user.avatar_anim_mime = anim_mime
            current_user.avatar_url = None
        except AvatarError as exc:
            db.session.commit()  # keep the other field edits
            flash(str(exc), "error")
            return redirect(url_for("main.settings"))

    db.session.commit()
    flash("Saved. Nice to meet you properly.", "success")
    return redirect(url_for("main.settings"))


@bp.route("/avatar/<int:user_id>")
def avatar(user_id):
    user = db.session.get(User, user_id)
    if user is None or not user.avatar_data:
        abort(404)
    resp = Response(bytes(user.avatar_data), mimetype=user.avatar_mime or "image/jpeg")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@bp.route("/avatar/<int:user_id>/anim")
def avatar_anim(user_id):
    """Animated GIF (when present) — profile page and settings preview."""
    user = db.session.get(User, user_id)
    if user is None:
        abort(404)
    if user.avatar_anim_data:
        resp = Response(bytes(user.avatar_anim_data),
                        mimetype=user.avatar_anim_mime or "image/gif")
        resp.headers["Cache-Control"] = "public, max-age=3600"
        resp.headers["Content-Disposition"] = "inline"
        return resp
    if user.avatar_data:
        return redirect(url_for("main.avatar", user_id=user_id))
    abort(404)


# --- Content Library --------------------------------------------------------

def _can_play_videos(user) -> bool:
    """Creator Content Tips play gate (Studio: content_hub_creator)."""
    return bool(getattr(user, "is_authenticated", False)
                and user.has_feature("content_hub_creator"))


def _can_play_video(user, video) -> bool:
    """Per-video play gate: free picks, healing tips, or creator tips."""
    if not getattr(user, "is_authenticated", False) or video is None:
        return False
    if video.free_access:
        return True
    if getattr(video, "healing_access", False) and user.has_feature("content_hub_healing"):
        return True
    return user.has_feature("content_hub_creator")


def _video_playable(video) -> bool:
    """True when the file (disk or legacy DB bytes) is actually available."""
    if video is None:
        return False
    if video.data:
        return True
    if video.disk_name:
        path = os.path.join(current_app.config["VIDEO_STORAGE_DIR"], video.disk_name)
        return os.path.exists(path)
    return False


def _can_read_reviews(user) -> bool:
    """Reel reviews are a Creator / Full Bloom perk — everyone else sees a taste."""
    return bool(getattr(user, "is_authenticated", False)
                and user.has_feature("reel_reviews"))


@bp.route("/watch")
def videos():
    """Content Hub: reel reviews (Creator+) and the signed-in video library."""
    can_browse = current_user.is_authenticated
    can_play_creator = _can_play_videos(current_user)
    items = []
    if can_browse:
        items = (Video.query.filter_by(published=True)
                 .order_by(Video.sort_order, Video.created_at.desc()).all())
    reviews = (ReelReview.query.filter_by(published=True)
               .order_by(ReelReview.created_at.desc()).limit(24).all())
    can_reel = _can_read_reviews(current_user)
    my_app = None
    my_reel = None
    week_key = reel_svc.current_week_key()
    week_reviews = reel_svc.published_reviews_for_week(week_key)
    if can_reel:
        my_app = reel_svc.application_for(current_user.id, week_key)
        my_reel = rotw_svc.submission_for(current_user.id, week_key)
    return render_template(
        "main/videos.html", videos=items, can_browse=can_browse,
        can_play=can_play_creator, can_play_video=_can_play_video,
        reviews=reviews, my_application=my_app, week_key=week_key,
        week_reviews=week_reviews, can_reel=can_reel,
        my_reel_submission=my_reel, min_shares=rotw_svc.MIN_SHARES,
        reviews_per_week=reel_svc.REVIEWS_PER_WEEK,
        max_mb=current_app.config.get("REEL_RAW_MAX_MB", 100),
    )


@bp.route("/watch/review-request", methods=["POST"])
@login_required
def reel_review_request():
    if not current_user.has_feature("reel_reviews"):
        flash("Reel reviews aren’t included in your plan.", "info")
        return redirect(url_for("main.membership"))
    week = reel_svc.current_week_key()
    if reel_svc.application_for(current_user.id, week):
        flash("You've already put a reel forward this week. "
              "A fresh round opens every Monday.", "info")
        return redirect(url_for("main.videos") + "#reviews")
    reel_url = (request.form.get("reel_url") or "").strip()[:500]
    if not reel_svc.is_instagram_reel_url(reel_url):
        flash("Paste the Instagram link of the reel you posted "
              "(it should look like instagram.com/reel/\u2026).", "error")
        return redirect(url_for("main.videos") + "#reviews")
    upload = request.files.get("raw_video")
    if not upload or not upload.filename:
        flash("Upload the raw video file for your reel too.", "error")
        return redirect(url_for("main.videos") + "#reviews")
    # Stream to VIDEO_STORAGE_DIR (same as Content Hub). Loading the whole
    # file into Postgres BYTEA OOMs Render workers and returns a 502.
    max_bytes = current_app.config.get("REEL_RAW_MAX_MB", 100) * 1024 * 1024
    try:
        disk_name, mime, fname, size = process_video(
            upload, current_app.config["VIDEO_STORAGE_DIR"], max_bytes)
    except VideoError as exc:
        flash(str(exc), "error")
        return redirect(url_for("main.videos") + "#reviews")
    app_row = ReelReviewApplication(
        user_id=current_user.id, week_key=week, reel_url=reel_url,
        disk_name=disk_name, filename=fname, mime=mime, size=size)
    db.session.add(app_row)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        delete_stored(current_app.config["VIDEO_STORAGE_DIR"], disk_name)
        log.exception("reel review application failed")
        flash("We couldn't save your entry just now — please try again.", "error")
        return redirect(url_for("main.videos") + "#reviews")
    flash("Your reel is in for this week. Reviews go up one a day — "
          "keep an eye on the Content Hub.", "success")
    return redirect(url_for("main.videos") + "#reviews")


@bp.route("/watch/reel-of-week", methods=["POST"])
@login_required
def reel_of_week_submit():
    """Put a reel forward for the home page spotlight."""
    back = url_for("main.videos") + "#reel-of-week"
    if not current_user.has_feature("reel_reviews"):
        flash("Reel of the Week is a Creator perk.", "info")
        return redirect(url_for("main.membership"))
    week = rotw_svc.current_week_key()
    if rotw_svc.submission_for(current_user.id, week):
        flash("You've already entered a reel this week. "
              "A fresh round opens every Monday.", "info")
        return redirect(back)

    reel_url = (request.form.get("reel_url") or "").strip()[:500]
    if not rotw_svc.is_instagram_reel_url(reel_url):
        flash("Paste the Instagram link of the reel "
              "(it should look like instagram.com/reel/\u2026).", "error")
        return redirect(back)
    try:
        shares = int((request.form.get("share_count") or "").strip() or 0)
    except ValueError:
        shares = 0
    if shares < rotw_svc.MIN_SHARES:
        flash(f"Reel of the Week is for reels with {rotw_svc.MIN_SHARES} shares "
              "or more. Tell us the share count from your Instagram insights.",
              "error")
        return redirect(back)
    if not request.form.get("confirm_shares"):
        flash("Tick the box to confirm the share count is accurate.", "error")
        return redirect(back)
    upload = request.files.get("raw_video")
    if not upload or not upload.filename:
        flash("Upload the raw video for your reel too.", "error")
        return redirect(back)

    max_bytes = current_app.config.get("REEL_RAW_MAX_MB", 100) * 1024 * 1024
    try:
        disk_name, mime, fname, size = process_video(
            upload, current_app.config["VIDEO_STORAGE_DIR"], max_bytes)
    except VideoError as exc:
        flash(str(exc), "error")
        return redirect(back)
    row = ReelSubmission(user_id=current_user.id, week_key=week,
                         reel_url=reel_url, share_count=shares,
                         disk_name=disk_name, filename=fname, mime=mime,
                         size=size)
    db.session.add(row)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        delete_stored(current_app.config["VIDEO_STORAGE_DIR"], disk_name)
        log.exception("reel of the week submission failed")
        flash("We couldn't save your entry just now — please try again.", "error")
        return redirect(back)
    flash("Your reel is in the running for this week's spotlight.", "success")
    return redirect(back)


@bp.route("/watch/<int:video_id>")
@login_required
def watch(video_id):
    video = db.session.get(Video, video_id)
    # Owner can preview unpublished drafts; everyone else needs published.
    if video is None:
        abort(404)
    if not video.published and not current_user.is_admin:
        abort(404)
    more = (Video.query.filter(Video.published.is_(True), Video.id != video.id)
            .order_by(Video.sort_order, Video.created_at.desc()).limit(6).all())
    can_play = _can_play_video(current_user, video)
    playable = _video_playable(video)
    return render_template("main/watch.html", video=video, more=more,
                           can_play=can_play, playable=playable,
                           access_label=video.access_label(current_user),
                           can_play_video=_can_play_video)


@bp.route("/watch/<int:video_id>/thumb")
@login_required
def video_thumb(video_id):
    video = db.session.get(Video, video_id)
    if video is None or not video.thumb_data:
        abort(404)
    if not video.published and not current_user.is_admin:
        abort(404)
    resp = Response(bytes(video.thumb_data), mimetype=video.thumb_mime or "image/jpeg")
    resp.headers["Cache-Control"] = "private, max-age=86400"
    return resp


def _range_response(data, mime, filename):
    """Serve bytes with HTTP Range support so <video> can seek."""
    length = len(data)
    range_header = request.headers.get("Range")
    if not range_header:
        resp = Response(data, mimetype=mime)
        resp.headers["Accept-Ranges"] = "bytes"
        resp.headers["Content-Length"] = str(length)
        resp.headers["Cache-Control"] = "private, no-store"
        return resp
    m = re.match(r"bytes=(\d*)-(\d*)", range_header)
    start, end = 0, length - 1
    if m:
        if m.group(1):
            start = int(m.group(1))
        if m.group(2):
            end = int(m.group(2))
    start = max(0, start)
    end = min(end, length - 1)
    if start > end:
        resp = Response(status=416)
        resp.headers["Content-Range"] = f"bytes */{length}"
        return resp
    chunk = data[start:end + 1]
    resp = Response(chunk, status=206, mimetype=mime)
    resp.headers["Accept-Ranges"] = "bytes"
    resp.headers["Content-Length"] = str(len(chunk))
    resp.headers["Cache-Control"] = "private, no-store"
    return resp


@bp.route("/watch/<int:video_id>/stream")
@login_required
def video_stream(video_id):
    video = db.session.get(Video, video_id)
    if video is None:
        abort(404)
    if not _can_play_video(current_user, video):
        abort(404)
    if not video.published and not current_user.is_admin:
        abort(404)
    if video.disk_name:
        path = os.path.join(current_app.config["VIDEO_STORAGE_DIR"], video.disk_name)
        if not os.path.exists(path):
            log.error("Video %s missing on disk: %s", video_id, path)
            abort(404)
        # conditional=True enables HTTP Range (206) so <video> can seek.
        resp = send_file(path, mimetype=video.mime or "video/mp4",
                         conditional=True, download_name=video.filename or "video",
                         as_attachment=False)
        resp.headers["Accept-Ranges"] = "bytes"
        resp.headers["Cache-Control"] = "private, no-store"
        return resp
    if video.data:  # legacy rows still stored in the database
        return _range_response(bytes(video.data), video.mime or "video/mp4",
                               video.filename or "video")
    abort(404)


def _reel_review_playable(review) -> bool:
    """True when the owner's review video is actually on the disk."""
    if review is None or not review.review_disk_name:
        return False
    path = os.path.join(current_app.config["VIDEO_STORAGE_DIR"],
                        review.review_disk_name)
    return os.path.exists(path)


@bp.route("/watch/reviews/<int:review_id>")
def reel_review(review_id):
    """A published reel review on its own page: the video plus the full write-up."""
    review = db.session.get(ReelReview, review_id)
    if review is None:
        abort(404)
    # Guests never see hidden reviews; the owner can still preview one.
    if not review.published and not getattr(current_user, "is_admin", False):
        abort(404)
    application = review.application
    # Reading the critique is a Creator perk; everyone else gets the opening.
    can_read = _can_read_reviews(current_user)
    more = (ReelReview.query
            .filter(ReelReview.published.is_(True), ReelReview.id != review.id)
            .order_by(ReelReview.created_at.desc()).limit(6).all())
    return render_template(
        "main/reel_review.html", review=review, application=application,
        author=application.author if application else None,
        reel_url=application.reel_url if application else "",
        reel_embed=instagram_embed_url(application.reel_url) if application else None,
        playable=_reel_review_playable(review) and can_read, more=more,
        can_read=can_read,
    )


@bp.route("/watch/reviews/<int:review_id>/stream")
@login_required
def reel_review_stream(review_id):
    """Stream the owner's review video (Creator / Full Bloom members)."""
    review = db.session.get(ReelReview, review_id)
    if review is None or not review.review_disk_name:
        abort(404)
    if not review.published and not current_user.is_admin:
        abort(404)
    if not _can_read_reviews(current_user):
        abort(404)
    path = os.path.join(current_app.config["VIDEO_STORAGE_DIR"], review.review_disk_name)
    if not os.path.exists(path):
        abort(404)
    resp = send_file(path, mimetype=review.review_mime or "video/mp4",
                     conditional=False, download_name=review.review_filename or "review",
                     as_attachment=False)
    resp.headers["Accept-Ranges"] = "bytes"
    resp.headers["Cache-Control"] = "private, no-store"
    return resp


@bp.route("/account/timezone", methods=["POST"])
@login_required
def save_timezone():
    """Remember the browser's IANA timezone for local timestamps."""
    from ..services.timefmt import normalize_timezone
    raw = request.form.get("timezone")
    if raw is None and request.is_json:
        raw = (request.get_json(silent=True) or {}).get("timezone")
    tz = normalize_timezone(raw)
    if tz and current_user.timezone != tz:
        current_user.timezone = tz
        db.session.commit()
    if request.headers.get("X-Requested-With") == "fetch" or request.is_json:
        return {"ok": True, "timezone": tz or current_user.timezone}
    return redirect(request.referrer or url_for("main.account"))


@bp.route("/account/password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "GET":
        return render_template("main/password.html")
    current = (request.form.get("current_password") or "").strip()
    new = (request.form.get("new_password") or "").strip()
    confirm = (request.form.get("new_password_confirm") or "").strip()
    if not current_user.check_password(current):
        flash("Your current password didn't match \u2014 no changes made.", "error")
        return redirect(url_for("main.change_password"))
    if len(new) < 8:
        flash("Your new password needs at least 8 characters.", "error")
        return redirect(url_for("main.change_password"))
    if new != confirm:
        flash("Those passwords don't match \u2014 enter the same one twice.", "error")
        return redirect(url_for("main.change_password"))
    if current_user.check_password(new):
        flash("Pick a different password \u2014 the new one can't be the same as your current password.", "error")
        return redirect(url_for("main.change_password"))
    current_user.set_password(new)
    db.session.commit()
    flash("Password updated.", "success")
    return redirect(url_for("main.settings"))


@bp.route("/account/delete", methods=["POST"])
@login_required
def delete_account():
    if request.form.get("confirm") != "yes":
        flash("Account not deleted \u2014 tick \u201cYes, I'm sure\u201d first.", "error")
        return redirect(url_for("main.settings"))
    if current_user.is_admin:
        flash("The owner account can't be closed from here.", "error")
        return redirect(url_for("main.settings"))
    from ..services.privacy import close_account
    close_account(current_user)
    from flask_login import logout_user
    logout_user()
    flash("Your account is closed. Thank you for the time you spent here.", "success")
    return redirect(url_for("main.index"))


@bp.route("/feedback", methods=["POST"])
@limiter.limit("8 per hour")
def feedback_submit():
    from ..services.feedback import submit_feedback
    kind = request.form.get("kind") or "feedback"
    body = request.form.get("body") or ""
    stars = request.form.get("stars")
    page_path = request.form.get("page_path") or request.referrer or ""
    if page_path.startswith(request.host_url):
        page_path = "/" + page_path[len(request.host_url):]
    contact = request.form.get("contact_email") or ""
    if request.form.get("website"):  # honeypot
        return redirect(request.referrer or url_for("main.index"))
    _row, msg = submit_feedback(kind=kind, body=body, stars=stars,
                                page_path=page_path, contact_email=contact)
    flash(msg, "success" if _row else "error")
    nxt = request.form.get("next") or request.referrer or url_for("main.index")
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = url_for("main.index")
    return redirect(nxt)


@bp.route("/subscribe", methods=["POST"])
@limiter.limit("5 per minute")
def subscribe():
    email = (request.form.get("email") or "").strip().lower()
    if request.form.get("website"):  # honeypot
        return redirect(url_for("main.index"))
    if not EMAIL_RE.match(email) or len(email) > 255:
        flash("That doesn't look like an email address \u2014 mind checking it?", "subscribe-error")
        return redirect(url_for("main.index") + "#letter")
    if Subscriber.query.filter_by(email=email).first():
        flash("You're already in \u2014 see you Sunday.", "subscribe-success")
    else:
        db.session.add(Subscriber(email=email))
        db.session.commit()
        try:
            from ..services.mailer import send_newsletter_welcome
            send_newsletter_welcome(email)
        except Exception:
            log.exception("Newsletter welcome email failed for %s", email)
        flash("You're in. One small step, every Sunday.", "subscribe-success")
    return redirect(url_for("main.index") + "#letter")


@bp.route("/faq")
def faq():
    items = FaqItem.query.order_by(FaqItem.sort_order).all()
    return render_template("main/faq.html", items=items)


@bp.route("/contact", methods=["GET", "POST"])
@limiter.limit("3 per hour", methods=["POST"])
def contact():
    if request.method == "POST":
        if request.form.get("website"):  # honeypot
            return redirect(url_for("main.contact"))
        name = (request.form.get("name") or "").strip()[:120]
        email = (request.form.get("email") or "").strip().lower()
        body = (request.form.get("message") or "").strip()[:5000]
        if not name or not body or not EMAIL_RE.match(email):
            flash("Please fill in your name, a valid email, and a message.", "error")
            return render_template("main/contact.html", form=request.form), 400
        db.session.add(ContactMessage(name=name, email=email, body=body))
        db.session.commit()
        send_contact_notification(name, email, body)
        flash("Got it. I read everything \u2014 you'll hear back soon.", "success")
        return redirect(url_for("main.contact"))
    return render_template("main/contact.html", form={})


@bp.route("/support-groups")
def support_groups_page():
    from ..services import support_groups as sg_svc

    # After 1:1 / facilitator checkout return, fulfill the session if the webhook lagged.
    session_id = (request.args.get("session_id") or "").strip()
    if (request.args.get("purchased") or session_id) and pay.configured():
        try:
            if session_id:
                pay.fulfill_checkout_session_id(session_id)
            pay.sync_recent_payments(days=7, max_pages=1)
            db.session.commit()
            if current_user.is_authenticated:
                flash("You’re booked — check My space → Activity for the confirmation.", "success")
        except Exception:
            log.exception("support groups: purchase sync after checkout failed")
            db.session.rollback()

    stats = sg_svc.circle_stats()
    healing = [s for s in stats if s["circle"].track == "healing"]
    building = [s for s in stats if s["circle"].track == "building"]
    my_meeting_ids = set()
    alert_circle_ids = set()
    can_schedule = False
    schedule_err = None
    member_tz = "UTC"
    facilitator_sessions = sg_svc.open_facilitator_sessions()
    facilitator_spots = {
        m.id: sg_svc.meeting_spots_left(m) for m in facilitator_sessions
    }
    facilitator_seats = {
        m.id: sg_svc.meeting_seat_count(m) for m in facilitator_sessions
    }
    my_one_on_ones = []
    if current_user.is_authenticated:
        member_tz = (current_user.timezone or "UTC").strip() or "UTC"
        if current_user.is_member():
            can_schedule, schedule_err = sg_svc.can_schedule_peer(current_user)
            alert_circle_ids = sg_svc.user_topic_alert_ids(current_user.id)
            for app in sg_svc.upcoming_for_user(current_user, limit=40):
                if app.meeting_id:
                    my_meeting_ids.add(app.meeting_id)
                if app.meeting and (app.meeting.kind or "") == "one_on_one":
                    my_one_on_ones.append(app.meeting)
    return render_template(
        "main/support_groups.html",
        healing_circles=healing,
        building_circles=building,
        my_meeting_ids=my_meeting_ids,
        alert_circle_ids=alert_circle_ids,
        can_schedule=can_schedule,
        schedule_err=schedule_err,
        member_tz=member_tz,
        peer_cap=sg_svc.PEER_MEETING_CAP,
        peer_minutes=sg_svc.peer_meeting_minutes(),
        max_sessions=sg_svc.MAX_OPEN_SESSIONS_PER_CIRCLE,
        facilitator_sessions=facilitator_sessions,
        facilitator_spots=facilitator_spots,
        facilitator_seats=facilitator_seats,
        facilitator_minutes=sg_svc.FACILITATOR_DURATION_MINUTES,
        facilitator_cap=sg_svc.FACILITATOR_MEETING_CAP,
        my_one_on_ones=my_one_on_ones,
        one_on_one_minutes=sg_svc.ONE_ON_ONE_DURATION_MINUTES,
        one_on_one_refundable={
            m.id: sg_svc.one_on_one_refundable(m) for m in my_one_on_ones
        },
        refund_hours=sg_svc.ONE_ON_ONE_REFUND_HOURS,
    )


@bp.route("/coaching/<coach>/book", methods=["GET", "POST"])
@login_required
@limiter.limit("20 per minute")
def coaching_book(coach):
    """Questionnaire + availability slot, then Stripe Checkout for a founder 1:1."""
    from ..services import coaching_intake as intake_svc
    from ..services import settings as settings_service
    from ..services import support_groups as sg_svc
    from ..services.timefmt import viewer_timezone

    key = intake_svc.normalize_coach(coach)
    if key != "saman":
        abort(404)
    questions = intake_svc.questions_for(key)
    if not questions:
        abort(404)

    price_id = (settings_service.get_setting(f"{key}_stripe_price_id") or "").strip()
    if not price_id:
        flash("Checkout for this 1:1 isn’t live yet — check back soon.", "info")
        return redirect(url_for("main.support_groups_page") + "#coaching")

    viewer_tz = viewer_timezone()
    slots = intake_svc.open_slots(key, viewer_tz=viewer_tz)

    if request.method == "POST":
        answers, err = intake_svc.parse_answers(request.form, key)
        if err:
            flash(err, "error")
            return redirect(url_for("main.coaching_book", coach=key))
        when = intake_svc.parse_slot_utc(request.form.get("slot_utc") or "")
        if when is None:
            flash("Pick an available date and time for your call.", "error")
            return redirect(url_for("main.coaching_book", coach=key))
        intake, err = intake_svc.create_pending_intake(
            current_user, coach=key, answers=answers, slot_utc=when,
        )
        if err:
            flash(err, "error")
            return redirect(url_for("main.coaching_book", coach=key))
        return redirect(url_for(
            "main.checkout_addon", kind=key, intake=intake.id,
        ))

    # Group questions by section for the template.
    sections: list[tuple[str, list]] = []
    for q in questions:
        if not sections or sections[-1][0] != q["section"]:
            sections.append((q["section"], []))
        sections[-1][1].append(q)

    return render_template(
        "main/coaching_book.html",
        coach=key,
        coach_label=intake_svc.coach_label(key),
        sections=sections,
        slots=slots,
        viewer_tz=viewer_tz,
        duration_min=60,
        refund_hours=sg_svc.ONE_ON_ONE_REFUND_HOURS,
    )


@bp.route("/checkout/addon/<kind>", methods=["GET", "POST"])
@limiter.limit("20 per minute")
def checkout_addon(kind):
    """Stripe Checkout for facilitator / 1:1 add-ons (price ids from Studio settings)."""
    from ..models import CoachingIntake
    from ..services import settings as settings_service

    catalog = {
        "facilitator": {
            "setting": "facilitator_stripe_price_id",
            "name": "Facilitator-led session",
            "slug": "addon-facilitator",
        },
        "ayesha": {
            "setting": "ayesha_stripe_price_id",
            "name": "1:1 with Ayesha",
            "slug": "addon-ayesha",
        },
        "saman": {
            "setting": "saman_stripe_price_id",
            "name": "1:1 with Saman",
            "slug": "addon-saman",
        },
    }
    kind_key = (kind or "").strip().lower()
    info = catalog.get(kind_key)
    if not info:
        abort(404)

    # Saman 1:1 must go through the questionnaire + slot picker first.
    intake_id = request.args.get("intake", type=int) or request.form.get("intake", type=int)
    if kind_key == "saman" and not intake_id:
        return redirect(url_for("main.coaching_book", coach="saman"))

    price_id = (settings_service.get_setting(info["setting"]) or "").strip()
    if not price_id:
        flash("Checkout for this add-on isn’t live yet — check back soon.", "info")
        return redirect(url_for("main.support_groups_page"))
    if not pay.configured():
        flash("Payments aren’t configured yet. Please try again later.", "error")
        return redirect(url_for("main.support_groups_page"))

    intake = None
    if intake_id:
        intake = db.session.get(CoachingIntake, intake_id)
        if (
            intake is None
            or intake.status != "pending_payment"
            or not current_user.is_authenticated
            or intake.user_id != current_user.id
        ):
            flash("That booking form expired — please fill it in again.", "error")
            if kind_key == "saman":
                return redirect(url_for("main.coaching_book", coach="saman"))
            return redirect(url_for("main.support_groups_page"))

    email = current_user.email if current_user.is_authenticated else None
    name = current_user.public_name() if current_user.is_authenticated else None
    metadata = {
        "slug": info["slug"],
        "kind": "addon",
        "product_name": info["name"],
        "addon": kind_key,
    }
    if intake is not None:
        metadata["coaching_intake_id"] = str(intake.id)
        metadata["coach"] = intake.coach
    try:
        url = pay.create_checkout_session(
            product_id=price_id,
            return_url=url_for(
                "main.support_groups_page", purchased=1, _external=True,
            ) + "#coaching",
            customer_email=email,
            customer_name=name,
            metadata=metadata,
        )
    except pay.StripeError as exc:
        flash(str(exc), "error")
        return redirect(url_for("main.support_groups_page"))
    return redirect(url)


@bp.route("/support-groups/schedule", methods=["POST"])
@login_required
def schedule_support_session():
    from ..services import support_groups as sg_svc
    if not current_user.is_member():
        flash("Support groups are for Healing, Creator, and Full Bloom members.", "error")
        return redirect(url_for("main.membership", next=url_for("main.support_groups_page")))
    circle_id = request.form.get("circle_id", type=int)
    tz = (request.form.get("timezone") or current_user.timezone or "UTC").strip()
    meeting, err = sg_svc.schedule_peer_session(
        current_user,
        circle_id=circle_id,
        date_s=request.form.get("meeting_date") or "",
        time_s=request.form.get("meeting_time") or "",
        tz_name=tz,
        topic_title=request.form.get("topic_title") or "",
    )
    if err:
        flash(err, "error")
    else:
        label = (
            sg_svc.meeting_display_title(meeting)
            if meeting else "your circle"
        )
        flash(
            f"Session scheduled for {label}. "
            "Others can join from upcoming sessions.",
            "success",
        )
    return redirect(url_for("main.support_groups_page") + (f"#circle-{circle_id}" if circle_id else ""))


@bp.route("/support-groups/circles/<int:circle_id>/notify", methods=["POST"])
@login_required
def toggle_support_topic_alert(circle_id):
    from ..services import support_groups as sg_svc
    if not current_user.is_member():
        flash("Support groups are for Healing, Creator, and Full Bloom members.", "error")
        return redirect(url_for("main.membership", next=url_for("main.support_groups_page")))
    now_on, err = sg_svc.toggle_topic_alert(current_user, circle_id)
    if err:
        flash(err, "error")
    elif now_on:
        flash("We'll notify you when a session is scheduled for this topic.", "success")
    else:
        flash("Topic alerts turned off for this circle.", "info")
    return redirect(url_for("main.support_groups_page") + f"#circle-{circle_id}")


@bp.route("/support-groups/meetings/<int:meeting_id>/join", methods=["POST"])
@login_required
def join_support_session(meeting_id):
    from ..services import support_groups as sg_svc
    if not current_user.is_member():
        flash("Support groups are for Healing, Creator, and Full Bloom members.", "error")
        return redirect(url_for("main.membership", next=url_for("main.support_groups_page")))
    _, err = sg_svc.join_peer_session(current_user, meeting_id)
    if err:
        flash(err, "error")
    else:
        flash("You're in — use Join when you're ready for the session.", "success")
    return redirect(url_for("main.support_groups_page"))


@bp.route("/support-groups/meetings/<int:meeting_id>/leave", methods=["POST"])
@login_required
def leave_support_session(meeting_id):
    from ..services import support_groups as sg_svc
    err = sg_svc.leave_peer_session(current_user, meeting_id)
    if err:
        flash(err, "error")
    else:
        flash("You've left that session.", "success")
    return redirect(url_for("main.support_groups_page"))


@bp.route("/support-groups/one-on-one/<int:meeting_id>/cancel", methods=["POST"])
@login_required
def cancel_one_on_one(meeting_id):
    from ..services import support_groups as sg_svc
    err, refundable = sg_svc.cancel_one_on_one(current_user, meeting_id)
    if err:
        flash(err, "error")
    elif refundable:
        flash("Your 1:1 is cancelled. You cancelled more than 24 hours ahead, "
              "so your refund is on its way — allow a few days for it to land.",
              "success")
    else:
        flash("Your 1:1 is cancelled. It was inside the 24-hour window, so "
              "this one isn't refundable.", "info")
    return redirect(url_for("main.support_groups_page") + "#coaching")


@bp.route("/support-groups/meetings/<int:meeting_id>/room")
@login_required
def support_session_room(meeting_id):
    """Embedded Daily.co call for seated members (live window only)."""
    from ..models import SupportGroupMeeting
    from ..services import support_groups as sg_svc
    from ..services import daily as daily_svc

    meeting = db.session.get(SupportGroupMeeting, meeting_id) or abort(404)
    seat = sg_svc.user_selected_on_meeting(current_user.id, meeting.id)
    if seat is None and not current_user.is_admin:
        flash("Save a seat on that session before joining the room.", "error")
        return redirect(url_for("main.support_groups_page"))

    # Past live window → wrap (and close the meeting so Studio stops listing it).
    if meeting.status == "scheduled" and meeting.scheduled_at:
        if sg_svc.meeting_phase(meeting) == "ended":
            try:
                sg_svc.complete_meeting(meeting)
            except Exception:
                log.exception("Could not auto-complete meeting %s", meeting.id)
            return redirect(url_for("main.support_session_wrap", meeting_id=meeting.id))
    if meeting.status == "completed":
        return redirect(url_for("main.support_session_wrap", meeting_id=meeting.id))
    if meeting.status != "scheduled" or not meeting.room_url or not meeting.room_name:
        flash("That session isn’t open yet.", "error")
        return redirect(url_for("main.support_groups_page"))

    phase = sg_svc.meeting_phase(meeting)
    if phase == "waiting":
        return redirect(url_for("main.support_session_waiting", meeting_id=meeting.id))
    if phase == "ended":
        return redirect(url_for("main.support_session_wrap", meeting_id=meeting.id))

    is_host = meeting.scheduled_by_user_id == current_user.id
    # Refresh nbf/exp so older rooms (created before early-open) become joinable.
    if meeting.scheduled_at and meeting.room_name:
        try:
            info = daily_svc.update_room(
                meeting.room_name,
                scheduled_at=meeting.scheduled_at,
                duration_minutes=sg_svc.meeting_duration_minutes(meeting),
                max_participants=sg_svc.meeting_max_participants(meeting),
                enable_cloud_recording=(meeting.kind or "") == "one_on_one",
            )
            if info and info.room_url:
                meeting.zoom_url = info.room_url
                db.session.commit()
        except daily_svc.DailyError:
            log.warning(
                "daily: could not refresh room %s before join", meeting.room_name,
            )
    is_one_on_one = (meeting.kind or "") == "one_on_one"
    try:
        token = daily_svc.create_meeting_token(
            room_name=meeting.room_name,
            user_name=current_user.public_name() or current_user.email,
            is_owner=is_host or current_user.is_admin,
            scheduled_at=meeting.scheduled_at,
            duration_minutes=sg_svc.meeting_duration_minutes(meeting),
            enable_cloud_recording=is_one_on_one,
            start_cloud_recording=is_one_on_one and (is_host or current_user.is_admin),
        )
    except daily_svc.DailyError as exc:
        flash(str(exc), "error")
        return redirect(url_for("main.support_groups_page"))

    return render_template(
        "main/support_session_room.html",
        meeting=meeting,
        daily_room_url=meeting.room_url,
        daily_token=token,
        is_host=is_host,
        duration_min=sg_svc.peer_meeting_minutes(),
        wrap_url=url_for("main.support_session_wrap", meeting_id=meeting.id),
    )


@bp.route("/support-groups/meetings/<int:meeting_id>/status")
@login_required
@limiter.limit("60 per minute")
def support_session_status(meeting_id):
    """Lightweight live status for the waiting room (phase, countdown, seats)."""
    from datetime import timezone as _tz
    from ..models import SupportGroupMeeting
    from ..services import support_groups as sg_svc

    meeting = db.session.get(SupportGroupMeeting, meeting_id) or abort(404)
    if meeting.status != "scheduled" or not meeting.scheduled_at:
        return jsonify({"phase": "unavailable"}), 200

    seat = sg_svc.user_selected_on_meeting(current_user.id, meeting.id)
    if seat is None and not current_user.is_admin:
        return jsonify({"phase": "forbidden"}), 403

    phase = sg_svc.meeting_phase(meeting)
    start = meeting.scheduled_at.replace(tzinfo=_tz.utc)
    now = utcnow().replace(tzinfo=_tz.utc)
    seats = sg_svc.meeting_seats(meeting)
    members = []
    for row in seats:
        user = row.author
        if not user or user.deleted_at:
            continue
        members.append({
            "id": user.id,
            "name": user.public_name() or "Member",
            "is_you": user.id == current_user.id,
            "is_host": meeting.scheduled_by_user_id == user.id,
        })
    payload = {
        "phase": phase,
        "starts_at": start.isoformat().replace("+00:00", "Z"),
        "starts_ms": int(start.timestamp() * 1000),
        "server_now": now.isoformat().replace("+00:00", "Z"),
        "server_ms": int(now.timestamp() * 1000),
        "seats": len(members),
        "capacity": int(meeting.capacity or sg_svc.PEER_MEETING_CAP),
        "members": members,
        "duration_min": sg_svc.peer_meeting_minutes(),
        "room_url": (
            url_for("main.support_session_room", meeting_id=meeting.id)
            if phase == "live" else None
        ),
        "wrap_url": (
            url_for("main.support_session_wrap", meeting_id=meeting.id)
            if phase == "ended" else None
        ),
    }
    return jsonify(payload)


@bp.route("/support-groups/meetings/<int:meeting_id>/waiting")
@login_required
def support_session_waiting(meeting_id):
    """Pre-start waiting room — room unlocks at the scheduled time."""
    from datetime import timezone as _tz
    from ..models import SupportGroupMeeting
    from ..services import support_groups as sg_svc

    meeting = db.session.get(SupportGroupMeeting, meeting_id) or abort(404)
    if meeting.status != "scheduled" or not meeting.scheduled_at:
        flash("That session isn’t available.", "error")
        return redirect(url_for("main.support_groups_page"))

    seat = sg_svc.user_selected_on_meeting(current_user.id, meeting.id)
    if seat is None and not current_user.is_admin:
        flash("Save a seat on that session before joining the room.", "error")
        return redirect(url_for("main.support_groups_page"))

    phase = sg_svc.meeting_phase(meeting)
    if phase == "live":
        return redirect(url_for("main.support_session_room", meeting_id=meeting.id))
    if phase == "ended":
        return redirect(url_for("main.support_session_wrap", meeting_id=meeting.id))

    start = meeting.scheduled_at.replace(tzinfo=_tz.utc)
    now = utcnow().replace(tzinfo=_tz.utc)
    seats = sg_svc.meeting_seats(meeting)
    left_sec = max(0, int((start - now).total_seconds()))
    h, rem = divmod(left_sec, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        initial_countdown = f"{h:02d}:{m:02d}:{s:02d}"
    else:
        initial_countdown = f"{m:02d}:{s:02d}"
    return render_template(
        "main/support_session_waiting.html",
        meeting=meeting,
        duration_min=sg_svc.peer_meeting_minutes(),
        starts_at_iso=start.isoformat().replace("+00:00", "Z"),
        starts_at_ms=int(start.timestamp() * 1000),
        server_now_iso=now.isoformat().replace("+00:00", "Z"),
        server_now_ms=int(now.timestamp() * 1000),
        initial_countdown=initial_countdown,
        room_url=url_for("main.support_session_room", meeting_id=meeting.id),
        status_url=url_for("main.support_session_status", meeting_id=meeting.id),
        initial_seats=len(seats),
        capacity=int(meeting.capacity or sg_svc.PEER_MEETING_CAP),
        initial_members=[
            {
                "id": row.author.id,
                "name": row.author.public_name() or "Member",
                "is_you": row.author.id == current_user.id,
                "is_host": meeting.scheduled_by_user_id == row.author.id,
            }
            for row in seats
            if row.author and not row.author.deleted_at
        ],
    )


@bp.route("/support-groups/meetings/<int:meeting_id>/wrap")
@login_required
def support_session_wrap(meeting_id):
    """Skippable post-session page: peers + optional report."""
    from ..models import SUPPORT_REPORT_REASONS, SupportGroupMeeting
    from ..services import support_groups as sg_svc

    meeting = db.session.get(SupportGroupMeeting, meeting_id) or abort(404)
    seat = sg_svc.user_selected_on_meeting(current_user.id, meeting.id)
    # Also allow wrap after cancel/complete if they were seated (selected seats gone).
    if seat is None and not current_user.is_admin:
        # Fall back: any historical seat on this meeting for this user.
        from ..models import SupportGroupApplication
        past = (SupportGroupApplication.query
                .filter_by(user_id=current_user.id, meeting_id=meeting.id)
                .first())
        if past is None:
            flash("That session wrap isn’t available.", "error")
            return redirect(url_for("main.support_groups_page"))

    peers = sg_svc.wrap_peers(meeting, current_user)
    return render_template(
        "main/support_session_wrap.html",
        meeting=meeting,
        peers=peers,
        report_reasons=SUPPORT_REPORT_REASONS,
    )


@bp.route("/support-groups/meetings/<int:meeting_id>/report/<int:user_id>",
          methods=["POST"])
@login_required
def support_session_report(meeting_id, user_id):
    from ..models import SupportGroupMeeting
    from ..services import support_groups as sg_svc
    from ..services.content_reports import submit_member_report

    meeting = db.session.get(SupportGroupMeeting, meeting_id) or abort(404)
    peers = {u.id for u in sg_svc.wrap_peers(meeting, current_user)}
    if user_id not in peers and not current_user.is_admin:
        flash("You can only report someone from this session.", "error")
        return redirect(url_for("main.support_session_wrap", meeting_id=meeting_id))

    label = meeting.circle.title if meeting.circle else "Support session"
    _, msg = submit_member_report(
        reporter=current_user,
        user_id=user_id,
        reason=request.form.get("reason") or "",
        note=request.form.get("note") or "",
        meeting_id=meeting.id,
        meeting_label=label,
    )
    flash(msg, "success")
    return redirect(url_for("main.support_session_wrap", meeting_id=meeting_id))


@bp.route("/privacy")
def privacy():
    return _legal_page("privacy", "Privacy Policy")


@bp.route("/terms")
def terms():
    return _legal_page("terms", "Terms of Service")


@bp.route("/refunds")
def refunds():
    return _legal_page("refunds", "Refund Policy")


def _legal_page(slug, title):
    from ..services import legal_copy
    from ..services.settings import get_setting

    page = Page.query.filter_by(slug=slug).first()
    canonical = {
        "privacy": legal_copy.PRIVACY,
        "terms": legal_copy.TERMS,
        "refunds": legal_copy.REFUNDS,
    }.get(slug)
    version_stale = (
        get_setting("legal_copy_version", "") != legal_copy.LEGAL_COPY_VERSION
    )
    # Prefer stored Studio copy; fall back to canonical if missing, still a TODO,
    # or when LEGAL_COPY_VERSION was bumped (seed will sync the Page row too).
    if (page is None or not (page.body_md or "").strip()
            or "*TODO: legal review.*" in (page.body_md or "")
            or (version_stale and canonical)):
        if canonical:
            class _Tmp:
                pass
            tmp = _Tmp()
            tmp.title = title
            tmp.body_md = canonical
            page = tmp
    return render_template("main/page.html", page=page, fallback_title=title)
