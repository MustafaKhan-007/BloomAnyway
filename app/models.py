"""All SQLAlchemy models."""
import json
import random
from datetime import date, datetime, timedelta, timezone

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db

#: number of profanity warnings allowed before a forum ban (the ban lands on
#: the next offense after this many warnings)
FORUM_WARNING_LIMIT = 2

#: Letters that still read as words once a paragraph is blurred out.
_SHAPE_LETTERS = "aacdeeghiilmnoorrsttu"


def blurred_shape(text: str, *, seed: int = 0, limit: int = 420) -> str:
    """Filler shaped like the writing behind a paywall, carrying none of it.

    Blurring the real words in CSS would still ship them: they sit in the page
    source for anyone who opens it, and reader modes strip the blur outright.
    This keeps the rhythm of the paragraph — the word lengths, the shape of it
    — so it reads as writing you haven't unlocked rather than a grey box, while
    the words themselves never leave the server.
    """
    words = (text or "").split()
    if not words:
        return ""
    # Seeded so it doesn't reshuffle on every page load.
    rng = random.Random(seed or len(words))
    out: list[str] = []
    used = 0
    for word in words:
        size = min(len(word), 14)
        out.append("".join(rng.choice(_SHAPE_LETTERS) for _ in range(size)))
        used += size + 1
        if used >= limit:
            break
    return " ".join(out)


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --- constants (kept as plain strings for SQLite/Postgres portability) ------
PRODUCT_TYPES = ("course", "guide")

#: What a product can be, in the order the pills read best. A product can be
#: several of these at once — a course that ships templates, say — so the first
#: one it holds is the "primary", the one a card has room to show.
PRODUCT_KINDS: tuple[tuple[str, str], ...] = (
    ("workbook", "WORKBOOK"),
    ("course", "COURSE"),
    ("guide", "GUIDE"),
    ("audio", "AUDIO GUIDE"),
    ("template", "TEMPLATE"),
    ("bundle", "BUNDLE"),
)
PRODUCT_KIND_KEYS = tuple(key for key, _ in PRODUCT_KINDS)
PRODUCT_KIND_PILLS = dict(PRODUCT_KINDS)
PRODUCT_STATUSES = ("draft", "published", "archived")
QUOTE_CATEGORIES = ("comfort", "determination", "renewal")

#: membership tiers. "none" = free (quotes, shop, Content Hub free picks);
#: "healing" = healing community, 1 showcase listing, healing support / Ayesha;
#: "creator" = building community, 5 listings, tips, spotlight, reels, Saman;
#: "full_bloom" = everything in Healing and Creator.
MEMBERSHIPS = ("none", "healing", "creator", "full_bloom")
MEMBERSHIP_LABELS = {
    "none": "Free",
    "healing": "Healing",
    "creator": "Creator",
    "full_bloom": "Full Bloom",
}
#: ordering for upgrades. Healing and Creator are parallel halves;
#: Full Bloom sits above both. Owning both halves upgrades to Full Bloom.
MEMBERSHIP_RANK = {"none": 0, "healing": 1, "creator": 1, "full_bloom": 2}


def higher_membership(a: str, b: str) -> str:
    """Return the better tier of two. Healing + Creator → Full Bloom."""
    a, b = a or "none", b or "none"
    pair = {a, b}
    if "full_bloom" in pair:
        return "full_bloom"
    if "healing" in pair and "creator" in pair:
        return "full_bloom"
    return a if MEMBERSHIP_RANK.get(a, 0) >= MEMBERSHIP_RANK.get(b, 0) else b

#: subjects a course/guide can be filed under (owner picks one; drives the
#: filter tabs on the catalogue).
PRODUCT_SUBJECTS = (
    "Healing", "Confidence", "Relationships", "Parenting", "Money",
    "Creativity", "Content Creation", "Productivity", "Mindfulness", "Career",
)


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255))
    email_verified_at = db.Column(db.DateTime)
    display_name = db.Column(db.String(80))
    username = db.Column(db.String(30), unique=True, index=True)  # @handle for tags
    is_admin = db.Column(db.Boolean, nullable=False, default=False)
    # Studio access without edit rights (view-only co-owner / observer).
    admin_readonly = db.Column(db.Boolean, nullable=False, default=False)
    # Hand-made in Studio to fill out a quiet room. Looks like any other
    # account from the outside; owners see it flagged, and it is kept out of
    # anything that emails people, counts members, or talks to Stripe.
    is_demo = db.Column(db.Boolean, nullable=False, default=False, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    last_login_at = db.Column(db.DateTime)
    deleted_at = db.Column(db.DateTime)

    # profile / personalization
    avatar_url = db.Column(db.String(500))   # legacy external URL (still shown if set)
    # Avatar bytes are deferred: a members list or a forum page reads dozens of
    # user rows and never needs the images, only the URL to fetch them from.
    avatar_data = db.deferred(db.Column(db.LargeBinary))  # JPEG still
    avatar_mime = db.Column(db.String(40))
    avatar_anim_data = db.deferred(db.Column(db.LargeBinary))  # optional GIF
    avatar_anim_mime = db.Column(db.String(40))
    bio = db.Column(db.String(400))
    links_json = db.Column(db.Text)          # JSON list of {"label","url"}
    goals_json = db.Column(db.Text)          # JSON list of intent keys
    default_anonymous = db.Column(db.Boolean, nullable=False, default=False)
    timezone = db.Column(db.String(64))      # IANA tz from browser, e.g. Europe/Berlin

    # forum moderation
    forum_warnings = db.Column(db.Integer, nullable=False, default=0)
    forum_banned = db.Column(db.Boolean, nullable=False, default=False)

    # membership tier: none / healing / creator / full_bloom (owner-assigned)
    membership = db.Column(db.String(20), nullable=False, default="none")
    # When set, Stripe cancel-at-period-end is scheduled; access until this UTC time.
    membership_cancel_at = db.Column(db.DateTime)
    # Tier set by hand in Studio → Members. Wins over Stripe/orders until the
    # member pays for a membership again.
    membership_manual = db.Column(db.String(20))
    # When that Studio choice was made, so a webhook replayed for an older
    # payment can't undo it.
    membership_manual_at = db.Column(db.DateTime)

    # showing-up streak ("I showed up today")
    last_checkin_date = db.Column(db.Date)
    current_streak = db.Column(db.Integer, nullable=False, default=0)
    longest_streak = db.Column(db.Integer, nullable=False, default=0)
    total_checkins = db.Column(db.Integer, nullable=False, default=0)

    # up to 3 badge category keys the member chose to feature on their profile
    displayed_badges_json = db.Column(db.Text)

    # legacy signup-tour column (tour removed; column kept for existing DBs)
    tour_completed_at = db.Column(db.DateTime)

    codes = db.relationship("VerificationCode", backref="user", lazy="dynamic",
                            cascade="all, delete-orphan")
    favorites = db.relationship("QuoteFavorite", backref="user", lazy="dynamic",
                                cascade="all, delete-orphan")

    @property
    def is_active(self):  # Flask-Login: soft-deleted users cannot log in
        return self.deleted_at is None

    @property
    def is_verified(self):
        return self.email_verified_at is not None

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, password)

    def first_name(self):
        if self.display_name:
            return self.display_name.split()[0]
        return None

    def public_name(self):
        return self.display_name or (f"@{self.username}" if self.username else "Member")

    def at_handle(self) -> str:
        return f"@{self.username}" if self.username else ""

    def initials(self):
        base = (self.display_name or self.username or self.email or "?").strip()
        parts = base.split()
        if len(parts) >= 2:
            return (parts[0][0] + parts[1][0]).upper()
        return base[0].upper()

    def has_avatar(self) -> bool:
        # Checks the mime, not the bytes — the bytes are deferred, and asking
        # for them here would load every avatar on a page full of members.
        return bool(self.avatar_mime)

    def has_animated_avatar(self) -> bool:
        return bool(self.avatar_anim_mime)

    def is_owner(self) -> bool:
        """True for any Studio owner (full or view-only co-owner)."""
        return bool(self.is_admin)

    # --- membership tiers ---------------------------------------------------
    def preview_tier(self) -> str:
        """Tier this owner is browsing as, or "" when they are not previewing."""
        if not self.is_owner():
            return ""
        from .services.preview import preview_tier
        return preview_tier(self)

    def effective_membership(self) -> str:
        """The tier used for gating. Owner always ranks as Full Bloom."""
        preview = self.preview_tier()
        if preview:
            return preview
        if self.is_owner():
            return "full_bloom"
        return self.membership or "none"

    def is_healing_track(self) -> bool:
        """Healing community, healing tips, healing circles / Ayesha."""
        return self.effective_membership() in ("healing", "full_bloom")

    def is_creator_track(self) -> bool:
        """Building community, tips, spotlight, reels, creator circles / Saman."""
        return self.effective_membership() in ("creator", "full_bloom")

    def is_creator(self) -> bool:
        """Creator-track perks (Creator, Full Bloom, or owner)."""
        return self.is_creator_track()

    def is_member(self) -> bool:
        """Any paid membership (or owner)."""
        return self.effective_membership() in ("healing", "creator", "full_bloom")

    def membership_is_canceling(self) -> bool:
        """True when cancel-at-period-end is scheduled and access still remains."""
        ends = self.membership_cancel_at
        if ends is None or self.effective_membership() == "none":
            return False
        return ends >= utcnow()

    def membership_access_end_display(self) -> str:
        """Human date for canceling memberships, or empty."""
        if not self.membership_is_canceling():
            return ""
        try:
            return self.membership_cancel_at.strftime("%b %d, %Y")
        except Exception:
            return ""

    def has_feature(self, key: str) -> bool:
        """True when this user's plan includes a Studio-toggled capability."""
        from .services.plan_features import feature_enabled
        if self.is_admin and not self.preview_tier():
            return True
        return feature_enabled(self.effective_membership(), key)

    def feature_int(self, key: str, default: int = 0) -> int:
        from .services.plan_features import feature_value
        try:
            return int(feature_value(self.effective_membership(), key) or default)
        except (TypeError, ValueError):
            return default

    def membership_label(self) -> str:
        if self.is_admin and not self.preview_tier():
            return "Owner"
        return MEMBERSHIP_LABELS.get(self.effective_membership(), "Free")

    def goals(self) -> list:
        try:
            return json.loads(self.goals_json) if self.goals_json else []
        except ValueError:
            return []

    def set_goals(self, keys) -> None:
        self.goals_json = json.dumps(list(keys)) if keys else None

    def links(self) -> list:
        try:
            return json.loads(self.links_json) if self.links_json else []
        except ValueError:
            return []

    def set_links(self, links) -> None:
        self.links_json = json.dumps(list(links)) if links else None

    def displayed_badges(self) -> list:
        try:
            return json.loads(self.displayed_badges_json) if self.displayed_badges_json else []
        except ValueError:
            return []

    def set_displayed_badges(self, keys) -> None:
        self.displayed_badges_json = json.dumps(list(keys)[:3]) if keys else None

    def check_in(self) -> bool:
        """Record 'I showed up today'. Returns True if this was a new check-in."""
        today = date.today()
        if self.last_checkin_date == today:
            return False
        if self.last_checkin_date == today - timedelta(days=1):
            self.current_streak = (self.current_streak or 0) + 1
        else:
            self.current_streak = 1
        self.last_checkin_date = today
        self.total_checkins = (self.total_checkins or 0) + 1
        self.longest_streak = max(self.longest_streak or 0, self.current_streak)
        # per-day log so a "My Journey" export can show real history
        db.session.add(CheckIn(user_id=self.id, day=today))
        return True

    def checked_in_today(self) -> bool:
        return self.last_checkin_date == date.today()

    def streak_display(self) -> int:
        """Current streak, but shown as 0 if it lapsed (missed yesterday+today)."""
        if self.last_checkin_date is None:
            return 0
        if self.last_checkin_date >= date.today() - timedelta(days=1):
            return self.current_streak or 0
        return 0


