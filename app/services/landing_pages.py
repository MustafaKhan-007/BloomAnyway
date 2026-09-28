"""Landing pages the owner builds herself, block by block.

A page is a list of blocks, each one a type and a bag of fields, kept as JSON,
plus a few page-wide settings. ``partials/landing_blocks.html`` is the only
thing that turns any of it into markup.

**On formatting.** Text fields hold a small, fixed set of HTML — bold, italic,
underline, strikethrough, a link, a list, a coloured span — and nothing else.
Everything that arrives is put through bleach against the allow-lists below,
on the way in *and* again on the way out. ``strip=False`` is deliberate: a tag
we don't allow is escaped rather than deleted, so it shows as the words
somebody typed instead of vanishing, and text stored back when these fields
were plain reads exactly as it did then.

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

import bleach
from markupsafe import Markup

from ..extensions import db
from ..models import LandingPage, utcnow

#: Field kinds. ``line`` and ``rich`` are typed on the page itself; the rest
#: are set in the side panel.
LINE, RICH, URL, IMAGE, CHOICE, NUMBER = (
    "line", "rich", "url", "image", "choice", "number")
#: A colour of her own, and a name to link to. Both end up in an attribute
#: rather than between tags, so both are cleaned down to a shape that cannot
#: carry anything but what they are for.
COLOR, ANCHOR = "color", "anchor"

MAX_LINE = 1200      # generous: the cap is on markup, not on words
MAX_RICH = 20000
MAX_URL = 500
MAX_BLOCKS = 80
MAX_ITEMS = 24


# --- the formatting a text field may carry -----------------------------------

#: Inline marks. ``b``/``i`` as well as ``strong``/``em`` because which one a
#: browser writes for Ctrl+B is its own business.
_INLINE_TAGS = ["strong", "b", "em", "i", "u", "s", "mark", "span", "br"]
#: Blocks, for the fields with room for them.
_BLOCK_TAGS = ["p", "ul", "ol", "li", "a"]

LINE_TAGS = _INLINE_TAGS + ["a"]
RICH_TAGS = _INLINE_TAGS + _BLOCK_TAGS

#: Colours and sizes a span may carry. Classes, not inline styles — a style
#: attribute is a whole grammar to have to police, and this is six words.
TEXT_CLASSES = (
    "lp-t--plum", "lp-t--berry", "lp-t--rose", "lp-t--gold", "lp-t--muted",
    "lp-t--white", "lp-t--big", "lp-t--small", "lp-t--caps",
)


def _span_class_ok(tag, name, value):
    if name != "class":
        return False
    return all(part in TEXT_CLASSES for part in str(value).split())


def _link_attr_ok(tag, name, value):
    if name == "href":
        return bool(clean_url(value))
    return name in ("target", "rel", "title")


_ATTRS = {"span": _span_class_ok, "a": _link_attr_ok}
_PROTOCOLS = ["http", "https", "mailto"]

#: A browser writes a new line in a contenteditable as a div, and div is not
#: a tag worth allowing. Turned into what it means before cleaning.
_DIV_OPEN = re.compile(r"<div\b[^>]*>", re.I)
_DIV_CLOSE = re.compile(r"</div\s*>", re.I)
_BLOCK_SPLIT = re.compile(r"</(?:p|div|li)\s*>\s*<(?:p|div|li)\b[^>]*>", re.I)
_ANY_BLOCK = re.compile(r"</?(?:p|div|ul|ol|li)\b[^>]*>", re.I)


def _sanitize(raw, tags, *, single_line: bool) -> str:
    """Clean one text field down to the marks we allow."""
    text = str(raw if raw is not None else "")
    text = text.replace(" ", " ").replace("\r\n", "\n").replace("\r", "\n")
    if single_line:
        # One line of writing: paragraph breaks become line breaks, and any
        # block tag left over goes, so a heading can never contain a list.
        text = _BLOCK_SPLIT.sub("<br>", text)
        text = _ANY_BLOCK.sub("", text)
    else:
        text = _DIV_OPEN.sub("<p>", text)
        text = _DIV_CLOSE.sub("</p>", text)
    cleaned = bleach.clean(text, tags=tags, attributes=_ATTRS,
                           protocols=_PROTOCOLS, strip=False)
    # A field holding only empty markup is an empty field, so the editor's
    # placeholder shows instead of a blank box nobody can find.
    if not bleach.clean(cleaned, tags=[], strip=True).strip():
        if "<img" not in cleaned:
            return ""
    return cleaned.strip()


def clean_line(value) -> str:
    return _sanitize(value, LINE_TAGS, single_line=True)[:MAX_LINE]


def clean_rich(value) -> str:
    return _sanitize(value, RICH_TAGS, single_line=False)[:MAX_RICH]


def render_text(value, *, rich: bool = True) -> Markup:
    """What the templates print. Cleaned again here, on purpose.

    Cleaning on save is what makes the stored page safe; cleaning again on
    the way out is what makes a row written by an older version of this file,
    or edited by hand in the database, safe too. It costs a few microseconds
    on a string the length of a headline.
    """
    tags = RICH_TAGS if rich else LINE_TAGS
    return Markup(_sanitize(value, tags, single_line=not rich))


def strip_marks(value) -> str:
    """The words with no formatting — for titles, previews and alt text."""
    return bleach.clean(str(value or ""), tags=[], strip=True).strip()


#: Background treatments a block may be given. The value is a class suffix;
#: anything not on this list is dropped back to the default.
BACKGROUNDS = ("cream", "soft", "plum", "dark", "accent", "none")
PADDINGS = ("none", "small", "medium", "large", "huge")
ALIGNMENTS = ("left", "center", "right")
WIDTHS = ("narrow", "normal", "wide", "full")


def _bg(default="cream"):
    return {"kind": CHOICE, "options": BACKGROUNDS, "default": default,
            "label": "Background"}


def _pad(default="medium"):
    return {"kind": CHOICE, "options": PADDINGS, "default": default,
            "label": "Space around"}


def _align(default="left"):
    return {"kind": CHOICE, "options": ALIGNMENTS, "default": default,
            "label": "Alignment"}


def _width(default="normal"):
    return {"kind": CHOICE, "options": WIDTHS, "default": default,
            "label": "Content width"}


#: What every block carries whatever kind it is. Declared once rather than
#: repeated down each of the fifteen definitions below, and merged in
#: wherever a block's fields are read.
#:
#: ``visible`` is a hide, not a delete. Taking a section off the page for a
#: fortnight used to mean deleting it and writing it again afterwards, so
#: people kept a second copy of the page instead.
COMMON_FIELDS: dict[str, dict] = {
    "visible": {"kind": CHOICE, "options": ("show", "hide"),
                "default": "show", "label": "On the page"},
    "bg_color": {"kind": COLOR, "default": "", "label": "Background colour"},
    "text_color": {"kind": COLOR, "default": "", "label": "Text colour"},
    "anchor": {"kind": ANCHOR, "default": "", "label": "Link to this as"},
}


def block_fields(block_type: str) -> dict:
    """Every field this kind of block has, its own and the common ones.

    The common ones go last so the side panel reads as what this block *is*
    first and how it looks second.
    """
    spec = BLOCK_DEFS.get(block_type)
    if spec is None:
        return {}
    return {**spec["fields"], **COMMON_FIELDS}


def is_visible(block: dict) -> bool:
    """Whether this block goes out to a visitor."""
    return (block.get("fields") or {}).get("visible", "show") != "hide"


def visible_blocks(blocks) -> list[dict]:
    return [b for b in (blocks or []) if is_visible(b)]


#: Every block the builder can make, and every field it may carry. A field
#: that isn't here is thrown away on save, so a hand-made POST can't smuggle
#: one in.
BLOCK_DEFS: dict[str, dict] = {
    "hero": {
        "label": "Hero",
        "icon": "★",
        "hint": "Big opening — headline, a line under it, one button.",
        "fields": {
            "eyebrow": {"kind": LINE, "default": "A NEW ROUND IS OPEN"},
            "heading": {"kind": LINE, "default": "You don't need an audience.<br>You need a plan."},
            "body": {"kind": RICH, "default": "Eight weeks, four stages, and a room full of women doing it with you."},
            "button_text": {"kind": LINE, "default": "Join the challenge"},
            "button_url": {"kind": URL, "default": "/courses", "label": "Button link"},
            "button2_text": {"kind": LINE, "default": "", "label": "Second button"},
            "button2_url": {"kind": URL, "default": "", "label": "Second button link"},
            "image": {"kind": IMAGE, "default": "", "label": "Background image"},
            "overlay": {"kind": CHOICE, "options": ("dark", "light", "none"),
                        "default": "dark", "label": "Darken the picture"},
            "height": {"kind": CHOICE, "options": ("short", "tall", "full"),
                       "default": "tall", "label": "Height"},
            "align": _align("center"),
            "bg": _bg("plum"),
        },
    },
    "text": {
        "label": "Text",
        "icon": "¶",
        "hint": "A heading and a paragraph.",
        "fields": {
            "heading": {"kind": LINE, "default": "A heading goes here"},
            "body": {"kind": RICH, "default": "And the words that go under it. Select any of this to make it <strong>bold</strong>, <em>italic</em> or a link."},
            "align": _align(),
            "width": _width(),
            "pad": _pad(),
            "bg": _bg(),
        },
    },
    "columns": {
        "label": "Columns",
        "icon": "▥",
        "hint": "Two or three columns of writing side by side.",
        "fields": {
            "heading": {"kind": LINE, "default": ""},
            "count": {"kind": CHOICE, "options": ("2", "3"), "default": "2",
                      "label": "Columns"},
            "align": _align(),
            "pad": _pad(),
            "bg": _bg(),
        },
        "items": {
            "label": "Column",
            "max": 4,
            "fields": {
                "title": {"kind": LINE, "default": "A column"},
                "body": {"kind": RICH, "default": "What goes in it."},
            },
            "default_count": 2,
        },
    },
    "image": {
        "label": "Image",
        "icon": "▣",
        "hint": "One picture, with an optional caption.",
        "fields": {
            "image": {"kind": IMAGE, "default": ""},
            "link": {"kind": URL, "default": "", "label": "Picture links to"},
            "caption": {"kind": LINE, "default": ""},
            "width": _width("wide"),
            "rounded": {"kind": CHOICE, "options": ("yes", "no"),
                        "default": "yes", "label": "Rounded corners"},
            "pad": _pad(),
            "bg": _bg(),
        },
    },
    "gallery": {
        "label": "Gallery",
        "icon": "▦",
        "hint": "A grid of pictures.",
        "fields": {
            "heading": {"kind": LINE, "default": ""},
            "count": {"kind": CHOICE, "options": ("2", "3", "4"), "default": "3",
                      "label": "Per row"},
            "pad": _pad(),
            "bg": _bg(),
        },
        "items": {
            "label": "Picture",
            "max": 12,
            "fields": {
                "image": {"kind": IMAGE, "default": ""},
                "caption": {"kind": LINE, "default": ""},
            },
            "default_count": 3,
        },
    },
    "image_text": {
        "label": "Image & text",
        "icon": "◧",
        "hint": "A picture beside words.",
        "fields": {
            "image": {"kind": IMAGE, "default": ""},
            "heading": {"kind": LINE, "default": "Something worth showing"},
            "body": {"kind": RICH, "default": "Put the picture on whichever side reads better."},
            "button_text": {"kind": LINE, "default": ""},
            "button_url": {"kind": URL, "default": "", "label": "Button link"},
            "side": {"kind": CHOICE, "options": ("left", "right"),
                     "default": "left", "label": "Picture on the"},
            "ratio": {"kind": CHOICE, "options": ("even", "picture", "words"),
                      "default": "even", "label": "Give more room to"},
            "pad": _pad(),
            "bg": _bg(),
        },
    },
    "video": {
        "label": "Video",
        "icon": "▶",
        "hint": "A YouTube or Vimeo video.",
        "fields": {
            "heading": {"kind": LINE, "default": ""},
            "url": {"kind": URL, "default": "",
                    "label": "YouTube or Vimeo link"},
            "caption": {"kind": LINE, "default": ""},
            "width": _width("wide"),
            "pad": _pad(),
            "bg": _bg(),
        },
    },
    "stats": {
        "label": "Numbers",
        "icon": "◆",
        "hint": "A row of figures worth shouting about.",
        "fields": {
            "heading": {"kind": LINE, "default": ""},
            "pad": _pad(),
            "bg": _bg("soft"),
        },
        "items": {
            "label": "Number",
            "max": 6,
            "fields": {
                "value": {"kind": LINE, "default": "100+"},
                "label": {"kind": LINE, "default": "women through it"},
            },
            "default_count": 3,
        },
    },
    "features": {
        "label": "Cards",
        "icon": "▤",
        "hint": "What's inside, one card each.",
        "fields": {
            "heading": {"kind": LINE, "default": "What's inside"},
            "body": {"kind": RICH, "default": ""},
            "columns": {"kind": CHOICE, "options": ("2", "3", "4"),
                        "default": "3", "label": "Cards per row"},
            "card_style": {"kind": CHOICE, "options": ("raised", "outlined", "plain"),
                           "default": "raised", "label": "Card style"},
            "align": _align("center"),
            "pad": _pad(),
            "bg": _bg(),
        },
        "items": {
            "label": "Card",
            "max": 12,
            "fields": {
                "image": {"kind": IMAGE, "default": "", "label": "Picture"},
                "title": {"kind": LINE, "default": "Stage one"},
                "body": {"kind": RICH, "default": "What happens in it, in a sentence or two."},
            },
            "default_count": 3,
        },
    },
    "quote": {
        "label": "Quote",
        "icon": "❝",
        "hint": "Someone else's words.",
        "fields": {
            "quote": {"kind": RICH, "default": "I came in with nothing to sell and left with something people wanted."},
            "attribution": {"kind": LINE, "default": "— A member"},
            "image": {"kind": IMAGE, "default": "", "label": "Their photo"},
            "pad": _pad(),
            "bg": _bg("soft"),
        },
    },
    "faq": {
        "label": "Questions",
        "icon": "?",
        "hint": "The things people ask before they buy.",
        "fields": {
            "heading": {"kind": LINE, "default": "Before you join"},
            "style": {"kind": CHOICE, "options": ("open", "folded"),
                      "default": "open", "label": "Answers"},
            "pad": _pad(),
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
    "buttons": {
        "label": "Buttons",
        "icon": "⬭",
        "hint": "A row of links, on their own.",
        "fields": {
            "heading": {"kind": LINE, "default": ""},
            "align": _align("center"),
            "pad": _pad("small"),
            "bg": _bg(),
        },
        "items": {
            "label": "Button",
            "max": 4,
            "fields": {
                "text": {"kind": LINE, "default": "Join now"},
                "url": {"kind": URL, "default": "/courses", "label": "Links to"},
                "style": {"kind": CHOICE, "options": ("solid", "outline", "quiet"),
                          "default": "solid", "label": "Style"},
            },
            "default_count": 1,
        },
    },
    "cta": {
        "label": "Call to action",
        "icon": "➜",
        "hint": "The ask, on its own.",
        "fields": {
            "heading": {"kind": LINE, "default": "Ready when you are"},
            "body": {"kind": RICH, "default": "Doors are open now."},
            "button_text": {"kind": LINE, "default": "Join now"},
            "button_url": {"kind": URL, "default": "/courses", "label": "Button link"},
            "align": _align("center"),
            "pad": _pad("large"),
            "bg": _bg("plum"),
        },
    },
    "divider": {
        "label": "Divider",
        "icon": "—",
        "hint": "A line across the page.",
        "fields": {
            "style": {"kind": CHOICE, "options": ("line", "dots", "fade"),
                      "default": "line", "label": "Style"},
            "pad": _pad("small"),
            "bg": _bg(),
        },
    },
    "spacer": {
        "label": "Space",
        "icon": "␣",
        "hint": "An empty gap.",
        "fields": {
            "size": {"kind": CHOICE, "options": ("small", "medium", "large", "huge"),
                     "default": "medium", "label": "Height"},
            "bg": _bg("none"),
        },
    },
}

#: The order they appear in the "add a block" menu, grouped the way somebody
#: building a page thinks about them rather than alphabetically.
BLOCK_ORDER = ("hero", "text", "columns", "image", "image_text", "gallery",
               "video", "features", "stats", "quote", "faq", "buttons", "cta",
               "divider", "spacer")

#: Page-wide settings. Same shape as a block's fields so the side panel can
#: draw them with the code it already has.
PAGE_SETTINGS: dict[str, dict] = {
    "accent": {"kind": CHOICE,
               "options": ("plum", "berry", "rose", "gold", "ink"),
               "default": "plum", "label": "Accent colour"},
    "font": {"kind": CHOICE, "options": ("brand", "serif", "sans"),
             "default": "brand", "label": "Headings font"},
    "width": {"kind": CHOICE, "options": ("normal", "wide", "full"),
              "default": "normal", "label": "Page width"},
    "nav": {"kind": CHOICE, "options": ("show", "hide"), "default": "show",
            "label": "Site header & footer"},
    "description": {"kind": LINE, "default": "",
                    "label": "Search/social description"},
    "share_image": {"kind": IMAGE, "default": "", "label": "Share picture"},
}

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
             "fields": {k: f["default"]
                        for k, f in block_fields(block_type).items()}}
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


_HEX = re.compile(r"#(?:[0-9a-f]{3}|[0-9a-f]{6})\Z")


def clean_color(value) -> str:
    """``#a41f6b`` or nothing.

    This one is printed into a ``style`` attribute, which is the only place
    on a landing page where anything typed by a person ends up as CSS. A
    strict shape rather than a filter: there is no half-valid colour, and
    nothing shaped like this can close the attribute or start a second
    declaration.
    """
    raw = str(value if value is not None else "").strip().lower()
    if not raw:
        return ""
    if not raw.startswith("#"):
        raw = "#" + raw
    return raw if _HEX.match(raw) else ""


def clean_anchor(value) -> str:
    """A name a link can jump to: lowercase letters, digits and dashes.

    Goes into an ``id``, so it is cut to the characters that can only ever
    be a name — no spaces to break the attribute, nothing to confuse a
    selector with.
    """
    raw = re.sub(r"[^a-z0-9-]+", "-", str(value or "").strip().lower())
    return raw.strip("-")[:60]


def block_style(fields) -> str:
    """The inline style a block's own colours need, or "".

    Both values have been through :func:`clean_color`, so each is a hash and
    six hex digits or it is not here at all.
    """
    bag = fields or {}
    bits = []
    background = clean_color(bag.get("bg_color"))
    ink = clean_color(bag.get("text_color"))
    if background:
        bits.append(f"background:{background}")
    if ink:
        bits.append(f"color:{ink}")
    return ";".join(bits)


def _clean_field(spec: dict, value):
    kind = spec["kind"]
    if kind == LINE:
        return clean_line(value)
    if kind == RICH:
        return clean_rich(value)
    if kind == URL:
        return clean_url(value)
    if kind == IMAGE:
        return _clean_image(value)
    if kind == CHOICE:
        return _clean_choice(value, spec["options"], spec["default"])
    if kind == COLOR:
        return clean_color(value)
    if kind == ANCHOR:
        return clean_anchor(value)
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
    for key, field_spec in block_fields(block_type).items():
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


def normalize_settings(raw) -> dict:
    """Page-wide settings, cleaned the same way a block's fields are."""
    raw = raw if isinstance(raw, dict) else {}
    return {key: _clean_field(spec, raw.get(key, spec["default"]))
            for key, spec in PAGE_SETTINGS.items()}


