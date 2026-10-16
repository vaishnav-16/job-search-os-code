"""Deterministic JD-keyword coverage: what the JD asks for, what the resume says.

    python pipeline/ats_score.py <jd.md> [resume.docx] [resume.pdf]
    python pipeline/ats_score.py <jd.md> --terms [--json sheet.json]

The JD becomes weighted terms (curated phrases plus mined unigrams, bigrams and
product names), each weighted by capped occurrences x a requirements-block boost
x a corpus-rarity multiplier. Every term is then MATCHED (a surface form appears
in the resume docx or the PDF text layer) or MISSING, and the last line is the
whole contract, top ten missing terms by weight:

    ATS: NN% (K of M weighted terms matched); missing: term1, term2, ...

Report only, exit 0 on any scorable input. Output is ASCII and ban-list clean
because these lines get pasted into `review.md`, which `lint_generated.py` blocks
on exactly those patterns; a JD term that is itself unusable resume vocabulary
is dropped from the sheet for the same reason.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from analyze_corpus import PHRASES, STOP, compile_term, normalize  # noqa: E402

ROOT = HERE.parent
FREQUENCIES = HERE / "keyword_corpus" / "frequencies.json"
HOOK_PATH = ROOT / ".claude" / "hooks" / "lint_generated.py"

# Row statuses. Plain strings so they survive the JSON sidecar round trip.
MATCHED = "matched"
MISSING = "missing"

# Skills & Interests label lines. preflight.LABEL_PREFIXES also carries
# "Courses:", which belongs to Education rather than the Skills block, so it is
# deliberately NOT listed here.
SKILLS_LABEL_PREFIXES = ("Technical:", "Product:", "Interests:")

# Which source answered the rarity question on the last load. Printed on every
# run: frequencies.json is committed, so `rarity  frequencies.json` is the
# expected line EVERYWHERE, laptop and cloud alike. Anything else means the file
# is missing or unreadable and every multiplier fell back to neutral-plus.
FREQ_SOURCE = "none"


# --- JD boilerplate ---------------------------------------------------------
# analyze_corpus.STOP is a grammar stop list. Job posts carry a second layer of
# noise that is grammatical but never a resume keyword. Extending here rather
# than there keeps the corpus ranking fixed.
JD_STOP = set("""
experience experiences ability abilities strong excellent great good better best
new help helping join joining looking seeking want wants need needed please apply
applying applicant applicants candidate candidates role roles job jobs position
positions company companies team teams teammates people person world plus bonus
nice must required requires preferred minimum basic qualifications responsibilities
responsibility about comfort comfortable one two three four five day days week
weeks month months year years time times way ways thing things employee employees
employer employment benefits salary compensation range equal opportunity
opportunities disability veteran gender race religion sexual orientation national
origin applicable law laws accommodation accommodations offer offers hiring
interview level levels senior junior staff principal director intern internship
full-time part-time remote hybrid office location locations here there also
however therefore across around every many much less least able
work working works worked life lives above below everyone anyone someone
everything anything something related relating alongside available availability
exposure encourage encouraged grow growing meet meets know knows okay walks
values place places following session sessions date dates start starts
summer winter spring autumn january february march april june july august
september october november december jan feb mar apr jun jul aug sep sept oct nov
dec pursuing pursue degree degrees field fields prior another other others while
clearly directly gain gaining hands-on real actual meaningful various different
through where using use used based form forms legal regardless brings bring
interested interest understand understanding develop academic eligible
eligibility guidelines information email address relocation targeting sponsor
sponsorship authorization authorized status resume application applications
apply-to submit submitted review reviewed consider considered
into within without upon during before after over under again still both either
neither whether because since though although unless until once twice ever never
always often sometimes usually really quite very same main core major minor
overall general specific particular current currently recent recently future
past present full part per like along together toward towards graduation
graduate learn learns learned
these those being
""".split())
# The last line is a 2026-08-22 calibration addition and is deliberately three
# words long. analyze_corpus.STOP carries `this`, `that` and `been` but not their
# siblings, so `these`, `those` and `being` were reaching the term sheet as if
# they were JD keywords -- a plain grammar gap, fixable without judgement.
# Everything else the sweep found is a CONTENT word in some other JD, and adding
# it here would be curated-stop-list whack-a-mole.


def _hook_rules():
    """The lint hook's RULES, loaded the same way preflight.py loads them.

    Single source of truth for what may not appear in a generated file. Used to
    drop unusable JD vocabulary from the term sheet. A missing hook degrades to
    a literal fallback rather than failing the run.
    """
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("lint_generated", HOOK_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return [rx for _, rx in mod.RULES]
    except Exception:
        return [re.compile(r"(?i)\b(spearheaded|leverag(?:ed|es|ing)|passionate"
                           r"|delve|utilized?|robust|seamless(?:ly)?|holistic"
                           r"|cutting-edge|tapestry|empower(?:ed|ing)?"
                           r"|foster(?:ed|ing)?)\b")]


_UNUSABLE = None


def unusable_vocabulary(term: str) -> bool:
    """True if this term could never be written into a generated file."""
    global _UNUSABLE
    if _UNUSABLE is None:
        _UNUSABLE = _hook_rules()
    return any(rx.search(term) for rx in _UNUSABLE)


# --- terms ------------------------------------------------------------------

def fold(term: str) -> str:
    """Comparison form: lowercased, separators flattened to single spaces."""
    return re.sub(r"[\s\-/_]+", " ", term.lower()).strip()


def _singular(key: str) -> str:
    words = key.split()
    if words and len(words[-1]) > 3 and words[-1].endswith("s") \
            and not words[-1].endswith("ss"):
        words[-1] = words[-1][:-1]
    return " ".join(words)


# Surface forms that are the same term. First element is the canonical spelling.
SYNONYM_GROUPS = [
    ("go-to-market", "go to market", "gtm"),
    ("a/b test", "ab test", "a/b testing", "ab testing", "split test"),
    ("roadmap", "road map"),
    ("ai", "artificial intelligence"),
    ("generative ai", "genai", "gen ai"),
    ("llm", "large language model", "large language models", "llms"),
    ("machine learning", "ml"),
    ("okr", "okrs", "objectives and key results"),
    ("kpi", "kpis"),
    ("prd", "product requirements document"),
    ("0 to 1", "zero to one", "0-1", "zero-to-one", "0-to-1"),
    ("developer experience", "devex"),
    ("proof of concept", "poc"),
    ("api", "apis"),
    ("sdk", "sdks"),
    ("pii", "personally identifiable information"),
]

_ALIAS: dict[str, str] = {}
for _group in SYNONYM_GROUPS:
    for _member in _group:
        _ALIAS[fold(_member)] = _group[0]


def canonical_term(term: str) -> str:
    """The one spelling a family of surface forms is reported under."""
    key = fold(term)
    return _ALIAS.get(key, term.strip().lower())


def variants_for(term: str) -> list[str]:
    """Every surface form of a term: its synonym group plus mechanical
    hyphen/space/slash respellings.

    Mechanical separator-removal forms are held to 4+ characters and must carry
    a letter, so "0-1" never spawns the variant "01" and starts matching dates.
    """
    canon = canonical_term(term)
    seeds = {term.strip().lower(), canon}
    for group in SYNONYM_GROUPS:
        if group[0] == canon:
            seeds.update(group)
    out: list[str] = []
    for s in seeds:
        forms = {s}
        if "-" in s:
            forms.add(s.replace("-", " "))
        if " " in s:
            forms.add(s.replace(" ", "-"))
        if "/" in s:
            forms.add(s.replace("/", " "))
        squashed = re.sub(r"[\s\-/]+", "", s)
        if len(squashed) >= 4 and re.search(r"[a-z]", squashed):
            forms.add(squashed)
        for f in forms:
            f = f.strip()
            if len(f) >= 2 and f not in out:
                out.append(f)
    out.sort(key=lambda x: (x != canon, x))
    return out


@dataclass
class Term:
    text: str                     # canonical spelling
    variants: list[str]
    weight: float
    source: str                   # "phrase-list" | "jd-mined"
    occurrences: int = 0
    occurrences_capped: int = 0
    rarity: float = 1.0
    boosted: bool = False
    _rx: list = field(default_factory=list, repr=False, compare=False)

    def patterns(self) -> list:
        if not self._rx:
            self._rx = [compile_term(v) for v in self.variants]
        return self._rx

    def hits(self, normalized: str) -> bool:
        """Any surface form present in already-normalize()d text. Matching is
        deliberately tolerant: a fused "crossfunctional" still counts, because
        some ATS parsers read it that way."""
        return any(rx.search(normalized) for rx in self.patterns())

    def keys(self) -> list[str]:
        ks = []
        for v in [self.text] + self.variants:
            for k in (fold(v), _singular(fold(v))):
                if k and k not in ks:
                    ks.append(k)
        return ks

    def as_dict(self) -> dict:
        return {"text": self.text, "variants": self.variants,
                "weight": round(self.weight, 3), "source": self.source,
                "occurrences": self.occurrences, "rarity": self.rarity,
                "boosted": self.boosted}


# --- JD parsing -------------------------------------------------------------

REQ_HEADING_RX = re.compile(
    r"(?i)\bqualifications\b|\brequirements\b|\bwhat\s+you'?\s?ll\s+need\b"
    r"|\bwho\s+you\s+are\b|\bwhat\s+we'?\s?re\s+looking\s+for\b"
    r"|\bwhat\s+you'?\s?ll\s+bring\b|\babout\s+you\b|\byour\s+background\b"
    r"|\byou\s+(?:have|bring|will\s+have)\b|\bskills\s+(?:and|&)\s+experience\b"
    r"|\bnice\s+to\s+have\b|\bwe'?\s?d\s+love\s+to\s+see\b|\bmust\s+haves?\b")

TPM_TITLE_RX = re.compile(r"(?i)\b(technical\s+)?program\s+manage(r|ment)\b")


def title_line(jd_text: str) -> str:
    for line in jd_text.splitlines():
        s = line.strip()
        if s.startswith("#"):
            return s.lstrip("#").strip()
        if s:
            return s
    return ""


def bucket_for(jd_text: str) -> str:
    """PM unless the title line names a program-manager seat. "product manager"
    cannot match TPM_TITLE_RX, so no extra guard is needed."""
    return "TPM" if TPM_TITLE_RX.search(title_line(jd_text)) else "PM"


def _is_heading(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    if re.match(r"#{1,6}\s", s):
        return True
    if re.fullmatch(r"\*\*[^*]{2,80}\*\*:?", s):
        return True
    return (len(s) <= 80 and s.endswith(":")
            and not s.startswith(("-", "*", ">", "|")))


BULLET_RX = re.compile(r"^\s*(?:[-•·]\s|\*\s|\d+[.)]\s)")


def jd_body(jd_text: str) -> str:
    """The employer's text, minus the metadata block this pipeline writes.

    Every jd.md the scraper emits opens with Source / URL / Location / Board
    updated lines and closes that block with a `---` fence. Mining across it
    turns the posting's city and capture date into JD keywords ("york", "jan"),
    which is our own bookkeeping scored as though the employer had asked for it.
    """
    lines = jd_text.splitlines()
    for i, ln in enumerate(lines[:25]):  # a fence further down is a real rule
        if re.fullmatch(r"-{3,}\s*", ln):
            rest = "\n".join(lines[i + 1:])
            return rest if rest.strip() else jd_text
    return jd_text


def _block_end(lines: list[str], start: int, stop: int) -> int:
    """Where a requirements block really ends.

    Not at the next heading: a JD's last requirements block often has no heading
    after it, so heading-to-heading swallows the closing "we know not everyone
    will meet all the above" prose, and every word in it takes the x2 boost. A
    requirements block is a bullet list, so it ends at the first non-blank,
    non-bullet line AFTER the bullets have started (lead-in prose is kept).
    """
    seen_bullet = False
    for i in range(start + 1, stop):
        s = lines[i].strip()
        if not s:
            continue
        if BULLET_RX.match(lines[i]):
            seen_bullet = True
            continue
        if seen_bullet:
            return i
    return stop


def requirement_text(jd_text: str) -> str:
    """Normalized text of the title line plus every requirements-type block.

    A term that appears here is what the employer says it needs, so it takes the
    x2 weight boost, and a term named here even ONCE is mined (the >=2
    occurrence floor is a noise filter for prose, and a minimum-qualifications
    bullet is not prose).
    """
    lines = jd_body(jd_text).splitlines()
    heads = [i for i, ln in enumerate(lines) if _is_heading(ln)]
    keep = [title_line(jd_text)]
    for pos, i in enumerate(heads):
        if REQ_HEADING_RX.search(lines[i]):
            hard_stop = heads[pos + 1] if pos + 1 < len(heads) else len(lines)
            keep.extend(lines[i:_block_end(lines, i, hard_stop)])
    return normalize("\n".join(keep))


def company_names(jd_text: str) -> set:
    """The employer's own name, which is never a resume keyword.

    Read from the two places this repo's jd.md format puts it: the trailing
    segment of the `# <Role> - <Company>` title line, and the backticked board
    slug on the `**Source:**` line. Missing either is harmless.
    """
    out = set()
    title = title_line(jd_text)
    # Dash separators only. " at " would read "Product Manager at Scale" as the
    # company "Scale" and delete a real keyword from the sheet.
    for sep in (" - ", " – ", " — "):
        if sep in title:
            out.add(fold(title.rsplit(sep, 1)[1]))
    m = re.search(r"(?im)^\*\*Source:\*\*.*?board slug\s*`([^`]+)`", jd_text)
    if m:
        out.add(fold(m.group(1)))
    return {c for c in out if 2 <= len(c) <= 40}


def _token_spans(norm: str) -> list[tuple[str, int, int]]:
    return [(m.group(0), m.start(), m.end())
            for m in re.finditer(r"[a-z][a-z\-+/]{2,}", norm)]


def _mine(norm: str, req_norm: str) -> dict[str, int]:
    """Candidate unigrams/bigrams with their occurrence counts.

    Kept when they occur >=2x in the JD, or at least once inside a
    requirements block. Stopwords are dropped on both layers.
    """
    toks = _token_spans(norm)
    uni: dict[str, int] = {}
    bi: dict[str, int] = {}
    for i, (w, s, e) in enumerate(toks):
        if w not in STOP and w not in JD_STOP:
            uni[w] = uni.get(w, 0) + 1
        if i + 1 < len(toks):
            w2, s2, _ = toks[i + 1]
            if s2 == e + 1 and norm[e] == " " and w not in STOP and w2 not in STOP \
                    and w not in JD_STOP and w2 not in JD_STOP:
                phrase = f"{w} {w2}"
                bi[phrase] = bi.get(phrase, 0) + 1

    def keep(term: str, count: int) -> bool:
        # 4-character floor on mined prose unigrams. Below that a word is either
        # an acronym (mined separately, where all-caps is the evidence) or
        # filler; "jan" and "may" are not JD keywords.
        if " " not in term and len(term) < 4:
            return False
        return count >= 2 or bool(compile_term(term).search(req_norm))

    cands = {t: c for t, c in list(bi.items()) + list(uni.items()) if keep(t, c)}
    # A unigram that only ever appears inside a kept bigram is that bigram.
    for b, bc in bi.items():
        if b not in cands:
            continue
        for w in b.split():
            if cands.get(w) == bc:
                cands.pop(w, None)
    return cands


def _capitalized_names(jd_text: str, norm: str) -> dict[str, int]:
    """Product names the prose layer misses: internal-capital tokens (GitHub,
    PostgreSQL) and 2-6 character all-caps acronyms.

    Held to the same >=2 occurrence floor as mined prose. Known limitation: a
    single-mention product name does not surface, and an ordinary capitalized
    company name (Datadog, Stripe) is not detected at all, since separating one
    from a sentence-initial word without a gazetteer produces more noise than
    signal.
    """
    out: dict[str, int] = {}
    stop_caps = {"AND", "OR", "THE", "US", "USA", "NY", "CA", "EEO", "PM", "TPM",
                 "II", "III", "IV", "AM", "OK", "ID", "HR", "IT", "AI"}
    cands = set(re.findall(r"\b[a-z]+[A-Z][A-Za-z0-9]*\b", jd_text))
    cands |= {c for c in re.findall(r"\b[A-Z][a-z0-9]*[A-Z][A-Za-z0-9]*\b", jd_text)
              if c.upper() not in stop_caps}
    cands |= {c for c in re.findall(r"\b[A-Z][A-Z0-9]{1,5}\b", jd_text)
              if c not in stop_caps}
    for c in cands:
        # Same stop gate _mine applies. Without it this layer let ALL-CAPS
        # section headings through as if they were acronyms -- "ABOUT US",
        # "JOB RESPONSIBILITIES" put about/job/team into the term sheet, where
        # the prose miner would have rejected every one of them.
        if c.lower() in STOP or c.lower() in JD_STOP:
            continue
        n = len(compile_term(c.lower()).findall(norm))
        if n >= 2:
            out[c.lower()] = n
    return out


def _merged_spans(term: Term, norm: str) -> int:
    spans: list[tuple[int, int]] = []
    for rx in term.patterns():
        spans.extend((m.start(), m.end()) for m in rx.finditer(norm))
    if not spans:
        return 0
    spans.sort()
    count, end = 0, -1
    for s, e in spans:
        if s >= end:
            count += 1
            end = e
        else:
            end = max(end, e)
    return count


# --- corpus rarity ----------------------------------------------------------

def _index(pairs) -> dict[str, float]:
    idx: dict[str, float] = {}
    for term, pct in pairs:
        for key in (term.lower().strip(), fold(term), _singular(fold(term))):
            if key:
                idx[key] = max(idx.get(key, 0.0), float(pct))
    return idx


def load_corpus_frequencies() -> dict[str, dict[str, float]]:
    """Doc-frequency percentages per bucket from `keyword_corpus/frequencies.json`.

    Returns {"PM": {term: pct}, "TPM": {...}} and records the answering source in
    FREQ_SOURCE. A missing or unreadable file degrades to "none" and an empty
    map rather than raising: a scorer that dies would take the tailor loop with
    it. With no frequencies, every multiplier falls back to x1.5.
    """
    global FREQ_SOURCE
    if FREQUENCIES.exists():
        try:
            raw = json.loads(FREQUENCIES.read_text(encoding="utf-8"))
            out = {}
            for bucket in ("PM", "TPM"):
                pairs = [(t, pct) for t, _, pct in raw.get(bucket, {}).get("phrases", [])]
                pairs += [(t, pct) for t, _, pct in raw.get(bucket, {}).get("unigrams", [])]
                out[bucket] = _index(pairs)
            if out.get("PM") or out.get("TPM"):
                FREQ_SOURCE = "frequencies.json"
                return out
        except Exception:
            pass
    FREQ_SOURCE = "none"
    return {}


def corpus_frequency(keys, freq, bucket: str):
    idx = (freq or {}).get(bucket, {})
    hits = [idx[k] for k in keys if k in idx]
    return max(hits) if hits else None


def rarity_multiplier(keys, freq, bucket: str) -> float:
    """Common market vocabulary is cheap; rare vocabulary is what a match is
    worth. >=75% of the corpus damps to x0.5, 25-74% is neutral, below 25% or
    absent from the corpus is x1.5."""
    pct = corpus_frequency(keys, freq, bucket)
    if pct is None:
        return 1.5
    if pct >= 75:
        return 0.5
    return 1.0 if pct >= 25 else 1.5


def mined_rarity(keys, freq, bucket: str) -> float:
    """Rarity for a JD-MINED term. One-directional on purpose.

    A mined term is market vocabulary the curated PHRASES list never ranked, so
    it enters neutral -- it must NEVER inherit the x1.5 that `rarity_multiplier`
    gives a corpus-silent term, because for mined text "the corpus has never
    heard of it" means boilerplate at least as often as it means a rare domain
    keyword, and paying 1.5 for both would price the junk above the real thing.

    What the corpus CAN say is the other direction: a word carried by three
    quarters of the JDs in its bucket is generic market English ("build" 93%,
    "products" 91%), and that damps exactly the same way a common phrase-list
    term does. Same 75% band, deliberately -- a second constant would be a
    second thing to calibrate.
    """
    pct = corpus_frequency(keys, freq, bucket)
    return 0.5 if pct is not None and pct >= 75 else 1.0


# --- extraction -------------------------------------------------------------

def extract_jd_terms(jd_text: str, bucket: str, freq=None,
                     dropped: list = None) -> list[Term]:
    """Weighted terms for one JD.

    `dropped`, if given, collects the terms removed as unusable resume
    vocabulary. Pass it and report it: dropping a term shrinks the denominator,
    which moves the score in the flattering direction, and it was the one such
    path in this module that left no trace in the output.
    """
    body = jd_body(jd_text)
    norm = normalize(title_line(jd_text) + "\n" + body)
    req = requirement_text(jd_text)
    freq = load_corpus_frequencies() if freq is None else freq
    drop = company_names(jd_text)
    unusable: dict[str, None] = {}

    seen: dict[str, Term] = {}

    def add(surface: str, source: str) -> None:
        canon = canonical_term(surface)
        # Keyed on the folded form, so PHRASES' deliberate pairs ("cross
        # functional" alongside "cross-functional") collapse into one term
        # instead of being scored twice.
        key = fold(canon)
        if key in seen or key in drop:
            return
        if unusable_vocabulary(canon):
            # Only if it is actually in this JD, so the note counts real
            # removals rather than every ban-list word in the phrase list.
            if _merged_spans(Term(canon, variants_for(canon), 0.0, source), norm):
                unusable[canon] = None
            return
        t = Term(canon, variants_for(canon), 0.0, source)
        n = _merged_spans(t, norm)
        if n == 0:
            return
        t.occurrences = n
        t.occurrences_capped = min(n, 3)
        t.boosted = t.hits(req)
        t.rarity = (mined_rarity(t.keys(), freq, bucket) if source == "jd-mined"
                    else rarity_multiplier(t.keys(), freq, bucket))
        t.weight = t.occurrences_capped * (2.0 if t.boosted else 1.0) * t.rarity
        seen[key] = t

    for p in PHRASES:
        add(p.strip(), "phrase-list")
    for term in sorted(_mine(norm, req)):
        add(term, "jd-mined")
    for name in sorted(_capitalized_names(body, norm)):
        add(name, "jd-mined")

    if dropped is not None:
        dropped.extend(sorted(unusable))
    return sorted(seen.values(), key=lambda t: (-t.weight, t.text))


def dropped_note(dropped) -> str:
    """The note that makes the unusable-vocabulary drop visible, or "".

    Terms the resume may never write are not coverage targets, so they leave the
    term sheet. That shrinks the denominator, so it has to be said out loud.
    """
    if not dropped:
        return ""
    n = len(dropped)
    return (f"{n} term{'s' if n != 1 else ''} dropped as unusable resume "
            f"vocabulary (voice.md ban list, so not a coverage target): "
            + ", ".join(sorted(dropped)))


# --- resume text ------------------------------------------------------------

@dataclass
class ResumeText:
    docx: str = ""
    pdf: str = ""
    skills: str = ""
    notes: list = field(default_factory=list)

    @property
    def body(self) -> str:
        """The docx minus its Skills label lines. Skills-only detection is a
        docx-side judgement: the PDF text layer has no paragraph identity."""
        skip = {ln.strip() for ln in self.skills.splitlines() if ln.strip()}
        return "\n".join(ln for ln in self.docx.splitlines()
                         if ln.strip() and ln.strip() not in skip)


def resume_texts(docx_path, pdf_path=None) -> ResumeText:
    """Flattened docx text, its Skills label lines, and the PDF text layer.

    Both legs degrade rather than raise: a scorer that dies on a locked or
    corrupt PDF would take the tailor loop down with it.
    """
    rt = ResumeText()
    if docx_path:
        try:
            from docx_engine import Doc
            doc = Doc.load(docx_path)
            paras = [p.text for p in doc.paragraphs()]
            rt.docx = "\n".join(paras)
            rt.skills = "\n".join(p for p in paras
                                  if p.strip().startswith(SKILLS_LABEL_PREFIXES))
        except Exception as exc:
            rt.notes.append(f"docx unreadable ({exc}); docx leg skipped")
    if pdf_path:
        try:
            import compare_renderers
            rt.pdf = "\n".join(compare_renderers.lines_of(Path(pdf_path)))
        except Exception as exc:
            rt.notes.append(f"PDF unreadable ({exc}); PDF text layer leg skipped")
    return rt


# --- scoring ----------------------------------------------------------------

@dataclass
class Row:
    term: str
    weight: float
    status: str                   # MATCHED | MISSING
    where: str                    # "docx" | "pdf" | "both" | "-"


@dataclass
class Report:
    pct: float = 0.0
    matched_n: int = 0
    total_n: int = 0
    missing: list = field(default_factory=list)   # term texts, heaviest first
    matched_weight: float = 0.0
    total_weight: float = 0.0
    skills_only: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    notes: list = field(default_factory=list)


def score(terms, resume: ResumeText) -> Report:
    """Coverage of `terms` by `resume`.

    A term is matched when any of its surface forms appears in the docx text or
    in the PDF text layer; `where` records which side answered, so a term that
    survives the docx but dies in PDF extraction is visible as `docx` rather
    than `both`. The ATS parses the PDF text layer, not the docx.
    """
    docx_n = normalize(resume.docx)
    pdf_n = normalize(resume.pdf) if resume.pdf.strip() else ""
    body_n = normalize(resume.body)
    skills_n = normalize(resume.skills) if resume.skills.strip() else ""

    rep = Report(notes=list(resume.notes))
    missing: list[Term] = []
    for t in terms:
        d = t.hits(docx_n)
        p = t.hits(pdf_n) if pdf_n else False
        rep.total_n += 1
        rep.total_weight += t.weight
        if d or p:
            rep.matched_n += 1
            rep.matched_weight += t.weight
            where = "both" if (d and p) else ("docx" if d else "pdf")
            rep.rows.append(Row(t.text, t.weight, MATCHED, where))
            if d and skills_n and t.hits(skills_n) and not t.hits(body_n):
                rep.skills_only.append(t.text)
        else:
            missing.append(t)
            rep.rows.append(Row(t.text, t.weight, MISSING, "-"))

    rep.rows.sort(key=lambda r: (-r.weight, r.term))
    missing.sort(key=lambda t: (-t.weight, t.text))
    rep.missing = [t.text for t in missing]
    rep.pct = (100.0 * rep.matched_weight / rep.total_weight
               if rep.total_weight else 0.0)
    return rep


# --- output -----------------------------------------------------------------

_ASCII_MAP = {"—": ",", "–": ",", "≤": "<=", "≥": ">=", "→": " to ", "×": "x",
              "·": ";", "’": "'", "‘": "'", "“": '"', "”": '"', "§": "sec ",
              "≠": "!=", "…": "...", "•": "-", " ": " "}


def _ascii(s: str) -> str:
    """ASCII and ban-list clean, spacing untouched.

    Two jobs in one pass. The dashes have to go because these lines get pasted
    into `score.md` and `lint_generated.py` hard-blocks an em or en dash there.
    The rest has to go because a Windows console is cp1252 and a stray "<=" used
    to end the run in a UnicodeEncodeError halfway through the table, which is a
    report tool failing at reporting.
    """
    for bad, good in _ASCII_MAP.items():
        s = s.replace(bad, good)
    return s.encode("ascii", "replace").decode("ascii")


def _sanitize(s: str) -> str:
    """_ascii plus whitespace collapsed. For cell content, never whole lines:
    collapsing a table row would eat its column padding."""
    return re.sub(r"\s+", " ", _ascii(s)).strip()


def out(line: str = "") -> None:
    """Every line this tool prints goes through the ASCII gate."""
    print(_ascii(line))


def ats_line(rep: Report) -> str:
    """The one line every caller parses. Top ten missing terms by weight, no
    trailing period; the `; missing:` tail is omitted when nothing is missing."""
    line = (f"ATS: {round(rep.pct)}% ({rep.matched_n} of {rep.total_n} "
            f"weighted terms matched)")
    if rep.missing:
        line += "; missing: " + ", ".join(rep.missing[:10])
    return line


def terms_line(terms, bucket: str) -> str:
    return (f"TERMS: {len(terms)} terms / bucket {bucket} / "
            f"weight {round(sum(t.weight for t in terms))}")


def _table(rows) -> list[str]:
    head = ("term", "weight", "status", "where")
    data = [head] + [(r.term, f"{r.weight:.1f}", r.status, r.where) for r in rows]
    widths = [max(len(d[i]) for d in data) for i in range(len(head))]
    lines = []
    for i, d in enumerate(data):
        lines.append("  ".join(d[j].ljust(widths[j])
                               for j in range(len(head))).rstrip())
        if i == 0:
            lines.append("  ".join("-" * w for w in widths))
    return lines


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _write_sheet(path, sheet) -> None:
    if not path:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sheet, indent=1), encoding="utf-8")
    except OSError as exc:
        out(f"note   could not write {path}: {exc}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("jd")
    ap.add_argument("docx", nargs="?")
    ap.add_argument("pdf", nargs="?")
    ap.add_argument("--terms", action="store_true",
                    help="print the weighted JD term sheet only")
    ap.add_argument("--bucket", choices=("PM", "TPM"), default=None,
                    help="corpus bucket for the rarity multiplier "
                         "(default: from the JD title line)")
    ap.add_argument("--json", dest="json_path",
                    help="read/write the term sheet here; a bucket already "
                         "recorded in it is reused")
    args = ap.parse_args()

    jd_path = Path(args.jd)
    try:
        jd_text = _read(jd_path)
    except OSError as exc:
        out(f"cannot read JD {jd_path}: {exc}")
        return 2

    sheet_path = Path(args.json_path) if args.json_path else None
    prior = {}
    if sheet_path and sheet_path.exists():
        try:
            prior = json.loads(sheet_path.read_text(encoding="utf-8"))
        except Exception:
            prior = {}
    # A bucket read back from a sheet is untrusted input: it can be hand-edited
    # or written by another tool, and an unrecognized value silently sent every
    # term down the "absent from the corpus" branch at x1.5 (a 24% denominator
    # shift, measured). Validate, then fall back to the title regex and say so.
    early_notes = []
    prior_bucket = prior.get("bucket")
    if prior_bucket is not None and prior_bucket not in ("PM", "TPM"):
        early_notes.append(f"term sheet {sheet_path} carries an unrecognized "
                           f"bucket {prior_bucket!r} (expected PM or TPM): "
                           "ignored, falling back to the JD title line")
        prior_bucket = None
    bucket = args.bucket or prior_bucket or bucket_for(jd_text)
    origin = ("--bucket" if args.bucket else
              "term sheet" if prior_bucket else "JD title line")

    freq = load_corpus_frequencies()
    dropped: list = []
    terms = extract_jd_terms(jd_text, bucket, freq, dropped)

    notes = list(early_notes)
    if dropped_note(dropped):
        notes.append(dropped_note(dropped))
    if FREQ_SOURCE == "none":
        notes.append("no corpus frequencies found: every rarity multiplier "
                     "fell back to x1.5")

    out(f"JD      {jd_path}")
    out(f"title   {title_line(jd_text)}")
    out(f"bucket  {bucket} (from {origin})")
    out(f"rarity  {FREQ_SOURCE}")
    out(f"terms   {len(terms)} weighted terms")

    sheet = {"jd": str(jd_path), "title": title_line(jd_text), "bucket": bucket,
             "rarity_source": FREQ_SOURCE,
             "terms": [t.as_dict() for t in terms]}

    if args.terms:
        out()
        for t in terms:
            out(f"  {t.weight:6.1f}  {t.text}  [{t.source}"
                + (", requirements" if t.boosted else "") + "]")
        for n in notes:
            out(f"note   {n}")
        out()
        out(terms_line(terms, bucket))
        _write_sheet(sheet_path, sheet)
        return 0

    if not args.docx:
        for n in notes + ["no resume given: pass a .docx (and its .pdf) for a "
                          "coverage score, or use --terms"]:
            out(f"note   {n}")
        out()
        out(terms_line(terms, bucket))
        _write_sheet(sheet_path, sheet)
        return 0

    # A NAMED but unreadable docx is unusable input, not a gap to report around.
    # Scoring it would print 0% with every term missing, which reads like a
    # catastrophically bad resume rather than like a broken path, and a caller
    # parsing the last line cannot tell the two apart. Exit 2 with no ATS line.
    # Omitting the docx entirely is a different request and still returns the
    # TERMS line at exit 0.
    docx_path = Path(args.docx)
    pdf_path = Path(args.pdf) if args.pdf else docx_path.with_suffix(".pdf")
    if not docx_path.exists():
        out(f"cannot read resume {docx_path}: no such file. Omit the argument "
            "for a term sheet, or pass a readable .docx")
        return 2
    resume = resume_texts(docx_path, pdf_path if pdf_path.exists() else None)
    if not resume.docx.strip():
        for n in resume.notes:
            out(f"note   {n}")
        out(f"cannot read resume {docx_path}: no text extracted. Scoring it "
            "would report an empty resume rather than an unreadable file")
        return 2

    # A missing PDF is a REPORTED gap, not a failure: the docx still scores. The
    # ATS parses the PDF text layer, not the docx, so with no PDF a term that
    # survives in the docx but dies in extraction reads here as clean coverage.
    if not pdf_path.exists():
        notes.append(f"no PDF at {pdf_path}: the ATS reads the PDF text layer, "
                     "not the docx, so this score is the optimistic side of "
                     "that gap")

    rep = score(terms, resume)
    out()
    for line in _table(rep.rows):
        out(line)
    out()
    if rep.skills_only:
        notes.append("matched only on a Skills label line: "
                     + ", ".join(sorted(rep.skills_only)))
    for n in notes + rep.notes:
        out(f"note   {_sanitize(n)}")
    if notes or rep.notes:
        out()
    sheet["score"] = {"pct": round(rep.pct, 1), "matched": rep.matched_n,
                      "total": rep.total_n, "missing": rep.missing,
                      "rows": [r.__dict__ for r in rep.rows]}
    _write_sheet(sheet_path, sheet)
    out(ats_line(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
