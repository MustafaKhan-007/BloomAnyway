"""Admin panel. Every route requires is_admin + recent admin activity.

Freshness is a *sliding* idle timeout: each admin action pushes the clock
forward, so day-to-day use never nags. Re-authentication is only required after
``ADMIN_IDLE_DAYS`` of no admin activity.
"""
import csv
import io
import logging
import os
from datetime import date, datetime, timedelta
from functools import wraps

from flask import (Response, abort, current_app, flash, jsonify, redirect,
                   render_template, request, send_file, send_from_directory,
                   session, url_for)
from flask_login import current_user
from sqlalchemy import func
from sqlalchemy.orm import joinedload

from ..extensions import db
from ..models import (Announcement, ContactMessage, ContentReport, DRIP_MODES,
                      FaqItem, ForumComment,
                      ForumPost, MEMBERSHIPS, MEMBERSHIP_LABELS, MarketplaceListing,
                      MembershipPlan,
                      PRODUCT_KINDS,
                      Page, Product, ProductAsset, Quote, QuoteFavorite, QuotePin,
                      ReelReview, ReelReviewApplication, ReelSubmission,
                      SiteFeedback, Testimonial,
                      User, Video, QUOTE_CATEGORIES, utcnow)
from ..services import badges as badges_service
from ..services import demo_accounts
from ..services import quotes as quotes_service
from ..services import reel_of_week as rotw_svc
from ..services import reel_reviews as reel_svc
from ..services import stats
from ..services import mailer
from ..services.mailer import (last_send_error, send_customer_support_email,
                               send_styled_email)
from ..services.settings import DEFAULTS as SETTING_DEFAULTS
from ..services.settings import all_settings, get_setting, set_setting
from ..services.social import fetch_instagram_preview, instagram_handle
from ..services.videos import (VideoError, delete_stored, process_thumb,
                               process_video)
from . import bp

log = logging.getLogger(__name__)


@bp.before_request
def _studio_readonly_guard():
    """View-only owners may browse Studio but cannot POST/PUT/PATCH/DELETE."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    if not current_user.is_authenticated:
        return None
    if not getattr(current_user, "is_admin", False):
        return None
    if not getattr(current_user, "admin_readonly", False):
        return None
    if request.endpoint == "admin.preview":
        # Previewing writes nothing but a session key, and looking around as a
        # member is exactly what a view-only owner is here to do.
        return None
    flash(
        "This Studio account is view-only — you can look around, but changes are locked.",
        "error",
    )
    target = request.referrer
    if not target or "/admin" not in target:
        target = url_for("admin.dashboard")
    return redirect(target)


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        # 404 (not 403) so the panel's existence isn't revealed
        if not current_user.is_authenticated or not current_user.is_admin:
            abort(404)
        now = datetime.utcnow()
        idle_max = timedelta(days=current_app.config["ADMIN_IDLE_DAYS"])
        # last admin activity, falling back to the original sign-in time
        seen_at = session.get("admin_seen_at") or session.get("logged_in_at")
        try:
            active = seen_at and (now - datetime.fromisoformat(seen_at)) < idle_max
        except ValueError:
            active = False
        if not active:
            flash("It's been a while \u2014 please sign in again to open the studio.", "info")
            return redirect(url_for("auth.login", next=request.path))
        # slide the window forward on every admin action
        session.permanent = True
        session["admin_seen_at"] = now.isoformat()
        # Owner perks use effective_membership() (always Creator). Do not write
        # membership=creator here — that left demoted co-owners stuck on Creator.
        return f(*args, **kwargs)
    return wrapper


def _form_ids(name: str = "ids") -> list[int]:
    """Parse checkbox / multi-value id lists from a Studio bulk form."""
    out: list[int] = []
    seen: set[int] = set()
    for raw in request.form.getlist(name):
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


# ============================= VIEW AS A MEMBER ==============================
# Owners rank as Full Bloom everywhere, which makes a membership perk (and any
# paid tier) impossible to check from the inside. This drops the owner to a
# chosen tier for the session so the site gates them like a real member.

def _preview_return(fallback: str) -> str:
    """Where to send the owner back to. Same-site paths only."""
    target = (request.form.get("next") or "").strip()
    if target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return fallback


@bp.route("/preview", methods=["POST"])
@admin_required
def preview():
    from ..services import preview as preview_svc

    choice = (request.form.get("tier") or "").strip().lower()
    back = _preview_return(url_for("main.index"))
    if choice in ("", "off"):
        preview_svc.clear()
        flash("Back to your owner view.", "success")
        return redirect(_preview_return(url_for("admin.settings")))

    if not preview_svc.set_choice(choice):
        flash("That isn't a tier you can preview.", "error")
        return redirect(url_for("admin.settings"))

    state = preview_svc.state(current_user)
    flash(f"Now browsing as {state['label']}. Studio stays open, and nothing "
          "was changed on your account.", "success")
    return redirect(back)


# =============================== DASHBOARD ===================================

@bp.route("/")
@admin_required
def dashboard():
    today = date.today()
    # Throttled Stripe pull, started behind the page rather than in front of it
    # — the owner shouldn't wait on Stripe's API to see her own numbers. New
    # purchases appear on the next load. Manual sync: /admin/sync-purchases.
    from ..services import stripe_pay as pay
    if pay.configured():
        last_raw = (get_setting("stripe_last_sync_at") or "").strip()
        should_sync = True
        if last_raw:
            try:
                last_dt = datetime.fromisoformat(last_raw)
                should_sync = (datetime.utcnow() - last_dt).total_seconds() >= 15 * 60
            except ValueError:
                should_sync = True
        if should_sync and pay.start_background_sync(days=60, max_pages=2):
            set_setting("stripe_last_sync_at",
                        datetime.utcnow().isoformat(timespec="seconds"))
        pay.maybe_sweep_cancel_flags()
    from ..main.routes import CHALLENGE_ENROLL_URL
    from ..services import storage_health
    try:
        storage = storage_health.check()
        storage["videos"] = storage_health.video_check()
    except Exception:
        log.exception("dashboard: storage check failed")
        storage = {"wiped": False}
    return render_template(
        "admin/dashboard.html",
        challenge_enroll_url=CHALLENGE_ENROLL_URL,
        storage=storage,
        today_quote=quotes_service.quote_for(today),
        tomorrow_quote=quotes_service.quote_for(today + timedelta(days=1)),
        cards=stats.dashboard_cards(),
        chart_signups=stats.signups_by_week(12),
        chart_purchases=stats.purchases_over_time(90),
        trending_product=stats.trending_product(7),
        most_visited=stats.most_visited(7),
        memberships=stats.membership_breakdown(),
        video_count=stats.video_count(),
        marketplace=stats.marketplace_counts(),
        member_activity=stats.member_activity(),
        showcase_perf=stats.showcase_performance(),
        recent_feedback=stats.recent_feedback(),
        support_occupancy=stats.support_occupancy(),
        founder_days=stats.founder_days_remaining(),
        stripe_configured=pay.configured(),
    )


@bp.route("/sync-purchases", methods=["POST"])
@admin_required
def sync_purchases():
    """Manual pull of recent Stripe payments into Studio / My space."""
    from ..services import stripe_pay as pay
    if not pay.configured():
        flash("Add STRIPE_SECRET_KEY (and live mode) before syncing.", "error")
        return redirect(url_for("admin.dashboard"))
    try:
        result = pay.sync_recent_payments(days=90, max_pages=4)
        set_setting(
            "stripe_last_sync_at", datetime.utcnow().isoformat(timespec="seconds"))
    except Exception:
        log.exception("manual stripe sync failed")
        flash("Could not sync purchases from Stripe. Check the API key and mode.", "error")
        return redirect(url_for("admin.dashboard"))
    if not result.get("ok"):
        flash(result.get("error") or "Sync failed.", "error")
    elif result.get("imported"):
        flash(
            f"Imported {result['imported']} purchase"
            f"{'' if result['imported'] == 1 else 's'} "
            f"(checked {result.get('checked', 0)}).",
            "success",
        )
    else:
        flash(
            f"No new purchases — checked {result.get('checked', 0)} recent "
            "Stripe payment(s).",
            "info",
        )
    return redirect(url_for("admin.dashboard"))


@bp.route("/import-checkout-session", methods=["POST"])
@admin_required
def import_checkout_session():
    """Fulfill one Checkout Session by id (bypasses webhook — useful for $0 / missed deliveries)."""
    from ..services import stripe_pay as pay
    if not pay.configured():
        flash("Add STRIPE_SECRET_KEY (live mode) before importing.", "error")
        return redirect(url_for("admin.dashboard"))
    sid = (request.form.get("session_id") or "").strip()
    if not sid.startswith("cs_"):
        flash("Paste a Checkout Session id starting with cs_live_ or cs_test_.", "error")
        return redirect(url_for("admin.dashboard"))
    try:
        order = pay.fulfill_checkout_session_id(sid)
        if order is None:
            flash(
                "Could not import that session (not complete/paid, or Stripe retrieve failed). "
                "Check Render logs and that STRIPE_SECRET_KEY is the live key.",
                "error",
            )
            return redirect(url_for("admin.dashboard"))
        db.session.commit()
        flash(
            f"Imported {order.buyer_email} — {order.status} "
            f"({order.total_display()}). Buyer must use that same email in My Space.",
            "success",
        )
    except Exception as exc:
        db.session.rollback()
        log.exception("import checkout session failed: %s", sid)
        flash(f"Import failed: {exc}", "error")
    return redirect(url_for("admin.dashboard"))


# =============================== PRODUCTS ====================================

def _parse_accent(raw: str | None) -> str | None:
    """Normalize a #RRGGBB colour or return None."""
    value = (raw or "").strip()
    if len(value) == 7 and value.startswith("#"):
        try:
            int(value[1:], 16)
            return value.upper()
        except ValueError:
            return None
    return None


def _parse_price_cents(raw: str | None) -> int | None:
    text = (raw or "").strip().replace(",", "")
    if not text:
        return None
    try:
        return round(float(text) * 100)
    except ValueError:
        return None


#: how many curriculum modules one product can carry
MAX_MODULES = 24


def _blank_module(number: int) -> dict:
    return {"number": number, "title": "", "description": "", "asset": None,
            "release_at": None, "gap_days": 0}


def _row_has_work(form, row_number: int) -> bool:
    """Anything typed into this module row besides its title."""
    if (form.get(f"mod{row_number}_desc") or "").strip():
        return True
    for field in ("lesson_title", "lesson_desc", "text_title", "text_body"):
        if any((v or "").strip()
               for v in form.getlist(f"mod{row_number}_{field}")):
            return True
    return False


def _lesson_numbers(form, row_number: int) -> dict[int, int]:
    """Position of a lesson in the form → the lesson number it is saved as.

    An empty lesson row is nothing to save, so the third set of lesson fields
    on the page is not necessarily lesson three. Files uploaded with the form
    are named after the position they were rendered at, and need this to land
    in the right lesson. A lesson with a description but no title is kept and
    named, rather than thrown away with what was written in it.
    """
    titles = form.getlist(f"mod{row_number}_lesson_title")
    descs = form.getlist(f"mod{row_number}_lesson_desc")
    out: dict[int, int] = {}
    number = 0
    for pos, raw in enumerate(titles, start=1):
        written = (descs[pos - 1] if pos - 1 < len(descs) else "").strip()
        if not (raw or "").strip() and not written:
            continue
        number += 1
        out[pos] = number
    return out


def _move_module_content(product: Product, module_moves: dict[int, int],
                         lesson_moves: dict[int, dict[int, int]],
                         old_module_count: int,
                         removed: set[int] | None = None) -> None:
    """Carry each module's files to wherever its row has been moved to.

    Files are pinned to a module by number, while the rows on the form are
    positional, so without this a module dragged up the list arrives holding
    whatever used to sit in its new slot.

    Files are only ever deleted for a module the editor says was removed by
    hand. Working it out from what the form left unsaid is not good enough:
    anything that stops a row saying where its content lives — an older page,
    a field that didn't make it — would read as a removal and take real
    uploads with it. Left unclaimed for any other reason, a module's files
    stay exactly where they are, out of sight until its row comes back.

    A lesson that is gone only loses its own grouping: the files stay in the
    module, which is still there to hold them.
    """
    from ..services import assets as asset_svc

    gone = removed or set()
    # Read every position first: reassigning while walking would move a file
    # twice when two modules swap places.
    placed = [(a, a.module_index, a.lesson_index) for a in product.assets]
    dropped = 0
    for asset, old_module, old_lesson in placed:
        if not old_module:
            continue
        new_module = module_moves.get(old_module)
        if new_module is None:
            if old_module in gone and asset.parent_asset_id is None:
                asset_svc.delete_file(asset)
                db.session.delete(asset)
                dropped += 1
            continue
        asset.module_index = new_module
        moved = lesson_moves.get(new_module) or {}
        asset.lesson_index = moved.get(old_lesson) if old_lesson else None
    if dropped:
        flash(f"{dropped} file{'' if dropped == 1 else 's'} went with the "
              "module you removed.", "info")


