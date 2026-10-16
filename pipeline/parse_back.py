"""Parse-back check: re-derive structured fields from a SHIPPED PDF and diff
them against the record (Phase 5).

    python pipeline/parse_back.py <resume.pdf>
    python pipeline/parse_back.py --audit [folder ...]

Why this exists. Everything else in this repo checks resume TEXT: `ats_score`
counts terms, `preflight.check_hyphen_ats` asks whether a keyword survived
extraction, `compare_renderers` compares two renderers to each other. Nothing
asked the question an ATS actually answers first -- who did this person work
for, with what title, between which dates -- and nothing compared that answer
to the record (`profile/knowledge/record.json`). That is the only ATS claim in the pipeline which is
falsifiable, and it is what retires the "should we rebuild the template in
LaTeX/HTML" question with evidence instead of argument (LEARNINGS 2026-08-19:
the format was fine; scan the artifact before redesigning it).

It earned itself before it was written. A corpus scan over the drafts that
preceded this module found a role's end date rendered as `Present` against a
record that closes it on a specific day. The template renders the closed range
correctly, so the drift was introduced tailor-side, downstream of the one file
that knows the truth. That is exactly why the check belongs in preflight, which
every critic round and every cloud tailor run already passes through: a date
that drifts between the record and the page is the single easiest way to put an
indefensible claim on a resume, and the only reliable place to catch it is the
rendered artifact.

GRADING, and why it is not "dates exact" (the phrase in the plan). Taken as
string equality that check fires on the untouched template and on half the
shipped corpus, which is the always-on alarm this repo keeps rediscovering
(LEARNINGS 2026-08-18: before shipping an alarm, check what it reads TODAY).
Dates are compared at the COARSER of the two granularities:

  ERROR  an employer on the page that the record does not hold
  ERROR  a date that CONTRADICTS the record -- different year, a month outside
         the record's range, or `Present` on a role the record has ended
  WARN   a date COARSER than the record supports, where master-doc explicitly
         says to pre-apply the precision (Education range, Penzigo months,
         Citi months). Not decoration and not always-on: 40 of 58 shipped
         resumes already pre-apply the Education range.
  WARN   a role title outside the sanctioned variant set (CLAUDE.md rule 7)

A false ERROR here blocks every tailor, local and cloud, so the unknown-employer
rule is deliberately shape-guarded and the audit was run over all 58 shipped
PDFs before this was wired into preflight at all.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from docx_engine import shipped_resume_docx  # noqa: E402

ROOT = HERE.parent
APPLICATIONS = ROOT / "applications"
# The record the page is diffed against: structured facts (employers, spans,
# every title a resume may render), edited by hand when a date or title changes.
RECORD = ROOT / "profile" / "knowledge" / "record.json"

# --- date vocabulary ---------------------------------------------------------

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
# Sept is the one abbreviation that is not the first three letters, and it is
# live in the record ("Aug 2023 - Sept 2024"). A three-letter prefix match
# handles every other form, full names included.
MONTH_RE = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?"
YEAR_RE = r"(?:19|20)\d{2}"
# Hyphen, all four Unicode dashes Word/soffice emit, and the minus sign. A
# resume date range has been seen with each of the first three.
DASH_RE = r"[-‐‑‒–—―−]"
# A point in time as a resume writes it: "June 2026", "Jun 2026", "2026".
POINT_RE = rf"(?:{MONTH_RE}\s+)?{YEAR_RE}"
PRESENT_RE = r"Present"
# A date line may close on a short qualifier. "November 2026 (Expected)" is
# the correct way to date a degree that has not been conferred yet. Without
# this the end-anchored match fails, no role line is parsed, and the entry goes
# silently ungraded. Caught by the role-count mismatch WARN, which is the net
# that exists for exactly this: a parser miss and an omitted role look
# identical from the outside, so both are reported.
TAIL_RE = r"(?:\s*\([^)]{1,30}\))?"

RANGE_END = re.compile(
    rf"({POINT_RE})\s*{DASH_RE}\s*({POINT_RE}|{PRESENT_RE}){TAIL_RE}\s*$")
BARE_END = re.compile(rf"(?:^|\s)({POINT_RE}){TAIL_RE}\s*$")
# master-doc writes a same-year span with days: "Jun 10 - Aug 7, 2026".
MD_DAY_SPAN = re.compile(
    rf"({MONTH_RE})\s+\d{{1,2}}\s*{DASH_RE}\s*({MONTH_RE})\s+\d{{1,2}},\s*({YEAR_RE})")
MD_RANGE = re.compile(rf"({POINT_RE})\s*{DASH_RE}\s*({POINT_RE}|{PRESENT_RE})")

PRESENT = ("present",)  # sentinel; distinct from any (year, month) tuple


def parse_point(s: str):
    """'June 2026' -> (2026, 6); '2021' -> (2021, None); 'Present' -> PRESENT.

    Returns None on anything else, so a caller never invents a date it could
    not read -- the same reasoning as db.parse_posted_at returning None rather
    than guessing (LEARNINGS 2026-08-11).
    """
    s = (s or "").strip().rstrip(".,")
    if not s:
        return None
    if s.lower() == "present":
        return PRESENT
    m = re.fullmatch(rf"(?:({MONTH_RE})\s+)?({YEAR_RE})", s)
    if not m:
        return None
    mon = MONTHS.get(m.group(1)[:3].lower()) if m.group(1) else None
    return (int(m.group(2)), mon)


def show(point) -> str:
    if point is None:
        return "(none)"
    if point is PRESENT or point == PRESENT:
        return "Present"
    year, mon = point
    if mon is None:
        return str(year)
    return f"{[None, 'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'][mon]} {year}"


# --- the parsed shape --------------------------------------------------------

@dataclass
class Role:
    title: str
    start: object = None
    end: object = None
    raw: str = ""


@dataclass
class Entry:
    kind: str                    # "work" | "education"
    employer: str                # as rendered / as the record names it
    start: object = None         # employer-line span, when the page carries one
    end: object = None
    roles: list = field(default_factory=list)
    raw: str = ""


@dataclass
class Parsed:
    entries: list = field(default_factory=list)
    notes: list = field(default_factory=list)


# --- PDF side ----------------------------------------------------------------

# soffice puts the list glyph in the text layer as U+F0B7 (Symbol-font Private
# Use Area, LEARNINGS 2026-08-19) and compare_renderers.BULLET_CHARS does not
# strip it, so a bullet's FIRST line is self-identifying. Word emits U+2022.
# A bullet's CONTINUATION lines carry no marker, which is why the role-line
# shape test below cannot rely on this alone.
GLYPHS = "•·●▪"

WORK_BAND = "WORK EXPERIENCE"
EDU_BAND = "EDUCATION"

# Section bands are renamed and reordered per JD -- measured across the shipped
# corpus: bands such as `AI PRODUCT & LEADERSHIP` and `LEADERSHIP & AI PRODUCT
# WORK` sit ABOVE Education. So a band is recognised by SHAPE, and only
# the two bands this module grades are recognised by name.
def is_band(line: str) -> bool:
    s = line.strip()
    if not (3 < len(s) <= 60) or any(ch.isdigit() for ch in s):
        return False
    if not any(ch.isalpha() for ch in s):
        return False
    return s == s.upper()


CAPS_RUN = re.compile(r"^((?:[A-Z][A-Z&.'-]*)(?:\s+[A-Z][A-Z&.'-]*)*)\s+(\S.*)$")


def split_employer(line: str):
    """('FIDELITY INVESTMENTS', 'Merrimack, NH') for an employer line, else None.

    An employer line is an all-caps run followed by a mixed-case remainder; a
    section band is an all-caps run with NO remainder (is_band above). The
    guards exist because a bullet CONTINUATION line inside the same section
    carries no marker and can legitimately open on an acronym -- 'API by the
    firm cutoff, with weekly office hours' is a real line from a shipped
    resume. The remainder must therefore open on a capital (a location or a
    pipe-joined location|date always does, a mid-sentence continuation almost
    never does) and stay short enough to be a field rather than prose.
    """
    s = line.strip()
    m = CAPS_RUN.match(s)
    if not m:
        return None
    caps, rest = m.group(1).strip(), m.group(2).strip()
    if len(caps) < 4 or len(caps.split()) > 6:
        return None
    if not rest or not rest[0].isupper():
        return None
    # A field, not a sentence. The longest real one is
    # "Pune, India | July 2022 - June 2025" at 6 words.
    if len(rest.split()) > 10 or rest.endswith("."):
        return None
    return caps, rest


def _role_from(line: str):
    """A title -> date-range line, or None.

    Shape only: the caller has already established that the line sits inside a
    graded section and is not a bullet's first line. A bullet CONTINUATION can
    end in a year, so the guards are the ones that separate a field line from
    prose: no terminal period, bounded length, and a date that sits at the very
    end of the line.
    """
    s = line.strip()
    if not s or s.endswith(".") or len(s) > 120:
        return None
    m = RANGE_END.search(s)
    if m:
        start, end = parse_point(m.group(1)), parse_point(m.group(2))
    else:
        m = BARE_END.search(s)
        if not m:
            return None
        start, end = None, parse_point(m.group(1))
    title = s[:m.start()].strip(" |,")
    if not title:
        return None
    return Role(title=title, start=start, end=end, raw=s)


def parse(pdf_path) -> Parsed:
    """Structured fields as an extractor reads them off the shipped PDF.

    Reads compare_renderers.lines_of -- the same normalized text layer
    ats_score.resume_texts uses -- rather than preflight.pdf_text, because this
    parser keys on line structure and pdf_text joins pages with newlines only.
    """
    import compare_renderers
    return parse_lines(compare_renderers.lines_of(Path(pdf_path)))


def parse_lines(lines) -> Parsed:
    """The parser proper, over already-extracted lines.

    Split out from parse() so every diff class is testable without a renderer:
    a suite that can only run where soffice is installed is a suite that stops
    running (LEARNINGS 2026-09-01 on excluded suites).
    """
    out = Parsed()
    kind = None            # None while outside a graded section
    current: Entry | None = None

    for raw in lines:
        s = raw.strip()
        if not s:
            continue
        if is_band(s):
            if s == WORK_BAND:
                kind = "work"
            elif s == EDU_BAND:
                kind = "education"
            else:
                kind = None          # any other band ends the graded region
            current = None
            continue
        if kind is None:
            continue
        bullet = s[0] in GLYPHS
        if bullet:
            continue                 # bullet bodies carry no structured fields
        emp = split_employer(s)
        if emp:
            name, rest = emp
            start = end = None
            m = RANGE_END.search(rest)
            if m:
                start, end = parse_point(m.group(1)), parse_point(m.group(2))
            current = Entry(kind=kind, employer=name, start=start, end=end, raw=s)
            out.entries.append(current)
            continue
        if current is None:
            continue
        # Project / leadership lines are pipe-delimited and are recognised only
        # so they are EXCLUDED: grading CLAUDE.md rule 6's locked project titles
        # is a separate decision, not this module's job.
        if s.count("|") >= 2:
            continue
        role = _role_from(s)
        if role:
            current.roles.append(role)
    return out


# --- record side -------------------------------------------------------------

class AnchorError(RuntimeError):
    """A master-doc heading this module anchors on matched nothing.

    Raised rather than skipped on purpose. Phase 4a adds
    `### Bullets (CLAIM EXACTLY - locked set)` sections under Fidelity and each
    Citi role -- it edits the very headings anchored here. A silently
    unmatched anchor drops an entry from expected(), and then EVERY resume
    ERRORs with "employer not in the record", which is a corpus-wide false
    alarm that reads exactly like a real one.
    """


def _load_record(record: Path | None = None):
    path = RECORD if record is None else Path(record)
    if not path.exists():
        return None, path
    import json
    try:
        return json.loads(path.read_text(encoding="utf-8")), path
    except ValueError as exc:
        raise AnchorError(f"{path} is not valid JSON: {exc}")


def _pt(s):
    return parse_point(s) if s else None


def expected(record: Path | None = None) -> Parsed:
    """The record's own answer, read from profile/knowledge/record.json.

    Returns an empty Parsed (with a note) when the record file is ABSENT, so a
    clone with no profile/ degrades to a skipped check rather than an error. A
    present but malformed record raises AnchorError.
    """
    data, path = _load_record(record)
    if data is None:
        return Parsed(notes=[f"record absent at {path}; parse-back skipped"])
    out = Parsed()
    for kind in ("work", "education"):
        for e in data.get(kind, []):
            if "employer" not in e or not e.get("roles"):
                raise AnchorError(f"{path}: every {kind} entry needs employer and roles")
            roles = [Role(title=r["title"], start=_pt(r.get("start")), end=_pt(r.get("end")))
                     for r in e["roles"]]
            out.entries.append(Entry(kind=kind, employer=e["employer"],
                                     start=_pt(e.get("start")), end=_pt(e.get("end")),
                                     raw=e["employer"], roles=roles))
    return out


# Employer names as the RESUME renders them, mapped to the record's name. The
# resume writes NIT Karnataka out in full and small-caps every employer, so a
# raw string compare would report five unknown employers on every resume.
ALIASES = {
    "FIDELITY INVESTMENTS": "Fidelity Investments",
    "CITIGROUP": "Citigroup",
    "PENZIGO": "Penzigo",
    "DARTMOUTH COLLEGE": "Dartmouth College",
    "NATIONAL INSTITUTE OF TECHNOLOGY KARNATAKA": "NIT Karnataka",
    "NIT KARNATAKA": "NIT Karnataka",
}


def sanctioned_titles(record: Path | None = None) -> dict:
    """{record role title -> {every title variant a resume may render}}."""
    data, _ = _load_record(record)
    if data is None:
        return {}
    out: dict[str, set] = {}
    for kind in ("work", "education"):
        for e in data.get(kind, []):
            for r in e.get("roles", []):
                variants = {r["title"], r["title"].split(",")[0].strip()}
                variants.update(r.get("variants", []))
                out[r["title"]] = variants
    return out


# --- the diff ----------------------------------------------------------------

def _cmp_point(page, rec, where: str, what: str, findings: list) -> None:
    """One end of one date range, graded contradiction vs. lost precision."""
    if rec is None:
        return
    if page is None:
        findings.append(("warn", f"{where}: {what} is missing from the page; the "
                                 f"record holds {show(rec)}"))
        return
    if page == PRESENT or page is PRESENT:
        if rec != PRESENT:
            findings.append(("error", f"{where}: {what} renders as 'Present' but the "
                                      f"record ends it at {show(rec)} — the page "
                                      f"claims current employment that ended"))
        return
    if rec == PRESENT or rec is PRESENT:
        return                       # the record is open-ended; a concrete date is fine
    ryear, rmon = rec
    pyear, pmon = page
    if pyear != ryear:
        findings.append(("error", f"{where}: {what} is {show(page)} but the record "
                                  f"says {show(rec)}"))
        return
    if pmon is None and rmon is not None:
        findings.append(("warn", f"{where}: {what} renders as a bare year "
                                 f"({show(page)}) where the record holds {show(rec)} "
                                 f"— master-doc says to pre-apply the precision"))
        return
    if rmon is not None and pmon != rmon:
        findings.append(("error", f"{where}: {what} is {show(page)} but the record "
                                  f"says {show(rec)}"))


# A degree LEVEL, in every spelling the record and the resume use between them:
# master-doc writes "B.Tech, Electronics and Communication Engineering" while
# the resume writes "Bachelor of Technology in Electronics and Communication
# Engineering". Same degree, no shared substring at the front.
DEGREE_LEVELS = (
    ("bachelor", (r"\bbachelor\b", r"\bb\.?\s?tech\b", r"\bb\.?\s?e\.?\b", r"\bb\.?s\.?\b")),
    ("master", (r"\bmaster\b", r"\bm\.?\s?eng\b", r"\bmem\b", r"\bm\.?s\.?\b")),
    ("phd", (r"\bph\.?\s?d\b", r"\bdoctor")),
)


def _degree_level(text: str):
    low = text.lower()
    for level, pats in DEGREE_LEVELS:
        if any(re.search(p, low) for p in pats):
            return level
    return None


def _degree_field(title: str) -> str:
    """The subject, with the level and its connective stripped off."""
    t = re.sub(r"(?i)^\s*(bachelor|master|doctor)\w*(\s+of)?\s*", "", title)
    t = re.sub(r"(?i)^\s*(b\.?\s?tech|b\.?\s?e\.?|b\.?s\.?|m\.?\s?eng\w*|m\.?s\.?|mem|ph\.?\s?d)\b",
               "", t)
    t = re.sub(r"(?i)^\s*(technology|science|arts|engineering management)\b", r"\1", t)
    return re.sub(r"[^a-z ]+", " ", t.lower()).strip()


def _title_ok(page_title: str, variants: set, kind: str = "work") -> bool:
    """Does the rendered title carry a sanctioned ROLE token?

    Graded on the role half only. A resume may generalise an internal system
    name in a team title ('Trade Confirmation Workflows' for 'Confirmation
    Workflow Manager'), which is a readability choice and not a claim change.
    Grading the full string would WARN on every one of those for something
    cosmetic, and a checker that flags legitimate content trains people to
    ignore its output.

    Education is graded differently and deliberately, because claiming a degree
    the record does not hold is the most serious defect this module can catch,
    and the two files spell the same degree with no shared prefix. The test is
    LEVEL plus SUBJECT: 'Bachelor of Technology in Electronics and
    Communication Engineering' satisfies 'B.Tech, Electronics and Communication
    Engineering' because both are bachelor-level and the subject words are all
    present; 'Master of ...' against the same record entry would not.
    """
    if kind == "education":
        page_level = _degree_level(page_title)
        for v in variants:
            want_level = _degree_level(v)
            if want_level is not None and page_level != want_level:
                continue
            field = _degree_field(v)
            words = [w for w in field.split() if len(w) > 2]
            low = re.sub(r"[^a-z ]+", " ", page_title.lower())
            if words and all(w in low for w in words):
                return True
        return False
    head = page_title.split(",")[0].strip().lower()
    return any(head == v.split(",")[0].strip().lower() for v in variants)


def diff(parsed: Parsed, rec: Parsed, titles: dict | None = None, *,
         strict_role_count: bool = False) -> list:
    """[(grade, message)] — 'error' contradicts the record, 'warn' loses to it.

    `strict_role_count` (Phase 5b) is for the MASTER RESUME and nothing else. A
    one-pager renders a SELECTION of roles, so a count mismatch there is a
    legitimate tailoring choice and stays a WARN — and it must, because
    `preflight.check_parse_back` calls this on every resume, every critic round,
    local and cloud, where an ERROR blocks the tailor. The flag changes the
    GRADE only, so it buys an honest severity in the report rather than opening
    a gate that was otherwise shut.
    """
    findings: list = []
    if not rec.entries:
        return findings
    by_name = {e.employer: e for e in rec.entries}
    titles = {} if titles is None else titles

    for ent in parsed.entries:
        name = ALIASES.get(ent.employer.upper())
        if name is None or name not in by_name:
            findings.append(("error", f"employer {ent.employer!r} appears on the page "
                                      f"but is absent from the record"))
            continue
        want = by_name[name]
        where = name
        # The employer-line span is OPTIONAL: Phase 2 landed the Citi aggregate
        # range on 2026-08-25 and 8 of 58 shipped resumes carry it. Present ->
        # must match. Absent -> no finding, or 50 resumes ERROR on day one.
        if ent.start is not None or ent.end is not None:
            _cmp_point(ent.start, want.start, where, "employer-line start", findings)
            _cmp_point(ent.end, want.end, where, "employer-line end", findings)

        if len(ent.roles) != len(want.roles):
            findings.append(("error" if strict_role_count else "warn",
                             f"{where}: the page renders {len(ent.roles)} "
                             f"role line(s), the record holds "
                             f"{len(want.roles)}"))
        for page_role, rec_role in zip(ent.roles, want.roles):
            rw = f"{where} / {page_role.title!r}"
            _cmp_point(page_role.start, rec_role.start, rw, "start", findings)
            _cmp_point(page_role.end, rec_role.end, rw, "end", findings)
            variants = titles.get(rec_role.title, {rec_role.title})
            if not _title_ok(page_role.title, variants, ent.kind):
                findings.append(("warn", f"{where}: title {page_role.title!r} is "
                                         f"outside the sanctioned variants for "
                                         f"{rec_role.title!r} ({sorted(variants)})"))
    seen = {ALIASES.get(e.employer.upper()) for e in parsed.entries}
    for name in by_name:
        if name not in seen:
            findings.append(("warn", f"{name} is in the record but no employer line "
                                     f"for it was parsed off the page"))
    return findings


def check(pdf_path, record: Path | None = None, *,
          strict_role_count: bool = False) -> list:
    """The whole check for one PDF: parse, load the record, diff."""
    rec = expected(record)
    if not rec.entries:
        return [("warn", n) for n in rec.notes]
    return diff(parse(pdf_path), rec, sanctioned_titles(record),
                strict_role_count=strict_role_count)


# --- CLI ---------------------------------------------------------------------

def _report(label: str, findings: list) -> int:
    errs = [m for g, m in findings if g == "error"]
    warns = [m for g, m in findings if g == "warn"]
    if errs or warns:
        print(label)
    for m in errs:
        print(f"  ERROR  {m}")
    for m in warns:
        print(f"  warn   {m}")
    return len(errs)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("pdf", nargs="?", help="a shipped resume PDF")
    ap.add_argument("--audit", action="store_true",
                    help="sweep applications/ (or the folders named)")
    ap.add_argument("folders", nargs="*", help="folders for --audit")
    args = ap.parse_args()

    if not args.audit:
        if not args.pdf:
            ap.error("give a PDF, or --audit")
        return 1 if _report(str(args.pdf), check(args.pdf)) else 0

    folders = [Path(f) for f in args.folders] or \
        [d for d in sorted(APPLICATIONS.iterdir()) if d.is_dir()]
    rec = expected()
    titles = sanctioned_titles()
    total_err = total_warn = scanned = 0
    skipped: list[str] = []
    for folder in folders:
        docx = shipped_resume_docx(folder)
        pdf = docx.with_suffix(".pdf") if docx else None
        if pdf is None or not pdf.exists():
            skipped.append(folder.name)
            continue
        scanned += 1
        findings = diff(parse(pdf), rec, titles)
        total_err += _report(f"{folder.name}", findings)
        total_warn += sum(1 for g, _ in findings if g == "warn")
    print(f"\nparse-back audit: {scanned} PDF(s), {total_err} error(s), "
          f"{total_warn} warning(s); {len(skipped)} folder(s) with no shipped PDF")
    if skipped:
        print("  no shipped PDF: " + ", ".join(skipped))
    # --audit is a REPORT and exits 0 even with findings. The corpus holds rows
    # nothing can ever fix -- one submitted resume (history, not a build
    # output) and three dead ones -- so a gating sweep would be red forever,
    # and a verification command whose red state is normal teaches people to
    # stop reading it (LEARNINGS 2026-09-01, the `check applications/*/` case).
    # The gate is preflight, per-resume, on the resume being built; single-PDF
    # mode still exits 1 so a script can act on one artifact.
    return 0


if __name__ == "__main__":
    sys.exit(main())
