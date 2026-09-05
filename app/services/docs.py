"""Word files, drawn into pages so a .docx reads like any other PDF here.

Same bargain as the slide decks next door: Word isn't on the host and there is
no LibreOffice to shell out to, so a document is read with python-docx and
painted into a PDF with fpdf2. Headings, paragraphs, bold and italic, lists,
links, pictures and tables come across, which is what a worksheet or a handout
is made of. Columns, headers and footers, footnotes and tracked changes do
not, so a document built around those is better exported to PDF in Word.

The conversion happens once, on upload, and the PDF is what gets stored: from
there on the reader, the page counter, the bookmarks and the download are the
ones every other document already has.
"""
from __future__ import annotations

import logging
import os
from io import BytesIO

from fpdf import FPDF

log = logging.getLogger(__name__)

#: Word files are text; one this big is a folder of photographs in a wrapper.
MAX_BYTES = 25 * 1024 * 1024

#: Drawing takes a moment per paragraph, and somebody is waiting on the other
#: end of the upload. A manuscript's worth of them is better exported to PDF by
#: the program that already knows how than drawn a line at a time here.
MAX_BLOCKS = 8000

#: Word measures in English Metric Units; PDFs here are drawn in points.
EMU_PER_PT = 12700

#: What Word uses when a document never says: letter paper, inch margins,
#: eleven point text.
PAGE = (612.0, 792.0)
MARGIN = 72.0
BODY_PT = 11.0

#: A heading that inherits its size from a theme this reader can't see still
#: has to look like a heading.
HEADING_PT = {1: 20.0, 2: 16.0, 3: 13.5, 4: 12.0, 5: 11.5, 6: 11.0}

#: Core PDF fonts are Latin-1 only, and a document is full of typographer's
#: punctuation, so the same gentle transliteration the deck drawing uses.
_MAP = {
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
    "\u2013": "-", "\u2014": "-", "\u2026": "...", "\u2022": "-",
    "\u00a0": " ", "\u2039": "<", "\u203a": ">", "\u00b7": "-",
    "\u2192": "->", "\u2190": "<-", "\u2713": "v", "\u00ab": "<<",
    "\u00bb": ">>", "\u2011": "-", "\u25cf": "-", "\u25aa": "-",
    "\u2043": "-", "\u2010": "-", "\t": "    ",
}


class DocError(ValueError):
    pass


#: Both office drawers answer to the same few names, so the upload path can
#: hand a file to whichever one knows how to read it.
Error = DocError


def is_doc(filename: str) -> bool:
    return os.path.splitext(filename or "")[1].lower() == ".docx"


def pdf_name(filename: str) -> str:
    """What the drawn document is called once it is a PDF."""
    base = os.path.splitext(os.path.basename(filename or ""))[0] or "document"
    return f"{base}.pdf"


def title_from(filename: str) -> str:
    """The document's own name, for the piece list, without the extension."""
    return (os.path.splitext(os.path.basename(filename or ""))[0]
            .replace("_", " ").strip()[:160] or "Document")


def _t(text) -> str:
    s = str(text or "")
    for bad, plain in _MAP.items():
        s = s.replace(bad, plain)
    return s.encode("latin-1", "replace").decode("latin-1")


def _pt(emu, fallback: float = 0.0) -> float:
    try:
        return float(emu) / EMU_PER_PT
    except (TypeError, ValueError):
        return fallback


def to_pdf(data: bytes) -> bytes:
    """Draw a .docx into a PDF. Raises :class:`DocError` if it can't be read."""
    if not data:
        raise DocError("That file was empty.")
    if len(data) > MAX_BYTES:
        raise DocError(
            f"That document is over {MAX_BYTES // (1024 * 1024)} MB. Save it "
            "as a PDF from Word and upload that instead.")
    try:
        from docx import Document
    except ImportError as exc:  # pragma: no cover - dependency is in requirements
        raise DocError("Word files can't be read on this server yet.") from exc
    try:
        doc = Document(BytesIO(data))
    except Exception as exc:
        raise DocError(
            "That doesn't open as a Word file. If it is an older .doc, or it "
            "came from somewhere else, save it as a PDF and upload that.") from exc

    if _body_length(doc) > MAX_BLOCKS:
        raise DocError(
            "That document is very long. Save it as a PDF from Word and "
            "upload that instead.")

    page, margins = _paper(doc)
    pdf = FPDF(unit="pt", format=page)
    pdf.set_margins(margins[0], margins[1], margins[2])
    pdf.set_auto_page_break(True, margin=margins[3])
    pdf.c_margin = 0
    pdf.add_page()
    pdf.set_font("Helvetica", "", BODY_PT)

    drew, book = False, _Book(doc)
    for block in _blocks(doc):
        try:
            if getattr(block, "_tbl", None) is not None:
                drew = _draw_table(pdf, block) or drew
            else:
                drew = _draw_paragraph(pdf, doc, block, book) or drew
        except Exception:
            log.debug("docs: skipped a block that wouldn't draw", exc_info=True)
    if not drew:
        raise DocError("There is nothing written in that document.")
    return bytes(pdf.output())