class VerificationCode(db.Model):
    """One-time 6-digit email codes (account confirmation / password reset).

    Only the SHA-256 hash of the code is stored. Codes are single-use,
    expire after 15 minutes, and allow at most 5 wrong attempts.
    """
    __tablename__ = "verification_codes"

    PURPOSES = ("confirm", "reset")
    MAX_ATTEMPTS = 5

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    code_hash = db.Column(db.String(64), nullable=False)
    purpose = db.Column(db.String(10), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    used_at = db.Column(db.DateTime)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    request_ip = db.Column(db.String(45))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    def is_usable(self) -> bool:
        return (self.used_at is None
                and self.expires_at > utcnow()
                and self.attempts < self.MAX_ATTEMPTS)


class Product(db.Model):
    __tablename__ = "products"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(160), nullable=False)
    slug = db.Column(db.String(160), unique=True, nullable=False)
    type = db.Column(db.String(20), nullable=False, default="course")
    #: JSON list of every kind this is, when it is more than one. ``type`` above
    #: stays the primary so single-kind callers need to know nothing about this.
    types_json = db.Column(db.Text)
    subject = db.Column(db.String(60))   # filterable catalogue subject
    status = db.Column(db.String(20), nullable=False, default="draft")
    # Test mode: a real, buyable product that only owners can see, so the
    # whole checkout can be walked through without shoppers stumbling on it.
    test_mode = db.Column(db.Boolean, nullable=False, default=False, index=True)
    featured = db.Column(db.Boolean, nullable=False, default=False)
    badge = db.Column(db.String(30))
    sort_order = db.Column(db.Integer, nullable=False, default=0)

    promise = db.Column(db.String(120))
    description_md = db.Column(db.Text)
    audience = db.Column(db.Text)          # "Who this is for"
    contents_text = db.Column(db.Text)     # one item per line -> check-list
    curriculum_json = db.Column(db.Text)   # JSON: [{title, description}]

    cover_url = db.Column(db.String(500))
    # Deferred: catalogue pages read every product and link to the cover route.
    cover_data = db.deferred(db.Column(db.LargeBinary))  # JPEG (survives redeploys)
    cover_mime = db.Column(db.String(40))
    gallery_json = db.Column(db.Text)      # JSON: [url, ...]

    price_cents = db.Column(db.Integer)
    compare_at_cents = db.Column(db.Integer)
    #: A running promo: what it costs with the code, and the code to type at
    #: Stripe checkout. Both or neither — a price with no code is a mystery and
    #: a code with no price is nothing to advertise.
    promo_price_cents = db.Column(db.Integer)
    promo_code = db.Column(db.String(40))
    #: When the sale stops, stored UTC. None means it runs until taken down.
    promo_ends_at = db.Column(db.DateTime)
    currency = db.Column(db.String(3), nullable=False, default="USD")
    ls_checkout_url = db.Column(db.String(500))  # legacy; unused
    ls_variant_id = db.Column(db.String(40), index=True)  # legacy; unused
    stripe_price_id = db.Column(db.String(80), index=True)
    # healing | building — Courses & Guides lanes
    track = db.Column(db.String(20), index=True)
    meta_line = db.Column(db.String(200))  # e.g. "80 daily pages • PDF + printable"
    category_label = db.Column(db.String(80))  # e.g. "DIVORCE RECOVERY"
    # Optional card accent (#RRGGBB). Empty → track default (plum / gold).
    accent_color = db.Column(db.String(7))

    meta_title = db.Column(db.String(160))
    meta_description = db.Column(db.String(200))

    # Drip-feed: hand out the modules below one at a time, each unlocking
    # ``drip_interval_days`` after the one before it (counted from purchase).
    drip_enabled = db.Column(db.Boolean, nullable=False, default=False)
    drip_interval_days = db.Column(db.Integer, nullable=False, default=7)

    # Additional perk: buying this also grants a membership tier, free, for
    # ``perk_membership_months`` months.
    perk_membership_tier = db.Column(db.String(20))
    perk_membership_months = db.Column(db.Integer, nullable=False, default=0)

    # hidden recommendation tags (never shown to customers)
    tags_json = db.Column(db.Text)

    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    orders = db.relationship("Order", backref="product", lazy="dynamic")
    testimonials = db.relationship("Testimonial", backref="product", lazy="dynamic")
    assets = db.relationship("ProductAsset", backref="product", lazy="select",
                             order_by="ProductAsset.sort_order, ProductAsset.id",
                             cascade="all, delete-orphan")
    gallery_images = db.relationship(
        "ProductGalleryImage", backref="product", lazy="select",
        order_by="ProductGalleryImage.sort_order, ProductGalleryImage.id",
        cascade="all, delete-orphan",
    )

    def has_assets(self) -> bool:
        return len(self.assets) > 0

    def top_level_assets(self) -> list["ProductAsset"]:
        """Everything a buyer picks from directly.

        ``assets`` is the complete set, notes included, which is what deletion
        and disk cleanup want. Anywhere a list is shown, the notes belong with
        the file they were written for rather than beside it.
        """
        return [a for a in self.assets if a.parent_asset_id is None]

    def accent_hex(self) -> str | None:
        raw = (self.accent_color or "").strip()
        if len(raw) == 7 and raw.startswith("#"):
            try:
                int(raw[1:], 16)
                return raw.upper()
            except ValueError:
                return None
        return None

    @staticmethod
    def _shade(hex_color: str, factor: float) -> str:
        h = hex_color.lstrip("#")
        r = max(0, min(255, int(int(h[0:2], 16) * factor)))
        g = max(0, min(255, int(int(h[2:4], 16) * factor)))
        b = max(0, min(255, int(int(h[4:6], 16) * factor)))
        return f"#{r:02X}{g:02X}{b:02X}"

    def card_style(self) -> str:
        """Inline CSS variables for a custom card accent (empty if unset)."""
        accent = self.accent_hex()
        if not accent:
            return ""
        deep = self._shade(accent, 0.62)
        return f"--cg-accent:{accent};--cg-accent-deep:{deep};"

    def tags(self) -> list:
        try:
            return json.loads(self.tags_json) if self.tags_json else []
        except ValueError:
            return []

    def set_tags(self, tags) -> None:
        cleaned = [t.strip().lower() for t in tags if t.strip()]
        self.tags_json = json.dumps(cleaned) if cleaned else None

    def gallery(self) -> list[str]:
        try:
            raw = json.loads(self.gallery_json) if self.gallery_json else []
        except ValueError:
            return []
        if not isinstance(raw, list):
            return []
        out = []
        for u in raw:
            s = str(u or "").strip()
            if s:
                out.append(s[:500])
        return out

    def set_gallery(self, urls) -> None:
        cleaned = []
        for u in urls or []:
            s = str(u or "").strip()
            if s:
                cleaned.append(s[:500])
        self.gallery_json = json.dumps(cleaned) if cleaned else None

    def contents_list(self) -> list[str]:
        text = (self.contents_text or "").strip()
        if not text:
            return []
        return [ln.strip() for ln in text.splitlines() if ln.strip()][:40]

    @staticmethod
    def _clean_lessons(raw) -> list[dict]:
        """Lesson rows within a module: a title and an optional short note."""
        out = []
        for row in raw or []:
            if not isinstance(row, dict):
                continue
            title = str(row.get("title") or "").strip()
            if not title:
                continue
            out.append({
                "title": title[:160],
                "description": str(row.get("description") or "").strip()[:500],
            })
        return out

    def curriculum(self) -> list[dict]:
        try:
            raw = json.loads(self.curriculum_json) if self.curriculum_json else []
        except ValueError:
            return []
        if not isinstance(raw, list):
            return []
        out = []
        for row in raw:
            if not isinstance(row, dict):
                continue
            title = str(row.get("title") or "").strip()
            if not title:
                continue
            out.append({
                "title": title[:160],
                "description": str(row.get("description") or "").strip()[:500],
                "lessons": self._clean_lessons(row.get("lessons")),
            })
        return out

    def set_curriculum(self, rows) -> None:
        cleaned = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            title = str(row.get("title") or "").strip()
            if not title:
                continue
            cleaned.append({
                "title": title[:160],
                "description": str(row.get("description") or "").strip()[:500],
                "lessons": self._clean_lessons(row.get("lessons")),
            })
        self.curriculum_json = json.dumps(cleaned) if cleaned else None

    # --- modules, drip-feed and the membership perk ---------------------------
    def drip_days(self) -> int:
        """Days between module releases (1–365)."""
        try:
            days = int(self.drip_interval_days or 0)
        except (TypeError, ValueError):
            days = 0
        return max(1, min(365, days or 7))

    def module_items(self) -> dict[int, list["ProductAsset"]]:
        """Module number → everything in it, in the order it should be worked
        through. A module can hold any mix of videos, documents and written
        extracts.
        """
        out: dict[int, list[ProductAsset]] = {}
        for asset in self.top_level_assets():
            number = asset.module_index
            if number:
                out.setdefault(number, []).append(asset)
        return out

    def module_files(self) -> dict[int, "ProductAsset"]:
        """Module number → its first item. Kept for callers that only want one."""
        return {n: items[0] for n, items in self.module_items().items() if items}

    def modules(self) -> list[dict]:
        """Curriculum rows paired with their contents, numbered from 1.

        ``asset`` is the first item, which is what a caller wanting a single
        thing to show should use; ``contents`` is the whole list. It is not
        called "items" because Jinja would resolve that to the dict method.
        """
        by_number = self.module_items()
        rows = []
        for i, row in enumerate(self.curriculum(), start=1):
            contents = by_number.get(i, [])
            # Module-level content (no lesson) is the module intro; it shows
            # before the lessons in both the reader and the store page.
            intro = [a for a in contents if not getattr(a, "lesson_index", None)]
            lessons = []
            for li, meta in enumerate(row.get("lessons") or [], start=1):
                litems = [a for a in contents
                          if getattr(a, "lesson_index", None) == li]
                lessons.append({
                    "number": li,
                    "title": meta["title"],
                    "description": meta["description"],
                    "contents": litems,
                })
            rows.append({
                "number": i,
                "title": row["title"],
                "description": row["description"],
                "contents": contents,
                "intro": intro,
                "lessons": lessons,
                "asset": contents[0] if contents else None,
            })
        return rows

    def module_summaries(self) -> list[str]:
        """One phrase per curriculum row: "1 video, 2 documents, written notes".

        Counts only, never a file or extract title — the store page says how
        much is inside without giving the inside away.
        """
        groups = (
            ("video", ("video",)),
            ("audio track", ("audio",)),
            ("document", ("pdf", "doc", "docx", "html", "other")),
            ("interactive piece", ("h5p",)),
            ("image", ("image",)),
        )
        # Worked out from the loaded collection rather than by asking each
        # file for its notes, which would be a query per row of the page.
        noted = {a.parent_asset_id for a in self.assets if a.parent_asset_id}
        out = []
        for row in self.modules():
            contents = row["contents"]
            parts = []
            for label, kinds in groups:
                n = sum(1 for a in contents if a.kind in kinds)
                if n:
                    parts.append(f"{n} {label}" + ("" if n == 1 else "s"))
            if any(a.kind == "text" or a.id in noted for a in contents):
                parts.append("written notes")
            out.append(", ".join(parts))
        return out

    def is_dripped(self) -> bool:
        """Drip-feed only kicks in once there is more than one module."""
        return bool(self.drip_enabled) and len(self.curriculum()) > 1

    def visible_to(self, user) -> bool:
        """Can this account see the product at all? Test items are owners-only.

        Says nothing about whether they already bought it — people who bought
        a product before it was put in test mode keep reading it either way.
        """
        if not self.test_mode:
            return True
        return bool(getattr(user, "is_authenticated", False)
                    and getattr(user, "is_admin", False))

    def buyable_by(self, user) -> bool:
        """Only live products sell, and test ones only sell to owners."""
        return self.status == "published" and self.visible_to(user)

    def perk_tier(self) -> str:
        tier = (self.perk_membership_tier or "").strip().lower()
        return tier if tier in ("healing", "creator", "full_bloom") else ""

    def perk_months(self) -> int:
        try:
            months = int(self.perk_membership_months or 0)
        except (TypeError, ValueError):
            return 0
        return max(0, min(60, months))

    def has_perk(self) -> bool:
        return bool(self.perk_tier()) and self.perk_months() > 0

    def perk_summary(self) -> str:
        """e.g. "3 months of Creator membership, free" (empty when unset)."""
        if not self.has_perk():
            return ""
        months = self.perk_months()
        label = MEMBERSHIP_LABELS.get(self.perk_tier(), self.perk_tier())
        unit = "month" if months == 1 else "months"
        return f"{months} {unit} of {label} membership, free"

    def price_display(self):
        return self._money_display(self.price_cents)

    # --- a running promo code -------------------------------------------------
    def has_promo(self) -> bool:
        """A promo needs a code, a price that beats the normal one, and time left."""
        return bool(
            (self.promo_code or "").strip()
            and self.promo_price_cents is not None
            and self.price_cents is not None
            and 0 <= self.promo_price_cents < self.price_cents
            and not self.promo_expired()
        )

    def promo_expired(self) -> bool:
        """Past its deadline. No deadline set means it runs until taken down."""
        return bool(self.promo_ends_at and self.promo_ends_at <= utcnow())

    def promo_ends_display(self) -> str:
        """The deadline in the reader's own timezone, empty when open-ended."""
        if not self.promo_ends_at or not self.has_promo():
            return ""
        from .services.timefmt import format_local
        return format_local(self.promo_ends_at, "%b %d at %I:%M %p")

    def promo_display(self) -> str:
        if not self.has_promo():
            return ""
        return self._money_display(self.promo_price_cents)

    def promo_code_display(self) -> str:
        return (self.promo_code or "").strip().upper()

    def promo_saving_display(self) -> str:
        """How much comes off, in money — "save $12"."""
        if not self.has_promo():
            return ""
        return self._money_display(self.price_cents - self.promo_price_cents)

    def _money_display(self, cents) -> str:
        if cents is None:
            return ""
        symbol = {"USD": "$", "EUR": "\u20ac",
                  "GBP": "\u00a3"}.get(self.currency, self.currency + " ")
        amount = cents / 100
        return (f"{symbol}{amount:,.0f}" if cents % 100 == 0
                else f"{symbol}{amount:,.2f}")

    def compare_at_display(self):
        return self._money_display(self.compare_at_cents)

    # --- what this product is ------------------------------------------------
    # ``type`` holds the primary kind and is what cards, filters and the bundle
    # lookup read. ``types_json`` holds the whole set for products that are more
    # than one thing, so nothing that only understands a single type breaks.

    def types(self) -> list[str]:
        """Every kind this product is, primary first. Never empty."""
        try:
            raw = json.loads(self.types_json) if self.types_json else []
        except ValueError:
            raw = []
        out = []
        for value in raw if isinstance(raw, list) else []:
            key = str(value or "").strip().lower()
            if key in PRODUCT_KIND_KEYS and key not in out:
                out.append(key)
        primary = (self.type or "").strip().lower()
        if primary and primary not in out:
            out.insert(0, primary)
        return out or [primary or "guide"]

    def set_types(self, values) -> None:
        """Store the kinds this product is; the first becomes the primary."""
        cleaned = []
        for value in values or []:
            key = str(value or "").strip().lower()
            if key in PRODUCT_KIND_KEYS and key not in cleaned:
                cleaned.append(key)
        if not cleaned:
            return
        # Canonical order, so "primary" doesn't depend on tick order.
        cleaned.sort(key=PRODUCT_KIND_KEYS.index)
        self.type = cleaned[0]
        self.types_json = json.dumps(cleaned) if len(cleaned) > 1 else None

    def has_type(self, key: str) -> bool:
        return (key or "").strip().lower() in self.types()

    def type_pills(self) -> list[str]:
        """One pill per kind, for the places with room to show all of them."""
        return [PRODUCT_KIND_PILLS.get(k, k.upper()) for k in self.types()]

    def types_display(self) -> str:
        """e.g. "Course, Templates" — plain reading order, primary first."""
        return ", ".join(pill.title() for pill in self.type_pills())

    def type_label(self):
        return "Course" if self.type == "course" else "Notebook Guide"

    def publish_blockers(self):
        """List of human-readable requirements missing before publishing."""
        missing = []
        if not (self.promise or "").strip():
            missing.append("a one-line promise")
        if self.price_cents is None:
            missing.append("a price")
        if not (self.stripe_price_id or "").strip():
            missing.append("the Stripe price ID")
        return missing

    def cover_color(self) -> str:
        """Solid colour for the default flower cover."""
        accent = self.accent_hex()
        if accent:
            return accent
        if (self.track or "").strip() == "healing":
            return "#5A3158"
        return "#C4A574"

    def kind_short(self) -> str:
        first = self.top_level_assets()
        asset = first[0] if first else None
        kind = (asset.kind if asset else "") or ""
        if kind == "pdf":
            return "PDF"
        if kind == "h5p":
            return "H5P"
        if kind == "audio":
            return "AUDIO"
        if kind in ("doc", "docx"):
            return "DOC"
        if kind:
            return kind.upper()
        return self.type_pill()

    def type_pill(self) -> str:
        """The primary kind, for the one-badge places."""
        key = (self.type or "").lower()
        return PRODUCT_KIND_PILLS.get(key, (self.type or "GUIDE").upper())


