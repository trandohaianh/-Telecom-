"""Render the product portfolio Markdown to polished DOCX and PDF files."""
from __future__ import annotations

from pathlib import Path
import re
from xml.sax.saxutils import escape

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (PageBreak, Paragraph, Preformatted, SimpleDocTemplate,
                                Spacer, Table, TableStyle, HRFlowable, KeepTogether)


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs" / "PORTFOLIO_THREE_SYSTEMS.md"
DOCX_OUT = ROOT / "docs" / "PORTFOLIO_THREE_SYSTEMS.docx"
PDF_OUT = ROOT / "docs" / "PORTFOLIO_THREE_SYSTEMS.pdf"
NAVY = "16324F"
TEAL = "087E8B"
PALE = "EAF1F6"
GOLD = "D99A2B"


def inline_markup(text: str) -> str:
    safe = escape(text.replace("`", ""))
    safe = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", safe)
    safe = re.sub(r"\*(.+?)\*", r"<i>\1</i>", safe)
    return safe


def parse_blocks(text: str):
    lines = text.splitlines()
    blocks = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.strip() == "---":
            i += 1
            continue
        if line.startswith("```"):
            i += 1
            code = []
            while i < len(lines) and not lines[i].startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1
            blocks.append(("code", "\n".join(code)))
            continue
        if line.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                row = [part.strip() for part in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in row):
                    rows.append(row)
                i += 1
            blocks.append(("table", rows))
            continue
        if line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            blocks.append(("heading", (level, line[level:].strip())))
            i += 1
            continue
        if line.startswith(">"):
            quote_lines = []
            while i < len(lines) and lines[i].startswith(">"):
                quote_lines.append(lines[i].lstrip("> "))
                i += 1
            blocks.append(("quote", " ".join(quote_lines)))
            continue
        if re.match(r"\s*[-*] ", line):
            items = []
            while i < len(lines) and re.match(r"\s*[-*] ", lines[i]):
                indent = len(lines[i]) - len(lines[i].lstrip())
                items.append(("bullet", indent, re.sub(r"^\s*[-*] ", "", lines[i])))
                i += 1
            blocks.extend(items)
            continue
        if re.match(r"\s*\d+\. ", line):
            items = []
            while i < len(lines) and re.match(r"\s*\d+\. ", lines[i]):
                items.append(("number", 0, re.sub(r"^\s*\d+\. ", "", lines[i])))
                i += 1
            blocks.extend(items)
            continue
        paragraph = [line.strip()]
        i += 1
        while i < len(lines) and lines[i].strip() and lines[i].strip() != "---" \
                and not lines[i].startswith(("#", "|", "```", ">")) \
                and not re.match(r"\s*([-*] |\d+\. )", lines[i]):
            paragraph.append(lines[i].strip())
            i += 1
        blocks.append(("paragraph", " ".join(paragraph)))
    return blocks


def set_cell_shading(cell, fill: str):
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:fill"), fill)
    tc_pr.append(shd)


def set_repeat_table_header(row):
    tr_pr = row._tr.get_or_add_trPr()
    repeat = OxmlElement("w:tblHeader")
    repeat.set(qn("w:val"), "true")
    tr_pr.append(repeat)


def set_cell_text(cell, text: str, *, bold=False, color=None, size=8.3):
    cell.text = ""
    cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
    p = cell.paragraphs[0]
    p.paragraph_format.space_after = Pt(2)
    p.paragraph_format.space_before = Pt(2)
    run = p.add_run(text.replace("`", ""))
    run.font.name = "Arial"
    run.font.size = Pt(size)
    run.bold = bold
    if color:
        run.font.color.rgb = RGBColor.from_string(color)


def add_page_field(paragraph):
    run = paragraph.add_run()
    field = OxmlElement("w:fldSimple")
    field.set(qn("w:instr"), "PAGE")
    run._r.addnext(field)