def _paper(doc) -> tuple[tuple[float, float], tuple[float, float, float, float]]:
    """The page size and margins the document was written on."""
    try:
        section = doc.sections[0]
    except (IndexError, AttributeError):
        return PAGE, (MARGIN,) * 4
    width = _pt(section.page_width, PAGE[0]) or PAGE[0]
    height = _pt(section.page_height, PAGE[1]) or PAGE[1]
    left = _pt(section.left_margin, MARGIN) or MARGIN
    right = _pt(section.right_margin, MARGIN) or MARGIN
    top = _pt(section.top_margin, MARGIN) or MARGIN
    bottom = _pt(section.bottom_margin, MARGIN) or MARGIN
    # A margin wider than the paper is a document this reader has misread.
    if left + right > width * 0.8 or top + bottom > height * 0.8:
        left = right = top = bottom = MARGIN
    return (width, height), (left, top, right, bottom)


def _body_length(doc) -> int:
    try:
        return sum(1 for _ in doc.element.body.iterchildren())
    except Exception:
        return 0


def _blocks(doc):
    """Paragraphs and tables in the order they were written, not in two lists."""
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    for child in doc.element.body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, doc)
        elif child.tag == qn("w:tbl"):
            yield Table(child, doc)


# --- what a paragraph looks like ---------------------------------------------

_ALIGN = {1: "C", 2: "R", 3: "J"}  # WD_ALIGN_PARAGRAPH centre / right / justify


def _style_chain(style):
    seen = []
    while style is not None and len(seen) < 8:
        seen.append(style)
        style = getattr(style, "base_style", None)
    return seen


def _inherited(chain, attr):
    """A font setting the paragraph leaves to its style, or the style's style."""
    for style in chain:
        try:
            value = getattr(style.font, attr)
        except Exception:
            continue
        if value is not None:
            return value
    return None


class _Look:
    """Everything a paragraph takes from its style, read once.

    Asking python-docx for a paragraph's style means resolving the document's
    default style from scratch, which is by far the slowest thing here. Every
    paragraph in a long document shares a handful of styles, so each one is
    worked out the first time it is seen and remembered by name.
    """

    __slots__ = ("level", "size", "align", "bold", "italic", "underline",
                 "numbering")

    def __init__(self, style):
        chain = _style_chain(style)
        name = (getattr(style, "name", "") or "").lower()
        self.level = _named_level(name)
        self.size = None
        size = _inherited(chain, "size")
        if size is not None:
            try:
                self.size = float(size.pt)
            except Exception:
                self.size = None
        self.bold = _inherited(chain, "bold")
        self.italic = _inherited(chain, "italic")
        self.underline = _inherited(chain, "underline")
        self.align = None
        for step in chain:
            found = getattr(
                getattr(getattr(step, "paragraph_format", None), "alignment", None),
                "value", None)
            if found is not None:
                self.align = found
                break
        self.numbering = [step.element for step in chain
                          if getattr(step, "element", None) is not None]


class _Book:
    """One document's styles and lists, held while it is being drawn."""

    def __init__(self, doc):
        self.doc = doc
        self.lists = _Lists(doc)
        self._looks: dict[str | None, _Look] = {}

    def look(self, paragraph) -> _Look:
        key = _style_name(paragraph)
        found = self._looks.get(key)
        if found is None:
            found = self._looks[key] = _Look(getattr(paragraph, "style", None))
        return found


def _style_name(paragraph) -> str | None:
    """The style a paragraph names, straight off its XML and without resolving it."""
    from docx.oxml.ns import qn
    props = paragraph._p.find(qn("w:pPr"))
    named = props.find(qn("w:pStyle")) if props is not None else None
    return named.get(qn("w:val")) if named is not None else None


