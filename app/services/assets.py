"""Course/guide content: videos, documents and written extracts.

Files stream to the course media disk in chunks rather than into Postgres, so
a module can hold a full-length lesson video. ``data`` on older rows is still
read when present, so nothing uploaded before the move needs migrating.
"""
from __future__ import annotations

import logging
import os
import secrets
import time
import zipfile
from io import BytesIO

from flask import current_app
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

from ..extensions import db
from ..models import Product, ProductAsset

#: Cap for a file arriving in a single request. Anything larger has to come
#: through the chunked uploader, because Cloudflare Free rejects request
#: bodies over roughly 100 MB whatever this is set to.
MAX_BYTES = 90 * 1024 * 1024
_CHUNK = 1024 * 1024

log = logging.getLogger(__name__)


#: Ceiling for a file kept in the database rather than on a disk. Postgres
#: will hold far more, but a course video does not belong in a row: it is read
#: whole into memory to serve, and it travels with every backup.
DB_MAX_BYTES = 32 * 1024 * 1024


def in_database() -> bool:
    """True when there is no files directory, so bytes live in the database.

    A host without a persistent disk has nowhere durable to put a file: the
    container's own filesystem looks fine until the next deploy and then comes
    back empty. Leaving COURSE_FILES_DIR blank keeps files in Postgres, which
    survives a deploy, at the cost of a much smaller ceiling.
    """
    return not (current_app.config.get("COURSE_FILES_DIR") or "").strip()


def storage_dir() -> str:
    return current_app.config["COURSE_FILES_DIR"]


def parts_dir() -> str:
    # A part file only has to survive the upload itself, so with no disk it can
    # sit in the system's temporary space and be read into the database at the
    # end. That keeps the slice-by-slice uploader working either way.
    if in_database():
        import tempfile
        return os.path.join(tempfile.gettempdir(), "bloom-course-parts")
    return os.path.join(storage_dir(), "parts")


def max_upload_bytes() -> int:
    if in_database():
        return DB_MAX_BYTES
    return current_app.config["COURSE_UPLOAD_MAX_MB"] * 1024 * 1024


def disk_path(disk_name: str) -> str:
    """Absolute path of a stored file. Never trusts a caller-supplied path."""
    safe = os.path.basename(disk_name or "")
    if not safe:
        raise AssetError("That file is missing.")
    return os.path.join(storage_dir(), safe)


def read_bytes(asset: ProductAsset) -> bytes:
    """Whole contents of an asset, wherever it lives. Small files only."""
    if asset.disk_name:
        with open(disk_path(asset.disk_name), "rb") as fh:
            return fh.read()
    return bytes(asset.data or b"")


def delete_file(asset: ProductAsset) -> None:
    """Best-effort removal of an asset's file from the disk."""
    if not asset.disk_name:
        return
    try:
        os.remove(disk_path(asset.disk_name))
    except (OSError, AssetError):
        pass

_MIME_BY_EXT = {
    ".pdf": "application/pdf",
    ".h5p": "application/zip",
    ".zip": "application/zip",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".html": "text/html",
    ".htm": "text/html",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".epub": "application/epub+zip",
}

_KIND_BY_EXT = {
    ".pdf": "pdf",
    ".h5p": "h5p",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".gif": "image",
    ".webp": "image",
    ".mp4": "video",
    ".webm": "video",
    ".mp3": "audio",
    ".wav": "audio",
    ".m4a": "audio",
    ".txt": "text",
    ".md": "text",
    ".html": "html",
    ".htm": "html",
    ".doc": "doc",
    ".docx": "docx",
    ".epub": "other",
    ".zip": "other",
}


class AssetError(ValueError):
    pass


def detect_kind(filename: str, mime: str | None = None) -> str:
    ext = os.path.splitext(filename or "")[1].lower()
    if ext in _KIND_BY_EXT:
        return _KIND_BY_EXT[ext]
    mime = (mime or "").lower()
    if "pdf" in mime:
        return "pdf"
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    if "html" in mime:
        return "html"
    if mime.startswith("text/"):
        return "text"
    return "other"


