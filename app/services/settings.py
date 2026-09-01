"""Key-value site settings with a tiny in-process cache."""
import secrets
from datetime import date

from ..extensions import db
from ..models import Setting

#: internal settings (prefixed "_") are never exposed to templates via `site`
SECRET_KEY_SETTING = "_secret_key"

#: set once the support address has been filled in, so clearing it in Studio
#: sticks instead of being written back on the next boot
SUPPORT_EMAIL_SEEDED = "_contact_email_seeded"

#: support addresses we have shipped as the default over time. A site still
#: sitting on an old one gets moved to the current default; anything the owner
#: typed themselves is left exactly as it is.
RETIRED_SUPPORT_EMAILS = ("customersupport@bloomanyway.online",)

DEFAULTS = {
    "site_title": "Bloom Anyway",
    "instagram_url": "https://instagram.com/",
    "hero_image_url": "",
    "portrait_url": "",
    "contact_email": "bloomsupport@bloomanyway.online",
    "announcement_text": "",
    "announcement_expires": "",   # ISO date (YYYY-MM-DD); blank = never expires
    "announcement_url": "",       # optional; whole card is the button (URL hidden)
    # home-page spotlight
    "creator_name": "",
    "creator_instagram": "",
    "creator_image_url": "",
    "creator_blurb": "",
    "creator_expires": "",        # ISO date the Creator of the month runs until
    "reel_url": "",
    "reel_description": "",
    "reel_expires": "",           # ISO date the Reel of the week runs until
    # last end-date each slot was warned about, so owners get one notice each
    "spotlight_creator_notified": "",
    "spotlight_reel_notified": "",
    # 1:1 coaching + facilitator booking (external calendars)
    "ayesha_booking_url": "",
    "saman_booking_url": "",
    "facilitator_booking_url": "",
    # Stripe Price ids for paid add-ons (preferred over external booking URLs)
    "facilitator_stripe_price_id": "",
    "ayesha_stripe_price_id": "",
    "saman_stripe_price_id": "",
    # ISO date — banner + founder prices on /membership while today <= this date
    "founder_price_ends": "2026-09-30",
    # Memberships are under maintenance: only admins and the accounts whose
    # emails are listed here (one per line) can view/buy/manage memberships.
    # Everyone else sees an "under maintenance" page.
    "membership_access_emails": "",
}

#: old brand names that should be rewritten to the current default on boot/seed
_LEGACY_TITLES = frozenset({"first light", "no saddies just baddies"})

_cache: dict[str, str] = {}
_loaded = False


def _load():
    global _loaded
    _cache.clear()
    for row in Setting.query.all():
        if row.key.startswith("_"):   # internal (e.g. the secret key) — keep private
            continue
        _cache[row.key] = row.value
    _loaded = True


def get_or_create_secret_key() -> str:
    """A stable Flask secret key stored in the database, generated on first use.

    Lets the app run without a SECRET_KEY env var while still surviving restarts.
    """
    row = db.session.get(Setting, SECRET_KEY_SETTING)
    if row is None:
        row = Setting(key=SECRET_KEY_SETTING, value=secrets.token_hex(32))
        db.session.add(row)
        db.session.commit()
    return row.value


def get_setting(key: str, default: str | None = None) -> str:
    if not _loaded:
        _load()
    if default is None:
        default = DEFAULTS.get(key, "")
    return _cache.get(key, default)


def all_settings() -> dict:
    if not _loaded:
        _load()
    merged = dict(DEFAULTS)
    merged.update(_cache)
    return merged


def set_setting(key: str, value: str) -> None:
    row = db.session.get(Setting, key)
    if row is None:
        row = Setting(key=key, value=value)
        db.session.add(row)
    else:
        row.value = value
    db.session.commit()
    _cache[key] = value


def active_announcement() -> str:
    """The announcement text, or "" if unset or past its expiry date."""
    text = get_setting("announcement_text")
    if not text:
        return ""
    expires = get_setting("announcement_expires")
    if expires:
        try:
            if date.fromisoformat(expires) < date.today():
                return ""
        except ValueError:
            pass
    return text


