"""Images attached to community posts and comments.

Uploads are re-encoded to safe JPEGs — metadata stripped, dimensions capped —
and stored in the database, the same way avatars and listing images are, so
they survive a redeploy. Bad or oversized files are skipped rather than
sinking the post or comment they arrived with.
"""
import io
import logging

from PIL import Image, ImageOps, UnidentifiedImageError

from ..extensions import db
from ..models import ForumImage

log = logging.getLogger(__name__)

#: How many images one post or comment may carry.
MAX_IMAGES = 4
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_W, MAX_H = 1600, 1600
OUTPUT_MIME = "image/jpeg"


class CommunityImageError(ValueError):
    pass


def process_image(file_storage) -> tuple[bytes, str, int, int]:
    """Return ``(jpeg_bytes, mime, width, height)`` or raise CommunityImageError."""
    raw = file_storage.read(MAX_UPLOAD_BYTES + 1)
    if not raw:
        raise CommunityImageError("That image was empty.")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise CommunityImageError("Each image must be under 8 MB.")
    try:
        Image.open(io.BytesIO(raw)).verify()
        img = Image.open(io.BytesIO(raw))
        img = ImageOps.exif_transpose(img).convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError):
        raise CommunityImageError("That file wasn't an image we could read.")
    img.thumbnail((MAX_W, MAX_H), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=84, optimize=True)
    return out.getvalue(), OUTPUT_MIME, img.width, img.height


def attach_images(files, *, user, post=None, comment=None) -> tuple[int, bool]:
    """Store up to ``MAX_IMAGES`` uploaded images against a post or comment.

    Returns ``(saved_count, some_skipped)``. Empty/oversized/unreadable files
    and anything past the cap are skipped, so a bad attachment never loses the
    words it came with. The caller commits.
    """
    if post is None and comment is None:
        return 0, False
    real = [f for f in (files or []) if f and (getattr(f, "filename", "") or "").strip()]
    if not real:
        return 0, False

    saved = 0
    skipped = len(real) > MAX_IMAGES
    for fs in real[:MAX_IMAGES]:
        try:
            data, mime, w, h = process_image(fs)
        except CommunityImageError:
            skipped = True
            continue
        except Exception:
            log.exception("community image: unexpected processing failure")
            skipped = True
            continue
        db.session.add(ForumImage(
            post_id=(post.id if post is not None else None),
            comment_id=(comment.id if comment is not None else None),
            user_id=getattr(user, "id", None),
            data=data, mime=mime, width=w, height=h,
        ))
        saved += 1
    return saved, skipped
