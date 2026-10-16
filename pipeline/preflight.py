"""Deterministic pre-critic checks over the rendered resume.

    python pipeline/preflight.py <resume.docx> <resume.pdf> [--jd jd.md]

Mechanical docx/PDF quality only. Two severities: ERROR (exit 1 - fix before
the reviewers see the PDF) and WARN (informational). Whether a bullet is TRUE
or WELL-CHOSEN is a judgment call and stays with the reviewers; everything here
is something a regex or the PDF geometry can settle on its own.

The checks, one line each:
- check_bold - bold spans in a bullet: never lead with a verb, never a whole
  clause; no count cap since 2026-10-08 (the docx XML; the hook cannot see bold).
- check_text_guardrails - the lint hook's ban list, imported so the hook stays
  the single source of truth, run over the docx's flattened text.
- check_plain_english - WARN-only: "intake-to-approval" shorthand compounds.
- check_dates - WARN-only: month-year and year-only ranges mixed on one page.
- check_rhythm - WARN-only AI-smell: uniform closing clause shapes, repeated
  lead verbs, over-long bullets, stacked ", <verb>ing" chains.
- check_page_count - the one hard requirement: the PDF is exactly one page.
- check_text_layer - WARN-only: docx words that never reach the PDF text layer.
- check_positioning - at most one Positioning paragraph (absent is fine and
  deliberate), at most POS_MAX_WORDS words, at most two rendered lines. Pages
  outside applications/ only (the general and network resumes).
- check_tailored_page - pages under applications/: no positioning line, the
  template's section order, bold labels and project names, functional titles,
  no retired claim from record.json (Vaish, 2026-10-08), no certification under
  Education, and the CSPO linked to its badge (Vaish, 2026-10-15).
- check_font_families - the docx may NAME and the PDF may EMBED only Times New
  Roman / Liberation Serif; a substituted font silently sets line heights.
- check_hyphen_ats - every hyphenated term must survive PDF text extraction
  intact, or the ATS keyword is dead.
- check_widows - WARN-only: a bullet whose last rendered line holds 1-3 words.
- check_page_fill - a one-page render ending short of the docx's own bottom
  margin throws away the only resource the page has.
- check_parse_back - employer / title / dates re-derived from the PDF and
  diffed against the record (grading lives in parse_back).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from docx_engine import Doc  # noqa: E402

HOOK_PATH = HERE.parent / ".claude" / "hooks" / "lint_generated.py"

# Lead verbs the resume uses (master-doc sanctioned set + common variants).
# A bold span must never START with one — voice.md: "Never bold the verb."
VERBS = {
    "drove", "shipped", "built", "cut", "defined", "migrated", "designed",
    "codified", "doubled", "led", "scoped", "launched", "reduced", "improved",
    "automated", "eliminated", "delivered", "created", "developed", "extended",
    "replicated", "standardized", "prevented", "closed", "held", "removed",
    "caught", "made", "took", "turned", "specified", "sequenced", "resolved",
    "repositioned", "redirected", "narrowed", "conducted", "integrated",
}

# Skills/label lines whose leading bold label is template style, not emphasis.
LABEL_PREFIXES = ("Technical:", "Product:", "Interests:", "Courses:")

# --- rhythm thresholds -------------------------------------------------------
# CALIBRATION INPUTS, not settled doctrine. Every rhythm finding is WARN-only
# until a human-reviewed calibration sweep over the shipped corpus says which
# of these deserve ERROR. Tune here; nothing else reads the numbers.
# Calibrated 2026-08-22 against all 18 shipped resumes (217 bullets) and the
# critic findings in their review.md files. Each number below is pinned by
# measurement, not taste; the calibration report holds the confusion matrices.
RHYTHM_MAX_CLASS_RATIO = 0.4   # share of bullets allowed to share one closing shape
RHYTHM_MIN_BULLETS = 6         # below this a ratio is noise, not a pattern
RHYTHM_MAX_VERB_REPEATS = 3    # same opening verb on this many bullets
RHYTHM_MAX_WORDS = 42          # a bullet past this stops being skimmable
RHYTHM_MAX_GERUND_STACK = 2    # ", <verb>ing" clauses chained in one bullet

# The final clause is the text after the LAST comma or semicolon. Resume bullets
# carry NO terminal period, so nothing may anchor on `.` — a naive `[^.]*$`
# matches the entire bullet and every classification collapses into one class.
FINAL_CLAUSE = re.compile(r"[,;]([^,;]*)$")
CLS_BY_GERUND = re.compile(r"(?i)\b(?:by|via|through)\s+\w+ing\b")
CLS_GERUND_LED = re.compile(r"(?i)^\s*\w+ing\b")
CLS_WHICH_THAT = re.compile(r"(?i)^\s*(?:which|that)\b")
# `\w+ing` is a SHAPE test, not a gerund detector: "onboarding", "training" and
# "during" all match as nouns or prepositions. That is the residual
# false-positive source for the gerund checks and part of why they are WARN-only;
# the calibration sweep owns what is left. Do not bolt on a stoplist.
#
# The `[a-z]` lead is the ONE case constraint, and it is a measurement not a
# guess: 8 of 19 stacking WARNs across the 18 shipped resumes (42%) came from
# capitalized org-list nouns — ", Engineering, and Compliance" — which are team
# names, not clause chains. It is safe precisely because resume bullets carry no
# terminal periods, so a genuine mid-bullet gerund is never sentence-initial and
# is always lowercase. This pattern must therefore NEVER be compiled (?i): the
# case IS the signal.
GERUND_CHAIN = re.compile(r",\s+[a-z]\w*ing\b")


def _load_hook_rules():
    """Import RULES + DATE_RANGE from the lint hook — voice.md's enforcement
    point stays the single source of truth for the ban list."""
    spec = importlib.util.spec_from_file_location("lint_generated", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.RULES + getattr(mod, "RESUME_RULES", []), mod.DATE_RANGE


def bold_spans(par_xml: str) -> list[str]:
    spans = []
    for r in re.findall(r"<w:r\b.*?</w:r>", par_xml, re.S):
        if "<w:b/>" in r or "<w:b " in r:
            t = "".join(re.findall(r"<w:t(?: [^>]*)?>(.*?)</w:t>", r, re.S))
            if t.strip():
                spans.append(t.strip().rstrip(",.;"))
    return spans


# No count cap on bold since 2026-10-08 (Vaish: bold what should be bold without
# making it noise). The span-shape rules below are the noise guard.


def check_bold(doc: Doc, errors: list, warns: list) -> None:
    for p in doc.paragraphs():
        text = p.text.strip()
        if not text or not p.is_bullet:
            continue
        if text.startswith(LABEL_PREFIXES):
            continue  # label bold is template style
        spans = bold_spans(p.xml)
        where = f"bullet {p.index} ({text[:40]!r})"
        for s in spans:
            first = re.split(r"\s+", s)[0].lower().strip(",.")
            if first in VERBS:
                errors.append(f"bold span starts with a verb in {where}: {s!r} "
                              "(voice.md: never bold the verb)")
            elif len(s.split()) >= 5 and not re.search(r"\d", s):
                errors.append(f"clause-length bold span (5+ words, no digit) in "
                              f"{where}: {s!r} (voice.md: never bold a full clause)")
            elif len(s.split()) == 4 and not re.search(r"\d", s):
                warns.append(f"4-word no-digit bold span in {where}: {s!r} — "
                             "fine if it is a named technology or locked scope term")


# --- tailored-page conventions (Vaish, 2026-10-08) ---------------------------
# Every page under applications/ has one shape: no positioning line, the
# template's section order, bold labels and project names, functional titles,
# no retired claims. Scoped to applications/ because the general and network
# resumes build from the same template under their own rules.
SECTION_ORDER = ["WORK EXPERIENCE", "EDUCATION", "PROJECTS", "SKILLS & INTERESTS"]
SECTION_NAMES = set(SECTION_ORDER) | {"PRODUCTS", "EXPERIENCE", "SKILLS"}
# Official title variants, kept only on forward deployed engineer pages. These
# are invented placeholders that match pipeline/testdata/record.example.json:
# set them to the official variants your own record.json sanctions.
OFFICIAL_TITLES = ("Operations Associate",)
# Program titles are never used on any page, because the record sanctions none.
RETIRED_TITLES = OFFICIAL_TITLES + ("Technical Program Manager", "Program Manager",
                                    "Program Management Intern")
# Forward deployed engineer pages keep the official titles (Vaish, 2026-10-13):
# an engineering reader discounts "Product Owner".
# A forward deployed role is FDE unless it is a PM, program or strategist role
# (Forward Deployed PM roles stay on the PM framing and its titles).
FDE_TITLE_RE = re.compile(r"(?i)\bforward[- ]deployed\b|\bFDE\b")
FDE_NOT_RE = re.compile(r"(?i)\b(?:product|program)\s+manag|\bPM\b|\bstrategist\b")
FDE_RETIRED_TITLES = ("Technical Program Manager", "Program Manager",
                      "Program Management Intern")
# The functional variants, never used on an FDE page (also invented placeholders).
FUNCTIONAL_TITLES = ("Product Owner", "Product Analyst")
LABEL_RE = re.compile(r"^([A-Z][A-Za-z &/]{1,25}):")
# The CSPO lives in SKILLS & INTERESTS; Education holds degrees only (Vaish, 2026-10-15).
CERT_RE = re.compile(r"CSPO|Scrum Alliance|Certif", re.I)
# Every tailored page carries the CSPO linked to its badge (Vaish, 2026-10-15).
CSPO_BADGE_URL = "https://app.badgecert.com/public/badges/kjhswadu"
HYPERLINK_RE = re.compile(r'<w:hyperlink\b[^>]*?r:id="([^"]+)"[^>]*>(.*?)</w:hyperlink>', re.S)
RECORD_PATH = Path(__file__).resolve().parent.parent / "profile" / "knowledge" / "record.json"


def is_tailored_page(docx_path: Path) -> bool:
    return "applications" in Path(docx_path).resolve().parts


def job_title_of(docx_path: Path) -> str:
    """The job's title from the folder's brief.md heading ("# Company - Title")."""
    brief = Path(docx_path).resolve().parent / "brief.md"
    try:
        first = brief.read_text(encoding="utf-8-sig").lstrip().splitlines()[0]
    except OSError:
        return ""
    head = first.lstrip("#").strip()
    return head.split(" - ", 1)[1] if " - " in head else head


def is_fde_title(title: str) -> bool:
    title = title or ""
    return bool(FDE_TITLE_RE.search(title)) and not FDE_NOT_RE.search(title)


def retired_claims(record: Path = RECORD_PATH) -> list[dict]:
    try:
        return json.loads(record.read_text(encoding="utf-8")).get("retired_claims", [])
    except (OSError, ValueError):
        return []


def _first_run_bold(par_xml: str) -> bool:
    for r in re.findall(r"<w:r(?: [^>]*)?>.*?</w:r>", par_xml, re.S):
        if "".join(re.findall(r"<w:t(?: [^>]*)?>(.*?)</w:t>", r, re.S)).strip():
            return bool(re.search(r'<w:b(?: w:val="(?:1|true|on)")?/>', r))
    return False


def check_tailored_page(doc: Doc, errors: list, claims: list[dict] | None = None,
                        fde: bool = False) -> None:
    paras = [p for p in doc.paragraphs() if p.text.strip()]
    if any('w:val="Positioning"' in p.xml for p in paras):
        errors.append("a positioning line is on the page; tailored resumes carry none since "
                      "2026-10-08 (delete_paragraph on the Positioning slot, spend the lines "
                      "on a bullet)")
    heads = [p.text.strip() for p in paras if not p.is_bullet and p.text.strip() in SECTION_NAMES]
    if heads != SECTION_ORDER:
        errors.append(f"section order is {heads}; every tailored resume keeps the template's "
                      f"{SECTION_ORDER}")
    section = ""
    for p in paras:
        text = p.text.strip()
        if not p.is_bullet and text in SECTION_NAMES:
            section = text
            continue
        if fde and not p.is_bullet and text.startswith(FUNCTIONAL_TITLES):
            errors.append(f"functional title on an FDE page: {text[:60]!r}; forward deployed "
                          "engineer pages keep the official title variants record.json "
                          "sanctions")
        elif not p.is_bullet and text.startswith(FDE_RETIRED_TITLES if fde else RETIRED_TITLES):
            errors.append(f"retired title on the page: {text[:60]!r}; titles are the functional "
                          "variants record.json sanctions, or the official ones on an FDE page")
        if section == "EDUCATION" and CERT_RE.search(text):
            errors.append(f"a certification is under EDUCATION: {text[:60]!r}; the CSPO is not a "
                          "degree credential (under Dartmouth it reads as Dartmouth's). It goes in "
                          "SKILLS & INTERESTS: 'Agile (CSPO® Certified)' on the Product line, or a "
                          "'Certification:' line there when the JD asks for one")
        label = LABEL_RE.match(text)
        is_label = (section == "SKILLS & INTERESTS" and label) or text.startswith("Courses")
        is_project = section == "PROJECTS" and not p.is_bullet and " | " in text
        if (is_label or is_project) and not _first_run_bold(p.xml):
            what = "project name" if is_project else "label"
            errors.append(f"{what} not bold: {text[:50]!r} (the template bolds it)")
    full = "\n".join(p.text for p in paras)
    for c in (retired_claims() if claims is None else claims):
        if re.search(c["pattern"], full, re.I):
            errors.append(f"retired claim /{c['pattern']}/ is on the page; use: {c['use']}")


def check_cspo_link(paras_xml: list[str], targets: dict[str, str], errors: list) -> None:
    """The CSPO must be on the page as a link to its badge (Vaish, 2026-10-15)."""
    for xml in paras_xml:
        for rid, inner in HYPERLINK_RE.findall(xml):
            text = "".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", inner))
            if targets.get(rid) == CSPO_BADGE_URL and re.search(r"CSPO|Scrum Product Owner", text):
                return
    errors.append("the CSPO is not linked to its badge; every resume carries it linked: "
                  f"'Agile ([CSPO®]({CSPO_BADGE_URL}) Certified)' on the Product line, or the "
                  "Certification line in SKILLS & INTERESTS (SKILL.md)")


def pdf_text(pdf_path: Path) -> str:
    from pypdf import PdfReader
    return "\n".join((pg.extract_text() or "") for pg in PdfReader(str(pdf_path)).pages)


def page_fill_surplus(ys: list[float], line_h: float,
                      bottom_margin_pt: float) -> float:
    """Unused body lines at the foot of the page: how far the lowest baseline
    sits above the page's own bottom margin, divided by one body line.

    Pure arithmetic, separated from the PDF geometry so the bands can be tested
    without rendering anything.
    """
    return (min(ys) - bottom_margin_pt) / line_h


def check_page_count(pdf_path: Path, errors: list) -> None:
    """The one hard requirement: exactly one page. Everything else is advisory."""
    from pypdf import PdfReader
    n = len(PdfReader(str(pdf_path)).pages)
    if n != 1:
        errors.append(f"PDF renders on {n} pages; the resume must be exactly one page")


def check_page_fill(pdf_path: Path, bottom_margin_pt: float | None,
                    errors: list, warns: list) -> None:
    """One page is the budget; ending short throws part of the budget away.

    The reference is the docx's OWN bottom margin (`<w:pgMar w:bottom>`, via
    `docx_engine.Doc.bottom_margin_pt`), because that is where the printable
    page actually ends. It used to be the measured TOP GAP, and that was wrong
    twice over (LEARNINGS 2026-09-03): the top gap is the first line's ASCENT,
    not a margin — 26.9pt against a real top margin of 11.9pt — so every
    measurement was ~1.3 body lines lenient; and being a measurement of the
    render rather than of the page, it could not see room a MARGIN CUT creates
    at all. On one resume the 12pt-to-6pt cut freed 2.8 body lines that
    this check reported as zero surplus.

    Added 2026-08-20, after a resume that had already exited its critic loop was
    found sitting at 43.8pt bottom against a 26.9pt top: 3.7 body lines wasted.
    Cause was procedural and would recur. The bottom gap had been measured once
    at round 0, then six widow reclaims across three critic rounds each handed a
    line back and only one was ever spent. Prose in the skill could not stop
    that; a check that runs before every critic round can.

    Only meaningful on a single-page render: on 2+ pages the problem is
    overflow, which the one-page loop already owns.
    """
    if bottom_margin_pt is None:
        warns.append("page-fill leg skipped: the docx carries no <w:pgMar>, so the "
                     "page's own bottom margin is unknown and there is no honest "
                     "reference to measure the foot of the page against")
        return
    from pypdf import PdfReader
    reader = PdfReader(str(pdf_path))
    if len(reader.pages) != 1:
        return
    page = reader.pages[0]
    baselines: list[float] = []
    page.extract_text(visitor_text=lambda t, cm, tm, fd, fs:
                      baselines.append(round(tm[5], 1)) if t.strip() else None)
    ys = sorted(set(baselines))
    if len(ys) < 10:
        return
    gaps = [ys[i + 1] - ys[i] for i in range(len(ys) - 1)]
    body = sorted(g for g in gaps if 9.0 < g < 15.0)
    if not body:
        return
    line_h = body[len(body) // 2]

    surplus_lines = page_fill_surplus(ys, line_h, bottom_margin_pt)
    if surplus_lines < 1.0:
        return
    msg = (f"page runs SHORT: lowest baseline sits {min(ys):.1f}pt up the page "
           f"against the docx's own {bottom_margin_pt:.1f}pt bottom margin, "
           f"which is {surplus_lines:.1f} body "
           f"lines of unused space on a one-page document. Spend it on the most "
           f"JD-relevant evidence from the master resume or profile/knowledge/: `doc.clone_paragraph(src, before)` "
           f"adds a bullet that inherits the template's numbering and indent, or "
           f"lengthen an existing bullet with anchored detail. Never pad")
    (errors if surplus_lines >= 2.0 else warns).append(msg)


# The only font families a resume may name or embed. Times New Roman is the
# template's; Liberation Serif is what a Linux seat metric-substitutes for it,
# and that substitution is SAFE precisely because the two are metric-identical
# (0 of 73 glyphs differ in advance width, identical hhea/OS-2 verticals).
SANCTIONED_FONTS = ("timesnewroman", "liberationserif")


def _sanctioned(family: str) -> bool:
    """Is this font name one of the sanctioned FAMILIES?

    Matched by containment, not equality, because a PDF carries PostScript
    names: the same face is `Times New Roman` in the docx and
    `TimesNewRomanPS-BoldItalicMT` once embedded. Containment accepts every
    weight and style of a sanctioned family and still rejects `SymbolMT` and
    `OpenSymbol`, which is the distinction that matters here.
    """
    flat = re.sub(r"[^a-z]", "", family.casefold())
    return any(s in flat for s in SANCTIONED_FONTS)


def check_font_families(doc: Doc, pdf_path: Path | None,
                        errors: list, warns: list) -> None:
    """A font the rendering seat does not have will be SUBSTITUTED, silently,
    and a substituted font sets line heights.

    This is the check that would have caught the 2026-09-04 defect on the day
    it shipped. `numbering.xml` named the bullet glyph's font as **Symbol**,
    whose Windows metrics are 1.2251 em against Times' 1.1499 -- and since a
    line's height is the max over its runs, and the numbering glyph is a run on
    the bullet's FIRST line, every bullet on a seat WITH Symbol MT (Word,
    LibreOffice on Windows) set 0.75pt taller than on a seat without it. Over
    ~15 bullets that is a whole body line, which is why the cloud counted one
    page for 40 resumes Word paginates to two.

    So both halves are checked, because they fail differently: the DOCX must not
    NAME a font outside the sanctioned set (that is the defect at rest), and the
    rendered PDF must not EMBED one (that is the defect after a substitution
    nobody declared). A page whose height depends on a font the next seat may
    not have is a page count that is not trustworthy, which makes this an ERROR
    rather than a warning.
    """
    named: set[str] = set()
    for part in ("word/numbering.xml", "word/styles.xml", "word/document.xml"):
        blob = doc._parts.get(part)
        if not blob:
            continue
        xml = blob.decode("utf-8", errors="replace")
        # Only inside <w:rFonts>. The same attribute NAMES appear on <w:lang>,
        # where `w:eastAsia="en-US"` is a locale and not a font at all -- read
        # them loose and the check reports the language tag as a missing family.
        for tag in re.findall(r"<w:rFonts(?=[ />])[^>]*>", xml):
            named |= set(re.findall(r'w:(?:ascii|hAnsi|eastAsia|cs)="([^"]+)"',
                                    tag))
    for fam in sorted(named):
        if not _sanctioned(fam):
            errors.append(
                f"the docx names the font {fam!r}, which is not one of "
                f"{', '.join(SANCTIONED_FONTS)}. A seat without it substitutes "
                "silently, and a substituted font sets line heights - this is "
                "the class of defect that made 40 resumes count one page in the "
                "cloud and two in Word (2026-09-04)")

    if pdf_path is None or not pdf_path.exists():
        return
    from pypdf import PdfReader
    embedded: set[str] = set()
    try:
        for page in PdfReader(str(pdf_path)).pages:
            fonts = (page.get("/Resources") or {}).get("/Font") or {}
            try:
                fonts = fonts.get_object()
            except AttributeError:
                pass
            for ref in fonts.values():
                try:
                    base = str(ref.get_object().get("/BaseFont", ""))
                except Exception:
                    continue
                embedded.add(base.lstrip("/").split("+")[-1])
    except Exception as exc:                       # a render we cannot read is
        warns.append(f"embedded-font leg skipped: {str(exc)[:120]}")
        return
    for fam in sorted(embedded):
        if not _sanctioned(fam):
            errors.append(
                f"the rendered PDF embeds {fam!r}, which is outside "
                f"{', '.join(SANCTIONED_FONTS)}. Whatever named it, this seat "
                "resolved it to a face another seat will resolve differently, "
                "so this render's page count does not transfer")


def check_parse_back(pdf_path: Path, errors: list, warns: list) -> None:
    """Re-derive employer / title / dates from the PDF and diff the record.

    Every other check here reasons about resume TEXT. This one asks the
    question an ATS answers first -- who, what title, which dates -- and
    compares the answer to master-doc. Added 2026-09-01 (Phase 5) after a corpus
    scan found a role's end date rendered as `Present` against a record that
    closes it on a specific day. The template renders the closed range correctly,
    so the drift was introduced tailor-side, and preflight is the one place every
    critic round and every cloud tailor run already passes through.

    Grading lives in parse_back: a CONTRADICTION is an ERROR, a date coarser
    than the record supports is a WARN. Wired in only after the audit over all
    58 shipped PDFs produced zero false positives -- an ERROR-grade check here
    blocks every tailor, local and cloud (LEARNINGS 2026-08-18: before shipping
    an alarm, check what it reads today).
    """
    try:
        # Imported INSIDE the try on purpose: an ImportError here would
        # otherwise escape main() and take preflight down before every critic
        # round, which is precisely what this function promises cannot happen.
        # It also means the except clauses cannot NAME parse_back.AnchorError,
        # so the two failure grades are told apart by class name instead.
        import parse_back
        findings = parse_back.check(pdf_path)
    except Exception as exc:                      # noqa: BLE001
        if type(exc).__name__ == "AnchorError":
            # The record could not be READ. Loud, because the alternative is a
            # check that silently is not running.
            errors.append(f"parse-back could not read the record: {exc}")
        else:
            warns.append(f"parse-back leg skipped ({exc.__class__.__name__}: {exc})")
        return
    for grade, msg in findings:
        (errors if grade == "error" else warns).append(f"parse-back — {msg}")


def check_hyphen_ats(doc_text: str, pdf: str, errors: list, warns: list,
                     jd_text: str = "") -> None:
    """Every hyphenated token in the docx must survive PDF text extraction
    intact. Three failure grades, because ATS extractors disagree:
    - fused ("embeddingbased") — the keyword is dead in ANY extractor: ERROR.
    - split across the line break with hyphen kept ("embedding-\\nbased") —
      pypdf keeps it, other extractors fuse or space it: ERROR when the term's
      stem appears in the JD (a keyword match is at stake), WARN otherwise.
    - split by injected spaces on ONE line ("real -time"): the renderer's text
      layer is corrupt. Same grading as a line break.

    That last grade used to be waived as pypdf noise, and it was the wrong
    call. It is a property of the RENDERER, not the extractor: Word's PDF
    writer sprays spaces through justified text and every gap-inference
    extractor (the family most ATS parsers belong to) reads them, while
    soffice's layer is clean for the same docx. Now that soffice writes every
    shipped PDF (docx_engine.default_renderer, 2026-08-19), a spray means
    something really is wrong rather than "pypdf again".
    """
    jd_lower = jd_text.lower()
    pdf_nospace = re.sub(r"\s+", "", pdf)
    for tok in sorted(set(re.findall(r"\b\w+(?:-\w+)+\b", doc_text))):
        if re.fullmatch(r"[\d-]+", tok):
            continue  # date ranges / numeric spans
        stem = tok.split("-")[0].lower()
        at_stake = bool(jd_lower and len(stem) > 3 and stem in jd_lower)
        # Whitespace-tolerant match, then inspect what the whitespace WAS.
        loose = re.compile(r"\s*".join(re.escape(c) for c in tok))
        m = loose.search(pdf)
        if m:
            hit = m.group(0)
            if hit == tok:
                continue  # extracted verbatim
            if "\n" in hit:
                broken = "sits on a line break (extractors variously fuse, space, or keep it)"
            else:
                broken = (f"extracts with injected spaces as {hit!r} — the PDF text "
                          "layer splits the keyword")
            grade = errors if at_stake else warns
        elif tok.replace("-", "") in pdf_nospace:
            broken = f"extracts fused as {tok.replace('-', '')!r}"
            grade = errors
        else:
            warns.append(f"hyphenated term {tok!r} not found in the PDF text layer "
                         "in any form — extraction is unreadable there; inspect manually")
            continue
        grade.append(f"hyphenated term {tok!r} {broken} in the PDF text layer — "
                     "re-render with soffice, or rephrase/reflow so it cannot "
                     "land on a line break"
                     + (" (JD keyword at stake)" if grade is errors else ""))


def check_widows(doc: Doc, pdf: str, warns: list) -> None:
    # Normalized the same way compare_renderers.normalize does, so the two
    # files reason over identical line text. (This does NOT change any word
    # count -- bare .split() already collapses whitespace runs -- it just stops
    # the two modules disagreeing about what a rendered line says.)
    lines = [" ".join(ln.split()) for ln in pdf.splitlines() if ln.strip()]
    for p in doc.paragraphs():
        if not p.is_bullet or not p.text.strip():
            continue
        if p.text.strip().startswith(LABEL_PREFIXES):
            continue
        tail_words = p.text.strip().split()[-2:]
        head_words = p.text.strip().split()[:4]
        for i, ln in enumerate(lines):
            if ln.split()[-2:] == tail_words and len(ln.split()) <= 3:
                # last rendered line of this bullet holds <=3 words?
                if " ".join(head_words) not in ln:
                    warns.append(f"possible widow line ({len(ln.split())} words: "
                                 f"{ln!r}) ending bullet {p.index} — a rephrase "
                                 "reclaims a full line")
                break


def role_bullets(doc: Doc) -> list[tuple[str, str, int]]:
    """[(employer, role line, bullet count)] down WORK EXPERIENCE."""
    out: list[list] = []
    employer, in_work = "", False
    for p in doc.paragraphs():
        text = p.text.strip()
        if not text:
            continue
        if not p.is_bullet and text in SECTION_NAMES:
            in_work = text == "WORK EXPERIENCE"
            continue
        if not in_work:
            continue
        if p.is_bullet:
            if out:
                out[-1][2] += 1
        elif len(text.split()[0]) > 2 and text.split()[0].isupper():
            employer = text   # "FIDELITY INVESTMENTSMerrimack, NH": the tab is not text
        else:
            out.append([employer, text, 0])
    return [tuple(r) for r in out]


def check_weight(doc: Doc, warns: list) -> None:
    """Fidelity is one summer and Citi three years (Vaish, 2026-10-13): warn when
    the internship outweighs every Citi role. A WARN, because a JD squarely about
    Fidelity's work may justify a tie, and the tailor says why in the brief."""
    roles = role_bullets(doc)
    fid = sum(n for e, _, n in roles if e.upper().startswith("FIDELITY"))
    citi = [n for e, _, n in roles if e.upper().startswith("CITIGROUP")]
    if fid and citi and fid > max(citi):
        warns.append(f"Fidelity has {fid} bullets, more than any Citi role ({citi}); Citi "
                     "carries the weight unless the JD is squarely Fidelity's work")


