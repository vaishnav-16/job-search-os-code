"""The requirement screen: what a 2026 AI screener checks, emulated per job.

    python pipeline/requirements.py extract <folder>            # writes <folder>/requirements.json
    python pipeline/requirements.py plan <folder>               # tailor's view, before drafting
    python pipeline/requirements.py packet <folder> [--docx D] [--pdf P]   # the screener's input
    python pipeline/requirements.py grade <folder> --round N [--docx D] [--pdf P] [--out F]

Why this shape (research 2026-10-14, docs/ats-screen.md): the screeners that
read a PM resume today (Workday HiredScore Fit & Gap, Greenhouse Talent
Matching, Ashby AI-assisted review, LinkedIn Hiring Assistant) do not count
keywords. They split the posting's QUALIFICATIONS into basic and preferred
items, judge each one met or not against the parsed resume with cited
evidence, and show the recruiter a coarse tier. HiredScore's documented
grades: A = all basic and most preferred, B = all basic, C = most basic,
D = fewer. Recruiters also run literal keyword and title searches, so
findability is reported beside the grade, never blended into it.

Division of labour:
- `extract` (no LLM) pulls the qualification bullets out of jd.md and tags each
  basic or preferred. The list is fixed for the whole run, so the tailor cannot
  pick an easier target and every round is graded against the same items.
- The `ats-scanner` agent (an LLM) judges each item and quotes the resume line
  that proves it, into `screen-r<N>.json`.
- `grade` (no LLM) checks every quote is really on the page (an unverifiable
  quote earns nothing), does the arithmetic, and writes the review section and
  `screen.json`, which the dashboard reads.

Report only: nothing in the pipeline gates on the grade.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import ats_score as A  # noqa: E402

ROOT = HERE.parent
RECORD = ROOT / "profile" / "knowledge" / "record.json"
REQUIREMENTS = "requirements.json"
SCREEN = "screen.json"
VERSION = 1

BASIC, PREFERRED, KNOCKOUT = "basic", "preferred", "knockout"
MET, PARTIAL, NOT_MET = "met", "partial", "not_met"
CREDIT = {MET: 1.0, PARTIAL: 0.5, NOT_MET: 0.0}
KIND_WEIGHT = {BASIC: 2.0, PREFERRED: 1.0}

MAX_BASIC, MAX_PREFERRED = 20, 10
MIN_ITEMS = 2          # fewer than this and the scanner derives the list instead (Capital One posts 1 basic + 1-2 preferred)
FINDABILITY_TERMS = 15
MIN_QUOTE_WORDS = 3    # "SQL" alone proves nothing; the agent is told four

# --- extraction ---------------------------------------------------------------
#
# Calibrated against several hundred jd.md files under applications/ (2026-10-14). The
# shapes that matter, by how often they occur:
#   - markdown or bold headings ("## Qualifications", "**Basic Qualifications:**",
#     "****Requirements****");
#   - plain-text headings with no colon or markup ("Skills", "Benefits"), which
#     is how LinkedIn and Jobright flatten a posting, so a short line is a heading
#     only when the WHOLE line is a known section name;
#   - basic and preferred split by heading (Amazon, Capital One, Google) or
#     marked inline ("... preferred", "... is a plus"), as EA and many others do;
#   - "OR" lines between alternatives and "+" sub-bullets (Capital One);
#   - no headings at all (about one in twelve), which the scanner derives.

_TIDY = str.maketrans({"\u2019": "'", "\u2018": "'", "\u201c": '"', "\u201d": '"',
                       "\u00a0": " ", "\u200b": "", "\ufeff": ""})

QUAL_HEAD = (
    r"(?:(?:basic|minimum|min|preferred|required|desired|additional|key|bonus|nice[\s-]to[\s-]haves?"
    r"|must[\s-]haves?|job|technical|professional|core|your|the|our|other|ideal|candidate|"
    r"qualifications?\s*(?:and|&)|education\s*(?:and|&)|skills\s*(?:and|&))\s+)*"
    r"(?:qualifications?|requirements?|skills?(?:\s*(?:and|&|,)\s*(?:experience|qualifications|abilities"
    r"|knowledge|competencies))*|experience(?:\s*(?:and|&)\s*(?:skills|qualifications|education))?"
    r"|education(?:\s*(?:and|&)\s*(?:experience|training))?|competencies|knowledge(?:,?\s*skills"
    r"(?:,?\s*(?:and|&)\s*abilities)?)?|nice[\s-]to[\s-]haves?|must[\s-]haves?|bonus(?:\s+points)?"
    r"|certifications?|licenses?(?:\s+or\s+certifications)?)"
    r"|what\s+(?:you'?ll|you\s+will|you|we)\s+(?:need|bring|have|look\s+for|expect)(?:\s+to\s+succeed)?"
    r"(?:\s+to\s+(?:the\s+)?(?:role|team|table))?"
    r"|what\s+we'?r?e?\s*(?:are\s+)?looking\s+for(?:\s+in\s+you)?|who\s+we'?r?e?\s*(?:are\s+)?looking\s+for"
    r"|who\s+you\s+are|about\s+you|you\s+(?:have|bring|will\s+have|might\s+(?:also\s+)?have|should\s+have)"
    r"|you\s+(?:might|may|will|could)\s+(?:thrive|excel|be\s+a\s+(?:great|good|strong)\s+fit|succeed)\b.*"
    r"|(?:it'?s\s+a\s+)?(?:bonus|plus)\s+if\s+you\b.*|(?:we'?d|we\s+would)\s+love\s+(?:to\s+see|it\s+if)\b.*"
    r"|ideal\s+candidate\b.*|is\s+this\s+(?:role|you)\b.*|the\s+ideal\s+candidate\b.*"
    r"|what\s+it\s+takes|to\s+succeed\s+in\s+this\s+role.*|requirements?\s+and\s+qualifications"
    r"|(?:basic|minimum|preferred|required)\s+(?:qualifications?|requirements?)\b.*"
    r"|required|preferred|desired|optional|nice[\s-]to[\s-]haves?"
    r"|you'?ll\s+need(?:\s+to\s+have)?|what\s+you'?ll\s+need\s+to\s+be\s+successful"
    r"|(?:talent|skills|competencies|experience)\b.{0,30}\bfor\s+success"
    r"|(?:core|critical|key|required)\s+competencies\b.*|skills\s*/\s*competencies"
    r"|credentials\b.*|to\s+be\s+successful\b.*|to\s+thrive\s+in\s+this\s+role\b.*"
    r"|(?:we'?re|we\s+are)\s+seeking\s+the\s+following|we'?d\s+love\s+to\s+hear\s+from\s+you\b.*"
    r"|while\s+it'?s\s+not\s+required\b.*|experience,\s+education\b.*"
    r"|(?:work\s+)?experience\s+(?:and|&)\s+education|education\s+(?:and|&)\s+experience\s+required"
    r"|you\s+bring\b.*|what\s+we'?re\s+looking\s+for\b.*|the\s+experience"
    r"|required\s+(?:skills|experience)\b.*|what\s+we\s+need\s+to\s+see"
    r"|(?:what\s+)?we\s+require\b.*"
    r"|ways\s+to\s+stand\s+out\b.*|(?:you\s+have\s+an\s+)?advantage\s+if\b.*|even\s+better\s+if\b.*"
    r"|what\s+makes\s+you\s+(?:a\s+)?(?:great|good|strong|ideal|dream)\s+(?:fit|candidate)\b.*")
OTHER_HEAD = (
    r"(?:key\s+|your\s+|essential\s+|core\s+|primary\s+|the\s+|job\s+|role\s+)*"
    r"(?:responsibilities|duties|benefits|perks|compensation|summary|overview|description|details"
    r"|location|locations|eeo(?:\s+statement)?|pay(?:\s+range.*)?|salary.*|position\s+type|travel.*"
    r"|team|role|opportunity|mission|impact|culture|values)"
    r"|what\s+(?:you'?ll|you\s+will|you)\s+(?:do|be\s+doing|work\s+on|own|achieve|learn)\b.*"
    r"|in\s+this\s+role\b.*|about\s+.{2,45}|who\s+we\s+are|why\s+.{2,40}|our\s+.{2,30}|how\s+we\b.*"
    r"|what\s+success\s+looks\s+like.*|what\s+we\s+offer|the\s+role\s+will.*|ready\s+to\s+.*"
    r"|equal\s+(?:employment\s+)?opportunity.*|state[\s-]specific\s+notices|physical.*demands.*"
    r"|where\s+you'?ll\s+work|job\s+title|reporting\s+line|work\s+model|workplace\s+type"
    r"|benefits\s+(?:and|&)\s+perks|the\s+responsibilities\b.*")
QUAL_HEAD_RX = re.compile(rf"(?i)^(?:{QUAL_HEAD})$")
OTHER_HEAD_RX = re.compile(rf"(?i)^(?:{OTHER_HEAD})$")
# Section names that look like qualifications but are not the candidate's to
# meet with a resume line (they are screening questions or working conditions).
NOT_QUAL_RX = re.compile(
    r"(?i)\bphysical\b|\btravel\b|\bwork(?:ing)?\s+(?:environment|conditions|schedule)\b"
    r"|\bschedule\b|\bbenefits?\b|\bcompensation\b|\bpay\b|\bsalary\b|\bresponsibilit"
    r"|\bperks?\b|\binterview|\bin\s+return\b|\bwellness\b|\blife\s+at\b|\bvalues\b"
    r"|\boutcomes\b|\bsuccess\s+(?:measures|metrics|looks)\b|\bautomated\s+processing\b"
    r"|\bwe'?ll\s+(?:bring|give|offer)\b|\bwhat\s+you'?ll\s+get\b|\bwhat\s+we\s+offer\b"
    r"|\bin-office\b|\boffice\s+requirement|\bjob\s+category\b|\bcommitment\b")
# "Requirements: 3+ years of ..." on one line: a heading with its first item.
# Only the strong section names; "Certifications: CSM" inside a list is an item.
INLINE_HEAD_RX = re.compile(
    r"(?i)^((?:basic|minimum|required|preferred|desired|key)?\s*(?:qualifications?|requirements?)"
    r"|what\s+you'?ll\s+need|what\s+we'?re\s+looking\s+for|about\s+you|you\s+have)"
    r"\s*:\s*(\S.{8,})$")

# A heading that marks the whole block preferred.
PREFERRED_HEADING_RX = re.compile(
    r"(?i)\bpreferred\b|\bnice[\s-]to[\s-]haves?\b|\bbonus\b|\bplus\b|\bwe'?\s?d\s+love\b"
    r"|\bwould\s+love\b|\bideally\b|\bdesired\b|\bstand\s+out\b|\bgood\s+to\s+have\b"
    r"|\badditional\b|\bmight\s+(?:also\s+)?have\b|\badvantage\b|\beven\s+better\b"
    r"|\bdifferentiate\b|\bsets?\s+you\s+apart\b|\bedge\b|\brecommended\b|\bdesirable\b|\bnice\b"
    r"|\bnot\s+required\b|\bsupplementary\b")
# A bullet that marks itself preferred inside a basic block ("... is a plus").
PREFERRED_INLINE_RX = re.compile(
    r"(?i)\bpreferred\b|\bpreferably\b|\b(?:is\s+)?a\s+(?:big\s+|strong\s+)?plus\b|\bbonus\b"
    r"|\bnice[\s-]to[\s-]have\b|\bideally\b|\bdesired\b|\bdesirable\b|\bhighly\s+valued\b")
REQUIRED_INLINE_RX = re.compile(r"(?i)\brequired\b|\bmust\b|\bminimum\b")
# "3+ years of PM, preferably in payments": the preference narrows a required
# item, it does not make the item optional (86 of 6,484 items read preferred
# that way, 2026-10-14). Only the qualifier clause is a preference.
QUALIFIER_RX = re.compile(
    r"(?i)(?:,|\(|\b(?:and|but))\s*(?:preferably|ideally|preferred)\b"
    r"|\b(?:preferably|ideally|preferred)\s+(?:in|with|at|from|on|for|by|involving|someone)\b")
# Working conditions a screening question answers, not a resume line: "ability
# to travel up to 25%", "willing to relocate". Knockouts, out of the grade. A
# domain ("travel & hospitality") is not one.
CONDITION_RX = re.compile(
    r"(?i)\b(?:ability|able|willing(?:ness)?|interest)\s+(?:and\s+\w+\s+)?to\s+(?:travel|relocate)\b"
    r"|\btravel\s+(?:up\s+to|may\s+be|is\s+(?:required|expected)|required|periodically|\d)"
    r"|^\W*(?:occasional|some|periodic|frequent|limited)\s+travel\b|\bwilling\s+to\s+relocate\b")

BULLET_LINE_RX = re.compile(r"^\s*(?:[-*•·+◦▪●○–]\s+|\d{1,2}[.)]\s+|[a-z][.)]\s+|o\s+(?=[A-Z]))")
ITALIC_NOTE_RX = re.compile(r"^\s*[*_](?![*_\s]).+[^*_\s][*_]\s*$")
JOINER_RX = re.compile(r"(?i)^\W*(?:or|and|and/or)\W*$")
# Closing prose a block must not swallow when it has no heading after it.
BOILERPLATE_RX = re.compile(
    r"(?i)don'?t\s+(?:meet|check)|not\s+(?:everyone|all)\b|equal\s+opportunity|we\s+encourage"
    r"|apply\s+(?:anyway|even)|reasonable\s+accommodation|regardless\s+of|pay\s+range|salary"
    r"|benefits|we\s+offer|base\s+pay|compensation|this\s+(?:role|position)\s+is\s+(?:not\s+)?eligible"
    r"|e-?verify|privacy|background\s+check"
    r"|encouraged\s+to\s+apply|requirements\s+to\s+be\s+considered|(?:are|is)\s+a\s+bonus,?\s+not\s+a\s+requirement")

YEARS_RX = re.compile(r"(?i)(\d{1,2})\s*\+?\s*(?:(?:-|to|–)\s*\d{1,2}\s*\+?\s*)?years?")
DEGREE_RX = re.compile(
    r"(?i)\b(?:bachelor'?s?|master'?s?|mba|ph\.?d|b\.?s\.?|m\.?s\.?|degree)\b")
# A credential is proven by the line that states it, wherever it sits.
CREDENTIAL_RX = re.compile(r"(?i)\bcertif|\bcertified\b|\blicen[cs]e|\b(?:cspo|psm|csm|pmp|cfa|cpa|safe)\b")
# Items a screening question answers, not a resume line: they are listed as
# knockouts and left out of the grade.
AUTH_RX = re.compile(
    r"(?i)\bauthori[sz]ed\s+to\s+work\b|\bwork\s+authori[sz]ation\b|\bsponsorship\b"
    r"|\bclearance\b|\bu\.?s\.?\s+citizen|\bcitizenship\b|\bsecurity\s+screening"
    r"|\bbackground\s+(?:check|screening)|\bdrug\s+(?:test|screen)|\bpolygraph\b")


# A bare list of personality traits ("Curious, energetic, positive, solution-oriented,
# collaborative, trusted"; "Team player, customer-focused mindset") states no skill
# a resume line can prove, so no screener grades it. Dropped at extraction (4 of 750
# JDs, 2026-10-15: ClarityPay had graded C on it).
TRAIT_WORDS = frozenset("""
curious energetic positive solution-oriented collaborative trusted trustworthy humble hungry
driven passionate motivated self-motivated self-starter proactive highly detail-oriented
organized creative resourceful adaptable flexible enthusiastic kind empathetic gritty
ambitious resilient team player customer-focused customer-obsessed mindset attitude
growth can-do optimistic friendly reliable dependable honest and or a an with""".split())


def is_trait_list(text: str) -> bool:
    words = [w.strip(".,;:()!").lower() for w in text.split()]
    words = [w for w in words if w]
    return (0 < len(words) <= 12 and not any(ch.isdigit() for ch in text)
            and sum(w in TRAIT_WORDS for w in words) >= 0.8 * len(words))


RULE_LINE_RX = re.compile(r"^\s*[-=_*]{3,}\s*$")
HTML_RX = re.compile(r"(?i)<(?:li|p|ul|ol|h[1-6]|div|br)\b")


def _html_to_text(s: str) -> str:
    """Some boards hand over the posting as HTML; flatten it to the markdown
    shapes the rest of this module reads (headings, bullets, bold)."""
    if not HTML_RX.search(s):
        return s
    import html as _html
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)<h[1-6][^>]*>", "\n## ", s)
    s = re.sub(r"(?i)<li[^>]*>", "\n- ", s)
    s = re.sub(r"(?i)</?(?:strong|b)>", "**", s)
    s = re.sub(r"(?i)</(?:p|div|h[1-6]|li|ul|ol)>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"\*\*\s*\*\*", "", s)
    return _html.unescape(s)


def _bare(line: str) -> str:
    """A line with heading markup gone: #, bold/italic markers, trailing colon."""
    s = line.strip().translate(_TIDY)
    s = re.sub(r"^#{1,6}\s*", "", s)
    s = re.sub(r"[*_]{1,}", "", s).strip()
    s = s.replace("\\", "")
    return s.rstrip(":").strip()


