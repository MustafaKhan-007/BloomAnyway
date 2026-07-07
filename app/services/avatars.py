"""Process uploaded avatar images into small, safe, square JPEGs.

Stored in the database (not on disk) so avatars survive Render deploys, which
wipe the ephemeral filesystem. Re-encoding also strips EXIF/metadata and
neutralises most malformed-image tricks.
"""
import io

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_UPLOAD_BYTES = 6 * 1024 * 1024   # 6 MB before decoding
OUTPUT_SIZE = 400                     # square px
OUTPUT_MIME = "image/jpeg"


class AvatarError(ValueError):
    pass


def process_avatar(file_storage) -> tuple[bytes, str]:
    """Return (jpeg_bytes, mime) for a Werkzeug FileStorage, or raise AvatarError."""
    raw = file_storage.read(MAX_UPLOAD_BYTES + 1)
    if not raw:
        raise AvatarError("That file was empty.")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise AvatarError("That image is over 6 MB \u2014 try a smaller one.")

    try:
        img = Image.open(io.BytesIO(raw))
        img.verify()                       # sniff for a valid image
        img = Image.open(io.BytesIO(raw))  # reopen (verify() exhausts the file)
        img = ImageOps.exif_transpose(img)  # respect phone orientation
        img = img.convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError):
        raise AvatarError("That didn't look like an image we could read.")

    # centre-crop to a square, then downscale
    img = ImageOps.fit(img, (OUTPUT_SIZE, OUTPUT_SIZE), Image.LANCZOS)

    out = io.BytesIO()
    img.save(out, format="JPEG", quality=85, optimize=True)
    return out.getvalue(), OUTPUT_MIME