def check_dates(doc: Doc, warns: list) -> None:
    month_year = year_only = 0
    for p in doc.paragraphs():
        t = p.text
        if re.search(r"(?:January|February|March|April|May|June|July|August|"
                     r"September|October|November|December)\s+20\d\d\s*–", t):
            month_year += 1
        elif re.search(r"\b20\d\d\s*–\s*(?:20\d\d|Present)", t):
            year_only += 1
    if month_year and year_only:
        warns.append(f"mixed date formats ({month_year} month-year vs {year_only} "
                     "year-only ranges); use one format throughout, record.json has the months")


def final_clause(text: str) -> str:
    """Everything after the last comma or semicolon; the whole bullet if neither
    appears. This is what a skimming reader's eye lands on last, and it is where
    the shipped corpus's uniformity concentrated."""
    m = FINAL_CLAUSE.search(text)
    return (m.group(1) if m else text).strip()


def clause_class(clause: str) -> str:
    """Shape class of a final clause. Order is deliberate: a clause can both
    start with a gerund and carry a `by <verb>ing` tail ("cutting review time by
    embedding it"), and by-gerund is the shape the critic actually named."""
    if CLS_BY_GERUND.search(clause):
        return "by-gerund"
    if CLS_GERUND_LED.match(clause):
        return "comma-gerund"
    if CLS_WHICH_THAT.match(clause):
        return "which-that"
    toks = clause.split()
    if toks:
        metric = sum(1 for t in toks if re.search(r"[\d%$]", t))
        if re.match(r"^\s*\d", clause) or metric * 2 > len(toks):
            return "bare-metric"
    return "other"


