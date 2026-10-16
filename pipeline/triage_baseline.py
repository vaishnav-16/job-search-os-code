"""One-time backlog triage after the first Layer 1 poll.

A first ATS poll returns every currently-open role, not just new ones, so the
initial queue mixes today's postings with roles that have been open for months.
Scoring all of them wastes Layer 2 budget on stale listings.

This marks anything whose ORIGINAL posting date is older than the cutoff as
`baseline_skip`: the row stays in the DB so dedupe still suppresses it forever,
but it is never scored. Steady-state runs (3h cron, JobSpy ≤24h) will not need
this — it exists for the one-time cold start.

    python pipeline/triage_baseline.py            # dry run, prints the split
    python pipeline/triage_baseline.py --apply    # write the statuses
    python pipeline/triage_baseline.py --days 14  # different cutoff
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db

QUEUE_DIR = Path(__file__).resolve().parent.parent / "queue"


def classify(conn, cutoff_days: float):
    """Split status='new' rows into fresh / stale / undated."""
    fresh, stale, undated = [], [], []
    for r in conn.execute("SELECT id, company, title, location, url, posted_at, source "
                          "FROM jobs WHERE status = 'new'"):
        age = db.days_since_posted(r["posted_at"])
        row = dict(r, age_days=age)
        if age is None:
            undated.append(row)
        elif age > cutoff_days:
            stale.append(row)
        else:
            fresh.append(row)
    return fresh, stale, undated


def main():
    ap = argparse.ArgumentParser(description="First-run backlog triage")
    ap.add_argument("--days", type=float, default=7.0, help="freshness cutoff (default 7)")
    ap.add_argument("--apply", action="store_true", help="write statuses (default: dry run)")
    args = ap.parse_args()

    conn = db.connect()
    fresh, stale, undated = classify(conn, args.days)
    total = len(fresh) + len(stale) + len(undated)

    print(f"queued jobs (status='new'): {total}")
    print(f"  <= {args.days:g}d  -> stays 'new', enters scoring : {len(fresh)}")
    print(f"  >  {args.days:g}d  -> 'baseline_skip'             : {len(stale)}")
    print(f"  no posting date -> kept for scoring (unknown != stale): {len(undated)}")

    print("\nfresh by source:")
    by_src = {}
    for r in fresh:
        by_src[r["source"]] = by_src.get(r["source"], 0) + 1
    for src, n in sorted(by_src.items(), key=lambda kv: -kv[1]):
        print(f"  {src:<18}{n:>4}")

    if stale:
        ages = sorted(r["age_days"] for r in stale)
        print(f"\nstale age range: {ages[0]:.0f}d to {ages[-1]:.0f}d "
              f"(median {ages[len(ages)//2]:.0f}d)")

    if not args.apply:
        print("\nDRY RUN. Re-run with --apply to write statuses.")
        return 0

    for r in stale:
        db.set_status(conn, r["id"], "baseline_skip", via="baseline")
        qf = QUEUE_DIR / f"{r['id']}.json"
        if qf.exists():
            qf.unlink()  # queue/ holds work for Layer 2; skipped rows are not work
    print(f"\nmarked {len(stale)} rows baseline_skip and removed their queue files")
    print("db totals:", db.counts_by_status(conn))
    return 0


if __name__ == "__main__":
    sys.exit(main())
