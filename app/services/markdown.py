"""Markdown -> sanitized HTML (allow-list via bleach)."""
import re

import bleach
import markdown as md
from markupsafe import Markup

#: Markdown proper has no strikethrough and the extension that adds it isn't
#: worth a dependency, so it is swapped in afterwards. Running on the rendered
#: HTML keeps the tildes out of Markdown's way.
_STRIKE = re.compile(r"~~(.+?)~~", re.S)

ALLOWED_TAGS = [
    "p", "br", "strong", "em", "b", "i", "u", "s",
    "h2", "h3", "h4", "blockquote", "ul", "ol", "li",
    "a", "img", "hr", "code", "pre",
]
ALLOWED_ATTRS = {
    "a": ["href", "title", "rel"],
    "img": ["src", "alt", "title", "loading"],
}
ALLOWED_PROTOCOLS = ["https", "http", "mailto"]


def render_markdown(text: str | None, *, breaks: bool = False) -> Markup:
    """Turn written text into safe HTML.

    ``breaks`` keeps every line ending as a line break, for the places that
    used to be shown that way — so writing that relies on where the lines fall
    reads exactly as it did before it could also be formatted.
    """
    if not text:
        return Markup("")
    extensions = ["extra", "sane_lists"]
    if breaks:
        extensions.append("nl2br")
    html = md.markdown(text, extensions=extensions)
    html = _STRIKE.sub(r"<s>\1</s>", html)
    clean = bleach.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRS,
        protocols=ALLOWED_PROTOCOLS,
        strip=True,
    )
    clean = bleach.linkify(clean, callbacks=[bleach.callbacks.nofollow])
    return Markup(clean)