def render_docx(blocks):
    doc = Document()
    sec = doc.sections[0]
    sec.top_margin, sec.bottom_margin = Inches(.65), Inches(.65)
    sec.left_margin, sec.right_margin = Inches(.78), Inches(.78)
    styles = doc.styles
    for name in ("Normal", "Body Text"):
        styles[name].font.name = "Arial"
        styles[name].font.size = Pt(9.3)
        styles[name].font.color.rgb = RGBColor(38, 52, 68)
        styles[name].paragraph_format.space_after = Pt(5)
        styles[name].paragraph_format.line_spacing = 1.08
    for name, size, color in (("Title", 29, NAVY), ("Heading 1", 19, NAVY),
                              ("Heading 2", 14, TEAL), ("Heading 3", 11, NAVY)):
        st = styles[name]
        st.font.name = "Arial"
        st.font.size = Pt(size)
        st.font.bold = True
        st.font.color.rgb = RGBColor.from_string(color)
        st.paragraph_format.space_before = Pt(12 if name != "Title" else 0)
        st.paragraph_format.space_after = Pt(6)
        st.paragraph_format.keep_with_next = True

    header = sec.header.paragraphs[0]
    header.text = "PRODUCT PORTFOLIO  |  TELECOM · DATA · EDUCATION"
    header.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    for r in header.runs:
        r.font.name, r.font.size, r.font.color.rgb = "Arial", Pt(7), RGBColor.from_string(TEAL)
    footer = sec.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    rr = footer.add_run("Hồ sơ kiến trúc · 28/10/2026   |   Trang ")
    rr.font.size, rr.font.color.rgb = Pt(8), RGBColor.from_string("667788")
    add_page_field(footer)

    doc.add_paragraph("HỒ SƠ SẢN PHẨM", style="Subtitle")
    doc.add_paragraph("Ba hệ thống, một tầm nhìn dữ liệu", style="Title")
    p = doc.add_paragraph()
    r = p.add_run("FRAUD TELECOM   /   SPARK–KAFKA   /   EDU DSS")
    r.bold, r.font.size, r.font.color.rgb = True, Pt(13), RGBColor.from_string(TEAL)
    doc.add_paragraph("Kiến trúc · Thuật toán · Kết quả · Người dùng · Rủi ro · Lộ trình")
    doc.add_paragraph("Bản trình bày cho lãnh đạo, khách hàng viễn thông và hội đồng kỹ thuật")
    doc.add_paragraph("28 tháng 10 năm 2026")
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(30)
    r = p.add_run("Kết luận trước: nền tảng kỹ thuật đã có bằng chứng chức năng, nhưng mức độ trưởng thành khác nhau. Telecom chưa đạt p99 30 ms ổn định; Spark chưa chứng minh scale đa máy; Edu DSS đang chờ pipeline nghiệp vụ và IAM thật.")
    r.bold = True
    r.font.size = Pt(12)
    r.font.color.rgb = RGBColor.from_string(NAVY)
    doc.add_page_break()

    doc.add_paragraph("Nội dung", style="Heading 1")
    toc_items = [block[1][1] for block in blocks
                 if block[0] == "heading" and block[1][0] <= 2]
    for title in toc_items:
        p = doc.add_paragraph(style="List Bullet")
        p.paragraph_format.left_indent = Inches(.2)
        p.paragraph_format.space_after = Pt(1)
        p.add_run(title)
    doc.add_page_break()

    first_title_seen = False
    in_main_content = False
    for block in blocks:
        kind = block[0]
        if kind == "heading":
            level, title = block[1]
            if level == 1 and title.startswith("HỒ SƠ"):  # represented by the cover
                first_title_seen = True
                continue
            if level == 2 and title.startswith("Fraud Telecom"):
                in_main_content = True
            style_level = min(max(level, 1), 3)
            doc.add_paragraph(title, style=f"Heading {style_level}")
        elif kind == "paragraph":
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            p.add_run(block[1].replace("`", ""))
        elif kind == "quote":
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Inches(.25)
            p.paragraph_format.right_indent = Inches(.2)
            p.paragraph_format.space_before = Pt(4)
            p.paragraph_format.space_after = Pt(8)
            run = p.add_run(block[1].replace("**", "").replace("`", ""))
            run.italic = True
            run.font.color.rgb = RGBColor.from_string(TEAL)
        elif kind in ("bullet", "number"):
            _, indent, text = block
            style = "List Bullet" if kind == "bullet" else "List Number"
            p = doc.add_paragraph(style=style)
            p.paragraph_format.left_indent = Inches(.25 + min(indent / 24, .5))
            p.paragraph_format.space_after = Pt(2)
            p.add_run(text.replace("`", ""))
        elif kind == "code":
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Inches(.15)
            p.paragraph_format.right_indent = Inches(.15)
            p.paragraph_format.space_before, p.paragraph_format.space_after = Pt(4), Pt(8)
            pPr = p._p.get_or_add_pPr()
            shd = OxmlElement("w:shd")
            shd.set(qn("w:fill"), PALE)
            pPr.append(shd)
            run = p.add_run(block[1])
            run.font.name, run.font.size = "Consolas", Pt(7.3)
            run.font.color.rgb = RGBColor.from_string(NAVY)
        elif kind == "table":
            rows = block[1]
            if not rows:
                continue
            columns = max(map(len, rows))
            table = doc.add_table(rows=0, cols=columns)
            table.style = "Table Grid"
            table.autofit = True
            for ridx, values in enumerate(rows):
                cells = table.add_row().cells
                for cidx in range(columns):
                    value = values[cidx] if cidx < len(values) else ""
                    if ridx == 0:
                        set_cell_shading(cells[cidx], NAVY)
                        set_cell_text(cells[cidx], value, bold=True, color="FFFFFF", size=8)
                    else:
                        if ridx % 2 == 0:
                            set_cell_shading(cells[cidx], "F1F5F8")
                        set_cell_text(cells[cidx], value, size=7.8)
                if ridx == 0:
                    set_repeat_table_header(table.rows[0])
            doc.add_paragraph().paragraph_format.space_after = Pt(1)
    doc.core_properties.title = "Hồ sơ sản phẩm: Fraud Telecom, Spark/Kafka, Edu DSS"
    doc.core_properties.subject = "Architecture, operating model, evidence, risk and roadmap"
    doc.core_properties.author = "Product & Architecture"
    doc.save(DOCX_OUT)


