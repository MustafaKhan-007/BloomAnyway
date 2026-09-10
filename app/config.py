"""Configuration classes, read from environment variables.

Local development works with zero configuration (SQLite + console email).
Production (APP_ENV=production) refuses to boot with missing secrets.
"""
import os
from datetime import timedelta


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        return "sqlite:///firstlight-dev.db"
    # Render (and Heroku) hand out postgres:// which SQLAlchemy 2.x rejects.
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


class Config:
    # Optional: if unset, a persistent key is generated and stored in the
    # database on first boot (see app factory), so it survives restarts
    # without needing an env var.
    SECRET_KEY = os.environ.get("SECRET_KEY", "").strip()

    SQLALCHEMY_DATABASE_URI = _database_url()
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # Managed Postgres (e.g. Render) drops idle connections; without pre-ping the
    # first request after an idle spell hits a dead connection and 500s
    # ("something went sideways"). Pre-ping + recycle keeps the pool healthy.
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
        "pool_recycle": 280,
    }

    # Video uploads are streamed to a directory on disk (a mounted persistent
    # disk in production, the instance folder locally) instead of the database,
    # so they can be large without exhausting worker memory. MAX_VIDEO_MB is the
    # per-file cap; MAX_CONTENT_LENGTH sits just above it (+ headroom for the
    # thumbnail and several 25 MB course files) and rejects absurd bodies fast.
    MAX_VIDEO_MB = int(os.environ.get("MAX_VIDEO_MB", "1024") or 1024)
    VIDEO_STORAGE_DIR = os.environ.get("VIDEO_STORAGE_DIR", "").strip()

    # Raw reel uploads — the file behind a reel-review request or a Reel of
    # the Week entry. Their own folder on the media disk, because they are
    # swept weekly and a sweep must not be able to reach the owner's videos.
    REEL_RAW_DIR = os.environ.get("REEL_RAW_DIR", "").strip()
    # Sent up a slice at a time, like a course video, so this ceiling is not
    # the request body limit any more. Cloudflare Free still rejects a single
    # body over ~100 MB, which is why REEL_CHUNK_MB stays well under it.
    REEL_RAW_MAX_MB = int(os.environ.get("REEL_RAW_MAX_MB", "2048") or 2048)
    REEL_CHUNK_MB = max(1, min(
        32, int(os.environ.get("REEL_CHUNK_MB", "8") or 8)))
    MAX_CONTENT_LENGTH = (MAX_VIDEO_MB + 32) * 1024 * 1024

    # Course module files (lesson videos, worksheets, slides). These stream to
    # their own directory on the media disk rather than into Postgres, so a
    # module can hold a full-length video without bloating the database or a
    # worker's memory.
    COURSE_FILES_DIR = os.environ.get("COURSE_FILES_DIR", "").strip()
    # Studio uploads a big file in pieces, so the per-file ceiling is not the
    # request body limit any more. Cloudflare Free still rejects a single body
    # over ~100 MB, which is why COURSE_CHUNK_MB stays well under it.
    COURSE_UPLOAD_MAX_MB = int(
        os.environ.get("COURSE_UPLOAD_MAX_MB", "2048") or 2048)
    COURSE_CHUNK_MB = max(1, min(
        32, int(os.environ.get("COURSE_CHUNK_MB", "8") or 8)))

    # Sessions / auth
    SESSION_COOKIE_NAME = "firstlight_session"
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    REMEMBER_COOKIE_NAME = "firstlight_remember"
    REMEMBER_COOKIE_DURATION = timedelta(days=30)
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SAMESITE = "Lax"
    CODE_MAX_AGE_MINUTES = 15
    # Admin panel uses a sliding "idle" timeout instead of a hard daily re-login:
    # each admin action refreshes the clock, and re-auth is only required after
    # this many days of no admin activity. The session cookie must outlive it.
    ADMIN_IDLE_DAYS = 14
    PERMANENT_SESSION_LIFETIME = timedelta(days=30)

    # Email — two transports, first configured one wins:
    # 1. BREVO_API_KEY: HTTP API (works on hosts that block SMTP, e.g. Render)
    # 2. SMTP_*: classic SMTP relay (optional fallback)
    BREVO_API_KEY = os.environ.get("BREVO_API_KEY", "").strip()
    # Transactional template IDs (Brevo → Transactional → Templates, number after #).
    # Dedicated templates (#2–#9) for known scenarios. General (#10) only for
    # emails that do not yet have their own Brevo template (SUBJECT / PREVIEW /
    # HEADER / TITLE / BODY / BUTTON_TEXT / BUTTON_URL).
    BREVO_TEMPLATE_GENERAL = int(
        os.environ.get("BREVO_TEMPLATE_GENERAL", "10") or 0
    )
    # Welcome: sent once after email is verified. Confirm: 6-digit signup code email.
    BREVO_TEMPLATE_WELCOME = int(
        os.environ.get("BREVO_TEMPLATE_WELCOME")
        or os.environ.get("BREVO_TEMPLATE_CONFIRM_LEGACY", "2")
        or 0
    )
    BREVO_TEMPLATE_CONFIRM = int(
        os.environ.get("BREVO_TEMPLATE_CONFIRM", "3") or 0
    )
    BREVO_TEMPLATE_RECEIPT = int(
        os.environ.get("BREVO_TEMPLATE_RECEIPT", "4") or 0
    )
    BREVO_TEMPLATE_HEALING = int(
        os.environ.get("BREVO_TEMPLATE_HEALING", "5") or 0
    )
    BREVO_TEMPLATE_CREATOR = int(
        os.environ.get("BREVO_TEMPLATE_CREATOR", "6") or 0
    )
    BREVO_TEMPLATE_CARD_DECLINED = int(
        os.environ.get("BREVO_TEMPLATE_CARD_DECLINED", "7") or 0
    )
    BREVO_TEMPLATE_CANCEL = int(
        os.environ.get("BREVO_TEMPLATE_CANCEL", "8") or 0
    )
    BREVO_TEMPLATE_NEWSLETTER = int(
        os.environ.get("BREVO_TEMPLATE_NEWSLETTER", "9") or 0
    )
    BREVO_TEMPLATE_SUPPORT_BOOKED = int(
        os.environ.get("BREVO_TEMPLATE_SUPPORT_BOOKED", "11") or 0
    )
    BREVO_TEMPLATE_SUPPORT_LEFT = int(
        os.environ.get("BREVO_TEMPLATE_SUPPORT_LEFT", "12") or 0
    )
    BREVO_TEMPLATE_SUPPORT_REMINDER = int(
        os.environ.get("BREVO_TEMPLATE_SUPPORT_REMINDER", "13") or 0
    )
    BREVO_TEMPLATE_SUPPORT_HOST_CANCEL = int(
        os.environ.get("BREVO_TEMPLATE_SUPPORT_HOST_CANCEL", "14") or 0
    )
    BREVO_TEMPLATE_FACILITATOR_BOOKED = int(
        os.environ.get("BREVO_TEMPLATE_FACILITATOR_BOOKED", "15") or 0
    )
    BREVO_TEMPLATE_ONE_ON_ONE_BOOKED = int(
        os.environ.get("BREVO_TEMPLATE_ONE_ON_ONE_BOOKED", "16") or 0
    )
    BREVO_TEMPLATE_FACILITATOR_CANCELLED = int(
        os.environ.get("BREVO_TEMPLATE_FACILITATOR_CANCELLED", "17") or 0
    )
    BREVO_TEMPLATE_ONE_ON_ONE_CANCELLED = int(
        os.environ.get("BREVO_TEMPLATE_ONE_ON_ONE_CANCELLED", "18") or 0
    )
    BREVO_TEMPLATE_FULL_BLOOM = int(
        os.environ.get("BREVO_TEMPLATE_FULL_BLOOM", "19") or 0
    )
    # Replies an owner writes in Studio. All three take the same five
    # parameters (SUBJECT / PREVIEW / HEADER / TITLE / BODY); which one goes
    # out depends on the address chosen, so each face keeps its own look.
    BREVO_TEMPLATE_CUSTOMER_SUPPORT = int(
        os.environ.get("BREVO_TEMPLATE_CUSTOMER_SUPPORT", "20") or 0
    )
    BREVO_TEMPLATE_REPLY_CREATOR = int(
        os.environ.get("BREVO_TEMPLATE_REPLY_CREATOR", "21") or 0
    )
    BREVO_TEMPLATE_REPLY_HEALING = int(
        os.environ.get("BREVO_TEMPLATE_REPLY_HEALING", "22") or 0
    )
    # Sent alongside the receipt when what was bought is the challenge, so the
    # buyer gets what they paid for and what happens next in the same breath.
    # Gifts have no designed template yet, so these sit at 0 and the general
    # layout carries them. Set one here the day there is one.
    BREVO_TEMPLATE_GIFT_RECEIVED = int(
        os.environ.get("BREVO_TEMPLATE_GIFT_RECEIVED", "0") or 0
    )
    BREVO_TEMPLATE_GIFT_SENT = int(
        os.environ.get("BREVO_TEMPLATE_GIFT_SENT", "0") or 0
    )
    BREVO_TEMPLATE_GIFT_STUCK = int(
        os.environ.get("BREVO_TEMPLATE_GIFT_STUCK", "0") or 0
    )
    BREVO_TEMPLATE_CHALLENGE_WELCOME = int(
        os.environ.get("BREVO_TEMPLATE_CHALLENGE_WELCOME", "30") or 0
    )
    # Optional absolute site origin for email CTAs when no request context.
    PUBLIC_BASE_URL = (
        os.environ.get("PUBLIC_BASE_URL", "").strip()
        or "https://www.bloomanyway.online"
    )
    # Days of access kept after a failed membership renewal charge.
    # After this window Stripe cancels the sub and membership access is revoked.
    MEMBERSHIP_GRACE_DAYS = int(
        os.environ.get("MEMBERSHIP_GRACE_DAYS", "5") or 5
    )
    SMTP_HOST = os.environ.get("SMTP_HOST", "").strip()
    SMTP_PORT = int(os.environ.get("SMTP_PORT", "587") or 587)
    SMTP_USER = os.environ.get("SMTP_USER", "")
    SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
    MAIL_FROM = os.environ.get("MAIL_FROM", "Bloom Anyway <hello@localhost>")

    # Cloudflare Turnstile (signup only). Prefer TURNSTILE_SECRET (Spin naming);
    # TURNSTILE_SECRET_KEY is accepted as a legacy alias.
    TURNSTILE_SITE_KEY = (
        os.environ.get("TURNSTILE_SITE_KEY", "").strip()
        or "0x4AAAAAAEAGFowmHgyFM5Kf"
    )
    TURNSTILE_SECRET = (
        os.environ.get("TURNSTILE_SECRET", "").strip()
        or os.environ.get("TURNSTILE_SECRET_KEY", "").strip()
    )
    TURNSTILE_SECRET_KEY = TURNSTILE_SECRET  # legacy alias

    # Stripe (courses, guides, memberships). Secret key + webhook signing secret.
    STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
    # Optional shared secret for /cron/* jobs (e.g. support-group reminders).
    CRON_SECRET = os.environ.get("CRON_SECRET", "").strip()
    # Optional self-hosted digital files for ShopPurchase.file_key
    SHOP_FILES_DIR = os.environ.get("SHOP_FILES_DIR", "").strip()

    # Daily.co (embedded support-group rooms).
    # API key from https://dashboard.daily.co/developers
    DAILY_API_KEY = os.environ.get("DAILY_API_KEY", "").strip()
    # Optional subdomain label used only for stub URLs in tests (e.g. bloomanyway).
    DAILY_DOMAIN = os.environ.get("DAILY_DOMAIN", "bloomanyway").strip() or "bloomanyway"
    DAILY_MEETING_DURATION = int(os.environ.get("DAILY_MEETING_DURATION", "30") or 30)
    # Force stub rooms without calling Daily (tests); auto-on when TESTING
    # and DAILY_API_KEY is unset.
    DAILY_STUB = os.environ.get("DAILY_STUB", "").strip().lower() in (
        "1", "true", "yes", "on",
    )

    # Flask-Limiter: in-memory storage. Fine at this scale; counters reset on
    # deploy/restart (noted in README).
    RATELIMIT_STORAGE_URI = "memory://"


