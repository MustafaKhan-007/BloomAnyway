"""Key-value site settings, read fresh for each page.

Two gunicorn workers serve this site, and each one used to keep its own copy
of these for the life of the process. Saving in Studio changed the copy
belonging to whichever worker took the POST; the other one went on serving
what it had read at boot, and no amount of refreshing would shift it —
"I changed the date and it still says the old one" until the next deploy.

So nothing is held across a request now. The rows are read once per request
and shared for the length of it, which is one small query for a table of a
few dozen values. Outside a request — jobs, the CLI, boot — a short-lived
process copy stands in.
"""
import secrets
import time
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
RETIRED_SUPPORT_EMAILS = ("bloomsupport@bloomanyway.online",)

DEFAULTS = {
    "site_title": "Bloom Anyway",
    "instagram_url": "https://instagram.com/",
    "hero_image_url": "",
    "portrait_url": "",
    "contact_email": "customersupport@bloomanyway.online",
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
}

#: old brand names that should be rewritten to the current default on boot/seed
_LEGACY_TITLES = frozenset({"first light", "no saddies just baddies"})

#: last known values, for use away from a request and if the database blinks
_cache: dict[str, str] = {}
_read_at = 0.0

#: how long that stand-in copy is trusted outside a request
_TTL_SECONDS = 5.0


def _read_rows() -> dict[str, str]:
    rows = {}
    for row in Setting.query.all():
        if row.key.startswith("_"):   # internal (e.g. the secret key) — keep private
            continue
        rows[row.key] = row.value
    return rows


def _reload() -> dict[str, str]:
    """Read the table and keep what came back.

    A database that blinks hands back the last thing we read rather than every
    default at once, which would blank the site's name and support address
    until it came back.
    """
    global _cache, _read_at
    try:
        rows = _read_rows()
    except Exception:
        return _cache
    # Put the finished dict in place rather than emptying and refilling the
    # one being read: four threads share this worker, and one of them must
    # never catch it halfway.
    _cache = rows
    _read_at = time.monotonic()
    return rows


def _current() -> dict[str, str]:
    """Everything stored, as it stands now.

    Read once per request and kept for the rest of it, so a page can ask fifty
    times over and pay for one query.
    """
    try:
        from flask import g, has_request_context
        in_request = has_request_context()
    except Exception:
        in_request = False

    if in_request:
        held = getattr(g, "_site_settings", None)
        if held is None:
            held = _reload()
            g._site_settings = held
        return held
    if _cache and (time.monotonic() - _read_at) <= _TTL_SECONDS:
        return _cache
    return _reload()


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
    if default is None:
        default = DEFAULTS.get(key, "")
    return _current().get(key, default)


def all_settings() -> dict:
    merged = dict(DEFAULTS)
    merged.update(_current())
    return merged


def set_setting(key: str, value: str) -> None:
    row = db.session.get(Setting, key)
    if row is None:
        row = Setting(key=key, value=value)
        db.session.add(row)
    else:
        row.value = value
    db.session.commit()
    # The rest of this request reads what was just saved, rather than the copy
    # taken before it was.
    _cache[key] = value
    try:
        from flask import g, has_request_context
        if has_request_context() and getattr(g, "_site_settings", None) is not None:
            g._site_settings[key] = value
    except Exception:
        pass


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
    """Forget everything held, so the next read goes to the table."""
    global _cache, _read_at
    _cache = {}
    _read_at = 0.0
    try:
        from flask import g, has_request_context
        if has_request_context():
            g.pop("_site_settings", None)
    except Exception:
        pass
