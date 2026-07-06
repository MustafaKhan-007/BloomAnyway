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
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-not-secret")

    SQLALCHEMY_DATABASE_URI = _database_url()
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    SITE_URL = os.environ.get("SITE_URL", "http://localhost:5000").rstrip("/")
    ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "").strip().lower()

    # Sessions / auth
    SESSION_COOKIE_NAME = "firstlight_session"
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    REMEMBER_COOKIE_NAME = "firstlight_remember"
    REMEMBER_COOKIE_DURATION = timedelta(days=30)
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SAMESITE = "Lax"
    MAGIC_LINK_MAX_AGE_MINUTES = 15
    ADMIN_FRESH_LOGIN_HOURS = 24

    # Email (SMTP relay: Resend, Postmark, any)
    SMTP_HOST = os.environ.get("SMTP_HOST", "").strip()
    SMTP_PORT = int(os.environ.get("SMTP_PORT", "587") or 587)
    SMTP_USER = os.environ.get("SMTP_USER", "")
    SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
    MAIL_FROM = os.environ.get("MAIL_FROM", "First Light <hello@localhost>")

    # Lemon Squeezy
    LEMONSQUEEZY_API_KEY = os.environ.get("LEMONSQUEEZY_API_KEY", "")
    LEMONSQUEEZY_WEBHOOK_SECRET = os.environ.get("LEMONSQUEEZY_WEBHOOK_SECRET", "")
    LEMONSQUEEZY_STORE_ID = os.environ.get("LEMONSQUEEZY_STORE_ID", "")

    # Flask-Limiter: in-memory storage. Fine at this scale; counters reset on
    # deploy/restart (noted in README).
    RATELIMIT_STORAGE_URI = "memory://"


class DevConfig(Config):
    DEBUG = True
    SESSION_COOKIE_SECURE = False
    REMEMBER_COOKIE_SECURE = False


class ProdConfig(Config):
    DEBUG = False
    PREFERRED_URL_SCHEME = "https"
    SESSION_COOKIE_SECURE = True
    REMEMBER_COOKIE_SECURE = True

    #: env vars that must be present (and not left at a placeholder) in prod
    REQUIRED_ENV = (
        "SECRET_KEY",
        "DATABASE_URL",
        "LEMONSQUEEZY_WEBHOOK_SECRET",
        "SMTP_HOST",
        "SMTP_PORT",
        "SMTP_USER",
        "SMTP_PASSWORD",
        "MAIL_FROM",
        "ADMIN_EMAIL",
        "SITE_URL",
    )

    @classmethod
    def validate(cls) -> None:
        placeholders = {"", "change-me", "change-me-too", "dev-only-not-secret"}
        missing = [
            name for name in cls.REQUIRED_ENV
            if os.environ.get(name, "").strip() in placeholders
        ]
        if missing:
            raise RuntimeError(
                "Refusing to start in production. Missing/placeholder env vars: "
                + ", ".join(missing)
            )


def get_config():
    env = os.environ.get("APP_ENV", "development").lower()
    if env == "production":
        ProdConfig.validate()
        return ProdConfig
    return DevConfig
