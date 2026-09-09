"""A product bought for somebody else.

The buyer pays; the recipient gets the thing. Everything downstream of a
purchase — the library row, the reader, the free membership months a product
carries — hangs off the address on the :class:`ShopPurchase`, so a gift is
mostly a matter of writing the recipient's address there instead of the
buyer's. This module is the rest of it: who a gift is for, and telling both
of them where it went.

Nothing here decides whether a payment happened. It is handed a paid order.
"""
from __future__ import annotations

import logging

from sqlalchemy import func, or_

from ..extensions import db
from ..models import Order, User, utcnow

log = logging.getLogger(__name__)

#: as much of a message as goes with a gift, and as much as Stripe carries
#: in one metadata value with room to spare
NOTE_MAX = 400


def clean_note(raw: str | None) -> str:
    """A gift note as it will be stored and read: one block, no runaway."""
    text = "\n".join(line.strip() for line in (raw or "").splitlines())
    return text.strip()[:NOTE_MAX]


def clean_email(raw: str | None) -> str:
    return (raw or "").strip().lower()[:255]


def is_gift(order: Order | None) -> bool:
    """True when this order was bought for an address other than the payer's."""
    return order is not None and order.is_gift()


def holder_email(order: Order | None, paid_by: str) -> str:
    """Whose shelf this purchase belongs on.

    The buyer's, normally. A gift goes to the person it was bought for — that
    one line is what makes the reader, the library and the membership months
    all land on the right account, since every one of them follows the
    address on the purchase.
    """
    if is_gift(order):
        return clean_email(order.gift_to_email)
    return paid_by


def account_for_id(user_id: int | None) -> User | None:
    """A live account by id — who the buyer picked out of the previews."""
    if not user_id:
        return None
    user = db.session.get(User, int(user_id))
    return user if user is not None and user.deleted_at is None else None


def account_for(email: str | None) -> User | None:
    address = clean_email(email)
    if not address or "@" not in address:
        return None
    return (User.query
            .filter(func.lower(User.email) == address,
                    User.deleted_at.is_(None))
            .first())


def preview_for(user: User) -> dict:
    """The little card shown while somebody is choosing who to send to.

    Name, handle, face, tier — the same things their profile says out loud.
    Never the address: knowing one is what finds somebody here, and it isn't
    something this hands back.
    """
    from flask import url_for

    face = None
    try:
        if user.has_avatar():
            face = url_for("main.avatar", user_id=user.id)
        elif user.avatar_url:
            face = user.avatar_url
    except Exception:
        face = None
    return {
        "id": user.id,
        "name": user.public_name(),
        "handle": f"@{user.username}" if user.username else "",
        "tier": user.membership_label(),
        "avatar": face,
        "initials": user.initials(),
    }


def suggest(query: str, *, viewer: User | None = None,
            limit: int = 6) -> list[dict]:
    """Who somebody might be gifting to, from what they have typed so far.

    A whole address matches the account it belongs to and nothing else: a
    prefix of one matches nobody. Typing ``a@`` and reading off the members
    it turns up is how a list of everybody's email addresses gets made, and
    an address is the one thing here that has to be known beforehand rather
    than discovered.

    A name or a handle searches as it goes, the same way @mentions already
    do — those are on show anywhere anyone posts.
    """
    from .demo_accounts import is_demo_address
    from .social_graph import normalize_username

    text = (query or "").strip()
    if len(text) < 2:
        return []
    exclude = getattr(viewer, "id", None)
    base = [User.deleted_at.is_(None)]
    if exclude:
        base.append(User.id != exclude)

    if "@" in text:
        address = clean_email(text)
        if address.startswith("@"):
            # An @handle, not half an address.
            text = address.lstrip("@")
        elif "." not in address.split("@", 1)[1]:
            return []
        else:
            if is_demo_address(address):
                return []
            row = (User.query
                   .filter(*base, func.lower(User.email) == address)
                   .first())
            return [preview_for(row)] if row is not None else []

    handle = normalize_username(text)
    if not handle:
        return []
    uname = func.lower(func.coalesce(User.username, ""))
    dname = func.lower(func.coalesce(User.display_name, ""))
    starts = (uname.startswith(handle, autoescape=True),
              dname.startswith(handle, autoescape=True))
    rows = (User.query
            .filter(*base, or_(*starts))
            .order_by(starts[0].desc(), User.id)
            .limit(limit)
            .all())
    return [preview_for(row) for row in rows
            if not is_demo_address(row.email or "")]


def already_has(email: str, product) -> bool:
    """True when this address already owns the product being gifted."""
    from ..models import ShopPurchase
    from .course_reader import catalog_product_for_purchase

    address = clean_email(email)
    if not address or product is None:
        return False
    rows = (ShopPurchase.query
            .filter(func.lower(ShopPurchase.customer_email) == address,
                    ShopPurchase.status != "refunded")
            .all())
    for row in rows:
        try:
            held = catalog_product_for_purchase(row)
        except Exception:
            held = None
        if held is not None and held.id == product.id:
            return True
    return False


def _claim(order: Order) -> bool:
    """Take this gift, so two fulfils can't hand it over twice."""
    if getattr(order, "gift_told_at", None) is not None:
        return False
    order.gift_told_at = utcnow()
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        log.exception("gift: could not claim %s",
                      getattr(order, "ls_order_id", None))
        return False
    return True


