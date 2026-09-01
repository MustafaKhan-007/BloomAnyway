"""Format stored UTC datetimes in the viewer's timezone.

Timezone names come from the IANA database via ``zoneinfo`` (+ the ``tzdata``
package on platforms that don't ship zoneinfo). Offsets are computed at
request time so DST stays correct year-round.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from flask import request
from flask_login import current_user

DEFAULT_TZ = "UTC"

# Prefer these near the top of Studio pickers (still in the full list too).
_COMMON_TZ = (
    "UTC",
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "America/Phoenix",
    "America/Toronto",
    "America/Vancouver",
    "America/Mexico_City",
    "America/Sao_Paulo",
    "Europe/London",
    "Europe/Dublin",
    "Europe/Paris",
    "Europe/Berlin",
    "Europe/Amsterdam",
    "Europe/Madrid",
    "Europe/Rome",
    "Europe/Zurich",
    "Europe/Stockholm",
    "Africa/Cairo",
    "Africa/Johannesburg",
    "Asia/Dubai",
    "Asia/Karachi",
    "Asia/Kolkata",
    "Asia/Bangkok",
    "Asia/Singapore",
    "Asia/Hong_Kong",
    "Asia/Shanghai",
    "Asia/Tokyo",
    "Asia/Seoul",
    "Australia/Sydney",
    "Australia/Melbourne",
    "Pacific/Auckland",
)


def normalize_timezone(name: str | None) -> str | None:
    raw = (name or "").strip()
    if not raw or len(raw) > 64:
        return None
    try:
        ZoneInfo(raw)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return None
    return raw


def viewer_timezone() -> str:
    if getattr(current_user, "is_authenticated", False):
        saved = normalize_timezone(getattr(current_user, "timezone", None))
        if saved:
            return saved
    cookie = normalize_timezone(request.cookies.get("tz"))
    return cookie or DEFAULT_TZ


def to_local(dt: datetime | None, tz_name: str | None = None) -> datetime | None:
    if dt is None:
        return None
    tz = ZoneInfo(normalize_timezone(tz_name) or viewer_timezone())
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz)


def format_local(dt: datetime | None, fmt: str = "%b %d, %Y · %I:%M %p",
                 tz_name: str | None = None) -> str:
    local = to_local(dt, tz_name)
    if local is None:
        return ""
    return local.strftime(fmt)


def local_now(tz_name: str | None = None) -> datetime:
    """Right now, in the viewer's timezone."""
    tz = ZoneInfo(normalize_timezone(tz_name) or viewer_timezone())
    return datetime.now(timezone.utc).astimezone(tz)


def greeting(name: str | None = "", tz_name: str | None = None,
             now: datetime | None = None) -> str:
    """Time-of-day hello on the viewer's clock, not the server's.

    The small hours get "Still awake?" rather than a cheery good morning —
    someone opening My Space at 3am isn't starting their day.
    """
    hour = (now or local_now(tz_name)).hour
    who = (name or "").strip()
    if hour < 5:
        return f"Still awake, {who}?" if who else "Still awake?"
    if hour < 12:
        label = "Good morning"
    elif hour < 18:
        label = "Good afternoon"
    else:
        label = "Good evening"
    return f"{label}, {who}." if who else f"{label}."