def _strip_config_quotes(value: str) -> str:
    v = (value or "").strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1].strip()
    return v


# Normalize env pastes once at import (Render/dashboard often wrap in quotes).
Config.BREVO_API_KEY = _strip_config_quotes(Config.BREVO_API_KEY)
Config.MAIL_FROM = _strip_config_quotes(Config.MAIL_FROM)
Config.TURNSTILE_SITE_KEY = _strip_config_quotes(Config.TURNSTILE_SITE_KEY)
Config.TURNSTILE_SECRET = _strip_config_quotes(Config.TURNSTILE_SECRET)
Config.TURNSTILE_SECRET_KEY = Config.TURNSTILE_SECRET
Config.DAILY_API_KEY = _strip_config_quotes(Config.DAILY_API_KEY)
Config.DAILY_DOMAIN = _strip_config_quotes(Config.DAILY_DOMAIN) or "bloomanyway"


class DevConfig(Config):
    DEBUG = True
    SESSION_COOKIE_SECURE = False
    REMEMBER_COOKIE_SECURE = False
    # Zero-config local dev: a fixed dev key unless one is provided.
    SECRET_KEY = os.environ.get("SECRET_KEY", "").strip() or "dev-only-not-secret"
    # Cloudflare always-pass test keys when no real secret is set (Turnstile docs).
    TURNSTILE_SITE_KEY = (
        os.environ.get("TURNSTILE_SITE_KEY", "").strip()
        or "1x00000000000000000000AA"
    )
    TURNSTILE_SECRET = (
        os.environ.get("TURNSTILE_SECRET", "").strip()
        or os.environ.get("TURNSTILE_SECRET_KEY", "").strip()
        or "1x0000000000000000000000000000000AA"
    )
    TURNSTILE_SECRET_KEY = TURNSTILE_SECRET

