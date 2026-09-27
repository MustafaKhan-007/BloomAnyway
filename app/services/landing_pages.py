"""Landing pages the owner builds herself, block by block.

A page is a list of blocks, each one a type and a bag of fields, kept as JSON.
Nothing here is HTML: the editor sends back the *words*, and
``partials/landing_blocks.html`` is the only thing that turns them into
markup. That is what keeps a page built in Studio from being a way to put
script on the site — everything below is text, a picked-from-a-list choice,
or a URL that has been looked at.

Every page has two copies of itself. ``draft_json`` is what Studio shows and
Save writes; ``published_json`` is what visitors get, and only Publish moves
one to the other. A page that has never been published is not on the site at
all — so a half-built page is not something a visitor can stumble into, and
the owner can leave one half-built for a week without hiding it first.
"""
from __future__ import annotations

import json
import re
import secrets
from urllib.parse import urlparse

from ..extensions import db
from ..models import LandingPage, utcnow

#: Field kinds. ``line`` and ``rich`` are edited on the page itself and always
#: come back as plain text — the editor sends innerText, never innerHTML.
#: The rest are edited in the side panel.
LINE, RICH, URL, IMAGE, CHOICE = "line", "rich", "url", "image", "choice"

MAX_LINE = 300
MAX_RICH = 4000
MAX_URL = 500
MAX_BLOCKS = 60
MAX_ITEMS = 20

#: Background treatments a block may be given. The value is a class suffix;
#: anything not on this list is dropped back to the default.
BACKGROUNDS = ("cream", "soft", "plum", "dark")


def _bg(default="cream"):
    return {"kind": CHOICE, "options": BACKGROUNDS, "default": default,
            "label": "Background"}


#: Every block the builder can make, and every field it may carry. A field
#: that isn't here is thrown away on save, so a hand-made POST can't smuggle
#: one in.
BLOCK_DEFS: dict[str, dict] = {
    "hero": {
        "label": "Hero",
        "hint": "Big opening — headline, a line under it, one button.",
        "fields": {
            "eyebrow": {"kind": LINE, "default": "A NEW ROUND IS OPEN"},
            "heading": {"kind": LINE, "default": "You don't need an audience.\nYou need a plan."},
            "body": {"kind": RICH, "default": "Eight weeks, four stages, and a room full of women doing it with you."},
            "button_text": {"kind": LINE, "default": "Join the challenge"},
            "button_url": {"kind": URL, "default": "/courses"},
            "image": {"kind": IMAGE, "default": "", "label": "Background image"},
            "bg": _bg("plum"),
        },
    },
    "text": {
        "label": "Text",
        "hint": "A heading and a paragraph.",
        "fields": {
            "heading": {"kind": LINE, "default": "A heading goes here"},
            "body": {"kind": RICH, "default": "And the words that go under it. Write as much or as little as you like — press Enter for a new line."},
            "align": {"kind": CHOICE, "options": ("left", "center"),
                      "default": "left", "label": "Alignment"},
            "bg": _bg(),
        },
    },
    "image": {
        "label": "Image",
        "hint": "One picture, with an optional caption.",
        "fields": {
            "image": {"kind": IMAGE, "default": ""},
            "caption": {"kind": LINE, "default": ""},
            "width": {"kind": CHOICE, "options": ("narrow", "wide", "full"),
                      "default": "wide", "label": "Width"},
            "bg": _bg(),
        },
    },
    "image_text": {
        "label": "Image & text",
        "hint": "A picture beside words.",
        "fields": {
            "image": {"kind": IMAGE, "default": ""},
            "heading": {"kind": LINE, "default": "Something worth showing"},
            "body": {"kind": RICH, "default": "Put the picture on whichever side reads better."},
            "button_text": {"kind": LINE, "default": ""},
            "button_url": {"kind": URL, "default": ""},
            "side": {"kind": CHOICE, "options": ("left", "right"),
                     "default": "left", "label": "Picture on the"},
            "bg": _bg(),
        },
    },
    "stats": {
        "label": "Numbers",
        "hint": "A row of figures worth shouting about.",
        "fields": {
            "heading": {"kind": LINE, "default": ""},
            "bg": _bg("soft"),
        },
        "items": {
            "label": "Number",
            "max": 4,
            "fields": {
                "value": {"kind": LINE, "default": "100+"},
                "label": {"kind": LINE, "default": "women through it"},
            },
            "default_count": 3,
        },
    },
    "features": {
        "label": "Cards",
        "hint": "What's inside, one card each.",
        "fields": {
            "heading": {"kind": LINE, "default": "What's inside"},
            "body": {"kind": RICH, "default": ""},
            "columns": {"kind": CHOICE, "options": ("2", "3"), "default": "3",
                        "label": "Cards per row"},
            "bg": _bg(),
        },
        "items": {
            "label": "Card",
            "max": 12,
            "fields": {
                "title": {"kind": LINE, "default": "Stage one"},
                "body": {"kind": RICH, "default": "What happens in it, in a sentence or two."},
            },
            "default_count": 3,
        },
    },
    "quote": {
        "label": "Quote",
        "hint": "Someone else's words.",
        "fields": {
            "quote": {"kind": RICH, "default": "I came in with nothing to sell and left with something people wanted."},
            "attribution": {"kind": LINE, "default": "— A member"},
            "bg": _bg("soft"),
        },
    },
    "faq": {
        "label": "Questions",
        "hint": "The things people ask before they buy.",
        "fields": {
            "heading": {"kind": LINE, "default": "Before you join"},
            "bg": _bg(),
        },
        "items": {
            "label": "Question",
            "max": MAX_ITEMS,
            "fields": {
                "question": {"kind": LINE, "default": "How much time does it take?"},
                "answer": {"kind": RICH, "default": "An hour or two a week, and you keep everything afterwards."},
            },
            "default_count": 3,
        },
    },
    "cta": {
        "label": "Call to action",
        "hint": "The ask, on its own.",
        "fields": {
            "heading": {"kind": LINE, "default": "Ready when you are"},
            "body": {"kind": RICH, "default": "Doors are open now."},
            "button_text": {"kind": LINE, "default": "Join now"},
            "button_url": {"kind": URL, "default": "/courses"},
            "bg": _bg("plum"),
        },
    },
    "divider": {
        "label": "Divider",
        "hint": "A little breathing room.",
        "fields": {
            "bg": _bg(),
        },
    },
}

