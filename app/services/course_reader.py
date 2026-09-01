"""Course reading progress + purchase → catalog product linking."""
from __future__ import annotations

import zipfile
from io import BytesIO
from pathlib import Path

from flask import current_app
from sqlalchemy import func

from ..extensions import db
from ..models import CourseProgress, Product, ProductAsset, ShopPurchase, utcnow


def catalog_product_for_purchase(purchase: ShopPurchase) -> Product | None:
    """Match a shop purchase to a Studio catalogue product via Stripe price id."""
    if purchase is None:
        return None
    keys = []
    for raw in (purchase.variant_id, purchase.product_id):
        key = (raw or "").strip()
        if key and key not in keys:
            keys.append(key)
    for key in keys:
        row = Product.query.filter_by(stripe_price_id=key).first()
        if row:
            return row
        row = Product.query.filter_by(ls_variant_id=key).first()
        if row:
            return row
    # Fallback: exact title match (helps older purchases).
    name = (purchase.product_name or "").strip()
    if name:
        return Product.query.filter(func.lower(Product.title) == name.lower()).first()
    return None


def primary_asset(product: Product | None) -> ProductAsset | None:
    if product is None:
        return None
    top = product.top_level_assets()
    return top[0] if top else None


def general_asset(product: Product | None) -> ProductAsset | None:
    """First file that isn't tied to a module, so it's always readable."""
    if product is None:
        return None
    for asset in product.top_level_assets():
        if not asset.module_index:
            return asset
    return None


def open_module(modules: list[dict], wanted: int | None) -> dict | None:
    """The module to open: the one asked for, else the first that's ready."""
    ready = [m for m in modules if m.get("contents") and m.get("unlocked")]
    if not ready:
        return None
    if wanted:
        for row in ready:
            if row["number"] == wanted:
                return row
    return ready[0]


def open_item(module: dict | None, wanted: int | None) -> ProductAsset | None:
    """Which piece of a module to show: the one asked for, else the first.

    A module can hold several videos, documents and written extracts, so the
    reader always has a list to choose from rather than a single file.
    """
    contents = (module or {}).get("contents") or []
    if not contents:
        return None
    if wanted:
        for item in contents:
            if item.id == wanted:
                return item
    return contents[0]


def owned_purchase(user, purchase_id: int) -> ShopPurchase | None:
    purchase = db.session.get(ShopPurchase, purchase_id)
    if (purchase is None
            or not user
            or not getattr(user, "is_authenticated", False)
            or purchase.user_id != user.id
            or purchase.status != "linked"):
        return None
    return purchase


def get_progress(user_id: int, purchase_id: int) -> CourseProgress | None:
    return (CourseProgress.query
            .filter_by(user_id=user_id, shop_purchase_id=purchase_id)
            .first())


def progress_map_for(user_id: int, purchase_ids: list[int]) -> dict[int, CourseProgress]:
    if not purchase_ids:
        return {}
    rows = (CourseProgress.query
            .filter(CourseProgress.user_id == user_id,
                    CourseProgress.shop_purchase_id.in_(purchase_ids))
            .all())
    return {r.shop_purchase_id: r for r in rows}


def save_progress(
    *,
    user_id: int,
    purchase_id: int,
    product_id: int | None,
    current_page: int,
    total_pages: int,
    module_index: int | None = None,
) -> CourseProgress:
    page = max(1, int(current_page or 1))
    total = max(0, int(total_pages or 0))
    if total > 0:
        page = min(page, total)
        percent = int(round(100 * page / total))
        percent = max(0, min(100, percent))
    else:
        percent = 0

    row = get_progress(user_id, purchase_id)
    if row is None:
        row = CourseProgress(
            user_id=user_id,
            shop_purchase_id=purchase_id,
            product_id=product_id,
        )
        db.session.add(row)
    row.product_id = product_id or row.product_id
    row.module_index = module_index or None
    row.current_page = page
    row.total_pages = total
    row.percent = percent
    row.updated_at = utcnow()
    return row


def toggle_bookmark(
    *,
    user_id: int,
    purchase_id: int,
    product_id: int | None,
    page: int,
) -> tuple[CourseProgress, bool]:
    """Add or remove a bookmarked page. Returns (row, is_bookmarked)."""
    page = max(1, int(page or 1))
    row = get_progress(user_id, purchase_id)
    if row is None:
        row = CourseProgress(
            user_id=user_id,
            shop_purchase_id=purchase_id,
            product_id=product_id,
            current_page=page,
        )
        db.session.add(row)
    pages = row.bookmarks()
    if page in pages:
        pages = [p for p in pages if p != page]
        bookmarked = False
    else:
        pages.append(page)
        bookmarked = True
    row.set_bookmarks(pages)
    row.updated_at = utcnow()
    return row, bookmarked


def save_form_data(
    *,
    user_id: int,
    purchase_id: int,
    product_id: int | None,
    form_data: dict,
) -> CourseProgress:
    """Persist a buyer's fillable-PDF answers for this purchase."""
    row = get_progress(user_id, purchase_id)
    if row is None:
        row = CourseProgress(
            user_id=user_id,
            shop_purchase_id=purchase_id,
            product_id=product_id,
        )
        db.session.add(row)
    row.product_id = product_id or row.product_id
    row.set_form_data(form_data if isinstance(form_data, dict) else {})
    row.updated_at = utcnow()
    return row


def h5p_cache_dir(asset_id: int) -> Path:
    root = Path(current_app.instance_path) / "h5p_cache" / str(asset_id)
    return root


def ensure_h5p_extracted(asset: ProductAsset) -> Path:
    """Extract an .h5p (zip) package once; reuse on later opens."""
    dest = h5p_cache_dir(asset.id)
    marker = dest / ".ready"
    if marker.is_file() and (dest / "h5p.json").is_file():
        return dest
    dest.mkdir(parents=True, exist_ok=True)
    from . import assets as asset_svc
    with zipfile.ZipFile(BytesIO(asset_svc.read_bytes(asset))) as zf:
        zf.extractall(dest)
    marker.write_text("ok", encoding="utf-8")
    return dest


def safe_h5p_file(asset_id: int, relpath: str) -> Path | None:
    """Resolve a path inside an extracted H5P package (no traversal)."""
    base = h5p_cache_dir(asset_id).resolve()
    if not base.is_dir():
        return None
    cleaned = (relpath or "").replace("\\", "/").lstrip("/")
    if not cleaned or ".." in cleaned.split("/"):
        return None
    target = (base / cleaned).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return None
    if not target.is_file():
        return None
    return target
