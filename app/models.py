"""All SQLAlchemy models."""
from datetime import datetime, timezone

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --- constants (kept as plain strings for SQLite/Postgres portability) ------
PRODUCT_TYPES = ("course", "guide")
PRODUCT_STATUSES = ("draft", "published", "archived")
QUOTE_CATEGORIES = ("comfort", "determination", "renewal")


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255))
    email_verified_at = db.Column(db.DateTime)
    display_name = db.Column(db.String(80))
    is_admin = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    last_login_at = db.Column(db.DateTime)
    deleted_at = db.Column(db.DateTime)

    codes = db.relationship("VerificationCode", backref="user", lazy="dynamic",
                            cascade="all, delete-orphan")
    check_ins = db.relationship("CheckIn", backref="user", lazy="dynamic",
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
    status = db.Column(db.String(20), nullable=False, default="draft")
    featured = db.Column(db.Boolean, nullable=False, default=False)
    badge = db.Column(db.String(30))
    sort_order = db.Column(db.Integer, nullable=False, default=0)

    promise = db.Column(db.String(120))
    description_md = db.Column(db.Text)
    audience = db.Column(db.Text)          # "Who this is for"
    contents_text = db.Column(db.Text)     # one item per line -> check-list
    curriculum_json = db.Column(db.Text)   # JSON: [{title, description}]

    cover_url = db.Column(db.String(500))
    gallery_json = db.Column(db.Text)      # JSON: [url, ...]

    price_cents = db.Column(db.Integer)
    compare_at_cents = db.Column(db.Integer)
    currency = db.Column(db.String(3), nullable=False, default="USD")
    ls_checkout_url = db.Column(db.String(500))
    ls_variant_id = db.Column(db.String(40), index=True)

    meta_title = db.Column(db.String(160))
    meta_description = db.Column(db.String(200))

    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    orders = db.relationship("Order", backref="product", lazy="dynamic")
    testimonials = db.relationship("Testimonial", backref="product", lazy="dynamic")

    def price_display(self):
        if self.price_cents is None:
            return ""
        symbol = {"USD": "$", "EUR": "\u20ac", "GBP": "\u00a3"}.get(self.currency, self.currency + " ")
        amount = self.price_cents / 100
        return f"{symbol}{amount:,.0f}" if self.price_cents % 100 == 0 else f"{symbol}{amount:,.2f}"

    def compare_at_display(self):
        if self.compare_at_cents is None:
            return ""
        symbol = {"USD": "$", "EUR": "\u20ac", "GBP": "\u00a3"}.get(self.currency, self.currency + " ")
        amount = self.compare_at_cents / 100
        return f"{symbol}{amount:,.0f}" if self.compare_at_cents % 100 == 0 else f"{symbol}{amount:,.2f}"

    def type_label(self):
        return "Course" if self.type == "course" else "Notebook Guide"

    def publish_blockers(self):
        """List of human-readable requirements missing before publishing."""
        missing = []
        if not (self.promise or "").strip():
            missing.append("a one-line promise")
        if not (self.cover_url or "").strip():
            missing.append("a cover image URL")
        if self.price_cents is None:
            missing.append("a price")
        if not (self.ls_checkout_url or "").strip():
            missing.append("the Lemon Squeezy buy link")
        return missing


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
    __tablename__ = "check_ins"
    __table_args__ = (db.UniqueConstraint("user_id", "date", name="uq_checkin_user_date"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    date = db.Column(db.Date, nullable=False)


class Order(db.Model):
    __tablename__ = "orders"

    id = db.Column(db.Integer, primary_key=True)
    ls_order_id = db.Column(db.String(40), unique=True, nullable=False)
    ls_variant_id = db.Column(db.String(40))
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"))
    buyer_email = db.Column(db.String(255), nullable=False, index=True)
    total_cents = db.Column(db.Integer, nullable=False, default=0)
    currency = db.Column(db.String(3), nullable=False, default="USD")
    status = db.Column(db.String(20), nullable=False, default="paid")
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


class Subscriber(db.Model):
    __tablename__ = "subscribers"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
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


class ContactMessage(db.Model):
    __tablename__ = "contact_messages"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(255), nullable=False)
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
