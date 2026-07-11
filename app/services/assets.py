"""Validate uploaded course/guide files and render Word docs for inline reading.

Files (PDF / Word) are stored in the database so they survive Render's ephemeral
disk, then served to buyers through an ownership-gated route. There is no public
download link: PDFs are embedded, and .docx files are converted to sanitized
HTML on the fly with `mammoth`.
"""
import io
import os

import bleach

MAX_UPLOAD_BYTES = 25 * 1024 * 1024   # 25 MB per file

EXT_KIND = {".pdf": "pdf", ".doc": "doc", ".docx": "docx"}
KIND_MIME = {
    "pdf": "application/pdf",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
#: first bytes we expect for each kind (a light sniff, not full validation)
_MAGIC = {
    "pdf": (b"%PDF",),
    "docx": (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
    "doc": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),
}

# tags mammoth may emit that are safe to keep for reading
_ALLOWED_TAGS = [
    "p", "br", "h1", "h2", "h3", "h4", "h5", "h6", "strong", "b", "em", "i",
    "u", "s", "sub", "sup", "a", "ul", "ol", "li", "blockquote", "pre", "code",
    "hr", "table", "thead", "tbody", "tr", "td", "th", "img", "span", "div",
]
_ALLOWED_ATTRS = {
    "a": ["href", "title", "target", "rel"],
    "img": ["src", "alt", "width", "height"],
    "td": ["colspan", "rowspan"],
    "th": ["colspan", "rowspan"],
}
_ALLOWED_PROTOCOLS = ["http", "https", "mailto", "data"]


class AssetError(ValueError):
    pass


def process_asset(file_storage):
    """Validate an uploaded file. Returns (data, mime, kind, filename).

    Raises AssetError with a friendly message on anything we won't accept.
    """
    name = os.path.basename(file_storage.filename or "").strip()
    ext = os.path.splitext(name)[1].lower()
    kind = EXT_KIND.get(ext)
    if kind is None:
        raise AssetError("only PDF or Word files (.pdf, .doc, .docx) are allowed.")

    raw = file_storage.read(MAX_UPLOAD_BYTES + 1)
    if not raw:
        raise AssetError("that file was empty.")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise AssetError("that file is over 25 MB \u2014 try a smaller one.")

    if not any(raw.startswith(sig) for sig in _MAGIC[kind]):
        raise AssetError("that file's contents didn't match its extension.")

    return raw, KIND_MIME[kind], kind, name[:255]


def docx_to_html(data: bytes):
    """Convert .docx bytes to sanitized HTML for inline reading, or None."""
    try:
        import mammoth
        result = mammoth.convert_to_html(io.BytesIO(data))
        html = result.value or ""
    except Exception:
        return None
    if not html.strip():
        return None
    return bleach.clean(html, tags=_ALLOWED_TAGS, attributes=_ALLOWED_ATTRS,
                        protocols=_ALLOWED_PROTOCOLS, strip=True)
