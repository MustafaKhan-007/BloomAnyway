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
from .landing_templates import DEFAULT_TEMPLATE, TEMPLATES

#: Field kinds. ``line`` and ``rich`` are typed on the page itself; the rest
#: are set in the side panel.
LINE, RICH, URL, IMAGE, CHOICE, NUMBER = (
    "line", "rich", "url", "image", "choice", "number")
#: A colour of her own, and a name to link to. Both end up in an attribute
#: rather than between tags, so both are cleaned down to a shape that cannot
#: carry anything but what they are for.
COLOR, ANCHOR = "color", "anchor"
#: A whole number in a range — where a block sits across the page, and how
#: much of it the block takes.
SPAN = "span"

#: The page is twelve columns wide. Everything defaults to all twelve, which
#: is a block stacked under the last one — exactly what every page did
#: before there were columns at all.
GRID_COLUMNS = 12

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
    # Where it sits across the page and how much of it it takes. Twelve of
    # twelve starting at one is a block on its own line, which is what
    # every block was before any of this — so a page written last month
    # comes out of here looking exactly as it did.
    "col_span": {"kind": SPAN, "min": 1, "max": GRID_COLUMNS,
                 "default": GRID_COLUMNS, "label": "Width"},
    "col_start": {"kind": SPAN, "min": 1, "max": GRID_COLUMNS,
                  "default": 1, "label": "Starts at column"},
    # Pulls a block up into the one above it, for overlapping a card onto a
    # hero and the like. Steps rather than a number: a free measurement is
    # a thing to fiddle with for an hour and get wrong on a phone.
    "pull": {"kind": CHOICE, "options": ("none", "small", "medium", "large"),
             "default": "none", "label": "Pull up into the block above"},
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
            "eyebrow": {"kind": LINE, "default": "", "label": "Small line above"},
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
            "eyebrow": {"kind": LINE, "default": "", "label": "Small line above"},
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
            "eyebrow": {"kind": LINE, "default": "", "label": "Small line above"},
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
            "eyebrow": {"kind": LINE, "default": "", "label": "Small line above"},
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
            "eyebrow": {"kind": LINE, "default": "", "label": "Small line above"},
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
            "eyebrow": {"kind": LINE, "default": "", "label": "Small line above"},
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
            "eyebrow": {"kind": LINE, "default": "", "label": "Small line above"},
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
            "eyebrow": {"kind": LINE, "default": "", "label": "Small line above"},
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
            "eyebrow": {"kind": LINE, "default": "", "label": "Small line above"},
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
            "eyebrow": {"kind": LINE, "default": "", "label": "Small line above"},
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


def clean_span(value, lo: int, hi: int, default: int) -> int:
    """A whole number inside its range, or the default."""
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return number if lo <= number <= hi else default


def grid_area(fields) -> tuple[int, int]:
    """``(start, span)`` for a block, kept inside the twelve columns.

    Clamped here rather than trusted: a span of 8 starting at column 9
    would run off the end of the grid, and CSS answers that by inventing
    four more columns and squeezing the whole page into them.
    """
    bag = fields or {}
    span = clean_span(bag.get("col_span"), 1, GRID_COLUMNS, GRID_COLUMNS)
    start = clean_span(bag.get("col_start"), 1, GRID_COLUMNS, 1)
    if start + span > GRID_COLUMNS + 1:
        start = max(1, GRID_COLUMNS + 1 - span)
    return start, span


def grid_style(fields) -> str:
    """``grid-column:…`` for a block, or "" when it is the whole width.

    Printed twice: onto the block itself for the live page, and onto the
    wrapper the builder puts around it — because the grid item is the
    block out there and the wrapper in here, and the two have to lay out
    the same or the builder is showing something the visitor won't get.
    """
    start, span = grid_area(fields)
    if (start, span) == (1, GRID_COLUMNS):
        return ""
    return f"grid-column:{start}/span {span}"


def clean_anchor(value) -> str:
    """A name a link can jump to: lowercase letters, digits and dashes.

    Goes into an ``id``, so it is cut to the characters that can only ever
    be a name — no spaces to break the attribute, nothing to confuse a
    selector with.
    """
    raw = re.sub(r"[^a-z0-9-]+", "-", str(value or "").strip().lower())
    return raw.strip("-")[:60]


