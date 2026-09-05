"""Markdown -> sanitized HTML (allow-list via bleach)."""
import re

import bleach
import markdown as md
from markupsafe import Markup

#: Markdown proper has no strikethrough and the extension that adds it isn't
#: worth a dependency, so it is swapped in afterwards. Running on the rendered
#: HTML keeps the tildes out of Markdown's way.
_STRIKE = re.compile(r"~~(.+?)~~", re.S)

#: Markdown only reads a list that starts straight under a line of writing as
#: more of that same line, so "Key takeaways:" followed by dashes came out as
#: one run-on paragraph. People write it that way anyway, so the blank line
#: Markdown wants is put in for them.
_BULLET = re.compile(r"^ {0,3}(?:[-*+]|\d{1,9}[.)])[ \t]+\S")
_RULE = re.compile(r"^ {0,3}([-*_])[ \t]*(?:\1[ \t]*){2,}$")


def _space_lists(text: str) -> str:
    out: list[str] = []
    in_list = False
    for line in text.split("\n"):
        bare = line.strip()
        if _BULLET.match(line) and not _RULE.match(line):
            if not in_list and out and out[-1].strip():
                out.append("")
            in_list = True
        elif not bare:
            pass
        elif not line[:1].isspace():
            in_list = False
        out.append(line)
    return "\n".join(out)

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
    html = md.markdown(_space_lists(text), extensions=extensions)
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