def _looks_like_h5p(data: bytes, filename: str) -> bool:
    ext = os.path.splitext(filename or "")[1].lower()
    if ext == ".h5p":
        return True
    if ext != ".zip":
        return False
    try:
        with zipfile.ZipFile(BytesIO(data)) as zf:
            names = set(zf.namelist())
            return "h5p.json" in names or any(n.endswith("/h5p.json") for n in names)
    except zipfile.BadZipFile:
        return False


#: What may ride along with a receipt. Mail providers reject anything much
#: over ten megabytes, and a receipt is not the place for a whole course.
RECEIPT_MAX_BYTES = 8 * 1024 * 1024
RECEIPT_MAX_FILES = 3


def receipt_files(product, *, budget: int = RECEIPT_MAX_BYTES,
                  limit: int = RECEIPT_MAX_FILES) -> list[dict]:
    """The PDFs to send with a purchase receipt.

    Only what the buyer could open the moment they paid: on a drip-fed course
    the modules arrive on their own schedule, so sending them by email would
    hand over the whole thing on day one. Anything too big to post is left out
    and stays where it always was, in their library.
    """
    out: list[dict] = []
    if product is None:
        log.info("receipt: no product behind this payment, nothing to attach")
        return out
    dripped = product.is_dripped()
    spent = 0
    # Every way a file can be left out is quiet on its own, and an email that
    # arrives without the guide looks the same whichever it was. One line says
    # which, so it doesn't have to be guessed at afterwards.
    skipped: list[str] = []
    for asset in product.top_level_assets():
        name = asset.filename or f"asset {asset.id}"
        if asset.kind != "pdf":
            skipped.append(f"{name}: not a PDF ({asset.kind})")
            continue
        if dripped and asset.module_index:
            skipped.append(f"{name}: in module {asset.module_index}, not open yet")
            continue
        size = asset.size or 0
        if size <= 0:
            skipped.append(f"{name}: no size recorded")
            continue
        if spent + size > budget:
            skipped.append(f"{name}: over what an email will carry")
            continue
        try:
            data = read_bytes(asset)
        except Exception as exc:
            skipped.append(f"{name}: could not be read ({exc.__class__.__name__})")
            continue
        if not data:
            skipped.append(f"{name}: nothing in it")
            continue
        out.append({"name": asset.filename or "guide.pdf", "data": data})
        spent += size
        if len(out) >= limit:
            break
    if out:
        log.info("receipt for %s: attaching %s", product.slug,
                 ", ".join(f["name"] for f in out))
    else:
        log.warning("receipt for %s: nothing attached — %s", product.slug,
                    "; ".join(skipped) or "the product has no files")
    return out


def _check_whole_bytes(raw: bytes, kind: str) -> None:
    """The same cut-short check, for a file held in memory."""
    if kind != "pdf":
        return
    if not raw.startswith(b"%PDF-"):
        raise AssetError("That doesn't look like a PDF inside — check the file "
                         "and try again.")
    if b"%%EOF" not in raw[-4096:]:
        raise AssetError("That PDF arrived cut short, so it wouldn't open for "
                         "anyone. Upload it again.")


def _check_whole_pdf(path: str, kind: str) -> None:
    """Refuse a PDF that stops before its end marker.

    A cut-short upload still saves and still looks like a file in Studio; the
    breakage only shows later, to a buyer, as a reader that won't open it. A
    PDF ends with %%EOF, so a missing one means the upload didn't finish.
    """
    if kind != "pdf":
        return
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            head = fh.read(5)
            # Generous: readers look back about a kilobyte for the marker, so
            # anything without one in four is beyond them as well as us.
            fh.seek(max(0, size - 4096))
            tail = fh.read()
    except OSError:
        return
    if not head.startswith(b"%PDF-"):
        raise AssetError("That doesn't look like a PDF inside — check the file "
                         "and try again.")
    if b"%%EOF" not in tail:
        raise AssetError("That PDF arrived cut short, so it wouldn't open for "
                         "anyone. Upload it again.")


