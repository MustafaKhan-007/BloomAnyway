"""Bloom Anyway — app factory."""
import logging
from datetime import date

from dotenv import load_dotenv

load_dotenv()

from flask import (Flask, abort, flash, redirect, render_template,  # noqa: E402
                   request)
from flask_login import current_user  # noqa: E402
from sqlalchemy import text  # noqa: E402

from .config import get_config  # noqa: E402
from .extensions import csrf, db, limiter, login_manager, migrate  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

CSP = (
    "default-src 'self'; "
    "script-src 'self' https://cdn.jsdelivr.net https://challenges.cloudflare.com "
    "https://js.stripe.com https://unpkg.com; "
    "worker-src 'self' blob: https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.jsdelivr.net; "
    "font-src 'self' https://fonts.gstatic.com https://cdn.jsdelivr.net data:; "
    "img-src 'self' https: data: blob:; "
    "media-src 'self' blob: mediastream: https://*.daily.co; "
    "frame-src 'self' blob: https://www.instagram.com https://instagram.com "
    "https://challenges.cloudflare.com "
    "https://js.stripe.com https://hooks.stripe.com https://checkout.stripe.com "
    "https://*.daily.co https://daily.co; "
    "connect-src 'self' https://challenges.cloudflare.com "
    "https://cdn.jsdelivr.net https://api.stripe.com https://checkout.stripe.com "
    "https://unpkg.com https://*.daily.co wss://*.daily.co https://daily.co; "
    "base-uri 'self'; form-action 'self' https://checkout.stripe.com; "
    "frame-ancestors 'none'"
)


def _ensure_secret_key(app):
    """Use SECRET_KEY from config if set; otherwise fall back to a persistent
    key stored in the database (generated once). Only touches the DB when no
    key was provided, and degrades to a temporary key if the DB is unreachable."""
    if app.config.get("SECRET_KEY"):
        return
    from .services.settings import get_or_create_secret_key
    with app.app_context():
        try:
            app.config["SECRET_KEY"] = get_or_create_secret_key()
        except Exception:
            db.session.rollback()   # leave the session clean for e.g. `flask db upgrade`
            import secrets
            app.config["SECRET_KEY"] = secrets.token_hex(32)
            logging.getLogger(__name__).warning(
                "Could not load a persistent SECRET_KEY from the database "
                "(is it migrated yet?); using a temporary key for this process."
            )


def _ensure_brand(app):
    """Rewrite a leftover 'First Light' site title, and seed the support address."""
    from .services.settings import ensure_brand_title, ensure_support_email
    log = logging.getLogger(__name__)
    with app.app_context():
        try:
            if ensure_brand_title():
                log.info("Renamed site title from a legacy brand to Bloom Anyway.")
        except Exception:
            db.session.rollback()
        try:
            if ensure_support_email():
                log.info("Filled in the public customer support email address.")
        except Exception:
            db.session.rollback()


def _heal_stale_tiers(app):
    """Drop Creator tier left on demoted co-owners (one-time)."""
    with app.app_context():
        try:
            from .services.owners import heal_stale_owner_creator_tiers
            heal_stale_owner_creator_tiers()
        except Exception:
            db.session.rollback()


