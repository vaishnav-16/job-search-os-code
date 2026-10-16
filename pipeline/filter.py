"""Regex hard filters — the last deterministic gate before any LLM sees a job.

Per CLAUDE.md Layer 1 §4 these auto-reject: explicit no-sponsorship, 6+ YOE
required, Director/Staff/Principal titles, a Senior title whose JD states no
years, pure marketing/sales PM, outside US.

PRECISION RULE (CLAUDE.md): reject only on high-confidence patterns. Ambiguous
phrasing passes through to the scorer. A wrongly rejected job is invisible
forever; a wrongly passed one costs one cheap scoring call. Every regex below
was calibrated against real JD text already in the queue, and the test suite
asserts both directions (rejects AND the near-misses that must survive).

    python pipeline/filter.py            # dry run over status='new'
    python pipeline/filter.py --apply    # write statuses
"""

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db
import ingest_scores
import scrape

QUEUE_DIR = Path(__file__).resolve().parent.parent / "queue"

# --- seniority -------------------------------------------------------------
# "Lead" and "Senior" are deliberately absent. Vaish's call: "Lead" is level
# ambiguous ("Lead Digital Product Manager" at Wells Fargo is senior, "Lead
# Product Operations Associate" at ETS is not), so it goes to the scorer, which
# reads the JD's YOE ask to disambiguate. "Senior" is handled by SENIOR_LEVEL
# below, and only when the JD states no years.
SENIOR_TITLE = re.compile(
    r"(?i)\b(principal|staff|director|dir\.|vp|vice president|head of|"
    r"distinguished|fellow|architect|executive)\b")
# "Chief of Staff" would be double-caught by \bstaff\b and \bchief\b, but
# preferences.md classifies it as a strategy role that is score-CAPPED at 6,
# not auto-rejected. Same for other chief-of-* constructions.
SENIOR_EXEMPT = re.compile(r"(?i)chief of staff")

# --- sponsorship -----------------------------------------------------------
# Must not fire on "executive sponsors and steering forums" (real Cigna JD) or
# "project sponsor". Every pattern therefore requires an explicit negation plus
# a visa/authorization context.
NO_SPONSORSHIP = re.compile(
    r"(?i)("
    r"(?:visa\s+)?sponsorship\s+(?:is\s+)?(?:generally\s+|currently\s+)?not\s+"
    r"(?:available|offered|provided|supported)"
    # The routine re-laned these by hand (notes 10-07, 10-08): an eligibility
    # line ("must not require sponsorship now or in the future") and Capital
    # One's "will not sponsor a new applicant for employment authorization".
    r"|(?:must|will|does)\s+not\s+(?:now\s+or\s+in\s+the\s+future\s+)?require\s+"
    r"(?:employer\s+|visa\s+|immigration\s+)?sponsorship"
    r"|not\s+eligible\s+for\s+(?:visa\s+)?sponsorship"
    r"|(?:cannot|can\s?not|unable\s+to|will\s+not|do(?:es)?\s+not)\s+"
    r"(?:currently\s+|be\s+able\s+to\s+)?(?:provide|offer|sponsor)"
    r"(?:\s+new)?\s*(?:visa\s+|immigration\s+)?(?:sponsorship|sponsor)"
    r"|does\s+not\s+now\s+and(?:/or)?\s+will\s+not\s+in\s+the\s+future\s+require\s+sponsorship"
    r"|without\s+the\s+need\s+for\s+(?:current\s+or\s+future\s+)?(?:visa\s+)?sponsorship"
    r"|no\s+visa\s+sponsorship"
    # "unable to sponsor visas" — sponsor as the verb, visa as its object.
    r"|(?:cannot|can\s?not|unable\s+to|will\s+not|do(?:es)?\s+not)\s+"
    r"(?:currently\s+)?sponsor\s+(?:any\s+|an?\s+)?(?:new\s+)?"
    r"(?:visas?|work\s+visas?|candidates?|applicants?|individuals?|employment|H-?1B)"
    r")")

# --- years of experience ---------------------------------------------------
# Range-aware so "5-8 years" yields 5, not 8. Optional leading range group.
YEARS = re.compile(r"(?:(\d{1,2})\s*(?:[-–—]|to)\s*)?(\d{1,2})\s*\+?\s*years?", re.I)
# "At least 18 years of age" is a real T-Mobile line and is not a YOE ask.
AGE_CONTEXT = re.compile(r"(?i)^\s*(?:of\s+age|old\b)")
# Company-history boilerplate: "For more than 75 years, we've been advancing the
# science of measurement" (real ETS line) and "For more than 20 years, UST has
# worked..." (real UST line). Both wrongly read as a 75- and 20-year YOE ask.
COMPANY_HISTORY = re.compile(
    r"(?i)(?:for|over|spanning|celebrating|in\s+our|backed\s+by)\s+"
    r"(?:more\s+than\s+|over\s+|nearly\s+|almost\s+|the\s+past\s+)?$")
