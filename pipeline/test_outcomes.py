"""Tests for outcome capture. Run: python pipeline/test_outcomes.py

Phase 6 added a `status_history` table so the pipeline can answer "what came
back" instead of only "where does this row stand now". Four properties carry the
design and every one of them is easy to break by accident:

  1. Only TRACKED_STATUSES transitions are recorded. Recording every status
     write is ~500 rows/day inside a binary committed ~25x a day -- the shape
     that took .git to 166 MB in four days. The consequence stated positively:
     NO PATH OUTSIDE set_status CAN PRODUCE A TRACKED TRANSITION, which is what
     makes filter.py's raw UPDATE a non-case rather than an exemption. That is
     asserted here, not left to a comment.
  2. The backfill is deterministic. It stamps `first_seen`, a stored column, so
     the local seat, the Actions cron and CI produce byte-identical rows in a
     file all of them commit.
  3. A backfilled row is a LOWER bound on when Vaish applied (detection precedes
     applying), so it may count in a funnel and may never drive a nudge. Nudging
     off a lower bound announces that a 3-day-old application has been silent
     for three weeks.
  4. `rejected` means two different things depending on whether an application
     happened. Both live `rejected` rows today are Vaish rejecting a POSTING;
     only a rejection carrying an `applied` transition is an employer's.

The nudge cannot fire against real data on the day it ships, so its fixture is
built here deliberately: a defence you have only ever seen pass is untested
(LEARNINGS 2026-08-19, 2026-08-30).

Temp DBs throughout: no network, no LLM, and pipeline/jobs.sqlite is opened once,
read-only, to assert every live `source` value maps to a real channel.
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import db  # noqa: E402
import filter as jobfilter  # noqa: E402
import scrape  # noqa: E402

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}  (got {got!r}, want {want!r})")
        FAILS.append(label)


def fresh_db(tmp: Path, name: str = "t.sqlite"):
    return db.connect(tmp / name)


def hist(conn, jid: str | None = None) -> list:
    sql = "SELECT id, status, at, via FROM status_history"
    args = ()
    if jid:
        sql += " WHERE id = ?"
        args = (jid,)
    return [tuple(r) for r in conn.execute(sql + " ORDER BY at, status", args)]


def add(conn, company="TestCo", title="Product Manager", location="Boston, MA",
        source="greenhouse", status="new"):
    return db.insert_job(conn, company, title, location, url="",
                         source=source, status=status)


def iso(dt) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# --- migration + backfill ---------------------------------------------------

def test_migration_and_backfill():
    print("\n-- migration + backfill --")
    tmp = Path(tempfile.mkdtemp(prefix="outcomes-"))
    try:
        conn = fresh_db(tmp)
        check("status_history exists on a fresh DB",
              bool(conn.execute("SELECT name FROM sqlite_master WHERE"
                                " name='status_history'").fetchone()), True)

        # A DB that predates the table: build one without it, then migrate.
        raw = sqlite3.connect(tmp / "old.sqlite")
        raw.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, company TEXT,"
                    " company_norm TEXT, title TEXT, title_norm TEXT,"
                    " location TEXT, loc_bucket TEXT, url TEXT, source TEXT,"
                    " jd_text TEXT, posted_at TEXT, first_seen TEXT,"
                    " status TEXT, notes TEXT)")
        raw.execute("INSERT INTO jobs VALUES ('abc','C','c','T','t',"
                    "'','','', 'greenhouse','','','2026-08-01T00:00:00Z',"
                    "'applied','')")
        raw.commit()
        raw.close()
        conn2 = db.connect(tmp / "old.sqlite")
        check("migrating an old DB creates the table", len(hist(conn2)), 1)
        check("backfill stamps first_seen and via=backfill",
              hist(conn2)[0], ("abc", "applied", "2026-08-01T00:00:00Z", "backfill"))

        conn2.close()
        conn3 = db.connect(tmp / "old.sqlite")
        check("a second connect() adds nothing", len(hist(conn3)), 1)
        conn3.close()

        # A row whose application was recorded properly is never re-backfilled.
        jid = add(conn, status="tailored")
        db.set_status(conn, jid, "applied", at="2026-08-20T09:00:00Z", via="mark")
        conn.close()
        conn = db.connect(tmp / "t.sqlite")
        rows = [r for r in hist(conn, jid) if r[1] == "applied"]
        check("an already-recorded applied row is not backfilled", len(rows), 1)
        check("...and keeps the recorded via", rows[0][3], "mark")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --- the tracked-transition rule --------------------------------------------

def test_only_tracked_transitions_are_recorded():
    print("\n-- tracked transitions --")
    tmp = Path(tempfile.mkdtemp(prefix="outcomes-"))
    try:
        conn = fresh_db(tmp)

        a = add(conn, title="A Manager")
        db.set_score(conn, a, 9, "auto-tailor; fine")
        check("new -> scored records nothing", hist(conn, a), [])
        db.set_status(conn, a, "tailored", via="sync")
        check("scored -> tailored records", len(hist(conn, a)), 1)
        # The loss Phase 7 left open: a finished resume swept before its sidecar
        # folded. Untracked on the NEW side, tracked on the old -- kept.
        db.set_status(conn, a, "stale_skip", via="freshness")
        check("tailored -> stale_skip records (old side tracked)",
              [r[1] for r in hist(conn, a)], ["stale_skip", "tailored"])

        b = add(conn, title="B Manager")
        db.set_status(conn, b, "skipped", via="x")
        db.set_status(conn, b, "new", via="x")
        db.set_status(conn, b, "stale_skip", via="freshness")
        db.set_status(conn, b, "new", via="x")
        db.set_status(conn, b, "baseline_skip", via="baseline")
        check("no untracked-to-untracked transition records", hist(conn, b), [])

        c = add(conn, title="C Manager", status="applied")
        db.set_status(conn, c, "applied", via="mark")
        check("re-marking the status a row already holds records nothing",
              hist(conn, c), [])
        db.set_status(conn, c, "interview", at="2026-09-01T10:00:00Z",
                      via="dashboard")
        check("applied -> interview records with the given at and via",
              hist(conn, c)[-1][2:], ("2026-09-01T10:00:00Z", "dashboard"))
        db.set_status(conn, c, "interview", at="2026-09-01T10:00:00Z",
                      via="dashboard")
        check("replaying the identical transition is a no-op",
              len([r for r in hist(conn, c) if r[1] == "interview"]), 1)

        try:
            db.set_status(conn, "no-such-id", "applied")
            raised = False
        except ValueError:
            raised = True
        check("set_status on an unknown id raises rather than writing nowhere",
              raised, True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_no_path_outside_set_status_makes_a_tracked_transition():
    """filter.py writes status with raw SQL. Under the tracked rule that is a
    non-case, not an exemption -- assert it rather than trusting the comment."""
    print("\n-- the raw-UPDATE writers --")
    tmp = Path(tempfile.mkdtemp(prefix="outcomes-"))
    try:
        conn = fresh_db(tmp)
        jid = add(conn, title="Marketing Product Manager")
        # Exactly filter.sweep's statement (filter.py: the hard-filter retro-sweep).
        conn.execute("UPDATE jobs SET status = 'skipped', notes = ? WHERE id = ?",
                     ("hard-filter:marketing", jid))
        conn.commit()
        check("filter's raw skip write leaves no history", hist(conn, jid), [])
        check("...and the row really did move",
              conn.execute("SELECT status FROM jobs WHERE id = ?",
                           (jid,)).fetchone()[0], "skipped")

        # set_score's atomic status write goes through the same recorder, so a
        # TRACKED row that is ever re-scored records correctly.
        j2 = add(conn, title="Rescored Manager", status="needs_review")
        db.set_score(conn, j2, 8, "auto-tailor; x")
        check("re-scoring a tracked row records the transition",
              [r[1] for r in hist(conn, j2)], ["scored"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --- channels ---------------------------------------------------------------

def test_channel_of():
    print("\n-- channel_of --")
    check("the ATS tuple is exactly scrape.FETCHERS",
          set(db.ATS_SOURCES), set(scrape.FETCHERS))
    for s in scrape.FETCHERS:
        check(f"{s} -> ats", db.channel_of(s), "ats")
    for site in ("indeed", "google", "linkedin"):
        check(f"jobspy:{site} -> aggregator",
              db.channel_of(f"jobspy:{site}"), "aggregator")
    for feed in scrape.JOBRIGHT_FEEDS:
        check(f"{feed['source']} -> curated",
              db.channel_of(feed["source"]), "curated")
    for src in ("getro:accel", "consider:gv"):
        check(f"{src} -> startup", db.channel_of(src), "startup")
    check("seed -> seed", db.channel_of("seed"), "seed")
    check("an unknown source is named, not guessed",
          db.channel_of("monster"), "other")
    check("empty source does not crash", db.channel_of(""), "other")

    # The live corpus is the only thing that proves the mapping covers reality.
    if db.DB_PATH.exists():
        live = db.connect_readonly(db.DB_PATH)
        unmapped = sorted({r[0] for r in live.execute("SELECT DISTINCT source FROM jobs")
                           if db.channel_of(r[0]) == "other"})
        live.close()
        check("every source in the live DB maps to a real channel", unmapped, [])


# --- the funnel -------------------------------------------------------------

def test_funnel():
    print("\n-- outcome funnel --")
    tmp = Path(tempfile.mkdtemp(prefix="outcomes-"))
    try:
        conn = fresh_db(tmp)

        seed = add(conn, company="SeedCo", title="Old PM", source="seed",
                   status="tailored")
        db.set_status(conn, seed, "applied", via="mark")
        check("seed rows are excluded from the funnel",
              db.outcome_funnel(conn).get("seed"), None)

        # A posting Vaish rejected without ever applying. It must NOT count as a
        # rejection in the funnel -- that arm means "an employer said no".
        r = add(conn, company="DupCo", title="Product Owner I", status="tailored")
        db.set_status(conn, r, "rejected", via="mark")
        f = db.outcome_funnel(conn)
        check("a rejection with no application is not in the funnel at all",
              f.get("ats"), None)

        g = add(conn, company="GhCo", title="Platform PM", status="tailored")
        db.set_status(conn, g, "applied", via="mark")
        db.set_status(conn, g, "rejected", via="mark")
        f = db.outcome_funnel(conn)["ats"]
        check("a rejection after an application counts", f["rejected"], 1)
        check("...and the application still counts", f["applied"], 1)
        check("...and it is a recorded one", f["recorded"], 1)

        i = add(conn, company="IndCo", title="Growth PM",
                source="jobspy:indeed", status="applied")
        conn.close()
        conn = db.connect(tmp / "t.sqlite")     # backfills IndCo
        f = db.outcome_funnel(conn)["aggregator"]
        check("a backfilled application counts in applied", f["applied"], 1)
        check("...but never in recorded", f["recorded"], 0)

        db.set_status(conn, i, "interview", via="dashboard")
        db.set_status(conn, i, "rejected", via="dashboard")
        f = db.outcome_funnel(conn)["aggregator"]
        check("a row that reached interview then rejected counts in both",
              (f["applied"], f["interview"], f["rejected"]), (1, 1, 1))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --- the nudge --------------------------------------------------------------

def test_nudge():
    """The list is empty against real data on the day it ships, so it gets a
    fixture that makes it fire -- otherwise it is a defence never seen work."""
    print("\n-- no-outcome-yet nudge --")
    tmp = Path(tempfile.mkdtemp(prefix="outcomes-"))
    try:
        conn = fresh_db(tmp)
        now = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)

        old = add(conn, company="QuietCo", title="Silent PM", status="tailored")
        db.set_status(conn, old, "applied", at=iso(now - timedelta(days=15)),
                      via="mark")
        recent = add(conn, company="FreshCo", title="Recent PM", status="tailored")
        db.set_status(conn, recent, "applied", at=iso(now - timedelta(days=13)),
                      via="mark")
        moved = add(conn, company="MovedCo", title="Moving PM", status="tailored")
        db.set_status(conn, moved, "applied", at=iso(now - timedelta(days=30)),
                      via="mark")
        db.set_status(conn, moved, "interview", at=iso(now - timedelta(days=2)),
                      via="dashboard")

        got = db.no_outcome_yet(conn, now=now)
        check("a 15-day-silent application is nudged",
              [d["company"] for d in got], ["QuietCo"])
        check("...with the right elapsed days", got[0]["days"], 15)

        # The load-bearing exclusion: same age, but the only timestamp is
        # first_seen, which is when the JOB was found, not when he applied.
        back = db.insert_job(conn, "BackCo", "Backfilled PM", "Boston, MA",
                             url="", source="greenhouse", status="applied")
        conn.execute("UPDATE jobs SET first_seen = ? WHERE id = ?",
                     (iso(now - timedelta(days=40)), back))
        conn.commit()
        conn.close()
        conn = db.connect(tmp / "t.sqlite")
        check("the backfilled row really is 40 days old",
              conn.execute("SELECT at FROM status_history WHERE id = ?",
                           (back,)).fetchone()[0], iso(now - timedelta(days=40)))
        check("a backfilled application is never nudged",
              [d["company"] for d in db.no_outcome_yet(conn, now=now)], ["QuietCo"])

        # A row that has moved on is not an open application.
        check("a row past applied is not nudged",
              "MovedCo" in [d["company"] for d in db.no_outcome_yet(conn, now=now)],
              False)
        check("a 13-day-old application is not nudged yet",
              "FreshCo" in [d["company"] for d in db.no_outcome_yet(conn, now=now)],
              False)

        text = db.outcomes_report(conn, now=now)
        check("the report is ASCII (Task Scheduler pipes through cmd.exe)",
              text.isascii(), True)
        check("the report names the nudged role", "QuietCo" in text, True)
        check("no rate is printed below the floor", "%" in text, False)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    test_migration_and_backfill()
    test_only_tracked_transitions_are_recorded()
    test_no_path_outside_set_status_makes_a_tracked_transition()
    test_channel_of()
    test_funnel()
    test_nudge()

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILURE(S): {', '.join(FAILS)}")
        return 1
    print("all outcome-capture tests pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