def _apply_product_fields(product: Product, form) -> dict[int, int]:
    """Map studio form fields onto a Product (caller commits).

    Returns ``{form row number: module number}`` — blank rows are dropped, so
    the two can drift once an owner clears a module in the middle.
    """
    from ..services.catalog import slugify_title, unique_product_slug
    # Dates on this form are typed on the owner's own calendar and stored UTC.
    from ..services.timefmt import parse_owner_parts

    title = (form.get("title") or "").strip()[:160]
    if title:
        product.title = title
    track = (form.get("track") or product.track or "healing").strip()
    product.track = track if track in ("healing", "building") else "healing"
    # "What it is" is a select-all-that-apply. ``set_types`` keeps the first as
    # the primary; a single ``type`` still works for anything posting the old
    # field. Nothing ticked leaves whatever it already was.
    kinds = [k for k in form.getlist("types") if (k or "").strip()]
    if not kinds and (form.get("type") or "").strip():
        kinds = [form.get("type").strip()]
    if kinds:
        product.set_types(kinds)
    elif not (product.type or "").strip():
        product.type = "guide"
    product.category_label = (form.get("category_label") or "").strip()[:80] or None
    product.badge = (form.get("badge") or "").strip()[:30] or None
    product.promise = (form.get("promise") or "").strip()[:120] or None
    product.meta_line = (form.get("meta_line") or "").strip()[:200] or None
    product.receipt_description = (
        form.get("receipt_description") or "").strip()[:600] or None
    product.description_md = (form.get("description") or "").strip() or None
    product.audience = (form.get("audience") or "").strip() or None
    product.contents_text = (form.get("contents") or "").strip() or None

    # Where each row's content lives now, so it can be carried to wherever the
    # row has been moved to. Rows the editor didn't stamp (an older page, or a
    # caller posting fields directly) leave this empty and nothing is moved.
    old_module_count = len(product.curriculum())
    tracked = any(f"mod{i}_from" in form for i in range(1, MAX_MODULES + 1))
    module_moves: dict[int, int] = {}
    lesson_moves: dict[int, dict[int, int]] = {}

    curriculum_rows = []
    module_numbers: dict[int, int] = {}
    unnamed: list[str] = []
    for i in range(1, MAX_MODULES + 1):
        t = (form.get(f"mod{i}_title") or "").strip()
        # The form always offers a couple of empty rows, so an empty one is
        # nothing to save. A row with work in it is a different thing: it used
        # to be dropped for want of a title, taking the lessons written inside
        # it, with nothing said about where they went.
        if not t:
            if not _row_has_work(form, i):
                continue
            t = f"Module {len(curriculum_rows) + 1}"
            unnamed.append(t)
        # Lessons are the subsections inside a module; their content and text
        # extracts are assigned per file below. Titles arrive as repeated
        # fields so an owner can add as many as they like.
        lesson_titles = form.getlist(f"mod{i}_lesson_title")
        lesson_descs = form.getlist(f"mod{i}_lesson_desc")
        lesson_froms = form.getlist(f"mod{i}_lesson_from")
        number = len(curriculum_rows) + 1
        old = (form.get(f"mod{i}_from") or "").strip()
        if old.isdigit():
            module_moves[int(old)] = number
        lessons = []
        for pos in sorted(_lesson_numbers(form, i)):
            j = pos - 1
            lt = (lesson_titles[j] or "").strip()
            ld = (lesson_descs[j] if j < len(lesson_descs) else "").strip()
            if not lt:
                lt = f"Lesson {len(lessons) + 1}"
                unnamed.append(f"{t} · {lt}")
            lessons.append({"title": lt[:160], "description": ld[:8000]})
            was = (lesson_froms[j] if j < len(lesson_froms) else "").strip()
            if was.isdigit():
                lesson_moves.setdefault(number, {})[int(was)] = len(lessons)
        # Each module's own place in the schedule. Only one of these is read
        # when the course runs, depending on the mode, but both are kept so
        # switching modes to compare and switching back loses nothing.
        release = parse_owner_parts(
            (form.get(f"mod{i}_release_date") or "").strip(),
            (form.get(f"mod{i}_release_time") or "").strip() or "09:00",
            getattr(current_user, "timezone", None),
        ) if (form.get(f"mod{i}_release_date") or "").strip() else None
        curriculum_rows.append({
            "title": t[:160],
            "description": (form.get(f"mod{i}_desc") or "").strip()[:500],
            "lessons": lessons,
            "release_at": release.isoformat() if release else "",
            "gap_days": (form.get(f"mod{i}_gap_days") or "").strip(),
        })
        module_numbers[i] = len(curriculum_rows)
    product.set_curriculum(curriculum_rows)
    if unnamed:
        flash("Saved " + ", ".join(unnamed[:4])
              + (" and more" if len(unnamed) > 4 else "")
              + " under a stand-in name — nothing typed there was lost, and "
              "you can rename them whenever.", "info")
    if tracked:
        removed = {int(v) for v in form.getlist("removed_module")
                   if str(v).strip().isdigit()}
        _move_module_content(product, module_moves, lesson_moves,
                             old_module_count, removed)

    product.drip_enabled = bool(form.get("drip"))
    days = (form.get("drip_interval_days") or "").strip()
    if days:
        try:
            product.drip_interval_days = max(1, min(365, int(days)))
        except ValueError:
            pass
    if not product.drip_interval_days:
        product.drip_interval_days = 7
    mode = (form.get("drip_mode") or "").strip().lower()
    product.drip_mode = mode if mode in DRIP_MODES else "interval"
    tz_name = getattr(current_user, "timezone", None)
    starts_date = (form.get("drip_starts_date") or "").strip()
    if starts_date:
        product.drip_starts_at = parse_owner_parts(
            starts_date,
            (form.get("drip_starts_time") or "").strip() or "09:00", tz_name)
        if product.drip_starts_at is None:
            flash("That release date didn't look right, so the modules will "
                  "open from each buyer's own start instead.", "info")
    else:
        product.drip_starts_at = None

    shelf_date = (form.get("off_shelf_date") or "").strip()
    if shelf_date:
        product.off_shelf_at = parse_owner_parts(
            shelf_date,
            (form.get("off_shelf_time") or "").strip() or "23:59", tz_name)
        if product.off_shelf_at is None:
            flash("That last-day-on-sale date didn't look right, so this is "
                  "still on sale.", "info")
    else:
        product.off_shelf_at = None

    perk_tier = (form.get("perk_tier") or "").strip().lower()
    product.perk_membership_tier = (
        perk_tier if perk_tier in ("healing", "creator", "full_bloom") else None)
    try:
        perk_months = max(0, min(60, int((form.get("perk_months") or "0").strip() or 0)))
    except ValueError:
        perk_months = 0
    if product.perk_membership_tier and perk_months < 1:
        perk_months = 1
    product.perk_membership_months = perk_months if product.perk_membership_tier else 0

    product.stripe_price_id = (form.get("stripe") or "").strip() or None
    price = _parse_price_cents(form.get("price"))
    if price is not None or form.get("price") is not None:
        # Allow clearing price with empty field on edit
        if (form.get("price") or "").strip() == "":
            product.price_cents = None
        elif price is not None:
            product.price_cents = price
    compare = _parse_price_cents(form.get("compare_at"))
    if (form.get("compare_at") or "").strip() == "":
        product.compare_at_cents = None
    elif compare is not None:
        product.compare_at_cents = compare

    reverts_date = (form.get("price_reverts_date") or "").strip()
    if reverts_date:
        product.price_reverts_at = parse_owner_parts(
            reverts_date,
            (form.get("price_reverts_time") or "").strip() or "23:59", tz_name)
        if product.price_reverts_at is None:
            flash("That date for the price going back up didn't look right, so "
                  "the page won't mention one.", "info")
    else:
        product.price_reverts_at = None

    # A promo needs both halves. Clearing either one takes the banner down,
    # rather than leaving a code with no price or a price with no code.
    promo_code = (form.get("promo_code") or "").strip().upper()[:40]
    promo_price = _parse_price_cents(form.get("promo_price"))
    if not promo_code or (form.get("promo_price") or "").strip() == "":
        product.promo_code = None
        product.promo_price_cents = None
        product.promo_ends_at = None
    else:
        product.promo_code = promo_code
        product.promo_price_cents = promo_price
        ends_date = (form.get("promo_ends_date") or "").strip()
        product.promo_ends_at = parse_owner_parts(
            ends_date,
            (form.get("promo_ends_time") or "").strip() or "23:59",
            getattr(current_user, "timezone", None),
        ) if ends_date else None
        if ends_date and product.promo_ends_at is None:
            flash("That promo end date didn't look right, so the sale was left "
                  "running with no deadline.", "info")
    if (product.promo_price_cents is not None and product.price_cents is not None
            and product.promo_price_cents >= product.price_cents):
        flash("The promo price needs to be lower than the normal price, so "
              "the banner was left off.", "info")
        product.promo_code = None
        product.promo_price_cents = None

    if form.get("use_accent"):
        product.accent_color = _parse_accent(form.get("accent"))
    else:
        product.accent_color = None

    product.test_mode = bool(form.get("test_mode"))

    want_live = bool(form.get("live"))
    if want_live:
        blockers = product.publish_blockers()
        if blockers:
            product.status = "draft"
        else:
            product.status = "published"
    else:
        product.status = "draft"

    new_slug = (form.get("slug") or "").strip().lower()
    if new_slug:
        cleaned = slugify_title(new_slug)
        if cleaned and cleaned != product.slug:
            product.slug = unique_product_slug(cleaned, exclude_id=product.id)
    return module_numbers


def _warn_test_not_live(product: Product) -> None:
    """A test product still has to be live before there is anything to buy."""
    if product.test_mode and product.status != "published":
        flash("Test mode is on, but the product is still a draft — tick "
              "“Live on Courses” too if you want to try buying it.", "info")


def _told_suffix(told: int) -> str:
    """Owners never receive their own broadcast, so say it went out."""
    if not told:
        return ""
    who = "member was" if told == 1 else "members were"
    return f" {told} {who} notified."


def _announce_product(product: Product) -> int:
    """Tell members when a product goes live. No-op for drafts and test items."""
    if product.status != "published" or product.test_mode:
        return 0
    from ..services.social_graph import notify_everyone
    return notify_everyone(
        kind="course",
        body=f"New in Courses & Guides: “{(product.title or '')[:80]}”",
        url=url_for("main.course_detail", slug=product.slug),
        actor_id=current_user.id,
        exclude_id=current_user.id,
    )


def _remap_module_files(product: Product, module_numbers: dict[int, int]) -> None:
    """Follow module files when rows shift; a deleted row leaves its file loose."""
    for asset in product.assets:
        if asset.module_index:
            asset.module_index = module_numbers.get(asset.module_index)


def _upload_limits() -> dict:
    """What Studio may promise about file sizes, in the two upload paths."""
    from ..services.assets import MAX_BYTES
    return {
        "course_max_mb": current_app.config["COURSE_UPLOAD_MAX_MB"],
        "inline_max_mb": MAX_BYTES // (1024 * 1024),
    }


def _save_module_files(product: Product, form, files,
                       module_numbers: dict[int, int]) -> int:
    """Add whatever was attached to each module on this save.

    A module holds as much as the owner wants: several videos, several
    documents and several written extracts. Everything here is added — nothing
    replaces what a module already has, which is removed on its own.
    """
    from ..services.assets import AssetError, add_asset, add_text

    saved = 0
    for row_number, module_number in module_numbers.items():
        title = (form.get(f"mod{row_number}_title") or "").strip()[:160] or None
        uploads = [
            u for u in files.getlist(f"mod{row_number}_file")
            if u and getattr(u, "filename", None)
        ]
        for upload in uploads:
            try:
                add_asset(product, upload, title=title, module_index=module_number)
            except AssetError as exc:
                flash(f"Module {module_number}: {exc}", "error")
                continue
            except Exception:
                log.exception("module file upload failed")
                flash(f"Module {module_number}: that file didn’t upload.", "error")
                continue
            saved += 1

        # Files attached to a lesson on the page itself. The uploader that
        # sends files one at a time only exists once the product has an id to
        # upload against, so without this a lesson on a product being created
        # has no way to take anything at all.
        for pos, lesson_number in _lesson_numbers(form, row_number).items():
            for upload in files.getlist(f"mod{row_number}_lesson{pos}_file"):
                if not (upload and getattr(upload, "filename", None)):
                    continue
                # No title: several files can share a lesson, and each one
                # reading as its own filename beats all of them reading as the
                # lesson they are in.
                try:
                    add_asset(product, upload, module_index=module_number,
                              lesson_index=lesson_number)
                except AssetError as exc:
                    flash(f"Lesson {lesson_number}: {exc}", "error")
                    continue
                except Exception:
                    log.exception("lesson file upload failed")
                    flash(f"Lesson {lesson_number}: that file didn’t upload.",
                          "error")
                    continue
                saved += 1

        bodies = form.getlist(f"mod{row_number}_text_body")
        headings = form.getlist(f"mod{row_number}_text_title")
        for i, body in enumerate(bodies):
            if not (body or "").strip():
                continue
            heading = headings[i] if i < len(headings) else ""
            try:
                add_text(product, body,
                         title=heading or f"Extract {i + 1}",
                         module_index=module_number)
            except AssetError as exc:
                flash(f"Module {module_number}: {exc}", "error")
                continue
            saved += 1
    return saved


def _save_asset_notes(product: Product, form) -> int:
    """Written extracts belonging to one file, plus edits to existing ones.

    ``note_<id>_*`` rewrites an extract where it sits — module-level ones
    included, which until now could only be removed and retyped.
    ``newnote_<parentId>_*`` repeats, one per extract being added to a file.
    """
    from ..services.assets import AssetError, add_note, edit_text

    touched = 0
    for asset in product.assets:
        if asset.is_text() and f"note_{asset.id}_body" in form:
            body = form.get(f"note_{asset.id}_body") or ""
            title = (form.get(f"note_{asset.id}_title") or "").strip()[:160]
            try:
                edit_text(asset, body, title=title)
            except AssetError as exc:
                flash(f"“{asset.display_title()}”: {exc}", "error")
                continue
            touched += 1

    for asset in product.top_level_assets():
        if asset.is_text():
            continue
        bodies = form.getlist(f"newnote_{asset.id}_body")
        headings = form.getlist(f"newnote_{asset.id}_title")
        for i, body in enumerate(bodies):
            if not (body or "").strip():
                continue
            heading = (headings[i] if i < len(headings) else "").strip()
            try:
                add_note(asset, body, title=heading or f"Extract {i + 1}")
            except AssetError as exc:
                flash(f"“{asset.display_title()}”: {exc}", "error")
                continue
            touched += 1
    return touched


def _save_asset_lessons(product: Product, form) -> None:
    """Pin each module file to a lesson from its ``asset_<id>_lesson`` select.

    Content is uploaded to a module, then sorted into that module's lessons
    here (empty / 0 = module intro). Read after the curriculum is saved so the
    lesson count is known; a stale number just falls back to the intro.
    """
    lesson_counts = {row["number"]: len(row["lessons"])
                     for row in product.modules()}
    for asset in product.top_level_assets():
        key = f"asset_{asset.id}_lesson"
        if key not in form:
            continue
        raw = (form.get(key) or "").strip()
        wanted = int(raw) if raw.isdigit() else 0
        count = lesson_counts.get(asset.module_index or 0, 0)
        asset.lesson_index = wanted if (asset.module_index
                                        and 1 <= wanted <= count) else None
        # Extracts hanging off this file ride along with it.
        for note in asset.notes:
            note.lesson_index = asset.lesson_index


@bp.route("/products")
@admin_required
def products():
    items = (Product.query
             .options(joinedload(Product.assets))
             .order_by(Product.track, Product.sort_order, Product.id).all())
    return render_template("admin/products.html", items=items)


@bp.route("/products/new", methods=["GET", "POST"])
@admin_required
def product_new():
    from ..services.catalog import unique_product_slug
    from ..services.assets import AssetError, add_asset
    from ..services.product_covers import CoverError, process_and_save as save_cover

    if request.method == "POST":
        title = (request.form.get("title") or "").strip()[:160]
        if not title:
            flash("Give the product a title.", "error")
            return redirect(url_for("admin.product_new"))
        product = Product(
            title=title,
            slug=unique_product_slug(title),
            type="guide",
            track="healing",
            status="draft",
            currency="USD",
        )
        module_numbers = _apply_product_fields(product, request.form)
        if not product.title:
            product.title = title
        db.session.add(product)
        db.session.flush()
        _save_module_files(product, request.form, request.files, module_numbers)
        cover = request.files.get("cover")
        if cover and getattr(cover, "filename", None):
            try:
                product.cover_url = save_cover(product.id, cover)
            except CoverError as exc:
                flash(str(exc), "error")
            except Exception:
                log.exception("create product cover upload failed")
                flash("Product saved, but the cover didn’t upload.", "error")
        upload = request.files.get("asset")
        if upload and getattr(upload, "filename", None):
            try:
                add_asset(
                    product, upload,
                    title=(request.form.get("asset_title") or "").strip()[:160] or None,
                )
            except AssetError as exc:
                flash(str(exc), "error")
            except Exception:
                log.exception("create product asset upload failed")
                flash("Product saved, but the reading file didn’t upload.", "error")
        teasers = request.files.getlist("teasers")
        gallery_urls = []
        for teaser in teasers:
            if not teaser or not getattr(teaser, "filename", None):
                continue
            try:
                from ..services.product_covers import process_gallery_image
                gallery_urls.append(process_gallery_image(product.id, teaser))
            except CoverError as exc:
                flash(str(exc), "error")
            except Exception:
                log.exception("create product teaser upload failed")
        if gallery_urls:
            product.set_gallery(gallery_urls)
        blockers = product.publish_blockers() if product.status == "published" else []
        if blockers:
            product.status = "draft"
            flash(
                "Saved as draft — still need: " + ", ".join(blockers) + ".",
                "info",
            )
        told = _announce_product(product)
        db.session.commit()
        flash(f"“{product.title}” created." + _told_suffix(told), "success")
        _warn_test_not_live(product)
        return redirect(url_for("admin.product_edit", product_id=product.id))

    blank = Product(title="", slug="", type="guide", track="healing",
                    status="draft", currency="USD", drip_enabled=False,
                    drip_interval_days=7, perk_membership_months=0)
    return render_template(
        "admin/product_form.html",
        product=blank,
        is_new=True,
        modules=[_blank_module(1), _blank_module(2)],
        max_modules=MAX_MODULES,
        drip_modes=DRIP_MODES,
        product_kinds=PRODUCT_KINDS,
        **_upload_limits(),
    )


