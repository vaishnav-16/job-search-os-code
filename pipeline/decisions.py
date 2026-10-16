"""Fold decisions Vaish recorded on the dashboard into the DB.

    python pipeline/decisions.py            # report only (dry run)
    python pipeline/decisions.py --apply    # write the decisions into the DB

WHY A FILE AND NOT A DB WRITE
Same reason score sidecars exist (see ingest_scores.py): `pipeline/jobs.sqlite`
is a committed binary with exactly two writers, and the dashboard is neither of
them. The published page is a static artifact with no server, so a button press
can only leave a note behind. It appends one entry to `decisions/pending.json`,
which travels through git like everything else, and `sync.py` step 3.2 is what
turns those notes into status and lane changes. Nothing here involves an LLM:
the decision was already made by a human, this is only bookkeeping.

FILE LIFECYCLE (the subtle part)
`decisions/pending.json` is ALWAYS present, holding at least `[]`. The fold
rewrites it with the survivors; it never deletes it, because sync.py names the
path in `git add` and a missing file fails the stage and wedges every later run.

An entry lives until the fold can act on it, then it is dropped:
  - a mark whose row is already in that status was done elsewhere -> drop
  - a promote whose row has moved past `scored` (stale_skip, tailored) can
    never happen now -> drop, and say why
  - a promote whose row is already in the auto-tailor lane has nothing left to
    do (that IS the post-promote state) -> drop, and say why
  - a promote on a `skipped` row whose queue file was restored by hand revives
    it: status back to `scored`, lane auto-tailor, JD back from the file. With
    no queue file it is obsolete like any other closed row
  - a promote whose row is still `new` has not been scored yet -> KEEP; the
    score may land in step 3 of a later sync, or of this very one
  - an id this clone has never seen -> KEEP; the row may arrive with the next
    detection pull
Anything still pending after 24h is WEDGED: it is not dropped (only Vaish can
decide that) but it is counted in the sync's outcome, because a decision that
silently never lands is indistinguishable from one that was never recorded.

Malformed entries are reported and LEFT IN THE FILE untouched. They represent a
decision Vaish believes he made; dropping them silently loses it.

IDEMPOTENCY IS THE HARD REQUIREMENT
sync.py's push retry replays `fold()` verbatim after a `reset --hard
origin/main` that restores both the DB and this file to their pre-run state, so
every branch here must be replay-safe. That is why a promote's marker date comes
from the entry's own `at` and never from today, and why `db.set_lane` refuses to
insert a marker it can already see. Since the already-in-lane drop above, a
replayed promote never reaches set_lane at all -- it is classified obsolete on
the second pass -- but the marker guard stays: it is what makes the two paths
produce the same bytes, and it still covers a hand-run set_lane.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db  # noqa: E402
import ingest_scores  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PENDING_PATH = ROOT / "decisions" / "pending.json"
QUEUE_DIR = ROOT / "queue"

# Marks mirror /mark's closed set; `promote` moves a scored job into the
# auto-tailor lane; `cancel` is the browser's undo, kept here as a backstop for
# an entry that reached the file anyway.
MARK_ACTIONS = ("applied", "rejected", "skipped", "interview", "offer")
# Exactly /mark step 2: these three take the job out of the work set, so its
# queue file and its score sidecar must die together with the status change.
QUEUE_CLEARING = ("applied", "rejected", "skipped")
ACTIONS = (*MARK_ACTIONS, "promote", "cancel")

ID_RE = re.compile(r"[0-9a-f]{40}")
WEDGED_HOURS = 24


def parse_at(value) -> datetime | None:
    """The entry's UTC timestamp, or None if it is not ISO 8601.

    Deliberately NOT db.parse_posted_at: that one accepts a bare integer as
    Lever epoch milliseconds, and a decision stamped "1755900000000" is a bug,
    not a timestamp.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def lane_of(notes: str) -> str:
    """The lane clause, which the scorer writes FIRST in `notes`.

    Same one-liner as sync.lane_of and the same convention db.set_lane rewrites;
    duplicated rather than imported because sync pulls in the whole local
    pipeline and this module is also loaded by the fold's own tests.
    """
    return (notes or "").split(";")[0].strip()


def promote_marker(at: str) -> str:
    """The notes clause a promotion leaves behind.

    Dated from the entry, never from today: a refold after a push race must
    reproduce the same notes string byte for byte.
    """
    return f"promoted via dashboard {parse_at(at).date()}"


def write_pending(entries: list) -> None:
    """Rewrite the file (never delete it). LF newline on every platform."""
    PENDING_PATH.parent.mkdir(exist_ok=True)
    # write_bytes, not write_text: text mode translates \n to \r\n on Windows,
    # and this file is committed, so the line endings would flip every sync.
    PENDING_PATH.write_bytes((json.dumps(entries, indent=2) + "\n").encode("utf-8"))


