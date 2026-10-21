"""Docs から書き出した Markdown を Word 文書にする（図は手元の画像に差し戻す）。

python scripts/md_to_docx.py <入力.md> <出力.docx> <画像1> <画像2> ...
画像は本文中の図の置き換え（[image: ...]）の順に対応させる。
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Mm, Pt, RGBColor

FONT_JA = "Hiragino Sans"
FONT_EN = "Arial"
INK2 = RGBColor(0x52, 0x51, 0x4E)


def set_font(run, size: float | None = None, bold: bool | None = None, mono: bool = False, color: RGBColor | None = None) -> None:
    run.font.name = "Menlo" if mono else FONT_EN
    rpr = run._element.get_or_add_rPr()
    fonts = rpr.find(qn("w:rFonts"))
    if fonts is None:
        fonts = OxmlElement("w:rFonts")
        rpr.append(fonts)
    fonts.set(qn("w:eastAsia"), FONT_JA)
    if size:
        run.font.size = Pt(size)
    if bold is not None:
        run.font.bold = bold
    if color is not None:
        run.font.color.rgb = color


def clean(s: str) -> str:
    s = s.replace("\\_", "_").replace("\\]", "]").replace("&#91;", "[")
    return re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1（\2）", s)


def add_inline(par, text: str, size: float = 10.5) -> None:
    """**太字** を含む 1 行を段落に足す。"""
    for k, part in enumerate(re.split(r"\*\*", clean(text))):
        if part:
            set_font(par.add_run(part), size=size, bold=(k % 2 == 1))


def shade(cell, hex_color: str) -> None:
    tcpr = cell._element.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_color)
    tcpr.append(shd)


def borders(table) -> None:
    """罫線を明示する（表示ソフトによって既定の罫線が出ないため）。"""
    tblpr = table._element.tblPr
    b = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        e = OxmlElement(f"w:{edge}")
        e.set(qn("w:val"), "single")
        e.set(qn("w:sz"), "4")
        e.set(qn("w:color"), "BFBFBF")
        b.append(e)
    tblpr.append(b)


def add_table(doc, rows: list[list[str]]) -> None:
    ncol = len(rows[0])
    t = doc.add_table(rows=len(rows), cols=ncol)
    t.style = "Table Grid"
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    t.autofit = False
    borders(t)
    # 本文幅 170mm を、文字数の多い列ほど広く配る（最小 18mm）
    lens = [max(len(clean(r[c])) if c < len(r) else 0 for r in rows) for c in range(ncol)]
    raw = [max(18.0, min(80.0, 3.2 * L)) for L in lens]
    widths = [w * 170.0 / sum(raw) for w in raw]
    for r, row in enumerate(rows):
        for c in range(ncol):
            v = row[c] if c < len(row) else ""
            cell = t.cell(r, c)
            cell.width = Mm(widths[c])
            cell.text = ""
            p = cell.paragraphs[0]
            p.paragraph_format.space_after = Pt(0)
            p.paragraph_format.space_before = Pt(0)
            add_inline(p, v, size=8.5)
            if r == 0:
                shade(cell, "E9EEF5")
                for run in p.runs:
                    run.font.bold = True
    doc.add_paragraph()


def main() -> None:
    src, out, images = Path(sys.argv[1]), Path(sys.argv[2]), [Path(p) for p in sys.argv[3:]]
    lines = src.read_text(encoding="utf-8").splitlines()
    doc = Document()
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Mm(210), Mm(297)
    sec.left_margin = sec.right_margin = Mm(20)
    sec.top_margin = sec.bottom_margin = Mm(18)
    normal = doc.styles["Normal"]
    normal.font.name = FONT_EN
    normal.font.size = Pt(10.5)
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), FONT_JA)
    for lvl, size in ((1, 15), (2, 12.5), (3, 11)):
        st = doc.styles[f"Heading {lvl}"]
        st.font.name = FONT_EN
        st.font.size = Pt(size)
        st.font.color.rgb = RGBColor(0x0B, 0x0B, 0x0B)
        st.element.rPr.rFonts.set(qn("w:eastAsia"), FONT_JA)
    img_i = 0
    i = 0
    while i < len(lines):
        s = lines[i]
        if s.startswith("# "):
            p = doc.add_paragraph()
            set_font(p.add_run(clean(s[2:])), size=18, bold=True)
        elif re.match(r"^[A-Z][a-z]{2} \d{1,2}, \d{4} ·", s):
            p = doc.add_paragraph()
            # 日付と署名の行。環境変数 MD_TO_DOCX_BYLINE があればそれに置き換える（既定は Markdown の行のまま）
            set_font(p.add_run(os.environ.get("MD_TO_DOCX_BYLINE") or clean(s)), size=9.5, color=INK2)
        elif s.startswith("### "):
            doc.add_heading(clean(s[4:]), level=3)
        elif s.startswith("## "):
            doc.add_heading(clean(s[3:]), level=2)
        elif s.startswith("```"):
            i += 1
            code = []
            while i < len(lines) and not lines[i].startswith("```"):
                code.append(lines[i])
                i += 1
            for line in code:
                p = doc.add_paragraph()
                p.paragraph_format.space_after = Pt(0)
                set_font(p.add_run(line), size=7.5, mono=True)
            doc.add_paragraph()
        elif s.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split(" | ")]
                if not all(set(c) <= set("-: ") for c in cells):
                    rows.append(cells)
                i += 1
            add_table(doc, rows)
            continue
        elif s.startswith("&#91;image:") or s.startswith("[image:"):
            cap = clean(s).removeprefix("[image:").rstrip("]").strip()
            if img_i < len(images):
                doc.add_picture(str(images[img_i]), width=Mm(170))
                doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
                img_i += 1
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            set_font(p.add_run(f"図 {img_i}　{cap}"), size=8.5, color=INK2)
        elif re.match(r"^- \[ \] ", s):
            p = doc.add_paragraph(style="List Bullet")
            add_inline(p, "☐ " + s[6:])
        elif s.startswith("- "):
            p = doc.add_paragraph(style="List Bullet")
            add_inline(p, s[2:])
        elif re.match(r"^\d+\. ", s):
            p = doc.add_paragraph(style="List Number")
            add_inline(p, re.sub(r"^\d+\. ", "", s))
        elif s.strip():
            add_inline(doc.add_paragraph(), s)
        i += 1
    doc.save(out)
    print(f"{out}（図 {img_i} 点）")


if __name__ == "__main__":
    main()