class ProductAsset(db.Model):
    """One piece of a course: a video, a document, or a written extract.

    Bytes live in one of three places. Anything uploaded now is streamed to the
    media disk and referenced by ``disk_name``, which is what makes room for
    hour-long videos. Written extracts have no file at all — the words sit in
    ``body``. ``data`` is the old in-database blob, kept so files uploaded
    before the move still open.
    """
    __tablename__ = "product_assets"

    KINDS = ("pdf", "h5p", "image", "video", "audio", "text", "html",
             "doc", "docx", "other")

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False, index=True)
    title = db.Column(db.String(160))
    filename = db.Column(db.String(255), nullable=False)
    mime = db.Column(db.String(120), nullable=False)
    kind = db.Column(db.String(20), nullable=False)   # pdf / h5p / image / …
    size = db.Column(db.Integer, nullable=False, default=0)
    # Deferred: course readers list every file and download one at a time.
    data = db.deferred(db.Column(db.LargeBinary))
    #: File on the course media disk. Set for everything uploaded since the
    #: move off the database; ``data`` is set instead on older rows.
    disk_name = db.Column(db.String(120))
    #: Written extract typed straight into Studio, rather than uploaded.
    body = db.deferred(db.Column(db.Text))
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    # Curriculum module this file belongs to (1-based). None = general file,
    # always available to buyers even when the product is drip-fed.
    module_index = db.Column(db.Integer, index=True)
    #: Lesson within the module (1-based). None = module-level content (a module
    #: intro), shown before the lessons. The module stays the drip unit, so this
    #: never affects locking — a lesson opens exactly when its module does.
    lesson_index = db.Column(db.Integer, index=True)
    #: Set on a written extract that belongs to one file rather than to the
    #: module as a whole. It carries its parent's ``module_index`` too, so drip
    #: gating needs to know nothing about the nesting.
    parent_asset_id = db.Column(
        db.Integer, db.ForeignKey("product_assets.id", ondelete="CASCADE"),
        index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    notes = db.relationship(
        "ProductAsset",
        cascade="all, delete-orphan",
        order_by="ProductAsset.sort_order, ProductAsset.id",
        backref=db.backref("parent", remote_side=[id]),
    )

    def display_title(self):
        return self.title or self.filename

    def size_mb(self):
        return round((self.size or 0) / 1024 / 1024, 1)

    def size_display(self) -> str:
        """Human size — MB is a silly unit for a two-paragraph extract."""
        n = self.size or 0
        if n < 1024:
            return f"{n} B"
        if n < 1024 * 1024:
            return f"{round(n / 1024)} KB"
        if n < 1024 * 1024 * 1024:
            return f"{n / 1024 / 1024:.1f} MB"
        return f"{n / 1024 / 1024 / 1024:.2f} GB"

    def is_text(self) -> bool:
        """A written extract, shown inline rather than fetched as a file."""
        return self.kind == "text" and self.body is not None

    def kind_label(self) -> str:
        return {
            "pdf": "PDF", "h5p": "Interactive", "image": "Image",
            "video": "Video", "audio": "Audio", "text": "Text",
            "html": "Page", "doc": "Document", "docx": "Document",
        }.get(self.kind or "", "File")


class CourseProgress(db.Model):
    """How far a member has read through a purchased course/guide."""
    __tablename__ = "course_progress"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    shop_purchase_id = db.Column(
        db.Integer, db.ForeignKey("shop_purchases.id"), nullable=False, unique=True, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), index=True)
    current_page = db.Column(db.Integer, nullable=False, default=1)
    total_pages = db.Column(db.Integer, nullable=False, default=0)
    percent = db.Column(db.Integer, nullable=False, default=0)
    bookmarks_json = db.Column(db.Text)  # JSON list of page numbers
    #: Answers typed into a fillable PDF: {form-field id: value}. Kept per
    #: purchase so a workbook opens with the buyer's own work still in it.
    form_data_json = db.Column(db.Text)
    # Which module the saved position belongs to, so switching modules in a
    # drip-fed course doesn't drop the reader mid-way through another file.
    module_index = db.Column(db.Integer)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    purchase = db.relationship("ShopPurchase")
    product = db.relationship("Product")

    def bookmarks(self) -> list[int]:
        try:
            raw = json.loads(self.bookmarks_json) if self.bookmarks_json else []
        except (TypeError, ValueError):
            return []
        out = []
        for item in raw:
            try:
                n = int(item)
            except (TypeError, ValueError):
                continue
            if n >= 1 and n not in out:
                out.append(n)
        return sorted(out)

    def set_bookmarks(self, pages) -> None:
        cleaned = []
        for item in pages or []:
            try:
                n = int(item)
            except (TypeError, ValueError):
                continue
            if n >= 1 and n not in cleaned:
                cleaned.append(n)
        cleaned.sort()
        self.bookmarks_json = json.dumps(cleaned) if cleaned else None

    def form_data(self) -> dict:
        try:
            raw = json.loads(self.form_data_json) if self.form_data_json else {}
        except (TypeError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def set_form_data(self, data) -> None:
        if not isinstance(data, dict) or not data:
            self.form_data_json = None
            return
        # Cap the stored blob so a runaway payload can't bloat the row.
        blob = json.dumps(data)[:200_000]
        self.form_data_json = blob


class MembershipPlan(db.Model):
    """A sellable membership (Healing / Creator). Buying one (matched by Stripe
    product id on the payment) upgrades the buyer's ``users.membership`` tier."""
    __tablename__ = "membership_plans"

    id = db.Column(db.Integer, primary_key=True)
    tier = db.Column(db.String(20), unique=True, nullable=False)  # healing / creator / full_bloom
    name = db.Column(db.String(80), nullable=False)
    tagline = db.Column(db.String(160))
    price_cents = db.Column(db.Integer)
    annual_price_cents = db.Column(db.Integer)
    # Optional launch-window locked-in amounts (shown while founder pricing is live)
    founder_price_cents = db.Column(db.Integer)
    founder_annual_price_cents = db.Column(db.Integer)
    currency = db.Column(db.String(3), nullable=False, default="USD")
    period = db.Column(db.String(20), nullable=False, default="month")  # month / year / once
    ls_variant_id = db.Column(db.String(40), index=True)  # legacy
    ls_checkout_url = db.Column(db.String(500))  # legacy
    # Unique so one Stripe price can never map to two plans (DB-enforced).
    stripe_price_id = db.Column(db.String(80), unique=True, index=True)
    stripe_price_id_annual = db.Column(db.String(80), unique=True, index=True)
    # Stripe Product ids (prod_…) — monthly / annual products in Stripe.
    stripe_product_id = db.Column(db.String(80), unique=True, index=True)
    stripe_product_id_annual = db.Column(db.String(80), unique=True, index=True)
    active = db.Column(db.Boolean, nullable=False, default=False)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    features_json = db.Column(db.Text)  # JSON: toggled non-free capabilities

    def label(self):
        return MEMBERSHIP_LABELS.get(self.tier, self.tier.title())

    def features(self) -> dict:
        from .services.plan_features import parse_features_json
        return parse_features_json(self.features_json, self.tier)

    def set_features(self, features: dict) -> None:
        from .services.plan_features import features_to_json, normalize_features
        self.features_json = features_to_json(normalize_features(features, self.tier))

    def feature(self, key: str):
        return self.features().get(key)

    def perk_labels(self) -> list[str]:
        from .services.plan_features import perk_labels
        return perk_labels(self.features())

    def _money(self, cents):
        if cents is None:
            return ""
        symbol = {"USD": "$", "EUR": "\u20ac", "GBP": "\u00a3"}.get(self.currency, self.currency + " ")
        amount = cents / 100
        return f"{symbol}{amount:,.0f}" if cents % 100 == 0 else f"{symbol}{amount:,.2f}"

    def price_display(self):
        return self._money(self.price_cents)

    def annual_price_display(self):
        return self._money(self.annual_price_cents)

    def is_buyable(self, billing: str = "monthly"):
        billing = (billing or "monthly").strip().lower()
        if not self.active:
            return False
        if billing in ("annual", "year", "yearly"):
            return bool((self.stripe_price_id_annual or "").strip())
        return bool((self.stripe_price_id or "").strip())

    def payment_product_id(self, billing: str = "monthly") -> str | None:
        billing = (billing or "monthly").strip().lower()
        if billing in ("annual", "year", "yearly"):
            return (self.stripe_price_id_annual or "").strip() or None
        return (self.stripe_price_id or self.ls_variant_id or "").strip() or None


class Quote(db.Model):
    __tablename__ = "quotes"

    id = db.Column(db.Integer, primary_key=True)
    text = db.Column(db.String(240), nullable=False)
    author = db.Column(db.String(120))
    category = db.Column(db.String(20), nullable=False, default="comfort")
    active = db.Column(db.Boolean, nullable=False, default=True)
    times_shown = db.Column(db.Integer, nullable=False, default=0)
    last_shown_date = db.Column(db.Date)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    favorites = db.relationship("QuoteFavorite", backref="quote", lazy="dynamic",
                                cascade="all, delete-orphan")


class QuotePin(db.Model):
    __tablename__ = "quote_pins"

    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, unique=True, nullable=False)
    quote_id = db.Column(db.Integer, db.ForeignKey("quotes.id"), nullable=False)

    quote = db.relationship("Quote")