def _named_level(name: str) -> int:
    if name.startswith("title"):
        return 1
    if name.startswith("subtitle"):
        return 3
    if not name.startswith("heading"):
        return 0
    tail = name.replace("heading", "").strip()
    return int(tail) if tail.isdigit() else 1


def _size(paragraph, look: _Look) -> float:
    for run in paragraph.runs:
        try:
            if run.font.size is not None:
                return float(run.font.size.pt)
        except Exception:
            continue
    if look.size is not None:
        return look.size
    if look.level:
        return HEADING_PT.get(look.level, BODY_PT)
    return BODY_PT


def _align(paragraph, look: _Look) -> str:
    value = getattr(getattr(paragraph, "alignment", None), "value", None)
    if value is None:
        value = look.align
    return _ALIGN.get(value, "L")


def _rgb(color) -> tuple[int, int, int] | None:
    try:
        if color is None or color.type is None or color.rgb is None:
            return None
        raw = color.rgb
    except Exception:
        return None
    try:
        return (raw[0], raw[1], raw[2])
    except (TypeError, IndexError):
        try:
            value = int(str(raw), 16)
        except ValueError:
            return None
        return ((value >> 16) & 255, (value >> 8) & 255, value & 255)


def _list_ref(paragraph, look: _Look) -> tuple[int, int] | None:
    """Which list and which rung of it this line is on, if it is in one.

    Word writes the list onto the paragraph when somebody clicks the bullet
    button, but onto the style when the paragraph is styled as a list, so both
    have to be asked.
    """
    from docx.oxml.ns import qn
    for holder in [paragraph._p] + look.numbering:
        props = holder.find(qn("w:pPr"))
        numbering = props.find(qn("w:numPr")) if props is not None else None
        if numbering is None:
            continue
        return (_val(numbering.find(qn("w:numId")), 0),
                max(0, min(8, _val(numbering.find(qn("w:ilvl")), 0))))
    return None


def _val(element, fallback: int) -> int:
    from docx.oxml.ns import qn
    try:
        return int(element.get(qn("w:val")))
    except (AttributeError, TypeError, ValueError):
        return fallback


class _Lists:
    """The bullets and the numbers, counted as the document goes by."""

    def __init__(self, doc):
        self.formats = _numbering_formats(doc)
        self.counts: dict[tuple[int, int], int] = {}

    def marker(self, ref: tuple[int, int]) -> str:
        kind = self.formats.get(ref[0], {}).get(ref[1], "bullet")
        if kind in ("bullet", "none", ""):
            return "-"
        self.counts[ref] = self.counts.get(ref, 0) + 1
        for deeper in [k for k in self.counts if k[0] == ref[0] and k[1] > ref[1]]:
            self.counts[deeper] = 0
        count = self.counts[ref]
        if kind in ("lowerLetter", "upperLetter"):
            letter = chr(ord("a") + (count - 1) % 26)
            return (letter.upper() if kind == "upperLetter" else letter) + "."
        return f"{count}."


def _numbering_formats(doc) -> dict[int, dict[int, str]]:
    """How each list in the document marks its rungs: bullets or numbers."""
    from docx.oxml.ns import qn
    try:
        root = doc.part.numbering_part.element
    except Exception:
        return {}
    shapes: dict[int, dict[int, str]] = {}
    for abstract in root.findall(qn("w:abstractNum")):
        levels = {}
        for level in abstract.findall(qn("w:lvl")):
            try:
                rung = int(level.get(qn("w:ilvl")))
            except (TypeError, ValueError):
                continue
            fmt = level.find(qn("w:numFmt"))
            levels[rung] = (fmt.get(qn("w:val")) if fmt is not None else "bullet")
        key = abstract.get(qn("w:abstractNumId"))
        if key is not None:
            shapes[int(key)] = levels
    out: dict[int, dict[int, str]] = {}
    for num in root.findall(qn("w:num")):
        link = num.find(qn("w:abstractNumId"))
        try:
            out[int(num.get(qn("w:numId")))] = shapes.get(_val(link, -1), {})
        except (TypeError, ValueError):
            continue
    return out


def _indent(paragraph) -> float:
    """How far in from the margin the paragraph itself asks to sit."""
    try:
        left = paragraph.paragraph_format.left_indent
        return max(0.0, min(200.0, float(left.pt))) if left is not None else 0.0
    except Exception:
        return 0.0