@bp.route("/products/<int:product_id>/edit", methods=["GET", "POST"])
@admin_required
def product_edit(product_id):
    product = (Product.query
               .options(joinedload(Product.assets))
               .filter_by(id=product_id).first())
    if product is None:
        flash("That product was already gone.", "info")
        return redirect(url_for("admin.products"))

    if request.method == "POST":
        from ..services.product_covers import CoverError, process_and_save as save_cover

        prev_public = product.status == "published" and not product.test_mode
        module_numbers = _apply_product_fields(product, request.form)
        _remap_module_files(product, module_numbers)
        _save_asset_notes(product, request.form)
        _save_module_files(product, request.form, request.files, module_numbers)
        _save_asset_lessons(product, request.form)
        cover = request.files.get("cover")
        if cover and getattr(cover, "filename", None):
            try:
                product.cover_url = save_cover(product.id, cover)
            except CoverError as exc:
                flash(str(exc), "error")
            except Exception:
                log.exception("edit product cover upload failed")
                flash("Product saved, but the cover didn’t upload.", "error")
        blockers = product.publish_blockers() if product.status == "published" else []
        if blockers:
            product.status = "draft"
            flash(
                "Kept as draft — still need: " + ", ".join(blockers) + ".",
                "info",
            )
        elif product.status == "published" and product.test_mode:
            flash(f"“{product.title}” is in test mode — only you and your "
                  "co-owners can see it or buy it.", "success")
        elif product.status == "published" and not prev_public:
            told = _announce_product(product)
            flash(f"“{product.title}” is now live on Courses." + _told_suffix(told),
                  "success")
        else:
            flash("Product saved.", "success")
        _warn_test_not_live(product)
        db.session.commit()
        return redirect(url_for("admin.product_edit", product_id=product.id))

    # Anything uploaded before decks were drawn into pages is still a .pptx no
    # buyer can read here. Opening the product is as good a moment as any.
    from ..services import assets as asset_svc
    try:
        drawn = asset_svc.redraw_decks(product)
        if drawn:
            db.session.commit()
            flash(f"Turned {drawn} slide deck{'' if drawn == 1 else 's'} into "
                  "pages — buyers read those on the site now.", "success")
    except Exception:
        db.session.rollback()
        log.exception("studio: could not draw this product's decks")

    modules = product.modules()
    while len(modules) < 2:
        modules.append(_blank_module(len(modules) + 1))
    return render_template(
        "admin/product_form.html",
        product=product,
        is_new=False,
        modules=modules,
        max_modules=MAX_MODULES,
        drip_modes=DRIP_MODES,
        product_kinds=PRODUCT_KINDS,
        blockers=product.publish_blockers(),
        **_upload_limits(),
    )


@bp.route("/products/<int:product_id>/assets", methods=["POST"])
@admin_required
def product_asset_upload(product_id):
    from ..services.assets import AssetError, add_asset

    product = db.session.get(Product, product_id)
    if product is None:
        flash("That product was already gone.", "info")
        return redirect(url_for("admin.products"))
    upload = request.files.get("asset")
    title = (request.form.get("asset_title") or "").strip()[:160] or None
    try:
        asset = add_asset(product, upload, title=title)
        db.session.commit()
        flash(f"Uploaded “{asset.display_title()}” for on-site reading.", "success")
    except AssetError as exc:
        db.session.rollback()
        flash(str(exc), "error")
    except Exception:
        db.session.rollback()
        log.exception("product asset upload failed")
        flash("Could not upload that file. Try a smaller PDF or H5P.", "error")
    return redirect(url_for("admin.product_edit", product_id=product_id))


@bp.route("/products/<int:product_id>/assets/<int:asset_id>/delete", methods=["POST"])
@admin_required
def product_asset_delete(product_id, asset_id):
    from ..services import assets as asset_svc

    product = db.session.get(Product, product_id)
    asset = db.session.get(ProductAsset, asset_id)
    if product is None or asset is None or asset.product_id != product.id:
        flash("That file was already gone.", "info")
        return redirect(url_for("admin.products"))
    label = asset.display_title()
    asset_svc.delete_file(asset)
    db.session.delete(asset)
    db.session.commit()
    flash(f"Removed “{label}”.", "success")
    return redirect(url_for("admin.product_edit", product_id=product_id))


# --- big uploads, a slice at a time ------------------------------------------
# Cloudflare Free rejects any request body over roughly 100 MB, so a lesson
# video cannot arrive in one piece however the app is configured. Studio cuts
# the file up in the browser and posts the slices here instead.

@bp.route("/products/<int:product_id>/uploads/begin", methods=["POST"])
@admin_required
def product_upload_begin(product_id):
    from ..services.assets import AssetError, begin_upload

    product = db.session.get(Product, product_id)
    if product is None:
        return jsonify({"error": "That product was already gone."}), 404
    payload = request.get_json(silent=True) or {}
    try:
        upload_id = begin_upload(
            str(payload.get("filename") or ""),
            int(payload.get("size") or 0))
    except (AssetError, TypeError, ValueError) as exc:
        return jsonify({"error": str(exc) or "That file didn't look right."}), 400
    return jsonify({
        "upload_id": upload_id,
        "chunk_bytes": current_app.config["COURSE_CHUNK_MB"] * 1024 * 1024,
    })


@bp.route("/products/<int:product_id>/uploads/<upload_id>/chunk", methods=["POST"])
@admin_required
def product_upload_chunk(product_id, upload_id):
    from ..services.assets import AssetError, append_chunk

    if db.session.get(Product, product_id) is None:
        return jsonify({"error": "That product was already gone."}), 404
    part = request.files.get("chunk")
    data = part.read() if part else request.get_data()
    if not data:
        return jsonify({"error": "That slice was empty."}), 400
    try:
        received = append_chunk(upload_id, data)
    except AssetError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"received": received})


@bp.route("/products/<int:product_id>/uploads/<upload_id>/finish", methods=["POST"])
@admin_required
def product_upload_finish(product_id, upload_id):
    from ..services.assets import AssetError, abort_upload, finish_upload

    product = db.session.get(Product, product_id)
    if product is None:
        return jsonify({"error": "That product was already gone."}), 404
    payload = request.get_json(silent=True) or {}
    module = payload.get("module")
    try:
        module_index = int(module) if module else None
    except (TypeError, ValueError):
        module_index = None
    lesson = payload.get("lesson")
    try:
        lesson_index = int(lesson) if lesson else None
    except (TypeError, ValueError):
        lesson_index = None
    if not module_index:
        lesson_index = None  # a loose file has no lesson
    elif lesson_index and lesson_index > product.lesson_count(module_index):
        # The lesson was added in the editor but never saved, so pinning the
        # file to it would strand the file. Land it in the module instead,
        # where the owner can see it and sort it once the lesson exists.
        lesson_index = None
    try:
        asset = finish_upload(
            product, upload_id, str(payload.get("filename") or ""),
            title=(str(payload.get("title") or "").strip() or None),
            module_index=module_index, lesson_index=lesson_index)
        db.session.commit()
    except AssetError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 400
    except Exception:
        db.session.rollback()
        abort_upload(upload_id)
        log.exception("course upload finish failed")
        return jsonify({"error": "We couldn't save that file. Try again."}), 500
    return jsonify({
        "asset_id": asset.id,
        "title": asset.display_title(),
        "kind": asset.kind,
        "kind_label": asset.kind_label(),
        "size": asset.size_display(),
        "module": asset.module_index or 0,
        "lesson": asset.lesson_index or 0,
        "delete_url": url_for("admin.product_asset_delete",
                              product_id=product.id, asset_id=asset.id),
    })


@bp.route("/products/<int:product_id>/uploads/<upload_id>/abort", methods=["POST"])
@admin_required
def product_upload_abort(product_id, upload_id):
    from ..services.assets import abort_upload
    abort_upload(upload_id)
    return jsonify({"ok": True})


@bp.route("/products/<int:product_id>/cover", methods=["POST"])
@admin_required
def product_cover_upload(product_id):
    from ..services.product_covers import CoverError, clear as clear_cover, process_and_save

    product = db.session.get(Product, product_id)
    if product is None:
        flash("That product was already gone.", "info")
        return redirect(url_for("admin.products"))
    if request.form.get("clear_cover"):
        clear_cover(product.id)
        product.cover_url = None
        db.session.commit()
        flash("Cover removed — the flower default is back.", "success")
        return redirect(url_for("admin.product_edit", product_id=product_id))
    upload = request.files.get("cover")
    try:
        product.cover_url = process_and_save(product.id, upload)
        db.session.commit()
        flash("Cover image saved.", "success")
    except CoverError as exc:
        db.session.rollback()
        flash(str(exc), "error")
    except Exception:
        db.session.rollback()
        log.exception("product cover upload failed")
        flash("Could not upload that cover. Try a JPG or PNG under 8 MB.", "error")
    return redirect(url_for("admin.product_edit", product_id=product_id))


@bp.route("/products/<int:product_id>/gallery", methods=["POST"])
@admin_required
def product_gallery_upload(product_id):
    from ..services.product_covers import (
        CoverError, clear_gallery_image, process_gallery_image,
    )

    product = db.session.get(Product, product_id)
    if product is None:
        flash("That product was already gone.", "info")
        return redirect(url_for("admin.products"))

    remove_url = (request.form.get("remove_url") or "").strip()
    if remove_url:
        gallery = [u for u in product.gallery() if u != remove_url]
        product.set_gallery(gallery)
        # Best-effort file delete when URL is ours
        prefix = f"/media/product-gallery/{product.id}/"
        if remove_url.startswith(prefix):
            clear_gallery_image(product.id, remove_url[len(prefix):])
        db.session.commit()
        flash("Teaser removed.", "success")
        return redirect(url_for("admin.product_edit", product_id=product_id))

    gallery = product.gallery()
    added = 0
    for teaser in request.files.getlist("teasers"):
        if not teaser or not getattr(teaser, "filename", None):
            continue
        try:
            gallery.append(process_gallery_image(product.id, teaser))
            added += 1
        except CoverError as exc:
            flash(str(exc), "error")
        except Exception:
            log.exception("product teaser upload failed")
            flash("Could not upload one of the teaser images.", "error")
    if added:
        product.set_gallery(gallery)
        db.session.commit()
        flash(f"Added {added} teaser image{'s' if added != 1 else ''}.", "success")
    return redirect(url_for("admin.product_edit", product_id=product_id))


def _delete_product(product: Product) -> int:
    """Delete a catalogue product; return count of unlinked past orders."""
    from ..models import CourseProgress, Order, Testimonial
    from ..services.product_covers import clear as clear_cover, clear_all_gallery

    order_n = product.orders.count()
    if order_n:
        (Order.query.filter_by(product_id=product.id)
         .update({Order.product_id: None}, synchronize_session=False))
    (Testimonial.query.filter_by(product_id=product.id)
     .update({Testimonial.product_id: None}, synchronize_session=False))
    (CourseProgress.query.filter_by(product_id=product.id)
     .update({CourseProgress.product_id: None}, synchronize_session=False))

    clear_cover(product.id)
    clear_all_gallery(product.id)
    from ..services import assets as asset_svc
    for asset in product.assets:
        asset_svc.delete_file(asset)
    # Rows go with the product's own cascade. Naming each asset here as well
    # would delete an extract down two paths at once — once for its file and
    # once for the product — and the second DELETE would find nothing.
    db.session.delete(product)
    return order_n


@bp.route("/products/<int:product_id>/delete", methods=["POST"])
@admin_required
def product_delete(product_id):
    product = db.session.get(Product, product_id)
    if product is None:
        flash("That product was already gone.", "info")
        return redirect(url_for("admin.products"))
    title = product.title
    order_n = _delete_product(product)
    db.session.commit()
    if order_n:
        flash(
            f"Deleted “{title}”. {order_n} past order"
            f"{'s' if order_n != 1 else ''} stay in your records, unlinked.",
            "success",
        )
    else:
        flash(f"Deleted “{title}”.", "success")
    return redirect(url_for("admin.products"))


@bp.route("/products/bulk-delete", methods=["POST"])
@admin_required
def products_bulk_delete():
    ids = _form_ids()
    if not ids:
        flash("Select at least one product to delete.", "error")
        return redirect(url_for("admin.products"))
    deleted = 0
    for pid in ids:
        product = db.session.get(Product, pid)
        if product is None:
            continue
        _delete_product(product)
        deleted += 1
    db.session.commit()
    flash(
        f"Deleted {deleted} product{'s' if deleted != 1 else ''}.",
        "success" if deleted else "info",
    )
    return redirect(url_for("admin.products"))


# ================================ QUOTES =====================================

@bp.route("/quotes")
@admin_required
def quotes():
    items = Quote.query.order_by(Quote.id.desc()).all()
    fav_counts = dict(
        db.session.query(QuoteFavorite.quote_id, func.count(QuoteFavorite.id))
        .group_by(QuoteFavorite.quote_id).all()
    )
    pins = QuotePin.query.filter(QuotePin.date >= date.today()).order_by(QuotePin.date).all()
    tomorrow = date.today() + timedelta(days=1)
    return render_template("admin/quotes.html", quotes=items, fav_counts=fav_counts,
                           pins=pins, tomorrow=tomorrow,
                           tomorrow_quote=quotes_service.quote_for(tomorrow),
                           categories=QUOTE_CATEGORIES)


@bp.route("/quotes/save", methods=["POST"])
@bp.route("/quotes/<int:quote_id>/save", methods=["POST"])
@admin_required
def quote_save(quote_id=None):
    quote = db.session.get(Quote, quote_id) if quote_id else Quote()
    if quote_id and quote is None:
        abort(404)
    text = (request.form.get("text") or "").strip()
    category = request.form.get("category")
    if not text or len(text) > 240:
        flash("Quote text is required (240 characters max).", "error")
        return redirect(url_for("admin.quotes"))
    if category not in QUOTE_CATEGORIES:
        flash("Pick a category.", "error")
        return redirect(url_for("admin.quotes"))
    quote.text = text
    quote.author = (request.form.get("author") or "").strip() or None
    quote.category = category
    quote.active = bool(request.form.get("active", quote_id is None))
    if quote.id is None:
        db.session.add(quote)
    db.session.commit()
    flash("Quote saved.", "success")
    return redirect(url_for("admin.quotes"))


@bp.route("/quotes/<int:quote_id>/toggle", methods=["POST"])
@admin_required
def quote_toggle(quote_id):
    quote = db.session.get(Quote, quote_id) or abort(404)
    quote.active = not quote.active
    db.session.commit()
    return redirect(url_for("admin.quotes"))


@bp.route("/quotes/<int:quote_id>/delete", methods=["POST"])
@admin_required
def quote_delete(quote_id):
    quote = db.session.get(Quote, quote_id) or abort(404)
    QuotePin.query.filter_by(quote_id=quote.id).delete()
    db.session.delete(quote)
    db.session.commit()
    flash("Quote deleted.", "success")
    return redirect(url_for("admin.quotes"))


@bp.route("/quotes/bulk-delete", methods=["POST"])
@admin_required
def quotes_bulk_delete():
    ids = _form_ids()
    if not ids:
        flash("Select at least one quote to delete.", "error")
        return redirect(url_for("admin.quotes"))
    deleted = 0
    for qid in ids:
        quote = db.session.get(Quote, qid)
        if quote is None:
            continue
        QuotePin.query.filter_by(quote_id=quote.id).delete()
        QuoteFavorite.query.filter_by(quote_id=quote.id).delete()
        db.session.delete(quote)
        deleted += 1
    db.session.commit()
    flash(
        f"Deleted {deleted} quote{'s' if deleted != 1 else ''}.",
        "success" if deleted else "info",
    )
    return redirect(url_for("admin.quotes"))


@bp.route("/quotes/pin", methods=["POST"])
@admin_required
def quote_pin():
    try:
        pin_date = date.fromisoformat(request.form.get("date", ""))
        quote_id = int(request.form.get("quote_id", ""))
    except (ValueError, TypeError):
        flash("Pick a date and a quote to pin.", "error")
        return redirect(url_for("admin.quotes"))
    if db.session.get(Quote, quote_id) is None:
        abort(404)
    pin = QuotePin.query.filter_by(date=pin_date).first()
    if pin:
        pin.quote_id = quote_id
    else:
        db.session.add(QuotePin(date=pin_date, quote_id=quote_id))
    db.session.commit()
    flash(f"Pinned for {pin_date.isoformat()}.", "success")
    return redirect(url_for("admin.quotes"))


@bp.route("/quotes/pin/<int:pin_id>/delete", methods=["POST"])
@admin_required
def quote_unpin(pin_id):
    pin = db.session.get(QuotePin, pin_id) or abort(404)
    db.session.delete(pin)
    db.session.commit()
    flash("Pin removed \u2014 that day goes back to rotation.", "success")
    return redirect(url_for("admin.quotes"))


@bp.route("/quotes/pins/bulk-delete", methods=["POST"])
@admin_required
def quotes_pins_bulk_delete():
    ids = _form_ids()
    if not ids:
        flash("Select at least one pin to remove.", "error")
        return redirect(url_for("admin.quotes"))
    deleted = 0
    for pid in ids:
        pin = db.session.get(QuotePin, pid)
        if pin is None:
            continue
        db.session.delete(pin)
        deleted += 1
    db.session.commit()
    flash(
        f"Removed {deleted} pin{'s' if deleted != 1 else ''}.",
        "success" if deleted else "info",
    )
    return redirect(url_for("admin.quotes"))


