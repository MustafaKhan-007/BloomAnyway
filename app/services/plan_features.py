"""Per-plan membership feature toggles (Studio → sitewide gating + marketing)."""
from __future__ import annotations

import json
from typing import Any

# Non-free capabilities owners can turn on/off per paid plan.
FEATURE_DEFS: list[dict[str, Any]] = [
    {
        "key": "community_healing",
        "label": "Healing community",
        "help": "Read, post, and reply in Healing rooms.",
        "kind": "bool",
        "matrix": "Healing community",
        "perk": "Healing community — read, post & reply",
    },
    {
        "key": "community_building",
        "label": "Building / Creator community",
        "help": "Read, post, and reply in Building rooms.",
        "kind": "bool",
        "matrix": "Building / Creator community",
        "perk": "Building community — read, post & reply",
    },
    {
        "key": "content_hub_healing",
        "label": "Watch Healing Content Hub tips",
        "help": "Play tips marked for Healing members.",
        "kind": "bool",
        "matrix": None,  # folded into “Watch Content Hub tips”
        "perk": "Connect with women all over the world",
    },
    {
        "key": "content_hub_creator",
        "label": "Watch Creator Content Hub tips",
        "help": "Play Creator tips (and full Hub library).",
        "kind": "bool",
        "matrix": None,
        "perk": "Creator Content Hub tips",
    },
    {
        "key": "reel_reviews",
        "label": "Request a weekly reel review",
        "help": "Submit reels for founder review.",
        "kind": "bool",
        "matrix": "Request a weekly reel review",
        "perk": "Weekly reel review requests",
    },
    {
        "key": "spotlight",
        "label": "Home-page spotlight eligibility",
        "help": "Eligible for Creator of the Month / homepage features.",
        "kind": "bool",
        "matrix": "Home-page spotlight eligibility",
        "perk": "Home-page spotlight eligibility",
    },
    {
        "key": "showcase_listings",
        "label": "Active Showcase listings",
        "help": "How many live Showcase ads this plan may run at once.",
        "kind": "int",
        "min": 0,
        "max": 50,
        "matrix": "Showcase listings",
        "perk": None,  # built from the number
    },
    {
        "key": "support_healing",
        "label": "Healing support groups / Ayesha 1:1",
        "help": "Join healing circles and book Ayesha.",
        "kind": "bool",
        "matrix": "Healing support groups / Ayesha 1:1",
        "perk": "Healing peer support groups & Ayesha 1:1",
    },
    {
        "key": "support_creator",
        "label": "Creator support groups / Saman 1:1",
        "help": "Join creator circles and book Saman.",
        "kind": "bool",
        "matrix": "Creator support groups / Saman 1:1",
        "perk": "Creator support groups & Saman 1:1",
    },
    {
        "key": "profile_links",
        "label": "Profile links",
        "help": "Add external links on the public profile.",
        "kind": "bool",
        "matrix": "Profile links",
        "perk": "Profile links",
    },
    {
        "key": "journey_export",
        "label": "My Journey keepsake export",
        "help": "Download the Journey PDF from My space.",
        "kind": "bool",
        "matrix": "My Journey keepsake export",
        "perk": "My Journey keepsake export",
    },
]

FEATURE_KEYS = [f["key"] for f in FEATURE_DEFS]
_FEATURE_BY_KEY = {f["key"]: f for f in FEATURE_DEFS}

#: Defaults match the historical hard-coded tier behaviour.
DEFAULT_FEATURES: dict[str, dict[str, Any]] = {
    "none": {
        "community_healing": False,
        "community_building": False,
        "content_hub_healing": False,
        "content_hub_creator": False,
        "reel_reviews": False,
        "spotlight": False,
        "showcase_listings": 0,
        "support_healing": False,
        "support_creator": False,
        "profile_links": False,
        "journey_export": False,
    },
    "healing": {
        "community_healing": True,
        "community_building": False,
        "content_hub_healing": True,
        "content_hub_creator": False,
        "reel_reviews": False,
        "spotlight": False,
        "showcase_listings": 1,
        "support_healing": True,
        "support_creator": False,
        "profile_links": True,
        "journey_export": True,
    },
    "creator": {
        "community_healing": False,
        "community_building": True,
        "content_hub_healing": False,
        "content_hub_creator": True,
        "reel_reviews": True,
        "spotlight": True,
        "showcase_listings": 5,
        "support_healing": False,
        "support_creator": True,
        "profile_links": True,
        "journey_export": True,
    },
    "full_bloom": {
        "community_healing": True,
        "community_building": True,
        "content_hub_healing": True,
        "content_hub_creator": True,
        "reel_reviews": True,
        "spotlight": True,
        "showcase_listings": 5,
        "support_healing": True,
        "support_creator": True,
        "profile_links": True,
        "journey_export": True,
    },
}