# A YOE requirement talks about experience. Company age does not. Requiring this
# keyword near the figure is what separates "12+ years of PM experience" from
# "For more than 75 years, we've been advancing...".
EXPERIENCE_CONTEXT = re.compile(
    r"(?i)(experience|expertise|background|track\s+record|tenure|"
    r"years?\s+(?:in|across|as)\s+(?:a\s+|an\s+)?(?:product|program|project|technical|"
    r"similar|related|software|engineering))")
# A "preferred / nice to have" ask is not a hard requirement, so it never
# triggers rejection ("8+ years preferred" must pass, per CLAUDE.md).
SOFT_CONTEXT = re.compile(
    r"(?i)(preferred|nice[- ]to[- ]have|bonus|a plus|is a plus|desirable|ideally|"
    r"would be great|not required)")
# Vaish 2026-09-16: 3+ years of experience; 7+ asks are out of reach. Lowered to
# 6 on 2026-10-09: 5 is the stretch line, and since 10-08 supply outruns the
# scorer (~800 roles a day, 360 scored), 73 of 169 scorer skips citing years.
YOE_CEILING = 6
# A Senior / Sr / III / IV title whose JD states no years scores (b)=1 and is
# never a stretch, so the scorer could only digest it (Vaish 2026-10-09: drop
# them). Only with a JD in hand: a title-only pre-check must not drop a
# "Senior Product Analyst, 3+ years" before its page is read.
SENIOR_LEVEL = re.compile(r"(?i)\b(senior|sr\.?|iii|iv)\b")
# Which section a figure sits in: the LAST marker before it wins, inline or as a
# heading. "### Preferred qualifications:" then "* 8 years ..." is soft however
# far down the list it sits; the old +-120-char window both missed that and
# softened hard bullets that merely sat next to "an advanced degree is
# preferred" (10-06 replay: 25 JDs the scorer skipped for 7+ years, 0 filtered).
SOFT_SECTION = re.compile(
    r"(?i)(?:preferred|desired|nice[- ]to[- ]have|bonus|stand out)\s*(?:qualifications|skills|"
    r"experience|requirements)?\s*(?::|\n|\*\*)")
HARD_SECTION = re.compile(
    r"(?i)(?:minimum|basic|required)\s+(?:qualifications|requirements|experience)|"
    r"requirements\s*:|what you(?:'|’)ll (?:need|bring)|what you bring|must[- ]haves?")
# A team's or company's tenure, not an ask of the candidate: "a team with 20+
# years of experience", "Backed by 18 years of proprietary credit data".
# Tenure phrasing only: a bare "we"/"our" also opens the commonest HARD ask ("We
# are looking for a PM with 8+ years of experience").
TEAM_CONTEXT = re.compile(
    r"(?i)(?:\b(?:team|company|firm|founders?|leadership)\s+(?:with|of|that\s+has|brings?|has)"
    r"|\bwe(?:'ve|\s+have)\s+(?:been|spent|built|had)|\bcombined)"
    r"\s*(?:over|more\s+than|nearly|almost)?\s*$")
# Two figures joined by "or" are alternative paths (degree-dependent asks).
ALTERNATIVE = re.compile(r"(?i)\bor\b")