def _parse_import(raw: str):
    """`text | author | category` per line -> (rows, problems)."""
    rows, problems = [], []
    existing = {q.text.strip().lower() for q in Quote.query.all()}
    seen_in_batch = set()
    for i, line in enumerate(raw.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        text = parts[0] if parts else ""
        author = parts[1] if len(parts) > 1 and parts[1] else None
        category = (parts[2].lower() if len(parts) > 2 else "comfort")
        if not text or len(text) > 240:
            problems.append(f"Line {i}: text missing or over 240 chars \u2014 skipped.")
            continue
        if category not in QUOTE_CATEGORIES:
            problems.append(f'Line {i}: unknown category "{category}" \u2014 using comfort.')
            category = "comfort"
        key = text.lower()
        if key in existing or key in seen_in_batch:
            problems.append(f"Line {i}: duplicate \u2014 skipped.")
            continue
        seen_in_batch.add(key)
        rows.append({"text": text, "author": author, "category": category})
    return rows, problems


@bp.route("/quotes/import", methods=["POST"])
@admin_required
def quote_import():
    raw = request.form.get("bulk") or ""
    rows, problems = _parse_import(raw)
    if request.form.get("confirm") == "yes":
        for row in rows:
            db.session.add(Quote(**row))
        db.session.commit()
        flash(f"Imported {len(rows)} quotes." + (f" ({len(problems)} lines skipped.)" if problems else ""), "success")
        return redirect(url_for("admin.quotes"))
    return render_template("admin/quote_import_preview.html", rows=rows,
                           problems=problems, raw=raw)


# ============================ TESTIMONIALS ===================================

@bp.route("/testimonials")
@admin_required
def testimonials():
    items = Testimonial.query.order_by(Testimonial.sort_order).all()
    products = Product.query.order_by(Product.title).all()
    return render_template("admin/testimonials.html", items=items, products=products)


@bp.route("/testimonials/save", methods=["POST"])
@bp.route("/testimonials/<int:item_id>/save", methods=["POST"])
@admin_required
def testimonial_save(item_id=None):
    item = db.session.get(Testimonial, item_id) if item_id else Testimonial()
    if item_id and item is None:
        abort(404)
    quote = (request.form.get("quote") or "").strip()
    first_name = (request.form.get("first_name") or "").strip()[:60]
    if not quote or not first_name:
        flash("A testimonial needs both a quote and a first name.", "error")
        return redirect(url_for("admin.testimonials"))
    item.quote = quote
    item.first_name = first_name
    item.product_id = int(request.form["product_id"]) if request.form.get("product_id") else None
    item.show_on_home = bool(request.form.get("show_on_home"))
    try:
        item.sort_order = int(request.form.get("sort_order") or 0)
    except ValueError:
        item.sort_order = 0
    if item.id is None:
        db.session.add(item)
    db.session.commit()
    flash("Testimonial saved.", "success")
    return redirect(url_for("admin.testimonials"))


@bp.route("/testimonials/<int:item_id>/delete", methods=["POST"])
@admin_required
def testimonial_delete(item_id):
    item = db.session.get(Testimonial, item_id) or abort(404)
    db.session.delete(item)
    db.session.commit()
    flash("Testimonial deleted.", "success")
    return redirect(url_for("admin.testimonials"))


@bp.route("/testimonials/bulk-delete", methods=["POST"])
@admin_required
def testimonials_bulk_delete():
    ids = _form_ids()
    if not ids:
        flash("Select at least one testimonial to delete.", "error")
        return redirect(url_for("admin.testimonials"))
    deleted = 0
    for tid in ids:
        item = db.session.get(Testimonial, tid)
        if item is None:
            continue
        db.session.delete(item)
        deleted += 1
    db.session.commit()
    flash(
        f"Deleted {deleted} testimonial{'s' if deleted != 1 else ''}.",
        "success" if deleted else "info",
    )
    return redirect(url_for("admin.testimonials"))


# ================================= FAQ =======================================

@bp.route("/faq")
@admin_required
def faq():
    items = FaqItem.query.order_by(FaqItem.sort_order).all()
    return render_template("admin/faq.html", items=items)


@bp.route("/faq/save", methods=["POST"])
@bp.route("/faq/<int:item_id>/save", methods=["POST"])
@admin_required
def faq_save(item_id=None):
    item = db.session.get(FaqItem, item_id) if item_id else FaqItem()
    if item_id and item is None:
        abort(404)
    question = (request.form.get("question") or "").strip()[:240]
    answer = (request.form.get("answer_md") or "").strip()
    if not question or not answer:
        flash("A FAQ item needs both a question and an answer.", "error")
        return redirect(url_for("admin.faq"))
    item.question = question
    item.answer_md = answer
    try:
        item.sort_order = int(request.form.get("sort_order") or 0)
    except ValueError:
        item.sort_order = 0
    if item.id is None:
        db.session.add(item)
    db.session.commit()
    flash("FAQ saved.", "success")
    return redirect(url_for("admin.faq"))


@bp.route("/faq/<int:item_id>/delete", methods=["POST"])
@admin_required
def faq_delete(item_id):
    item = db.session.get(FaqItem, item_id) or abort(404)
    db.session.delete(item)
    db.session.commit()
    flash("FAQ item deleted.", "success")
    return redirect(url_for("admin.faq"))


@bp.route("/faq/bulk-delete", methods=["POST"])
@admin_required
def faq_bulk_delete():
    ids = _form_ids()
    if not ids:
        flash("Select at least one FAQ item to delete.", "error")
        return redirect(url_for("admin.faq"))
    deleted = 0
    for fid in ids:
        item = db.session.get(FaqItem, fid)
        if item is None:
            continue
        db.session.delete(item)
        deleted += 1
    db.session.commit()
    flash(
        f"Deleted {deleted} FAQ item{'s' if deleted != 1 else ''}.",
        "success" if deleted else "info",
    )
    return redirect(url_for("admin.faq"))


# ================================ PAGES ======================================

EDITABLE_PAGES = (
    ("about", "Her Story (About page)"),
    ("privacy", "Privacy Policy"),
    ("terms", "Terms of Service"),
    ("refunds", "Refund Policy"),
)


@bp.route("/pages")
@admin_required
def pages():
    existing = {p.slug: p for p in Page.query.all()}
    return render_template("admin/pages.html", editable=EDITABLE_PAGES, existing=existing)


@bp.route("/pages/<slug>", methods=["GET", "POST"])
@admin_required
def page_edit(slug):
    labels = dict(EDITABLE_PAGES)
    if slug not in labels:
        abort(404)
    page = Page.query.filter_by(slug=slug).first()
    if request.method == "POST":
        title = (request.form.get("title") or labels[slug]).strip()[:160]
        body = request.form.get("body_md") or ""
        if page is None:
            page = Page(slug=slug, title=title, body_md=body)
            db.session.add(page)
        else:
            page.title = title
            page.body_md = body
        db.session.commit()
        flash("Page saved.", "success")
        return redirect(url_for("admin.pages"))
    return render_template("admin/page_form.html", page=page, slug=slug, label=labels[slug])


# ============================= LEGACY REDIRECTS ==============================
# Old Studio paths — keep as soft redirects so bookmarks don't 404.

@bp.route("/subscribers")
@bp.route("/subscribers/export.csv")
@admin_required
def subscribers():
    flash("Main payment totals are on the Dashboard. Full history is in Stripe.", "info")
    return redirect(url_for("admin.dashboard"))


@bp.route("/orders")
@bp.route("/orders/export.csv")
@admin_required
def orders():
    flash("Payment totals are on the Dashboard. Full history is in Stripe.", "info")
    return redirect(url_for("admin.dashboard"))


# =============================== SPOTLIGHT ===================================

#: fields the spotlight form posts
_SPOTLIGHT_FORM_KEYS = (
    "creator_name",
    "creator_instagram",
    "creator_image_url",
    "creator_blurb",
    "creator_expires",
    "reel_url",
    "reel_description",
    "reel_expires",
)
#: everything this page owns — the general Settings save must leave these alone
_SPOTLIGHT_KEYS = _SPOTLIGHT_FORM_KEYS + (
    "spotlight_creator_notified",
    "spotlight_reel_notified",
)

#: Site images are set by uploading one, never by typing an address. They are
#: kept out of the form sweep below because there is no field to sweep: reading
#: a missing field would blank the uploaded image every time Settings is saved.
_IMAGE_URL_KEYS = ("portrait_url", "hero_image_url")


#: Hosts that hand out photo links good for a while rather than for good.
_TEMPORARY_PHOTO_HOSTS = ("cdninstagram.com", "fbcdn.net")


def _is_temporary_photo_link(url: str | None) -> bool:
    """True for a photo address that will stop working on its own."""
    text = (url or "").strip().lower()
    if not text.startswith("http"):
        return False
    return any(host in text for host in _TEMPORARY_PHOTO_HOSTS)


def _spotlight_end_date(kind: str, raw: str, filled: bool):
    """Run-until date for a slot: the owner's date, else the default run."""
    from ..services import spotlight as spot

    if not filled:
        return None
    try:
        return date.fromisoformat((raw or "").strip()[:10])
    except ValueError:
        return spot.default_end(kind)


@bp.route("/spotlight", methods=["GET", "POST"])
@admin_required
def spotlight():
    """Home-page Creator of the Month + Reel of the Week."""
    from ..services import spotlight as spot

    if request.method == "POST":
        if request.form.get("pick_creator"):
            pick = spot.pick_random_creator(
                get_setting("creator_instagram") or "")
            if pick is None:
                flash(
                    "No one is eligible yet — Creator members need an Instagram "
                    "link on their Bloom Anyway profile to go in the draw.",
                    "info",
                )
                return redirect(url_for("admin.spotlight"))
            flash(
                f"Drew {pick['name']} out of the hat. Check the details below "
                "and hit Save spotlight to put them on the home page.",
                "success",
            )
            return redirect(url_for("admin.spotlight", draft=pick["user_id"]))
        if request.form.get("clear_spotlight_creator"):
            for key in ("creator_name", "creator_instagram", "creator_image_url",
                        "creator_blurb", "creator_expires",
                        "spotlight_creator_notified"):
                set_setting(key, "")
            from ..services.site_images import clear as clear_site_image
            clear_site_image("creator")
            flash("Creator of the month cleared from the home page.", "success")
            return redirect(url_for("admin.spotlight"))
        if request.form.get("clear_spotlight_reel"):
            rotw_svc.clear_featured()
            for key in ("reel_url", "reel_description", "reel_expires",
                        "spotlight_reel_notified"):
                set_setting(key, "")
            flash("Reel of the week cleared from the home page.", "success")
            return redirect(url_for("admin.spotlight"))
        feature_id = (request.form.get("feature_reel") or "").strip()
        if feature_id.isdigit():
            entry = db.session.get(ReelSubmission, int(feature_id)) or abort(404)
            rotw_svc.feature(entry)
            spot.mark_slot_saved("reel", filled=True,
                                 end=spot.default_end("reel"))
            who = entry.author.public_name() if entry.author else "That member"
            flash(f"{who}'s reel is now the Reel of the Week on the home page.",
                  "success")
            return redirect(url_for("admin.spotlight"))

        values = {key: (request.form.get(key) or "").strip()
                  for key in _SPOTLIGHT_FORM_KEYS}
        handle = instagram_handle(values.get("creator_instagram") or "")
        values["creator_instagram"] = handle
        from ..services.site_images import (SiteImageError, clear as clear_site_image,
                                            process_and_save, save_from_url)
        try:
            if request.form.get("clear_creator"):
                clear_site_image("creator")
                values["creator_image_url"] = ""
            creator = request.files.get("creator_file")
            if creator and creator.filename:
                values["creator_image_url"] = process_and_save("creator", creator)
        except SiteImageError as exc:
            flash(str(exc), "error")
            return redirect(url_for("admin.spotlight"))
        # A link straight to Instagram's photo is signed and runs out, leaving
        # a broken circle on the home page weeks later. Anything pointing there
        # is dropped, and a fresh one is copied to us rather than linked.
        if _is_temporary_photo_link(values.get("creator_image_url")):
            values["creator_image_url"] = ""
        if handle and (not values.get("creator_image_url")
                       or not values.get("creator_blurb")):
            preview = fetch_instagram_preview(handle)
            if preview.get("image") and not values.get("creator_image_url"):
                values["creator_image_url"] = save_from_url("creator",
                                                            preview["image"])
            if preview.get("blurb") and not values.get("creator_blurb"):
                values["creator_blurb"] = preview["blurb"]

        creator_end = _spotlight_end_date(
            "creator", values["creator_expires"], bool(values["creator_name"]))
        reel_end = _spotlight_end_date(
            "reel", values["reel_expires"], bool(values["reel_url"]))
        values.pop("creator_expires")
        values.pop("reel_expires")
        for key, val in values.items():
            set_setting(key, val)
        spot.mark_slot_saved("creator", filled=bool(values["creator_name"]),
                             end=creator_end)
        spot.mark_slot_saved("reel", filled=bool(values["reel_url"]),
                             end=reel_end)
        flash("Home spotlight saved.", "success")
        return redirect(url_for("admin.spotlight"))

    values = all_settings()
    ready, missing = spot.eligible_split()
    draft = None
    draft_id = (request.args.get("draft") or "").strip()
    if draft_id.isdigit():
        draft = spot.candidate(int(draft_id))
    if draft:
        # Prefill from the drawn member, but don't publish until she saves.
        values = dict(values)
        values["creator_name"] = draft["name"]
        values["creator_instagram"] = draft["handle"]
        values["creator_blurb"] = draft["bio"] or values.get("creator_blurb") or ""
        if draft["has_photo"]:
            values["creator_image_url"] = url_for(
                "main.avatar", user_id=draft["user_id"], _external=True)
    if values.get("creator_instagram"):
        h = instagram_handle(values["creator_instagram"])
        values["creator_instagram"] = f"@{h}" if h else values["creator_instagram"]
    if not values.get("creator_expires") and values.get("creator_name"):
        values["creator_expires"] = spot.default_end("creator").isoformat()
    if not values.get("reel_expires") and values.get("reel_url"):
        values["reel_expires"] = spot.default_end("reel").isoformat()
    return render_template(
        "admin/spotlight.html",
        values=values,
        eligible=ready,
        no_instagram=missing,
        draft=draft,
        slots=spot.spotlight_slots(),
        reel_entries=rotw_svc.week_submissions(),
        reel_week=rotw_svc.current_week_key(),
        min_shares=rotw_svc.MIN_SHARES,
    )


@bp.route("/spotlight/reel/<int:entry_id>/raw")
@admin_required
def spotlight_reel_raw(entry_id):
    """Download a Reel of the Week entrant's raw video (Studio only)."""
    entry = db.session.get(ReelSubmission, entry_id) or abort(404)
    disk_name = os.path.basename(entry.disk_name or "")
    if not disk_name:
        flash("That entry has no raw video upload.", "error")
        return redirect(url_for("admin.spotlight"))
    directory = os.path.abspath(current_app.config["VIDEO_STORAGE_DIR"])
    if not os.path.isfile(os.path.join(directory, disk_name)):
        flash("That entry's raw video is no longer on the server.", "error")
        return redirect(url_for("admin.spotlight"))
    resp = send_from_directory(
        directory, disk_name,
        mimetype=entry.mime or "application/octet-stream",
        as_attachment=True,
        download_name=entry.filename or "reel.mp4",
        max_age=0,
    )
    resp.headers["Cache-Control"] = "private, no-store"
    return resp


# =============================== SETTINGS ====================================

@bp.route("/settings/test-email", methods=["POST"])
@admin_required
def settings_test_email():
    """Send a one-off test via the live Brevo/SMTP config (Studio only)."""
    to = ((request.form.get("to") or "").strip()
          or (current_user.email or "").strip())
    if not to or "@" not in to:
        flash("Give me an address to send the test to.", "error")
        return redirect(url_for("admin.settings"))

    wanted = (request.form.get("template") or "").strip().lower()
    # The reply templates differ only in which address they wear, so one test
    # path covers all three rather than a near-copy each.
    reply_keys = {"support": "support", "saman": "saman", "ayesha": "ayesha"}
    if wanted in reply_keys:
        key = reply_keys[wanted]
        who = next(s for s in mailer.sender_choices() if s["key"] == key)
        template_id = mailer.reply_template_for(key)
        label = f"{who['name']} reply template (#{template_id})"
        sent = send_customer_support_email(
            to,
            subject=f"Bloom Anyway — test reply from {who['name']}",
            preview=f"If you received this, template #{template_id} is working.",
            header="Bloom Anyway",
            title=f"Test reply from {who['name']}",
            body=(
                f"If you received this, template #{template_id} is wired up "
                "and every placeholder resolved.\n\n"
                "This is how a reply to someone's question will reach them: no "
                "button, no upsell, just the answer."
            ),
            sender=mailer.sender_from(key),
            sender_key=key,
        )
    else:
        label = "general template (#10)"
        sent = send_styled_email(
            to,
            subject="Bloom Anyway — test email",
            preview="If you received this, email sending from the site is working.",
            header="Bloom Anyway",
            title="Test email",
            body=(
                "If you received this, email sending from the site is working.\n\n"
                "This uses the general Brevo template (#10)."
            ),
            button_text="Open Studio",
            button_url=url_for("admin.dashboard", _external=True),
        )

    if sent:
        flash(f"Test email sent to {to} using the {label}. Check inbox and spam.",
              "success")
    else:
        hint = last_send_error() or "Unknown email error — check Render logs for Brevo."
        flash(f"Test email failed. {hint}", "error")
    return redirect(url_for("admin.settings"))


#: Announcements are written on their own page, so a settings save must leave
#: them exactly as they are rather than blanking what isn't on the form.
_ANNOUNCEMENT_KEYS = ("announcement_text", "announcement_expires",
                      "announcement_url")


@bp.route("/announcements", methods=["GET", "POST"])
@admin_required
def announcements():
    """Everything showing on the home page, and the box to write one more.

    Announcements used to live near the bottom of Settings, where nothing
    said whether one was still running: an owner who wrote one a fortnight
    ago and saw nothing on the home page had no way to tell that it had
    quietly passed its date. Here they are the page.
    """
    from ..services.homepage import content_hub_groups
    from ..services.settings import active_announcement, sanitize_announcement_url

    if request.method == "POST":
        if request.form.get("clear_announcement"):
            for key in _ANNOUNCEMENT_KEYS:
                set_setting(key, "")
            flash("Announcement removed.", "success")
            return redirect(url_for("admin.announcements"))
        if request.form.get("add_announcement"):
            body = (request.form.get("ann_body") or "").strip()[:300]
            if body:
                expires = date.today() + timedelta(days=7)  # default: 1 week
                raw = (request.form.get("ann_expires") or "").strip()
                if raw:
                    try:
                        expires = date.fromisoformat(raw)
                    except ValueError:
                        pass
                link = sanitize_announcement_url(request.form.get("ann_url"))
                db.session.add(Announcement(body=body, expires=expires,
                                            link_url=link or None))
                from ..services.social_graph import notify_everyone
                told = notify_everyone(
                    kind="announcement",
                    body=f"Site update: {body[:120]}",
                    url=link or url_for("main.index"),
                    actor_id=current_user.id,
                    exclude_id=current_user.id,
                )
                db.session.commit()
                flash("Announcement added." + _told_suffix(told), "success")
            else:
                flash("Write something first.", "error")
            return redirect(url_for("admin.announcements"))
        remove_id = request.form.get("remove_announcement")
        if remove_id and remove_id.isdigit():
            ann = db.session.get(Announcement, int(remove_id))
            if ann:
                db.session.delete(ann)
                db.session.commit()
            flash("Announcement removed.", "success")
            return redirect(url_for("admin.announcements"))
        remove_ids = _form_ids()
        if request.form.get("bulk_remove_announcements") and remove_ids:
            deleted = 0
            for aid in remove_ids:
                ann = db.session.get(Announcement, aid)
                if ann is None:
                    continue
                db.session.delete(ann)
                deleted += 1
            db.session.commit()
            flash(
                f"Removed {deleted} announcement{'s' if deleted != 1 else ''}.",
                "success" if deleted else "info",
            )
            return redirect(url_for("admin.announcements"))
        flash("Nothing to do with that.", "info")
        return redirect(url_for("admin.announcements"))

    rows = (Announcement.query
            .order_by(Announcement.sort_order,
                      Announcement.created_at.desc()).all())
    quick = active_announcement()
    return render_template(
        "admin/announcements.html",
        rows=rows,
        live=[a for a in rows if a.is_live()],
        quick=quick,
        quick_expires=get_setting("announcement_expires", ""),
        quick_url=get_setting("announcement_url", ""),
        # What the hub has dropped into the same strip, so the page shows
        # everything a member is seeing rather than only half of it.
        hub_groups=content_hub_groups(),
        today=date.today(),
        default_expires=(date.today() + timedelta(days=7)).isoformat(),
    )


@bp.route("/settings", methods=["GET", "POST"])
@admin_required
def settings():
    if request.method == "POST":
        values = {key: (request.form.get(key) or "").strip()
                  for key in SETTING_DEFAULTS
                  if key not in _SPOTLIGHT_KEYS and key not in _IMAGE_URL_KEYS
                  and key not in _ANNOUNCEMENT_KEYS}
        # Site images: uploaded, cleared, or left exactly as they were.
        from ..services.site_images import (SiteImageError, clear as clear_site_image,
                                            process_and_save)
        try:
            if request.form.get("clear_portrait"):
                clear_site_image("portrait")
                values["portrait_url"] = ""
            portrait = request.files.get("portrait_file")
            if portrait and portrait.filename:
                values["portrait_url"] = process_and_save("portrait", portrait)
            if request.form.get("clear_hero"):
                clear_site_image("hero")
                values["hero_image_url"] = ""
            hero = request.files.get("hero_file")
            if hero and hero.filename:
                values["hero_image_url"] = process_and_save("hero", hero)
        except SiteImageError as exc:
            flash(str(exc), "error")
            return redirect(url_for("admin.settings"))
        for key, val in values.items():
            set_setting(key, val)
        flash("Settings saved.", "success")
        return redirect(url_for("admin.settings"))
    values = all_settings()
    return render_template("admin/settings.html", values=values,
                           today=date.today())


# ============================ MARKETPLACE ====================================

@bp.route("/marketplace")
@admin_required
def marketplace():
    listings = (MarketplaceListing.query
                .options(joinedload(MarketplaceListing.author))
                .order_by(MarketplaceListing.active.desc(),
                          MarketplaceListing.created_at.desc()).all())
    return render_template("admin/marketplace.html", listings=listings)


@bp.route("/marketplace/<int:listing_id>/toggle", methods=["POST"])
@admin_required
def marketplace_toggle(listing_id):
    ln = db.session.get(MarketplaceListing, listing_id) or abort(404)
    ln.active = not ln.active
    db.session.commit()
    flash("Listing hidden." if not ln.active else "Listing restored.", "success")
    return redirect(url_for("admin.marketplace"))


@bp.route("/marketplace/<int:listing_id>/delete", methods=["POST"])
@admin_required
def marketplace_delete(listing_id):
    ln = db.session.get(MarketplaceListing, listing_id) or abort(404)
    db.session.delete(ln)
    db.session.commit()
    flash("Listing deleted.", "success")
    return redirect(url_for("admin.marketplace"))


@bp.route("/marketplace/bulk-delete", methods=["POST"])
@admin_required
def marketplace_bulk_delete():
    ids = _form_ids()
    if not ids:
        flash("Select at least one listing to delete.", "error")
        return redirect(url_for("admin.marketplace"))
    deleted = 0
    for lid in ids:
        ln = db.session.get(MarketplaceListing, lid)
        if ln is None:
            continue
        db.session.delete(ln)
        deleted += 1
    db.session.commit()
    flash(
        f"Deleted {deleted} listing{'s' if deleted != 1 else ''}.",
        "success" if deleted else "info",
    )
    return redirect(url_for("admin.marketplace"))


# ============================ CONTENT TIPS ===================================

@bp.route("/videos")
@admin_required
def videos():
    items = Video.query.order_by(Video.sort_order, Video.created_at.desc()).all()
    return render_template("admin/videos.html", videos=items)


@bp.route("/videos/new", methods=["GET", "POST"])
@bp.route("/videos/<int:video_id>/edit", methods=["GET", "POST"])
@admin_required
def video_form(video_id=None):
    video = db.session.get(Video, video_id) if video_id else None
    if video_id and video is None:
        abort(404)

    if request.method == "POST":
        title = (request.form.get("title") or "").strip()[:160]
        description = (request.form.get("description") or "").strip() or None
        body = (request.form.get("body") or "").strip() or None
        published = bool(request.form.get("published"))
        free_access = bool(request.form.get("free_access"))
        healing_access = bool(request.form.get("healing_access"))
        remove_video = bool(request.form.get("remove_video"))
        try:
            sort_order = int(request.form.get("sort_order") or 0)
        except ValueError:
            sort_order = 0

        errors = []
        if not title:
            errors.append("A title is required.")

        new_video = None
        upload = request.files.get("video_file")
        if upload and upload.filename:
            try:
                new_video = process_video(
                    upload, current_app.config["VIDEO_STORAGE_DIR"],
                    current_app.config["MAX_VIDEO_MB"] * 1024 * 1024)
            except VideoError as exc:
                errors.append(str(exc))

        # A tip is the writing; the video is a bonus. One of them has to exist.
        keeps_video = bool(
            new_video
            or (video is not None and video.has_video() and not remove_video)
        )
        if not body and not keeps_video:
            errors.append("Write the tip, or attach a video to go with it.")

        new_thumb = None
        thumb = request.files.get("thumb_file")
        if thumb and thumb.filename:
            try:
                new_thumb = process_thumb(thumb)
            except VideoError as exc:
                errors.append(str(exc))

        if errors:
            for e in errors:
                flash(e, "error")
        else:
            old_disk = None
            was_live = bool(video and video.published)
            try:
                if video is None:
                    video = Video()
                    db.session.add(video)
                video.title = title
                video.description = description
                video.body = body
                video.published = published
                video.free_access = free_access
                video.healing_access = healing_access
                video.sort_order = sort_order
                if new_video:
                    disk_name, mime, fname, size = new_video
                    old_disk = video.disk_name  # replaced file, delete after commit
                    video.disk_name, video.mime, video.filename = disk_name, mime, fname
                    video.size = size
                    video.data = None
                elif remove_video and video.has_video():
                    old_disk = video.disk_name
                    video.disk_name = video.mime = video.filename = None
                    video.size = 0
                    video.data = None
                if new_thumb:
                    video.thumb_data, video.thumb_mime = new_thumb
                if request.form.get("remove_thumb"):
                    video.thumb_data = None
                    video.thumb_mime = None
                db.session.flush()
                told = 0
                if published and not was_live:
                    from ..services.social_graph import notify_everyone
                    told = notify_everyone(
                        kind="content_hub",
                        body=f"New on Content Hub: “{title[:80]}”",
                        url=url_for("main.watch", video_id=video.id),
                        actor_id=current_user.id,
                        exclude_id=current_user.id,
                    )
                db.session.commit()
            except Exception:
                db.session.rollback()
                log.exception("content tip save failed")
                # a brand-new file we just wrote is now orphaned; clean it up
                if new_video:
                    delete_stored(current_app.config["VIDEO_STORAGE_DIR"], new_video[0])
                flash("We couldn't save that tip just now \u2014 please try again.",
                      "error")
            else:
                if old_disk:
                    delete_stored(current_app.config["VIDEO_STORAGE_DIR"], old_disk)
                if told:
                    flash(f"Tip saved, and {told} "
                          f"{'member was' if told == 1 else 'members were'} "
                          "notified. It's on the home page for a day too.",
                          "success")
                else:
                    flash("Tip saved.", "success")
                return redirect(url_for("admin.videos"))

    return render_template("admin/video_form.html", video=video,
                           max_mb=current_app.config["MAX_VIDEO_MB"])


@bp.route("/videos/<int:video_id>/delete", methods=["POST"])
@admin_required
def video_delete(video_id):
    video = db.session.get(Video, video_id) or abort(404)
    disk_name = video.disk_name
    db.session.delete(video)
    db.session.commit()
    delete_stored(current_app.config["VIDEO_STORAGE_DIR"], disk_name)
    flash("Tip deleted.", "success")
    return redirect(url_for("admin.videos"))


@bp.route("/videos/bulk-delete", methods=["POST"])
@admin_required
def videos_bulk_delete():
    ids = _form_ids()
    if not ids:
        flash("Select at least one tip to delete.", "error")
        return redirect(url_for("admin.videos"))
    deleted = 0
    storage = current_app.config["VIDEO_STORAGE_DIR"]
    for vid in ids:
        video = db.session.get(Video, vid)
        if video is None:
            continue
        disk_name = video.disk_name
        db.session.delete(video)
        db.session.flush()
        delete_stored(storage, disk_name)
        deleted += 1
    db.session.commit()
    flash(
        f"Deleted {deleted} tip{'s' if deleted != 1 else ''}.",
        "success" if deleted else "info",
    )
    return redirect(url_for("admin.videos"))


# =============================== MEMBERS =====================================

def _csv_response(filename: str, header: list[str], rows: list[list]) -> Response:
    """UTF-8 CSV (with BOM) for Excel + Brevo / Mailchimp / Klaviyo imports."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows(rows)
    # BOM helps Excel open UTF-8 correctly; ESPs ignore it fine.
    payload = "\ufeff" + buf.getvalue()
    return Response(
        payload,
        mimetype="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@bp.route("/members")
@admin_required
def members():
    q = (request.args.get("q") or "").strip()
    membership = (request.args.get("membership") or "").strip().lower()
    if membership not in MEMBERSHIPS:
        membership = ""
    query = User.query.filter(User.deleted_at.is_(None))
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(User.email.ilike(like),
                                    User.display_name.ilike(like)))
    if membership:
        query = query.filter(User.membership == membership)
    people = query.order_by(User.created_at.desc()).limit(200).all()
    counts = dict(db.session.query(User.membership, func.count(User.id))
                  .filter(User.deleted_at.is_(None)).group_by(User.membership).all())
    return render_template("admin/members.html", people=people, counts=counts,
                           memberships=MEMBERSHIPS,
                           membership_labels=MEMBERSHIP_LABELS, q=q,
                           membership_filter=membership,
                           demo_count=demo_accounts.count(),
                           demo_min_password=demo_accounts.MIN_PASSWORD)


@bp.route("/members/demo", methods=["POST"])
@admin_required
def members_add_demo():
    """Create a stand-in account from just a username and a password."""
    username = (request.form.get("username") or "").strip()
    password = (request.form.get("password") or "").strip()
    display_name = (request.form.get("display_name") or "").strip()
    membership = (request.form.get("membership") or "none").strip().lower()

    err = demo_accounts.validation_error(username, password, membership)
    if err:
        flash(err, "error")
        return redirect(url_for("admin.members"))
    user = demo_accounts.create(username, password,
                                display_name=display_name,
                                membership=membership)
    log.info("studio: demo account @%s created by user %s",
             user.username, current_user.id)
    flash(f"Added {user.public_name()}. They sign in with @{user.username} "
          "and the password you just set.", "success")
    return redirect(url_for("admin.members"))


@bp.route("/members/refresh-cancellations", methods=["POST"])
@admin_required
def members_refresh_cancellations():
    """Re-read scheduled cancels from Stripe and flag them on the member list."""
    from ..services import stripe_pay as pay

    if not pay.configured():
        flash("Stripe isn't configured, so there's nothing to read.", "error")
        return redirect(url_for("admin.members"))
    result = pay.sweep_cancel_flags()
    if not result.get("ok"):
        flash("Couldn't reach Stripe just now — nothing was changed. Try again "
              "in a minute.", "error")
    elif result["changed"]:
        flash(f"Checked {result['checked']} subscription(s). "
              f"{result['canceling']} are ending; updated {result['changed']} "
              "member(s).", "success")
    else:
        flash(f"Checked {result['checked']} subscription(s). "
              f"{result['canceling']} are ending — the list was already right.",
              "info")
    return redirect(url_for("admin.members"))


@bp.route("/members/audit")
@admin_required
def members_audit():
    """Why each paid member holds their tier — and which ones Stripe can't explain."""
    from ..services import membership_audit as audit_svc
    from ..services import stripe_pay as pay

    tier = (request.args.get("membership") or "").strip().lower()
    if tier not in MEMBERSHIPS:
        tier = ""
    return render_template(
        "admin/members_audit.html",
        audit=audit_svc.audit(tier),
        tier=tier,
        membership_labels=MEMBERSHIP_LABELS,
        source_labels=audit_svc.SOURCE_LABELS,
        source_help=audit_svc.SOURCE_HELP,
        stripe_configured=pay.configured(),
    )


@bp.route("/members/audit/resync", methods=["POST"])
@admin_required
def members_audit_resync():
    from ..services import membership_audit as audit_svc

    tier = (request.form.get("membership") or "").strip().lower()
    if tier not in MEMBERSHIPS:
        tier = ""
    result = audit_svc.resync_from_stripe(tier)
    changed = result["changed"]
    stuck = result.get("unreachable") or 0
    if changed:
        names = ", ".join(f"{c['name']} ({c['from']} → {c['to']})"
                          for c in changed[:6])
        more = f" and {len(changed) - 6} more" if len(changed) > 6 else ""
        flash(f"Checked {result['checked']} member(s). Corrected {len(changed)}: "
              f"{names}{more}.", "success")
    elif result["checked"]:
        flash(f"Checked {result['checked']} member(s) against Stripe — every "
              "tier already matched.", "info")
    if stuck:
        flash(f"Stripe didn't answer for {stuck} member(s), so they were left "
              "alone rather than guessed at. Try again in a minute; if it keeps "
              "happening the Stripe key or connection needs looking at.", "error")
    return redirect(url_for("admin.members_audit", membership=tier or None))


@bp.route("/members/export.csv")
@admin_required
def members_export_csv():
    """Email list for marketing tools (Email, First Name, Last Name, …)."""
    q = (request.args.get("q") or "").strip()
    membership = (request.args.get("membership") or "").strip().lower()
    query = User.query.filter(User.deleted_at.is_(None),
                              User.is_demo.is_(False))
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(User.email.ilike(like),
                                    User.display_name.ilike(like)))
    if membership in MEMBERSHIPS:
        query = query.filter(User.membership == membership)
    people = query.order_by(User.created_at.desc()).all()

    rows = []
    for m in people:
        email = (m.email or "").strip().lower()
        if not email or email.endswith("@invalid.local") or "@" not in email:
            continue
        name = (m.display_name or "").strip()
        parts = name.split(None, 1) if name else []
        first = parts[0] if parts else ""
        last = parts[1] if len(parts) > 1 else ""
        tier = MEMBERSHIP_LABELS.get(m.membership, m.membership or "Free")
        joined = m.created_at.strftime("%Y-%m-%d") if m.created_at else ""
        rows.append([email, first, last, name, tier, joined])

    stamp = utcnow().strftime("%Y%m%d")
    return _csv_response(
        f"bloom-anyway-members-{stamp}.csv",
        ["Email", "First Name", "Last Name", "Full Name", "Membership", "Joined"],
        rows,
    )


