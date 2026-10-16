"""What the tailor keeps wishing were true (read-only, no LLM).

    python pipeline/evidence_asks.py               # every brief
    python pipeline/evidence_asks.py --days 14     # briefs from the last 14 days
    python pipeline/evidence_asks.py --jsonl out.jsonl   # every ask, tagged, for a clustering pass

Each brief ends with "Would raise the score if true": the facts that would have
lifted that resume had the knowledge base held them. Read one at a time they are
noise; across a few hundred briefs they rank the questions worth asking Vaish
(improvement plan W1). This prints counts by theme and by the project or role an
ask names, with examples, so a fact session starts from the most frequent gaps.
Rerun weekly; a theme that stops shrinking after a fact session was not answered.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APPS = ROOT / "applications"

SECTION = re.compile(r"(?ims)^#+\s*would raise the score if true\s*\n(.*?)(?=^#+\s|\Z)")
FOLDER_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})$")

# What the ask is for. An ask can carry several themes.
THEMES = [
    ("users/adoption numbers", r"\busers?\b|adoption|usage|active|retention|sign-?ups?|installs?|"
                               r"downloads?|sends|traffic|waitlist|customers? count|how many people"),
    ("customer-facing", r"customer|client|partner|external|field|on-?site|embedded|implementation|"
                        r"onboard|support queue|account team|stakeholder interview"),
    ("experimentation/analytics", r"\bsql\b|a/b|experiment|control group|lift|funnel|conversion|"
                                  r"cohort|dashboard|metric|analytics|looker|tableau|amplitude"),
    ("pricing/GTM/launch", r"pric|monetiz|revenue|gtm|go-to-market|launch|marketing|sales|"
                           r"packaging|business case|p&l|margin"),
    ("coding/technical depth", r"\bcode\b|coding|commit|pull request|\bprs?\b|repo|deploy|production|"
                               r"api|sdk|latency|infra|kubernetes|aws|gcp|azure|pipeline|"
                               r"typescript|python|javascript|react|gpu|serving|benchmark"),
    ("AI/ML outcome", r"\bmodel\b|\bllm\b|eval|agent|genai|gen ai|rag|fine-?tun|prompt|hallucin"),
    ("team/leadership scope", r"\bled\b|\blead\b|manag(ed|ing) (a|the)? ?team|direct reports|"
                              r"mentor|hired|cross-functional|engineers? (he|on)|team size"),
    ("design/UX", r"design|figma|ux\b|user research|usability|prototype|interview(ed)? users"),
    ("domain exposure", r"exposure|domain|industry|insurance|health|tax|payments?|card|credit|"
                        r"lending|retail|e-?commerce|gaming|game|security|privacy|crypto|"
                        r"supply chain|logistics|energy|climate|education|legal"),
]
THEMES = [(name, re.compile(pat, re.I)) for name, pat in THEMES]

# Which piece of his record the ask names (the knowledge file it would land in).
SUBJECTS = [
    ("Networking OS", r"networking os|email digest"),
    ("Penzigo", r"penzigo"),
    ("Citi", r"\bciti\b|trade-?fail|request queue"),
    ("Fidelity", r"fidelity|copilot"),
    ("SAP", r"\bsap\b|experience garage"),
    ("Heat Wise", r"heat ?wise"),
    ("Opendoor Justice", r"opendoor|justice"),
    ("Hallucination detector", r"hallucination|deberta"),
    ("Tuck research", r"\btuck\b"),
    ("GyftEasy", r"gyft"),
    ("Job search OS", r"job[- ]search os"),
]
SUBJECTS = [(name, re.compile(pat, re.I)) for name, pat in SUBJECTS]


def brief_asks(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
    m = SECTION.search(text)
    if not m:
        return []
    asks, cur = [], None
    for line in m.group(1).splitlines():
        s = line.strip()
        if s.startswith(("- ", "* ")):
            cur = s[2:].strip()
            asks.append(cur)
        elif s and asks and not s.startswith("#"):
            asks[-1] = f"{asks[-1]} {s}"   # a wrapped bullet
    return [a for a in asks if a]


def collect(days: int | None) -> list[dict]:
    since = date.today() - timedelta(days=days) if days else None
    rows = []
    for brief in sorted(APPS.glob("*/brief.md")):
        folder = brief.parent.name
        m = FOLDER_DATE.search(folder)
        when = m.group(1) if m else ""
        if since and (not when or date.fromisoformat(when) < since):
            continue
        for ask in brief_asks(brief):
            rows.append({
                "folder": folder,
                "date": when,
                "ask": ask,
                "themes": [n for n, rx in THEMES if rx.search(ask)] or ["other"],
                "subjects": [n for n, rx in SUBJECTS if rx.search(ask)],
            })
    return rows


def report(rows: list[dict], examples: int) -> str:
    folders = {r["folder"] for r in rows}
    out = [f"{len(rows)} asks from {len(folders)} briefs", ""]
    for label, key in (("By theme", "themes"), ("By project or role named", "subjects")):
        counts, ex = Counter(), defaultdict(list)
        for r in rows:
            for k in r[key]:
                counts[k] += 1
                ex[k].append(r["ask"])
        out.append(f"## {label}")
        for k, n in counts.most_common():
            out.append(f"{n:5d}  {k}")
            for a in (ex[k][-examples:] if examples else []):   # newest briefs last
                out.append(f"         - {a[:160]}")
        out.append("")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, help="only briefs whose folder date is this recent")
    ap.add_argument("--examples", type=int, default=3, help="examples per bucket")
    ap.add_argument("--jsonl", type=Path, help="also write every tagged ask here")
    args = ap.parse_args()
    rows = collect(args.days)
    if args.jsonl:
        args.jsonl.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    print(report(rows, args.examples))


if __name__ == "__main__":
    main()
