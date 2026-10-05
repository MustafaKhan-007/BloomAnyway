"""@username mentions, follows, and in-app notifications."""
import re

from markupsafe import Markup, escape
from sqlalchemy import func, or_

from ..extensions import db
from ..models import Follow, ForumPost, Notification, User, utcnow

# Letters, numbers, underscore only; 3–30 chars; must start with a letter.
USERNAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,29}$")
MENTION_RE = re.compile(r"(?<![\w@])@([a-zA-Z][a-zA-Z0-9_]{2,29})\b")

RESERVED_USERNAMES = frozenset({
    "admin", "administrator", "owner", "support", "help", "bloom",
    "bloomanyway", "mod", "moderator", "staff", "system", "root",
    "null", "undefined", "api", "www", "mail", "email", "me", "you",
    # "all" is the room-wide mention below. A member holding that handle
    # would make every @all ambiguous, and would be notified by name every
    # time an owner addressed the room.
    "all", "everyone", "here", "channel", "community", "official",
})

#: The one @ that isn't a person: it addresses the whole room. Only an owner
#: can send it — a room of several hundred is not something to hand to
#: everybody — and only inside a community room, where "the room" has a
#: meaning. Matched case-insensitively because nobody types @All on purpose
#: but plenty of phones capitalise it.
ALL_HANDLE = "all"
ALL_RE = re.compile(r"(?<![\w@])@all\b", re.I)


def normalize_username(raw: str) -> str:
    return (raw or "").strip().lstrip("@").lower()


def is_valid_username(raw: str) -> bool:
    handle = normalize_username(raw)
    if not USERNAME_RE.match(handle):
        return False
    if handle in RESERVED_USERNAMES:
        return False
    if "__" in handle:
        return False
    return True


def username_error(raw: str) -> str | None:
    """Human-readable validation error, or None if the handle is fine."""
    handle = normalize_username(raw)
    if not handle:
        return "Pick a username so people can tag you."
    if len(handle) < 3:
        return "Usernames need at least 3 characters."
    if len(handle) > 30:
        return "Usernames can be at most 30 characters."
    if not handle[0].isalpha():
        return "Usernames must start with a letter."
    if not re.match(r"^[a-z0-9_]+$", handle):
        return "Only letters, numbers, and underscores — no spaces or symbols."
    if "__" in handle:
        return "Skip double underscores — one at a time is plenty."
    if handle in RESERVED_USERNAMES:
        return "That username is reserved. Try another."
    if not USERNAME_RE.match(handle):
        return "That username isn't allowed. Try letters, numbers, and underscores."
    return None


def allocate_username(email: str, preferred: str | None = None) -> str:
    """Pick a unique username from preferred or the email local-part."""
    base = normalize_username(preferred or "")
    if not is_valid_username(base):
        local = (email or "member").split("@", 1)[0]
        base = re.sub(r"[^a-z0-9_]", "", local.lower())[:24] or "member"
        if base and not base[0].isalpha():
            base = "u" + base
        if len(base) < 3:
            base = (base + "user")[:3]
        if not is_valid_username(base):
            base = "member"
    candidate = base
    n = 0
    while (User.query.filter(func.lower(User.username) == candidate).first()
           or candidate in RESERVED_USERNAMES):
        n += 1
        suffix = str(n)
        candidate = f"{base[: 30 - len(suffix)]}{suffix}"
    return candidate


def ensure_usernames(limit: int = 300) -> int:
    """Allocate handles for members who still don't have one (e.g. pre-feature)."""
    missing = (User.query
               .filter(User.deleted_at.is_(None),
                       or_(User.username.is_(None), User.username == ""))
               .limit(limit)
               .all())
    for u in missing:
        u.username = allocate_username(u.email)
    if missing:
        db.session.commit()
    return len(missing)


def suggest_usernames(query: str, *, limit: int = 8, exclude_id: int | None = None):
    """Autocomplete matches for @mention typing.

    Matches @username prefix, and also display-name prefix so people can find
    someone by the name they know. Empty query returns a small starter list.
    """
    ensure_usernames()
    q = normalize_username(query)
    base = (
        User.deleted_at.is_(None),
        User.username.isnot(None),
        User.username != "",
    )
    if exclude_id:
        base = (*base, User.id != exclude_id)

    if not q:
        rows = (User.query.filter(*base)
                .order_by(User.username)
                .limit(limit)
                .all())
    else:
        uname = func.lower(User.username)
        dname = func.lower(func.coalesce(User.display_name, ""))
        rows = (User.query.filter(
                    *base,
                    or_(uname.startswith(q),
                        dname.startswith(q),
                        uname.contains(q)))
                .order_by(
                    # Prefix hits on the handle first, then everything else.
                    uname.startswith(q).desc(),
                    User.username,
                )
                .limit(limit)
                .all())

    return [{
        "username": u.username,
        "name": (u.display_name or "").strip(),
        "id": u.id,
    } for u in rows]