# --- security clearance / US-citizenship requirement -----------------------
# Added 2026-08-17 on Vaish's instruction to skip govcon/defence/clearance roles.
# This is an ELIGIBILITY gate, not a preference: a posting that requires a
# clearance or citizenship cannot be accepted regardless of fit, so unlike a
# weak-fit role it is worth rejecting before the scorer rather than after.
#
# Calibrated against the two Guidehouse Product Owner JDs that reached the
# auto-tailor lane at 7 ("Must be able to obtain a Public Trust Clearance (US
# Citizen)"). Per the precision rule these fire only on an explicit requirement:
#  - a named clearance level, or
#  - "clearance required/must obtain/active ... clearance", or
#  - an explicit US-citizenship requirement.
# Deliberately NOT matched, because they are not requirements on this candidate:
# "clearance" alone (legal/compliance clearance, clearing trades — Vaish's own
# Citi work is full of settlement "clearing"), "security" alone, and
# "equal opportunity ... citizenship status" EEO boilerplate.
CLEARANCE = re.compile(
    r"(?i)("
    # A NAMED clearance level. "clearance" alone is never enough: a JD can ask
    # for legal/marketing clearance, and Vaish's own Citi domain is full of
    # trade "clearing".
    # NOTE: bare "SCI" is deliberately NOT here. It is an ordinary acronym in
    # other domains — Save the Children International abbreviates itself SCI
    # throughout its own JDs, which this rule wrongly rejected on 2026-08-17.
    # SCI only counts inside "TS/SCI" or spelled out.
    r"\b(?:top[\s-]?secret|ts/sci|sci\s+clearance|sensitive\s+compartmented"
    r"|secret\s+clearance|public\s+trust"
    r"|q\s+clearance|l\s+clearance|dod\s+clearance|security\s+clearance"
    r"|government\s+clearance|federal\s+clearance|interim\s+clearance)"
    r"|clearance\s+(?:is\s+)?required"
    r"|\bpolygraph\b"
    r"|must\s+be\s+a\s+u\.?s\.?\s+citizen"
    r"|u\.?s\.?\s+citizenship\s+(?:is\s+)?required"
    r"|requires?\s+u\.?s\.?\s+citizenship"
    r"|\(\s*u\.?s\.?\s+citizen\s*\)"
    r")")
# EEO boilerplate names citizenship as a protected class, never as a requirement.
CLEARANCE_EXEMPT = re.compile(
    r"(?i)(equal\s+(?:employment\s+)?opportunit|without\s+regard\s+to|"
    r"protected\s+(?:veteran|class)|discriminat)")
# ITAR / export-control boilerplate reads "must be a U.S. citizen OR national, OR
# permanent resident (Green Card), OR lawfully admitted as a refugee" — an
# INCLUSIVE list that a green-card holder satisfies, not a citizens-only bar.
# Real example that this rule exists for: Blue Origin, 2026-08-17. Matching only
# the "must be a U.S. citizen" prefix rejected a job the JD actually permits, and
# a wrongly rejected job is invisible forever.
CLEARANCE_INCLUSIVE = re.compile(
    r"(?i)(permanent\s+resident|green\s?card|lawfully\s+admitted|asylee|refugee|"
    r"protected\s+individual|u\.?s\.?\s+national)")

# --- marketing / sales -----------------------------------------------------
# Title-level only. "Growth Product Manager" is a real PM lane and must pass.
MARKETING_SALES = re.compile(
    r"(?i)(product\s+marketing|marketing\s+manager|marketing\s+specialist|"
    r"\bsales\b|account\s+executive|business\s+development|demand\s+gen|"
    r"brand\s+manager|communications\s+coordinator)")


def _in_soft_section(jd_text: str, pos: int) -> bool:
    soft = [m.end() for m in SOFT_SECTION.finditer(jd_text, 0, pos)]
    hard = [m.end() for m in HARD_SECTION.finditer(jd_text, 0, pos)]
    return bool(soft) and (not hard or soft[-1] > hard[-1])


def _soft_on_line(jd_text: str, start: int, end: int) -> bool:
    """A soft word on the figure's own line (bullet), within 150 chars of it."""
    ls = jd_text.rfind("\n", 0, start) + 1
    le = jd_text.find("\n", end)
    le = len(jd_text) if le < 0 else le
    return bool(SOFT_CONTEXT.search(jd_text[max(ls, start - 150):min(le, end + 150)]))


def required_figures(jd_text: str) -> list[tuple[int, int, int]]:
    """Every hard YOE ask as (start, end, years); a range counts as its low end."""
    found = []
    for m in YEARS.finditer(jd_text or ""):
        tail = jd_text[m.end():m.end() + 12]
        if AGE_CONTEXT.match(tail):
            continue
        before = jd_text[max(0, m.start() - 50):m.start()]
        if COMPANY_HISTORY.search(before[-30:]) or TEAM_CONTEXT.search(before):
            continue
        if _soft_on_line(jd_text, m.start(), m.end()) or _in_soft_section(jd_text, m.start()):
            continue
        if not EXPERIENCE_CONTEXT.search(jd_text[max(0, m.start() - 60):m.end() + 90]):
            continue
        low, high = m.group(1), m.group(2)
        try:
            found.append((m.start(), m.end(), int(low) if low else int(high)))
        except (TypeError, ValueError):
            continue
    return found


