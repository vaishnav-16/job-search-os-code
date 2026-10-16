"""Retrofit a shipped resume docx off the Symbol bullet glyph, and re-render it.

Why this exists (2026-09-04). Every resume built before today inherited the
template's `numbering.xml`, which named the bullet glyph's font as **Symbol**.
A line's height is the max over its runs and the numbering glyph is a run on the
bullet's FIRST line, so:

    Times New Roman   hhea 1825/-443/87  -> 1.1499 em = 11.50pt at 10pt
    Symbol MT         hhea 2059/-450/0   -> 1.2251 em = 12.25pt at 10pt
    OpenSymbol                           -> 1.0591 em = 10.59pt at 10pt

On a seat WITH Symbol MT (Word, LibreOffice on Windows) every bullet's first
line set 0.75pt taller; on the Linux cloud, LibreOffice substituted OpenSymbol,
which is smaller than Times and therefore set nothing. Over ~15 bullets that is
one body line, which is why 40 shipped resumes counted one page in the cloud and
two in Word. The template is fixed; this carries the same fix to docx already
written from the old one.

It is deliberately a SEPARATE tool and not part of `sync.py`: sync is
deterministic and unattended, and rewriting a shipped artifact is neither. Run
it from a local session, read the table it prints, and commit.

The rewrite is pure XML on the zip parts -- never a Word round-trip, which
churns the package `test_docx_fidelity` pins.

    python pipeline/retrofit_bullet_font.py applications/*2026-09-04   # dry run
    python pipeline/retrofit_bullet_font.py --apply applications/foo   # write

A docx that needs no change is left alone, so this is safe to re-run.
"""
from __future__ import annotations

import argparse
import re
import shutil
import tempfile
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import docx_engine  # noqa: E402

# Only these parts can name a font that affects layout.
PARTS = ("word/numbering.xml", "word/styles.xml", "word/document.xml")

# glyph -> replacement, all three carried by Times New Roman AND Liberation
# Serif (cmap-verified 2026-09-04), so no seat has to substitute anything.
GLYPHS = {
    "": "•",   # Symbol   round bullet  -> BULLET
    "o": "◦",        # Courier  hollow bullet -> WHITE BULLET
    "": "▪",   # Wingdings square       -> BLACK SMALL SQUARE
}
SUBSTITUTABLE = ("Symbol", "Courier New", "Wingdings")


def rewrite_xml(part: str, xml: str) -> str:
    """Return `xml` with every substitutable font and glyph replaced."""
    if part == "word/numbering.xml":
        for old, new in GLYPHS.items():
            xml = xml.replace(f'<w:lvlText w:val="{old}"/>',
                              f'<w:lvlText w:val="{new}"/>')
    names = "|".join(re.escape(f) for f in SUBSTITUTABLE)
    return re.sub(rf'(w:ascii|w:hAnsi|w:eastAsia|w:cs)="(?:{names})"',
                  r'\1="Times New Roman"', xml)


def retrofit(src: Path, dst: Path) -> int:
    """Rewrite `src` into `dst`. Returns how many parts actually changed."""
    changed = 0
    zin = zipfile.ZipFile(src)
    try:
        with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename in PARTS:
                    xml = data.decode("utf-8")
                    out = rewrite_xml(item.filename, xml)
                    if out != xml:
                        changed += 1
                        data = out.encode("utf-8")
                zout.writestr(item, data)
    finally:
        zin.close()
    return changed


def resume_docx(folder: Path) -> Path | None:
    cand = [p for p in folder.glob("*.docx")
            if not p.name.startswith("~$") and "pass1" not in p.name]
    return cand[0] if len(cand) == 1 else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("folders", nargs="+")
    ap.add_argument("--apply", action="store_true",
                    help="write the docx and re-render its PDF (default: dry run)")
    args = ap.parse_args(argv)

    print(f"{'folder':<52} {'parts':<6} {'soffice pages':<14} word")
    rc = 0
    for raw in args.folders:
        folder = Path(raw)
        docx = resume_docx(folder)
        if docx is None:
            print(f"{folder.name[:51]:<52} no unambiguous .docx")
            rc = 1
            continue
        tmp = docx.with_suffix(".retrofit-tmp")
        n = retrofit(docx, tmp)
        if not n:
            tmp.unlink(missing_ok=True)
            print(f"{folder.name[:51]:<52} {'0':<6} already single-family")
            continue
        if not args.apply:
            tmp.unlink(missing_ok=True)
            print(f"{folder.name[:51]:<52} {n:<6} (dry run)")
            continue
        shutil.move(str(tmp), str(docx))
        # The shipped PDF is what Vaish submits, so it is re-rendered with the
        # renderer doctrine says writes every shipped PDF.
        _, pages = docx_engine.render_pdf(docx, docx.with_suffix(".pdf"),
                                          renderer="soffice")
        # Word owns pagination truth; render it to scratch only, never over the
        # artifact (docx_engine refuses to write Word output under applications/).
        scratch = Path(tempfile.gettempdir()) / "retrofit_check"
        scratch.mkdir(parents=True, exist_ok=True)   # Word will not create it
        try:
            _, wpages = docx_engine.render_pdf(docx, scratch / (docx.stem + ".pdf"),
                                               renderer="word")
        except Exception as exc:                       # no Word on this seat
            wpages = f"n/a ({str(exc)[:40]})"
        print(f"{folder.name[:51]:<52} {n:<6} {pages:<14} {wpages}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
