"""Template-faithful docx editing engine — milestone 3.

Operates on word/document.xml as a string (byte-level surgery), never through a
reserializing XML parser: the template's <w:document> start tag declares ~25
namespaces and the body carries 200+ rsid attributes that must round-trip
untouched. Every other zip part is copied byte-for-byte on save.

Hard-won template facts this engine encodes (see LEARNINGS.md, 2026-08-13):
- Formatting is direct, not style-driven. A paragraph's <w:pPr> (pStyle,
  numPr override, tabs, ind 357/357, jc) is preserved VERBATIM on rewrite;
  emitting a bare pStyle renders with the style's broken negative indent.
- Empty spacer paragraphs carry all vertical rhythm (the document has zero
  <w:spacing> elements). They must never be deleted.
- Real bullets are identified by <w:numPr> with a nonzero numId. Four decoy
  paragraphs carry pStyle=AchievementBullet with numId=0.
- Word fragments words across runs ($450|K|+ ...). All text search happens on
  flattened paragraph text, never per-<w:t>.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

# A whole paragraph. Safe: the template has no nested paragraph containers
# (no tables/textboxes/drawings). <w:p\b does not match <w:pPr ('p' and 'P'
# are both word chars, so there is no \b between them).
PARA_RE = re.compile(r"<w:p\b[^>]*/>|<w:p\b.*?</w:p>", re.S)
# <w:t> or <w:t xml:space="...">, but NOT <w:tab .../> or <w:tabs> — the space
# before attributes is mandatory in the pattern precisely to exclude those.
TEXT_RE = re.compile(r"<w:t(?: [^>]*)?>(.*?)</w:t>", re.S)
PPR_RE = re.compile(r"<w:pPr>.*?</w:pPr>", re.S)
NUMID_RE = re.compile(r'<w:numId w:val="(\d+)"/>')
# Attributes are matched order-independently: Word emits w:val on pStyle but
# nothing guarantees it stays the only attribute on the element.
PSTYLE_RE = re.compile(r'<w:pStyle[^>]*w:val="([^"]*)"')
# [label](url) in replacement text becomes a Hyperlink-styled <w:hyperlink> run
LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
# One run (<w:r> or <w:r ...>; never <w:rPr>), and a run property that turns bold on.
RUN_RE = re.compile(r"<w:r(?: [^>]*)?>.*?</w:r>", re.S)
BOLD_RE = re.compile(r'<w:b(?: w:val="(?:1|true|on)")?/>')
SECTION_HEADINGS = {"WORK EXPERIENCE", "EDUCATION", "PROJECTS", "SKILLS & INTERESTS"}
# Employers a page may drop whole. Fidelity and Citigroup are on every page;
# only their bullet counts move (Vaish, 2026-10-13).
OPT_IN_EMPLOYERS = ("PENZIGO",)

# The pass-1 boundary copy the resume-tailor skill banks before its keyword
# pass. It lives beside the shipped resume in the same application folder, so
# EVERY `*.docx` glob over `applications/` has to exclude it or it is treated as
# a second shipped resume. It is an audit artifact: the JD-blind, unconverged,
# never-critiqued draft. Defined here, once, because three modules glob those
# folders (dashboard links, sync's resume_docx, the renderer parity audit) and a
# filter that lives in one of a set of parallel readers is not a filter.
PASS1_SUFFIX = ".pass1.docx"


def shipped_resume_docx(folder):
    """The one shipped resume in an application folder, never the pass-1 copy.

    Lives here beside PASS1_SUFFIX because it is the same question the constant
    exists to answer, and because two audits now ask it: attestation_audit and
    parse_back. Deliberately NOT named resume_docx -- sync.resume_docx(rec)
    already owns that name for a different contract (a tailored sidecar dict,
    with a resolve-by-elimination fallback). Returns None when the folder holds
    zero or several candidates, so a caller never guesses which one shipped.

    Distinct from sync's on purpose: this one has no fallback. An audit that
    guessed which file shipped would report findings against a file nobody sent.
    """
    from pathlib import Path as _Path
    folder = _Path(folder)
    hits = [q for q in sorted(folder.glob("Vaishnav_Ajai_Resume_*.docx"))
            if not q.name.endswith(PASS1_SUFFIX)]
    return hits[0] if len(hits) == 1 else None

RELS_PART = "word/_rels/document.xml.rels"
HYPERLINK_TYPE = ("http://schemas.openxmlformats.org/officeDocument/2006/"
                  "relationships/hyperlink")

RENDER_PS1 = Path(__file__).resolve().parent / "render_pdf.ps1"

# soffice ships without adding itself to PATH on Windows, so the discovery chain
# needs the install location explicitly or the canonical renderer is unreachable
# on the one machine that renders shipped resumes.
SOFFICE_FALLBACKS = (
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
)


def _unescape(s: str) -> str:
    return s.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


@dataclass
class Para:
    index: int
    xml: str

    @property
    def text(self) -> str:
        """Flattened visible text — the only safe way to search a paragraph."""
        return _unescape("".join(TEXT_RE.findall(self.xml)))

    @property
    def is_spacer(self) -> bool:
        return self.text.strip() == ""

    @property
    def is_bullet(self) -> bool:
        m = NUMID_RE.search(self.xml)
        return bool("<w:numPr>" in self.xml and m and m.group(1) != "0")

    @property
    def style(self) -> str:
        """The paragraph's pStyle val, or "" when it carries none.

        Added 2026-08-25 for the positioning line. Every other paragraph in
        this template is located by TEXT, which the positioning line cannot be:
        its text is whatever clause pass 1 selected, so there is no stable
        needle. The style is the one durable handle on it.
        """
        m = PSTYLE_RE.search(self.xml)
        return m.group(1) if m else ""


class Doc:
    """One loaded .docx. Mutations rewrite self._xml; save() rezips."""

    def __init__(self, parts: dict[str, bytes], order: list[zipfile.ZipInfo]):
        self._parts = parts
        self._order = order
        self._xml = parts["word/document.xml"].decode("utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "Doc":
        with zipfile.ZipFile(path) as z:
            order = z.infolist()
            parts = {i.filename: z.read(i.filename) for i in order}
        return cls(parts, order)

    # -- reading ----------------------------------------------------------

    def paragraphs(self) -> list[Para]:
        return [Para(i, m.group(0)) for i, m in enumerate(PARA_RE.finditer(self._xml))]

    def find(self, needle: str) -> Para:
        """The unique paragraph whose flattened text contains needle."""
        hits = [p for p in self.paragraphs() if needle in p.text]
        if len(hits) != 1:
            raise ValueError(f"{needle!r} matched {len(hits)} paragraphs, need exactly 1")
        return hits[0]

    def find_style(self, style_id: str) -> Para:
        """The unique paragraph carrying this pStyle.

        The style-based sibling of find(). The positioning line is located this
        way because its TEXT is whatever clause pass 1 selected, so no needle is
        stable; the skill's own rule is to trust find() over indices, and this
        is what makes that possible for a paragraph with no fixed text.
        """
        hits = [p for p in self.paragraphs() if p.style == style_id]
        if len(hits) != 1:
            raise ValueError(
                f"style {style_id!r} matched {len(hits)} paragraphs, need exactly 1")
        return hits[0]

    def page_margins(self) -> dict[str, float]:
        """The section's page margins in POINTS, read from <w:pgMar>.

        Read-only, and deliberately so: the margins live in <w:sectPr>, which
        `test_docx_fidelity.py` asserts byte-identical after a round trip. This
        exists because `preflight.check_page_fill` needs the page's real bottom
        edge to measure how much of the one page a resume actually spent —
        LEARNINGS 2026-09-03: the top GAP it used to reference is the first
        line's ascent (26.9pt against an 11.9pt real margin), so it is ~1.3
        lines lenient and cannot see room a margin cut creates at all.

        Word stores margins in twips (1/1440 inch); a point is 1/72 inch, so
        the conversion is /20. Returns {} when the document carries no
        <w:pgMar> — the caller decides what an unknown margin means, rather
        than measuring against a silent default.
        """
        m = re.search(r"<w:pgMar\b[^>]*/>", self._xml)
        if not m:
            return {}
        attrs = dict(re.findall(r'w:(\w+)="(-?\d+)"', m.group(0)))
        return {side: round(int(attrs[side]) / 20.0, 2)
                for side in ("top", "right", "bottom", "left") if side in attrs}

    def hyperlink_targets(self) -> dict[str, str]:
        """rId -> URL for every hyperlink relationship in document.xml.rels."""
        rels = self._parts.get(RELS_PART, b"").decode("utf-8")
        out = {}
        for entry in re.findall(r"<Relationship\b[^>]*/>", rels):
            rid, target = re.search(r'Id="([^"]+)"', entry), re.search(r'Target="([^"]+)"', entry)
            if '/hyperlink"' in entry and rid and target:
                out[rid.group(1)] = _unescape(target.group(1))
        return out

    def bottom_margin_pt(self) -> float | None:
        """The bottom page margin in points, or None if the document has none."""
        return self.page_margins().get("bottom")

    def _span(self, index: int) -> tuple[int, int]:
        spans = [m.span() for m in PARA_RE.finditer(self._xml)]
        if not 0 <= index < len(spans):
            raise IndexError(f"paragraph index {index} out of range ({len(spans)})")
        return spans[index]

    # -- mutating ---------------------------------------------------------

    def _plain_runs(self, text: str) -> str:
        runs = []
        for i, seg in enumerate(text.split("**")):
            if seg == "":
                continue
            bold = i % 2 == 1  # odd segments are inside **...**
            rpr = "<w:rPr><w:b/><w:bCs/></w:rPr>" if bold else ""
            runs.append(f'<w:r>{rpr}<w:t xml:space="preserve">{escape(seg)}</w:t></w:r>')
        return "".join(runs)

    def _ensure_hyperlink_rel(self, url: str) -> str:
        """Return the rId for url, adding an External hyperlink rel if needed."""
        rels = self._parts[RELS_PART].decode("utf-8")
        for m in re.finditer(r"<Relationship\b[^>]*/>", rels):
            entry = m.group(0)
            if '/hyperlink"' in entry and f'Target="{escape(url)}"' in entry:
                return re.search(r'Id="(rId\d+)"', entry).group(1)
        n = 1 + max(int(m.group(1)) for m in re.finditer(r'Id="rId(\d+)"', rels))
        rid = f"rId{n}"
        entry = (f'<Relationship Id="{rid}" Type="{HYPERLINK_TYPE}" '
                 f'Target="{escape(url)}" TargetMode="External"/>')
        self._parts[RELS_PART] = rels.replace(
            "</Relationships>", entry + "</Relationships>").encode("utf-8")
        return rid

    @staticmethod
    def _keep_bold_label(block: str, text: str) -> str:
        """Carry a paragraph's bold lead-in (a label or project name) onto new text.

        The template bolds "Technical:", "Courses" and project names ahead of
        plain text. A rewrite that forgot the `**` markers used to drop that
        bold, so 26 of 35 pages on 10-07/08 shipped with plain labels on some
        lines and bold on others. Bullets open with a plain run and are left
        alone; text that already opens with `**` is the caller's choice.
        """
        if text.startswith("**"):
            return text
        first = next((r for r in RUN_RE.findall(block)
                      if "".join(TEXT_RE.findall(r)).strip()), None)
        if first is None or not BOLD_RE.search(first):
            return text
        old = _unescape("".join(TEXT_RE.findall(first))).rstrip()
        if old and text.startswith(old):
            label = old
        else:
            m = re.match(r"([^|:]{1,40}?)(?= \| )|([^|:]{1,30}:)", text)
            if not m:
                return text
            label = m.group(1) or m.group(2)
        return f"**{label}**{text[len(label):]}"

    def replace_paragraph_text(self, index: int, text: str) -> None:
        """Rebuild a paragraph's runs from `text`, preserving <w:pPr> verbatim.

        `**bold**` spans become <w:b/><w:bCs/> runs, matching how the template
        bolds metrics inside bullets (plain runs carry no rPr at all).
        `[label](url)` becomes a <w:hyperlink> run in the Hyperlink character
        style, with its relationship created in document.xml.rels as needed.
        """
        start, end = self._span(index)
        block = self._xml[start:end]
        open_tag = re.match(r"<w:p\b[^>]*>", block)
        ppr = PPR_RE.search(block)
        if not open_tag or not ppr:
            raise ValueError(f"paragraph {index} has no pPr; refusing a blind rewrite")
        if text.count("**") % 2:
            raise ValueError("unbalanced ** bold markers")
        text = self._keep_bold_label(block, text)

        runs, pos = [], 0
        for m in LINK_RE.finditer(text):
            runs.append(self._plain_runs(text[pos:m.start()]))
            rid = self._ensure_hyperlink_rel(m.group(2))
            runs.append(
                f'<w:hyperlink r:id="{rid}" w:history="1"><w:r><w:rPr>'
                f'<w:rStyle w:val="Hyperlink"/><w:szCs w:val="20"/></w:rPr>'
                f'<w:t xml:space="preserve">{escape(m.group(1))}</w:t></w:r></w:hyperlink>')
            pos = m.end()
        runs.append(self._plain_runs(text[pos:]))

        self._xml = (self._xml[:start] + open_tag.group(0) + ppr.group(0)
                     + "".join(runs) + "</w:p>" + self._xml[end:])

    def replace_run_text(self, index: int, old: str, new: str) -> None:
        """Swap text inside a single existing run, keeping its rPr (style) intact.

        For styled one-run lines (role titles, org names). `old` must sit whole
        inside one <w:t>; use replace_paragraph_text when it is fragmented.
        """
        start, end = self._span(index)
        block = self._xml[start:end]
        needle = escape(old)
        hits = [m for m in TEXT_RE.finditer(block) if needle in m.group(1)]
        if len(hits) != 1:
            raise ValueError(
                f"{old!r} found in {len(hits)} runs of paragraph {index}; "
                "need exactly 1 (run may be fragmented — flatten instead)")
        m = hits[0]
        new_block = (block[:m.start(1)]
                     + m.group(1).replace(needle, escape(new))
                     + block[m.end(1):])
        self._xml = self._xml[:start] + new_block + self._xml[end:]

    def resize_spacer(self, index: int, half_points: int) -> None:
        """Overflow ladder step (c'): trim a spacer paragraph's font size.

        Floor is 8 half-points (4pt) — the template's own smallest spacer —
        and only spacer paragraphs may be resized. Log every use in score.md.
        """
        if half_points < 8:
            raise ValueError("spacer floor is 8 half-points (4pt); never go below")
        start, end = self._span(index)
        block = self._xml[start:end]
        if not Para(index, block).is_spacer:
            raise ValueError(f"paragraph {index} is not a spacer; resize_spacer "
                             "only touches empty rhythm paragraphs")
        new_block, n = re.subn(r'(<w:sz(?:Cs)? w:val=")\d+("/>)',
                               lambda m: f"{m.group(1)}{half_points}{m.group(2)}",
                               block)
        if n == 0:
            raise ValueError(f"spacer {index} carries no explicit w:sz to resize")
        self._xml = self._xml[:start] + new_block + self._xml[end:]

    def move_paragraphs(self, start_idx: int, end_idx: int, before_idx: int) -> None:
        """Relocate the contiguous block [start_idx, end_idx] to sit before
        before_idx. Section reordering (e.g. Leadership above Education) moves
        whole <w:p> elements so every pPr, run style, tab stop, and hyperlink
        travels untouched — never reorder sections by swapping text between
        paragraphs (that would carry bullet numbering onto headings).

        before_idx is an index in the CURRENT paragraph list and must lie
        outside the moved block. The result is a pure permutation: the pPr
        multiset is unchanged by construction.
        """
        spans = [m.span() for m in PARA_RE.finditer(self._xml)]
        n = len(spans)
        if not (0 <= start_idx <= end_idx < n):
            raise IndexError(f"block [{start_idx}, {end_idx}] out of range ({n})")
        if start_idx <= before_idx <= end_idx:
            raise ValueError("before_idx lies inside the moved block")
        if not (0 <= before_idx <= n):
            raise IndexError(f"before_idx {before_idx} out of range ({n})")
        between = [self._xml[spans[i][1]:spans[i + 1][0]] for i in range(n - 1)]
        if any(s.strip() for s in between):
            raise ValueError("non-paragraph content between paragraphs; refusing to reorder")

        order = list(range(n))
        block = order[start_idx:end_idx + 1]
        rest = order[:start_idx] + order[end_idx + 1:]
        at = rest.index(before_idx) if before_idx < n and before_idx in rest else len(rest)
        new_order = rest[:at] + block + rest[at:]

        body_start, body_end = spans[0][0], spans[-1][1]
        joined = "".join(self._xml[spans[i][0]:spans[i][1]] for i in new_order)
        self._xml = self._xml[:body_start] + joined + self._xml[body_end:]

    def clone_paragraph(self, src_idx: int, before_idx: int) -> int:
        """Duplicate paragraph `src_idx` and insert the copy before `before_idx`.

        The ONLY sanctioned way to ADD a paragraph, and the counterpart to
        delete_paragraph. The clone carries the source's entire <w:pPr> by
        construction, so cloning a bullet yields a real bullet with the
        template's own numbering, indent, tab stops and justification. Never
        hand-build a <w:p>: the template's list numbering lives in numPr and a
        hand-built paragraph silently loses it.

        Returns the new paragraph's index, which is `before_idx` (inserting
        before k puts the clone at k). Set its text with replace_paragraph_text.
        Indices at and after before_idx shift by one, exactly as they do after
        delete_paragraph, so clone LAST or re-read paragraphs() afterwards.

        Word's w14:paraId / w14:textId are stripped from the copy. They are
        meant to be document-unique and Word regenerates them on open; leaving
        duplicates behind is the kind of dirt that survives a round trip.
        """
        spans = [m.span() for m in PARA_RE.finditer(self._xml)]
        n = len(spans)
        if not (0 <= src_idx < n):
            raise IndexError(f"src_idx {src_idx} out of range ({n})")
        if not (0 <= before_idx <= n):
            raise IndexError(f"before_idx {before_idx} out of range ({n})")
        block = self._xml[spans[src_idx][0]:spans[src_idx][1]]
        m = re.match(r"<w:p\b[^>]*?/?>", block)
        if not m:
            raise ValueError(f"paragraph {src_idx} has no parseable open tag")
        open_tag = re.sub(r'\s+w14:(?:paraId|textId)="[^"]*"', "", m.group(0))
        clone = open_tag + block[m.end():]
        at = spans[before_idx][0] if before_idx < n else spans[-1][1]
        self._xml = self._xml[:at] + clone + self._xml[at:]
        return before_idx

    def delete_paragraph(self, index: int) -> None:
        """Remove one whole paragraph. Refuses spacers — they carry the layout."""
        start, end = self._span(index)
        block = self._xml[start:end]
        if Para(index, block).is_spacer:
            raise ValueError(
                f"paragraph {index} is an empty spacer; spacers carry the "
                "template's vertical rhythm and must never be deleted")
        self._xml = self._xml[:start] + self._xml[end:]

    def delete_employer(self, header_needle: str) -> int:
        """Remove one employer block: the spacer above its header, the header,
        its role line(s) and bullets, up to (not including) the next spacer.

        Penzigo is opt-in since 2026-10-13 (Vaish): it stays only when the JD
        rewards it. delete_paragraph refuses spacers, so removing a block one
        paragraph at a time would leave a doubled gap; this takes exactly the
        spacer that separated the block from the one above, so the rhythm below
        (the end-of-section spacers before EDUCATION) is untouched. Refuses
        anything but a non-bullet header with a spacer above it, and refuses to
        cross a section heading. Returns how many paragraphs it removed.
        """
        if header_needle.upper() not in OPT_IN_EMPLOYERS:
            raise ValueError(f"{header_needle!r} is not opt-in ({OPT_IN_EMPLOYERS}); "
                             "Fidelity and Citigroup stay, only their bullets move")
        paras = self.paragraphs()
        hits = [p for p in paras if not p.is_bullet and p.text.strip().upper()
                .startswith(header_needle.upper())]
        if len(hits) != 1:
            raise ValueError(f"{header_needle!r} matches {len(hits)} header paragraphs")
        h = hits[0].index
        if h == 0 or not paras[h - 1].is_spacer:
            raise ValueError(f"no spacer above {header_needle!r}; not an employer block")
        end = h + 1
        while end < len(paras) and not paras[end].is_spacer:
            if paras[end].text.strip().upper() in SECTION_HEADINGS:
                raise ValueError(f"{header_needle!r} block runs into a section heading")
            end += 1
        for i in range(end - 1, h - 2, -1):   # bottom up, so indices hold
            start, stop = self._span(i)
            self._xml = self._xml[:start] + self._xml[stop:]
        return end - (h - 1)

    # -- writing ----------------------------------------------------------

    def save(self, path: str | Path) -> None:
        # Rule (LEARNINGS 2026-08-14): removing hyperlinked content must remove
        # its relationship — an orphaned rel is invisible in Word but dirty.
        rels = self._parts[RELS_PART].decode("utf-8")
        used = set(re.findall(r'r:id="(rId\d+)"', self._xml))

        def keep(m: re.Match) -> str:
            entry = m.group(0)
            if '/hyperlink"' not in entry:
                return entry
            rid = re.search(r'Id="(rId\d+)"', entry).group(1)
            return entry if rid in used else ""

        pruned = re.sub(r"<Relationship\b[^>]*/>", keep, rels)
        if pruned != rels:
            self._parts[RELS_PART] = pruned.encode("utf-8")

        self._parts["word/document.xml"] = self._xml.encode("utf-8")
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            for info in self._order:
                z.writestr(info, self._parts[info.filename])


def default_renderer() -> str:
    """Which renderer produces the SHIPPED pdf: 'soffice' everywhere.

    Word is no longer a default anywhere. Its PDF writer sprays spaces inside
    words on justified text ("real -time", "cross -functional"), which survives
    to any gap-inference extractor — the family most ATS parsers belong to —
    and lands on exactly the compound keywords a JD match scores. Measured
    2026-08-19 on 9 of 10 shipped resumes; soffice's text layer is clean in both
    pypdf and poppler for the same docx. Word stays the page-count cross-check
    (see render_pdf), never the writer of a file anyone submits.

    RESUME_RENDERER overrides, which is how a caller asks for Word explicitly.
    """
    choice = os.environ.get("RESUME_RENDERER", "").strip().lower()
    if choice:
        if choice not in ("word", "soffice"):
            raise ValueError(f"RESUME_RENDERER must be 'word' or 'soffice', got {choice!r}")
        return choice
    return "soffice"


def _render_word(docx_path: Path, pdf_path: Path) -> tuple[Path, int]:
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", str(RENDER_PS1), str(docx_path), str(pdf_path)],
        capture_output=True, text=True, timeout=300)
    out = (proc.stdout or "") + (proc.stderr or "")
    m = re.search(r"PAGES=(\d+)", out)
    if proc.returncode != 0 or not m or not pdf_path.exists():
        raise RuntimeError(f"render_pdf/word failed (rc={proc.returncode}): {out.strip()[:500]}")
    return pdf_path, int(m.group(1))


def _render_soffice(docx_path: Path, pdf_path: Path) -> tuple[Path, int]:
    """LibreOffice headless. Page count comes from the PDF, not the converter.

    soffice names its output <stem>.pdf in --outdir and offers no way to name it,
    so convert into a temp dir and move. It also silently exits 0 having written
    nothing when a stale user profile is locked, hence the explicit existence
    check and the private -env:UserInstallation profile (a Routine runs several
    renders in one session; a shared profile serializes and occasionally wedges).
    """
    soffice = (os.environ.get("SOFFICE_BIN") or shutil.which("soffice")
               or shutil.which("libreoffice")
               or next((p for p in SOFFICE_FALLBACKS if Path(p).exists()), None))
    if not soffice:
        raise RuntimeError(
            "render_pdf/soffice: no soffice binary found (set SOFFICE_BIN, or "
            "apt install libreoffice-writer, or install LibreOffice on Windows)")
    with tempfile.TemporaryDirectory(prefix="render_pdf_") as tmp:
        tmpdir = Path(tmp)
        profile = (tmpdir / "profile").as_uri()
        proc = subprocess.run(
            [soffice, f"-env:UserInstallation={profile}", "--headless", "--norestore",
             "--convert-to", "pdf:writer_pdf_Export", "--outdir", str(tmpdir), str(docx_path)],
            capture_output=True, text=True, timeout=300)
        produced = tmpdir / (docx_path.stem + ".pdf")
        if proc.returncode != 0 or not produced.exists():
            out = ((proc.stdout or "") + (proc.stderr or "")).strip()[:500]
            raise RuntimeError(f"render_pdf/soffice failed (rc={proc.returncode}): {out}")
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(produced), str(pdf_path))
    return pdf_path, count_pdf_pages(pdf_path)


def count_pdf_pages(pdf_path: str | Path) -> int:
    """Page count of a rendered PDF. Word reports its own; soffice needs this."""
    from pypdf import PdfReader  # local-only dep; keep Layer 1 requirements lean
    return len(PdfReader(str(pdf_path)).pages)


def render_pdf(docx_path: str | Path, pdf_path: str | Path | None = None,
               renderer: str | None = None) -> tuple[Path, int]:
    """Render a docx to PDF and return (pdf_path, rendered page count).

    Two renderers with split, non-interchangeable roles (revised 2026-08-19):
    - 'soffice' (LibreOffice headless) WRITES every shipped PDF, on every
      platform. Its text layer extracts cleanly, and the text layer is what an
      ATS reads; Word's sprays spaces inside compound keywords.
    - 'word' (Windows, Word COM) is the PAGE-COUNT cross-check only. Word's
      pagination is still what a recruiter sees, so a disagreement with soffice
      is flagged on the dashboard (word_pages=N; never a status change). It renders to scratch and
      must never overwrite a canonical PDF.
    """
    docx_path = Path(docx_path).resolve()
    pdf_path = Path(pdf_path) if pdf_path else docx_path.with_suffix(".pdf")
    which = (renderer or default_renderer()).lower()
    if which == "word":
        # Hard stop, not a docstring. Word renders to scratch for the page
        # count; a Word PDF under applications/ is a submittable artifact with
        # a corrupt text layer. The default pdf_path IS the canonical sibling,
        # so `render_pdf(docx, renderer="word")` with no target would ship one.
        if "applications" in pdf_path.resolve().parts:
            raise RuntimeError(
                f"refusing to write a Word render into applications/: "
                f"{pdf_path}. Word's PDF text layer sprays spaces inside "
                "compound keywords and an ATS parses that layer. Render to a "
                "scratch path for the page count, or use renderer='soffice' "
                "for the shipped file.")
        return _render_word(docx_path, pdf_path)
    if which == "soffice":
        return _render_soffice(docx_path, pdf_path)
    raise ValueError(f"unknown renderer {which!r} (expected 'word' or 'soffice')")