def head_kind(line: str) -> str | None:
    """"qual", "other", or None when the line is not a heading.

    Markup (#, a bold-only line, a trailing colon) makes a short line a heading
    of either kind; a bare short line is a heading only when the whole of it is
    a known section name, so a requirement line such as "Experience with SQL"
    is never mistaken for one.
    """
    raw = line.strip()
    if not raw or BULLET_LINE_RX.match(raw) or JOINER_RX.match(_bare(raw) or "x"):
        return None
    s = _bare(raw)
    if not s or len(s) > 80 or s.endswith("."):
        return None
    if NOT_QUAL_RX.search(s):
        return "other"
    if QUAL_HEAD_RX.match(s):
        return "qual"
    if OTHER_HEAD_RX.match(s):
        return "other"
    marked = (raw.startswith("#") or re.fullmatch(r"[*_]{2,}.+?[*_]{2,}:?", raw)
              or (raw.endswith(":") and len(s.split()) <= 8))
    if marked:
        if re.search(r"(?i)responsib|what\s+you'?ll\s+do", s):
            return "other"
        if re.search(r"(?i)qualif|requirement|skills|experience|looking\s+for"
                     r"|you\s+(?:have|bring|need)|about\s+you|must|nice", s):
            return "qual"
        return "sub"        # resolved by extract() from the heading before it
    return None