class QuoteFavorite(db.Model):
    __tablename__ = "quote_favorites"
    __table_args__ = (db.UniqueConstraint("user_id", "quote_id", name="uq_favorite_user_quote"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    quote_id = db.Column(db.Integer, db.ForeignKey("quotes.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)


class CheckIn(db.Model):
    """One row per day a member 'shows up' — the raw history behind streaks."""
    __tablename__ = "check_ins"
    __table_args__ = (db.UniqueConstraint("user_id", "day", name="uq_checkin_user_day"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    day = db.Column(db.Date, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)


JOURNAL_PROMPTS = (
    ("mind", "What's on your mind?"),
    ("change", "What's a change you made today?"),
    ("grateful", "What are you grateful for right now?"),
    ("soft", "What felt soft or kind today?"),
    ("hard", "What felt hard — and how did you meet it?"),
    ("proud", "What's one thing you're quietly proud of?"),
    ("learn", "What did today teach you?"),
    ("body", "How is your body asking to be treated?"),
    ("rest", "Where could you rest a little more?"),
    ("brave", "Where were you brave in a small way?"),
    ("courage", "What is one thing you did today that took courage?"),
    ("space", "Where did you make space for yourself?"),
    ("release", "What are you ready to put down for tonight?"),
    ("keep", "What do you want to keep from today?"),
    ("truth", "What's one honest sentence about how you feel?"),
    ("hope", "What are you hoping for tomorrow?"),
    ("notice", "What did you notice that you usually rush past?"),
    ("help", "Who or what helped you get through today?"),
    ("boundary", "Where did you protect your peace today?"),
    ("joy", "What made you smile, even briefly?"),
    ("fear", "What fear showed up — and what did you do anyway?"),
    ("self", "What would you tell a friend in your shoes?"),
    ("energy", "What drained you, and what filled you up?"),
    ("need", "What do you need more of right now?"),
    ("less", "What could you do a little less of?"),
    ("win", "What's a tiny win from today?"),
    ("forgive", "Is there something you can forgive yourself for?"),
    ("future", "What future version of you would thank you for today?"),
    ("present", "What is true for you in this exact moment?"),
    ("love", "How did you show love — to yourself or someone else?"),
    ("miss", "What did you miss, and can you offer it tomorrow?"),
    ("anchor", "What kept you steady today?"),
    ("storm", "If today was weather, what was it — and what's clearing?"),
    ("voice", "What did you say (or not say) that mattered?"),
    ("home", "When did you feel most at home in yourself?"),
    ("grow", "Where are you growing, even if it doesn't look like it?"),
    ("enough", "Where were you enough today, without proving anything?"),
    ("begin", "What would a gentle new beginning look like tonight?"),
    ("end", "What chapter are you closing, even a little?"),
    ("bloom", "Where did you bloom anyway today?"),
    ("quiet", "What did the quiet parts of your day say?"),
    ("noise", "What noise can you step away from?"),
    ("choose", "What choice today felt like yours?"),
    ("letgo", "What can you leave on the page and not carry to bed?"),
    ("light", "Where did a little light get in?"),
    ("shadow", "What shadow showed up with something to teach?"),
    ("friend", "How were you a friend to yourself today?"),
    ("world", "What in the world felt beautiful for a second?"),
    ("work", "What part of your work (or day) felt meaningful?"),
    ("play", "Did you make any room for play or ease?"),
    ("breathe", "When did you remember to breathe today?"),
    ("tomorrow", "What intention do you want to carry into tomorrow?"),
    ("tender", "What part of you needs a little tenderness tonight?"),
    ("celebrate", "What deserves a quiet celebration from today?"),
    ("honest", "What are you ready to be honest about with yourself?"),
    ("nourish", "What nourished you today — even a little?"),
    ("setdown", "What can you set down without explaining it?"),
    ("root", "What rooted you when things felt shaky?"),
    ("open", "Where did you stay open when it would have been easier to close?"),
    ("kind", "Where did kindness find you today?"),
)

# Free-write option (not in the random prompt pool).
JOURNAL_FREEWRITE = ("free", "")


def journal_prompt_map() -> dict:
    """All valid prompt keys → labels, including free-write."""
    mapping = dict(JOURNAL_PROMPTS)
    mapping[JOURNAL_FREEWRITE[0]] = JOURNAL_FREEWRITE[1]
    return mapping


def random_journal_prompt():
    """Pick one prompt at random for the streak / journal UI."""
    import random
    return random.choice(JOURNAL_PROMPTS)


def sample_journal_prompts(n: int = 4) -> list:
    """Return ``n`` random prompts for the journal sidebar."""
    import random
    pool = list(JOURNAL_PROMPTS)
    if n >= len(pool):
        random.shuffle(pool)
        return pool
    return random.sample(pool, n)


# 5-point mood scale for the day's journal (bloom-themed, not yellow smileys).
# key, emoji, short a11y / hover label
MOODS = (
    ("sad", "\U0001f940", "Heavy"),       # wilted flower
    ("low", "\U0001f342", "Tender"),      # fallen leaf
    ("neutral", "\U0001f33f", "Steady"),  # herb
    ("soft", "\U0001f338", "Soft"),       # cherry blossom
    ("bloom", "\U0001f33b", "Radiant"),   # sunflower
)
MOOD_KEYS = frozenset(k for k, _, _ in MOODS)
MOOD_BY_KEY = {k: (emoji, label) for k, emoji, label in MOODS}


def mood_emoji(key: str | None) -> str:
    if not key:
        return ""
    pair = MOOD_BY_KEY.get(key)
    return pair[0] if pair else ""


def mood_label(key: str | None) -> str:
    if not key:
        return ""
    pair = MOOD_BY_KEY.get(key)
    return pair[1] if pair else ""


class JournalEntry(db.Model):
    """A private journal page (multiple pages allowed per day)."""
    __tablename__ = "journal_entries"
    __table_args__ = (
        db.Index("ix_journal_entries_user_day", "user_id", "day"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    day = db.Column(db.Date, nullable=False)
    prompt_key = db.Column(db.String(40), nullable=False, default="mind")
    prompt_label = db.Column(db.String(120), nullable=False, default="")
    body = db.Column(db.Text, nullable=False, default="")
    mood = db.Column(db.String(20))  # MOODS key; optional
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    author = db.relationship("User", backref=db.backref("journal_entries", lazy="dynamic"))

    def mood_emoji(self) -> str:
        return mood_emoji(self.mood)

    def mood_label(self) -> str:
        return mood_label(self.mood)


class Follow(db.Model):
    __tablename__ = "follows"
    __table_args__ = (db.UniqueConstraint("follower_id", "following_id",
                                          name="uq_follow_pair"),)

    id = db.Column(db.Integer, primary_key=True)
    follower_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    following_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)


class Notification(db.Model):
    __tablename__ = "notifications"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    kind = db.Column(db.String(30), nullable=False)
    # follow_post / mention / followed / follow_listing / content_hub / shop
    post_id = db.Column(db.Integer, db.ForeignKey("forum_posts.id", ondelete="SET NULL"))
    body = db.Column(db.String(300), nullable=False, default="")
    url = db.Column(db.String(300))  # optional deep link when not a forum post
    read_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    actor = db.relationship("User", foreign_keys=[actor_id])
    post = db.relationship("ForumPost")

    def href(self) -> str | None:
        """Best link for this notification."""
        if self.url:
            return self.url
        if self.kind in ("support_group", "support_group_alert"):
            return None
        if self.post_id:
            from flask import url_for
            try:
                return url_for("forums.post", post_id=self.post_id)
            except RuntimeError:
                return f"/forums/p/{self.post_id}"
        return None


class Video(db.Model):
    """A Content Hub tip: written advice with an optional video attached.

    ``description`` is the short summary on hub cards; ``body`` is the tip
    itself and is gated the same way playback is. Creator members get
    everything by default; ``free_access`` / ``healing_access`` open a tip up
    to lower tiers. Video files stream from disk (VIDEO_STORAGE_DIR);
    thumbnails live in the DB."""
    __tablename__ = "videos"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(160), nullable=False)
    description = db.Column(db.Text)
    body = db.Column(db.Text)              # the written tip
    filename = db.Column(db.String(255))   # original upload name (for download)
    disk_name = db.Column(db.String(64))   # stored file name on disk
    mime = db.Column(db.String(120))       # None for tips with no video
    size = db.Column(db.Integer, nullable=False, default=0)
    # Deferred: the hub lists every tip and only ever needs the text.
    data = db.deferred(db.Column(db.LargeBinary))   # legacy DB-stored bytes
    thumb_data = db.deferred(db.Column(db.LargeBinary))
    thumb_mime = db.Column(db.String(40))
    published = db.Column(db.Boolean, nullable=False, default=True)
    free_access = db.Column(db.Boolean, nullable=False, default=False)
    # When True, Healing / Full Bloom members can watch (in addition to free picks).
    healing_access = db.Column(db.Boolean, nullable=False, default=False)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    def access_label(self, user) -> str:
        """Membership inclusion label for cards / watch pages."""
        if self.free_access:
            if getattr(user, "is_authenticated", False):
                return "Included in your membership"
            return "Included in Free membership"
        if getattr(user, "is_authenticated", False):
            if getattr(user, "is_admin", False) or (
                    hasattr(user, "has_feature") and user.has_feature("content_hub_creator")):
                return "Included in your membership"
            if self.healing_access and hasattr(user, "has_feature") and user.has_feature("content_hub_healing"):
                return "Included in your membership"
        if self.healing_access:
            return "Included in Healing membership"
        return "Included in Creator membership"

    def has_thumb(self) -> bool:
        return bool(self.thumb_mime)

    def has_video(self) -> bool:
        # ``mime`` is set for every row that carries video bytes, so this
        # answers without pulling the (deferred) file out of the database.
        return bool(self.disk_name or self.mime)

    def summary(self, limit: int = 150) -> str:
        """Card blurb: the owner's summary, else the opening of the tip."""
        text = (self.description or "").strip()
        if not text:
            text = " ".join((self.body or "").split())
        if len(text) <= limit:
            return text
        return text[:limit].rsplit(" ", 1)[0] + "\u2026"

    def teaser(self, limit: int = 240) -> str:
        """Opening of the tip for members whose plan doesn't cover it.

        Never more than half of it — a short tip would otherwise be given away
        in full on the page that asks them to upgrade.
        """
        words = (self.body or "").split()
        if not words:
            return ""
        text = " ".join(words[:max(1, len(words) // 2)])
        if len(text) > limit:
            text = text[:limit].rsplit(" ", 1)[0]
        return text + "\u2026"

    def locked_shape(self) -> str:
        """What a member who can't read this tip is shown instead of it."""
        return blurred_shape(self.body, seed=self.id or 0)

    def read_minutes(self) -> int:
        words = len((self.body or "").split())
        return max(1, round(words / 200)) if words else 0

    def size_mb(self):
        return round((self.size or 0) / 1024 / 1024, 1)


# Order.status "ended" means the membership stopped (cancelled, revoked, or the
# subscription ran out) but the money was still collected. Access checks look for
# "paid" only; revenue counts both, so a cancellation can't rewrite past earnings
# the way marking it "refunded" used to.
COLLECTED_ORDER_STATUSES = ("paid", "ended")


class Order(db.Model):
    __tablename__ = "orders"

    id = db.Column(db.Integer, primary_key=True)
    ls_order_id = db.Column(db.String(120), unique=True, nullable=False)
    ls_variant_id = db.Column(db.String(80))
    # Set at membership checkout from metadata.tier / price→plan. Source of truth
    # for which plan this paid order grants (healing / creator / full_bloom).
    membership_tier = db.Column(db.String(20), index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"))
    buyer_email = db.Column(db.String(255), nullable=False, index=True)
    # if the buyer gifted this to a friend, the friend's account email gets
    # access to the product's files instead of/along with the buyer
    gift_to_email = db.Column(db.String(255), index=True)
    total_cents = db.Column(db.Integer, nullable=False, default=0)
    currency = db.Column(db.String(3), nullable=False, default="USD")
    status = db.Column(db.String(20), nullable=False, default="paid")
    # Set when a membership welcome email is claimed/sent for this order.
    # Used to stop duplicate welcomes across checkout + invoice events.
    welcome_sent_at = db.Column(db.DateTime)
    # Stripe subscription this payment belongs to; renewals share it. Survives
    # account deletion (which scrubs buyer_email), so it is the only way to tell
    # that a later charge continues a membership whose owner is gone.
    stripe_subscription_id = db.Column(db.String(80), index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    def total_display(self):
        symbol = {"USD": "$", "EUR": "\u20ac", "GBP": "\u00a3"}.get(self.currency, self.currency + " ")
        return f"{symbol}{self.total_cents / 100:,.2f}"

    def masked_email(self):
        try:
            local, domain = self.buyer_email.split("@", 1)
            return f"{local[0]}\u2022\u2022\u2022@{domain}"
        except ValueError:
            return "\u2022\u2022\u2022"


class ShopPurchase(db.Model):
    """A digital purchase from shop.bloomanyway.online (Lemon Squeezy storefront).

    Linked to a Bloom Anyway account by email when possible; otherwise stays
    pending_link until that email signs up / logs in.
    """
    __tablename__ = "shop_purchases"

    STATUSES = ("pending_link", "linked", "refunded")

    id = db.Column(db.Integer, primary_key=True)
    lemon_squeezy_order_id = db.Column(db.String(120), unique=True, nullable=False)
    customer_email = db.Column(db.String(255), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)
    product_name = db.Column(db.String(200), nullable=False, default="Shop purchase")
    product_id = db.Column(db.String(80))   # Lemon product id
    variant_id = db.Column(db.String(80))   # Lemon variant id
    # Lemon webhooks do not include a stable signed file URL; we store the
    # order receipt URL (customer portal with downloads) when present.
    download_url = db.Column(db.String(1000))
    file_key = db.Column(db.String(255), index=True)  # self-hosted file id
    purchased_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    status = db.Column(db.String(20), nullable=False, default="pending_link")

    user = db.relationship("User", backref=db.backref("shop_purchases", lazy="dynamic"))


class Subscriber(db.Model):
    __tablename__ = "subscribers"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)


class ChallengeWaitlist(db.Model):
    """Round-1 signups for the 2-month Creator Challenge.

    The challenge enrols directly now, so nothing writes here any more — the
    table is kept so the emails collected before that change aren't lost.
    """
    __tablename__ = "challenge_waitlist"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)


class Testimonial(db.Model):
    __tablename__ = "testimonials"

    id = db.Column(db.Integer, primary_key=True)
    quote = db.Column(db.Text, nullable=False)
    first_name = db.Column(db.String(60), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"))
    show_on_home = db.Column(db.Boolean, nullable=False, default=False)
    sort_order = db.Column(db.Integer, nullable=False, default=0)


class FaqItem(db.Model):
    __tablename__ = "faq_items"

    id = db.Column(db.Integer, primary_key=True)
    question = db.Column(db.String(240), nullable=False)
    answer_md = db.Column(db.Text, nullable=False)
    sort_order = db.Column(db.Integer, nullable=False, default=0)


class Page(db.Model):
    __tablename__ = "pages"

    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(80), unique=True, nullable=False)
    title = db.Column(db.String(160), nullable=False)
    body_md = db.Column(db.Text, nullable=False, default="")
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)


class Setting(db.Model):
    __tablename__ = "settings"

    key = db.Column(db.String(80), primary_key=True)
    value = db.Column(db.Text, nullable=False, default="")


class PageView(db.Model):
    __tablename__ = "page_views"
    __table_args__ = (db.UniqueConstraint("path", "date", name="uq_pageview_path_date"),)

    id = db.Column(db.Integer, primary_key=True)
    path = db.Column(db.String(300), nullable=False)
    date = db.Column(db.Date, nullable=False)
    count = db.Column(db.Integer, nullable=False, default=0)


class VisitEvent(db.Model):
    """Where a visitor arrived from (UTM / referrer) for Studio insights."""
    __tablename__ = "visit_events"

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)
    path = db.Column(db.String(300), nullable=False)
    source = db.Column(db.String(80), nullable=False, index=True)  # Facebook, Instagram, …
    referrer = db.Column(db.String(500))
    utm_source = db.Column(db.String(120))
    utm_medium = db.Column(db.String(120))
    utm_campaign = db.Column(db.String(160))
    session_key = db.Column(db.String(64), index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)

    user = db.relationship("User")


class ContactMessage(db.Model):
    """A message from the public contact form. Shown in Studio → Inbox."""
    __tablename__ = "contact_messages"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(255), nullable=False)
    body = db.Column(db.Text, nullable=False)
    # new / reviewed, mirroring SiteFeedback so the Inbox can triage both.
    status = db.Column(db.String(20), nullable=False, default="new", index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)


# --- community forums --------------------------------------------------------

class ForumCategory(db.Model):
    __tablename__ = "forum_categories"

    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(60), unique=True, nullable=False)
    name = db.Column(db.String(80), nullable=False)
    description = db.Column(db.String(240), nullable=False, default="")
    accent = db.Column(db.String(7))          # optional hex colour for the card
    sort_order = db.Column(db.Integer, nullable=False, default=0)

    posts = db.relationship("ForumPost", backref="category", lazy="dynamic",
                            cascade="all, delete-orphan")
    tags = db.relationship("ForumTag", backref="category", lazy="dynamic",
                           cascade="all, delete-orphan",
                           order_by="ForumTag.sort_order")


class ForumTag(db.Model):
    """A topic label within a forum (e.g. "Divorce & Custody" under Healing)."""
    __tablename__ = "forum_tags"
    __table_args__ = (db.UniqueConstraint("category_id", "slug", name="uq_tag_category_slug"),)

    id = db.Column(db.Integer, primary_key=True)
    category_id = db.Column(db.Integer, db.ForeignKey("forum_categories.id"), nullable=False, index=True)
    slug = db.Column(db.String(60), nullable=False)
    name = db.Column(db.String(80), nullable=False)
    sort_order = db.Column(db.Integer, nullable=False, default=0)


#: Site-wide "Looking for" intents on community posts (not per-forum).
LOOKING_FOR = (
    ("advice", "Advice"),
    ("support", "Support"),
    ("recognition", "Recognition"),
    ("listening", "A listening ear"),
    ("resources", "Resources"),
    ("accountability", "Accountability"),
    ("celebration", "Celebration"),
    ("company", "Company"),
)
LOOKING_FOR_LABELS = dict(LOOKING_FOR)
LOOKING_FOR_SLUGS = frozenset(LOOKING_FOR_LABELS)


def looking_for_label(slug: str | None) -> str | None:
    if not slug:
        return None
    return LOOKING_FOR_LABELS.get(slug)


class ForumPost(db.Model):
    __tablename__ = "forum_posts"

    id = db.Column(db.Integer, primary_key=True)
    category_id = db.Column(db.Integer, db.ForeignKey("forum_categories.id"), nullable=False, index=True)
    tag_id = db.Column(db.Integer, db.ForeignKey("forum_tags.id"), index=True)
    looking_for = db.Column(db.String(40), index=True)  # LOOKING_FOR slug
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    title = db.Column(db.String(160), nullable=False)
    body = db.Column(db.Text, nullable=False)
    anonymous = db.Column(db.Boolean, nullable=False, default=False)
    hidden = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    author = db.relationship("User")
    tag = db.relationship("ForumTag")
    comments = db.relationship("ForumComment", backref="post", lazy="dynamic",
                               cascade="all, delete-orphan")
    likes = db.relationship("ForumPostLike", backref="post", lazy="dynamic",
                            cascade="all, delete-orphan")

    def display_author(self):
        return "Anonymous" if self.anonymous else self.author.public_name()

    def looking_for_label(self) -> str | None:
        return looking_for_label(self.looking_for)

    def like_count(self):
        return self.likes.count()


class ForumComment(db.Model):
    __tablename__ = "forum_comments"

    id = db.Column(db.Integer, primary_key=True)
    post_id = db.Column(db.Integer, db.ForeignKey("forum_posts.id"), nullable=False, index=True)
    parent_id = db.Column(db.Integer, db.ForeignKey("forum_comments.id"), index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    body = db.Column(db.Text, nullable=False)
    anonymous = db.Column(db.Boolean, nullable=False, default=False)
    hidden = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    author = db.relationship("User")
    # one level of replies only (a reply cannot itself be replied to)
    replies = db.relationship("ForumComment",
                              backref=db.backref("parent", remote_side=[id]),
                              lazy="select", order_by="ForumComment.created_at",
                              cascade="all, delete-orphan")
    likes = db.relationship("ForumCommentLike", backref="comment", lazy="dynamic",
                            cascade="all, delete-orphan")

    def display_author(self):
        return "Anonymous" if self.anonymous else self.author.public_name()

    def like_count(self):
        return self.likes.count()


class ForumPostLike(db.Model):
    __tablename__ = "forum_post_likes"
    __table_args__ = (db.UniqueConstraint("user_id", "post_id", name="uq_postlike_user_post"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    post_id = db.Column(db.Integer, db.ForeignKey("forum_posts.id"), nullable=False)


class ForumCommentLike(db.Model):
    __tablename__ = "forum_comment_likes"
    __table_args__ = (db.UniqueConstraint("user_id", "comment_id", name="uq_commentlike_user_comment"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    comment_id = db.Column(db.Integer, db.ForeignKey("forum_comments.id"), nullable=False)


# --- announcements ----------------------------------------------------------

class Announcement(db.Model):
    """A home-page announcement. Several can be live at once; they stack tidily.
    Non-dismissible; the owner sets an optional expiry and optional link."""
    __tablename__ = "announcements"

    id = db.Column(db.Integer, primary_key=True)
    body = db.Column(db.String(300), nullable=False)
    link_url = db.Column(db.String(500))  # optional; whole card becomes the button
    expires = db.Column(db.Date)   # defaults to +1 week when created from Studio
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    def is_live(self) -> bool:
        return self.expires is None or self.expires >= date.today()


# --- marketplace ------------------------------------------------------------

MARKETPLACE_KINDS = ("product", "service", "business")
MARKETPLACE_KIND_LABELS = {
    "product": "Digital product", "service": "Service", "business": "Business",
}
#: how many active listings each tier may run at once
MARKETPLACE_LIMITS = {
    "none": 0,
    "healing": 1,
    "creator": 5,
    "full_bloom": 5,
}
#: how many tags a single listing may carry
MARKETPLACE_TAG_MAX = 24
#: Curated tag catalogue, grouped the way the site is: the healing track, the
#: building track, and broad tags that suit either. Anything narrower than this
#: (a single app, a single niche) belongs in a listing's own custom tags rather
#: than in a list every member has to read through.
MARKETPLACE_TAG_GROUPS = (
    ("Healing", "Finding your feet again", (
        "Healing", "Trauma-informed", "Divorce", "Custody & co-parenting",
        "Single moms", "Grief", "Starting over", "Confidence", "Boundaries",
        "Anxiety", "Self-care", "Mindfulness", "Journaling",
        "Faith & spirituality",
    )),
    ("Building", "Making something of your own", (
        "Building", "Content creation", "Social media", "Branding", "Writing",
        "Photography", "Video", "Podcast", "Speaking", "Marketing",
        "Selling online", "Business", "Freelance", "Money", "Career",
    )),
    ("Anything", "Broad tags that suit either side", (
        "Coaching", "Mentorship", "1:1", "Group", "Workshop", "Community",
        "Course", "Ebook", "Workbook", "Planner", "Template",
        "Digital download", "Membership", "Online", "In-person",
        "Beginner-friendly", "Free resource", "Wellness", "Home & family",
        "Style & beauty",
    )),
)
#: flat catalogue for lookups and filters
MARKETPLACE_TAGS = tuple(
    tag for _label, _hint, group in MARKETPLACE_TAG_GROUPS for tag in group
)


class MarketplaceListing(db.Model):
    """A member-run advert for a digital product or a service. We only
    advertise here and redirect to the seller's own site — no checkout."""
    __tablename__ = "marketplace_listings"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    kind = db.Column(db.String(20), nullable=False, default="product")  # product / service / business
    title = db.Column(db.String(140), nullable=False)
    description = db.Column(db.Text, nullable=False, default="")
    location = db.Column(db.String(120))     # services only
    price = db.Column(db.String(80))         # free text, e.g. "$49" or "From $20/hr"
    website_url = db.Column(db.String(500), nullable=False)
    tags_json = db.Column(db.Text)           # JSON list of free-form tags
    clicks = db.Column(db.Integer, nullable=False, default=0)  # outbound clicks (popularity)
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    author = db.relationship("User")
    images = db.relationship("ListingImage", backref="listing", lazy="select",
                             order_by="ListingImage.sort_order",
                             cascade="all, delete-orphan")

    def kind_label(self):
        return MARKETPLACE_KIND_LABELS.get(self.kind, "Listing")

    def tags(self) -> list:
        try:
            return json.loads(self.tags_json) if self.tags_json else []
        except ValueError:
            return []

    def set_tags(self, tags) -> None:
        self.tags_json = json.dumps(list(tags)) if tags else None

    def thumb(self):
        return self.images[0] if self.images else None


class ListingImage(db.Model):
    __tablename__ = "listing_images"

    id = db.Column(db.Integer, primary_key=True)
    listing_id = db.Column(db.Integer, db.ForeignKey("marketplace_listings.id"),
                           nullable=False, index=True)
    # Deferred: Showcase cards only need the image's id to build its URL.
    data = db.deferred(db.Column(db.LargeBinary, nullable=False))
    mime = db.Column(db.String(40), nullable=False, default="image/jpeg")
    sort_order = db.Column(db.Integer, nullable=False, default=0)


class ProductGalleryImage(db.Model):
    """Teaser / gallery JPEG for a course or guide (DB-backed for redeploys)."""
    __tablename__ = "product_gallery_images"
    __table_args__ = (
        db.UniqueConstraint("product_id", "filename",
                            name="uq_product_gallery_filename"),
    )

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"),
                           nullable=False, index=True)
    filename = db.Column(db.String(80), nullable=False)
    data = db.deferred(db.Column(db.LargeBinary, nullable=False))
    mime = db.Column(db.String(40), nullable=False, default="image/jpeg")
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)


# --- reel reviews (Content Hub) ---------------------------------------------

class ReelReviewApplication(db.Model):
    """A Creator member's weekly request for a reel review.

    One entry per user per week (``week_key`` = that Monday, Atlanta time).
    Owners review one entry a day; Monday clears whatever wasn't reached.
    """
    __tablename__ = "reel_review_applications"
    __table_args__ = (
        db.UniqueConstraint("user_id", "week_key", name="uq_reel_app_user_week"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    week_key = db.Column(db.Date, nullable=False, index=True)  # Monday of the week
    reel_url = db.Column(db.String(500), nullable=False)
    disk_name = db.Column(db.String(64))   # raw video on VIDEO_STORAGE_DIR
    # Deferred: only the streaming route ever wants these legacy bytes.
    data = db.deferred(db.Column(db.LargeBinary))  # legacy: stored in Postgres
    filename = db.Column(db.String(255))
    mime = db.Column(db.String(120), nullable=False, default="video/mp4")
    size = db.Column(db.Integer, nullable=False, default=0)
    selected = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    author = db.relationship("User")
    review = db.relationship("ReelReview", backref="application", uselist=False,
                             cascade="all, delete-orphan")

    def has_raw_video(self) -> bool:
        # ``size`` stands in for the deferred bytes on legacy DB-stored rows.
        return bool(self.disk_name) or bool(self.size)


class ReelReview(db.Model):
    """A published reel review — public on the Content Hub."""
    __tablename__ = "reel_reviews"

    id = db.Column(db.Integer, primary_key=True)
    application_id = db.Column(db.Integer, db.ForeignKey("reel_review_applications.id"),
                               nullable=False, unique=True)
    title = db.Column(db.String(160), nullable=False)
    body = db.Column(db.Text, nullable=False, default="")
    review_disk_name = db.Column(db.String(64))  # optional owner review video
    review_mime = db.Column(db.String(120))
    review_filename = db.Column(db.String(255))
    published = db.Column(db.Boolean, nullable=False, default=True)
    #: the Atlanta day this went out, so one a day can be enforced and counted
    review_date = db.Column(db.Date, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    def teaser(self, limit: int = 220) -> str:
        """The opening of the write-up, for members who can't read it yet."""
        text = " ".join((self.body or "").split())
        if not text:
            return ""
        words = text.split(" ")
        text = " ".join(words[: max(1, len(words) // 2)])
        return (text[:limit].rstrip() + "…") if text else ""

    def locked_shape(self) -> str:
        """What a member who can't read the review is shown instead of it."""
        return blurred_shape(self.body, seed=self.id or 0)


class ReelSubmission(db.Model):
    """A Creator member's entry for the home page Reel of the Week.

    One entry per user per week (``week_key`` = that Monday, Atlanta time).
    Needs the Instagram link and the raw video, and the member states the
    share count — Instagram gives us no way to check it, so the owner sees
    the number and decides.
    """
    __tablename__ = "reel_submissions"
    __table_args__ = (
        db.UniqueConstraint("user_id", "week_key", name="uq_reel_sub_user_week"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    week_key = db.Column(db.Date, nullable=False, index=True)
    reel_url = db.Column(db.String(500), nullable=False)
    share_count = db.Column(db.Integer, nullable=False, default=0)
    disk_name = db.Column(db.String(64))
    filename = db.Column(db.String(255))
    mime = db.Column(db.String(120), nullable=False, default="video/mp4")
    size = db.Column(db.Integer, nullable=False, default=0)
    featured = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    author = db.relationship("User")

    def has_raw_video(self) -> bool:
        return bool(self.disk_name) or bool(self.size)


# --- site-branded images (hero / story teaser uploads) ----------------------

SITE_IMAGE_KEYS = ("portrait", "hero", "creator")


class SiteImage(db.Model):
    """Owner-uploaded site images stored in the DB (survive ephemeral disks)."""
    __tablename__ = "site_images"

    key = db.Column(db.String(40), primary_key=True)
    data = db.Column(db.LargeBinary, nullable=False)
    mime = db.Column(db.String(40), nullable=False, default="image/jpeg")
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow)


# --- site feedback, complaints, error reports, content reports ---------------

FEEDBACK_KINDS = ("feedback", "complaint", "error")
FEEDBACK_STATUSES = ("new", "reviewed")
CONTENT_REPORT_STATUSES = ("open", "resolved", "dismissed")
CONTENT_REPORT_TARGETS = ("post", "comment", "user")

# Peer-session report reasons (dropdown on the post-meeting wrap page).
SUPPORT_REPORT_REASONS = (
    ("harassment", "Harassment or hostility"),
    ("inappropriate", "Inappropriate or sexual content"),
    ("spam", "Spam or solicitation"),
    ("privacy", "Sharing private information"),
    ("disrupting", "Disrupting the session"),
    ("other", "Other"),
)


class SiteFeedback(db.Model):
    """Star ratings, complaints, and error reports from members / visitors."""
    __tablename__ = "site_feedback"

    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(20), nullable=False, index=True)  # feedback / complaint / error
    stars = db.Column(db.Integer)  # 1–5 for kind=feedback
    body = db.Column(db.Text, nullable=False, default="")
    page_path = db.Column(db.String(300))
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)
    contact_email = db.Column(db.String(255))  # optional guest contact
    status = db.Column(db.String(20), nullable=False, default="new", index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    author = db.relationship("User")


class ContentReport(db.Model):
    """Member report of a forum post/comment or a peer-session member."""
    __tablename__ = "content_reports"

    id = db.Column(db.Integer, primary_key=True)
    target_type = db.Column(db.String(20), nullable=False)  # post / comment / user
    target_id = db.Column(db.Integer, nullable=False, index=True)
    reporter_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    reason = db.Column(db.String(80))  # structured reason key (esp. user reports)
    note = db.Column(db.String(500), nullable=False, default="")
    status = db.Column(db.String(20), nullable=False, default="open", index=True)
    auto_hidden = db.Column(db.Boolean, nullable=False, default=False)
    auto_reason = db.Column(db.String(240))
    owner_note = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    resolved_at = db.Column(db.DateTime)

    reporter = db.relationship("User")

    def reason_label(self) -> str:
        key = (self.reason or "").strip()
        for k, label in SUPPORT_REPORT_REASONS:
            if k == key:
                return label
        return key or (self.auto_reason or "")


# --- support / coaching groups (Daily.co peer rooms) -------------------------

SUPPORT_APP_STATUSES = ("pending", "selected", "cancelled", "attended")
SUPPORT_MEETING_STATUSES = ("draft", "scheduled", "completed", "cancelled")
SUPPORT_MEETING_KINDS = ("peer", "facilitator", "one_on_one")
SUPPORT_CIRCLE_TRACKS = ("healing", "building")

# Seed catalogue for peer circles shown on /support-groups and in Studio.
# Capacity is seats per session (peer meetings are capped at 8).
SUPPORT_CIRCLE_SEED = (
    ("divorce-recovery", "healing", "Divorce Recovery",
     "Process endings, paperwork, and the quiet after — with women who get it.",
     8, "Peer-scheduled", "heart"),
    ("co-parenting", "healing", "Co-Parenting Circle",
     "Navigate shared parenting, boundaries, and hard conversations with care.",
     8, "Peer-scheduled", "people"),
    ("starting-over", "healing", "Starting Over",
     "Rebuild identity, routines, and confidence when life looks nothing like before.",
     8, "Peer-scheduled", "sun"),
    ("grief-loss", "healing", "Grief & Loss",
     "Hold space for what was lost — without having to rush toward fine.",
     8, "Peer-scheduled", "candle"),
    ("new-creators", "building", "New Creators Circle",
     "Launch your first posts, offers, and habits without spinning alone.",
     8, "Peer-scheduled", "plant"),
    ("digital-products", "building", "Digital Product Builders",
     "Ship guides, templates, and downloads with peers who understand the messy middle.",
     8, "Peer-scheduled", "box"),
    ("scaling-up", "building", "Scaling Up",
     "Systems, launches, and sustainable growth when ready to level up.",
     8, "Peer-scheduled", "arrow"),
    ("money-investing", "building", "Money & Investing",
     "Normalize talking numbers, pricing, and building wealth on your terms.",
     8, "Peer-scheduled", "wallet"),
    ("custom-healing", "healing", "Custom",
     "Name your own Healing peer topic when you schedule — whatever you need to talk through.",
     8, "Peer-scheduled", "spark"),
    ("custom-building", "building", "Custom",
     "Name your own Creator accountability topic when you schedule — your call.",
     8, "Peer-scheduled", "spark"),
)


class SupportGroupCircle(db.Model):
    """A named peer circle category (Divorce Recovery, New Creators, etc.)."""
    __tablename__ = "support_group_circles"

    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(60), nullable=False, unique=True, index=True)
    track = db.Column(db.String(20), nullable=False, index=True)  # healing / building
    title = db.Column(db.String(120), nullable=False)
    blurb = db.Column(db.String(400), nullable=False, default="")
    capacity = db.Column(db.Integer, nullable=False, default=12)
    meets_label = db.Column(db.String(80), nullable=False, default="")
    icon = db.Column(db.String(40), nullable=False, default="heart")
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    active = db.Column(db.Boolean, nullable=False, default=True)

    applications = db.relationship(
        "SupportGroupApplication", back_populates="circle", lazy="dynamic",
    )
    meetings = db.relationship(
        "SupportGroupMeeting", back_populates="circle", lazy="dynamic",
    )
    topic_alerts = db.relationship(
        "SupportGroupTopicAlert", back_populates="circle", lazy="dynamic",
    )


class SupportGroupMeeting(db.Model):
    """A Daily.co support-group session with a fixed seat count."""
    __tablename__ = "support_group_meetings"

    id = db.Column(db.Integer, primary_key=True)
    circle_id = db.Column(
        db.Integer, db.ForeignKey("support_group_circles.id"), index=True,
    )
    capacity = db.Column(db.Integer, nullable=False, default=8)
    kind = db.Column(db.String(20), nullable=False, default="peer", index=True)
    scheduled_by_user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), index=True,
    )
    scheduled_at = db.Column(db.DateTime)  # stored UTC (naive)
    # Legacy column names; store Daily room URL + room name.
    zoom_url = db.Column(db.String(500))
    zoom_meeting_id = db.Column(db.String(64))
    status = db.Column(db.String(20), nullable=False, default="draft", index=True)
    booked_notified_at = db.Column(db.DateTime)
    reminded_at = db.Column(db.DateTime)
    notes = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    circle = db.relationship("SupportGroupCircle", back_populates="meetings")
    host = db.relationship("User", foreign_keys=[scheduled_by_user_id])
    applications = db.relationship(
        "SupportGroupApplication", back_populates="meeting", lazy="dynamic",
    )

    @property
    def room_url(self) -> str:
        return (self.zoom_url or "").strip()

    @property
    def room_name(self) -> str:
        return (self.zoom_meeting_id or "").strip()

    def is_bookable(self) -> bool:
        return bool(self.scheduled_at and self.room_url)

    def is_peer(self) -> bool:
        return (self.kind or "peer") == "peer"


class SupportGroupApplication(db.Model):
    """Healing/Creator request to join a specific support-group circle."""
    __tablename__ = "support_group_applications"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    circle_id = db.Column(
        db.Integer, db.ForeignKey("support_group_circles.id"), index=True,
    )
    meeting_id = db.Column(
        db.Integer, db.ForeignKey("support_group_meetings.id"), index=True,
    )
    message = db.Column(db.Text, nullable=False, default="")
    status = db.Column(db.String(20), nullable=False, default="pending", index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    author = db.relationship("User")
    circle = db.relationship("SupportGroupCircle", back_populates="applications")
    meeting = db.relationship("SupportGroupMeeting", back_populates="applications")


class SupportGroupTopicAlert(db.Model):
    """Member opted in to hear when a peer session is scheduled for a topic."""
    __tablename__ = "support_group_topic_alerts"
    __table_args__ = (
        db.UniqueConstraint(
            "user_id", "circle_id", name="uq_support_topic_alert_user_circle",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    circle_id = db.Column(
        db.Integer, db.ForeignKey("support_group_circles.id"), nullable=False, index=True,
    )
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    author = db.relationship("User")
    circle = db.relationship("SupportGroupCircle", back_populates="topic_alerts")


COACH_SLUGS = ("saman", "ayesha")
COACHING_INTAKE_STATUSES = (
    "pending_payment", "paid", "scheduled", "cancelled", "expired",
)


class CoachAvailability(db.Model):
    """Weekly availability window for a founder 1:1 coach (local to timezone)."""
    __tablename__ = "coach_availability"

    id = db.Column(db.Integer, primary_key=True)
    coach = db.Column(db.String(20), nullable=False, index=True)  # saman / ayesha
    weekday = db.Column(db.Integer, nullable=False)  # 0=Monday … 6=Sunday
    start_minute = db.Column(db.Integer, nullable=False)  # minutes from local midnight
    end_minute = db.Column(db.Integer, nullable=False)
    timezone = db.Column(db.String(64), nullable=False, default="UTC")
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)


class CoachingIntake(db.Model):
    """Pre-checkout questionnaire + chosen slot for a founder 1:1."""
    __tablename__ = "coaching_intakes"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    coach = db.Column(db.String(20), nullable=False, index=True)
    answers_json = db.Column(db.Text, nullable=False, default="{}")
    scheduled_at = db.Column(db.DateTime, nullable=False)  # UTC naive
    status = db.Column(
        db.String(20), nullable=False, default="pending_payment", index=True,
    )
    meeting_id = db.Column(
        db.Integer, db.ForeignKey("support_group_meetings.id"), index=True,
    )
    stripe_session_id = db.Column(db.String(120))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    member = db.relationship("User", foreign_keys=[user_id])
    meeting = db.relationship("SupportGroupMeeting")

    def answers(self) -> dict:
        import json
        try:
            data = json.loads(self.answers_json or "{}")
        except (TypeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def set_answers(self, data: dict) -> None:
        import json
        self.answers_json = json.dumps(data or {}, ensure_ascii=False)