#: The order they appear in the "add a block" menu.
BLOCK_ORDER = ("hero", "text", "image", "image_text", "features", "stats",
               "quote", "faq", "cta", "divider")


class LandingPageError(ValueError):
    pass


# --- making blocks -----------------------------------------------------------

def _new_id() -> str:
    return "b" + secrets.token_hex(6)


def new_item(block_type: str) -> dict:
    """One fresh repeating item for a block that has them."""
    spec = (BLOCK_DEFS.get(block_type) or {}).get("items")
    if not spec:
        return {}
    return {key: field["default"] for key, field in spec["fields"].items()}


def new_block(block_type: str) -> dict:
    """A block of this type, filled with its placeholder words."""
    spec = BLOCK_DEFS.get(block_type)
    if spec is None:
        raise LandingPageError("There's no block of that kind.")
    block = {"id": _new_id(), "type": block_type,
             "fields": {k: f["default"] for k, f in spec["fields"].items()}}
    if spec.get("items"):
        count = spec["items"].get("default_count", 1)
        block["items"] = [new_item(block_type) for _ in range(count)]
    return block


def default_blocks() -> list[dict]:
    """The page a brand-new landing page starts as.

    Deliberately a whole page rather than an empty canvas: something already
    laid out is far easier to make your own than a blank screen with an
    "add a block" button on it.
    """
    blocks = [new_block(t) for t in
              ("hero", "stats", "text", "features", "image_text", "quote",
               "faq", "cta")]
    return blocks


# --- cleaning what the editor sends back --------------------------------------

def _clean_line(value) -> str:
    text = str(value if value is not None else "")
    # Editors leave non-breaking spaces behind; they read as spaces but don't
    # compare or wrap like them.
    text = text.replace(" ", " ").replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.strip() for line in text.split("\n"))
    return text.strip()[:MAX_LINE]


def _clean_rich(value) -> str:
    text = str(value if value is not None else "")
    text = text.replace(" ", " ").replace("\r\n", "\n").replace("\r", "\n")
    # Any run of blank lines is one blank line; nobody means seven.
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:MAX_RICH]


