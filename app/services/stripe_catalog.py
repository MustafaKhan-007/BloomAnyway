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
from urllib.parse import urlsplit

from flask import current_app

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
    rows = (Product.query
            .filter(Product.status != "archived")
            .order_by(Product.id)
            .all())
    for product in rows:
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