def _offset_label(tz_name: str, at: datetime | None = None) -> str:
    """Current UTC offset for a zone, e.g. ``UTC-04:00`` / ``UTC+05:30``."""
    try:
        zi = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return "UTC"
    moment = at or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    local = moment.astimezone(zi)
    offset = local.utcoffset() or timedelta(0)
    total = int(offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    hours, rem = divmod(total, 3600)
    minutes = rem // 60
    if minutes:
        return f"UTC{sign}{hours:02d}:{minutes:02d}"
    return f"UTC{sign}{hours:02d}:00"


@lru_cache(maxsize=1)
def _iana_names() -> tuple[str, ...]:
    """All IANA zone names (skips awkward Etc/* except UTC)."""
    names = set()
    for name in available_timezones():
        if name in ("UTC", "GMT"):
            names.add("UTC")
            continue
        if name.startswith("Etc/"):
            continue
        names.add(name)
    return tuple(sorted(names))


@lru_cache(maxsize=2)
def _tz_option_shapes(hour_bucket: int) -> tuple[tuple, ...]:
    """``(value, label, region)`` for every zone, built at most once an hour.

    Labelling ~600 zones means ~600 ZoneInfo lookups, which was the slowest
    thing on any Studio page carrying a timezone picker. The offsets only move
    at DST boundaries, so an hourly rebuild keeps them honest.
    """
    now = datetime.now(timezone.utc)
    names = list(_iana_names())
    if "UTC" not in names:
        names.insert(0, "UTC")
    out = []
    for name in names:
        city = name.split("/")[-1].replace("_", " ")
        region = name.split("/")[0] if "/" in name else "Other"
        out.append((
            name,
            f"{city} — {name} ({_offset_label(name, now)})",
            region if name != "UTC" else "UTC",
        ))
    return tuple(out)


def timezone_groups(*, selected: str | None = None) -> list[dict]:
    """Grouped timezone options for Studio selects, with live UTC offsets.

    Offsets are computed hourly so DST is reflected automatically; the
    underlying IANA IDs never change and stay correct across seasons.
    """
    selected_n = normalize_timezone(selected) or DEFAULT_TZ
    bucket = int(datetime.now(timezone.utc).timestamp()) // 3600
    shapes = _tz_option_shapes(bucket)
    by_name = {value: (value, label, region) for value, label, region in shapes}

    def option(row) -> dict:
        value, label, region = row
        return {"value": value, "label": label, "region": region,
                "selected": value == selected_n}

    common_opts = [option(by_name[n]) for n in _COMMON_TZ if n in by_name]
    # Ensure selected appears in Common if it's not already.
    if selected_n not in {o["value"] for o in common_opts} and selected_n in by_name:
        common_opts.insert(1, option(by_name[selected_n]))

    by_region: dict[str, list[dict]] = {}
    for row in shapes:
        opt = option(row)
        by_region.setdefault(opt["region"], []).append(opt)

    groups = [{"label": "Common", "options": common_opts}]
    for region in sorted(by_region.keys(), key=lambda r: (r != "UTC", r)):
        opts = by_region[region]
        opts.sort(key=lambda o: o["label"].casefold())
        groups.append({"label": region, "options": opts})
    return groups


def timezone_label(name: str | None) -> str:
    """Short display like ``America/New_York (UTC-04:00)``."""
    tz = normalize_timezone(name) or DEFAULT_TZ
    return f"{tz} ({_offset_label(tz)})"


# --- reading a date and time the owner typed on their own calendar --------

def parse_owner_local(dt_local: str, tz_name: str | None) -> datetime | None:
    """Parse ``YYYY-MM-DDTHH:MM`` from the owner's calendar in their timezone → UTC naive."""
    raw = (dt_local or "").strip()
    if not raw:
        return None
    try:
        if len(raw) == 16:
            local = datetime.strptime(raw, "%Y-%m-%dT%H:%M")
        else:
            local = datetime.fromisoformat(raw)
    except ValueError:
        return None
    tz = normalize_timezone(tz_name) or DEFAULT_TZ
    aware = local.replace(tzinfo=ZoneInfo(tz))
    return aware.astimezone(timezone.utc).replace(tzinfo=None)


def parse_owner_parts(date_s: str, time_s: str, tz_name: str | None) -> datetime | None:
    d = (date_s or "").strip()
    t = (time_s or "").strip()
    if not d or not t:
        return None
    if len(t) == 5:
        t = t + ":00"
    return parse_owner_local(f"{d}T{t[:8]}", tz_name)