def describe(filename: str, mimetype: str | None) -> tuple[str, str, str]:
    """Return (safe filename, mime, kind) for an upload."""
    safe = secure_filename(filename or "") or "course-file"
    ext = os.path.splitext(safe)[1].lower()
    mime = (mimetype or "").strip() or _MIME_BY_EXT.get(
        ext, "application/octet-stream")
    return safe[:255], mime[:120], detect_kind(safe, mime)[:20]


def _store_stream(stream, first: bytes, limit: int, ext: str) -> tuple[str, int]:
    """Write a stream to the course disk in chunks. Returns (disk_name, size).

    Chunked so a large video never sits in a worker's memory, and so the size
    cap can stop a runaway upload partway instead of after we've read it all.
    """
    os.makedirs(storage_dir(), exist_ok=True)
    disk_name = secrets.token_hex(16) + ext
    path = os.path.join(storage_dir(), disk_name)
    size = 0
    try:
        with open(path, "wb") as fh:
            if first:
                fh.write(first)
                size = len(first)
            while True:
                chunk = stream.read(_CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise AssetError(
                        f"That file is over {limit // (1024 * 1024)} MB.")
                fh.write(chunk)
    except AssetError:
        _safe_remove(path)
        raise
    except OSError:
        _safe_remove(path)
        raise AssetError("We couldn't save that upload just now — try again.")
    if not size:
        _safe_remove(path)
        raise AssetError("That file was empty.")
    return disk_name, size


def _safe_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _next_order(product: Product, module_index: int | None,
                parent: ProductAsset | None = None) -> int:
    """Append to the end of whatever list this belongs to.

    A note is ordered among its file's other notes, not among the module —
    otherwise the first extract written for module 3's video would sort ahead
    of the video itself.
    """
    if parent is not None:
        same = list(parent.notes)
        return max((a.sort_order or 0) for a in same) + 1 if same else 1
    same = [a for a in product.top_level_assets() if a.module_index == module_index]
    if same:
        return max((a.sort_order or 0) for a in same) + 1
    return len(product.top_level_assets())


def add_asset(
    product: Product,
    upload: FileStorage,
    *,
    title: str | None = None,
    module_index: int | None = None,
    lesson_index: int | None = None,
) -> ProductAsset:
    """Store a file arriving in one request and attach it to the product."""
    if upload is None or not getattr(upload, "filename", None):
        raise AssetError("Choose a file to upload.")
    filename, mime, kind = describe(upload.filename, upload.mimetype)
    ext = os.path.splitext(filename)[1].lower()

    stream = upload.stream
    head = stream.read(_CHUNK)
    if not head:
        raise AssetError("That file was empty.")

    if in_database():
        raw = head + stream.read(DB_MAX_BYTES + 1 - len(head))
        if len(raw) > DB_MAX_BYTES:
            raise AssetError(
                f"With no disk attached, files are kept in the database and "
                f"have to stay under {DB_MAX_BYTES // (1024 * 1024)} MB.")
        if ext in (".h5p", ".zip") and _looks_like_h5p(raw, filename):
            kind, mime = "h5p", "application/zip"
            if not filename.lower().endswith(".h5p"):
                filename = os.path.splitext(filename)[0] + ".h5p"
        _check_whole_bytes(raw, kind)
        return _attach(product, title=title, filename=filename, mime=mime,
                       kind=kind, size=len(raw), data=raw,
                       module_index=module_index, lesson_index=lesson_index)

    disk_name, size = _store_stream(stream, head, MAX_BYTES, ext)
    # A zip only reveals itself as an H5P package from its central directory,
    # which sits at the end, so this has to wait until the file has landed.
    if ext in (".h5p", ".zip"):
        with open(disk_path(disk_name), "rb") as fh:
            if _looks_like_h5p(fh.read(), filename):
                kind, mime = "h5p", "application/zip"
                if not filename.lower().endswith(".h5p"):
                    filename = os.path.splitext(filename)[0] + ".h5p"
    try:
        _check_whole_pdf(disk_path(disk_name), kind)
    except AssetError:
        _safe_remove(disk_path(disk_name))
        raise
    return _attach(product, title=title, filename=filename, mime=mime,
                   kind=kind, size=size, disk_name=disk_name,
                   module_index=module_index, lesson_index=lesson_index)


def add_text(
    product: Product,
    body: str,
    *,
    title: str | None = None,
    module_index: int | None = None,
    lesson_index: int | None = None,
) -> ProductAsset:
    """Attach a written extract typed into Studio. No file involved."""
    text = _clean_body(body)
    name = (title or "").strip()[:160] or "Extract"
    return _attach(
        product, title=name,
        filename=(secure_filename(name) or "extract")[:240] + ".md",
        mime="text/markdown", kind="text",
        size=len(text.encode("utf-8")), body=text,
        module_index=module_index, lesson_index=lesson_index)


def add_note(parent: ProductAsset, body: str, *,
             title: str | None = None) -> ProductAsset:
    """Attach a written extract to one file rather than to the whole module.

    It takes its parent's ``module_index`` so drip gating, which only looks at
    that number, keeps a locked module's notes locked with no special case.
    """
    if parent is None or parent.parent_asset_id is not None:
        raise AssetError("An extract can't have extracts of its own.")
    text = _clean_body(body)
    name = (title or "").strip()[:160] or "Extract"
    return _attach(
        parent.product, title=name,
        filename=(secure_filename(name) or "extract")[:240] + ".md",
        mime="text/markdown", kind="text",
        size=len(text.encode("utf-8")), body=text,
        module_index=parent.module_index,
        lesson_index=parent.lesson_index, parent=parent)


def edit_text(asset: ProductAsset, body: str, *,
              title: str | None = None) -> ProductAsset:
    """Rewrite an extract in place, so a typo doesn't cost a delete and retype."""
    if asset is None or not asset.is_text():
        raise AssetError("That isn't a written extract.")
    text = _clean_body(body)
    asset.title = (title or "").strip()[:160] or asset.title or "Extract"
    asset.body = text
    asset.size = len(text.encode("utf-8"))
    return asset


def _clean_body(body: str) -> str:
    text = (body or "").strip()
    if not text:
        raise AssetError("Write something first.")
    if len(text) > 200_000:
        raise AssetError("That extract is very long — split it into two.")
    return text


def _attach(product: Product, *, title, filename, mime, kind, size,
            disk_name=None, data=None, body=None, module_index=None,
            lesson_index=None,
            parent: ProductAsset | None = None) -> ProductAsset:
    asset = ProductAsset(
        product_id=product.id,
        title=(title or "").strip()[:160] or None,
        filename=filename,
        mime=mime,
        kind=kind,
        size=size,
        disk_name=disk_name,
        data=data,
        body=body,
        sort_order=_next_order(product, module_index, parent),
        module_index=module_index,
        lesson_index=lesson_index,
        parent=parent,
    )
    db.session.add(asset)
    return asset


# --- chunked uploads ---------------------------------------------------------
# Studio sends a big file a slice at a time and we append each slice to a part
# file on the media disk. Nothing about this depends on which worker handles a
# given slice, because the state is the file itself.

#: How long a half-finished upload is kept before it is treated as abandoned.
PART_KEEP_HOURS = 24


def sweep_parts(older_than_hours: int = PART_KEEP_HOURS) -> int:
    """Clear out slices from uploads nobody finished.

    A tab closed midway through a large video leaves its part file behind,
    and nothing ever came back for it. On a disk sized to the library that
    actually exists, a few forgotten gigabytes is the difference between
    room to spare and no room at all.
    """
    folder = parts_dir()
    if not os.path.isdir(folder):
        return 0
    cutoff = time.time() - max(1, int(older_than_hours)) * 3600
    cleared = 0
    for name in os.listdir(folder):
        path = os.path.join(folder, name)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
                cleared += 1
        except OSError:
            continue
    if cleared:
        log.info("uploads: cleared %s abandoned part file(s)", cleared)
    return cleared


def begin_upload(filename: str, declared_size: int) -> str:
    """Reserve a part file and return its id."""
    if declared_size <= 0:
        raise AssetError("That file was empty.")
    if declared_size > max_upload_bytes():
        raise AssetError(
            f"That file is over {max_upload_bytes() // (1024 * 1024)} MB.")
    os.makedirs(parts_dir(), exist_ok=True)
    # Starting one upload is as good a moment as any to clear away the ones
    # that were never finished, and it needs no scheduler to happen.
    try:
        sweep_parts()
    except Exception:
        log.exception("uploads: could not sweep abandoned parts")
    ext = os.path.splitext(secure_filename(filename or ""))[1].lower()
    upload_id = secrets.token_hex(16) + ext
    open(_part_path(upload_id), "wb").close()
    return upload_id


def _part_path(upload_id: str) -> str:
    safe = os.path.basename(upload_id or "")
    if not safe:
        raise AssetError("That upload has expired — start it again.")
    return os.path.join(parts_dir(), safe)


def append_chunk(upload_id: str, data: bytes) -> int:
    """Append one slice. Returns how many bytes have arrived so far."""
    path = _part_path(upload_id)
    if not os.path.isfile(path):
        raise AssetError("That upload has expired — start it again.")
    if os.path.getsize(path) + len(data) > max_upload_bytes():
        _safe_remove(path)
        raise AssetError(
            f"That file is over {max_upload_bytes() // (1024 * 1024)} MB.")
    with open(path, "ab") as fh:
        fh.write(data)
    return os.path.getsize(path)


def abort_upload(upload_id: str) -> None:
    try:
        _safe_remove(_part_path(upload_id))
    except AssetError:
        pass


def finish_upload(product: Product, upload_id: str, filename: str, *,
                  title: str | None = None,
                  module_index: int | None = None,
                  lesson_index: int | None = None) -> ProductAsset:
    """Turn a completed part file into a real asset."""
    path = _part_path(upload_id)
    if not os.path.isfile(path):
        raise AssetError("That upload has expired — start it again.")
    size = os.path.getsize(path)
    if not size:
        _safe_remove(path)
        raise AssetError("That file was empty.")

    name, mime, kind = describe(filename, None)
    ext = os.path.splitext(name)[1].lower()
    if ext in (".h5p", ".zip"):
        with open(path, "rb") as fh:
            if _looks_like_h5p(fh.read(), name):
                kind, mime = "h5p", "application/zip"
                if not name.lower().endswith(".h5p"):
                    name = os.path.splitext(name)[0] + ".h5p"
    # A slice going missing is exactly how a large upload ends up short, so
    # this is the last place to catch it before a buyer does.
    try:
        _check_whole_pdf(path, kind)
    except AssetError:
        _safe_remove(path)
        raise

    if in_database():
        with open(path, "rb") as fh:
            raw = fh.read()
        _safe_remove(path)
        if len(raw) > DB_MAX_BYTES:
            raise AssetError(
                f"With no disk attached, files are kept in the database and "
                f"have to stay under {DB_MAX_BYTES // (1024 * 1024)} MB.")
        return _attach(product, title=title, filename=name, mime=mime,
                       kind=kind, size=len(raw), data=raw,
                       module_index=module_index, lesson_index=lesson_index)

    os.makedirs(storage_dir(), exist_ok=True)
    disk_name = secrets.token_hex(16) + (os.path.splitext(name)[1].lower())
    try:
        os.replace(path, os.path.join(storage_dir(), disk_name))
    except OSError:
        _safe_remove(path)
        raise AssetError("We couldn't save that upload just now — try again.")
    return _attach(product, title=title, filename=name, mime=mime, kind=kind,
                   size=size, disk_name=disk_name, module_index=module_index,
                   lesson_index=lesson_index)