def _read() -> tuple[list[dict], list[tuple[object, str]], list[dict], bool]:
    """(entries, malformed, duplicates, file_is_array).

    Split from load() only so plan() can also see the duplicate losers, which
    are dropped rather than preserved and so cannot ride along in `malformed`.
    """
    malformed: list[tuple[object, str]] = []
    if not PENDING_PATH.exists():
        return [], malformed, [], True          # nothing recorded yet
    raw = PENDING_PATH.read_text(encoding="utf-8")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        return [], [(PENDING_PATH.name, f"unparseable JSON: {e}")], [], False
    if not isinstance(parsed, list):
        return [], [(PENDING_PATH.name,
                     f"top level is {type(parsed).__name__}, not an array")], [], False

    good: list[dict] = []
    for rec in parsed:
        if not isinstance(rec, dict):
            malformed.append((rec, "entry is not an object"))
        elif not ID_RE.fullmatch(str(rec.get("id", ""))):
            malformed.append((rec, f"id {rec.get('id')!r} is not 40 lowercase hex"))
        elif rec.get("action") not in ACTIONS:
            malformed.append((rec, f"unknown action {rec.get('action')!r}"))
        elif parse_at(rec.get("at")) is None:
            malformed.append((rec, f"'at' {rec.get('at')!r} is not UTC ISO 8601"))
        else:
            good.append(rec)

    # At most one entry per id survives. The browser already replaces by id
    # before it pushes, so this is a backstop against two tabs racing.
    latest: dict[str, dict] = {}
    duplicates: list[dict] = []
    for rec in good:
        prev = latest.get(rec["id"])
        if prev is None:
            latest[rec["id"]] = rec
        elif parse_at(rec["at"]) > parse_at(prev["at"]):
            latest[rec["id"]] = rec
            duplicates.append(prev)
        else:
            duplicates.append(rec)
    return list(latest.values()), malformed, duplicates, True


def load() -> tuple[list[dict], list[tuple[object, str]]]:
    """(valid entries, [(entry, reason)] for malformed ones)."""
    entries, malformed, _, _ = _read()
    return entries, malformed


def plan(conn) -> dict:
    """Classify every entry against its DB row without writing anything."""
    entries, malformed, duplicates, file_is_array = _read()
    rows = {r[0]: (r[1], r[2])
            for r in conn.execute("SELECT id, status, notes FROM jobs")}
    out: dict = {"applied": [], "pending": [], "obsolete": [], "dropped_cancel": [],
                 "wedged": [], "malformed": malformed, "duplicates": duplicates,
                 "file_is_array": file_is_array}

    for rec in entries:
        jid, action = rec["id"], rec["action"]
        if action == "cancel":
            out["dropped_cancel"].append(rec)
            continue
        row = rows.get(jid)
        if row is None:
            out["pending"].append((rec, "no row for this id in this clone"))
            continue
        status, notes = row
        if status == action:
            out["obsolete"].append((rec, f"row is already {status}"))
        elif action in MARK_ACTIONS:
            out["applied"].append((rec, f"{status} -> {action}"))
        elif status == "scored":
            # A promote onto the lane the row already holds changes nothing that
            # travels: set_lane would rewrite `notes` to a value it already has,
            # and sync.reconcile_sidecars only rewrites a sidecar when the lanes
            # DIFFER, so the cloud tailor never sees it. What it would leave is
            # one dated marker clause per day the entry survives. Same reasoning
            # dashboard.promotable() uses to not draw the button at all.
            if lane_of(notes) == "auto-tailor":
                out["obsolete"].append((rec, "row is already in the auto-tailor lane"))
            else:
                out["applied"].append(
                    (rec, f"lane -> auto-tailor ({promote_marker(rec['at'])})"))
        elif status == "new":
            out["pending"].append((rec, "not scored yet"))
        elif status == "skipped" and (QUEUE_DIR / f"{jid}.json").exists():
            # The way back from a skip made by mistake (10-13: a bulk triage
            # skipped nine roles Vaish then wanted). The skip deleted the queue
            # file, so a restored one is the proof this is deliberate: a dashboard
            # click alone can never revive a row. The file also carries the JD
            # the skip shed from the DB.
            out["applied"].append(
                (rec, f"skipped -> scored, lane -> auto-tailor ({promote_marker(rec['at'])})"))
        else:
            out["obsolete"].append((rec, f"row is {status}, past promotion"))

    now = datetime.now(timezone.utc)
    for rec, _ in out["pending"]:
        age = (now - parse_at(rec["at"])).total_seconds() / 3600
        if age > WEDGED_HOURS:
            out["wedged"].append((rec, age))
    return out


def foldable(p: dict) -> bool:
    """True when a fold would change something: an entry it applies or drops.

    W7 (2026-10-13): a dashboard click no longer starts fold.yml; detect.yml asks
    this after every run and dispatches a fold only when it says yes. Entries that
    can only wait (row not scored yet, id not seen) and malformed ones the fold
    leaves alone would otherwise dispatch a do-nothing fold every 2h.
    """
    return bool(p["applied"] or p["obsolete"] or p["dropped_cancel"])


