"""Render profile/master-resume/*.md to .docx and .pdf for review, via LibreOffice.

Usage: python pipeline/render_master_md.py [path/to/resume.md]

Plain conversion (headings, bullets, bold) so Vaish can read the master PM resume as a
document. The markdown stays canonical; the tailor reads the .md, never these renders.
"""
from __future__ import annotations

import html
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT = ROOT / "profile" / "master-resume" / "Vaishnav_Ajai_Master_PM_Resume.md"


def inline(text: str) -> str:
    text = html.escape(text, quote=False)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"`(.+?)`", r"<code>\1</code>", text)
    return text


def to_html(md: str) -> str:
    out = ["<html><head><meta charset='utf-8'><style>"
           "body{font-family:'Times New Roman',serif;font-size:10pt;margin:0.6in}"
           "h1{font-size:16pt;margin:0 0 4pt} h2{font-size:12pt;margin:12pt 0 4pt;"
           "border-bottom:1px solid #000} h3{font-size:10.5pt;margin:8pt 0 2pt}"
           "h4{font-size:10pt;margin:6pt 0 2pt;font-style:italic} p{margin:2pt 0}"
           "ul{margin:2pt 0 4pt 16pt;padding:0} li{margin:0 0 3pt}"
           "blockquote{color:#444;font-size:9pt;margin:4pt 0;padding-left:8pt;"
           "border-left:2px solid #999}</style></head><body>"]
    para: list[str] = []
    in_list = False
    in_quote = False

    def flush_para():
        nonlocal para
        if para:
            out.append("<p>" + inline(" ".join(para)) + "</p>")
            para = []

    def close_list():
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    def close_quote():
        nonlocal in_quote
        if in_quote:
            out.append("</blockquote>")
            in_quote = False

    lines = md.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.match(r"^(#{1,4})\s+(.*)$", line)
        if m:
            flush_para(); close_list(); close_quote()
            lvl = len(m.group(1))
            out.append(f"<h{lvl}>{inline(m.group(2))}</h{lvl}>")
        elif line.startswith("> "):
            flush_para(); close_list()
            if not in_quote:
                out.append("<blockquote>"); in_quote = True
            out.append(inline(line[2:]) + " ")
        elif re.match(r"^- ", line):
            flush_para(); close_quote()
            if not in_list:
                out.append("<ul>"); in_list = True
            item = [line[2:]]
            while i + 1 < len(lines) and re.match(r"^\s{2,}\S", lines[i + 1]):
                i += 1
                item.append(lines[i].strip())
            out.append("<li>" + inline(" ".join(item)) + "</li>")
        elif not line.strip():
            flush_para(); close_list(); close_quote()
        else:
            close_list(); close_quote()
            para.append(line.strip())
        i += 1
    flush_para(); close_list(); close_quote()
    out.append("</body></html>")
    return "\n".join(out)


def main() -> int:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT
    md = src.read_text(encoding="utf-8")
    html_path = src.with_suffix(".html")
    html_path.write_text(to_html(md), encoding="utf-8")
    soffice = shutil.which("soffice") or shutil.which("soffice.exe")
    if not soffice:
        for cand in (r"C:\Program Files\LibreOffice\program\soffice.exe",
                     r"C:\Program Files (x86)\LibreOffice\program\soffice.exe"):
            if Path(cand).exists():
                soffice = cand
                break
    if not soffice:
        print("soffice not found; wrote", html_path)
        return 1
    for fmt in ("docx:MS Word 2007 XML", "pdf:writer_web_pdf_Export"):
        subprocess.run([soffice, "--headless", "--convert-to", fmt, "--outdir",
                        str(src.parent), str(html_path)], check=True,
                       capture_output=True, text=True)
    html_path.unlink()
    print("rendered", src.with_suffix(".docx").name, "and", src.with_suffix(".pdf").name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