@bp.route("/members/<int:user_id>/membership", methods=["POST"])
@admin_required
def set_membership(user_id):
    member = db.session.get(User, user_id) or abort(404)
    if member.is_admin:
        flash("The owner account always keeps Full Bloom access.", "info")
        return redirect(request.form.get("next") or url_for("admin.members"))
    tier = request.form.get("membership")
    if tier in MEMBERSHIPS:
        from ..services.memberships import set_manual_tier
        info = set_manual_tier(member, tier)
        db.session.commit()
        msg = f"{member.public_name()} \u2192 {member.membership_label()}."
        if info["revoked"]:
            msg += " Their paid membership was cancelled in Stripe."
        flash(msg, "success")
        if info["errors"]:
            log.warning("set_membership: billing cleanup issues for user %s: %s",
                        member.id, info["errors"])
            flash(
                "Their tier was saved, but Stripe didn't confirm the "
                "cancellation \u2014 check their subscription in the Stripe "
                "dashboard so they aren't charged again.",
                "error",
            )
    next_url = request.form.get("next") or url_for(
        "admin.members",
        q=request.form.get("q") or None,
        membership=request.form.get("membership_filter") or None,
    )
    return redirect(next_url)


@bp.route("/members/<int:user_id>/remove", methods=["POST"])
@admin_required
def remove_member(user_id):
    """Hard-delete a member/user account from the Members page."""
    from ..services.privacy import close_account

    member = db.session.get(User, user_id) or abort(404)
    back = url_for(
        "admin.members",
        q=request.form.get("q") or None,
        membership=request.form.get("membership_filter") or None,
    )
    if member.deleted_at is not None:
        flash("That account is already removed.", "info")
        return redirect(back)
    if member.is_admin:
        flash("Studio owners can't be removed from Members.", "error")
        return redirect(back)
    name = member.public_name()
    close_account(member)
    flash(f"{name} was removed from Bloom Anyway.", "success")
    return redirect(back)