def normalize_features(raw: dict | None, tier: str | None = None) -> dict[str, Any]:
    """Merge raw stored values onto defaults for ``tier`` (or empty defaults)."""
    base = dict(DEFAULT_FEATURES.get(tier or "", DEFAULT_FEATURES["none"]))
    if not isinstance(raw, dict):
        return base
    out = dict(base)
    for key, meta in _FEATURE_BY_KEY.items():
        if key not in raw:
            continue
        val = raw[key]
        if meta["kind"] == "int":
            try:
                n = int(val)
            except (TypeError, ValueError):
                continue
            lo = int(meta.get("min", 0))
            hi = int(meta.get("max", 50))
            out[key] = max(lo, min(hi, n))
        else:
            out[key] = bool(val)
    return out


def parse_features_json(text: str | None, tier: str | None = None) -> dict[str, Any]:
    raw = None
    if text:
        try:
            raw = json.loads(text)
        except (TypeError, ValueError):
            raw = None
    return normalize_features(raw if isinstance(raw, dict) else None, tier)


def features_to_json(features: dict[str, Any]) -> str:
    cleaned = normalize_features(features)
    return json.dumps(cleaned, sort_keys=True)


def feature_value(tier: str | None, key: str) -> Any:
    """Resolve a feature for a membership tier (reads DB plan when available)."""
    tier = (tier or "none").strip().lower() or "none"
    if key not in _FEATURE_BY_KEY:
        return DEFAULT_FEATURES.get(tier, DEFAULT_FEATURES["none"]).get(key)
    plan = _plan_for_tier(tier)
    if plan is not None:
        return plan.feature(key)
    return DEFAULT_FEATURES.get(tier, DEFAULT_FEATURES["none"]).get(key)


def feature_enabled(tier: str | None, key: str) -> bool:
    meta = _FEATURE_BY_KEY.get(key)
    if meta and meta["kind"] == "int":
        try:
            return int(feature_value(tier, key) or 0) > 0
        except (TypeError, ValueError):
            return False
    return bool(feature_value(tier, key))


def _plan_for_tier(tier: str):
    """The stored plan for a tier, looked up once per request.

    Every ``user.has_feature(...)`` call lands here, and pages that gate row by
    row were re-running the same query dozens of times. Plans only change from
    Studio, which is a different request, so caching for the life of this one
    is safe.
    """
    if tier in ("", "none"):
        return None
    try:
        from flask import g, has_app_context
        if not has_app_context():
            return None
        from ..models import MembershipPlan
        cache = getattr(g, "_membership_plan_cache", None)
        if cache is None:
            # There are only ever a handful of plans, and a page that gates by
            # tier asks about several — read them all in one go.
            cache = {p.tier: p for p in MembershipPlan.query.all()}
            g._membership_plan_cache = cache
        return cache.get(tier)
    except Exception:
        return None


def perk_labels(features: dict[str, Any]) -> list[str]:
    """Marketing bullets for a plan card from enabled features."""
    feats = normalize_features(features)
    labels: list[str] = []
    for meta in FEATURE_DEFS:
        key = meta["key"]
        if meta["kind"] == "int":
            n = int(feats.get(key) or 0)
            if n <= 0:
                continue
            labels.append(
                f"{n} Showcase listing{'s' if n != 1 else ''} at a time"
            )
            continue
        if feats.get(key) and meta.get("perk"):
            labels.append(meta["perk"])
    return labels


def build_membership_matrix(plans: dict) -> list[tuple]:
    """Comparison rows for /membership: (label, free, healing, creator, full)."""
    def feats(tier: str) -> dict[str, Any]:
        plan = plans.get(tier) if plans else None
        if plan is not None and hasattr(plan, "features"):
            return plan.features()
        return dict(DEFAULT_FEATURES.get(tier, DEFAULT_FEATURES["none"]))

    h, c, f = feats("healing"), feats("creator"), feats("full_bloom")

    def b(d: dict, key: str):
        return bool(d.get(key))

    def listings(d: dict):
        n = int(d.get("showcase_listings") or 0)
        if n <= 0:
            return False
        return f"{n} active"

    def watch_cell(d: dict):
        heal = b(d, "content_hub_healing")
        crea = b(d, "content_hub_creator")
        if crea and heal:
            return True
        if crea:
            return True
        if heal:
            return "Healing tips"
        return False

    rows: list[tuple] = [
        ("Buy courses & guides", True, True, True, True),
        ("Daily quotes & motivation", True, True, True, True),
        ("Earn & display badges", True, True, True, True),
        ("Browse the Content Hub", True, True, True, True),
        ("Watch Content Hub tips", "Free picks",
         watch_cell(h) or "Free picks",
         watch_cell(c) if watch_cell(c) else "Free picks",
         watch_cell(f) if watch_cell(f) else "Free picks"),
    ]
    # If paid tip access is True, show check; keep Free picks note only when no paid tips
    def watch_display(d: dict):
        cell = watch_cell(d)
        if cell is True:
            return True
        if cell:
            return cell
        return "Free picks"

    rows[4] = ("Watch Content Hub tips", "Free picks",
               watch_display(h), watch_display(c), watch_display(f))

    for meta in FEATURE_DEFS:
        label = meta.get("matrix")
        if not label:
            continue
        key = meta["key"]
        if meta["kind"] == "int":
            rows.append((label, False, listings(h), listings(c), listings(f)))
        else:
            rows.append((label, False, b(h, key), b(c, key), b(f, key)))
    return rows