def _sender_name(order: Order) -> str:
    """What the recipient is told to call whoever sent it.

    Their name if they have an account here, and the address they paid with
    if they don't — that address is the only thing the recipient has to
    recognise them by.
    """
    payer = account_for(getattr(order, "buyer_email", None))
    if payer is not None:
        return payer.public_name()
    return clean_email(getattr(order, "buyer_email", None)) or "Someone"


def tell_them(order: Order, *, product=None, name: str = "") -> bool:
    """Tell the recipient a gift arrived, and the buyer where it went.

    Claimed against the order first, so a webhook arriving twice doesn't send
    the same gift twice. Each side is told on its own: a bounce at one address
    must not take the other with it.
    """
    if not is_gift(order):
        return False
    if not _claim(order):
        log.info("gift: %s was already handed over",
                 getattr(order, "ls_order_id", None))
        return False

    title = (name or "").strip() or (product.title if product is not None else
                                     "") or "a product"
    note = clean_note(getattr(order, "gift_note", None))
    to_email = clean_email(order.gift_to_email)
    recipient = account_for(to_email)
    from_name = _sender_name(order)
    from_email = clean_email(getattr(order, "buyer_email", None))

    _tell_recipient(recipient, to_email, title=title, note=note,
                    from_name=from_name, from_email=from_email,
                    product=product)
    _tell_sender(order, title=title, to_email=to_email, recipient=recipient,
                 product=product)
    log.info("gift: %s → %s (%s)", from_email or "someone", to_email, title)
    return True


def _tell_recipient(recipient, to_email, *, title, note, from_name,
                    from_email, product) -> None:
    """The one who has been given something: a bell, and an email."""
    from .mailer import send_gift_received

    perk = product.perk_offer() if product is not None else ""
    # A guide arrives by email when it is bought. Gifted, it arrives with the
    # gift — the buyer's receipt goes out without it, since the guide is not
    # theirs.
    came_with = []
    try:
        from .assets import receipt_files
        came_with = receipt_files(product)
    except Exception:
        log.exception("gift: could not gather the files for %s", to_email)
    if recipient is not None:
        from .social_graph import notify
        body = f"{from_name} sent you “{title}” as a gift."
        if perk:
            body += f" It comes with {perk}."
        if note:
            body += f" They wrote: “{note}”"
        try:
            notify(recipient.id, kind="gift", body=body, url="/account")
        except Exception:
            log.exception("gift: could not put a note on %s's bell",
                          recipient.id)
    try:
        send_gift_received(
            to_email, product_name=title, from_name=from_name,
            from_email=from_email, note=note, perk=perk,
            has_account=recipient is not None,
            attachments=came_with,
        )
    except Exception:
        log.exception("gift: the email to %s did not go", to_email)


def _tell_sender(order, *, title, to_email, recipient, product=None) -> None:
    """The one who paid: it arrived, and whether it is being held."""
    from .mailer import send_gift_sent

    payer = account_for(getattr(order, "buyer_email", None))
    where = ""
    if product is not None and getattr(product, "slug", None):
        try:
            from flask import url_for
            where = url_for("main.course_detail", slug=product.slug,
                            _external=True)
        except Exception:
            where = ""
    to_name = recipient.public_name() if recipient is not None else to_email
    waiting = recipient is None
    if payer is not None:
        from .social_graph import notify
        if waiting:
            body = (f"Your gift of “{title}” is waiting for {to_email} — it "
                    "opens the moment they make an account with that address.")
        else:
            body = f"Your gift of “{title}” is on {to_name}'s shelf."
        try:
            notify(payer.id, kind="gift_sent", body=body, url="/account")
        except Exception:
            log.exception("gift: could not put a note on %s's bell", payer.id)
    try:
        send_gift_sent(clean_email(order.buyer_email), product_name=title,
                       to_name=to_name, to_email=to_email, waiting=waiting,
                       product_url=where)
    except Exception:
        log.exception("gift: the note to %s did not go", order.buyer_email)


def tell_sender_it_failed(order: Order, *, name: str = "",
                          why: str = "") -> None:
    """Say that a gift that was paid for did not reach anybody.

    Somebody has paid for this. Saying nothing leaves them believing it
    arrived, so they are told plainly, and so are the owners — this is theirs
    to put right by hand.
    """
    from .mailer import send_gift_stuck
    from .social_graph import notify, notify_owners

    title = (name or "").strip() or "your gift"
    to_email = clean_email(getattr(order, "gift_to_email", None))
    payer = account_for(getattr(order, "buyer_email", None))
    log.error("gift: %s did not reach %s (%s)",
              getattr(order, "ls_order_id", None), to_email, why or "no reason")
    if payer is not None:
        try:
            notify(payer.id, kind="gift_sent",
                   body=(f"Your gift of “{title}” hasn't reached {to_email} "
                         "yet. We know, and we're on it — nothing else for "
                         "you to do."),
                   url="/contact")
        except Exception:
            log.exception("gift: could not put a note on %s's bell", payer.id)
    try:
        notify_owners(
            kind="gift_sent",
            body=(f"A gift of “{title}” from "
                  f"{clean_email(getattr(order, 'buyer_email', None))} to "
                  f"{to_email} did not go through. Order "
                  f"{getattr(order, 'ls_order_id', '')}."),
            url="/admin/",
        )
    except Exception:
        log.exception("gift: could not tell the owners about %s",
                      getattr(order, "ls_order_id", None))
    try:
        send_gift_stuck(clean_email(getattr(order, "buyer_email", None)),
                        product_name=title, to_email=to_email)
    except Exception:
        log.exception("gift: could not tell %s it is stuck",
                      getattr(order, "buyer_email", None))
