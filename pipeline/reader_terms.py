"""Domain terms the JD's reader may not know (W9, plain language for the reader).

    python pipeline/reader_terms.py scan                      # every application folder
    python pipeline/reader_terms.py scan --since 2026-10-13 --until 2026-10-14
    python pipeline/reader_terms.py scan applications/<folder> [...]

WHY (2026-10-14). Pages written for the JD's reader read better. Pipeline
pages carried about 11
bank and enterprise terms a page that their own JD never used (CSDR, REMIT, JFSA,
middle office, WebLogic...), and nothing in the pipeline checked for it.

The rule is "write for the JD's reader", not "strip jargon". A term stays when the
JD uses it, or when the JD shows the reader works in that domain (an anchor word:
CSDR on a page for a bank or a trading desk is a signal, not noise). Otherwise the
page should say what the thing does, and GLOSS is a true plain description the
tailor may use.

Read the rendered PDF, never the docx: the docx keeps hidden template paragraphs
that inflate a count. A term on the Skills lines is counted apart from the prose,
because a tool name there is findability, not a sentence the reader must parse.
Preflight calls `unexplained()` and WARNs; this file's CLI is the W9 instrument.
"""

from __future__ import annotations

import argparse
import re
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APPS = ROOT / "applications"
FOLDER_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})$")

# Words that show the JD's reader works in a domain. Matched case-insensitively
# against the JD. Kept broad on purpose: a false "shares the domain" costs one
# missed WARN, a false "does not" teaches the tailor to ignore the warning.
ANCHORS = {
    "bank": r"\bbank(?:s|ing)?\b|\btrad(?:ing|er|ers)\b|\bsettlement\b|capital markets|"
            r"\bsecurities\b|\bbroker|\bderivatives\b|regulatory reporting|"
            r"financial services|financial institution|asset management|"
            r"investment management|wealth management|\bcustody\b|\bclearing\b|"
            r"\bfintech\b|post-trade|front office|middle office|back office",
    "microsoft": r"power platform|power apps|power automate|microsoft 365|\bm365\b|"
                 r"sharepoint|low-code",
    "enterprise-architecture": r"enterprise architecture|application portfolio|\bleanix\b|"
                               r"\bsap\b",
    "java-middleware": r"\bjava ee\b|\bj2ee\b|weblogic|websphere|\bjboss\b|middleware|"
                       r"application server",
    "messaging": r"\bkafka\b|streaming|event-driven|event driven|message queue|"
                 r"messaging|pub/sub|real-time data|\brabbitmq\b|distributed systems|"
                 r"\bbackend\b|back-end|infrastructure",
    "containers": r"\baws\b|amazon web services|container|docker|kubernetes|\becs\b|"
                  r"infrastructure|devops",
    "python-web": r"\bpython\b",
    "java": r"\bjava\b|\bbackend\b|back-end",
    "ml": r"machine learning|\bml\b|anomaly|data science|modeling|modelling",
    "api": r"\bapis?\b|graphql",
}


@dataclass(frozen=True)
class Term:
    name: str
    pattern: str        # regex; acronyms match case-sensitively, phrases do not
    gloss: str          # a true plain description, safe to paste onto a page
    domain: str         # key into ANCHORS
    case: bool = True   # True = case-sensitive (acronyms)

    def rx(self) -> re.Pattern:
        return re.compile(self.pattern, 0 if self.case else re.I)


# From the master resume and profile/knowledge/ (10-16). Each gloss describes what
# the thing is in Vaish's own work, so it stays true wherever it lands.
TERMS = [
    Term("CSDR", r"\bCSDR\b", "the EU rule that fines trades that settle late", "bank"),
    Term("REMIT", r"\bREMIT\b", "the EU rule for reporting wholesale energy trades", "bank"),
    Term("JFSA", r"\bJFSA\b", "Japan's financial regulator", "bank"),
    Term("ISO 20022", r"\bISO\s*20022\b",
         "the global message standard banks use to report and pay", "bank"),
    Term("consent order", r"\bconsent[\s-]+order\b",
         "a regulator's binding order to fix specific problems", "bank", case=False),
    Term("middle office", r"\bmiddle[\s-]+office\b",
         "the bank team that checks and reports trades after they are booked", "bank",
         case=False),
    Term("OTC", r"\bOTC\b", "traded directly between two firms rather than on an exchange",
         "bank"),
    Term("DTCC", r"\bDTCC\b", "the US market utility that receives trade reports", "bank"),
    Term("Equias", r"\bEquias\b",
         "the vendor that sends energy trade reports to the EU regulator", "bank"),
    Term("RRM", r"\bRRM\b", "the registered firm that submits trade reports to the regulator",
         "bank"),
    Term("trade fail", r"\btrade[\s-]+fails?\b",
         "a trade that does not settle on its due date", "bank", case=False),
    Term("collateral", r"\bcollateral\b",
         "the assets pledged against trades, which regulators want reported", "bank",
         case=False),
    Term("Basel", r"\bBasel\b", "the global rules on how much capital a bank must hold",
         "bank"),
    Term("AML", r"\bAML\b", "anti money laundering checks", "bank"),
    Term("Power Automate", r"\bPower[\s-]+Automate\b", "Microsoft's workflow automation tool",
         "microsoft", case=False),
    Term("LeanIX", r"\bLeanIX\b", "SAP's tool for mapping a company's applications",
         "enterprise-architecture", case=False),
    Term("WebLogic", r"\bWebLogic\b", "Oracle's older Java application server",
         "java-middleware", case=False),
    Term("Kafka", r"\bKafka\b", "a system that passes event messages between services",
         "messaging", case=False),
    Term("RabbitMQ", r"\bRabbitMQ\b", "a message queue between services", "messaging",
         case=False),
    Term("JMS", r"\bJMS\b", "Java's interface for message queues", "java"),
    Term("ECS", r"\bECS\b", "AWS's service for running containers", "containers"),
    Term("CherryPy", r"\bCherryPy\b", "a small Python web framework", "python-web",
         case=False),
    Term("isolation forest", r"\bisolation[\s-]+forest\b",
         "a standard model for spotting outliers", "ml", case=False),
    Term("GraphQL", r"\bGraphQL\b", "a query language for APIs", "api", case=False),
]