class ProdConfig(Config):
    DEBUG = False
    PREFERRED_URL_SCHEME = "https"
    SESSION_COOKIE_SECURE = True
    REMEMBER_COOKIE_SECURE = True

    #: the only env vars that must be present in prod (everything else is
    #: optional or auto-managed)
    REQUIRED_ENV = (
        "DATABASE_URL",
        "MAIL_FROM",
    )
    #: at least one email transport must be configured
    SMTP_ENV = ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD")

    @classmethod
    def validate(cls) -> None:
        def unset(name):
            return os.environ.get(name, "").strip() == ""

        def strip_quotes(value: str) -> str:
            v = (value or "").strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1].strip()
            return v

        missing = [name for name in cls.REQUIRED_ENV if unset(name)]
        if unset("BREVO_API_KEY") and any(unset(name) for name in cls.SMTP_ENV):
            missing.append("BREVO_API_KEY or all of SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASSWORD")
        # Turnstile: site key defaults to the dashboard widget; set TURNSTILE_SECRET
        # on the host so signup siteverify can succeed.
        mail_from = strip_quotes(os.environ.get("MAIL_FROM", ""))
        if mail_from and ("@" not in mail_from or mail_from.lower().endswith("@localhost")):
            missing.append("MAIL_FROM (must be a real verified sender, not @localhost)")
        if missing:
            raise RuntimeError(
                "Refusing to start in production. Missing/placeholder env vars: "
                + ", ".join(missing)
            )
        # A SQLite database in production lives on an ephemeral disk (wiped on
        # every restart/deploy), which silently loses the owner account, orders,
        # etc. Force a real, persistent database.
        if cls.SQLALCHEMY_DATABASE_URI.startswith("sqlite"):
            raise RuntimeError(
                "Refusing to start in production with a SQLite database — it is "
                "not persistent. Attach a managed Postgres database and set "
                "DATABASE_URL to its connection string."
            )


def get_config():
    env = os.environ.get("APP_ENV", "").lower()
    # Render sets RENDER=true on every service. If APP_ENV wasn't set explicitly
    # we still force production there, so the app uses the managed (persistent)
    # Postgres via DATABASE_URL instead of ephemeral SQLite — otherwise the disk
    # is wiped on every restart/deploy and the owner account "resets".
    if not env and os.environ.get("RENDER"):
        env = "production"
    if not env:
        env = "development"
    if env == "production":
        ProdConfig.validate()
        return ProdConfig
    return DevConfig