@bp.route("/members/bulk-remove", methods=["POST"])
@admin_required
def members_bulk_remove():
    """Hard-delete multiple member accounts from the Members page."""
    from ..services.privacy import close_account

    back = url_for(
        "admin.members",
        q=request.form.get("q") or None,
        membership=request.form.get("membership_filter") or None,
    )
    ids = _form_ids()
    if not ids:
        flash("Select at least one member to remove.", "error")
        return redirect(back)
    removed = 0
    skipped_owners = 0
    for uid in ids:
        member = db.session.get(User, uid)
        if member is None or member.deleted_at is not None:
            continue
        if member.is_admin:
            skipped_owners += 1
            continue
        close_account(member)
        removed += 1
    if removed:
        flash(
            f"Removed {removed} member{'s' if removed != 1 else ''} from Bloom Anyway.",
            "success",
        )
    if skipped_owners:
        flash(
            f"Skipped {skipped_owners} Studio owner"
            f"{'s' if skipped_owners != 1 else ''} (can't remove owners here).",
            "info",
        )
    if not removed and not skipped_owners:
        flash("No members were removed.", "info")
    return redirect(back)

# =============================== OWNERS ======================================

@bp.route("/owners")
@admin_required
def owners():
    from ..services import owners as owners_svc
    return render_template(
        "admin/owners.html",
        owners=owners_svc.current_owners(),
        invites=owners_svc.invite_entries(),
        me_email=(current_user.email or "").strip().lower(),
        studio_readonly=bool(getattr(current_user, "admin_readonly", False)),
    )


@bp.route("/owners/invite", methods=["POST"])
@admin_required
def owners_invite():
    from ..services import owners as owners_svc
    role = (request.form.get("role") or "full").strip().lower()
    readonly = role in ("view", "readonly", "view-only", "view_only")
    ok, msg = owners_svc.invite(
        request.form.get("email") or "",
        actor=current_user,
        readonly=readonly,
    )
    flash(msg, "success" if ok else "error")
    return redirect(url_for("admin.owners"))


@bp.route("/owners/remove", methods=["POST"])
@admin_required
def owners_remove():
    from ..services import owners as owners_svc
    ok, msg = owners_svc.remove(request.form.get("email") or "", actor=current_user)
    flash(msg, "success" if ok else "error")
    return redirect(url_for("admin.owners"))


@bp.route("/owners/bulk-remove", methods=["POST"])
@admin_required
def owners_bulk_remove():
    from ..services import owners as owners_svc

    emails = [
        (e or "").strip().lower()
        for e in request.form.getlist("emails")
        if (e or "").strip()
    ]
    # Dedupe while preserving order
    seen: set[str] = set()
    unique = []
    for e in emails:
        if e in seen:
            continue
        seen.add(e)
        unique.append(e)
    if not unique:
        flash("Select at least one owner to remove.", "error")
        return redirect(url_for("admin.owners"))
    removed = 0
    errors = []
    for email in unique:
        ok, msg = owners_svc.remove(email, actor=current_user)
        if ok:
            removed += 1
        else:
            errors.append(msg)
    if removed:
        flash(
            f"Removed owner access for {removed} "
            f"account{'s' if removed != 1 else ''}.",
            "success",
        )
    for msg in errors[:3]:
        flash(msg, "error")
    return redirect(url_for("admin.owners"))


# ============================ MEMBERSHIP PLANS ===============================

_PLAN_DEFAULTS = {
    "healing": {"name": "Healing membership",
                "tagline": "Healing community, support, and one Showcase listing.",
                "sort_order": 1},
    "creator": {"name": "Creator membership",
                "tagline": "Building community, tips, spotlight, and Showcase.",
                "sort_order": 2},
    "full_bloom": {"name": "Full Bloom membership",
                   "tagline": "Everything in Healing and Creator.",
                   "sort_order": 3},
}


def _get_plans():
    """Return membership plans, creating any that are missing."""
    plans = {p.tier: p for p in MembershipPlan.query.all()}
    changed = False
    for tier, d in _PLAN_DEFAULTS.items():
        if tier not in plans:
            plan = MembershipPlan(tier=tier, name=d["name"], tagline=d["tagline"],
                                  sort_order=d["sort_order"])
            db.session.add(plan)
            plans[tier] = plan
            changed = True
        else:
            # Keep names/taglines fresh when still on old defaults
            pass
    if changed:
        db.session.commit()
    return [plans["healing"], plans["creator"], plans["full_bloom"]]


@bp.route("/memberships", methods=["GET", "POST"])
@admin_required
def membership_plans():
    plans = _get_plans()
    if request.method == "POST":
        for plan in plans:
            p = plan.tier
            plan.name = (request.form.get(f"{p}_name") or plan.name).strip()
            plan.tagline = (request.form.get(f"{p}_tagline") or "").strip() or None
            plan.currency = (request.form.get(f"{p}_currency") or "USD").strip().upper()[:3]
            plan.period = "month"
            plan.stripe_price_id = (request.form.get(f"{p}_stripe") or "").strip() or None
            plan.stripe_price_id_annual = (
                request.form.get(f"{p}_stripe_annual") or "").strip() or None
            plan.stripe_product_id = (
                request.form.get(f"{p}_stripe_product") or "").strip() or None
            plan.stripe_product_id_annual = (
                request.form.get(f"{p}_stripe_product_annual") or "").strip() or None
            plan.active = bool(request.form.get(f"{p}_active"))
            raw_t = (request.form.get(f"{p}_trial_days") or "").strip()
            if raw_t.isdigit():
                plan.trial_days = max(0, min(730, int(raw_t)))
            elif raw_t == "":
                plan.trial_days = 0
            raw = (request.form.get(f"{p}_price") or "").strip().replace(",", "")
            try:
                plan.price_cents = round(float(raw) * 100) if raw else None
            except ValueError:
                plan.price_cents = plan.price_cents
            raw_y = (request.form.get(f"{p}_annual_price") or "").strip().replace(",", "")
            try:
                plan.annual_price_cents = round(float(raw_y) * 100) if raw_y else None
            except ValueError:
                plan.annual_price_cents = plan.annual_price_cents
            raw_f = (request.form.get(f"{p}_founder_price") or "").strip().replace(",", "")
            try:
                plan.founder_price_cents = round(float(raw_f) * 100) if raw_f else None
            except ValueError:
                plan.founder_price_cents = plan.founder_price_cents
            raw_fy = (request.form.get(f"{p}_founder_annual_price") or "").strip().replace(",", "")
            try:
                plan.founder_annual_price_cents = (
                    round(float(raw_fy) * 100) if raw_fy else None
                )
            except ValueError:
                plan.founder_annual_price_cents = plan.founder_annual_price_cents
        # Reject the same Stripe price/product on two plans (also DB unique indexes).
        seen_prices: dict[str, str] = {}
        seen_products: dict[str, str] = {}
        dupes = []
        for plan in plans:
            month = (plan.stripe_price_id or "").strip() or None
            year = (plan.stripe_price_id_annual or "").strip() or None
            plan.stripe_price_id = month
            plan.stripe_price_id_annual = year
            pm = (plan.stripe_product_id or "").strip() or None
            py = (plan.stripe_product_id_annual or "").strip() or None
            plan.stripe_product_id = pm
            plan.stripe_product_id_annual = py
            if month and year and month == year:
                dupes.append(f"{month} (monthly+annual price on {plan.tier})")
            if pm and py and pm == py:
                dupes.append(f"{pm} (monthly+annual product on {plan.tier})")
            for key in (month, year):
                if not key:
                    continue
                other = seen_prices.get(key)
                if other and other != plan.tier:
                    dupes.append(f"{key} ({other} + {plan.tier})")
                else:
                    seen_prices[key] = plan.tier
            for key in (pm, py):
                if not key:
                    continue
                other = seen_products.get(key)
                if other and other != plan.tier:
                    dupes.append(f"{key} ({other} + {plan.tier})")
                else:
                    seen_products[key] = plan.tier
        if dupes:
            db.session.rollback()
            flash(
                "Each Stripe price/product can only belong to one plan. Duplicates: "
                + "; ".join(dupes),
                "error",
            )
            return redirect(url_for("admin.membership_plans"))
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            log.exception("membership plans save failed")
            flash(
                "Could not save plans — each Stripe price/product id must be unique.",
                "error",
            )
            return redirect(url_for("admin.membership_plans"))
        flash("Membership plans saved.", "success")
        return redirect(url_for("admin.membership_plans"))
    return render_template("admin/membership_plans.html", plans=plans)


# ================================ BADGES =====================================

@bp.route("/badges", methods=["GET", "POST"])
@admin_required
def badges():
    if request.method == "POST":
        if request.form.get("reset"):
            badges_service.reset_thresholds()
            flash("Milestones reset to their defaults.", "success")
            return redirect(url_for("admin.badges"))

        mapping, errors = {}, []
        for cat_key, cat in badges_service.CATEGORIES.items():
            values = []
            for level in range(1, len(cat["tiers"]) + 1):
                raw = (request.form.get(f"t_{cat_key}_{level}") or "").strip()
                try:
                    n = int(raw)
                except ValueError:
                    errors.append(f"{cat['name']}: milestone {level} must be a whole number.")
                    break
                if n < 1:
                    errors.append(f"{cat['name']}: milestones must be at least 1.")
                    break
                if values and n <= values[-1]:
                    errors.append(f"{cat['name']}: each milestone must be higher than the one before.")
                    break
                values.append(n)
            if len(values) == len(cat["tiers"]):
                mapping[cat_key] = values

        if errors:
            for msg in errors:
                flash(msg, "error")
            return redirect(url_for("admin.badges"))

        badges_service.set_thresholds(mapping)
        flash("Milestones saved.", "success")
        return redirect(url_for("admin.badges"))

    return render_template("admin/badges.html",
                           overview=badges_service.all_badges_overview(),
                           owner_badge=badges_service.OWNER_BADGE)


# ============================ FEEDBACK INBOX =================================

