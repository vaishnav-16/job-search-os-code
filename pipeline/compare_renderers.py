"""Word vs LibreOffice render fidelity — the gate on cloud tailoring.

The cloud Routine can only run LibreOffice, but Word's pagination is what a
recruiter sees. Before trusting a cloud-tailored resume we have to know whether
the two engines agree, and where they don't.

What is compared, in order of how much it matters:
  1. Page count. A disagreement here is disqualifying on its own: the whole
     one-page gate is a page count, and the overflow ladder is driven by it.
  2. Line breaks. Both engines get the same text; if they wrap it differently
     then widow detection and the ATS hyphen check (preflight.py) read a
     different document than the one Vaish sends. Compared as the sequence of
     rendered lines, so a single shifted wrap shows up as one diff, not many.
  3. Text content. Guards against a silently dropped run (small-caps headers and
     the tab-stop header line are the usual suspects).

Usage:
    python pipeline/compare_renderers.py                 # template + shipped resumes
    python pipeline/compare_renderers.py a.docx b.docx    # specific files
Exit 0 = renderers agree on every file (cloud tailoring is safe).
Exit 1 = at least one disagreement (fall back to cloud-drafts/local-render).
"""

from __future__ import annotations

import difflib
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import docx_engine  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def default_targets() -> list[Path]:
    targets = []
    template = ROOT / "profile" / "template" / "Vaishnav_Ajai_Resume_Template_Format.docx"
    if template.exists():
        targets.append(template)
    # Every shipped resume: these are the real artifacts, and they exercise the
    # engine's edits (moved sections, resized spacers) that the template does not.
    # `resume.pass1.docx` is excluded: it is the JD-blind pass-1 draft, not a
    # shipped artifact. Including it would silently double this audit's corpus
    # with unconverged drafts, and the parity claim on record is "page counts
    # agree on all 10 SHIPPED files" -- a baseline that stops meaning anything
    # the moment the denominator quietly changes.
    for docx in sorted((ROOT / "applications").glob("*/*.docx")):
        if (not docx.name.startswith("~$")
                and not docx.name.endswith(docx_engine.PASS1_SUFFIX)):
            targets.append(docx)
    return targets


# Two extraction artifacts, neither of which is a rendering difference:
#  - Word puts the list bullet glyph in the text layer; LibreOffice does not.
#    Both draw a visible bullet, so comparing on it only produces noise.
#  - Word's extraction sprays spurious spaces into justified text ("point -of-
#    contact", "3 -4 weeks", "Regulator y"). LibreOffice's is clean. Collapsing
#    all whitespace is what makes the two comparable at all.
BULLET_CHARS = "•·●▪"


def normalize(line: str) -> str:
    return " ".join(line.lstrip(BULLET_CHARS + " \t").split())


def lines_of(pdf_path: Path) -> list[str]:
    from pypdf import PdfReader
    out: list[str] = []
    for page in PdfReader(str(pdf_path)).pages:
        for raw in (page.extract_text() or "").splitlines():
            line = normalize(raw)
            if line:
                out.append(line)
    return out


SPRAY_RE = re.compile(r"(\w+) +(-\w+)|(\w+-) +(\w+)")


def spray_terms(lines: list[str]) -> set[str]:
    """Compound terms a renderer split with an injected space ("real -time").

    Returned de-sprayed ("real-time") so the two renderers' sets are comparable.
    """
    found = set()
    for ln in lines:
        for m in SPRAY_RE.finditer(ln):
            found.add("".join(g for g in m.groups() if g))
    return found


