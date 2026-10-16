"""Recurring freshness sweep + age reporting for Layer 2.

Vaish's policy: apply inside 24h of posting, and past 48h it is not worth
applying at all. This module enforces the enforceable half of that.

    python pipeline/freshness.py                 # report: age histogram, dry run
    python pipeline/freshness.py --apply         # mark provably-stale as stale_skip
    python pipeline/freshness.py --hours 72      # different cutoff

Why "provably" stale and not just "stale": 82% of the corpus carries a
date-only posting date, which is a 24h-wide interval rather than a number. A
row is swept only when even the YOUNGEST it could be exceeds the cutoff. That
means some genuinely-stale rows survive a sweep (the undecidable middle), which
is the correct direction of error per CLAUDE.md: a wrongly rejected job is
invisible forever, a wrongly passed one costs one cheap scoring call.

Supersedes triage_baseline.py, which used the pessimistic single-number age and
therefore over-rejected the straddling day-bucket.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db  # noqa: E402
import ingest_scores  # noqa: E402

QUEUE_DIR = Path(__file__).resolve().parent.parent / "queue"

# Reported buckets key off the OPTIMISTIC end (best_hours): "could this still
# be inside the window?" That is the question that decides whether to apply.
#
# Membership is db.age_tier(), never a local re-derivation of the same cuts. A
# row whose only age evidence is when we first saw it (precision "seen") has a
# real lower bound but no posting date, and age_tier is the one place that says
# such a row may be demoted to "provably stale" but never promoted to "apply
# now" — restating the cuts here is how the two surfaces drift apart.
BUCKETS = (
    ("<24h  (apply now)", "now"),
    ("24-48h (still worth it)", "soon"),
    (">48h  (provably stale)", "stale"),
    ("no posting date", "nodate"),
)


# Both are pre-tailoring work states: 'new' awaits scoring, 'scored' awaits
# Vaish's promotion from the digest. A scored-but-stale job is just as dead as
# an unscored one, and leaving it in the digest is noise he has to re-triage.
WORK_STATUSES = ("new", "scored")


def rows_for(conn, statuses=WORK_STATUSES):
    if isinstance(statuses, str):
        statuses = (statuses,)
    q = ",".join("?" * len(statuses))
    return list(conn.execute(
        "SELECT id, company, title, source, posted_at, first_seen, score, status "
        f"FROM jobs WHERE status IN ({q})", tuple(statuses)))


def histogram(rows) -> dict[str, int]:
    out = {name: 0 for name, _ in BUCKETS}
    label_of = {tier: name for name, tier in BUCKETS}
    for r in rows:
        best, _worst, precision = db.job_age(r["posted_at"], r["first_seen"])
        out[label_of[db.age_tier(best, precision)]] += 1
    return out


def stale_rows(rows, hours: float):
    # first_seen is passed because observation is proof of age: a row we
    # recorded four days ago is at least four days old, whatever its posting
    # date claims. Without it this gate read only `posted_at`, and 82% of the
    # corpus carries a date-only posting date whose optimistic end never
    # exceeds 48h until the calendar catches up — 54 of 197 work rows were
    # demonstrably past the apply window and unsweepable on 2026-08-23.
    return [r for r in rows
            if db.is_provably_stale(r["posted_at"], hours, first_seen=r["first_seen"])]


def sweep(conn, hours: float = db.STALE_HOURS, apply: bool = False) -> list:
    """Mark provably-stale `new` rows as stale_skip. Returns the affected rows."""
    doomed = stale_rows(rows_for(conn), hours)
    if apply:
        for r in doomed:
            db.set_status(conn, r["id"], "stale_skip", via="freshness")
            qf = QUEUE_DIR / f"{r['id']}.json"
            if qf.exists():
                qf.unlink()  # queue/ holds work for Layer 2; stale rows are not work
            # A score sidecar outlives ingest on purpose, but not the job's
            # membership in the work set: leaving one behind would make the
            # cloud Routine treat this job as already-scored forever.
            ingest_scores.drop_sidecar(r["id"])
        conn.commit()
    return doomed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--hours", type=float, default=db.STALE_HOURS,
                    help=f"stale cutoff in hours (default {db.STALE_HOURS:g})")
    ap.add_argument("--apply", action="store_true", help="write statuses (default: dry run)")
    args = ap.parse_args()

    conn = db.connect()
    rows = rows_for(conn)
    by_status: dict[str, int] = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    print(f"jobs awaiting work (new + scored): {len(rows)}  "
          f"{by_status}\n")
    for name, n in histogram(rows).items():
        print(f"  {name:<26}{n:>5}")

    doomed = stale_rows(rows, args.hours)
    print(f"\nprovably older than {args.hours:g}h (youngest possible age exceeds it): {len(doomed)}")
    by_src: dict[str, int] = {}
    for r in doomed:
        by_src[r["source"]] = by_src.get(r["source"], 0) + 1
    for src, n in sorted(by_src.items(), key=lambda kv: -kv[1]):
        print(f"  {src:<20}{n:>5}")
    if doomed:
        worst = sorted(doomed, key=lambda r: -(db.job_age(r["posted_at"], r["first_seen"])[0] or 0))
        print("\n  oldest few:")
        for r in worst[:5]:
            b, w, p = db.job_age(r["posted_at"], r["first_seen"])
            # 14 wide, not 8: the label now carries its own verb ("posted 12h
            # ago" / "seen 5d ago") so a reader never has to guess whether a
            # number is a posting age or a sighting age.
            print(f"    {db.age_label(b, w, p):>14}  {r['company'][:22]:<24}{r['title'][:40]}")

    survivors = len(rows) - len(doomed)
    print(f"\nsurvive the sweep and stay in play: {survivors}")
    if not args.apply:
        print("\nDRY RUN. Re-run with --apply to write statuses.")
        return 0

    sweep(conn, args.hours, apply=True)
    print(f"\nmarked {len(doomed)} rows stale_skip and removed their queue files")
    # Freshly-staled rows just left the work set; drop their JD bodies in the
    # same run so the commit that stales them also stops carrying their text.
    shed, freed = db.shed_jd_text(conn)
    if shed:
        print(f"shed jd_text from {shed} settled row(s) ({freed / 1e3:.0f} KB)")
    print("db totals:", db.counts_by_status(conn))
    return 0


if __name__ == "__main__":
    sys.exit(main())
