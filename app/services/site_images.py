"""Process and store owner-uploaded site images (hero / story teaser)."""
from __future__ import annotations

import io

from PIL import Image, ImageOps, UnidentifiedImageError

from ..extensions import db
from ..models import SITE_IMAGE_KEYS, SiteImage, utcnow

MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_EDGE = 1600
OUTPUT_MIME = "image/jpeg"

#: Display aspect ratios on the home page (width, height). Studio crop matches these.
ASPECT_RATIOS = {
    "portrait": (4, 5),   # home Their Story photo
    "hero": (1, 1),       # optional square site image
    "creator": (1, 1),    # creator-of-the-month photo
}


class SiteImageError(ValueError):
    pass


def public_path(key: str) -> str:
    return f"/media/site/{key}"


def _cover_crop(img: Image.Image, aw: int, ah: int) -> Image.Image:
    """Center-crop to aspect ``aw:ah`` (no-op when already matching)."""
    tw, th = img.size
    if tw < 1 or th < 1 or aw < 1 or ah < 1:
        return img
    target = aw / ah
    current = tw / th
    if abs(current - target) < 0.02:
        return img
    if current > target:
        nw = max(1, int(round(th * target)))
        left = max(0, (tw - nw) // 2)
        return img.crop((left, 0, left + nw, th))
    nh = max(1, int(round(tw / target)))
    top = max(0, (th - nh) // 2)
    return img.crop((0, top, tw, top + nh))


def process_and_save(key: str, file_storage) -> str:
    """Validate, crop to slot aspect, resize, store. Returns the public path."""
    if key not in SITE_IMAGE_KEYS:
        raise SiteImageError("Unknown image slot.")
    if not file_storage or not getattr(file_storage, "filename", None):
        raise SiteImageError("Choose an image file first.")
    raw = file_storage.read(MAX_UPLOAD_BYTES + 1)
    if not raw:
        raise SiteImageError("That file was empty.")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise SiteImageError("Keep site images under 8 MB.")
    return _store(key, raw)


def _store(key: str, raw: bytes) -> str:
    """Crop to the slot's shape, shrink, and keep it in the database."""
    try:
        img = Image.open(io.BytesIO(raw))
        img = ImageOps.exif_transpose(img)
        img.load()
    except (UnidentifiedImageError, OSError) as exc:
        raise SiteImageError("That doesn't look like a usable image.") from exc

    img = img.convert("RGB")
    ratio = ASPECT_RATIOS.get(key)
    if ratio:
        img = _cover_crop(img, ratio[0], ratio[1])
    img.thumbnail((MAX_EDGE, MAX_EDGE), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=88, optimize=True)
    data = out.getvalue()

    row = db.session.get(SiteImage, key)
    if row is None:
        row = SiteImage(key=key, data=data, mime=OUTPUT_MIME, updated_at=utcnow())
        db.session.add(row)
    else:
        row.data = data
        row.mime = OUTPUT_MIME
        row.updated_at = utcnow()
    db.session.commit()
    return public_path(key)


def save_from_url(key: str, url: str) -> str:
    """Fetch a picture from elsewhere and keep our own copy of it.

    Instagram hands out photo links that are signed and time-limited, so one
    pointed at rather than kept works on the day it is set and quietly turns
    into a broken circle weeks later. Returns "" if it can't be fetched, which
    the caller should treat as having no picture at all.
    """
    import logging

    import requests

    if key not in SITE_IMAGE_KEYS or not (url or "").startswith("http"):
        return ""
    try:
        resp = requests.get(url, timeout=6, stream=True, headers={
            "User-Agent": "Mozilla/5.0 (compatible; BloomAnywayBot/1.0)",
        })
        if resp.status_code != 200:
            return ""
        raw = resp.raw.read(MAX_UPLOAD_BYTES + 1, decode_content=True)
    except Exception:
        logging.getLogger(__name__).info("site image: could not fetch %s", url)
        return ""
    if not raw or len(raw) > MAX_UPLOAD_BYTES:
        return ""
    try:
        return _store(key, raw)
    except SiteImageError:
        return ""


def clear(key: str) -> None:
    if key not in SITE_IMAGE_KEYS:
        return
    row = db.session.get(SiteImage, key)
    if row is not None:
        db.session.delete(row)
        db.session.commit()


def get(key: str) -> SiteImage | None:
    if key not in SITE_IMAGE_KEYS:
        return None
    return db.session.get(SiteImage, key)
