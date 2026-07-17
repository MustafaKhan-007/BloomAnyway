"""The "My Journey" keepsake — a designed PDF of a member's showing-up story:
their streaks, the days they returned, and the lines they held onto.

Built with fpdf2 (pure Python, no system libraries — deploys anywhere). Core
fonts are Latin-1 only, so all user text is gently transliterated first.
"""
from datetime import date

from fpdf import FPDF

from ..extensions import db
from ..models import CheckIn, Quote, QuoteFavorite
from .settings import get_setting

# brand palette (Bloom Anyway)
PLUM = (122, 46, 98)
BERRY = (160, 58, 124)
ROSE = (224, 138, 109)
GOLD = (239, 167, 51)
INK = (43, 38, 34)
INK_SOFT = (107, 97, 89)
IVORY = (250, 245, 238)
IVORY_DEEP = (243, 233, 218)

# smart punctuation -> ASCII so core PDF fonts never choke
_MAP = {
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
    "\u2013": "-", "\u2014": "-", "\u2026": "...", "\u2022": "-",
    "\u00a0": " ", "\u2039": "<", "\u203a": ">", "\u00b7": "-",
}


def _t(s) -> str:
    s = str(s or "")
    for k, v in _MAP.items():
        s = s.replace(k, v)
    return s.encode("latin-1", "replace").decode("latin-1")


def _grad(pdf, x, y, w, h, c1, c2, steps=80):
    """A left-to-right gradient band, painted as thin slices."""
    sw = w / steps
    for i in range(steps):
        t = i / (steps - 1)
        r = round(c1[0] + (c2[0] - c1[0]) * t)
        g = round(c1[1] + (c2[1] - c1[1]) * t)
        b = round(c1[2] + (c2[2] - c1[2]) * t)
        pdf.set_fill_color(r, g, b)
        pdf.rect(x + i * sw, y, sw + 0.4, h, "F")


class _Journey(FPDF):
    site_title = "Bloom Anyway"
    handle = ""

    def header(self):
        _grad(self, 0, 0, self.w, 4.5, PLUM, GOLD)

    def footer(self):
        self.set_y(-13)
        self.set_font("Helvetica", "", 8)
        self.set_text_color(*INK_SOFT)
        tag = self.site_title + ("   -   " + self.handle if self.handle else "")
        self.cell(0, 8, _t(tag), align="C")


def _month_year(d) -> str:
    return d.strftime("%B %Y") if d else ""