def clean_url(value) -> str:
    """A link we are willing to put on a page, or "".

    Relative paths and ordinary web addresses only. ``javascript:`` is the
    reason this function exists; ``data:`` is the other one.
    """
    raw = str(value if value is not None else "").strip()[:MAX_URL]
    if not raw:
        return ""
    if raw.startswith("//"):          # protocol-relative: whose site is it?
        return ""
    if raw.startswith("/") or raw.startswith("#"):
        return raw
    parsed = urlparse(raw)
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return raw
    if not parsed.scheme and parsed.path and "." in parsed.path.split("/")[0]:
        return "https://" + raw       # they typed bloomanyway.online
    return ""


def _clean_image(value) -> str:
    """An image address: one of ours, or somewhere else on the web."""
    return clean_url(value)


def _clean_choice(value, options, default) -> str:
    text = str(value if value is not None else "").strip()
    return text if text in options else default


def _clean_field(spec: dict, value):
    kind = spec["kind"]
    if kind == LINE:
        return _clean_line(value)
    if kind == RICH:
        return _clean_rich(value)
    if kind == URL:
        return clean_url(value)
    if kind == IMAGE:
        return _clean_image(value)
    if kind == CHOICE:
        return _clean_choice(value, spec["options"], spec["default"])
    return ""


def normalize_block(raw) -> dict | None:
    """One block from the editor, with everything we don't know thrown away."""
    if not isinstance(raw, dict):
        return None
    block_type = str(raw.get("type") or "").strip()
    spec = BLOCK_DEFS.get(block_type)
    if spec is None:
        return None

    raw_fields = raw.get("fields")
    if not isinstance(raw_fields, dict):
        raw_fields = {}
    fields = {}
    for key, field_spec in spec["fields"].items():
        if key in raw_fields:
            fields[key] = _clean_field(field_spec, raw_fields[key])
        else:
            fields[key] = field_spec["default"]

    block_id = str(raw.get("id") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", block_id):
        block_id = _new_id()
    block = {"id": block_id, "type": block_type, "fields": fields}

    item_spec = spec.get("items")
    if item_spec:
        raw_items = raw.get("items")
        raw_items = raw_items if isinstance(raw_items, list) else []
        items = []
        for raw_item in raw_items[: item_spec.get("max", MAX_ITEMS)]:
            if not isinstance(raw_item, dict):
                continue
            items.append({
                key: _clean_field(field_spec, raw_item.get(key,
                                                           field_spec["default"]))
                for key, field_spec in item_spec["fields"].items()
            })
        block["items"] = items
    return block


def normalize_blocks(raw) -> list[dict]:
    """The whole page from the editor, cleaned. Unknown blocks are dropped."""
    if not isinstance(raw, list):
        return []
    out = []
    seen_ids = set()
    for entry in raw[:MAX_BLOCKS]:
        block = normalize_block(entry)
        if block is None:
            continue
        # Two blocks with one id would make the editor's own bookkeeping lie.
        while block["id"] in seen_ids:
            block["id"] = _new_id()
        seen_ids.add(block["id"])
        out.append(block)
    return out


def blocks_from_json(text: str | None) -> list[dict]:
    """Read stored JSON back, forgiving anything that isn't readable.

    A page whose JSON will not parse renders as an empty page rather than a
    500 — the owner can still open the editor and put it right.
    """
    if not text:
        return []
    try:
        return normalize_blocks(json.loads(text))
    except (ValueError, TypeError):
        return []


def blocks_json(blocks: list[dict]) -> str:
    return json.dumps(blocks, ensure_ascii=False, separators=(",", ":"))


# --- slugs -------------------------------------------------------------------

#: Paths a landing page may not sit on, because something else already answers
#: there and the landing page would shadow it.
RESERVED_SLUGS = {
    "new", "admin", "api", "static", "media", "login", "logout", "register",
    "account", "settings", "courses", "membership", "watch", "forums",
    "marketplace", "showcase", "about", "contact", "faq", "quotes", "privacy",
    "terms", "refunds", "challenge", "healthz", "setup", "gift", "library",
    "checkout", "cron", "webhooks", "u", "p",
}


def slugify(text: str, fallback: str = "page") -> str:
    base = re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower()).strip("-")
    base = base[:60].strip("-") or fallback
    if base in RESERVED_SLUGS:
        base = base + "-page"
    return base


def unique_slug(text: str, *, exclude_id: int | None = None) -> str:
    """A slug nothing else is using."""
    base = slugify(text)
    candidate = base
    n = 2
    while True:
        query = LandingPage.query.filter(LandingPage.slug == candidate)
        if exclude_id is not None:
            query = query.filter(LandingPage.id != exclude_id)
        if query.first() is None:
            return candidate
        candidate = f"{base}-{n}"[:70]
        n += 1


