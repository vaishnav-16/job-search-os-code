"""Collect PM/TPM JDs from target-tier ATS boards into pipeline/keyword_corpus/ats/.

One-time-ish collector for the M6 keyword corpus. Reuses scrape.py's fetchers
and filter.py's hard filters so the corpus reflects the roles the pipeline
actually targets (no Principal/Staff/Director, US-only, no marketing PM).

Run: python pipeline/collect_corpus.py
"""

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import filter as hardfilter  # noqa: E402
import scrape  # noqa: E402

OUT = HERE / "keyword_corpus" / "ats"

# Target-tier boards to try. A 404/empty board is reported, never fatal.
BOARDS = [
    ("greenhouse", "stripe", "Stripe"),
    ("greenhouse", "datadog", "Datadog"),
    ("greenhouse", "robinhood", "Robinhood"),
    ("greenhouse", "affirm", "Affirm"),
    ("greenhouse", "brex", "Brex"),
    ("greenhouse", "gusto", "Gusto"),
    ("greenhouse", "coinbase", "Coinbase"),
    ("greenhouse", "databricks", "Databricks"),
    ("greenhouse", "cloudflare", "Cloudflare"),
    ("greenhouse", "dropbox", "Dropbox"),
    ("greenhouse", "samsara", "Samsara"),
    ("greenhouse", "twilio", "Twilio"),
    ("greenhouse", "figma", "Figma"),
    ("greenhouse", "airtable", "Airtable"),
    ("greenhouse", "gitlab", "GitLab"),
    ("greenhouse", "mongodb", "MongoDB"),
    ("greenhouse", "reddit", "Reddit"),
    ("greenhouse", "anthropic", "Anthropic"),
    ("ashby", "ramp", "Ramp"),
    ("ashby", "plaid", "Plaid"),
    ("ashby", "linear", "Linear"),
    ("ashby", "openai", "OpenAI"),
    ("ashby", "deel", "Deel"),
    ("ashby", "retool", "Retool"),
    ("ashby", "mercury", "Mercury"),
    ("lever", "palantir", "Palantir"),
]

TITLE_RE = re.compile(
    r"(?i)\b(product manager|associate product manager|product owner|"
    r"technical program manager|program manager)\b")
TPM_RE = re.compile(r"(?i)\b(technical program manager|program manager|tpm)\b")
MAX_PER_COMPANY = 4
MIN_JD_CHARS = 1500  # a real JD; skips stub postings


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:70]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    manifest, per_source = [], {}
    for board, slug, company in BOARDS:
        try:
            jobs = scrape.FETCHERS[board](slug)
        except Exception as e:
            per_source[company] = f"ERROR {type(e).__name__}"
            continue
        kept = 0
        for j in jobs:
            title, jd, loc = j["title"], j.get("jd", ""), j.get("location", "")
            if kept >= MAX_PER_COMPANY:
                break
            if not TITLE_RE.search(title) or len(jd) < MIN_JD_CHARS:
                continue
            if hardfilter.hard_filter(title, jd, loc):
                continue  # senior title / non-US / 8+ yoe / marketing
            if not scrape.location_ok(loc, re.compile(scrape.US_SIGNAL.pattern, re.I)) \
               and "remote" not in loc.lower():
                pass  # location_ok already consulted inside hard_filter path; keep permissive
            bucket = "TPM" if TPM_RE.search(title) else "PM"
            name = f"{slugify(company)}--{slugify(title)}.md"
            (OUT / name).write_text(
                "---\n"
                f"company: {company}\ntitle: {title}\nurl: {j.get('url','')}\n"
                f"bucket: {bucket}\nlocation: {loc}\ncollected: 2026-08-14\n"
                "---\n\n" + jd + "\n", encoding="utf-8")
            manifest.append({"file": name, "company": company, "title": title,
                             "bucket": bucket})
            kept += 1
        per_source[company] = kept
    (OUT.parent / "ats_manifest.json").write_text(
        json.dumps(manifest, indent=1), encoding="utf-8")
    pm = sum(1 for m in manifest if m["bucket"] == "PM")
    tpm = len(manifest) - pm
    print(f"collected {len(manifest)} JDs (PM {pm} / TPM {tpm})")
    for c, n in per_source.items():
        print(f"  {c:<12} {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