# The stacked-abstract-noun check that lived here was DELETED 2026-08-22 by the
# calibration sweep, using the four-item deletion map it shipped with. Recorded
# so nobody rebuilds it: across all 217 shipped bullets the longest abstract-noun
# run was 2, reached exactly 3 times, and all three are legitimate English
# ("prediction interpretation", "activation friction", "specification
# completion"). So the check never fired at its threshold of 3, and lowering it
# to 2 would have produced 3 false positives against a >2 deletion criterion. It
# was a check with no reachable true positive on this corpus.


def check_rhythm(doc: Doc, errors: list, warns: list) -> None:
    """Mechanical AI-smell checks over the bullets: shape uniformity, repeated
    lead verbs, over-long bullets, stacked gerund clauses.

    `errors` is accepted and deliberately never written. Every finding here is
    WARN-only until a human-reviewed calibration sweep over the shipped corpus
    justifies promoting a threshold — an ERROR blocks the critic round, and a
    checker that blocks on an uncalibrated heuristic trains people to ignore it.
    """
    del errors  # WARN-only by design; see docstring.

    bullets = []
    for p in doc.paragraphs():
        t = p.text.strip()
        is_pos = 'w:val="Positioning"' in p.xml
        if not t or (not p.is_bullet and not is_pos) or t.startswith(LABEL_PREFIXES):
            continue
        bullets.append((p.index, t))   # the positioning line is judged like a bullet
    if not bullets:
        return

    def where(idx: int, text: str) -> str:
        return f"bullet {idx} ({text[:40]!r})"

    # 1. uniform trailing-clause class. CALIBRATION (2026-08-22): 0.40 kept.
    # Against the LAST critic round per folder (the round that judged the docx
    # measured here) it scores 5 TP / 2 FP / 2 FN; dropping the cap to 0.30
    # trades those 2 FPs for 7. It only looks too loose if you label a folder by
    # findings from an EARLIER round, which the tailor then fixed. Known ceiling
    # on this check: the critic counts one class ("trailing participial clause",
    # 7-12 of 12 on the shipped corpus) where clause_class splits the same
    # bullets across by-gerund, comma-gerund and `other`, so no ratio reaches the
    # critic's sensitivity. That is a taxonomy fix, not a threshold one.
    # The denominator is EVERY counted bullet,
    # including the `other` ones: `other` is the absence of a shape, so it can
    # never trigger, but excluding it from the denominator would let three
    # by-gerund bullets among twenty read as 100% uniform.
    if len(bullets) >= RHYTHM_MIN_BULLETS:
        counts: dict[str, list] = {}
        for idx, t in bullets:
            cls = clause_class(final_clause(t))
            if cls != "other":
                counts.setdefault(cls, []).append((idx, t))
        for cls, members in sorted(counts.items(), key=lambda kv: -len(kv[1])):
            ratio = len(members) / len(bullets)
            if ratio > RHYTHM_MAX_CLASS_RATIO:
                idx, t = members[0]
                warns.append(
                    f"uniform bullet shape: {len(members)} of {len(bullets)} "
                    f"bullets ({ratio:.0%}) close in the {cls!r} pattern, over "
                    f"the {RHYTHM_MAX_CLASS_RATIO:.0%} cap — e.g. "
                    f"{where(idx, t)}. Vary how bullets END: a metric close, a "
                    "scope close and a by-gerund close should not all be the "
                    "same one")

    # 2. repeated opening verb. CALIBRATION (2026-08-22): 3 kept, and pinned by
    # ground truth rather than left alone. The critic named repeated openers at
    # exactly three ("Cut" x3-4 on chime, "Drove" x3 + "Cut" x3 on microsoft), so
    # a threshold of 4 would miss the cases a human actually flagged; on the
    # shipped corpus only one folder reaches 4 at all.
    openers: dict[str, list] = {}
    for idx, t in bullets:
        tok = re.split(r"\s+", t)[0].lower().strip(",.;:")
        if tok:
            openers.setdefault(tok, []).append(idx)
    for tok, idxs in openers.items():
        if len(idxs) >= RHYTHM_MAX_VERB_REPEATS:
            warns.append(
                f"opening verb {tok!r} leads {len(idxs)} bullets "
                f"({', '.join(str(i) for i in idxs)}) — a repeated lead verb "
                "reads as one template; swap in the strongest distinct verb the "
                "master doc supports")

    for idx, t in bullets:
        # 3. bullet length. CALIBRATION (2026-08-22): 32 -> 42, the single
        # biggest correction this sweep made. At 32 the check flagged 155 of 217
        # shipped bullets on all 18 resumes, i.e. it described the corpus instead
        # of naming outliers, and a checker that flags legitimate content trains
        # people to ignore it.
        #
        # A word count is a PROXY. What it stands in for is a bullet eating a
        # third justified line, so the cap was fitted to that: measured against
        # rendered line counts in the shipped PDFs, 42 scores precision 0.88 /
        # recall 0.75 against "renders to 3+ lines" (the F1 optimum; 40 is
        # 0.61/0.85, 44 is 0.90/0.45). 42 is the corpus's 90th-percentile bullet
        # length, and because the test is strictly greater-than it flags the top
        # 7.8% -- 17 bullets across 10 folders.
        #
        # Against the DIRECT ground truth it is a compromise, not a win: the
        # corpus holds three bullets a critic explicitly called run-ons, at 45,
        # 44 and 40 words, and 42 catches two of the three. Catching all three
        # needs 39, which costs precision (36 bullets, 0.51).
        # DECIDED 2026-08-22 by Vaish: 42 stays (docs/calibration-2026-08-22.md
        # section 4 was the basis). The 40-word tail is the critic's readability
        # axis to catch, not this regex. Every finding here is still WARN-only;
        # no ERROR promotion was requested.
        n = len(t.split())
        if n > RHYTHM_MAX_WORDS:
            warns.append(
                f"bullet length: {n} words in {where(idx, t)} — past "
                f"{RHYTHM_MAX_WORDS} a bullet stops surviving the 6-second "
                "skim; cut connective filler, never metrics or scope")

        # 4. gerund clause-stacking. CALIBRATION (2026-08-22): 2 kept, and the
        # stoplist T2 left open is deliberately NOT added. Every one of the 22
        # matched clauses behind the 11 WARNs on the shipped corpus is a real
        # gerund (migrating, prioritizing, codifying, achieving, ...); the
        # residual false-positive class the case constraint could not reach
        # ("onboarding", "training", "during") did not occur once. A stoplist
        # would cost complexity to remove zero measured noise.
        chain = GERUND_CHAIN.findall(t)
        if len(chain) >= RHYTHM_MAX_GERUND_STACK:
            warns.append(
                f"gerund clause-stacking: {len(chain)} ', <verb>ing' clauses in "
                f"{where(idx, t)} — the 'turning X into Y, cutting Z' chain is "
                "the most machine-sounding shape on the page; split the bullet "
                "or drop a link")