def _clean_item(line: str) -> str:
    s = BULLET_LINE_RX.sub("", line.translate(_TIDY), count=1).strip()
    s = re.sub(r"\*\*|__|`", "", s).replace("\\", "")
    s = re.sub(r"\s+", " ", s).strip(" ;,")
    return s


def _flags(text: str) -> dict:
    out = {}
    years = [int(m.group(1)) for m in YEARS_RX.finditer(text) if 0 < int(m.group(1)) <= 20]
    if years:
        out["years"] = min(years)
    if DEGREE_RX.search(text):
        out["degree"] = True
    if AUTH_RX.search(text):
        out["authorization"] = True
    return out


def _block_items(lines: list[str]) -> list[str]:
    """Items of one qualifications block (the lines after its heading, up to the
    next heading).

    Bullets are items; a "+" or indented sub-bullet under an item ending in ":"
    joins it; an "OR" line joins its neighbours into one alternative. A block
    with no bullets is read one line per item (LinkedIn's flattened form). Once
    bullets have started, closing boilerplate prose ends the block.
    """
    has_bullets = sum(1 for ln in lines if BULLET_LINE_RX.match(ln)) >= 2
    items: list[str] = []
    join_next = False
    started = False
    for ln in lines:
        if not ln.strip():
            continue
        if JOINER_RX.match(_bare(ln) or "x"):
            join_next = bool(items)
            continue
        lead = re.match(r"^(?:OR|or|and/or)\s+(\S.*)$", _clean_item(ln))
        if lead and items:          # Microsoft: "OR equivalent experience." on its own line
            items[-1] = f"{items[-1]} OR {lead.group(1)}"
            continue
        if ITALIC_NOTE_RX.match(ln):
            continue                # "*At this time, X will not sponsor ...*"
        is_bullet = bool(BULLET_LINE_RX.match(ln))
        text = _clean_item(ln)
        if not text:
            continue
        nested = is_bullet and (ln[:1] in " \t" and len(ln) - len(ln.lstrip()) >= 2
                                or ln.lstrip().startswith(("+", "◦", "o ")))
        if has_bullets and not is_bullet:
            if started:
                break               # the bullets are over: what follows is prose
            continue                # lead-in prose before the bullets
        if BOILERPLATE_RX.search(text) and not has_bullets:
            break
        started = True
        if items and (join_next or (nested and items[-1].endswith(":"))):
            sep = " OR " if join_next else " "
            items[-1] = f"{items[-1]}{sep}{text}"
            join_next = False
            continue
        join_next = False
        items.append(text)
    return [i for i in items if 3 <= len(i) <= 700]


