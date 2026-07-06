"""Markdown -> sanitized HTML (allow-list via bleach)."""
import bleach
import markdown as md
from markupsafe import Markup

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


def render_markdown(text: str | None) -> Markup:
    if not text:
        return Markup("")
    html = md.markdown(text, extensions=["extra", "sane_lists"])
    clean = bleach.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRS,
        protocols=ALLOWED_PROTOCOLS,
        strip=True,
    )
    clean = bleach.linkify(clean, callbacks=[bleach.callbacks.nofollow])
    return Markup(clean)