def mentions_everyone(text: str | None) -> bool:
    """Whether this writing addresses the whole room."""
    return bool(ALL_RE.search(text or ""))


def find_mentioned_users(text: str) -> list[User]:
    handles = {normalize_username(m) for m in MENTION_RE.findall(text or "")}
    # @all is the room, not a person. Reserved above, but a handle allocated
    # before it was reserved would still be sitting in the table.
    handles.discard(ALL_HANDLE)
    if not handles:
        return []
    users = (User.query
             .filter(func.lower(User.username).in_(handles), User.deleted_at.is_(None))
             .all())
    return users


def linkify_mentions(text: str, author: User | None = None) -> Markup:
    """Escape text, turn @handles into profile links, keep newlines as <br>.

    ``author`` is who wrote it, and decides one thing: whether an ``@all`` in
    here is drawn as the room-wide mention it is. Only an owner's counts, and
    marking up somebody else's would tell every reader that several hundred
    people had been rung when nobody had.
    """
    if not text:
        return Markup("")
    broadcast = can_mention_everyone(author)
    parts = []
    last = 0
    for m in MENTION_RE.finditer(text):
        parts.append(str(escape(text[last:m.start()])))
        handle = m.group(1)
        if handle.lower() == ALL_HANDLE:
            if broadcast:
                parts.append(
                    '<span class="mention mention--all" '
                    'title="Everyone who can read this room">'
                    f'@{escape(handle)}</span>')
            else:
                parts.append(str(escape(m.group(0))))
            last = m.end()
            continue
        user = (User.query
                .filter(func.lower(User.username) == handle.lower(),
                        User.deleted_at.is_(None))
                .first())
        if user:
            from flask import url_for
            href = url_for("main.profile", user_id=user.id)
            parts.append(
                f'<a class="mention" href="{href}">@{escape(user.username)}</a>'
            )
        else:
            parts.append(str(escape(m.group(0))))
        last = m.end()
    parts.append(str(escape(text[last:])))
    return Markup("".join(parts).replace("\n", "<br>\n"))


def is_following(follower: User, target: User) -> bool:
    if not follower or not target or follower.id == target.id:
        return False
    return Follow.query.filter_by(
        follower_id=follower.id, following_id=target.id).first() is not None


def follow_counts(user: User) -> tuple[int, int]:
    followers = Follow.query.filter_by(following_id=user.id).count()
    following = Follow.query.filter_by(follower_id=user.id).count()
    return followers, following


def toggle_follow(follower: User, target: User) -> bool:
    """Follow or unfollow. Returns True if now following."""
    if follower.id == target.id:
        return False
    row = Follow.query.filter_by(
        follower_id=follower.id, following_id=target.id).first()
    if row:
        db.session.delete(row)
        return False
    db.session.add(Follow(follower_id=follower.id, following_id=target.id))
    handle = f"@{follower.username}" if follower.username else follower.public_name()
    from flask import url_for
    try:
        profile_url = url_for("main.profile", user_id=follower.id)
    except RuntimeError:
        profile_url = f"/u/{follower.id}"
    notify(target.id, kind="followed",
           body=f"{handle} started following you",
           actor_id=follower.id, url=profile_url)
    return True


def notify(user_id: int, *, kind: str, body: str, actor_id: int | None = None,
           post_id: int | None = None, url: str | None = None):
    if user_id == actor_id:
        return
    db.session.add(Notification(
        user_id=user_id, actor_id=actor_id, kind=kind,
        post_id=post_id, url=(url or None),
        body=(body or "")[:300], created_at=utcnow(),
    ))


def notify_followers_of_post(author: User, post: ForumPost):
    if post.anonymous:
        return
    follower_ids = [f.follower_id for f in
                    Follow.query.filter_by(following_id=author.id).all()]
    handle = f"@{author.username}" if author.username else author.public_name()
    body = f"{handle} posted “{(post.title or '')[:80]}”"
    for fid in follower_ids:
        notify(fid, kind="follow_post", body=body,
               actor_id=author.id, post_id=post.id)