def extract(jd_text: str) -> list[dict]:
    """Qualification items in JD order: [{"id","kind","text","section",...flags}]."""
    # The whole file, not ats_score.jd_body: that one cuts at the first "---"
    # in the top 25 lines, which in a Google posting sits AFTER the
    # qualifications. Metadata lines are never under a qualifications heading.
    text = _html_to_text(jd_text).translate(_TIDY)
    text = re.sub(r"(?<=\S)[ \t]*(#{2,6}\s)", r"\n\1", text)   # "...**.### **Minimum"
    raw = [("" if RULE_LINE_RX.match(ln) else ln) for ln in text.splitlines()]
    title = A.title_line(jd_text)
    lines: list[str] = []
    for ln in raw:                      # split "Requirements: 3+ years ..." in two
        m = INLINE_HEAD_RX.match(_bare(ln)) if not BULLET_LINE_RX.match(ln) else None
        if m and len(m.group(1)) <= 60:
            lines += [m.group(1) + ":", "- " + m.group(2)]
        else:
            lines.append(ln)
    # The "# <Company> - <Title>" line this pipeline writes is not a section,
    # even when the title says "Experience" ("Digital Brokerage Experience").
    heads = [(i, None if (title and re.match(r"#\s", ln.strip())
                          and ln.strip()[1:].strip() == title) else head_kind(ln))
             for i, ln in enumerate(lines)]
    heads = [(i, k) for i, k in heads if k]
    # An unknown marked heading ("**Required**", "**Product Management**") right
    # after a qualifications heading is a sub-section of it; anywhere else it
    # ends whatever block came before.
    # Only directly under it ("**Qualifications**" then "**Required**") or in a
    # chain of such category headings (BCG's "**Product Management**",
    # "**Data & Analytics**"); a stray "Interviewing with Veeva" after a filled
    # block is not one.
    prev_i, prev_k, prev_sub = None, None, False
    for n, (i, k) in enumerate(heads):
        is_sub = k == "sub"
        if is_sub:
            empty_gap = prev_i is not None and not any(
                ln.strip() for ln in lines[prev_i + 1:i])
            k = "qual" if prev_k == "qual" and (empty_gap or prev_sub) else "other"
            heads[n] = (i, k)
        prev_i, prev_k, prev_sub = i, k, is_sub and k == "qual"
    found: list[tuple[str, str, str]] = []      # (kind, text, section)
    for pos, (i, k) in enumerate(heads):
        if k != "qual":
            continue
        head = _bare(lines[i])
        stop = heads[pos + 1][0] if pos + 1 < len(heads) else len(lines)
        block_preferred = bool(PREFERRED_HEADING_RX.search(head))
        for text in _block_items(lines[i + 1:stop]):
            if is_trait_list(text):
                continue
            if AUTH_RX.search(text) or CONDITION_RX.search(text):
                kind = KNOCKOUT
            elif block_preferred:
                kind = PREFERRED
            elif (PREFERRED_INLINE_RX.search(text) and not REQUIRED_INLINE_RX.search(text)
                  and not QUALIFIER_RX.search(text)):
                kind = PREFERRED
            else:
                kind = BASIC
            found.append((kind, text, head[:60]))

    seen, items = set(), []
    counts = {BASIC: 0, PREFERRED: 0, KNOCKOUT: 0}
    cap = {BASIC: MAX_BASIC, PREFERRED: MAX_PREFERRED, KNOCKOUT: 5}
    for kind, text, section in found:
        key = A.fold(text)
        if key in seen or counts[kind] >= cap[kind]:
            continue
        seen.add(key)
        counts[kind] += 1
        items.append({"kind": kind, "text": text, "section": section, **_flags(text)})
    return [{"id": f"R{n}", **it} for n, it in enumerate(items, 1)]


def write_requirements(folder: Path, items: list[dict], source: str) -> dict:
    doc = {"version": VERSION, "source": source, "jd": "jd.md",
           "requirements": items}
    (folder / REQUIREMENTS).write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n",
                                       encoding="utf-8")
    return doc


def load_requirements(folder: Path) -> dict | None:
    p = folder / REQUIREMENTS
    if not p.is_file():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


# --- the candidate's dated record (what a parser computes from the page) -------

_MONTHS = {m: i for i, m in enumerate(
    "january february march april may june july august september october november december".split(), 1)}


def _ym(s: str) -> tuple[int, int] | None:
    m = re.match(r"(?i)\s*([a-z]+)\s+(\d{4})", s or "")
    if m and m.group(1).lower() in _MONTHS:
        return int(m.group(2)), _MONTHS[m.group(1).lower()]
    m = re.match(r"\s*(\d{4})", s or "")
    return (int(m.group(1)), 1) if m else None


def _months(start: str, end: str) -> int:
    a, b = _ym(start), _ym(end)
    if not a or not b:
        return 0
    return max(0, (b[0] - a[0]) * 12 + (b[1] - a[1]) + 1)


