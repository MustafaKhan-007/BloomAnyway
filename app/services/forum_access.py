"""Who can read which community room — asked about people other than the viewer.

``forums/routes._can_access_category`` already answers this for whoever is
looking at the page, through ``current_user.has_feature``. That one is
preview-aware on purpose: an owner looking around as a Free member should be
shut out of the rooms a Free member is shut out of.

This answers the same question about *other* people, which is a different
question and must not go near the session. ``User.effective_membership``
reads the owner-preview out of the session, so asking it about three hundred
accounts during one request would quietly hand every one of them whatever
tier the person looking at the page is pretending to be.

Used by the ``@all`` mention, which is the first thing here that has ever
needed to know who else is in the room.
"""
from __future__ import annotations

from sqlalchemy import or_

from ..models import User
from .plan_features import feature_enabled

#: Tiers that are "a member" at all. Mirrors ``User.is_member``.
MEMBER_TIERS = ("healing", "creator", "full_bloom")

#: Which plan feature each room is gated on. A room not on this list is open
#: to every member, which is what ``_can_access_category`` falls through to.
ROOM_FEATURE = {
    "building": "community_building",
    "healing": "community_healing",
}


def tier_of(user: User) -> str:
    """The plan this account actually has, with no preview layer over it.

    An owner ranks as Full Bloom here the same way they do everywhere else —
    they can read every room — but it is read off ``is_admin`` rather than
    out of the session.
    """
    if user is None:
        return "none"
    if getattr(user, "is_admin", False):
        return "full_bloom"
    return (user.membership or "none")


def room_feature(category) -> str | None:
    """The feature key this room is gated on, or None if it is open."""
    slug = (getattr(category, "slug", None) or "").lower()
    return ROOM_FEATURE.get(slug)


def can_read(user: User, category) -> bool:
    """Whether this account may read this room."""
    if user is None or getattr(user, "deleted_at", None) is not None:
        return False
    if getattr(user, "is_admin", False):
        return True
    tier = tier_of(user)
    if tier not in MEMBER_TIERS:
        return False
    key = room_feature(category)
    if key is None:
        return True
    return feature_enabled(tier, key)


def tiers_that_can_read(category) -> set[str]:
    """Every tier whose plan opens this room.

    Worked out once and turned into a single ``IN`` below, rather than asking
    the question per account: the plans come out of the settings table and
    the answer is the same for everybody on a tier.
    """
    key = room_feature(category)
    if key is None:
        return set(MEMBER_TIERS)
    return {tier for tier in MEMBER_TIERS if feature_enabled(tier, key)}


def readers_of(category):
    """A query for every live account that can read this room.

    Owners are always in it — they can read everything — and stand-in
    accounts never are, because they are nobody's inbox.
    """
    tiers = tiers_that_can_read(category)
    reachable = User.is_admin.is_(True)
    if tiers:
        reachable = or_(reachable, User.membership.in_(sorted(tiers)))
    return (User.query
            .filter(User.deleted_at.is_(None),
                    User.is_demo.is_(False),
                    reachable))