def register_fonts():
    font_dir = Path("C:/Windows/Fonts")
    regular = font_dir / "arial.ttf"
    bold = font_dir / "arialbd.ttf"
    mono = font_dir / "consola.ttf"
    if not regular.exists() or not bold.exists():
        raise RuntimeError("Unicode TrueType fonts (Arial) are required for Vietnamese PDF output")
    pdfmetrics.registerFont(TTFont("ArialVN", str(regular)))
    pdfmetrics.registerFont(TTFont("ArialVN-Bold", str(bold)))
    if mono.exists():
        pdfmetrics.registerFont(TTFont("ConsolasVN", str(mono)))
    else:
        pdfmetrics.registerFont(TTFont("ConsolasVN", str(regular)))


def build_pdf(blocks):
    register_fonts()
    width, height = A4
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="BodyVN", fontName="ArialVN", fontSize=8.6, leading=12,
        textColor=colors.HexColor("#263444"), alignment=TA_JUSTIFY, spaceAfter=5))
    styles.add(ParagraphStyle(name="H1VN", fontName="ArialVN-Bold", fontSize=18, leading=22,
        textColor=colors.HexColor("#" + NAVY), spaceBefore=13, spaceAfter=7, keepWithNext=True))
    styles.add(ParagraphStyle(name="H2VN", fontName="ArialVN-Bold", fontSize=13, leading=16,
        textColor=colors.HexColor("#" + TEAL), spaceBefore=10, spaceAfter=5, keepWithNext=True))
    styles.add(ParagraphStyle(name="H3VN", fontName="ArialVN-Bold", fontSize=10.5, leading=13,
        textColor=colors.HexColor("#" + NAVY), spaceBefore=8, spaceAfter=4, keepWithNext=True))
    styles.add(ParagraphStyle(name="QuoteVN", fontName="ArialVN", fontSize=9, leading=13,
        textColor=colors.HexColor("#" + TEAL), leftIndent=10, rightIndent=10,
        borderColor=colors.HexColor("#" + TEAL), borderWidth=1, borderPadding=7, spaceAfter=9))
    styles.add(ParagraphStyle(name="SmallVN", fontName="ArialVN", fontSize=7.2, leading=9,
        textColor=colors.HexColor("#263444"), spaceAfter=1))
    styles.add(ParagraphStyle(name="TableHeadVN", fontName="ArialVN-Bold", fontSize=7.2, leading=9,
        textColor=colors.white))
    styles.add(ParagraphStyle(name="CodeVN", fontName="ConsolasVN", fontSize=6.8, leading=8.4,
        textColor=colors.HexColor("#" + NAVY), leftIndent=4, rightIndent=4, spaceAfter=1))

    story = []
    story.append(Spacer(1, 36 * mm))
    story.append(Paragraph("HỒ SƠ SẢN PHẨM", ParagraphStyle("CoverEyebrow", fontName="ArialVN-Bold",
        fontSize=12, leading=15, textColor=colors.HexColor("#" + TEAL), alignment=TA_CENTER)))
    story.append(Spacer(1, 8 * mm))
    story.append(Paragraph("Ba hệ thống,<br/>một tầm nhìn dữ liệu", ParagraphStyle("CoverTitle",
        fontName="ArialVN-Bold", fontSize=29, leading=35, textColor=colors.HexColor("#" + NAVY),
        alignment=TA_CENTER)))
    story.append(Spacer(1, 8 * mm))
    story.append(Paragraph("FRAUD TELECOM &nbsp; / &nbsp; SPARK–KAFKA &nbsp; / &nbsp; EDU DSS",
        ParagraphStyle("CoverSub", fontName="ArialVN-Bold", fontSize=12, leading=16,
        textColor=colors.HexColor("#" + TEAL), alignment=TA_CENTER)))
    story.append(Spacer(1, 8 * mm))
    story.append(Paragraph("Kiến trúc · Thuật toán · Kết quả · Người dùng · Rủi ro · Lộ trình",
        ParagraphStyle("CoverLine", fontName="ArialVN", fontSize=11, leading=15,
        textColor=colors.HexColor("#263444"), alignment=TA_CENTER)))
    story.append(Spacer(1, 5 * mm))
    story.append(Paragraph("Bản trình bày cho lãnh đạo, khách hàng viễn thông và hội đồng kỹ thuật<br/>28 tháng 10 năm 2026",
        ParagraphStyle("CoverDate", fontName="ArialVN", fontSize=9, leading=14,
        textColor=colors.HexColor("#667788"), alignment=TA_CENTER)))
    story.append(Spacer(1, 35 * mm))
    story.append(Paragraph("KẾT LUẬN TRƯỚC", ParagraphStyle("CoverCalloutHead", fontName="ArialVN-Bold",
        fontSize=10, textColor=colors.HexColor("#" + GOLD), alignment=TA_CENTER)))
    story.append(Spacer(1, 3 * mm))
    story.append(Paragraph("Nền tảng có bằng chứng chức năng, nhưng maturity khác nhau: Telecom chưa đạt p99 30 ms ổn định; Spark chưa chứng minh scale đa máy; Edu DSS đang chờ pipeline nghiệp vụ và IAM thật.",
        ParagraphStyle("CoverCallout", fontName="ArialVN-Bold", fontSize=12, leading=17,
        textColor=colors.HexColor("#" + NAVY), alignment=TA_CENTER, leftIndent=15, rightIndent=15)))
    story.append(PageBreak())

    story.append(Paragraph("NỘI DUNG", styles["H1VN"]))
    for block in blocks:
        if block[0] == "heading" and block[1][0] <= 2:
            story.append(Paragraph("• &nbsp;" + inline_markup(block[1][1]), styles["BodyVN"]))
    story.append(PageBreak())

    for block in blocks:
        kind = block[0]
        if kind == "heading":
            level, title = block[1]
            if level == 1 and title.startswith("HỒ SƠ"):
                continue
            style = styles["H1VN"] if level == 1 else styles["H2VN"] if level == 2 else styles["H3VN"]
            story.append(Paragraph(inline_markup(title), style))
        elif kind == "paragraph":
            story.append(Paragraph(inline_markup(block[1]), styles["BodyVN"]))
        elif kind == "quote":
            story.append(Paragraph(inline_markup(block[1]), styles["QuoteVN"]))
        elif kind in ("bullet", "number"):
            _, indent, text = block
            prefix = "•" if kind == "bullet" else "–"
            story.append(Paragraph("&nbsp;" * min(indent // 2, 8) + prefix + " &nbsp;" + inline_markup(text),
                                   styles["BodyVN"]))
        elif kind == "code":
            code = escape(block[1])
            story.append(Table([[Preformatted(code, styles["CodeVN"], maxLineLength=105, splitChars=" /─│┌┐└┘├┤┬┴┼─→←▼▾", newLineChars="\n")]],
                colWidths=[width - 39 * mm], style=TableStyle([
                    ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#" + PALE)),
                    ("BOX", (0, 0), (-1, -1), .5, colors.HexColor("#C7D5E0")),
                    ("LEFTPADDING", (0, 0), (-1, -1), 7), ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                    ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ])))
            story.append(Spacer(1, 4))
        elif kind == "table":
            rows = block[1]
            if not rows:
                continue
            col_count = max(map(len, rows))
            usable = width - 34 * mm
            col_width = usable / col_count
            data = []
            for ridx, row in enumerate(rows):
                row = row + [""] * (col_count - len(row))
                style = styles["TableHeadVN"] if ridx == 0 else styles["SmallVN"]
                data.append([Paragraph(inline_markup(cell), style) for cell in row])
            tbl = Table(data, colWidths=[col_width] * col_count, repeatRows=1, hAlign="LEFT")
            tbl.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#" + NAVY)),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F1F5F8")]),
                ("GRID", (0, 0), (-1, -1), .35, colors.HexColor("#CAD5DE")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]))
            story.extend([tbl, Spacer(1, 6)])

    def page_chrome(canvas, doc):
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor("#D6E0E8"))
        canvas.line(17 * mm, height - 14 * mm, width - 17 * mm, height - 14 * mm)
        canvas.setFont("ArialVN-Bold", 7)
        canvas.setFillColor(colors.HexColor("#" + TEAL))
        canvas.drawString(17 * mm, height - 11 * mm, "PRODUCT PORTFOLIO")
        canvas.setFont("ArialVN", 7)
        canvas.setFillColor(colors.HexColor("#667788"))
        canvas.drawRightString(width - 17 * mm, height - 11 * mm, "TELECOM · DATA · EDUCATION")
        canvas.line(17 * mm, 12 * mm, width - 17 * mm, 12 * mm)
        canvas.drawString(17 * mm, 8 * mm, "Hồ sơ kiến trúc · 28/10/2026")
        canvas.drawRightString(width - 17 * mm, 8 * mm, f"Trang {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(str(PDF_OUT), pagesize=A4, rightMargin=17 * mm, leftMargin=17 * mm,
                            topMargin=20 * mm, bottomMargin=18 * mm,
                            title="Hồ sơ sản phẩm: Fraud Telecom, Spark/Kafka, Edu DSS",
                            author="Product & Architecture")
    doc.build(story, onFirstPage=page_chrome, onLaterPages=page_chrome)


def main():
    blocks = parse_blocks(SOURCE.read_text(encoding="utf-8"))
    render_docx(blocks)
    build_pdf(blocks)
    print(f"DOCX: {DOCX_OUT} ({DOCX_OUT.stat().st_size:,} bytes)")
    print(f"PDF:  {PDF_OUT} ({PDF_OUT.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