# Shorthand compounds of the "intake-to-approval" / "time-to-process" shape.
# LETTERS ONLY on both sides, which is the whole precision story: `0-to-1` and
# `3-to-4-week` are numeric spans a person really does say aloud, and they sit
# on differentiator terms the bank uses deliberately. `end-to-end` is ordinary
# English and is allowlisted by name.
PLAIN_ENGLISH_RE = re.compile(r"\b[A-Za-z]+(?:-to-[A-Za-z]+)+\b")
PLAIN_ENGLISH_ALLOW = {"end-to-end"}


def check_plain_english(doc_text: str, warns: list) -> None:
    """voice.md diagnostic 5, "Say it aloud": a bullet must read as a sentence a
    person would say. The mechanical slice of that is the noun-fragment
    compound — "intake-to-approval", "time-to-process", "import-to-outreach" —
    which is resume shorthand nobody speaks, and which a regex can see where
    the rest of the diagnostic needs a human ear.

    WARN only: the tailor writes every bullet and decides, and a blocking
    heuristic here would train it to ignore the whole report.
    """
    seen: dict[str, str] = {}
    for hit in PLAIN_ENGLISH_RE.findall(doc_text):
        low = hit.lower()
        if low in PLAIN_ENGLISH_ALLOW or low in seen:
            continue
        seen[low] = hit
    for hit in seen.values():
        warns.append(f"shorthand compound {hit!r}: voice.md diagnostic 5 (Say it aloud) "
                     f"wants it as words a person would say")