def create_app(config_class=None):
    app = Flask(__name__)
    app.config.from_object(config_class or get_config())

    # Prefer live process env for email secrets (Render dashboard updates +
    # avoids stale class attributes from import order).
    import os as _os
    from .config import _strip_config_quotes as _sq
    if _os.environ.get("BREVO_API_KEY", "").strip():
        app.config["BREVO_API_KEY"] = _sq(_os.environ.get("BREVO_API_KEY", ""))
    if _os.environ.get("MAIL_FROM", "").strip():
        app.config["MAIL_FROM"] = _sq(_os.environ.get("MAIL_FROM", ""))
    if _os.environ.get("TURNSTILE_SITE_KEY", "").strip():
        app.config["TURNSTILE_SITE_KEY"] = _sq(_os.environ.get("TURNSTILE_SITE_KEY", ""))
    _ts_secret = (_os.environ.get("TURNSTILE_SECRET", "").strip()
                  or _os.environ.get("TURNSTILE_SECRET_KEY", "").strip())
    if _ts_secret:
        app.config["TURNSTILE_SECRET"] = _sq(_ts_secret)
        app.config["TURNSTILE_SECRET_KEY"] = app.config["TURNSTILE_SECRET"]

    # Resolve where uploaded videos live: a mounted persistent disk in
    # production (VIDEO_STORAGE_DIR), or the instance folder locally.
    #
    # Failing that, they go on whichever disk the course files are on. One
    # attached disk and one setting is how this is usually run, and a Content
    # Hub video landing in the container's own filesystem looks fine until the
    # next deploy takes it — with nothing said, because the row survives.
    course_configured = (app.config.get("COURSE_FILES_DIR") or "").strip()
    video_dir = (app.config.get("VIDEO_STORAGE_DIR") or "").strip()
    if not video_dir and course_configured:
        video_dir = _os.path.join(_os.path.dirname(
            course_configured.rstrip("/") or "/"), "videos")
    video_dir = video_dir or _os.path.join(app.instance_path, "videos")
    app.config["VIDEO_STORAGE_DIR"] = video_dir
    try:
        _os.makedirs(video_dir, exist_ok=True)
    except OSError:
        logging.getLogger(__name__).warning(
            "Could not create video storage directory %s", video_dir)

    shop_dir = (app.config.get("SHOP_FILES_DIR") or "").strip() \
        or _os.path.join(app.instance_path, "shop_files")
    app.config["SHOP_FILES_DIR"] = shop_dir
    try:
        _os.makedirs(shop_dir, exist_ok=True)
    except OSError:
        logging.getLogger(__name__).warning(
            "Could not create shop files directory %s", shop_dir)

    course_dir = (app.config.get("COURSE_FILES_DIR") or "").strip() \
        or _os.path.join(app.instance_path, "course_files")
    app.config["COURSE_FILES_DIR"] = course_dir
    try:
        # Part-uploads land in a sibling folder so a half-sent file is never
        # mistaken for a finished one.
        _os.makedirs(_os.path.join(course_dir, "parts"), exist_ok=True)
    except OSError:
        logging.getLogger(__name__).warning(
            "Could not create course files directory %s", course_dir)

    db.init_app(app)
    migrate.init_app(app, db)
    _ensure_secret_key(app)
    _ensure_brand(app)
    _heal_stale_tiers(app)
    csrf.init_app(app)
    limiter.init_app(app)

    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message = "Sign in to keep going."

    from .models import User

    @login_manager.user_loader
    def load_user(user_id):
        user = db.session.get(User, int(user_id))
        if user and user.deleted_at is None:
            return user
        return None

    # --- blueprints ----------------------------------------------------------
    from .auth import bp as auth_bp
    from .main import bp as main_bp
    from .admin import bp as admin_bp
    from .webhooks import bp as webhooks_bp
    from .forums import bp as forums_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp, url_prefix="/admin")
    app.register_blueprint(webhooks_bp, url_prefix="/webhooks")
    app.register_blueprint(forums_bp, url_prefix="/forums")
    csrf.exempt(webhooks_bp)  # webhook signature check replaces CSRF here

    # Opportunistic housekeeping (also hit /cron/support-groups): support-group
    # reminders, the heads-up before a spotlight slot runs out, and Monday's
    # clear-out of last week's reel entries.
    @app.before_request
    def _timed_reminders():
        path = request.path or ""
        if path.startswith(("/static/", "/healthz", "/cron/")):
            return None
        try:
            from .services import support_groups as sg_svc
            sg_svc.maybe_sweep_reminders()
        except Exception:
            pass
        try:
            from .services import spotlight as spot_svc
            spot_svc.maybe_sweep()
        except Exception:
            pass
        try:
            from .services import reel_of_week as rotw_svc
            rotw_svc.maybe_sweep()
        except Exception:
            pass
        return None

    # --- template globals / filters ------------------------------------------
    from markupsafe import Markup, escape

    from .services.markdown import render_markdown
    from .services.settings import active_announcements, all_settings

    app.jinja_env.filters["markdown"] = render_markdown

    def nl2br(value):
        """Escape user text, then turn newlines into <br> for safe display."""
        escaped = escape(value or "")
        return Markup(str(escaped).replace("\n", "<br>\n"))

    app.jinja_env.filters["nl2br"] = nl2br

    from .services.social_graph import (
        linkify_mentions, recent_notifications, unread_notification_count,
    )
    app.jinja_env.filters["mentions"] = linkify_mentions

    from .services.timefmt import format_local, local_tag, viewer_timezone
    app.jinja_env.filters["localtime"] = format_local
    # Same wording as ``localtime``, wrapped so a browser on another clock can
    # put it right in place. Not for use inside an attribute.
    app.jinja_env.filters["when"] = local_tag

    from .services import badges as badges_service

    app.jinja_env.globals.update(
        primary_badge=badges_service.primary_badge,
        profile_badges=badges_service.profile_badges,
    )

    @app.context_processor
    def inject_globals():
        unread = 0
        nav_notes = []
        anns = []
        if getattr(current_user, "is_authenticated", False):
            try:
                unread = unread_notification_count(current_user)
                nav_notes = recent_notifications(current_user, limit=8)
            except Exception:
                unread = 0
                nav_notes = []
        try:
            anns = active_announcements()
        except Exception:
            anns = []
        preview = {"on": False}
        if getattr(current_user, "is_admin", False):
            try:
                from .services.preview import CHOICES, choice_label
                from .services.preview import state as preview_state
                preview = preview_state(current_user)
                preview["options"] = [(c, choice_label(c)) for c in CHOICES]
            except Exception:
                preview = {"on": False}
        try:
            viewer_tz = viewer_timezone()
        except Exception:
            viewer_tz = "UTC"
        return {"site": all_settings(),
                "announcements": anns,
                "viewer_tz": viewer_tz,
                "viewer_tz_pinned": bool(
                    getattr(current_user, "timezone_pinned", False)),
                "current_year": date.today().year,
                "unread_notes": unread,
                "nav_notifications": nav_notes,
                "owner_preview": preview,
                "turnstile_site_key": app.config.get("TURNSTILE_SITE_KEY") or ""}

    # --- health check ---------------------------------------------------------
    @app.route("/healthz")
    def healthz():
        db.session.execute(text("SELECT 1"))
        return {"status": "ok"}, 200

    # --- cron (shared secret) -------------------------------------------------
    @app.route("/cron/support-groups")
    @app.route("/cron/support-groups/remind")
    def cron_support_group_reminders():
        secret = (app.config.get("CRON_SECRET") or "").strip()
        if not secret:
            abort(404)
        auth = (request.headers.get("Authorization") or "").strip()
        token = ""
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
        if not token:
            token = (request.args.get("key") or "").strip()
        if token != secret:
            abort(404)
        from .services import reel_of_week as rotw_svc
        from .services import spotlight as spot_svc
        from .services import stripe_pay as pay
        from .services import support_groups as sg_svc
        n = sg_svc.dispatch_due_reminders()
        spotlight_notices = spot_svc.sweep_expiry_notices()
        cancels = pay.sweep_cancel_flags() if pay.configured() else {}
        return {"ok": True, "reminders": n, "spotlight": spotlight_notices,
                "cancel_flags": cancels,
                "reels_cleared": rotw_svc.sweep_old_weeks()}, 200

    # --- lightweight page-view counter (no cookies, no IPs) --------------------
    from .models import PageView

    TRACK_EXCLUDE = ("/admin", "/static", "/healthz", "/webhooks", "/auth")

    @app.after_request
    def track_and_harden(response):
        # security headers on everything
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = CSP

        try:
            if (
                request.method == "GET"
                and response.status_code == 200
                and response.mimetype == "text/html"
                and not any(request.path.startswith(p) for p in TRACK_EXCLUDE)
                and len(request.path) <= 300
            ):
                today = date.today()
                dialect = db.engine.dialect.name
                table = PageView.__table__
                if dialect == "postgresql":
                    from sqlalchemy.dialects.postgresql import insert as dialect_insert
                else:
                    from sqlalchemy.dialects.sqlite import insert as dialect_insert
                stmt = dialect_insert(table).values(
                    path=request.path, date=today, count=1)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["path", "date"],
                    set_={"count": table.c.count + 1},
                )
                db.session.execute(stmt)
                db.session.commit()
                try:
                    from .services.attribution import maybe_record_visit
                    if maybe_record_visit() is not None:
                        db.session.commit()
                except Exception:
                    db.session.rollback()
        except Exception:
            db.session.rollback()
        return response

    # --- error pages -----------------------------------------------------------
    @app.errorhandler(404)
    def not_found(_e):
        return render_template("errors/404.html"), 404

    @app.errorhandler(500)
    def server_error(_e):
        db.session.rollback()
        return render_template("errors/500.html"), 500

    @app.errorhandler(429)
    def too_many(_e):
        return render_template("errors/429.html"), 429

    @app.errorhandler(413)
    def too_large(_e):
        # Prefer showing the message inline on the form the user came from
        # (same-origin only) rather than bouncing them to a full error page.
        ref = request.referrer
        if ref and ref.startswith(request.host_url):
            flash("That file was too large to upload \u2014 please choose a smaller "
                  "one and try again.", "error")
            return redirect(ref)
        return render_template("errors/413.html"), 413

    return app