@bp.route("/inbox")
@admin_required
def inbox():
    """Unified inbox: contact messages, feedback, complaints, error and content reports."""
    filt = (request.args.get("filter") or "all").strip().lower()
    allowed = {"all", "messages", "feedback", "complaint", "error",
               "reports", "open", "resolved"}
    if filt not in allowed:
        filt = "all"

    message_rows = []
    if filt in ("all", "messages"):
        message_rows = (ContactMessage.query
                        .order_by(ContactMessage.created_at.desc())
                        .limit(100).all())

    feedback_q = (SiteFeedback.query.options(joinedload(SiteFeedback.author))
                  .order_by(SiteFeedback.created_at.desc()))
    reports_q = (ContentReport.query.options(joinedload(ContentReport.reporter))
                 .order_by(ContentReport.created_at.desc()))

    show_feedback = filt in ("all", "feedback", "complaint", "error")
    show_reports = filt in ("all", "reports", "open", "resolved")

    feedback_rows = []
    if show_feedback:
        q = feedback_q
        if filt in ("feedback", "complaint", "error"):
            q = q.filter_by(kind=filt)
        feedback_rows = q.limit(100).all()

    report_rows = []
    if show_reports:
        q = reports_q
        if filt == "open":
            q = q.filter_by(status="open")
        elif filt == "resolved":
            q = q.filter(ContentReport.status.in_(("resolved", "dismissed")))
        report_rows = q.limit(100).all()

    # Attach target snippets for studio display
    enriched = []
    for r in report_rows:
        target = None
        snippet = ""
        if r.target_type == "post":
            target = db.session.get(ForumPost, r.target_id)
            if target:
                snippet = f"{target.title}: {(target.body or '')[:160]}"
        elif r.target_type == "comment":
            target = db.session.get(ForumComment, r.target_id)
            if target:
                snippet = (target.body or "")[:200]
        elif r.target_type == "user":
            target = db.session.get(User, r.target_id)
            if target:
                name = (target.public_name() or "").strip()
                handle = (target.username or "").strip()
                bits = [name] if name else []
                if handle and name.lstrip("@").lower() != handle.lower():
                    bits.append(f"@{handle}")
                bits.append(f"warnings {target.forum_warnings or 0}")
                snippet = " · ".join(bits)
        enriched.append({"report": r, "target": target, "snippet": snippet})

    counts = {
        "messages": ContactMessage.query.filter_by(status="new").count(),
        "feedback": SiteFeedback.query.filter_by(kind="feedback").count(),
        "complaint": SiteFeedback.query.filter_by(kind="complaint").count(),
        "error": SiteFeedback.query.filter_by(kind="error").count(),
        "reports_open": ContentReport.query.filter_by(status="open").count(),
        "reports_resolved": ContentReport.query.filter(
            ContentReport.status.in_(("resolved", "dismissed"))).count(),
    }
    return render_template(
        "admin/inbox.html", filter=filt, feedback_rows=feedback_rows,
        report_rows=enriched, message_rows=message_rows, counts=counts,
        reply_to={row.id: feedback_reply_to(row) for row in feedback_rows},
    )


def _reply_flow(*, who, email, body, when, subject, back_filter, mark_reviewed):
    """Compose-and-send for a Studio reply, shared by messages and complaints.

    Same sender picker and the same Brevo template per address either way — a
    complaint is a person waiting on an answer just as much as a contact form
    message is, and there is no reason for two of these.
    """
    from types import SimpleNamespace

    quoted = "\n".join(f"> {line}" for line in (body or "").splitlines())
    first_name = (who or "").strip().split(" ")[0] or "there"
    draft = {
        "sender": mailer.DEFAULT_SENDER_KEY,
        "subject": subject,
        "preview": "",
        "header": "Bloom Anyway",
        "title": f"Hi {first_name},",
        "body": f"\n\n\nYou wrote:\n{quoted}",
    }

    if request.method == "POST":
        draft = {k: (request.form.get(k) or "").strip() for k in draft}
        missing = [label for key, label in (("subject", "a subject"),
                                            ("title", "a title"),
                                            ("body", "something to say"))
                   if not draft[key]]
        sender = mailer.sender_from(draft["sender"])
        if missing:
            flash("Your reply needs " + " and ".join(missing) + ".", "error")
        elif not sender:
            flash("Pick which address the reply should come from.", "error")
        elif mailer.send_customer_support_email(
            email,
            subject=draft["subject"],
            preview=draft["preview"] or draft["subject"],
            header=draft["header"] or "Bloom Anyway",
            title=draft["title"],
            body=draft["body"],
            sender=sender,
            sender_key=draft["sender"],
        ):
            mark_reviewed()
            db.session.commit()
            template_id = mailer.reply_template_for(draft["sender"])
            flash(f"Reply sent to {email} from {sender}"
                  + (f" on template #{template_id}." if template_id else "."),
                  "success")
            return redirect(url_for("admin.inbox", filter=back_filter))
        else:
            hint = last_send_error() or "Check the Render logs for Brevo."
            flash(f"The reply didn't send. {hint}", "error")

    return render_template(
        "admin/inbox_reply.html",
        msg=SimpleNamespace(name=who, email=email, body=body, created_at=when),
        draft=draft, senders=mailer.sender_choices())


@bp.route("/inbox/messages/<int:message_id>/reply", methods=["GET", "POST"])
@admin_required
def inbox_message_reply(message_id):
    """Write and send a reply to a contact message from inside Studio."""
    msg = db.session.get(ContactMessage, message_id) or abort(404)

    def _reviewed():
        msg.status = "reviewed"

    return _reply_flow(
        who=msg.name, email=msg.email, body=msg.body, when=msg.created_at,
        subject="Re: your message to Bloom Anyway",
        back_filter="messages", mark_reviewed=_reviewed)


#: What the reply is answering, per kind of note left through the site widget.
_FEEDBACK_REPLY_SUBJECTS = {
    "complaint": "Re: your complaint to Bloom Anyway",
    "error": "Re: the problem you reported",
    "feedback": "Re: your feedback for Bloom Anyway",
}


def feedback_reply_to(row) -> str:
    """Where a reply to this note would go, or empty if there's nowhere."""
    direct = (getattr(row, "contact_email", None) or "").strip()
    if direct:
        return direct
    author = getattr(row, "author", None)
    if author is not None and not author.deleted_at:
        return (author.email or "").strip()
    return ""


@bp.route("/inbox/feedback/<int:item_id>/reply", methods=["GET", "POST"])
@admin_required
def inbox_feedback_reply(item_id):
    """Reply to a complaint, an error report, or a note left on the site."""
    row = db.session.get(SiteFeedback, item_id) or abort(404)
    email = feedback_reply_to(row)
    if not email:
        flash("That one was left without an address, so there's nowhere to "
              "reply to.", "info")
        return redirect(url_for("admin.inbox", filter=row.kind))

    author = row.author
    who = author.public_name() if author else email.split("@")[0]

    def _reviewed():
        from ..services.feedback import mark_reviewed
        mark_reviewed(row)

    return _reply_flow(
        who=who, email=email, body=row.body, when=row.created_at,
        subject=_FEEDBACK_REPLY_SUBJECTS.get(row.kind,
                                             "Re: your note to Bloom Anyway"),
        back_filter=row.kind, mark_reviewed=_reviewed)


@bp.route("/inbox/messages/<int:message_id>/reviewed", methods=["POST"])
@admin_required
def inbox_message_reviewed(message_id):
    row = db.session.get(ContactMessage, message_id) or abort(404)
    row.status = "reviewed"
    db.session.commit()
    flash(f"Marked {row.name}'s message reviewed.", "success")
    return redirect(url_for("admin.inbox",
                            filter=request.form.get("filter") or "messages"))


@bp.route("/inbox/feedback/<int:item_id>/reviewed", methods=["POST"])
@admin_required
def inbox_feedback_reviewed(item_id):
    from ..services.feedback import mark_reviewed
    row = db.session.get(SiteFeedback, item_id) or abort(404)
    mark_reviewed(row)
    flash("Marked reviewed.", "success")
    return redirect(url_for("admin.inbox", filter=request.form.get("filter") or "all"))


@bp.route("/inbox/reports/<int:report_id>/hide", methods=["POST"])
@admin_required
def inbox_report_hide(report_id):
    from ..services.content_reports import hide_target
    report = db.session.get(ContentReport, report_id) or abort(404)
    hide_target(report, owner_note=request.form.get("owner_note") or "")
    flash("Content hidden and reporter case resolved.", "success")
    return redirect(url_for("admin.inbox", filter=request.form.get("filter") or "reports"))


@bp.route("/inbox/reports/<int:report_id>/dismiss", methods=["POST"])
@admin_required
def inbox_report_dismiss(report_id):
    from ..services.content_reports import dismiss_report
    report = db.session.get(ContentReport, report_id) or abort(404)
    dismiss_report(report, owner_note=request.form.get("owner_note") or "")
    flash("Report dismissed — no take-down.", "success")
    return redirect(url_for("admin.inbox", filter=request.form.get("filter") or "reports"))


# =============================== COMMUNITY ===================================

def _enrich_flagged_members(members: list) -> list:
    """Attach each member's reports — theirs, their posts', their comments'.

    Batched across the whole list: doing it per member meant three queries a
    head, and this page shows everyone who has ever been flagged.
    """
    ids = [m.id for m in members]
    if not ids:
        return []

    owner_of_post = dict(
        db.session.query(ForumPost.id, ForumPost.user_id)
        .filter(ForumPost.user_id.in_(ids)).all()
    )
    owner_of_comment = dict(
        db.session.query(ForumComment.id, ForumComment.user_id)
        .filter(ForumComment.user_id.in_(ids)).all()
    )
    clauses = [db.and_(ContentReport.target_type == "user",
                       ContentReport.target_id.in_(ids))]
    if owner_of_post:
        clauses.append(db.and_(ContentReport.target_type == "post",
                               ContentReport.target_id.in_(owner_of_post)))
    if owner_of_comment:
        clauses.append(db.and_(ContentReport.target_type == "comment",
                               ContentReport.target_id.in_(owner_of_comment)))
    reports = (ContentReport.query
               .options(joinedload(ContentReport.reporter))
               .filter(db.or_(*clauses))
               .order_by(ContentReport.created_at.desc())
               .all())

    by_member: dict[int, list] = {mid: [] for mid in ids}
    for r in reports:
        if r.target_type == "user":
            owner = r.target_id
        elif r.target_type == "post":
            owner = owner_of_post.get(r.target_id)
        else:
            owner = owner_of_comment.get(r.target_id)
        if owner in by_member and len(by_member[owner]) < 30:
            by_member[owner].append(r)

    # the reported comment itself, so owners can read it and take it down
    flagged_comment_ids = {r.target_id for r in reports
                           if r.target_type == "comment"}
    comments = {}
    if flagged_comment_ids:
        comments = {
            c.id: c for c in ForumComment.query
            .filter(ForumComment.id.in_(flagged_comment_ids)).all()
        }

    return [{
        "member": m,
        "reports": by_member[m.id],
        "open_reports": sum(1 for r in by_member[m.id] if r.status == "open"),
        "comments": comments,
    } for m in members]


@bp.route("/community")
@admin_required
def community():
    posts = (ForumPost.query.options(joinedload(ForumPost.category),
                                     joinedload(ForumPost.author))
             .order_by(ForumPost.created_at.desc()).limit(100).all())
    flagged_q = (
        User.query.filter(
            User.deleted_at.is_(None),
            User.is_admin.is_(False),
            db.or_(User.forum_warnings > 0, User.forum_banned.is_(True)),
        )
        .order_by(User.forum_banned.desc(), User.forum_warnings.desc())
    )
    flagged_users = flagged_q.all()

    # Include anyone with an open peer/user report even if the counter was cleared.
    seen = {u.id for u in flagged_users}
    open_flag_ids = [
        tid for (tid,) in
        db.session.query(ContentReport.target_id)
        .filter_by(target_type="user", status="open")
        .distinct()
        .all()
    ]
    for uid in open_flag_ids:
        if uid in seen:
            continue
        extra = db.session.get(User, uid)
        if extra and extra.deleted_at is None and not extra.is_admin:
            flagged_users.append(extra)
            seen.add(uid)

    return render_template(
        "admin/community.html",
        posts=posts,
        flagged=_enrich_flagged_members(flagged_users),
        warning_limit=2,
    )


@bp.route("/community/post/<int:post_id>/delete", methods=["POST"])
@admin_required
def community_delete_post(post_id):
    from ..services import forum_moderation
    post = db.session.get(ForumPost, post_id) or abort(404)
    forum_moderation.delete_post(post)
    flash("Post removed.", "success")
    return redirect(url_for("admin.community"))


@bp.route("/community/posts/bulk-delete", methods=["POST"])
@admin_required
def community_bulk_delete_posts():
    from ..services import forum_moderation

    ids = _form_ids()
    if not ids:
        flash("Select at least one post to remove.", "error")
        return redirect(url_for("admin.community"))
    removed = 0
    for pid in ids:
        post = db.session.get(ForumPost, pid)
        if post is None:
            continue
        forum_moderation.delete_post(post)
        removed += 1
    flash(
        f"Removed {removed} post{'s' if removed != 1 else ''}.",
        "success" if removed else "info",
    )
    return redirect(url_for("admin.community"))


@bp.route("/community/comment/<int:comment_id>/delete", methods=["POST"])
@admin_required
def community_delete_comment(comment_id):
    from ..services import forum_moderation
    comment = db.session.get(ForumComment, comment_id) or abort(404)
    forum_moderation.delete_comment(comment)
    flash("Comment removed.", "success")
    return redirect(url_for("admin.community"))


def _community_member_for_moderation(user_id: int):
    """Return a moderatable member, or None after flashing why not.

    Studio owner accounts and removed members used to abort(404), which dumped
    owners onto the public "different path" page after Community actions.
    """
    member = db.session.get(User, user_id)
    if member is None or member.deleted_at is not None:
        flash("That member isn't available anymore.", "error")
        return None
    if member.is_admin:
        flash("Studio owner accounts can't be moderated from Community.", "info")
        return None
    return member


@bp.route("/community/member/<int:user_id>/reset", methods=["POST"])
@admin_required
def community_reset_member(user_id):
    member = _community_member_for_moderation(user_id)
    if member is None:
        return redirect(url_for("admin.community"))
    member.forum_warnings = 0
    member.forum_banned = False
    # Close open peer flags so they leave the "needs a look" list.
    try:
        ContentReport.query.filter_by(
            target_type="user", target_id=member.id, status="open",
        ).update(
            {
                "status": "resolved",
                "resolved_at": utcnow(),
                "owner_note": "Cleared with fresh start",
            },
            synchronize_session=False,
        )
    except Exception:
        log.exception("Fresh-start report close failed for user %s", member.id)
        for report in ContentReport.query.filter_by(
            target_type="user", target_id=member.id, status="open",
        ).all():
            report.status = "resolved"
            report.resolved_at = utcnow()
            report.owner_note = "Cleared with fresh start"
    db.session.commit()
    flash("Fresh start given — flags cleared and posting restored.", "success")
    return redirect(url_for("admin.community"))


@bp.route("/community/member/<int:user_id>/warn", methods=["POST"])
@admin_required
def community_warn_member(user_id):
    """Send a real in-app + email warning (peer flags alone do not notify)."""
    from ..services.mailer import send_styled_email
    from ..services.social_graph import notify

    member = _community_member_for_moderation(user_id)
    if member is None:
        return redirect(url_for("admin.community"))
    if member.forum_warnings < 1:
        member.forum_warnings = 1

    body = (
        "A studio owner reviewed reports about your account and is sending "
        "this gentle warning. Please keep community spaces and support "
        "sessions kind and respectful.\n\n"
        "If this feels like a mistake, reply to this email and we'll talk it through."
    )
    notify(
        member.id,
        kind="moderation",
        body=("Studio sent you a community warning — please keep spaces kind. "
              "Reach out if this seems wrong."),
        url="/settings",
    )
    db.session.commit()
    try:
        send_styled_email(
            member.email,
            subject="A gentle reminder from Bloom Anyway",
            preview="Please keep community and support spaces kind.",
            header="A GENTLE REMINDER",
            title="Community warning",
            body=body,
            button_text="Open settings",
            button_url=url_for("main.settings", _external=True),
        )
    except Exception:
        log.exception("Community warning email failed for user %s", member.id)

    flash(f"Warning sent to {member.public_name()}.", "success")
    return redirect(url_for("admin.community"))


@bp.route("/community/member/<int:user_id>/pause", methods=["POST"])
@admin_required
def community_pause_member(user_id):
    """Pause community posting (forums). Support booking still follows membership."""
    from ..services.social_graph import notify

    member = _community_member_for_moderation(user_id)
    if member is None:
        return redirect(url_for("admin.community"))
    member.forum_banned = True
    notify(
        member.id,
        kind="moderation",
        body=("Community posting is paused on your account. "
              "You can still read; reach out if you'd like to talk it through."),
        url="/settings",
    )
    db.session.commit()
    flash(f"Posting paused for {member.public_name()}.", "success")
    return redirect(url_for("admin.community"))


@bp.route("/community/member/<int:user_id>/revoke-access", methods=["POST"])
@admin_required
def community_revoke_access(user_id):
    """Revoke membership (community + support groups) and pause forum posting."""
    from ..services.memberships import set_manual_tier
    from ..services.social_graph import notify

    member = _community_member_for_moderation(user_id)
    if member is None:
        return redirect(url_for("admin.community"))
    info = set_manual_tier(member, "none")
    if info["errors"]:
        log.warning("revoke-access: billing cleanup issues for user %s: %s",
                    member.id, info["errors"])
    member.forum_banned = True
    notify(
        member.id,
        kind="moderation",
        body=("Your membership access (community and support groups) was revoked "
              "by the studio. Reach out if you'd like to talk it through."),
        url="/membership",
    )
    db.session.commit()
    flash(
        f"Community and support access revoked for {member.public_name()}.",
        "success",
    )
    return redirect(url_for("admin.community"))