def _spacing(paragraph, size: float) -> tuple[float, float, float]:
    """Line height, and the gaps this paragraph wants above and below it."""
    fmt = getattr(paragraph, "paragraph_format", None)
    line = size * 1.34
    try:
        if fmt is not None and fmt.line_spacing:
            spacing = float(fmt.line_spacing)
            line = size * 1.34 * spacing if spacing < 5 else float(spacing)
    except Exception:
        pass
    before = after = 0.0
    try:
        if fmt is not None and fmt.space_before is not None:
            before = max(0.0, min(48.0, float(fmt.space_before.pt)))
        if fmt is not None and fmt.space_after is not None:
            after = max(0.0, min(48.0, float(fmt.space_after.pt)))
        elif fmt is not None:
            after = size * 0.5
    except Exception:
        after = size * 0.5
    return max(size, min(size * 3, line)), before, after


def _face(run, look: _Look) -> str:
    """Bold, italic and underline, from the run or from the style behind it."""
    def ask(attr):
        try:
            value = getattr(run.font, attr) if run is not None else None
        except Exception:
            value = None
        if value is None:
            value = getattr(look, attr)
        return bool(value)

    face = ("B" if ask("bold") or look.level else "") + ("I" if ask("italic") else "")
    return face + ("U" if ask("underline") else "")


# --- drawing -----------------------------------------------------------------

def _page_break(paragraph) -> bool:
    """A break the writer put in by hand, rather than one the text ran into."""
    from docx.oxml.ns import qn
    return any(br.get(qn("w:type")) == "page"
               for br in paragraph._p.iter(qn("w:br")))


def _draw_paragraph(pdf, doc, paragraph, book: _Book) -> bool:
    if _page_break(paragraph):
        pdf.add_page()
    look = book.look(paragraph)
    size = _size(paragraph, look)
    line, before, after = _spacing(paragraph, size)

    drew = False
    for blob, want in _pictures(doc, paragraph):
        drew = _draw_image(pdf, blob, want) or drew

    text = _t(paragraph.text).strip()
    if not text:
        if not drew:
            pdf.ln(line * 0.6)
        return drew

    if before:
        pdf.ln(before)
    left = pdf.l_margin
    indent = _indent(paragraph)
    ref = _list_ref(paragraph, look)
    if ref is not None:
        indent = max(indent, 0.0) + ref[1] * 16.0
    if pdf.w - pdf.r_margin - left - indent < 40:
        indent = 0.0

    pdf.set_left_margin(left + indent)
    pdf.set_x(left + indent)
    if ref is not None:
        # Hung off to the side, so a line that wraps sits under its own words
        # rather than back under the bullet.
        gutter = max(14.0, size * 1.25)
        pdf.set_font("Helvetica", "", size)
        pdf.set_text_color(26, 26, 26)
        pdf.cell(gutter, line, text=book.lists.marker(ref))
        pdf.set_left_margin(left + indent + gutter)

    align = _align(paragraph, look)
    if align == "L" and _mixed(paragraph, look):
        _write_runs(pdf, paragraph, look, size, line)
    else:
        width = pdf.w - pdf.r_margin - pdf.get_x()
        pdf.set_font("Helvetica", _face(_first_run(paragraph), look), size)
        pdf.set_text_color(*(_run_colour(paragraph) or (26, 26, 26)))
        pdf.multi_cell(max(20.0, width), line, text=text, align=align,
                       new_x="LMARGIN", new_y="NEXT")
    pdf.set_left_margin(left)
    pdf.set_x(left)
    if after:
        pdf.ln(after)
    return True


def _first_run(paragraph):
    runs = paragraph.runs
    return runs[0] if runs else None


def _run_colour(paragraph):
    for run in paragraph.runs:
        found = _rgb(getattr(run.font, "color", None))
        if found:
            return found
    return None


def _mixed(paragraph, look: _Look) -> bool:
    """Whether the line changes face part-way, and so has to be written run by run."""
    faces = {_face(run, look) for run in paragraph.runs}
    return len(faces) > 1


def _write_runs(pdf, paragraph, look: _Look, size: float, line: float) -> None:
    """One line built from its runs, so a bold word in it stays bold."""
    for piece, link in _inline(paragraph):
        text = _t(piece.text)
        if not text:
            continue
        face = _face(piece, look)
        if link and "U" not in face:
            face += "U"
        pdf.set_font("Helvetica", face, size)
        colour = _rgb(getattr(piece.font, "color", None))
        pdf.set_text_color(*(colour or ((21, 71, 145) if link else (26, 26, 26))))
        pdf.write(line, text, link=link or "")
    pdf.ln(line)


