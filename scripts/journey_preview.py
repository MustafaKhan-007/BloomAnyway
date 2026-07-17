"""Generate a sample My Journey PDF with realistic data and rasterize page 1
to instance/journey_preview.png for a visual check.
Run: python scripts/journey_preview.py
"""
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("LEMONSQUEEZY_WEBHOOK_SECRET", "x")

from app import create_app
from app.config import DevConfig
from app.extensions import db
from app.models import CheckIn, Quote, QuoteFavorite, User


class C(DevConfig):
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"


app = create_app(C)
out_dir = Path(__file__).resolve().parents[1] / "instance"
out_dir.mkdir(exist_ok=True)

with app.app_context():
    db.create_all()
    u = User(email="dawn@example.com", display_name="Dawn",
             created_at=datetime(2026, 3, 2))
    u.set_password("x-strong-pass-123")
    u.current_streak = 12
    u.longest_streak = 41
    u.total_checkins = 63
    u.last_checkin_date = date.today()
    db.session.add(u)
    db.session.flush()

    quotes = [
        ("You do not have to be ready. You only have to begin.", "Bloom Anyway"),
        ("The morning does not ask what you did last night. It just arrives, gold and forgiving.", None),
        ("Small returns become a whole life. Keep coming back.", "Maya"),
        ("Grief is love with nowhere to go, so give it somewhere: a page, a walk, a breath.", None),
        ("You are not behind. You are exactly here, and here is where it starts.", "Bloom Anyway"),
    ]
    for text, author in quotes:
        q = Quote(text=text, author=author, category="comfort")
        db.session.add(q)
        db.session.flush()
        db.session.add(QuoteFavorite(user_id=u.id, quote_id=q.id))

    for i in range(63):
        db.session.add(CheckIn(user_id=u.id, day=date.today() - timedelta(days=i)))
    db.session.commit()

    from app.services.journey import build_journey_pdf
    pdf = build_journey_pdf(u)

pdf_path = out_dir / "journey_preview.pdf"
pdf_path.write_bytes(pdf)

import fitz  # pymupdf

doc = fitz.open(pdf_path)
png = out_dir / "journey_preview.png"
doc[0].get_pixmap(matrix=fitz.Matrix(2, 2)).save(png)
print(f"PDF pages: {doc.page_count}. Wrote {pdf_path} and {png}")