@bp.route("/community/member/<int:user_id>/remove", methods=["POST"])
@admin_required
def community_remove_member(user_id):
    """Hard-delete the account (same scrub as self-serve close account)."""
    from ..services.privacy import close_account

    member = _community_member_for_moderation(user_id)
    if member is None:
        return redirect(url_for("admin.community"))
    name = member.public_name()
    close_account(member)
    flash(f"{name} was removed from Bloom Anyway.", "success")
    return redirect(url_for("admin.community"))


@bp.route("/community/members/bulk-remove", methods=["POST"])
@admin_required
def community_bulk_remove_members():
    from ..services.privacy import close_account

    ids = _form_ids()
    if not ids:
        flash("Select at least one member to remove.", "error")
        return redirect(url_for("admin.community"))
    removed = 0
    for uid in ids:
        member = _community_member_for_moderation(uid)
        if member is None:
            continue
        close_account(member)
        removed += 1
    flash(
        f"Removed {removed} member{'s' if removed != 1 else ''} from Bloom Anyway.",
        "success" if removed else "info",
    )
    return redirect(url_for("admin.community"))


# ============================ REEL REVIEWS ===================================

@bp.route("/reel-reviews")
@admin_required
def reel_reviews():
    week = reel_svc.current_week_key()
    published = (ReelReview.query
                 .order_by(ReelReview.created_at.desc()).limit(40).all())
    return render_template("admin/reel_reviews.html", week_key=week,
                           applicants=reel_svc.week_applicants(week),
                           reviews=published,
                           progress=reel_svc.week_progress(week),
                           today=reel_svc.atlanta_today(),
                           today_review=reel_svc.review_on(),
                           max_mb=current_app.config["MAX_VIDEO_MB"])


@bp.route("/reel-reviews/pick", methods=["POST"])
@admin_required
def reel_reviews_pick():
    if reel_svc.day_is_done():
        flash("Today's review is already out — one a day. "
              "The next one can go up tomorrow.", "info")
        return redirect(url_for("admin.reel_reviews"))
    chosen = reel_svc.pick_random_applicant()
    if chosen is None:
        flash("Every entry this week has been reviewed already.", "info")
    else:
        flash(f"{chosen.author.public_name()} is up next — write their review below.",
              "success")
    return redirect(url_for("admin.reel_reviews"))


@bp.route("/reel-reviews/<int:app_id>/raw")
@admin_required
def reel_reviews_raw_download(app_id):
    """Download the applicant's raw unedited video (Studio only)."""
    application = db.session.get(ReelReviewApplication, app_id) or abort(404)
    name = application.filename or "raw-reel.mp4"
    mime = application.mime or "application/octet-stream"

    # Prefer on-disk file (streamed uploads). Fall back to legacy DB bytes.
    disk_name = os.path.basename(application.disk_name or "")
    if disk_name:
        directory = os.path.abspath(current_app.config["VIDEO_STORAGE_DIR"])
        path = os.path.join(directory, disk_name)
        if os.path.isfile(path):
            resp = send_from_directory(
                directory, disk_name,
                mimetype=mime,
                as_attachment=True,
                download_name=name,
                max_age=0,
            )
            resp.headers["Cache-Control"] = "private, no-store"
            return resp

    if application.data:
        resp = send_file(
            io.BytesIO(bytes(application.data)),
            mimetype=mime,
            as_attachment=True,
            download_name=name,
            max_age=0,
        )
        resp.headers["Cache-Control"] = "private, no-store"
        return resp

    flash("That entry has no raw video upload. Ask them to re-enter "
          "this week's draw with a fresh file.", "error")
    return redirect(url_for("admin.reel_reviews"))


@bp.route("/reel-reviews/<int:app_id>/publish", methods=["POST"])
@admin_required
def reel_reviews_publish(app_id):
    application = db.session.get(ReelReviewApplication, app_id) or abort(404)
    today = reel_svc.atlanta_today()
    already = reel_svc.review_on(today)
    # Editing the one that already went out today is fine; a second is not.
    if already and (application.review is None or already.id != application.review.id):
        flash("A review already went out today — one a day. "
              "The next one can go up tomorrow.", "error")
        return redirect(url_for("admin.reel_reviews"))
    title = (request.form.get("title") or "").strip()[:160]
    body = (request.form.get("body") or "").strip()
    if not title:
        flash("Give the review a title.", "error")
        return redirect(url_for("admin.reel_reviews"))
    review = application.review or ReelReview(application_id=application.id)
    if application.review is None:
        db.session.add(review)
    review.title = title
    review.body = body or ""
    review.published = True
    if review.review_date is None:
        review.review_date = today
    upload = request.files.get("review_video")
    if upload and upload.filename:
        try:
            disk_name, mime, fname, _size = process_video(
                upload, current_app.config["VIDEO_STORAGE_DIR"],
                current_app.config["MAX_VIDEO_MB"] * 1024 * 1024)
        except VideoError as exc:
            flash(str(exc), "error")
            return redirect(url_for("admin.reel_reviews"))
        if review.review_disk_name:
            delete_stored(current_app.config["VIDEO_STORAGE_DIR"],
                          review.review_disk_name)
        review.review_disk_name = disk_name
        review.review_mime = mime
        review.review_filename = fname
    application.selected = True
    db.session.flush()
    from ..services.social_graph import notify_everyone
    told = notify_everyone(
        kind="content_hub",
        body=f"New reel review on Content Hub: “{title[:80]}”",
        url=url_for("main.reel_review", review_id=review.id),
        actor_id=current_user.id,
        exclude_id=current_user.id,
    )
    db.session.commit()
    left = reel_svc.week_progress(application.week_key)["left"]
    tail = f" {left} left this week." if left else " That's all seven this week."
    flash("Reel review published to the Content Hub." + _told_suffix(told) + tail,
          "success")
    return redirect(url_for("admin.reel_reviews"))


@bp.route("/reel-reviews/review/<int:review_id>/unpublish", methods=["POST"])
@admin_required
def reel_reviews_unpublish(review_id):
    review = db.session.get(ReelReview, review_id) or abort(404)
    review.published = False
    db.session.commit()
    flash("Review hidden from the Content Hub. Put it back or delete it "
          "from the list below.", "success")
    return redirect(url_for("admin.reel_reviews"))


@bp.route("/reel-reviews/review/<int:review_id>/publish", methods=["POST"])
@admin_required
def reel_reviews_republish(review_id):
    """Put a hidden review back, as long as today's slot is free."""
    review = db.session.get(ReelReview, review_id) or abort(404)
    if review.published:
        return redirect(url_for("admin.reel_reviews"))
    today = reel_svc.atlanta_today()
    already = reel_svc.review_on(today)
    if already and already.id != review.id:
        flash("A review already went out today — one a day. "
              "This one can go back up tomorrow.", "error")
        return redirect(url_for("admin.reel_reviews"))
    review.published = True
    review.review_date = today
    db.session.commit()
    flash("Review is live on the Content Hub again.", "success")
    return redirect(url_for("admin.reel_reviews"))


@bp.route("/reel-reviews/review/<int:review_id>/delete", methods=["POST"])
@admin_required
def reel_reviews_delete(review_id):
    """Delete a review outright, freeing its entry to be reviewed again."""
    review = db.session.get(ReelReview, review_id) or abort(404)
    title = review.title
    if review.review_disk_name:
        delete_stored(current_app.config["VIDEO_STORAGE_DIR"],
                      review.review_disk_name)
    application = review.application
    if application is not None:
        # Back in the queue as if it had never been picked.
        application.selected = False
    db.session.delete(review)
    db.session.commit()
    flash(f"Deleted “{title[:60]}”. Their entry is back in the queue.",
          "success")
    return redirect(url_for("admin.reel_reviews"))


# --- support / coaching groups ----------------------------------------------

@bp.route("/support-groups")
@admin_required
def support_groups():
    from ..services import coaching_intake as intake_svc
    from ..services import support_groups as sg_svc
    # The sweep sends member emails one at a time. Forcing it here meant
    # opening this page waited on every one of them; the throttled sweep in
    # before_request (and /cron/support-groups) covers it either way.
    sg_svc.maybe_sweep_reminders()
    stats = sg_svc.circle_stats()
    open_rows = sg_svc.open_meetings()
    past = sg_svc.recent_meetings()
    owner_tz = (current_user.timezone or "UTC").strip() or "UTC"
    from ..services.timefmt import timezone_groups, timezone_label
    tz_groups = timezone_groups(selected=owner_tz)
    selected_tz_label = timezone_label(owner_tz)
    for group in tz_groups:
        for opt in group["options"]:
            if opt.get("selected"):
                selected_tz_label = opt["label"]
                break
        else:
            continue
        break
    # Both founders take 1:1s through the same questionnaire, so the panels
    # cover whoever has one rather than Saman alone.
    coaches = [(key, intake_svc.coach_label(key))
               for key in intake_svc.QUESTIONS_BY_COACH]
    intakes = intake_svc.studio_intakes(limit=30)
    intake_meeting_ids = {i.meeting_id for i in intakes if i.meeting_id}
    # Intake-linked 1:1s live only in the intakes panel (not duplicated below).
    open_rows = [m for m in open_rows if m.id not in intake_meeting_ids]
    seat_map = sg_svc.seats_for_meetings(
        open_rows + past
        + [i.meeting for i in intakes if i.meeting_id and i.meeting]
    )
    intake_rows = []
    for intake in intakes:
        answers = intake_svc.answer_rows(intake)
        meeting = intake.meeting
        intake_rows.append({
            "intake": intake,
            "coach_label": intake_svc.coach_label(intake.coach),
            "answers": answers,
            "member": intake.member,
            "meeting": meeting,
            "seats": (
                seat_map.get(intake.meeting_id, []) if intake.meeting_id else []
            ),
        })
    # The week editor opens on whatever is already saved for the chosen coach,
    # so editing and setting up for the first time are the same screen.
    picked_coach = (request.args.get("coach") or "").strip().lower()
    picked_coach = intake_svc.normalize_coach(picked_coach) or coaches[0][0]
    week_grid = {day: sorted(hours)
                 for day, hours in intake_svc.week_grid(picked_coach).items()}
    week_tz = intake_svc.week_timezone(picked_coach, owner_tz)
    week_counts = {day: len(hours) for day, hours in week_grid.items()}
    saved_weeks = [
        {
            "coach": key,
            "coach_label": label,
            "hours": sum(len(h) for h in intake_svc.week_grid(key).values()),
            "tz_label": timezone_label(intake_svc.week_timezone(key, owner_tz)),
        }
        for key, label in coaches
    ]
    return render_template(
        "admin/support_groups.html",
        circle_stats=stats,
        open_meetings=open_rows,
        past_meetings=past,
        seat_map=seat_map,
        owner_tz=owner_tz,
        coaches=coaches,
        picked_coach=picked_coach,
        week_grid=week_grid,
        week_counts=week_counts,
        week_tz=week_tz,
        day_hours=intake_svc.DAY_HOURS,
        saved_weeks=saved_weeks,
        intake_rows=intake_rows,
        weekday_labels=intake_svc.WEEKDAY_LABELS,
        minutes_to_hhmm=intake_svc.minutes_to_hhmm,
        tz_groups=tz_groups,
        selected_tz_label=selected_tz_label,
    )


@bp.route("/support-groups/availability", methods=["POST"])
@admin_required
def support_groups_availability():
    from ..services import coaching_intake as intake_svc

    coach = intake_svc.normalize_coach(request.form.get("coach") or "")
    if not coach:
        flash("Pick whose availability that is.", "error")
        return redirect(url_for("admin.support_groups"))

    # The whole week arrives at once as "weekday:hour" ticks, so unticking is
    # how a window is removed and there is nothing to save one row at a time.
    picks: dict[int, list[int]] = {day: [] for day in range(7)}
    for raw in request.form.getlist("slot"):
        day_s, _, hour_s = (raw or "").partition(":")
        if day_s.isdigit() and hour_s.isdigit():
            day, hour = int(day_s), int(hour_s)
            if 0 <= day <= 6 and 0 <= hour <= 23:
                picks[day].append(hour)

    tz = (request.form.get("timezone") or current_user.timezone or "UTC").strip()
    saved, err = intake_svc.set_week_availability(coach, picks, tz_name=tz)
    if err:
        flash(err, "error")
    elif saved:
        hours = sum(len(v) for v in picks.values())
        flash(f"{intake_svc.coach_label(coach)}'s week saved — {hours} "
              f"hour{'s' if hours != 1 else ''} open across "
              f"{saved} window{'s' if saved != 1 else ''}.", "success")
    else:
        flash(f"{intake_svc.coach_label(coach)} is now marked unavailable all "
              "week. Nothing can be booked until you open some hours.", "info")
    return redirect(url_for("admin.support_groups", coach=coach))


@bp.route("/support-groups/form", methods=["POST"])
@admin_required
def support_groups_form():
    from ..services import support_groups as sg_svc

    kind = (request.form.get("kind") or "").strip().lower()
    tz = (request.form.get("timezone") or current_user.timezone or "UTC").strip()
    meeting, err = sg_svc.schedule_studio_session(
        current_user,
        kind=kind,
        date_s=request.form.get("meeting_date") or "",
        time_s=request.form.get("meeting_time") or "",
        tz_name=tz,
        title=request.form.get("title") or "",
        coach=request.form.get("coach") or "",
        member_email=request.form.get("member_email") or "",
    )
    if err:
        flash(err, "error")
    else:
        label = sg_svc.meeting_display_title(meeting)
        flash(
            f"{label} scheduled — Daily room ready; seated members were emailed.",
            "success",
        )
    return redirect(url_for("admin.support_groups"))


@bp.route("/support-groups/<int:meeting_id>/schedule", methods=["POST"])
@admin_required
def support_groups_schedule(meeting_id):
    from ..models import SupportGroupMeeting
    from ..services import support_groups as sg_svc
    meeting = db.session.get(SupportGroupMeeting, meeting_id) or abort(404)
    if meeting.status not in ("draft", "scheduled"):
        flash("That meeting can no longer be scheduled.", "error")
        return redirect(url_for("admin.support_groups"))
    tz = (request.form.get("timezone") or current_user.timezone or "UTC").strip()
    # Prefer separate date + time fields; fall back to legacy datetime-local.
    from ..services.timefmt import parse_owner_local, parse_owner_parts

    when = parse_owner_parts(
        request.form.get("meeting_date") or "",
        request.form.get("meeting_time") or "",
        tz,
    )
    if when is None:
        when = parse_owner_local(request.form.get("scheduled_at") or "", tz)
    err = sg_svc.schedule_meeting(
        meeting,
        scheduled_at=when,
        owner=current_user,
    )
    if err:
        flash(err, "error")
    else:
        flash(
            "Meeting scheduled — Daily room ready; members were emailed and notified.",
            "success",
        )
    return redirect(url_for("admin.support_groups"))


@bp.route("/support-groups/<int:meeting_id>/complete", methods=["POST"])
@admin_required
def support_groups_complete(meeting_id):
    from ..models import SupportGroupMeeting
    from ..services import support_groups as sg_svc
    meeting = db.session.get(SupportGroupMeeting, meeting_id) or abort(404)
    if meeting.status != "scheduled":
        flash("Only scheduled meetings can be marked complete.", "error")
    else:
        sg_svc.complete_meeting(meeting)
        flash("Marked complete.", "success")
    return redirect(url_for("admin.support_groups"))


@bp.route("/support-groups/<int:meeting_id>/cancel", methods=["POST"])
@admin_required
def support_groups_cancel(meeting_id):
    from ..models import SupportGroupMeeting
    from ..services import support_groups as sg_svc
    meeting = db.session.get(SupportGroupMeeting, meeting_id) or abort(404)
    if meeting.status not in ("draft", "scheduled"):
        flash("That meeting is already closed.", "error")
    else:
        sg_svc.cancel_meeting(meeting, owner=current_user)
        flash("Meeting cancelled.", "success")
    return redirect(url_for("admin.support_groups"))