# --- the pages themselves ------------------------------------------------------

def create(title: str = "") -> LandingPage:
    """A new page, already laid out and not yet published."""
    name = _clean_line(title) or "Untitled landing page"
    page = LandingPage(
        title=name[:160],
        slug=unique_slug(name),
        draft_json=blocks_json(default_blocks()),
        published_json="",
    )
    db.session.add(page)
    db.session.flush()
    return page


def save_draft(page: LandingPage, blocks_raw, *, title=None, slug=None) -> None:
    """Write what the editor sent to the draft. Visitors see none of it."""
    page.draft_json = blocks_json(normalize_blocks(blocks_raw))
    if title is not None:
        cleaned = _clean_line(title)
        if cleaned:
            page.title = cleaned[:160]
    if slug is not None:
        wanted = slugify(slug or page.title)
        if wanted != page.slug:
            page.slug = unique_slug(wanted, exclude_id=page.id)
    page.updated_at = utcnow()


def publish(page: LandingPage) -> None:
    """Hand the draft to the public, exactly as it stands."""
    page.published_json = page.draft_json or blocks_json([])
    page.published_at = utcnow()
    page.updated_at = utcnow()


def unpublish(page: LandingPage) -> None:
    """Take it off the site. The draft is untouched, so nothing is lost."""
    page.published_json = ""
    page.published_at = None
    page.updated_at = utcnow()


def delete(page: LandingPage) -> None:
    db.session.delete(page)
    db.session.flush()
    sweep_unused_images()


# --- pictures ------------------------------------------------------------------

#: Uploaded landing-page pictures are ``SiteImage`` rows under this prefix, so
#: they survive a redeploy the way avatars do rather than living on a disk
#: that gets wiped. The key's shape is site_images' business — it is what
#: decides whether an address is one it will hand bytes out for.
from .site_images import FREEFORM_PREFIX as IMAGE_PREFIX  # noqa: E402
from .site_images import new_freeform_key as new_image_key  # noqa: E402


def referenced_image_keys() -> set[str]:
    """Every uploaded picture some page still points at, draft or published.

    A draft counts. Removing a picture and saving, then changing your mind
    before you publish, should not be the moment the file goes.
    """
    keys: set[str] = set()
    for draft, published in db.session.query(LandingPage.draft_json,
                                             LandingPage.published_json).all():
        for text in (draft, published):
            for block in blocks_from_json(text):
                for value in list(block.get("fields", {}).values()):
                    if isinstance(value, str) and IMAGE_PREFIX in value:
                        keys.add(value.rsplit("/", 1)[-1])
    return keys


def sweep_unused_images() -> int:
    """Drop uploaded pictures no page points at any more."""
    from ..models import SiteImage

    keep = referenced_image_keys()
    rows = (SiteImage.query
            .filter(SiteImage.key.like(IMAGE_PREFIX + "%")).all())
    cleared = 0
    for row in rows:
        if row.key in keep:
            continue
        db.session.delete(row)
        cleared += 1
    return cleared


# --- what the templates ask for ---------------------------------------------

def public_page(slug: str) -> LandingPage | None:
    """A published page by slug, or None."""
    page = LandingPage.query.filter_by(slug=slug).first()
    if page is None or not page.published_json:
        return None
    return page


def editor_context() -> dict:
    """What the builder needs to know about blocks, for its own menus.

    ``fields`` is a list rather than a map on purpose. It is handed over as
    JSON, and a JSON object's keys get sorted on the way — which put the side
    panel in alphabetical order (Background, Button, Image) instead of the
    order the fields are declared in. A list keeps the order we meant.
    """
    return {
        "order": list(BLOCK_ORDER),
        "defs": {
            key: {
                "label": spec["label"],
                "hint": spec.get("hint", ""),
                "fields": [
                    {"key": k,
                     "kind": f["kind"],
                     "label": f.get("label", k.replace("_", " ").capitalize()),
                     "options": list(f.get("options", ()))}
                    for k, f in spec["fields"].items()
                ],
                "items": bool(spec.get("items")),
                "item_label": (spec.get("items") or {}).get("label", "Item"),
                "item_max": (spec.get("items") or {}).get("max", 0),
                # What an added item starts as, so the browser can put one in
                # without asking us what a blank one looks like.
                "item_defaults": new_item(key),
            }
            for key, spec in BLOCK_DEFS.items()
        },
    }