# A recruiter hiring now counts a degree that completes before the start date:
# a posting's "5+ years with a bachelor's OR 2+ with an advanced degree" graded D
# on 10-14 because a December 2026 master's read as not held.
EXPECTED_WITHIN_MONTHS = 6


def degree_state(end: str, today: date | None = None) -> str:
    """"completed", "expected <end>, counts as held", or "in progress"."""
    today = today or date.today()
    ym = _ym(end)
    now = (today.year, today.month)
    if not ym or ym <= now:
        return "completed"
    ahead = (ym[0] - now[0]) * 12 + (ym[1] - now[1])
    if ahead <= EXPECTED_WITHIN_MONTHS:
        return (f"expected {end}, counts as held: it completes before a hire made now "
                "would start")
    return "in progress"


def candidate_facts(page_text: str, record_path: Path = RECORD) -> dict:
    """Years as an ATS computes them: from the dated roles that are ON the page.

    record.json is the source because preflight's parse-back already holds the
    page's dates to it; Penzigo counts only when the page carries it.
    """
    try:
        rec = json.loads(record_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    page = page_text.lower()
    full, product, intern = 0, 0, 0
    roles = []
    for emp in rec.get("work", []):
        if emp["employer"].split()[0].lower() not in page:
            continue
        for r in emp.get("roles", []):
            m = _months(r.get("start", ""), r.get("end", ""))
            title = r.get("title", "")
            roles.append(f"{title} ({emp['employer']}, {r.get('start')} to {r.get('end')})")
            if re.search(r"(?i)\bintern\b", title):
                intern += m
                continue
            full += m
            if re.search(r"(?i)\bproduct\b", title):
                product += m
    degrees = []
    for edu in rec.get("education", []):
        for r in edu.get("roles", []):
            state = degree_state(r.get("end", ""))
            degrees.append(f"{r.get('title')} ({edu['employer']}, {r.get('end')}, {state})")
    return {"full_time_years": round(full / 12, 1),
            "product_titled_years": round(product / 12, 1),
            "internship_years": round(intern / 12, 1),
            "roles": roles, "degrees": degrees}


# --- title and findability ----------------------------------------------------

ROLE_RX = re.compile(
    r"(?i)\b(?:technical\s+|senior\s+|associate\s+|junior\s+)?"
    r"(?:product|program|project)\s+(?:manager|management|owner|analyst|lead|operations)\b"
    r"|\bforward[\s-]deployed\s+(?:engineer|product\s+manager|pm)\b"
    r"|\bsolutions?\s+(?:engineer|consultant|architect)\b|\bbusiness\s+analyst\b"
    r"|\bdeveloper\s+(?:advocate|relations)\b|\b(?:data|business)\s+(?:product\s+)?analyst\b"
    r"|\bstrateg(?:y|ist)\b|\bchief\s+of\s+staff\b")


def jd_role(jd_text: str) -> str:
    """The searchable role phrase from the title, minus seniority words."""
    m = re.search(r"(?im)^\s*\**job\s+title:?\**:?\s*(.+)$", jd_text)
    for src in ([m.group(1)] if m else []) + [A.title_line(jd_text)]:
        r = ROLE_RX.search(src)
        if r:
            return re.sub(r"(?i)^(?:senior|associate|junior)\s+", "", r.group(0)).strip()
    return ""


DATED_LINE_RX = re.compile(
    r"(?i)\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+(?:19|20)\d{2}\b")


def title_check(jd_text: str, page_lines_: list[str]) -> dict:
    """Does the posting's role phrase appear in a JOB TITLE on the page?

    Parsers file titles from the dated role lines, and a recruiter's title
    search runs against those, so a project header ("SAP Experience Garage |
    AI Product Manager") does not count. `stem` is the wildcard search
    ("product manag*"), which finds "Product Management Intern".
    """
    role = jd_role(jd_text)
    if not role:
        return {"role": "", "exact": None, "stem": None, "titles": []}
    titles = [ln for ln in page_lines_ if DATED_LINE_RX.search(ln) and not ln.lstrip().startswith("•")]
    hay = A.fold(" | ".join(titles))
    exact = bool(re.search(rf"(?<![a-z]){re.escape(A.fold(role))}(?![a-z])", hay))
    stem_word = A.fold(re.sub(r"(?i)\b(manager|management)\b", "manag", role))
    stem = exact or stem_word in hay
    return {"role": role, "exact": exact, "stem": stem,
            "titles": [re.sub(r"\s+", " ", t).strip()[:90] for t in titles[:6]]}


# Tool, platform and acronym names inside a requirement ("SQL", "F2P",
# "Azure DevOps", "Figma"): what a recruiter types into a boolean search.
NAME_RX = re.compile(r"\b(?:[A-Z][A-Za-z0-9+#.]*[A-Z0-9][A-Za-z0-9+#.]*|[A-Z][a-z]+(?:\s+[A-Z][a-zA-Z]+)?)\b")
NAME_STOP = set("""a an the and or of for with in on to be is are our your you we this that
experience strong ability excellent proven demonstrated bachelor bachelors master masters degree
familiarity knowledge understanding working comfort comfortable excited ideally preferred plus
bonus must able skills skill track record prior previous solid deep hands""".split())


def candidate_terms(jd_text: str, reqs: list[dict]) -> list[str]:
    """Search-string candidates for the screener to choose from: the curated
    market phrases (analyze_corpus.PHRASES) named in the requirements, then the
    tool and acronym names written in them. Mined prose words are left out;
    "business" and "continuously" are nobody's search string."""
    rq = A.normalize(" ".join(r["text"] for r in reqs))
    drop = A.company_names(jd_text)
    out: list[str] = []
    for t in A.extract_jd_terms(jd_text, A.bucket_for(jd_text)):
        if t.source == "phrase-list" and t.hits(rq) and t.text not in out:
            out.append(t.text)
    for r in reqs:
        for m in NAME_RX.finditer(r["text"]):
            w = m.group(0).strip()
            sentence_start = m.start() == 0 or re.search(r"[.:;!?]\s*$", r["text"][:m.start()])
            if sentence_start or w.lower() in NAME_STOP or A.fold(w) in drop or len(w) < 2:
                continue
            if w.lower() not in (x.lower() for x in out):
                out.append(w)
    return out[:30]


def findability(terms: list[str], resume: "A.ResumeText") -> dict:
    """Literal presence of each search string on the page, with the same
    tolerant surface matching ats_score uses (A/B test = AB testing)."""
    terms = [t for t in terms if str(t).strip()][:FINDABILITY_TERMS]
    if not terms:
        return {"hit": 0, "total": 0, "missing": [], "skills_only": [], "terms": []}
    objs = [A.Term(A.canonical_term(t), A.variants_for(t), 1.0, "screen") for t in terms]
    rep = A.score(objs, resume)
    by = {o.text: t for o, t in zip(objs, terms)}
    return {"hit": rep.matched_n, "total": rep.total_n,
            "missing": [by.get(m, m) for m in rep.missing],
            "skills_only": [by.get(m, m) for m in rep.skills_only], "terms": terms}


# --- the page -----------------------------------------------------------------

def resume_paths(folder: Path, docx: str | None, pdf: str | None) -> tuple[Path | None, Path | None]:
    def pick(ext):
        c = [p for p in sorted(folder.glob(f"Vaishnav_Ajai_Resume_*{ext}"))
             if ".new." not in p.name]
        return c[0] if c else None
    return (Path(docx) if docx else pick(".docx")), (Path(pdf) if pdf else pick(".pdf"))


def page_lines(pdf: Path | None) -> list[str]:
    if not pdf or not pdf.is_file():
        return []
    try:
        from pypdf import PdfReader
        out = []
        for page in PdfReader(str(pdf)).pages:
            out.extend(ln.rstrip() for ln in (page.extract_text() or "").splitlines() if ln.strip())
        return out
    except Exception:
        return []


_QUOTE_MAP = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-",
                            " ": " ", "•": " ", "·": " "})