def _inline(paragraph):
    """Runs in order, each with the address behind it if it is a link."""
    try:
        from docx.text.hyperlink import Hyperlink
        content = list(paragraph.iter_inner_content())
    except Exception:
        return [(run, "") for run in paragraph.runs]
    out = []
    for item in content:
        if isinstance(item, Hyperlink):
            address = (item.address or "").strip()
            out.extend((run, address) for run in item.runs)
        else:
            out.append((item, ""))
    return out


def _pictures(doc, paragraph):
    """Every picture placed in this paragraph, with the size Word gave it."""
    from docx.oxml.ns import qn
    out = []
    for anchor in paragraph._p.iter():
        if anchor.tag not in (qn("wp:inline"), qn("wp:anchor")):
            continue
        blip = anchor.find(".//" + qn("a:blip"))
        if blip is None:
            continue
        rel = blip.get(qn("r:embed")) or blip.get(qn("r:link"))
        if not rel:
            continue
        try:
            blob = doc.part.related_parts[rel].blob
        except Exception:
            continue
        extent = anchor.find(qn("wp:extent"))
        want = None
        if extent is not None:
            want = (_pt(extent.get("cx")), _pt(extent.get("cy")))
        out.append((blob, want))
    return out


def _draw_image(pdf, blob: bytes, want) -> bool:
    """The picture at the size it was placed, shrunk to the page if it won't fit."""
    room = pdf.w - pdf.l_margin - pdf.r_margin
    width, height = want or (0.0, 0.0)
    if not width or not height:
        try:
            from PIL import Image
            with Image.open(BytesIO(blob)) as img:
                width, height = img.size
                width, height = width * 0.75, height * 0.75  # 96dpi pixels to points
        except Exception:
            return False
    if width > room:
        height, width = height * (room / width), room
    tall = pdf.h - pdf.b_margin - pdf.t_margin
    if height > tall:
        width, height = width * (tall / height), tall
    if pdf.get_y() + height > pdf.h - pdf.b_margin:
        pdf.add_page()
    try:
        pdf.image(BytesIO(blob), x=pdf.l_margin, y=pdf.get_y(), w=width, h=height)
    except Exception:
        log.debug("docs: a picture wouldn't draw", exc_info=True)
        return False
    pdf.set_y(pdf.get_y() + height + 6)
    return True


def _draw_table(pdf, table) -> bool:
    """A plain grid: every cell's writing, ruled, splitting across pages if long."""
    room = pdf.w - pdf.l_margin - pdf.r_margin
    columns = []
    for column in table.columns:
        columns.append(max(1.0, _pt(getattr(column, "width", None), 0.0) or 1.0))
    total = sum(columns) or 1.0
    if abs(total - room) > room * 0.5:  # widths Word never wrote down
        columns = [1.0] * len(columns)
        total = float(len(columns) or 1)
    widths = [room * (c / total) for c in columns]
    if not widths:
        return False

    size = min(BODY_PT, max(7.5, BODY_PT))
    line = size * 1.3
    pdf.set_draw_color(190, 190, 190)
    pdf.set_line_width(0.6)
    drew = False
    for row in table.rows:
        cells = list(row.cells)[:len(widths)]
        texts = [_t(cell.text).strip() for cell in cells]
        pdf.set_font("Helvetica", "", size)
        tall = line
        for text, width in zip(texts, widths):
            if not text:
                continue
            needed = pdf.multi_cell(width - 8, line, text=text, align="L",
                                    dry_run=True, output="HEIGHT")
            tall = max(tall, needed + 6)
        if pdf.get_y() + tall > pdf.h - pdf.b_margin:
            pdf.add_page()
        top, left = pdf.get_y(), pdf.l_margin
        for text, width in zip(texts, widths):
            pdf.rect(left, top, width, tall)
            if text:
                pdf.set_xy(left + 4, top + 3)
                pdf.set_text_color(26, 26, 26)
                pdf.multi_cell(width - 8, line, text=text, align="L",
                               new_x="LEFT", new_y="NEXT")
            left += width
        pdf.set_xy(pdf.l_margin, top + tall)
        drew = True
    if drew:
        pdf.ln(8)
    return drew
