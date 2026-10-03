"""Studio's half of the Stripe catalogue: products, pictures and prices.

Stripe is the till, not the shop. What a thing is called, what it says about
itself, what it looks like and what it costs are all decided in Studio, and
this keeps Stripe agreeing with that. Nobody should have to open two tabs and
copy an id between them to put something on sale.

The awkward part is the price, and it is awkward in Stripe rather than here:
a price object cannot be re-priced. The amount is fixed the moment it is
created. So "change the price" really means make a second price, point
checkout at that one, and archive the first. Everything below is arranged
around that one fact.

Which is also why :meth:`Product.retire_price_id` exists. Orders placed at
the old price still name the old id, and matching a purchase to the thing it
bought is how somebody gets to read what they paid for — so a price we have
stopped selling at is remembered, never dropped.

Nothing here raises into a request. An owner saving a product should always
get their product saved; a Stripe that is down or misconfigured is written
onto the row as ``stripe_sync_error`` and shown in Studio, not thrown at
somebody in the middle of writing a course.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from urllib.parse import urlsplit

from flask import current_app
from sqlalchemy import func

from ..extensions import db
from ..models import Product, utcnow
from . import stripe_pay as pay

log = logging.getLogger(__name__)

#: Stripe takes at most eight pictures on a product.
MAX_IMAGES = 8

#: Hosts Stripe could never fetch a picture from. Sending it a localhost URL
#: doesn't fail the call — it just puts a broken image on the checkout page,
#: which is worse than no image at all.
_PRIVATE_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "::1")


class CatalogError(RuntimeError):
    """Stripe refused something we asked for. Recorded, never raised on."""


def manages_catalog() -> bool:
    """Whether Studio is the one deciding what Stripe holds.

    Without a key there is nothing to talk to, and under the test suite we
    are not going to invent network calls — in both cases the owner keeps the
    old behaviour of typing a price id in by hand.
    """
    if current_app.config.get("TESTING"):
        return False
    return pay.configured()


# --- what Stripe ought to be holding -----------------------------------------

def public_base() -> str:
    """The address Stripe can reach us on, or empty if there isn't one.

    Deliberately not the request host: Studio is often open on localhost, and
    an image URL is fetched by Stripe's servers rather than by the browser
    that saved the form.
    """
    raw = (current_app.config.get("PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if not raw:
        return ""
    parts = urlsplit(raw)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host:
        return ""
    if host in _PRIVATE_HOSTS or host.endswith(".local"):
        return ""
    return raw


def image_urls(product: Product) -> list[str]:
    """The pictures already uploaded in Studio, as addresses Stripe can fetch.

    Cover first, then the teasers, because the cover is the one a buyer has
    been looking at all the way to the pay button.

    Each carries the product's own ``updated_at`` as a version. The cover
    lives at a fixed address, so without it Stripe would go on showing the
    picture that was there the first time it looked.
    """
    base = public_base()
    if not base or product is None or not product.id:
        return []
    stamp = int((product.updated_at or product.created_at or utcnow()).timestamp())
    paths = []
    cover = (product.cover_url or "").strip()
    if cover.startswith("/"):
        paths.append(cover)
    for url in product.gallery():
        if url.startswith("/") and url not in paths:
            paths.append(url)
    return [f"{base}{path}?v={stamp}" for path in paths[:MAX_IMAGES]]


def product_fields(product: Product) -> dict:
    """The Product payload Stripe should be holding for this row."""
    fields: dict = {
        "name": (product.title or "Untitled").strip()[:250] or "Untitled",
        "active": product.status != "archived",
        "metadata": {
            "bloom_product_id": str(product.id or ""),
            "slug": (product.slug or "")[:400],
        },
    }
    # Stripe treats an empty string as "clear it", which is what we want when
    # the owner deletes the blurb — but it refuses `None`.
    fields["description"] = product.stripe_blurb() or ""
    images = image_urls(product)
    if images:
        fields["images"] = images
    base = public_base()
    if base and (product.slug or "").strip():
        fields["url"] = f"{base}/courses/{product.slug}"
    return fields


def price_fields(product: Product) -> dict:
    """The Price payload for what this product costs today."""
    currency = (product.currency or "USD").strip().lower() or "usd"
    fields: dict = {
        "unit_amount": int(product.price_cents or 0),
        "currency": currency,
        "nickname": f"{(product.title or 'Product')[:180]} — {product.billing_label()}",
        "metadata": {"bloom_product_id": str(product.id or "")},
        # A stable name for "whatever this product costs now", carried from
        # the old price to the new one in the same call that makes the new
        # one. Handy in the Stripe dashboard, and it means a price id is
        # never the only way back to a product.
        "lookup_key": f"bloom_product_{int(product.id)}",
        "transfer_lookup_key": True,
    }
    interval = product.stripe_interval()
    if interval:
        fields["recurring"] = {"interval": interval[0],
                               "interval_count": interval[1]}
    return fields


def _price_is_right(price: dict | None, product: Product) -> bool:
    """Whether an existing Stripe price already charges what Studio says."""
    if not isinstance(price, dict) or not price:
        return False
    if price.get("active") is False:
        return False
    if int(price.get("unit_amount") or -1) != int(product.price_cents or 0):
        return False
    want_currency = (product.currency or "USD").strip().lower() or "usd"
    if str(price.get("currency") or "").strip().lower() != want_currency:
        return False
    recurring = price.get("recurring")
    interval = product.stripe_interval()
    if interval is None:
        return not recurring
    if not isinstance(recurring, dict):
        return False
    return (str(recurring.get("interval") or "") == interval[0]
            and int(recurring.get("interval_count") or 1) == interval[1])


# --- talking to Stripe --------------------------------------------------------

def _fetch_price(price_id: str) -> dict | None:
    """One price, or None if Stripe has never heard of it."""
    try:
        return pay._as_dict(pay.stripe.Price.retrieve(price_id))
    except Exception as exc:
        if "no such price" in str(exc).lower():
            return None
        raise


def _ensure_product(product: Product) -> str:
    """The ``prod_…`` for this row, made or adopted, and brought up to date."""
    fields = product_fields(product)
    existing = (product.stripe_product_id or "").strip()

    # Nothing recorded, but a price was pasted in by hand once. That price
    # belongs to a product already, and making a second one beside it would
    # leave the owner with two of everything in Stripe.
    if not existing and (product.stripe_price_id or "").strip():
        price = _fetch_price(product.stripe_price_id.strip())
        if price:
            found = pay._stripe_id(price.get("product"))
            if found:
                existing = found
                log.info("stripe catalog: adopted product %s for %s",
                         existing, product.slug)

    if existing:
        pay.stripe.Product.modify(existing, **fields)
        return existing
    made = pay._as_dict(pay.stripe.Product.create(**fields))
    new_id = pay._stripe_id(made.get("id")) or str(made.get("id") or "")
    if not new_id:
        raise CatalogError("Stripe made a product but gave back no id.")
    log.info("stripe catalog: created product %s for %s", new_id, product.slug)
    return new_id


def _ensure_price(product: Product, stripe_product_id: str, report: dict) -> str:
    """The price checkout should use, making a new one if the old is wrong."""
    current = (product.stripe_price_id or "").strip()
    if current:
        price = _fetch_price(current)
        if price and _price_is_right(price, product):
            return current
        if price is None:
            # The id on the row points at nothing. Worth saying out loud: it
            # is the difference between "we changed the price" and "somebody
            # deleted this in Stripe and checkout has been failing since".
            log.warning("stripe catalog: %s pointed at missing price %s",
                        product.slug, current)

    made = pay._as_dict(pay.stripe.Price.create(
        product=stripe_product_id, **price_fields(product)))
    new_id = pay._stripe_id(made.get("id")) or str(made.get("id") or "")
    if not new_id:
        raise CatalogError("Stripe made a price but gave back no id.")
    report["created_price"] = True

    if current and current != new_id:
        product.retire_price_id(current)
        report["retired"] = current
        # Archived rather than deleted: it is what every order placed before
        # today was charged at, and Stripe's own history needs it to stay.
        try:
            pay.stripe.Price.modify(current, active=False)
        except Exception:
            log.warning("stripe catalog: could not archive old price %s",
                        current, exc_info=True)
    log.info("stripe catalog: %s now sells on %s", product.slug, new_id)
    return new_id


def sync_product(product: Product) -> dict:
    """Make Stripe hold what Studio says about this product. Caller commits.

    Returns a small report rather than raising, so a save is never lost to a
    Stripe that is having a bad afternoon.
    """
    report = {"ok": False, "skipped": "", "created_price": False,
              "retired": None, "error": "", "price_id": "",
              "product_id": ""}
    if product is None:
        report["skipped"] = "no-product"
        return report
    if not manages_catalog():
        report["skipped"] = "stripe-off"
        return report
    if product.price_cents is None:
        # Nothing to sell yet. Not a failure — most products are saved once
        # with a title and nothing else while they are being written.
        report["skipped"] = "no-price"
        return report

    try:
        pay._configure_stripe()
        stripe_product_id = _ensure_product(product)
        price_id = _ensure_price(product, stripe_product_id, report)
    except Exception as exc:
        message = " ".join(str(exc).split())[:300]
        product.stripe_sync_error = message or "Stripe refused the change."
        log.exception("stripe catalog: could not sync %s", product.slug)
        report["error"] = product.stripe_sync_error
        return report

    product.stripe_product_id = stripe_product_id
    product.stripe_price_id = price_id
    product.stripe_synced_at = utcnow()
    product.stripe_sync_error = None
    report.update(ok=True, price_id=price_id, product_id=stripe_product_id)
    return report


# --- launch prices that put themselves back up --------------------------------
# A launch price used to be a promise the owner had to keep by hand: the
# product page counted down, the countdown hit zero, and Studio told her to go
# and change two things. Miss it by a day and the launch price was still being
# charged, which is the one mistake here that costs money every time somebody
# buys.
#
# Now the date does it. The order below is the whole of the care needed: the
# new Stripe price is made *first*, and what the page says only moves once
# Stripe has agreed to charge it. Fail the other way round and the page
# advertises a price Stripe will not take.

#: The longest a passing request will go without looking, when there is
#: nothing known to be waiting.
_REVERSION_IDLE_SEC = 60
#: When the next look is allowed, on the monotonic clock. Worked out from the
#: soonest date actually waiting rather than from a fixed gap — a plain
#: every-sixty-seconds throttle gets spent by a request a moment *before* the
#: date, and then the price sits at the launch figure for the rest of the
#: minute while the page is telling people it has gone up.
_next_reversion_check = 0.0


def due_reversions() -> list[Product]:
    """Products whose launch window has closed and that named a new price.

    A date with no figure beside it is a countdown and nothing more — the
    owner is saying "this goes up soon" without having decided to what, and
    guessing on her behalf is not ours to do.
    """
    return (Product.query
            .filter(Product.price_reverts_at.isnot(None),
                    Product.price_reverts_at <= utcnow(),
                    Product.reverts_to_cents.isnot(None),
                    Product.reverts_to_cents > 0,
                    Product.status != "archived")
            .order_by(Product.id).all())


def reversion_lock_query(product_id: int):
    """The locking read used before putting a price up.

    Split out so a test can check the lock is still asked for: SQLite drops
    ``FOR UPDATE`` silently, so running this proves nothing on its own. Two
    workers reaching the same due product at the same moment would otherwise
    both make a price in Stripe.
    """
    return Product.query.filter_by(id=product_id).with_for_update()


def apply_reversion(product: Product) -> dict:
    """Put one product's price up to what the launch said it would be.

    Caller commits. Returns a small report; raises nothing, because this runs
    off an ordinary page request and a Stripe that is down must not turn
    somebody's visit into a 500.
    """
    report = {"ok": False, "changed": False, "slug": product.slug or "",
              "was": product.price_cents, "now": product.price_cents,
              "error": "", "stripe": ""}
    want = int(product.reverts_to_cents or 0)
    if want <= 0:
        return report

    was = product.price_cents
    if was == want:
        # Already charging it — she got there first, or this is a retry after
        # Stripe took the price but the save didn't land. Either way the only
        # thing left is to take the countdown down.
        product.price_reverts_at = None
        product.reverts_to_cents = None
        report.update(ok=True, now=want)
        return report

    product.price_cents = want
    sync = sync_product(product)
    if sync["error"]:
        # Stripe wouldn't take it. Put the price back and leave the date
        # where it is: the launch runs a little long, which is the harmless
        # direction, and the next sweep tries again. ``sync_product`` has
        # already written the reason onto the row for Studio to show.
        product.price_cents = was
        report["error"] = sync["error"]
        return report

    product.price_reverts_at = None
    product.reverts_to_cents = None
    report.update(ok=True, changed=True, now=want,
                  stripe=sync.get("price_id") or "")
    log.info("stripe catalog: %s reverted to %s (%s)", product.slug, want,
             sync.get("skipped") or sync.get("price_id") or "")
    return report


def apply_due_reversions() -> dict:
    """Put up the price of everything whose launch window has closed."""
    tally = {"checked": 0, "reverted": 0, "failed": 0, "problems": []}
    due = due_reversions()
    if not due:
        return tally
    for found in due:
        tally["checked"] += 1
        # Re-read with the row held, and check it is still due: another
        # worker may have finished this one between the list and here.
        product = reversion_lock_query(found.id).first()
        if product is None or not product.price_reverts_at \
                or product.price_reverts_at > utcnow() \
                or not product.reverts_to_cents:
            db.session.rollback()
            continue
        report = apply_reversion(product)
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            tally["failed"] += 1
            log.exception("stripe catalog: could not save reversion for %s",
                          found.slug)
            continue
        if report["ok"]:
            tally["reverted"] += 1
        else:
            tally["failed"] += 1
            if len(tally["problems"]) < 5:
                tally["problems"].append(
                    f"{found.title}: {report['error'] or 'not applied'}")
    return tally


def earliest_pending() -> datetime | None:
    """The soonest a launch price is due to go up, or None if none is.

    One cheap scalar, used to decide when to bother looking again.
    """
    return (db.session.query(func.min(Product.price_reverts_at))
            .filter(Product.price_reverts_at.isnot(None),
                    Product.reverts_to_cents.isnot(None),
                    Product.reverts_to_cents > 0,
                    Product.status != "archived")
            .scalar())


def maybe_apply_reversions() -> dict:
    """Look if it is worth looking, and put up whatever is due.

    Safe to call from any request. When nothing is waiting this costs one
    scalar query a minute; when something is, it wakes within a second of
    the moment rather than whenever a fixed window happens to roll over.
    """
    global _next_reversion_check
    if time.monotonic() < _next_reversion_check:
        return {}
    try:
        result = apply_due_reversions()
        soonest = earliest_pending()
        if soonest is None:
            gap = _REVERSION_IDLE_SEC
        else:
            ahead = (soonest - utcnow()).total_seconds()
            # Still in the past after a sweep means Stripe refused it. Back
            # off rather than asking it the same question every second.
            gap = (_REVERSION_IDLE_SEC if ahead <= 0
                   else max(1.0, min(_REVERSION_IDLE_SEC, ahead)))
        _next_reversion_check = time.monotonic() + gap
        return result
    except Exception:
        # Don't let a bad query turn every page into a retry storm.
        _next_reversion_check = time.monotonic() + _REVERSION_IDLE_SEC
        log.exception("stripe catalog: price reversion sweep failed")
        try:
            db.session.rollback()
        except Exception:
            pass
        return {}


def sync_all() -> dict:
    """Bring the whole catalogue into step. For the button in Studio.

    Products written before Studio managed any of this have a price id typed
    in by hand and no product id at all; this adopts them where it can and
    creates what it can't, so nobody has to open thirty pages and press save
    on each one.
    """
    tally = {"checked": 0, "synced": 0, "priced": 0, "skipped": 0,
             "failed": 0, "problems": []}
    if not manages_catalog():
        tally["off"] = True
        return tally
    for product in Product.query.order_by(Product.id).all():
        # Archived products are worth a pass so the archiving reaches Stripe,
        # but only ones Stripe already knows about. Something taken off the
        # books here has no business being created there for the first time.
        if product.status == "archived" and not (product.stripe_product_id or ""):
            continue
        tally["checked"] += 1
        report = sync_product(product)
        if report["ok"]:
            tally["synced"] += 1
            if report["created_price"]:
                tally["priced"] += 1
        elif report["error"]:
            tally["failed"] += 1
            if len(tally["problems"]) < 5:
                tally["problems"].append(f"{product.title}: {report['error']}")
        else:
            tally["skipped"] += 1
        # One product at a time: a catalogue of thirty shouldn't lose the
        # twenty-nine that worked because the thirtieth was refused.
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            log.exception("stripe catalog: could not save sync for %s",
                          product.slug)
    return tally
