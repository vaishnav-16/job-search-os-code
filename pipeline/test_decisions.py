"""Tests for the dashboard decision fold. Run: python pipeline/test_decisions.py

The contract being guarded: the browser appends one entry per decision to
decisions/pending.json, and this fold is the ONLY thing that turns those into
DB writes. Three rules carry the design and every one of them is easy to break
by accident:

  1. The fold is idempotent. sync.py's push retry replays fold() verbatim after
     a `reset --hard origin/main`, which restores both the DB and the pending
     file to their pre-run state. A second fold must reproduce the first
     BYTE FOR BYTE, or a promote marker doubles and a rationale drifts.
  2. pending.json is rewritten, never deleted. sync.stage_and_commit() names the
     file, so an unlinked path fails `git add` and kills every later sync.
  3. Malformed entries stay in the file. They are Vaish's to fix; silently
     dropping them loses a decision he believes he recorded.

Temp dirs + a temp DB throughout: no network, no LLM, and pipeline/jobs.sqlite
is never opened.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import db  # noqa: E402
import decisions  # noqa: E402
import ingest_scores  # noqa: E402

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}  (got {got!r}, want {want!r})")
        FAILS.append(label)


def fresh_env():
    """Throwaway decisions/, queue/, scores/ and DB. Returns (tmp, conn)."""
    tmp = Path(tempfile.mkdtemp(prefix="decisions-test-"))
    for sub in ("decisions", "queue", "scores"):
        (tmp / sub).mkdir()
    decisions.PENDING_PATH = tmp / "decisions" / "pending.json"
    decisions.QUEUE_DIR = tmp / "queue"
    ingest_scores.SCORES_DIR = tmp / "scores"
    ingest_scores.QUEUE_DIR = tmp / "queue"
    decisions.write_pending([])
    return tmp, db.connect(tmp / "t.sqlite")


def add_job(conn, jid, status, tmp, notes="", score=None, with_queue=True):
    conn.execute(
        "INSERT OR REPLACE INTO jobs (id, company, company_norm, title, title_norm, "
        "location, url, source, jd_text, posted_at, first_seen, status, score, notes) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (jid, "Acme", "acme", "PM", "pm", "NY", "u", "greenhouse", "jd",
         "2026-08-20", db.now_iso(), status, score, notes))
    conn.commit()
    if with_queue:
        (tmp / "queue" / f"{jid}.json").write_text("{}", encoding="utf-8")


def put(entries):
    decisions.write_pending(entries)


def entry(jid, action, at="2026-08-20T14:03:00Z"):
    return {"id": jid, "action": action, "at": at}


def hours_ago(h):
    return (datetime.now(timezone.utc) - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")


def row_of(conn, jid):
    r = conn.execute("SELECT status, notes FROM jobs WHERE id=?", (jid,)).fetchone()
    return (r[0], r[1]) if r else None


A, B, C, D = "a" * 40, "b" * 40, "c" * 40, "d" * 40


# ------------------------------------------------------------- idempotency

def test_apply_twice_is_idempotent():
    print("apply twice changes nothing the second time")
    tmp, conn = fresh_env()
    try:
        add_job(conn, A, "scored", tmp, notes="digest; solid platform fit", score=6)
        add_job(conn, B, "new", tmp)
        put([entry(A, "promote"), entry(B, "rejected", "2026-08-20T15:00:00Z")])

        decisions.apply(conn, apply=True)
        rows1 = (row_of(conn, A), row_of(conn, B))
        bytes1 = decisions.PENDING_PATH.read_bytes()

        decisions.apply(conn, apply=True)
        rows2 = (row_of(conn, A), row_of(conn, B))

        check("promote rewrote the lane once",
              rows1[0], ("scored", "auto-tailor; promoted via dashboard 2026-08-20;"
                         " solid platform fit"))
        check("mark landed", rows1[1], ("rejected", ""))
        check("second apply leaves the rows byte-identical", rows2, rows1)
        check("second apply leaves pending.json byte-identical",
              decisions.PENDING_PATH.read_bytes(), bytes1)
        check("both entries were consumed", bytes1, b"[]\n")
        check("no CRLF in the written file", b"\r\n" in bytes1, False)
    finally:
        conn.close()
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_fold_records_the_browsers_own_timestamp():
    """A click travels through git and lands at the next sync, up to hours later.

    If the transition were stamped at fold time, every dashboard-recorded outcome
    would look like it happened when the laptop woke up -- which is the one thing
    a funnel measuring elapsed time cannot tolerate. So `at` comes off the entry.
    """
    print("a folded decision records the browser's `at`, not fold time")
    tmp, conn = fresh_env()
    try:
        add_job(conn, A, "applied", tmp)
        clicked = "2026-08-20T14:03:00Z"
        put([entry(A, "interview", clicked)])

        decisions.apply(conn, apply=True)
        rows = [tuple(r) for r in conn.execute(
            "SELECT status, at, via FROM status_history WHERE id = ?", (A,))]
        check("one history row, carrying the click's own timestamp",
              rows, [("interview", clicked, "dashboard")])

        # sync.push_with_retry replays the fold verbatim after a reset. Here the
        # DB is NOT reset, which is the harsher case: the guard that has to hold
        # is "the status did not change", not the UNIQUE constraint.
        decisions.apply(conn, apply=True)
        again = conn.execute(
            "SELECT COUNT(*) FROM status_history WHERE id = ?", (A,)).fetchone()[0]
        check("a replayed fold does not inflate the funnel", again, 1)

        # /mark writes the same entry tagged via=mark (pipeline/mark.py), so the
        # funnel can still tell a hand mark from a click.
        put([dict(entry(A, "offer", "2026-08-21T09:00:00Z"), via="mark")])
        decisions.apply(conn, apply=True)
        last = conn.execute("SELECT via FROM status_history WHERE id = ? AND status = 'offer'",
                            (A,)).fetchone()
        check("a /mark entry is recorded via mark", last and last[0], "mark")
    finally:
        conn.close()
        shutil.rmtree(tmp, ignore_errors=True)


def test_refold_after_reset_hard():
    print("refold after a push-race reset reproduces the same end state")
    tmp, conn = fresh_env()
    try:
        add_job(conn, A, "scored", tmp, notes="digest; solid platform fit", score=6)
        put([entry(A, "promote")])
        before = decisions.PENDING_PATH.read_bytes()

        decisions.apply(conn, apply=True)
        after_first = (row_of(conn, A), decisions.PENDING_PATH.read_bytes())

        # `git reset --hard origin/main` restores the committed pending file.
        decisions.PENDING_PATH.write_bytes(before)
        decisions.apply(conn, apply=True)

        check("refold reproduces the notes exactly",
              row_of(conn, A), after_first[0])
        check("refold reproduces pending.json exactly",
              decisions.PENDING_PATH.read_bytes(), after_first[1])
    finally:
        conn.close()
        shutil.rmtree(tmp, ignore_errors=True)


# ----------------------------------------------------------------- promote

def test_foldable_only_when_a_fold_would_change_something():
    print("detect dispatches a fold only when one would apply or drop an entry (W7)")
    tmp, conn = fresh_env()
    try:
        check("empty file: no fold", decisions.foldable(decisions.plan(conn)), False)
        add_job(conn, A, "new", tmp)
        put([entry(A, "promote")])
        check("a promote waiting for its score: no fold",
              decisions.foldable(decisions.plan(conn)), False)
        put([entry(B, "skipped")])
        check("an id this clone has not seen: no fold",
              decisions.foldable(decisions.plan(conn)), False)
        add_job(conn, B, "scored", tmp, notes="auto-tailor; x", score=8)
        check("a skip on a scored row: fold", decisions.foldable(decisions.plan(conn)), True)
        put([entry(A, "cancel")])
        check("a cancel: fold (it drops the entry)",
              decisions.foldable(decisions.plan(conn)), True)
        check("asking wrote nothing", json.loads(decisions.PENDING_PATH.read_text()),
              [entry(A, "cancel")])
    finally:
        conn.close()
        shutil.rmtree(tmp, ignore_errors=True)


def test_promote_waits_for_the_score():
    print("promote on an unscored row waits instead of guessing")
    tmp, conn = fresh_env()
    try:
        add_job(conn, A, "new", tmp)
        put([entry(A, "promote")])

        p = decisions.apply(conn, apply=True)
        check("kept pending while the row is 'new'", len(p["pending"]), 1)
        check("nothing applied", len(p["applied"]), 0)
        check("row untouched", row_of(conn, A), ("new", ""))
        check("entry survives in the file",
              [e["id"] for e in json.loads(decisions.PENDING_PATH.read_text())], [A])

        # The score lands (cloud Routine sidecar, folded in step 3 of the same run).
        db.set_score(conn, A, 6, "digest; solid platform fit")
        p2 = decisions.apply(conn, apply=True)
        check("promotes once the score is in", len(p2["applied"]), 1)
        check("lane rewritten", row_of(conn, A),
              ("scored", "auto-tailor; promoted via dashboard 2026-08-20;"
                         " solid platform fit"))
        check("entry dropped", decisions.PENDING_PATH.read_bytes(), b"[]\n")
    finally:
        conn.close()
        shutil.rmtree(tmp, ignore_errors=True)


def test_promote_on_a_dead_row_is_obsolete():
    print("promote loses to the row's own status")
    tmp, conn = fresh_env()
    try:
        add_job(conn, A, "stale_skip", tmp, notes="digest; too old")
        add_job(conn, B, "tailored", tmp, notes="auto-tailor; shipped")
        put([entry(A, "promote"), entry(B, "promote")])

        p = decisions.apply(conn, apply=True)
        reasons = " | ".join(why for _, why in p["obsolete"])
        check("both reported obsolete", len(p["obsolete"]), 2)
        check("the reason names the current status",
              "stale_skip" in reasons and "tailored" in reasons, True)
        check("notes untouched", (row_of(conn, A)[1], row_of(conn, B)[1]),
              ("digest; too old", "auto-tailor; shipped"))
        check("both entries dropped", decisions.PENDING_PATH.read_bytes(), b"[]\n")
    finally:
        conn.close()
        shutil.rmtree(tmp, ignore_errors=True)


def test_promote_revives_a_skip_only_with_its_queue_file_restored():
    print("promote on a skipped row revives it only when the queue file is back")
    tmp, conn = fresh_env()
    try:
        add_job(conn, A, "skipped", tmp, notes="digest; lead title", score=9, with_queue=False)
        add_job(conn, B, "skipped", tmp, notes="digest-stretch; ai fit", score=8)
        conn.execute("UPDATE jobs SET jd_text = '' WHERE id IN (?, ?)", (A, B))
        conn.commit()
        (tmp / "queue" / f"{B}.json").write_text(
            json.dumps({"id": B, "jd_markdown": "the restored JD"}), encoding="utf-8")
        put([entry(A, "promote"), entry(B, "promote")])

        p = decisions.apply(conn, apply=True)
        check("no queue file: obsolete, row untouched", row_of(conn, A),
              ("skipped", "digest; lead title"))
        check("restored queue file: revived into the tailor lane", row_of(conn, B),
              ("scored", "auto-tailor; promoted via dashboard 2026-08-20; ai fit"))
        check("JD restored from the queue file", conn.execute(
            "SELECT jd_text FROM jobs WHERE id=?", (B,)).fetchone()[0], "the restored JD")
        check("one applied, one obsolete", (len(p["applied"]), len(p["obsolete"])), (1, 1))

        # The push retry replays the fold; the second pass changes nothing.
        before = conn.execute("SELECT * FROM jobs WHERE id=?", (B,)).fetchone()
        put([entry(B, "promote")])
        decisions.apply(conn, apply=True)
        check("replay is byte-identical", conn.execute(
            "SELECT * FROM jobs WHERE id=?", (B,)).fetchone(), before)
    finally:
        conn.close()
        shutil.rmtree(tmp, ignore_errors=True)


def test_promote_into_the_lane_the_row_already_holds_is_obsolete():
    print("promote on an already-auto-tailor row is a no-op, not a notes rewrite")
    tmp, conn = fresh_env()
    try:
        # The scorer lanes every 8+ here itself, and it is also the post-promote
        # state, so this is the common case rather than an edge one.
        add_job(conn, A, "scored", tmp, notes="auto-tailor; strong platform fit, 9 total",
                score=9)
        put([entry(A, "promote")])

        p = decisions.apply(conn, apply=True)
        check("classified obsolete, not applied", len(p["applied"]), 0)
        check("the reason names the lane",
              [why for _, why in p["obsolete"]],
              ["row is already in the auto-tailor lane"])
        check("no marker stacked onto the notes", row_of(conn, A),
              ("scored", "auto-tailor; strong platform fit, 9 total"))
        check("entry dropped", decisions.PENDING_PATH.read_bytes(), b"[]\n")

        # A day later, same click again: still obsolete, still byte-identical.
        put([entry(A, "promote", "2026-08-21T09:00:00Z")])
        decisions.apply(conn, apply=True)
        check("a second day's promote adds no second marker", row_of(conn, A),
              ("scored", "auto-tailor; strong platform fit, 9 total"))

        # A row in any other lane still promotes.
        add_job(conn, B, "scored", tmp, notes="digest-stretch; senior title", score=8)
        put([entry(B, "promote")])
        p2 = decisions.apply(conn, apply=True)
        check("a digest-stretch row still promotes", len(p2["applied"]), 1)
        check("lane rewritten", row_of(conn, B),
              ("scored", "auto-tailor; promoted via dashboard 2026-08-20; senior title"))
    finally:
        conn.close()
        shutil.rmtree(tmp, ignore_errors=True)


def test_promoted_lane_reaches_the_sidecar():
    print("a promoted lane survives the sidecar reconcile the cloud tailor reads")
    tmp, conn = fresh_env()
    try:
        import sync
        add_job(conn, A, "scored", tmp, notes="digest; solid platform fit", score=6)
        ingest_scores.write_sidecar(A, 6, "digest; solid platform fit")
        put([entry(A, "promote")])
        decisions.apply(conn, apply=True)

        was_root = sync.ROOT
        sync.ROOT = tmp
        try:
            rec = sync.reconcile_sidecars(conn, apply=True)
        finally:
            sync.ROOT = was_root

        note = json.loads(ingest_scores.sidecar_path(A).read_text(encoding="utf-8"))["note"]
        check("reconcile saw the lane change", len(rec["relaned"]), 1)
        check("sidecar lane now agrees with the DB", sync.lane_of(note), "auto-tailor")
        check("marker carried into the sidecar",
              "promoted via dashboard 2026-08-20" in note, True)
        check("original rationale preserved", "solid platform fit" in note, True)
    finally:
        conn.close()
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------- marks

def test_applied_clears_queue_and_sidecar():
    print("applied/rejected/skipped clear the queue file and the sidecar (as /mark does)")
    tmp, conn = fresh_env()
    try:
        add_job(conn, A, "scored", tmp, notes="auto-tailor; strong", score=9)
        ingest_scores.write_sidecar(A, 9, "auto-tailor; strong")
        put([entry(A, "applied")])

        decisions.apply(conn, apply=True)
        check("status set", row_of(conn, A)[0], "applied")
        check("queue file gone", (tmp / "queue" / f"{A}.json").exists(), False)
        check("sidecar gone", ingest_scores.sidecar_path(A).exists(), False)
        check("entry dropped", decisions.PENDING_PATH.read_bytes(), b"[]\n")
        check("PENDING FILE STILL EXISTS (git add would fail otherwise)",
              decisions.PENDING_PATH.exists(), True)
    finally:
        conn.close()
        shutil.rmtree(tmp, ignore_errors=True)


def test_interview_and_offer_keep_the_work_set():
    print("interview/offer set status only")
    tmp, conn = fresh_env()
    try:
        add_job(conn, A, "applied", tmp, notes="auto-tailor; strong", score=9)
        ingest_scores.write_sidecar(A, 9, "auto-tailor; strong")
        add_job(conn, B, "applied", tmp)
        put([entry(A, "interview"), entry(B, "offer")])

        decisions.apply(conn, apply=True)
        check("interview set", row_of(conn, A)[0], "interview")
        check("offer set", row_of(conn, B)[0], "offer")
        check("queue file kept", (tmp / "queue" / f"{A}.json").exists(), True)
        check("sidecar kept", ingest_scores.sidecar_path(A).exists(), True)
    finally:
        conn.close()
        shutil.rmtree(tmp, ignore_errors=True)


def test_no_op_entries():
    print("entries with nothing to do are dropped, not applied")
    tmp, conn = fresh_env()
    try:
        add_job(conn, A, "applied", tmp)
        add_job(conn, B, "scored", tmp, notes="digest; fine", score=6)
        put([entry(A, "applied"), entry(C, "rejected"), entry(B, "cancel")])

        p = decisions.apply(conn, apply=True)
        check("status-equal entry is obsolete, not applied", len(p["obsolete"]), 1)
        check("no DB write for it", row_of(conn, A), ("applied", ""))
        check("cancel dropped", [e["id"] for e in p["dropped_cancel"]], [B])
        check("cancel left the row alone", row_of(conn, B), ("scored", "digest; fine"))
        check("id with no row stays pending", [e["id"] for e, _ in p["pending"]], [C])
        check("only the pending one survives in the file",
              [e["id"] for e in json.loads(decisions.PENDING_PATH.read_text())], [C])
    finally:
        conn.close()
        shutil.rmtree(tmp, ignore_errors=True)


# -------------------------------------------------------- file-level rules

def test_duplicate_ids_resolve_to_the_latest():
    print("one entry per id survives: latest 'at' wins")
    tmp, conn = fresh_env()
    try:
        add_job(conn, A, "scored", tmp, notes="digest; fine", score=6)
        put([entry(A, "applied", "2026-08-20T10:00:00Z"),
             entry(A, "skipped", "2026-08-20T12:00:00Z"),
             entry(A, "rejected", "2026-08-20T11:00:00Z")])

        p = decisions.apply(conn, apply=True)
        check("two losers reported", len(p["duplicates"]), 2)
        check("the latest decision is the one applied", row_of(conn, A)[0], "skipped")
        check("all three entries left the file",
              decisions.PENDING_PATH.read_bytes(), b"[]\n")
    finally:
        conn.close()
        shutil.rmtree(tmp, ignore_errors=True)


def test_malformed_entries_are_preserved():
    print("malformed entries are reported and left in the file, never applied")
    tmp, conn = fresh_env()
    try:
        add_job(conn, A, "scored", tmp, notes="digest; fine", score=6)
        bad_id = {"id": "A" * 40, "action": "applied", "at": "2026-08-20T10:00:00Z"}
        bad_action = {"id": B, "action": "delete", "at": "2026-08-20T10:00:00Z"}
        bad_at = {"id": C, "action": "applied", "at": "last tuesday"}
        no_at = {"id": D, "action": "applied"}
        put([entry(A, "applied"), bad_id, bad_action, bad_at, no_at, "not an entry"])

        p = decisions.apply(conn, apply=True)
        left = json.loads(decisions.PENDING_PATH.read_text())
        check("five malformed reported", len(p["malformed"]), 5)
        check("the valid one still applied", row_of(conn, A)[0], "applied")
        check("all five survive in the file", len(left), 5)
        check("the valid one is gone", A in json.dumps(left), False)

        # A file that is not an array cannot be merged with, so it is left alone.
        decisions.PENDING_PATH.write_bytes(b'{"id": "oops"}\n')
        p2 = decisions.apply(conn, apply=True)
        check("non-array file reported", len(p2["malformed"]), 1)
        check("non-array file NOT rewritten",
              decisions.PENDING_PATH.read_bytes(), b'{"id": "oops"}\n')

        decisions.PENDING_PATH.write_bytes(b"{not json\n")
        p3 = decisions.apply(conn, apply=True)
        check("unparseable file reported", len(p3["malformed"]), 1)
        check("unparseable file NOT rewritten",
              decisions.PENDING_PATH.read_bytes(), b"{not json\n")
    finally:
        conn.close()
        shutil.rmtree(tmp, ignore_errors=True)


def test_dry_run_touches_nothing():
    print("dry run reports and writes nothing")
    tmp, conn = fresh_env()
    try:
        add_job(conn, A, "scored", tmp, notes="digest; fine", score=6)
        put([entry(A, "applied")])
        before = decisions.PENDING_PATH.read_bytes()

        p = decisions.apply(conn, apply=False)
        check("plan still classifies", len(p["applied"]), 1)
        check("DB untouched", row_of(conn, A), ("scored", "digest; fine"))
        check("queue file untouched", (tmp / "queue" / f"{A}.json").exists(), True)
        check("file untouched", decisions.PENDING_PATH.read_bytes(), before)
    finally:
        conn.close()
        shutil.rmtree(tmp, ignore_errors=True)


def test_wedged_entries_are_named():
    print("an entry that has been pending for a day is named, not dropped")
    tmp, conn = fresh_env()
    try:
        add_job(conn, A, "new", tmp)
        put([entry(A, "promote", hours_ago(25)), entry(B, "applied", hours_ago(1))])

        p = decisions.apply(conn, apply=True)
        check("the old one is wedged", [e["id"] for e, _ in p["wedged"]], [A])
        check("both still pending", sorted(e["id"] for e, _ in p["pending"]), [A, B])
        check("neither was dropped",
              len(json.loads(decisions.PENDING_PATH.read_text())), 2)
    finally:
        conn.close()
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- set_lane

def test_set_lane():
    print("db.set_lane rewrites clause one and nothing else")
    tmp, conn = fresh_env()
    try:
        add_job(conn, A, "scored", tmp, notes="digest; solid platform fit, 6 total")

        db.set_lane(conn, A, "auto-tailor", marker="promoted via dashboard 2026-08-20")
        first = row_of(conn, A)[1]
        check("lane replaced, marker inserted, rationale preserved", first,
              "auto-tailor; promoted via dashboard 2026-08-20;"
              " solid platform fit, 6 total")

        db.set_lane(conn, A, "auto-tailor", marker="promoted via dashboard 2026-08-20")
        check("marker is not duplicated on replay", row_of(conn, A)[1], first)

        db.set_lane(conn, A, "digest")
        check("lane rewrite without a marker keeps the rest", row_of(conn, A)[1],
              "digest; promoted via dashboard 2026-08-20; solid platform fit, 6 total")

        add_job(conn, B, "scored", tmp, notes="")
        db.set_lane(conn, B, "auto-tailor")
        check("empty notes become just the lane", row_of(conn, B)[1], "auto-tailor")
        db.set_lane(conn, B, "digest-stretch", marker="promoted via dashboard 2026-08-21")
        check("empty notes plus marker", row_of(conn, B)[1],
              "digest-stretch; promoted via dashboard 2026-08-21")

        for label, kwargs in (
                ("unknown lane", {"lane": "tailor-now"}),
                # A literal em dash, on purpose: DB notes render into the
                # dashboard, and the lint hook bans the character everywhere
                # generated content lands.
                ("non-ASCII marker", {"lane": "digest", "marker": "promoted — today"}),
                ("marker with a clause separator", {"lane": "digest", "marker": "a; b"})):
            try:
                db.set_lane(conn, B, **kwargs)
                check(f"{label} rejected", "no error", "ValueError")
            except ValueError:
                check(f"{label} rejected", "ValueError", "ValueError")
    finally:
        conn.close()
        shutil.rmtree(tmp, ignore_errors=True)


# A pytest run reports per-function results, but check() records rather than
# raises, so without this sentinel a red check would pass silently there.
# Defined last on purpose: pytest runs in definition order.
def test_zz_no_failures():
    assert not FAILS, f"{len(FAILS)} check(s) failed: {', '.join(FAILS)}"


def main():
    test_apply_twice_is_idempotent()
    test_the_fold_records_the_browsers_own_timestamp()
    test_refold_after_reset_hard()
    test_promote_waits_for_the_score()
    test_promote_on_a_dead_row_is_obsolete()
    test_promote_revives_a_skip_only_with_its_queue_file_restored()
    test_promote_into_the_lane_the_row_already_holds_is_obsolete()
    test_promoted_lane_reaches_the_sidecar()
    test_applied_clears_queue_and_sidecar()
    test_interview_and_offer_keep_the_work_set()
    test_no_op_entries()
    test_duplicate_ids_resolve_to_the_latest()
    test_malformed_entries_are_preserved()
    test_dry_run_touches_nothing()
    test_wedged_entries_are_named()
    test_set_lane()
    test_foldable_only_when_a_fold_would_change_something()

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILURE(S): {', '.join(FAILS)}")
        return 1
    print("all decision-fold tests pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