def compare(docx: Path, outdir: Path) -> dict:
    result: dict = {"docx": docx, "ok": True, "problems": []}
    rendered = {}
    for engine in ("word", "soffice"):
        pdf = outdir / f"{docx.stem}.{engine}.pdf"
        try:
            _, pages = docx_engine.render_pdf(docx, pdf, renderer=engine)
        except Exception as exc:  # a renderer that cannot run IS the finding
            result["ok"] = False
            result["problems"].append(f"{engine}: render failed: {exc}")
            return result
        rendered[engine] = {"pages": pages, "lines": lines_of(pdf), "pdf": pdf}

    w, s = rendered["word"], rendered["soffice"]
    result.update(word_pages=w["pages"], soffice_pages=s["pages"],
                  word_lines=len(w["lines"]), soffice_lines=len(s["lines"]))

    if w["pages"] != s["pages"]:
        result["ok"] = False
        result["problems"].append(
            f"PAGE COUNT differs: word={w['pages']} soffice={s['pages']}")

    # Word's PDF writer injects spaces inside compound words on justified text
    # ("real -time"); soffice does not. Count that separately BEFORE the squash
    # below, which deletes all spaces and so would file the defect under
    # "different wrapping" — the reason it went unnamed here until 2026-08-19.
    sprays = {"word": spray_terms(w["lines"]), "soffice": spray_terms(s["lines"])}
    result["sprays"] = sprays
    only_word = sprays["word"] - sprays["soffice"]
    if only_word:
        result["problems"].append(
            f"WORD SPLITS {len(only_word)} compound term(s) with injected spaces "
            f"that soffice keeps whole: {', '.join(sorted(only_word)[:6])}"
            f"{' ...' if len(only_word) > 6 else ''} — an ATS parses this text "
            "layer, which is why soffice writes the shipped PDF")

    if w["lines"] != s["lines"]:
        # Distinguish "wrapped differently" from "different words". Same words in
        # the same order with different line boundaries is a wrap difference;
        # anything else means a run went missing.
        squash = lambda ls: "".join(ls).replace(" ", "")  # noqa: E731
        if squash(w["lines"]) == squash(s["lines"]):
            kind = "LINE BREAKS differ (same text, different wrapping)"
        else:
            kind = "TEXT differs (a run rendered differently or went missing)"
            result["ok"] = False
        diff = list(difflib.unified_diff(
            w["lines"], s["lines"], "word", "soffice", lineterm="", n=0))
        result["problems"].append(f"{kind}: {len(diff)} diff lines")
        result["diff"] = diff

    return result


def main(argv: list[str]) -> int:
    targets = [Path(a).resolve() for a in argv] if argv else default_targets()
    if not targets:
        print("no docx targets found", file=sys.stderr)
        return 1

    print(f"comparing {len(targets)} file(s): word (ground truth) vs soffice (cloud)\n")
    results = []
    with tempfile.TemporaryDirectory(prefix="renderer_cmp_") as tmp:
        outdir = Path(tmp)
        for docx in targets:
            r = compare(docx, outdir)
            results.append(r)
            flag = "OK  " if r["ok"] else "FAIL"
            pages = (f"word={r.get('word_pages')}p/{r.get('word_lines')}L  "
                     f"soffice={r.get('soffice_pages')}p/{r.get('soffice_lines')}L")
            print(f"[{flag}] {docx.name}")
            print(f"        {pages}")
            for p in r["problems"]:
                print(f"        - {p}")
            if r.get("diff"):
                for line in r["diff"][:40]:
                    print(f"          {line}")
                if len(r["diff"]) > 40:
                    print(f"          ... {len(r['diff']) - 40} more diff lines")
            print()

    failed = [r for r in results if not r["ok"]]
    wrapped = [r for r in results
               if r["ok"] and any("LINE BREAKS" in p for p in r["problems"])]
    print("=" * 68)
    if failed:
        print(f"VERDICT: FAIL — {len(failed)}/{len(results)} file(s) disagree materially.")
        print("Cloud tailoring cannot own the one-page gate. Fall back to:")
        print("  Routine drafts the docx -> local sync renders with Word -> converge locally.")
        return 1
    if wrapped:
        print(f"VERDICT: PASS WITH CAVEAT — page counts agree on all {len(results)} file(s),")
        print(f"but {len(wrapped)} wrap line breaks differently. Cloud tailoring is safe for")
        print("content decisions; keep the local Word re-render as the final page check and")
        print("expect widow/hyphen findings to differ slightly between the two.")
        return 0
    print(f"VERDICT: PASS — both engines agree on all {len(results)} file(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