def revive(conn, jid: str, rec: dict) -> None:
    """A promote on a skipped row whose queue file was restored: back to scored.

    The JD comes back from the queue file, because db.shed_jd_text emptied the
    column when the row left the work set, and reconcile_queue would otherwise
    one day re-emit the file from that empty column.
    """
    db.set_status(conn, jid, "scored", at=rec["at"], via=rec.get("via") or "dashboard")
    try:
        jd = json.loads((QUEUE_DIR / f"{jid}.json").read_text(encoding="utf-8")).get(
            "jd_markdown") or ""
    except (OSError, json.JSONDecodeError):
        jd = ""
    if jd:
        conn.execute("UPDATE jobs SET jd_text = ? WHERE id = ? AND jd_text = ''", (jd, jid))
        conn.commit()


def apply(conn, apply: bool = False) -> dict:
    """Fold the pending decisions in. Returns the plan either way."""
    p = plan(conn)
    if not apply:
        return p

    for rec, _ in p["applied"]:
        jid, action = rec["id"], rec["action"]
        if action in MARK_ACTIONS:
            # /mark writes the same entry with via="mark" (pipeline/mark.py).
            db.set_status(conn, jid, action, at=rec["at"], via=rec.get("via") or "dashboard")
            if action in QUEUE_CLEARING:
                queue_file = QUEUE_DIR / f"{jid}.json"
                if queue_file.exists():
                    queue_file.unlink()
                ingest_scores.drop_sidecar(jid)
        else:
            if conn.execute("SELECT status FROM jobs WHERE id = ?",
                            (jid,)).fetchone()[0] == "skipped":
                revive(conn, jid, rec)
            db.set_lane(conn, jid, "auto-tailor", marker=promote_marker(rec["at"]))

    if not p["file_is_array"]:
        # Nothing here can be merged into a file that is not a list of entries,
        # and overwriting it would destroy whatever Vaish still has to fix.
        return p
    survivors = [rec for rec, _ in p["pending"]] + [rec for rec, _ in p["malformed"]]
    write_pending(sorted(survivors, key=_order))
    return p


def _order(rec):
    """Stable file order: by `at`, then id. Junk entries (a bare string in the
    array) have neither and sort last rather than crashing the rewrite."""
    if not isinstance(rec, dict):
        return (1, "", "")
    return (0, str(rec.get("at", "")), str(rec.get("id", "")))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true",
                    help="write the decisions into the DB (default: dry run)")
    ap.add_argument("--foldable", action="store_true",
                    help="exit 0 when a fold would apply or drop an entry, else 1 (read only)")
    args = ap.parse_args()

    conn = db.connect()
    if args.foldable:
        p = plan(conn)
        yes = foldable(p)
        print(f"decisions: {len(p['applied'])} to apply, {len(p['obsolete'])} obsolete, "
              f"{len(p['dropped_cancel'])} cancelled, {len(p['pending'])} waiting, "
              f"{len(p['malformed'])} malformed -> {'fold' if yes else 'no fold needed'}")
        return 0 if yes else 1
    p = apply(conn, apply=args.apply)

    print(f"decisions in {PENDING_PATH.name}: "
          f"{len(p['applied']) + len(p['pending']) + len(p['obsolete']) + len(p['dropped_cancel'])}"
          f" valid, {len(p['malformed'])} malformed")
    print(f"  applied to the DB               : {len(p['applied'])}")
    print(f"  still pending (row not ready)   : {len(p['pending'])}")
    print(f"  obsolete (row already moved on) : {len(p['obsolete'])}")
    print(f"  cancelled before folding        : {len(p['dropped_cancel'])}")
    print(f"  duplicate ids superseded        : {len(p['duplicates'])}")
    for rec, what in p["applied"]:
        print(f"  {rec['id'][:8]}  {rec['action']:<9} {what}")
    for rec, why in p["obsolete"]:
        print(f"  {rec['id'][:8]}  {rec['action']:<9} DROPPED: {why}")
    for rec, why in p["pending"]:
        print(f"  {rec['id'][:8]}  {rec['action']:<9} kept: {why}")
    for rec, hours in p["wedged"]:
        print(f"  WEDGED {rec['id'][:8]}  {rec['action']} recorded {hours / 24:.1f} day(s)"
              " ago and still cannot land")
    for rec, why in p["malformed"]:
        print(f"  MALFORMED {rec}: {why}")

    if not args.apply:
        print("\nDRY RUN. Re-run with --apply to fold these into the DB.")
        return 0
    print(f"\nfolded {len(p['applied'])} decision(s)")
    print("db totals:", db.counts_by_status(conn))
    return 0


if __name__ == "__main__":
    sys.exit(main())