def default_settings() -> dict:
    return {k: v["default"] for k, v in PAGE_SETTINGS.items()}


def document_from_json(text: str | None) -> dict:
    """Read a stored page back as ``{"blocks": [...], "settings": {...}}``.

    Pages saved before there were settings are a bare JSON list, so that
    shape is still read — there is no migration to run and no page that has
    to be opened and re-saved to keep working.

    A page whose JSON will not parse comes back empty rather than raising:
    the owner can still open the builder and put it right, which they could
    not do if the page 500'd.
    """
    if not text:
        return {"blocks": [], "settings": default_settings()}
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return {"blocks": [], "settings": default_settings()}
    if isinstance(data, list):
        return {"blocks": normalize_blocks(data),
                "settings": default_settings()}
    if isinstance(data, dict):
        return {"blocks": normalize_blocks(data.get("blocks")),
                "settings": normalize_settings(data.get("settings"))}
    return {"blocks": [], "settings": default_settings()}


def blocks_from_json(text: str | None) -> list[dict]:
    """Just the blocks of a stored page."""
    return document_from_json(text)["blocks"]


def settings_from_json(text: str | None) -> dict:
    """Just the page-wide settings of a stored page."""
    return document_from_json(text)["settings"]


def blocks_json(blocks: list[dict], settings: dict | None = None) -> str:
    return json.dumps({"blocks": blocks,
                       "settings": normalize_settings(settings or {})},
                      ensure_ascii=False, separators=(",", ":"))


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
    name = strip_marks(title) or "Untitled landing page"
    page = LandingPage(
        title=name[:160],
        slug=unique_slug(name),
        draft_json=blocks_json(default_blocks(), default_settings()),
        published_json="",
    )
    db.session.add(page)
    db.session.flush()
    return page