def _flat(s: str) -> str:
    """Comparison form for quote checks: case, punctuation spacing, line-break
    hyphenation and bold markers all flattened."""
    s = s.translate(_QUOTE_MAP).replace("**", "")
    s = re.sub(r"-\s*\n\s*", "-", s)
    s = re.sub(r"[^a-z0-9%$+./&-]+", " ", s.lower())
    return " " + re.sub(r"\s+", " ", s).strip() + " "


def quote_on_page(quote: str, haystacks: list[str]) -> bool:
    q = _flat(quote).strip()
    if len(q.split()) < MIN_QUOTE_WORDS:
        return False
    return any(q in h for h in haystacks)


# --- grading ------------------------------------------------------------------

def grade_letter(items: list[dict]) -> str:
    """HiredScore's documented tiers, with `partial` counted as not met."""
    basic = [i for i in items if i["kind"] == BASIC]
    pref = [i for i in items if i["kind"] == PREFERRED]
    b_met = sum(i["verdict"] == MET for i in basic)
    p_met = sum(i["verdict"] == MET for i in pref)
    if not basic:
        # A posting that states no basic bar is graded on its preferred list.
        basic, b_met, pref, p_met = pref, p_met, [], 0
    if b_met == len(basic):
        return "A" if (not pref or p_met * 2 >= len(pref)) else "B"
    return "C" if b_met * 2 >= len(basic) else "D"


def screen_score(items: list[dict]) -> int:
    items = [i for i in items if i["kind"] in KIND_WEIGHT]
    num = sum(KIND_WEIGHT[i["kind"]] * CREDIT[i["verdict"]] for i in items)
    den = sum(KIND_WEIGHT[i["kind"]] for i in items)
    return round(100 * num / den) if den else 0


def _counts(items, kind):
    sub = [i for i in items if i["kind"] == kind]
    return {"met": sum(i["verdict"] == MET for i in sub),
            "partial": sum(i["verdict"] == PARTIAL for i in sub),
            "total": len(sub)}


def apply_verdicts(reqs: list[dict], screen: dict, haystacks: list[str],
                   body: str | None = None) -> list[dict]:
    """Join the scanner's verdicts onto the fixed list and verify every quote.

    A met or partial verdict needs at least one quote that is really on the
    page; without one it is graded not met and the row says why. A `met` whose
    every quote sits only on a Skills or Interests label line (absent from
    `body`, the page minus those lines) is graded partial, unless the item is a
    degree or certification, which that line is the right place to state: an evidence-based
    screener credits a listed skill less than work that shows it, and that line
    is where the old scanner's synonym credit lived. A requirement the scanner
    skipped is not met. The scanner cannot add, drop or reword items.
    """
    by_id = {}
    for v in screen.get("verdicts", []) or []:
        if isinstance(v, dict) and v.get("id"):
            by_id[str(v["id"]).strip()] = v
    out = []
    for r in reqs:
        if r["kind"] == KNOCKOUT:
            out.append({**r, "verdict": "n/a", "evidence": [], "to_meet": "",
                        "note": "a screening question answers this, not the resume"})
            continue
        v = by_id.get(r["id"], {})
        verdict = str(v.get("verdict", NOT_MET)).strip().lower().replace(" ", "_")
        if verdict not in CREDIT:
            verdict = NOT_MET
        ev = v.get("evidence") or []
        if isinstance(ev, str):
            ev = [ev] if ev.strip() else []
        good = [q for q in ev if isinstance(q, str) and quote_on_page(q, haystacks)]
        bad = [q for q in ev if isinstance(q, str) and q not in good]
        note = str(v.get("note", "")).strip()
        if verdict != NOT_MET and not good:
            note = ("evidence quote not found on the page; graded not met"
                    + (f" (was {verdict})" if verdict else ""))
            verdict = NOT_MET
        elif bad:
            note = (note + "; " if note else "") + f"{len(bad)} quote(s) not on the page ignored"
        credential = r.get("degree") or CREDENTIAL_RX.search(r.get("text", ""))
        if verdict == MET and body is not None and good and not credential and \
                not any(quote_on_page(q, [body]) for q in good):
            verdict = PARTIAL
            note = (note + "; " if note else "") + "evidence is only a Skills line; graded partial"
        if not v:
            note = "not graded by the scanner"
        out.append({**r, "verdict": verdict, "evidence": good, "note": note,
                    "to_meet": str(v.get("to_meet", "")).strip()})
    return out


def _cell(s: str, n: int = 140) -> str:
    s = re.sub(r"\s+", " ", (s or "").replace("|", "/")).strip()
    return s if len(s) <= n else s[: n - 3].rstrip() + "..."


VERDICT_WORD = {MET: "met", PARTIAL: "partial", NOT_MET: "not met", "n/a": "screening question"}