def build_journey_pdf(user) -> bytes:
    title = get_setting("site_title") or "Bloom Anyway"
    ig = get_setting("instagram_url") or ""
    handle = ""
    if "instagram.com/" in ig:
        h = ig.split("instagram.com/")[-1].strip().strip("/")
        if h:
            handle = "@" + h.lstrip("@")

    favorites = (db.session.query(Quote).join(QuoteFavorite)
                 .filter(QuoteFavorite.user_id == user.id)
                 .order_by(QuoteFavorite.created_at.desc()).all())
    days = [c.day for c in (CheckIn.query.filter_by(user_id=user.id)
                            .order_by(CheckIn.day.desc()).all())]

    pdf = _Journey(orientation="P", unit="mm", format="A4")
    pdf.site_title = _t(title)
    pdf.handle = _t(handle)
    pdf.set_auto_page_break(True, margin=22)
    pdf.set_margins(20, 20, 20)
    pdf.add_page()
    W = pdf.w - 40  # content width

    # --- cover ---------------------------------------------------------------
    pdf.set_y(30)
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_text_color(*BERRY)
    pdf.cell(0, 6, _t(title.upper()), align="C")
    pdf.ln(11)
    pdf.set_font("Times", "B", 42)
    pdf.set_text_color(*PLUM)
    pdf.cell(0, 18, "My Journey", align="C")
    pdf.ln(20)
    pdf.set_font("Times", "I", 15)
    pdf.set_text_color(*INK_SOFT)
    pdf.cell(0, 8, _t(user.public_name()), align="C")
    pdf.ln(9)
    since = _month_year(getattr(user, "created_at", None))
    if since:
        pdf.set_font("Helvetica", "", 10)
        pdf.cell(0, 6, _t("showing up since " + since), align="C")
        pdf.ln(8)

    pdf.ln(6)
    _grad(pdf, pdf.w / 2 - 30, pdf.get_y(), 60, 1.4, ROSE, GOLD)
    pdf.ln(12)

    # --- stat trio -----------------------------------------------------------
    stats = [
        (str(user.streak_display()), "Current streak"),
        (str(user.longest_streak or 0), "Longest streak"),
        (str(user.total_checkins or len(days)), "Days you showed up"),
    ]
    col = W / 3
    top = pdf.get_y()
    box_h = 30
    for i, (num, label) in enumerate(stats):
        x = 20 + i * col
        pdf.set_fill_color(*IVORY_DEEP)
        pdf.rect(x + 3, top, col - 6, box_h, "F")
        pdf.set_xy(x + 3, top + 5)
        pdf.set_font("Times", "B", 30)
        pdf.set_text_color(*GOLD)
        pdf.cell(col - 6, 14, _t(num), align="C")
        pdf.set_xy(x + 3, top + 20)
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(*INK_SOFT)
        pdf.cell(col - 6, 6, _t(label), align="C")
    pdf.set_y(top + box_h + 14)

    # --- the days you returned ----------------------------------------------
    _section(pdf, "The days you returned")
    if days:
        pdf.set_font("Helvetica", "", 11)
        pdf.set_text_color(*INK)
        lead = (f"Across {len(days)} day{'s' if len(days) != 1 else ''}, you chose to "
                f"come back to yourself. Here are the most recent:")
        pdf.multi_cell(W, 6, _t(lead))
        pdf.ln(2)
        recent = days[:60]
        line = "   ".join(_fmt_day(d) for d in recent)
        pdf.set_font("Helvetica", "", 10)
        pdf.set_text_color(*BERRY)
        pdf.multi_cell(W, 6.5, _t(line))
    else:
        pdf.set_font("Times", "I", 12)
        pdf.set_text_color(*INK_SOFT)
        pdf.multi_cell(W, 7, _t("Your story starts the first day you tap \u201cI showed up "
                                "today.\u201d Come back tomorrow, and the day after."))
    pdf.ln(8)

    # --- lines you held onto (the heart) ------------------------------------
    _section(pdf, "Lines you held onto")
    if favorites:
        for q in favorites:
            _quote_block(pdf, q, W)
    else:
        pdf.set_font("Times", "I", 12)
        pdf.set_text_color(*INK_SOFT)
        pdf.multi_cell(W, 7, _t("When a line lands, tap the heart on the quotes page "
                                "and it will live here \u2014 and in this keepsake."))
    pdf.ln(6)

    # --- closing -------------------------------------------------------------
    if pdf.get_y() > pdf.h - 60:
        pdf.add_page()
    _grad(pdf, 20, pdf.get_y(), W, 1.2, PLUM, GOLD)
    pdf.ln(8)
    pdf.set_font("Times", "I", 13)
    pdf.set_text_color(*PLUM)
    pdf.multi_cell(W, 7, _t("Keep going. The person you are becoming is already proud "
                            "of the one who kept showing up."), align="C")

    out = pdf.output()
    return bytes(out)


def _fmt_day(d) -> str:
    # %-d isn't portable (Windows); build the day number without a leading zero
    return f"{d.strftime('%b')} {d.day}" if hasattr(d, "strftime") else str(d)


def _section(pdf, label):
    pdf.set_font("Times", "B", 18)
    pdf.set_text_color(*PLUM)
    pdf.cell(0, 10, _t(label))
    pdf.ln(11)
    pdf.set_draw_color(*ROSE)
    pdf.set_line_width(0.4)
    y = pdf.get_y()
    pdf.line(20, y, pdf.w - 20, y)
    pdf.ln(5)


def _quote_block(pdf, quote, W):
    text = _t(quote.text)
    pdf.set_font("Times", "I", 13)
    # measure height by splitting; fpdf2 wraps in multi_cell
    start_y = pdf.get_y()
    # page-break guard: rough estimate
    if start_y > pdf.h - 45:
        pdf.add_page()
        start_y = pdf.get_y()
    left = 20
    pdf.set_xy(left + 6, start_y)
    pdf.set_text_color(*INK)
    pdf.multi_cell(W - 8, 7, _t("\u201c") + text + _t("\u201d"))
    end_y = pdf.get_y()
    if quote.author:
        pdf.set_x(left + 6)
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(*INK_SOFT)
        pdf.cell(0, 6, _t("\u2014 " + quote.author))
        pdf.ln(6)
        end_y = pdf.get_y()
    # rose accent bar down the left of the block
    pdf.set_fill_color(*ROSE)
    pdf.rect(left, start_y, 2, max(end_y - start_y - 1, 4), "F")
    pdf.ln(4)
