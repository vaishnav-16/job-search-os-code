"""Tests for the freshness helpers in db.py — interval semantics are the point.

The bug these guard against: treating a date-only posting date as a single
number. "2026-08-14" parsed at midnight reads as 41h old at 17:00 the next day,
so a naive >48h... no, a naive >24h gate rejects a role that may be 17h old.
82% of the corpus is date-only, so this is the common case, not the edge case.

Run: python pipeline/test_age.py   (no network, no DB, no LLM)
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import db  # noqa: E402

PASS = FAIL = 0
NOW = datetime(2026, 8, 15, 17, 0, 0, tzinfo=timezone.utc)


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"FAIL  {name}" + (f" — {detail}" if detail else ""))


def sweep_against_a_real_db() -> None:
    """freshness.sweep must pass first_seen through, and touch nothing else.

    The pure gate is covered above; what this pins is the WIRING and the blast
    radius. `first_seen` is written by db.insert_job at insert time, so the only
    way to age a row is to rewrite the column -- and every offset here is
    relative to the real clock rather than a hardcoded date, which is what stops
    this test rotting into a scheduled false alarm (LEARNINGS 2026-08-17).
    """
    import tempfile
    from datetime import timedelta

    import freshness
    import ingest_scores

    now = datetime.now(timezone.utc)

    def ago(**kw):
        return (now - timedelta(**kw)).strftime("%Y-%m-%dT%H:%M:%SZ")

    print("-- the sweep, wired --")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        conn = db.connect(Path(tmp) / "t.sqlite")
        qdir = Path(tmp) / "queue"
        qdir.mkdir()
        saved_q, saved_sc = freshness.QUEUE_DIR, ingest_scores.SCORES_DIR
        freshness.QUEUE_DIR = qdir
        ingest_scores.SCORES_DIR = Path(tmp) / "scores"
        try:
            # Every row carries the SAME fuzzy posting date, so the only thing
            # that can separate them is when we first saw them.
            yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
            made = {}
            for name, status, posted, seen in (
                    ("fresh", "new", yesterday, ago(hours=2)),
                    ("aged", "new", yesterday, ago(days=5)),
                    ("nodate_fresh", "new", "", ago(hours=2)),
                    ("nodate_aged", "scored", "", ago(days=5)),
                    ("never_seen", "new", "", ""),
                    ("tailored", "tailored", yesterday, ago(days=5)),
                    ("applied", "applied", yesterday, ago(days=5))):
                jid = db.insert_job(conn, "Acme", f"Product Manager {name}",
                                    "Boston, MA", "https://x", "test",
                                    posted_at=posted, status=status)
                conn.execute("UPDATE jobs SET first_seen = ? WHERE id = ?", (seen, jid))
                (qdir / f"{jid}.json").write_text("{}", encoding="utf-8")
                made[name] = jid
            conn.commit()

            swept = {r["id"] for r in freshness.sweep(conn, apply=True)}
            got = {n for n, jid in made.items() if jid in swept}
            check("only the rows an observation proves old are swept",
                  got == {"aged", "nodate_aged"}, sorted(got))
            status = dict(conn.execute("SELECT id, status FROM jobs"))
            check("a tailored row is never swept", status[made["tailored"]], "tailored")
            check("an applied row is never swept", status[made["applied"]], "applied")
            check("a row with no date and no sighting survives",
                  status[made["never_seen"]], "new")
            check("a swept row lands in stale_skip", status[made["aged"]], "stale_skip")
            check("a swept row loses its queue file",
                  not (qdir / f"{made['aged']}.json").exists())
            check("a surviving row keeps its queue file",
                  (qdir / f"{made['fresh']}.json").exists())
            conn.close()
        finally:
            freshness.QUEUE_DIR, ingest_scores.SCORES_DIR = saved_q, saved_sc


def main() -> int:
    print("-- shape handling --")
    b, w, p = db.job_age("2026-08-15T09:00:00+00:00", now=NOW)
    check("ISO with offset is exact", (round(b, 1), round(w, 1), p) == (8.0, 8.0, "exact"),
          f"{b},{w},{p}")

    b, w, p = db.job_age("2026-08-15T05:00:00-04:00", now=NOW)
    check("non-UTC offset normalized (09:00Z)", round(b, 1) == 8.0 and p == "exact", f"{b},{p}")

    b, w, p = db.job_age("2026-08-14T16:28:16.614+00:00", now=NOW)
    check("Ashby millisecond form is exact", round(b, 1) == 24.5 and p == "exact", f"{b},{p}")

    ms = str(int(datetime(2026, 8, 14, 17, 0, tzinfo=timezone.utc).timestamp() * 1000))
    b, w, p = db.job_age(ms, now=NOW)
    check("Lever epoch-ms is exact", round(b, 1) == 24.0 and p == "exact", f"{b},{p}")

    b, w, p = db.job_age("", now=NOW)
    check("empty is unknown", (b, w, p) == (None, None, "unknown"))

    b, w, p = db.job_age("not-a-date", now=NOW)
    check("garbage is unknown", (b, w, p) == (None, None, "unknown"))

    print("-- date-only is an INTERVAL, not a number --")
    b, w, p = db.job_age("2026-08-15", now=NOW)
    check("today: 0h to 17h", (round(b), round(w), p) == (0, 17, "day"), f"{b},{w}")

    b, w, p = db.job_age("2026-08-14", now=NOW)
    check("yesterday: 17h to 41h (straddles 24h)", (round(b), round(w), p) == (17, 41, "day"),
          f"{b},{w}")

    b, w, p = db.job_age("2026-08-13", now=NOW)
    check("two days: 41h to 65h (straddles 48h)", (round(b), round(w), p) == (41, 65, "day"),
          f"{b},{w}")

    check("interval width is exactly 24h", all(
        abs((db.job_age(d, now=NOW)[1] - db.job_age(d, now=NOW)[0]) - 24) < 0.01
        for d in ("2026-08-14", "2026-08-13", "2026-08-01")))

    check("today's lower bound floors at 0, never negative",
          db.job_age("2026-08-15", now=NOW)[0] >= 0)

    print("-- the gate: provably stale only --")
    # Pin the clock: these dates are fixed, so reading the real one made the
    # assertions rot into failures once the calendar moved past them.
    def stale(posted_at):
        return db.is_provably_stale(posted_at, now=NOW)

    check("yesterday is NOT provably stale (could be 17h old)", not stale("2026-08-14"))
    check("2 days ago is NOT provably stale (could be 41h old)", not stale("2026-08-13"))
    check("3 days ago IS provably stale", stale("2026-08-12"))
    check("unknown is never stale (doctrine)", not stale("") and not stale("junk"))

    from datetime import timedelta
    old = (NOW - timedelta(hours=49)).isoformat()
    fresh = (NOW - timedelta(hours=47)).isoformat()
    check("exact 49h old is stale at the 48h threshold",
          db.job_age(old, now=NOW)[0] > db.STALE_HOURS, str(db.job_age(old, now=NOW)))
    check("exact 47h old is not stale", db.job_age(fresh, now=NOW)[0] < db.STALE_HOURS)

    print("-- the 72h fetch window keeps what the 48h gate would judge later --")
    for d in ("2026-08-15", "2026-08-14", "2026-08-13"):
        b, _, _ = db.job_age(d, now=NOW)
        check(f"{d} survives the fetch window", b <= db.FETCH_WINDOW_HOURS)
    check("2026-08-11 is dropped at fetch (best case 89h)",
          db.job_age("2026-08-11", now=NOW)[0] > db.FETCH_WINDOW_HOURS)

    print("-- observation is proof of age: first_seen is a second lower bound --")
    # A posting we recorded 5 days ago is at least 5 days old, whatever its
    # posting date claims. That bound is airtight and free, and it is the only
    # thing that can decide a row whose posting date is a 24h-wide interval or
    # missing entirely. It tightens `best` ONLY -- it can prove a row is old and
    # can never prove one is young, so `worst` never moves down from it.
    fs_5d = (NOW - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fs_2h = (NOW - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")

    b, w, p = db.job_age("2026-08-14", fs_2h, now=NOW)
    check("a fresh sighting tightens nothing (posted_at already says more)",
          (round(b), round(w), p) == (17, 41, "day"), f"{b},{w},{p}")

    b, w, p = db.job_age("2026-08-14", fs_5d, now=NOW)
    check("an old sighting raises best above the posting date's own bound",
          round(b) == 120 and p == "day", f"{b},{w},{p}")
    check("best never exceeds worst, even when the sighting predates the date",
          b <= w, f"best={b} worst={w}")

    b, w, p = db.job_age("", fs_5d, now=NOW)
    check("no date + a known sighting is precision 'seen', not 'unknown'",
          (round(b), w, p) == (120, None, "seen"), f"{b},{w},{p}")

    b, w, p = db.job_age("", fs_2h, now=NOW)
    check("a 'seen' row's upper bound stays unknown (None), never invented",
          (round(b), w, p) == (2, None, "seen"), f"{b},{w},{p}")

    b, w, p = db.job_age("", "", now=NOW)
    check("no date and never seen is still 'unknown'", (b, w, p) == (None, None, "unknown"))

    check("an unparseable first_seen is ignored, not guessed",
          db.job_age("", "not-a-date", now=NOW) == (None, None, "unknown"))

    # The ingest path calls the one-argument form on rows that have no prior
    # sighting by construction. Pin that the added parameter changed nothing
    # there: a regression would start rejecting brand-new postings at fetch.
    check("the one-argument form is unchanged for every shape", all(
        db.job_age(v, now=NOW) == db.job_age(v, "", now=NOW)
        for v in ("2026-08-14", "2026-08-15T09:00:00Z", "", "junk", ms)))

    print("-- the sweep gate with the observation bound --")
    check("fuzzy date, seen 2h ago: NOT stale",
          not db.is_provably_stale("2026-08-14", first_seen=fs_2h, now=NOW))
    check("fuzzy date, seen 5 days ago: IS stale",
          db.is_provably_stale("2026-08-14", first_seen=fs_5d, now=NOW))
    check("no date, seen 5 days ago: IS stale",
          db.is_provably_stale("", first_seen=fs_5d, now=NOW))
    check("no date, seen 2h ago: NOT stale",
          not db.is_provably_stale("", first_seen=fs_2h, now=NOW))
    check("no date, never seen: NEVER stale (doctrine)",
          not db.is_provably_stale("", first_seen="", now=NOW))
    check("first_seen defaults to off, so every old caller is untouched",
          not db.is_provably_stale("2026-08-14", now=NOW))

    print("-- tiers: a lower bound can demote a row, never promote one --")
    # The bound proves age, so it can only ever move a row DOWN the board. A
    # no-date row seen 2h ago knows nothing about when it was posted; calling it
    # "apply now" would be the exact inverse of what this bound is for.
    def tier(posted_at, first_seen=""):
        best, _worst, precision = db.job_age(posted_at, first_seen, now=NOW)
        return db.age_tier(best, precision)

    check("seen-recently is 'nodate', never 'now'", tier("", fs_2h) == "nodate",
          tier("", fs_2h))
    check("seen-long-ago is 'stale'", tier("", fs_5d) == "stale", tier("", fs_5d))
    check("no date at all is 'nodate'", tier("", "") == "nodate")
    check("a genuinely fresh posting date is still 'now'", tier("2026-08-15") == "now")
    check("the observation bound demotes a fuzzy row to 'stale'",
          tier("2026-08-14", fs_5d) == "stale", tier("2026-08-14", fs_5d))

    print("-- labels --")
    check("date-only renders as a range", db.age_label(*db.job_age("2026-08-14", now=NOW)) == "17-41h",
          db.age_label(*db.job_age("2026-08-14", now=NOW)))
    check("exact renders as a real age reading",
          db.age_label(*db.job_age("2026-08-15T09:00:00Z", now=NOW)) == "posted 8h ago",
          db.age_label(*db.job_age("2026-08-15T09:00:00Z", now=NOW)))
    check("unknown says so", db.age_label(*db.job_age("", now=NOW)) == "age unknown")
    check("old renders in days", db.age_label(*db.job_age("2026-07-15T09:00:00Z", now=NOW)).endswith("d ago"))
    check("'seen' is worded as a sighting, not as a posting date",
          db.age_label(*db.job_age("", fs_5d, now=NOW)) == "seen 5d ago",
          db.age_label(*db.job_age("", fs_5d, now=NOW)))
    # Past 72h the day branch has always collapsed to whole days; what matters
    # is that the tightened row stops advertising the "17-41h" it no longer has.
    check("a 'day' row re-labels once the sighting proves it old",
          db.age_label(*db.job_age("2026-08-14", fs_5d, now=NOW)) == "5d",
          db.age_label(*db.job_age("2026-08-14", fs_5d, now=NOW)))
    check("a 'day' row still renders an INTERVAL while it has one",
          db.age_label(*db.job_age("2026-08-14", fs_2h, now=NOW)) == "17-41h")

    print("-- status vocabulary --")
    check("stale_skip is a valid status", "stale_skip" in db.STATUSES)
    check("needs_review is a valid status", "needs_review" in db.STATUSES)

    sweep_against_a_real_db()

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