def _lightness(colour: str) -> float:
    """Roughly how light a colour reads, 0 for black and 1 for white."""
    raw = colour.lstrip("#")
    if len(raw) == 3:
        raw = "".join(c * 2 for c in raw)
    red, green, blue = (int(raw[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


#: Near-black and near-white, taken from the theme so a worked-out text
#: colour still looks like it belongs to this site.
INK_ON_LIGHT = "#2b2622"
INK_ON_DARK = "#faf5ee"

#: Where the one flips to the other. A shade above the middle, because dark
#: text on a mid-tone reads worse than light text does.
INK_FLIP_AT = 0.55


def block_ink(fields) -> str:
    """The text colour a block will use: hers, or one worked out for her.

    A background of her own with nothing said about the words is the one
    combination that comes out unreadable — a dark green section still
    carrying the near-black the cream preset was using. So a chosen
    background with no chosen text colour gets whichever of near-black and
    near-white can actually be read on it, and saying one outright always
    wins over that.
    """
    bag = fields or {}
    chosen = clean_color(bag.get("text_color"))
    if chosen:
        return chosen
    background = clean_color(bag.get("bg_color"))
    if not background:
        return ""
    return INK_ON_LIGHT if _lightness(background) > INK_FLIP_AT else INK_ON_DARK


def block_style(fields) -> str:
    """Everything about a block that has to be a style rather than a class.

    Where it sits on the twelve-column grid, and any colours of its own.
    Every value here has been through a cleaner that can only return a
    number in range or a hash and hex digits, so none of it can carry
    anything but what it is for.
    """
    bag = fields or {}
    bits = []
    placed = grid_style(bag)
    if placed:
        bits.append(placed)
    background = clean_color(bag.get("bg_color"))
    if background:
        bits.append(f"background:{background}")
    ink = block_ink(bag)
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
    if kind == SPAN:
        return clean_span(value, spec.get("min", 1), spec.get("max", 12),
                          spec["default"])
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


# --- what a new page starts as -------------------------------------------------
#
# A new page has always opened as a whole page rather than a blank canvas:
# something already laid out is far easier to make your own than an empty
# screen with an "add a block" button on it. What changed is that there is
# now more than one such page to start from, and ``landing_templates`` holds
# their words.

def _template_block(spec: dict) -> dict:
    """One block of a template: its own defaults, with the template on top.

    A field the block hasn't got is an error rather than something quietly
    dropped. A template is written in a file rather than typed in the
    builder, so a misspelt key would otherwise surface as a section sitting
    there with its placeholder text and nothing to say why.
    """
    block_type = spec["type"]
    block = new_block(block_type)

    fields = spec.get("fields") or {}
    unknown = sorted(set(fields) - set(block_fields(block_type)))
    if unknown:
        raise LandingPageError(
            f"A {block_type} block has no field called {unknown[0]!r}.")
    block["fields"].update(fields)

    item_fields = (BLOCK_DEFS[block_type].get("items") or {}).get("fields")
    if "items" in spec:
        if not item_fields:
            raise LandingPageError(f"A {block_type} block has no items.")
        block["items"] = []
        for item in spec["items"]:
            unknown = sorted(set(item) - set(item_fields))
            if unknown:
                raise LandingPageError(
                    f"A {block_type} item has no field called {unknown[0]!r}.")
            block["items"].append({**new_item(block_type), **item})
    return block


def template_key(raw) -> str:
    """A template we have, or the one a new page opens as by default."""
    name = str(raw or "").strip()
    return name if name in TEMPLATES else DEFAULT_TEMPLATE


def template_title(key: str | None = None) -> str:
    """What to call a page started from this template, if nothing is typed."""
    return TEMPLATES[template_key(key)]["title"]


def template_blocks(key: str | None = None) -> list[dict]:
    """The blocks a page started from this template holds.

    Put through the same cleaner a save goes through. That is what stops a
    template being a back door — nothing in ``landing_templates`` can reach
    a page that couldn't be typed into the builder — and it means what lands
    in the database is the same shape the editor writes back, so the first
    Save changes nothing.
    """
    spec = TEMPLATES[template_key(key)]
    return normalize_blocks([_template_block(b) for b in spec["blocks"]])


def template_choices() -> list[dict]:
    """What the picker offers, in the order it offers them."""
    return [{"key": key, "label": spec["label"], "short": spec["short"],
             "hint": spec["hint"]}
            for key, spec in TEMPLATES.items()]


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

def create(title: str = "", template: str | None = None) -> LandingPage:
    """A new page, laid out from a template and not yet published.

    A template name we don't have falls back to the default one rather than
    failing. Which page somebody starts from is a preference, not an
    instruction worth refusing over.
    """
    key = template_key(template)
    name = strip_marks(title) or template_title(key)
    page = LandingPage(
        title=name[:160],
        slug=unique_slug(name),
        draft_json=blocks_json(template_blocks(key), default_settings()),
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
             "options": list(f.get("options", ())),
             "min": f.get("min"), "max": f.get("max")}
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