def check_reader_terms(pdf_text: str, jd_text: str, warns: list) -> None:
    """W9 (2026-10-16): domain terms on the page that the JD's reader may not know.

    reader_terms.unexplained() leaves out a term the JD uses or whose domain the
    JD shares, so what is left is a bank or tool word written for the wrong reader.
    WARN only: the tailor decides whether to say what the thing does instead
    (each warning carries a true plain gloss) or to keep the name because the
    work needs it. A term on the Skills lines is findability, reported apart.
    """
    if not jd_text:
        return
    from reader_terms import unexplained
    hits = unexplained(pdf_text, jd_text)
    for t in hits["prose"]:
        warns.append(f"reader term {t.name!r}: the JD never uses it; say what it is for "
                     f"this reader (e.g. \"{t.gloss}\") or keep it only if the bullet needs "
                     "the name")
    if hits["skills"]:
        warns.append("reader terms on the Skills lines the JD never uses: "
                     + ", ".join(t.name for t in hits["skills"])
                     + " (findability only; cut any this reader would not search)")


def check_text_layer(doc_text: str, pdf_text: str, warns: list) -> None:
    """Every word in the docx should survive into the PDF text layer.

    A parser reads the text layer, not the glyphs. One run shipped "features
    tha were built" in the layer while the page looked fine; the ATS scanner
    caught it by luck. Hyphenated words are skipped (line-break hyphenation is
    check_hyphen_ats's job) and this is a WARN: the tailor decides whether a
    miss is a real defect or an extraction quirk.
    """
    if not pdf_text.strip():
        return
    have = set(re.findall(r"[A-Za-z][A-Za-z']+", pdf_text.lower()))
    # Doc.text joins tab-separated runs with nothing between them ("COLLEGEHanover"),
    # so split at case boundaries before comparing.
    split = re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", doc_text)
    missing = []
    for w in sorted({w for w in re.findall(r"[A-Za-z][A-Za-z']+", split) if len(w) >= 4}):
        lw = w.lower()
        if lw not in have and not any(lw in h for h in have if len(h) > len(lw)):
            missing.append(w)
    if missing:
        warns.append("docx words missing from the PDF text layer (parser sees the layer, not the "
                     f"page): {', '.join(missing[:8])}")