def notify_followers_of_listing(author: User, listing):
    """Tell followers when someone they follow lists in the Showcase."""
    if not listing or not getattr(listing, "active", True):
        return
    follower_ids = [f.follower_id for f in
                    Follow.query.filter_by(following_id=author.id).all()]
    if not follower_ids:
        return
    handle = f"@{author.username}" if author.username else author.public_name()
    title = (listing.title or "a listing")[:80]
    from flask import url_for
    try:
        href = url_for("main.listing_detail", listing_id=listing.id)
    except RuntimeError:
        href = f"/marketplace/l/{listing.id}"
    body = f"{handle} shared “{title}” in Showcase"
    for fid in follower_ids:
        notify(fid, kind="follow_listing", body=body,
               actor_id=author.id, url=href)


def notify_mentions(actor: User, text: str, post_id: int | None = None):
    handle = f"@{actor.username}" if actor.username else actor.public_name()
    for user in find_mentioned_users(text):
        if user.id == actor.id:
            continue
        notify(user.id, kind="mention", body=f"{handle} mentioned you",
               actor_id=actor.id, post_id=post_id)


def can_mention_everyone(user: User | None) -> bool:
    """Whether this account may address a whole room.

    Owners only. Everything else here is one person telling one person; this
    is one person ringing several hundred phones, and it only takes a couple
    of those a day before people stop reading any of them.
    """
    return bool(user is not None
                and getattr(user, "is_authenticated", True)
                and getattr(user, "is_admin", False))


def notify_room(actor: User, category, text: str,
                post_id: int | None = None) -> int:
    """Tell everyone who can read this room that it was addressed.

    Returns how many were reached, so the owner can be told — they are the
    actor, so they never get their own, and otherwise have no way of seeing
    that anything happened.

    Nothing is sent when the writer isn't an owner: a member typing @all is
    typing three characters, not sending anything.
    """
    from .forum_access import readers_of

    if not can_mention_everyone(actor) or not mentions_everyone(text):
        return 0
    if category is None:
        return 0
    handle = f"@{actor.username}" if actor.username else actor.public_name()
    room = (getattr(category, "name", None) or "the community").strip()
    sent = 0
    for user in readers_of(category).yield_per(200):
        if user.id == actor.id:
            continue
        notify(user.id, kind="mention",
               body=f"{handle} mentioned everyone in {room}",
               actor_id=actor.id, post_id=post_id)
        sent += 1
    return sent


def notify_everyone(*, kind: str, body: str, url: str | None = None,
                    actor_id: int | None = None, exclude_id: int | None = None) -> int:
    """Fan out a broadcast notification (Content Hub, new course, etc.).

    Returns how many people it reached, so the owner can be told — they are
    the actor, so they never receive their own broadcast and otherwise have
    no way of seeing that it went out.
    """
    # Stand-in accounts are nobody's inbox — skip the rows entirely.
    q = User.query.filter(User.deleted_at.is_(None), User.is_demo.is_(False))
    if exclude_id:
        q = q.filter(User.id != exclude_id)
    sent = 0
    for u in q.yield_per(200):
        if actor_id and u.id == actor_id:
            continue  # notify() drops these anyway; don't count them
        notify(u.id, kind=kind, body=body, url=url, actor_id=actor_id)
        sent += 1
    return sent


def notify_owners(*, kind: str, body: str, url: str | None = None,
                  actor_id: int | None = None):
    """Fan out a Studio notification to every active owner account."""
    owners = (User.query
              .filter(User.is_admin.is_(True), User.deleted_at.is_(None))
              .all())
    for owner in owners:
        notify(owner.id, kind=kind, body=body, url=url, actor_id=actor_id)


def unread_notification_count(user: User) -> int:
    if not user:
        return 0
    return Notification.query.filter_by(user_id=user.id, read_at=None).count()


def mark_notifications_read(user: User) -> int:
    """Mark every unread notification as read. Returns how many were updated."""
    if not user:
        return 0
    rows = (Notification.query
            .filter_by(user_id=user.id, read_at=None)
            .all())
    if not rows:
        return 0
    now = utcnow()
    for n in rows:
        n.read_at = now
    db.session.commit()
    return len(rows)


def recent_notifications(user: User, limit: int = 8) -> list:
    if not user:
        return []
    return (Notification.query.filter_by(user_id=user.id)
            .order_by(Notification.created_at.desc())
            .limit(limit).all())