def duplicate(page: LandingPage) -> LandingPage:
    """A second copy of a page, as a draft, under a name of its own.

    What the copy is of is the *draft*, not what is live: the draft is the
    one being worked on, and copying a page is nearly always the start of
    a variation on the work in progress rather than on last month's.

    The copy is never published, whatever the original is. Two pages going
    live at once because one was duplicated is not something anybody asks
    for, and it is a hard thing to notice has happened.
    """
    name = f"{page.title} (copy)"[:160]
    copy = LandingPage(
        title=name,
        slug=unique_slug(name),
        draft_json=page.draft_json or blocks_json([], default_settings()),
        published_json="",
    )
    db.session.add(copy)
    db.session.flush()
    return copy


def save_draft(page: LandingPage, blocks_raw, *, title=None, slug=None,
               settings=None) -> None:
    """Write what the editor sent to the draft. Visitors see none of it."""
    # Settings the editor didn't send keep whatever the draft already had,
    # so an older editor tab saving a page cannot silently reset them.
    current = settings_from_json(page.draft_json)
    if isinstance(settings, dict):
        current.update(settings)
    page.draft_json = blocks_json(normalize_blocks(blocks_raw),
                                  normalize_settings(current))
    if title is not None:
        cleaned = strip_marks(title)
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

    def note(bag):
        for value in (bag or {}).values():
            if isinstance(value, str) and IMAGE_PREFIX in value:
                keys.add(value.rsplit("/", 1)[-1])

    for draft, published in db.session.query(LandingPage.draft_json,
                                             LandingPage.published_json).all():
        for text in (draft, published):
            doc = document_from_json(text)
            note(doc["settings"])
            for block in doc["blocks"]:
                note(block.get("fields"))
                # Cards and galleries keep their pictures on the items, so a
                # sweep that only read the block's own fields would delete
                # every picture in a gallery the moment it ran.
                for item in block.get("items") or []:
                    note(item)
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
    def field_rows(fields):
        return [
            {"key": k,
             "kind": f["kind"],
             "label": f.get("label", k.replace("_", " ").capitalize()),
             "options": list(f.get("options", ()))}
            for k, f in fields.items()
        ]

    return {
        "order": list(BLOCK_ORDER),
        "page_fields": field_rows(PAGE_SETTINGS),
        "defs": {
            key: {
                "label": spec["label"],
                "icon": spec.get("icon", "▦"),
                "hint": spec.get("hint", ""),
                "fields": field_rows(block_fields(key)),
                "items": bool(spec.get("items")),
                "item_label": (spec.get("items") or {}).get("label", "Item"),
                "item_max": (spec.get("items") or {}).get("max", 0),
                "item_fields": field_rows((spec.get("items") or {}).get("fields", {})),
                # What an added item starts as, so the browser can put one in
                # without asking us what a blank one looks like.
                "item_defaults": new_item(key),
            }
            for key, spec in BLOCK_DEFS.items()
        },
    }


# --- video embeds --------------------------------------------------------------

_YT = re.compile(
    r"(?:youtube\.com/(?:watch\?v=|embed/|shorts/)|youtu\.be/)([A-Za-z0-9_-]{6,20})")
_VIMEO = re.compile(r"vimeo\.com/(?:video/)?(\d{6,12})")


def video_embed_url(url: str | None) -> str:
    """A YouTube or Vimeo link turned into one we can put in an iframe.

    Only these two, and only the id out of the link — so what ends up in the
    ``src`` is a URL this function built, never one somebody pasted.
    """
    raw = str(url or "")
    found = _YT.search(raw)
    if found:
        return f"https://www.youtube-nocookie.com/embed/{found.group(1)}"
    found = _VIMEO.search(raw)
    if found:
        return f"https://player.vimeo.com/video/{found.group(1)}"
    return ""