def check_text_guardrails(doc_text: str, errors: list) -> None:
    rules, date_range = _load_hook_rules()
    masked = date_range.sub("", doc_text)
    for why, rx in rules:
        hits = sorted({m.group(0) for m in rx.finditer(masked)})
        if hits:
            errors.append(f"{why}: {', '.join(hits[:5])}")


# What an unquoted shell heredoc leaves behind when resume text passes through
# it: "$0" becomes the shell's name, "$200K" loses "$2" and reads "00K", "$1.5M"
# reads ".5M", and a quote fight turns "Fidelity's" into `Fidelity"s`. All four
# reached a reviewed page in the cloud (09-29 Fifth Third, 10-02 Mimecast).
SHELL_DAMAGE = [
    ("shell expanded $0 into a path", re.compile(r"/(?:usr/)?bin/(?:ba)?sh\b")),
    ("a $N figure lost its leading digits to the shell",
     re.compile(r"(?<![\w$.,])(?:00\d*|\.\d+)\s?[KMB]\b|(?<![\w$.,])00\d+\+?")),
    ("an apostrophe became a double quote", re.compile(r"[A-Za-z]\"s\b")),
]


def check_shell_damage(doc_text: str, errors: list) -> None:
    for why, rx in SHELL_DAMAGE:
        hits = sorted({m.group(0) for m in rx.finditer(doc_text)})
        if hits:
            errors.append(f"{why}: {', '.join(hits[:5])} (write resume text through a "
                          "quoted heredoc <<'EOF' or a file)")