# Where the Skills lines start in the PDF text layer. Everything after it is lists
# of names a recruiter searches, so a term there is not prose the reader must parse.
SKILLS_HEADING = re.compile(r"^\s*SKILLS\b", re.M)


def split_skills(page_text: str) -> tuple[str, str]:
    """(prose, skills) from the rendered page's text layer."""
    hits = list(SKILLS_HEADING.finditer(page_text))
    if not hits:
        return page_text, ""
    cut = hits[-1].start()
    return page_text[:cut], page_text[cut:]


def jd_shares_domain(term: Term, jd_text: str) -> bool:
    return bool(re.search(ANCHORS[term.domain], jd_text, re.I))


def unexplained(page_text: str, jd_text: str) -> dict[str, list[Term]]:
    """{"prose": [...], "skills": [...]}: terms on the page the JD's reader may not know.

    A term is left out when the JD uses it (same pattern) or shares its domain.
    """
    prose, skills = split_skills(page_text)
    out: dict[str, list[Term]] = {"prose": [], "skills": []}
    for t in TERMS:
        rx = t.rx()
        if rx.search(jd_text) or jd_shares_domain(t, jd_text):
            continue
        if rx.search(prose):
            out["prose"].append(t)
        elif rx.search(skills):
            out["skills"].append(t)
    return out


# --- the W9 instrument --------------------------------------------------------

def page_text(pdf: Path) -> str:
    from pypdf import PdfReader
    return "\n".join((pg.extract_text() or "") for pg in PdfReader(str(pdf)).pages)


def resume_pdf(folder: Path) -> Path | None:
    pdfs = sorted(folder.glob("Vaishnav_Ajai_Resume*.pdf"))
    pdfs = [p for p in pdfs if not p.name.endswith(".new.pdf")]
    return pdfs[0] if pdfs else None


def scan(folders: list[Path]) -> list[dict]:
    rows = []
    for f in folders:
        pdf, jd = resume_pdf(f), f / "jd.md"
        if not pdf or not jd.exists():
            continue
        hits = unexplained(page_text(pdf), jd.read_text(encoding="utf-8", errors="replace"))
        rows.append({"folder": f.name, "prose": [t.name for t in hits["prose"]],
                     "skills": [t.name for t in hits["skills"]]})
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan", help="count page-not-JD terms per application folder")
    s.add_argument("folders", nargs="*", type=Path)
    s.add_argument("--since", help="folder date on or after (YYYY-MM-DD)")
    s.add_argument("--until", help="folder date on or before (YYYY-MM-DD)")
    s.add_argument("--quiet", action="store_true", help="totals only")
    args = ap.parse_args(argv)

    folders = args.folders or sorted(p for p in APPS.iterdir() if p.is_dir())
    picked = []
    for f in folders:
        m = FOLDER_DATE.search(f.name)
        d = m.group(1) if m else ""
        if args.since and d < args.since or args.until and d > args.until:
            continue
        picked.append(f)
    rows = scan(picked)
    if not rows:
        print("no folder with a resume PDF and a jd.md")
        return 1
    if not args.quiet:
        for r in rows:
            print(f"{len(r['prose']):2d} prose {len(r['skills']):2d} skills  {r['folder']}"
                  f"  {', '.join(r['prose'])}")
    prose = [len(r["prose"]) for r in rows]
    total = [len(r["prose"]) + len(r["skills"]) for r in rows]
    counts: dict[str, int] = {}
    for r in rows:
        for n in r["prose"] + r["skills"]:
            counts[n] = counts.get(n, 0) + 1
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:12]
    print(f"\n{len(rows)} pages: mean {statistics.mean(prose):.1f} in prose, "
          f"{statistics.mean(total):.1f} with skills; "
          f"{sum(1 for n in total if n)} of {len(rows)} carry at least one")
    print("most frequent: " + ", ".join(f"{n} {c}" for n, c in top))
    return 0


if __name__ == "__main__":
    sys.exit(main())