def render_section(graded: list[dict], grade: str, score: int, facts: dict,
                   title: dict, find: dict, parse: list[str]) -> str:
    b, p = _counts(graded, BASIC), _counts(graded, PREFERRED)
    L = ["## ATS screen", ""]
    L.append(f"Grade: {grade} (basic {b['met']} of {b['total']} met, {b['partial']} partial;"
             f" preferred {p['met']} of {p['total']} met, {p['partial']} partial)."
             f" Screen score: {score}.")
    if title.get("role"):
        state = ("in a job title on the page" if title["exact"] else
                 "not in any job title as written; a wildcard search (manag*) would find one"
                 if title.get("stem") else "not in any job title on the page")
        L.append(f"Title search: the posting's role is \"{title['role']}\", {state}.")
    if find.get("total"):
        miss = ", ".join(find["missing"][:10]) or "none"
        L.append(f"Findability: {find['hit']} of {find['total']} qualification terms a recruiter"
                 f" would search are on the page; missing: {miss}.")
        if find.get("skills_only"):
            L.append("Only on a Skills line (weak for an evidence-based screener): "
                     + ", ".join(find["skills_only"]) + ".")
    L += ["", "| ID | Kind | Requirement | Verdict | Evidence on the page |", "|---|---|---|---|---|"]
    for g in graded:
        ev = " / ".join(f'"{_cell(q, 90)}"' for q in g["evidence"]) or "-"
        if g["note"]:
            ev += f" ({_cell(g['note'], 90)})"
        L.append(f"| {g['id']} | {g['kind']} | {_cell(g['text'])} | {VERDICT_WORD[g['verdict']]} | {ev} |")

    order = {(BASIC, NOT_MET): 0, (BASIC, PARTIAL): 1, (PREFERRED, NOT_MET): 2, (PREFERRED, PARTIAL): 3}
    gaps = sorted((g for g in graded if (g["kind"], g["verdict"]) in order),
                  key=lambda g: order[(g["kind"], g["verdict"])])
    L += ["", "Gaps to close (most valuable first):"]
    if gaps:
        for g in gaps:
            fix = g["to_meet"] or "no suggestion"
            L.append(f"- {g['id']} ({g['kind']}, {VERDICT_WORD[g['verdict']]}): {_cell(fix, 260)}")
    else:
        L.append("- none")

    flags = []
    for g in graded:
        if g.get("years") and g["kind"] == BASIC and facts:
            if g["years"] > facts.get("full_time_years", 0):
                flags.append(f"{g['id']} asks {g['years']}+ years; the page shows "
                             f"{facts['full_time_years']} years full time"
                             f" ({facts['product_titled_years']} in product-titled roles).")
            elif g["years"] > facts.get("product_titled_years", 0) and re.search(
                    r"(?i)product|pm\b", g["text"]) and not re.search(
                    r"(?i)software|engineering|development|technical|related|equivalent", g["text"]):
                # "product/program management OR software development" counts the
                # ML engineer year too; only a product-only ask uses the narrower count.
                flags.append(f"{g['id']} asks {g['years']}+ years of product work; the page shows "
                             f"{facts['product_titled_years']} in product-titled roles"
                             f" plus {facts['internship_years']} of internships.")
        if g.get("authorization"):
            flags.append(f"{g['id']} is a screening item (work authorization, background or"
                         " security screening): answered in the application questions, not by"
                         " the resume.")
    L += ["", "Knockout flags (a screening question usually asks these):"]
    L += [f"- {f}" for f in flags] or ["- none"]

    L += ["", "Parse and format:"]
    L += [f"- {_cell(x, 300)}" for x in parse if str(x).strip()] or ["- clean"]
    L += ["", f"ATS_SCORE: {score}", ""]
    return "\n".join(L)


# --- commands -----------------------------------------------------------------

def _page_bundle(folder: Path, docx: str | None, pdf: str | None):
    dpath, ppath = resume_paths(folder, docx, pdf)
    resume = A.resume_texts(dpath, ppath)
    # The PDF text layer is what an ATS parses; the docx is the fallback when
    # no PDF was rendered (a needs_review run, or a test).
    lines = page_lines(ppath) or [ln for ln in resume.docx.splitlines() if ln.strip()]
    page = "\n".join(lines)
    return dpath, ppath, resume, lines, page


def cmd_extract(folder: Path) -> int:
    jd = (folder / "jd.md").read_text(encoding="utf-8")
    items = extract(jd)
    if len(items) < MIN_ITEMS:
        write_requirements(folder, [], "none")
        print(f"requirements: {len(items)} qualification item(s) found; too few to grade."
              " The round-1 screener derives the list from the JD.")
        return 0
    write_requirements(folder, items, "extracted")
    b = sum(i["kind"] == BASIC for i in items)
    print(f"requirements: {len(items)} items ({b} basic, {len(items) - b} preferred)"
          f" -> {folder / REQUIREMENTS}")
    return 0


def _flag_words(r: dict) -> str:
    fl = []
    if r.get("years"):
        fl.append(f"{r['years']}+ yrs")
    if r.get("degree"):
        fl.append("degree")
    if r.get("authorization"):
        fl.append("work authorization")
    return f"  ({', '.join(fl)})" if fl else ""


def cmd_plan(folder: Path) -> int:
    """The tailor's view before drafting: the fixed list, the dated record as a
    parser will compute it, and the literal terms a recruiter would search."""
    jd = (folder / "jd.md").read_text(encoding="utf-8")
    doc = load_requirements(folder)
    if doc is None:
        cmd_extract(folder)
        doc = load_requirements(folder)
    reqs = doc.get("requirements", [])
    print(f"# Requirement plan ({len(reqs)} items, source {doc.get('source')})")
    for r in reqs:
        print(f"{r['id']} [{r['kind']}] {r['text']}{_flag_words(r)}")
    if not reqs:
        print("No qualifications block found: write the requirement map from the JD's"
              " must-haves yourself; the round-1 screener derives the graded list.")
    # No page exists yet, so every employer counts; the screener recomputes
    # from the rendered page, where Penzigo is usually gone.
    facts = candidate_facts("fidelity citigroup penzigo")
    print(f"\nDated record (Penzigo counts only if the page keeps it): "
          f"{facts.get('full_time_years')} yrs full time, "
          f"{facts.get('product_titled_years')} in product-titled roles, "
          f"{facts.get('internship_years')} of internships.")
    role = jd_role(jd)
    if role:
        print(f"Title a recruiter searches: \"{role}\" (matched against the dated title lines)")
    terms = candidate_terms(jd, reqs)
    if terms:
        print("Search strings a recruiter might type (phrases and names in the requirements): "
              + ", ".join(terms))
    return 0


def _search_terms(doc: dict, screen: dict | None, jd: str) -> list[str]:
    """Locked after round 1: requirements.json's list, else the round's own,
    else the candidates. The same strings are checked every round."""
    for src in (doc.get("search_terms"), (screen or {}).get("search_terms")):
        if isinstance(src, list) and any(str(x).strip() for x in src):
            return [str(x).strip() for x in src if str(x).strip()][:FINDABILITY_TERMS]
    return candidate_terms(jd, doc.get("requirements", []))[:12]