def min_required_years(jd_text: str):
    """The years a candidate must clear, or None if no hard ask is stated.

    Requirement bullets are ADDITIVE: "10 years of PM experience" plus "5 years
    taking technical products to market" asks for 10, and the minimum across
    them (the rule until 10-06) read 5 and let every such JD through. Figures
    joined by "or" are alternative paths and count as their smallest: "7-9 years
    (Bachelor's degree) or 5+ years (advanced degree)" is reachable at 5.
    """
    figs = required_figures(jd_text or "")
    if not figs:
        return None
    groups = [[figs[0]]]
    for a, b in zip(figs, figs[1:]):
        between = jd_text[a[1]:b[0]]
        # Same bullet only: "...management or a related role.\n* 5 years" is two
        # asks, and the "or" belongs to the first one's wording.
        if len(between) <= 160 and "\n" not in between and ALTERNATIVE.search(between):
            groups[-1].append(b)
        else:
            groups.append([b])
    return max(min(f[2] for f in g) for g in groups)


def requires_clearance(jd_text: str) -> bool:
    """True when the JD makes a clearance or US citizenship a REQUIREMENT.

    The exemption is checked in a window around each hit, never document-wide:
    almost every JD carries EEO boilerplate somewhere, so a document-level check
    would silently disable this rule on every posting.
    """
    if not jd_text:
        return False
    for m in CLEARANCE.finditer(jd_text):
        window = jd_text[max(0, m.start() - 160):m.end() + 160]
        if CLEARANCE_EXEMPT.search(window):
            continue
        # A citizenship phrase followed by an inclusive alternation is export-
        # control boilerplate, not a bar. Only the citizenship branch can be
        # softened this way: a NAMED clearance level means what it says.
        if CLEARANCE_INCLUSIVE.search(jd_text[m.end():m.end() + 200]):
            continue
        return True
    return False


def hard_filter(title: str, jd_text: str = "", location: str = ""):
    """Return the name of the rule that rejects this job, or None to keep it."""
    title = title or ""
    jd_text = jd_text or ""

    if SENIOR_TITLE.search(title) and not SENIOR_EXEMPT.search(title):
        return "seniority_title"
    if MARKETING_SALES.search(title):
        return "marketing_sales"
    if NO_SPONSORSHIP.search(jd_text):
        return "no_sponsorship"
    if requires_clearance(jd_text):
        return "clearance_required"
    years = min_required_years(jd_text)
    if years is not None and years >= YOE_CEILING:
        return "yoe_6plus"
    if years is None and jd_text.strip() and SENIOR_LEVEL.search(title):
        return "senior_no_years"
    if location and scrape.foreign_only(location):
        return "non_us"
    return None


def sweep(conn, apply: bool):
    """Retroactively filter rows already sitting at status='new'."""
    rows = list(conn.execute(
        "SELECT id, company, title, location, jd_text FROM jobs WHERE status = 'new'"))
    hits = []
    for r in rows:
        rule = hard_filter(r["title"], r["jd_text"], r["location"])
        if rule:
            hits.append((r, rule))

    by_rule = {}
    for _, rule in hits:
        by_rule[rule] = by_rule.get(rule, 0) + 1

    print(f"scanned {len(rows)} queued job(s); {len(hits)} would be filtered\n")
    for rule, n in sorted(by_rule.items(), key=lambda kv: -kv[1]):
        print(f"  {rule:<18}{n:>4}")
    print("\nrejected jobs (verify none of these should have survived):")
    for r, rule in hits:
        t = r["title"].encode("ascii", "replace").decode()[:52]
        c = str(r["company"]).encode("ascii", "replace").decode()[:18]
        print(f"  [{rule:<15}] {c:<18} | {t}")

    if not apply:
        print(f"\nDRY RUN. {len(rows) - len(hits)} would survive. Re-run with --apply.")
        return

    for r, rule in hits:
        conn.execute("UPDATE jobs SET status = 'skipped', notes = ? WHERE id = ?",
                     (f"hard-filter:{rule}", r["id"]))
        f = QUEUE_DIR / f"{r['id']}.json"
        if f.exists():
            f.unlink()
        ingest_scores.drop_sidecar(r["id"])  # sidecars track queue membership
    conn.commit()
    print(f"\nfiltered {len(hits)} row(s) to status 'skipped'; "
          f"{len(rows) - len(hits)} remain for scoring")
    print("db totals:", db.counts_by_status(conn))


def main():
    ap = argparse.ArgumentParser(description="Regex hard filters")
    ap.add_argument("--apply", action="store_true", help="write statuses (default: dry run)")
    args = ap.parse_args()
    sweep(db.connect(), args.apply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