def sanitize_announcement_url(raw: str | None) -> str:
    """Allow same-site paths or http(s) URLs; drop everything else."""
    url = (raw or "").strip()
    if not url:
        return ""
    if url.startswith("/") and not url.startswith("//"):
        return url[:500]
    lower = url.lower()
    if lower.startswith("http://") or lower.startswith("https://"):
        return url[:500]
    return ""


def _site_hosts() -> set[str]:
    """Hostnames that count as this Bloom Anyway site."""
    hosts = {"bloomanyway.com", "www.bloomanyway.com"}
    try:
        from flask import current_app, has_app_context, has_request_context, request
        if has_request_context():
            host = (request.host or "").split(":")[0].strip().lower()
            if host:
                hosts.add(host)
        if has_app_context():
            server = (current_app.config.get("SERVER_NAME") or "").split(":")[0].strip().lower()
            if server:
                hosts.add(server)
    except Exception:
        pass
    return hosts


def resolve_announcement_link(raw: str | None) -> tuple[str, bool]:
    """Return ``(href, is_external)``.

    Same-site absolute URLs are rewritten to a path so they open in the
    current tab; true off-site links stay absolute and open in a new tab.
    """
    from urllib.parse import urlparse

    url = sanitize_announcement_url(raw)
    if not url:
        return "", False
    if url.startswith("/"):
        return url, False
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if host and host in _site_hosts():
            path = parsed.path or "/"
            if parsed.query:
                path = f"{path}?{parsed.query}"
            if parsed.fragment:
                path = f"{path}#{parsed.fragment}"
            return path[:500], False
    except Exception:
        pass
    return url, True


def active_announcements() -> list[dict]:
    """Live announcements as ``{"body", "url", "external"}`` dicts."""
    from ..models import Announcement
    out: list[dict] = []
    try:
        legacy = active_announcement()
        if legacy:
            href, external = resolve_announcement_link(get_setting("announcement_url"))
            out.append({"body": legacy, "url": href, "external": external})
        rows = (Announcement.query
                .order_by(Announcement.sort_order, Announcement.created_at.desc()).all())
        for a in rows:
            if not a.is_live():
                continue
            href, external = resolve_announcement_link(a.link_url)
            out.append({"body": a.body, "url": href, "external": external})
    except Exception:
        # Missing table / DB hiccup must not blank the whole site.
        return out
    return out


def ensure_brand_title() -> bool:
    """If the stored site title is still an old brand name, rename it to
    ``Bloom Anyway``. Returns True when a rewrite happened. Safe to call on
    every boot — custom titles the owner typed themselves are left alone."""
    current = (get_setting("site_title") or "").strip()
    if current.lower() in _LEGACY_TITLES or not current:
        set_setting("site_title", DEFAULTS["site_title"])
        invalidate_cache()
        return True
    return False


def ensure_support_email() -> bool:
    """Keep the public support address current. Returns True if it changed.

    Two jobs. Fill it in the first time, guarded by a marker so an owner who
    deliberately clears the field doesn't get it written back on the next
    deploy. And move a site still sitting on an address we used to ship onto
    the current one — the marker means that first run never repeats, so a
    renamed mailbox would otherwise be stranded on every existing site.
    """
    current = (get_setting("contact_email") or "").strip()
    if current.lower() in RETIRED_SUPPORT_EMAILS:
        set_setting("contact_email", DEFAULTS["contact_email"])
        invalidate_cache()
        return True

    marker = db.session.get(Setting, SUPPORT_EMAIL_SEEDED)
    if marker is not None:
        return False
    filled = False
    if not current:
        set_setting("contact_email", DEFAULTS["contact_email"])
        filled = True
    db.session.add(Setting(key=SUPPORT_EMAIL_SEEDED, value="1"))
    db.session.commit()
    invalidate_cache()
    return filled


def invalidate_cache() -> None:
    global _loaded
    _loaded = False