# The positioning line under the contact block. A mechanical budget only: what
# the line SAYS is a judgment call the reviewers own. It is optional; when it
# exists it is one or two rendered lines.
POS_MAX_WORDS = 45


def _rendered_lines(line: str, text: str) -> int:
    """How many rendered PDF lines the positioning text spans (0 if not found)."""
    want = re.sub(r"\s+", " ", line).strip()
    rows = [re.sub(r"\s+", " ", r).strip() for r in text.splitlines() if r.strip()]
    for i, row in enumerate(rows):
        if not want.startswith(row[:25]) or len(row) < 10:
            continue
        acc, n = "", 0
        for row2 in rows[i:]:
            acc = (acc + " " + row2).strip()
            n += 1
            if len(acc) >= len(want) * 0.95:
                return n
            if not want.startswith(acc[:len(acc)]):
                break
    return 0


def check_positioning(doc: Doc, text: str, errors: list, warns: list) -> None:
    """The paragraph carrying the `Positioning` style: at most one, and inside
    the page budget when present. Absence is a deliberate choice (SKILL.md §2).

    A MISSING paragraph is a WARN, not an ERROR: older folders were built before
    the template carried the style and must stay resumable. Two paragraphs is an
    ERROR because the page has exactly one slot for it.
    """
    pos = [p for p in doc.paragraphs() if 'w:val="Positioning"' in p.xml]
    if not pos:
        warns.append("no paragraph carries the Positioning style - the template "
                     "seeds one under the contact block")
        return
    if len(pos) > 1:
        errors.append(f"{len(pos)} paragraphs carry the Positioning style; exactly one may")
        return

    line = pos[0].text.strip()
    if not line:
        errors.append("the Positioning paragraph is EMPTY")
        return

    words = len(line.split())
    if words > POS_MAX_WORDS:
        warns.append(f"positioning line runs {words} words, over the "
                     f"{POS_MAX_WORDS}-word budget - it is the first thing read "
                     f"and a long one costs bullet space")

    if text:
        n = _rendered_lines(line, text)
        if n > 3:
            warns.append(f"positioning line renders on {n} lines; the budget is three")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("docx")
    ap.add_argument("pdf", nargs="?", help="defaults to <docx>.pdf if present")
    ap.add_argument("--jd", help="jd.md — escalates split JD-keyword terms to ERROR")
    args = ap.parse_args()

    docx_path = Path(args.docx)
    pdf_path = Path(args.pdf) if args.pdf else docx_path.with_suffix(".pdf")
    doc = Doc.load(docx_path)
    doc_text = "\n".join(p.text for p in doc.paragraphs())

    errors: list[str] = []
    warns: list[str] = []
    # Hoisted: check_positioning needs both the docx and the rendered text, and
    # it must run whether or not a PDF exists (the docx-only legs still apply).
    text = pdf_text(pdf_path) if pdf_path.exists() else ""
    check_bold(doc, errors, warns)
    check_text_guardrails(doc_text, errors)
    check_shell_damage(doc_text, errors)
    check_plain_english(doc_text, warns)
    check_dates(doc, warns)
    check_rhythm(doc, errors, warns)
    if is_tailored_page(docx_path):
        check_tailored_page(doc, errors, fde=is_fde_title(job_title_of(docx_path)))
        check_cspo_link([p.xml for p in doc.paragraphs()], doc.hyperlink_targets(), errors)
        check_weight(doc, warns)
    else:
        check_positioning(doc, text, errors, warns)
    check_font_families(doc, pdf_path if pdf_path.exists() else None,
                        errors, warns)
    if pdf_path.exists():
        check_page_count(pdf_path, errors)
        check_text_layer(doc_text, text, warns)
        jd_text = Path(args.jd).read_text(encoding="utf-8") if args.jd else ""
        check_hyphen_ats(doc_text, text, errors, warns, jd_text)
        if is_tailored_page(docx_path):
            check_reader_terms(text, jd_text, warns)
        check_widows(doc, text, warns)
        check_page_fill(pdf_path, doc.bottom_margin_pt(), errors, warns)
        check_parse_back(pdf_path, errors, warns)
    else:
        warns.append(f"no PDF at {pdf_path} — hyphen/widow checks skipped; "
                     "render first, preflight must see what the critic sees")

    for e in errors:
        print(f"ERROR  {e}")
    for w in warns:
        print(f"warn   {w}")
    print(f"\npreflight: {len(errors)} error(s), {len(warns)} warning(s) — "
          + ("FIX BEFORE THE REVIEWERS RUN" if errors else "clear for the reviewers"))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
