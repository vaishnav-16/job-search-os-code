"""Rank keyword frequencies across the JD corpus, per PM/TPM bucket.

Counts DOCUMENT frequency (how many JDs mention a term at least once), not raw
occurrences — a term pasted 20x in one JD is one company's boilerplate, not
market vocabulary. Outputs pipeline/keyword_corpus/frequencies.json for the
judgment pass that writes profile/keywords.md.

Run: python pipeline/analyze_corpus.py
"""

import json
import re
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
CORPUS = HERE / "keyword_corpus"

STOP = set("""a an and are as at be been by for from has have if in into is it its of on or
that the their this to was we will with you your our they them he she i not no than then
who what which while about across it's were do does did done can could should would may
might must us more most other others including include includes such via etc per each
all any both like well who's within without &""".split())

# Phrases counted as units before unigram mining (multi-word market vocabulary).
PHRASES = [
    "product manager", "program manager", "technical program manager", "product management",
    "program management", "product owner", "roadmap", "road map", "cross-functional",
    "cross functional", "stakeholder", "go-to-market", "gtm", "a/b test", "ab test",
    "experimentation", "user research", "customer research", "customer discovery",
    "data-driven", "data driven", "sql", "analytics", "metrics", "kpi", "okr",
    "machine learning", "artificial intelligence", " ai ", "llm", "large language model",
    "generative ai", "genai", "agentic", "ai agent", "prompt", "evals", "evaluation",
    "fine-tun", "rag", "retrieval", "embedding", "api", "platform", "infrastructure",
    "developer experience", "devex", "sdk", "integration", "payments", "fintech",
    "risk", "fraud", "compliance", "regulatory", "security", "privacy", "trust and safety",
    "b2b", "saas", "enterprise", "growth", "activation", "retention", "engagement",
    "conversion", "funnel", "monetization", "pricing", "prioritize", "prioritization",
    "backlog", "sprint", "agile", "scrum", "jira", "requirements", "prd", "spec",
    "user stories", "acceptance criteria", "mvp", "0 to 1", "zero to one", "0-1",
    "discovery", "launch", "ship", "shipped", "execution", "strategy", "vision",
    "north star", "define", "drive", "own", "lead", "collaborate", "partner",
    "influence", "communicate", "communication", "presentation", "executive",
    "leadership", "engineers", "engineering", "design", "designers", "data science",
    "data scientists", "customer-facing", "customer obsession", "ambiguity",
    "fast-paced", "startup", "scale", "scalable", "reliability", "latency",
    "observability", "cloud", "aws", "azure", "gcp", "kubernetes", "etl",
    "data pipeline", "data platform", "dashboards", "instrumentation", "experiments",
    "hypothesis", "insights", "segmentation", "personas", "journey map",
    "dependencies", "risk management", "milestones", "schedule", "delivery",
    "workback", "escalation", "status report", "operating rhythm", "mechanisms",
    "systems thinking", "technical depth", "computer science", "mba",
    "years of experience", "stakeholder management", "product sense",
    "product strategy", "market research", "competitive analysis", "positioning",
    "adoption", "onboarding", "self-serve", "usability", "accessibility",
    "internationalization", "mobile", "web", "frontend", "backend", "full-stack",
    "ml models", "model performance", "ground truth", "annotation", "inference",
    "training data", "context window", "hallucination", "guardrails",
    "responsible ai", "safety", "red team", "benchmarks",
]


def normalize(text: str) -> str:
    """The canonical searchable form: lowercased, whitespace collapsed to single
    spaces, and space-padded at both ends.

    The padding is load-bearing. Every term regex here opens with `(?<![a-z])`,
    which a term sitting at position 0 of the string would satisfy anyway, but
    PHRASES carries entries written with surrounding spaces (" ai ") and callers
    slice this text; padding makes the first and last words look exactly like
    interior ones. Extracted from main() 2026-08-22 so ats_score.py matches
    against the same text this corpus was ranked over.
    """
    return " " + re.sub(r"\s+", " ", text.lower()) + " "


def compile_term(term: str) -> re.Pattern:
    """Boundary-aware pattern for one term, matched against normalize()d text.

    "rag" must not match inside "storage", "spec" not inside "specific". The
    lookarounds exclude only [a-z], so a term still matches next to a digit, a
    hyphen or punctuation ("ai-powered", "3-4 weeks"). Trailing-open for exactly
    one deliberate prefix, "fine-tun", which stands for fine-tune/fine-tuning.
    """
    key = term.strip()
    tail = r"" if key == "fine-tun" else r"(?![a-z])"
    return re.compile(r"(?<![a-z])" + re.escape(key) + tail)


def load_docs():
    docs = []
    for sub in ("ats", "giants"):
        d = CORPUS / sub
        if not d.exists():
            continue
        for f in sorted(d.glob("*.md")):
            text = f.read_text(encoding="utf-8", errors="replace")
            m = re.search(r"^bucket:\s*(PM|TPM)", text, re.M)
            bucket = m.group(1) if m else "PM"
            docs.append((f.name, bucket, normalize(text.split("---", 2)[-1])))
    return docs


def main() -> int:
    docs = load_docs()
    if not docs:
        print("no corpus found")
        return 1
    buckets = {"PM": [d for d in docs if d[1] == "PM"],
               "TPM": [d for d in docs if d[1] == "TPM"]}
    out = {"corpus_size": {k: len(v) for k, v in buckets.items()}}

    phrase_rx = {p.strip(): compile_term(p) for p in PHRASES}

    for name, group in buckets.items():
        n = len(group)
        phrase_df, unigram_df = Counter(), Counter()
        for _, _, body in group:
            seen_p = {k for k, rx in phrase_rx.items() if rx.search(body)}
            phrase_df.update(seen_p)
            words = set(re.findall(r"[a-z][a-z\-+/]{2,}", body))
            unigram_df.update(w for w in words if w not in STOP)
        out[name] = {
            "phrases": [(p, c, round(100 * c / n)) for p, c in phrase_df.most_common(120)],
            "unigrams": [(w, c, round(100 * c / n)) for w, c in unigram_df.most_common(150)],
        }

    (CORPUS / "frequencies.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    for name in ("PM", "TPM"):
        top = ", ".join(f"{p} {pct}%" for p, _, pct in out[name]["phrases"][:25])
        print(f"[{name} x{out['corpus_size'][name]}] {top}")
    print("full ranking -> pipeline/keyword_corpus/frequencies.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