def cmd_packet(folder: Path, docx: str | None, pdf: str | None) -> int:
    """Everything the screener needs, in one print: the fixed list, the
    computed record, the title and findability checks, and the page text."""
    jd = (folder / "jd.md").read_text(encoding="utf-8")
    doc = load_requirements(folder) or {"requirements": [], "source": "none"}
    _, ppath, resume, lines, page = _page_bundle(folder, docx, pdf)
    print("# Requirements (fixed list; grade every id, add none, reword none)")
    if doc["requirements"]:
        for r in doc["requirements"]:
            tail = "  (knockout: a screening question answers it; no verdict)" \
                if r["kind"] == KNOCKOUT else ""
            print(f"{r['id']} [{r['kind']}] {r['text']}{tail}")
    else:
        print("NONE EXTRACTED. Derive them from jd.md's qualifications (or, failing that, the"
              " responsibilities) and put a \"requirements\" list in your screen JSON:"
              ' [{"id":"R1","kind":"basic|preferred","text":"<the JD\'s own words>"}].')
    f = candidate_facts(page)
    print("\n# Dated record on this page (computed from the dates, as a parser does)")
    print(f"Full time: {f.get('full_time_years')} yrs; product-titled roles: "
          f"{f.get('product_titled_years')} yrs; internships: {f.get('internship_years')} yrs.")
    for r in f.get("roles", []):
        print(f"- {r}")
    for d in f.get("degrees", []):
        print(f"- {d}")
    t = title_check(jd, lines)
    if t.get("role"):
        print(f"\n# Title search: \"{t['role']}\" in a job title: exact {t['exact']},"
              f" wildcard {t['stem']}")
    if doc.get("search_terms"):
        print("# Search strings (locked in round 1): " + ", ".join(doc["search_terms"]))
    else:
        print("# Search-string candidates (round 1: choose 8-15 a recruiter would type,"
              " add any the JD needs): " + ", ".join(candidate_terms(jd, doc["requirements"])))
    print(f"\n# Page text ({ppath.name if ppath else 'no PDF'}; quote evidence from here verbatim)")
    for n, ln in enumerate(lines, 1):
        print(f"{n:02d}  {ln}")
    return 0


def cmd_grade(folder: Path, rnd: int, docx: str | None, pdf: str | None,
              out: str | None) -> int:
    jd = (folder / "jd.md").read_text(encoding="utf-8")
    spath = folder / f"screen-r{rnd}.json"
    if not spath.is_file():
        print(f"grade: {spath} is missing; write the verdicts first", file=sys.stderr)
        return 2
    try:
        screen = json.loads(spath.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"grade: {spath} is not valid JSON ({e}); fix it and re-run", file=sys.stderr)
        return 2
    doc = load_requirements(folder) or {"requirements": [], "source": "none"}
    reqs = doc["requirements"]
    source = doc.get("source", "extracted")
    if not reqs:
        derived, remap = [], {}
        for r in screen.get("requirements") or []:
            if isinstance(r, dict) and str(r.get("text", "")).strip():
                kind = PREFERRED if str(r.get("kind", "")).lower().startswith("pref") else BASIC
                text = _clean_item(str(r["text"]))
                new_id = f"R{len(derived) + 1}"
                remap[str(r.get("id", new_id)).strip()] = new_id
                derived.append({"id": new_id, "kind": kind, "text": text,
                                "section": "derived by the screener", **_flags(text)})
        if not derived:
            print("grade: requirements.json is empty and the screen JSON carries no"
                  " \"requirements\" list; derive them and re-run", file=sys.stderr)
            return 2
        for v in screen.get("verdicts") or []:
            if isinstance(v, dict) and str(v.get("id", "")).strip() in remap:
                v["id"] = remap[str(v["id"]).strip()]
        reqs, source = derived, "screener"
    terms = _search_terms(doc, screen, jd)
    if reqs is not doc["requirements"] or not doc.get("search_terms"):
        doc = write_requirements(folder, reqs, source)
        doc["search_terms"] = terms
        (folder / REQUIREMENTS).write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n",
                                           encoding="utf-8")

    dpath, ppath, resume, lines, page = _page_bundle(folder, docx, pdf)
    hay = [_flat(" ".join(lines)), _flat(resume.docx)]
    graded = apply_verdicts(reqs, screen, hay, _flat(resume.body) if resume.docx else None)
    grade, score = grade_letter(graded), screen_score(graded)
    facts = candidate_facts(page)
    title = title_check(jd, lines)
    find = findability(terms, resume)
    parse = [str(x) for x in (screen.get("parse") or []) if str(x).strip()]
    section = render_section(graded, grade, score, facts, title, find, parse)
    target = Path(out) if out else folder / f"review-ats-r{rnd}.md"
    target.write_text(section, encoding="utf-8")

    summary = {"version": VERSION, "round": rnd, "grade": grade, "score": score,
               "basic": _counts(graded, BASIC), "preferred": _counts(graded, PREFERRED),
               "title": {k: title.get(k) for k in ("role", "exact", "stem")},
               "findability": {k: find.get(k) for k in ("hit", "total", "missing")},
               "gaps": [g["id"] for g in graded if g["verdict"] in (NOT_MET, PARTIAL)]}
    (folder / SCREEN).write_text(json.dumps(summary, indent=1) + "\n", encoding="utf-8")
    b = summary["basic"]
    print(f"ATS screen: grade {grade}, score {score} (basic {b['met']}/{b['total']} met)"
          f" -> {target}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("extract", "plan", "packet", "grade"):
        p = sub.add_parser(name)
        p.add_argument("folder")
        if name in ("packet", "grade"):
            p.add_argument("--docx")
            p.add_argument("--pdf")
        if name == "grade":
            p.add_argument("--round", type=int, required=True)
            p.add_argument("--out")
    a = ap.parse_args(argv)
    folder = Path(a.folder)
    if not (folder / "jd.md").is_file():
        print(f"{folder}/jd.md not found", file=sys.stderr)
        return 2
    if a.cmd == "extract":
        return cmd_extract(folder)
    if a.cmd == "plan":
        return cmd_plan(folder)
    if a.cmd == "packet":
        return cmd_packet(folder, a.docx, a.pdf)
    return cmd_grade(folder, a.round, a.docx, a.pdf, a.out)


if __name__ == "__main__":
    sys.exit(main())
