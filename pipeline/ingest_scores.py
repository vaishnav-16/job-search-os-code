"""Ingest score sidecars written by the cloud scoring Routine.

    python pipeline/ingest_scores.py            # report only (dry run)
    python pipeline/ingest_scores.py --apply    # write scores into the DB
    python pipeline/ingest_scores.py record <id> <reply-file> [--scorer routine|local]
                                                # one job-scorer reply -> its sidecar

WHY SIDECARS AND NOT DIRECT DB WRITES
`pipeline/jobs.sqlite` is a committed BINARY file, so every writer is a
potential unmergeable rebase conflict. It has exactly two writers by design:
the Layer 1 cron and the local Layer 2 session. A cloud Routine writing scores
straight into it would be a third, and that conflict is not hypothetical: it
happened twice on 2026-08-15 and both times cost a manual recovery.

So the Routine writes one JSON sidecar per scored job to `scores/` on its own
`claude/judge` branch, and this script folds them into the DB locally. The
routine never touches the DB, the queue, or anything outside `scores/`.

SIDECAR LIFECYCLE (the subtle part)
"needs scoring" = queue file exists AND no sidecar exists. Sidecars are
therefore NOT deleted at ingest: a job stays at status 'scored' with its queue
file intact while it waits in the digest for Vaish to promote it, and deleting
the sidecar would make the next Routine run score it all over again. A sidecar
dies only when its queue file dies (tailored / skipped / stale_skip), which is
what `sweep_orphans` enforces.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SCORES_DIR = ROOT / "scores"
QUEUE_DIR = ROOT / "queue"


def sidecar_path(jid: str) -> Path:
    return SCORES_DIR / f"{jid}.json"


def write_sidecar(jid: str, score: int, note: str, scorer: str = "routine",
                  model: str = "sonnet", scored_at: str = "") -> Path:
    """Used by the Routine (and tests). Never writes to the DB."""
    SCORES_DIR.mkdir(exist_ok=True)
    p = sidecar_path(jid)
    p.write_text(json.dumps({
        "id": jid, "score": int(score), "note": note,
        "scored_at": scored_at or db.now_iso(), "scorer": scorer, "model": model,
    }, indent=2), encoding="utf-8")
    return p


class ScorerReplyError(ValueError):
    """A job-scorer reply missing a line that record() needs. Nothing is written."""


_LANES = ("auto-tailor", "digest", "skip")
# Any dash: a scorer that writes "7/10 – digest" (en dash) is still one reply, not a malformed one.
_TOTAL = re.compile(r"Total:\s*\d+\s*/\s*10\s*[-:–—]+\s*\**\s*(auto-tailor|digest|skip)", re.I)


def _machine_line(text: str, key: str) -> str | None:
    # [ \t], not \s: \s crosses the newline, so an empty "NOTE:" would read the
    # SCORE line below it as the note.
    hits = re.findall(rf"(?im)^[ \t]*\**{key}:\**[ \t]*(.*?)[ \t*]*$", text)
    return hits[-1].strip() if hits else None


def parse_reply(text: str) -> dict:
    """Read a job-scorer reply's Total lane and its GATES / STRETCH / NOTE / SCORE lines.

    The note format is load-bearing (worklist.lane_of splits on the first ";"), so
    it is composed here rather than by the routine thread (2026-10-08):
      any gate        -> "skip; gate: <gates>; <note>"   (the lane, whatever the total)
      stretch, digest -> "digest-stretch; <note>"        (worklist fills spare slots)
      otherwise       -> "<lane>; <note>"
    """
    score = _machine_line(text, "SCORE")
    if score is None or not re.fullmatch(r"\d{1,2}", score) or not 0 <= int(score) <= 10:
        raise ScorerReplyError(f"no valid SCORE line (got {score!r})")
    m = _TOTAL.search(text)
    if not m:
        raise ScorerReplyError("no 'Total: n/10 - <lane>' line")
    lane = m.group(1).lower()
    gates_raw = _machine_line(text, "GATES")
    if gates_raw is None:
        raise ScorerReplyError("no GATES line")
    gates = [g.strip() for g in gates_raw.split(";")
             if g.strip() and g.strip().lower().rstrip(".") not in ("none", "n/a", "")]
    stretch = (_machine_line(text, "STRETCH") or "no").lower().startswith("y")
    note = (_machine_line(text, "NOTE") or "").replace(";", ",").strip()
    if not note:
        raise ScorerReplyError("no NOTE line")
    if gates:
        lane, stretch = "skip", False
    stretch = stretch and lane == "digest"
    prefix = (f"skip; gate: {', '.join(gates)}" if gates
              else "digest-stretch" if stretch else lane)
    return {"score": int(score), "lane": lane, "gates": gates, "stretch": stretch,
            "note": f"{prefix}; {note}"}


def record(jid: str, reply_text: str, scorer: str = "routine", model: str = "sonnet") -> dict:
    """Parse one job-scorer reply and write its sidecar. Raises ScorerReplyError
    (and writes nothing) on a malformed reply, so the job stays unscored."""
    parsed = parse_reply(reply_text)
    write_sidecar(jid, parsed["score"], parsed["note"], scorer=scorer, model=model)
    return parsed


def load_sidecars() -> tuple[list[dict], list[tuple[Path, str]]]:
    """(valid records, [(path, reason)] for malformed ones)."""
    good, bad = [], []
    if not SCORES_DIR.exists():
        return good, bad
    for p in sorted(SCORES_DIR.glob("*.json")):
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            bad.append((p, f"unparseable JSON: {e}"))
            continue
        if not isinstance(rec, dict) or "id" not in rec or "score" not in rec:
            bad.append((p, "missing id/score"))
            continue
        if rec["id"] != p.stem:
            bad.append((p, f"id {rec['id']!r} does not match filename"))
            continue
        try:
            s = int(rec["score"])
        except (TypeError, ValueError):
            bad.append((p, f"non-integer score {rec.get('score')!r}"))
            continue
        if not 0 <= s <= 10:
            bad.append((p, f"score out of range: {s}"))
            continue
        rec["score"] = s
        good.append(rec)
    return good, bad


def plan(conn) -> dict:
    """Classify every sidecar without writing anything."""
    good, bad = load_sidecars()
    rows = {r[0]: (r[1], r[2] or "") for r in conn.execute("SELECT id, status, notes FROM jobs")}
    out = {"ingest": [], "relane": [], "already": [], "unknown": [], "malformed": bad}
    for rec in good:
        st, notes = rows.get(rec["id"], (None, ""))
        if st is None:
            out["unknown"].append(rec)      # not in this clone's DB yet
        elif st == "new":
            out["ingest"].append(rec)
        elif st == "scored" and is_relane(rec.get("note", ""), notes):
            out["relane"].append(rec)
        else:
            out["already"].append(rec)      # scored/tailored/applied/... : no-op
    return out


RELANES = ("auto-tailor", "digest", "digest-stretch", "skip")


def is_relane(side_note: str, db_notes: str) -> bool:
    """A scored row's sidecar now names another lane: fold it into the DB.

    Two writers do this, both by rewriting the sidecar: the routine re-lanes a
    job skip after reading the JD (a PERM ad, a part-time req, a work-authorization gate the
    scorer missed), and a local re-score moves a row under a new rubric. Without
    folding it here the DB keeps the old lane, sync.reconcile_sidecars rewrites
    the sidecar back from the DB, and the change is silently undone: the routine
    re-read the same JDs every fire (notes run 14), and the 10-10 re-score of 37
    old-rubric rows was reverted by the next fold (9 digest calls back to
    auto-tailor). Since 10-08 the DB is written only from sidecars and dashboard
    decisions, so a lane that differs in the sidecar is the newer call. A promoted
    row keeps its lane: Vaish's call wins. The fold's stale-copy guard
    (sync.keep_newer_sidecars) stops an older branch copy from posing as a change.
    """
    lane = lambda n: (n or "").split(";")[0].strip()
    return (lane(side_note) in RELANES and lane(side_note) != lane(db_notes)
            and "promoted via dashboard" not in (db_notes or ""))


def ingest(conn, apply: bool = False) -> dict:
    p = plan(conn)
    if apply:
        for rec in p["ingest"] + p["relane"]:
            db.set_score(conn, rec["id"], rec["score"], rec.get("note", ""))
    return p


def backfill(conn, apply: bool = False) -> list[str]:
    """Write sidecars for jobs this DB has already scored locally.

    Keeps the invariant "queue file without a sidecar == needs scoring" true
    for everyone. Without this, a digest job scored locally still has its queue
    file (Vaish may promote it for days) and no sidecar, so the cloud Routine
    would score it again on its next run. Harmless at ingest (a non-'new' row
    is a no-op) but a wasted call and a confusing report.
    """
    done = []
    for r in conn.execute("SELECT id, score, notes FROM jobs "
                          "WHERE score IS NOT NULL AND status IN ('scored','tailored')"):
        jid = r[0]
        if not (QUEUE_DIR / f"{jid}.json").exists() or sidecar_path(jid).exists():
            continue
        done.append(jid)
        if apply:
            write_sidecar(jid, r[1], r[2] or "", scorer="local")
    return done


def sweep_orphans(apply: bool = False) -> list[Path]:
    """Sidecars whose queue file is gone are dead: their job left the work set."""
    if not SCORES_DIR.exists():
        return []
    orphans = [p for p in sorted(SCORES_DIR.glob("*.json"))
               if not (QUEUE_DIR / f"{p.stem}.json").exists()]
    if apply:
        for p in orphans:
            p.unlink()
    return orphans


def drop_sidecar(jid: str) -> bool:
    """Called wherever a queue file is deleted, to keep the two in lockstep."""
    p = sidecar_path(jid)
    if p.exists():
        p.unlink()
        return True
    return False


def record_main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="ingest_scores.py record")
    ap.add_argument("jid")
    ap.add_argument("reply_file", help="the job-scorer reply, saved verbatim ('-' = stdin)")
    ap.add_argument("--scorer", default="routine", choices=("routine", "local"))
    ap.add_argument("--model", default="sonnet")
    a = ap.parse_args(argv)
    if not re.fullmatch(r"[0-9a-f]{40}", a.jid):
        print(f"STOPPED: {a.jid!r} is not a 40-hex job id")
        return 2
    text = (sys.stdin.read() if a.reply_file == "-"
            else Path(a.reply_file).read_text(encoding="utf-8"))
    try:
        r = record(a.jid, text, scorer=a.scorer, model=a.model)
    except ScorerReplyError as e:
        print(f"STOPPED: {a.jid[:8]} not recorded, malformed scorer reply: {e}")
        return 1
    print(f"{a.jid[:8]} score {r['score']} -> {r['note']}")
    return 0


def record_dir_main(argv: list[str]) -> int:
    """Record every `<40-hex id>.md` reply file in a directory, verbatim.

    WHY (W4, 2026-10-11). score-queue's coordinator was told to save each scorer
    reply verbatim and run `record` on it. Sonnet instead re-typed the replies
    from memory through a shell helper, once hard-coding `GATES: none` and
    `STRETCH: no` for a whole batch, so a gate a scorer found could be dropped
    without a trace. Now each job-scorer writes its own reply file and this reads
    the files as they are: no model sits between the scorer and the sidecar.
    """
    ap = argparse.ArgumentParser(prog="ingest_scores.py record-dir")
    ap.add_argument("reply_dir", help="directory of <40-hex id>.md files written by job-scorer")
    ap.add_argument("--scorer", default="routine", choices=("routine", "local"))
    ap.add_argument("--model", default="sonnet")
    a = ap.parse_args(argv)
    files = sorted(p for p in Path(a.reply_dir).glob("*.md")
                   if re.fullmatch(r"[0-9a-f]{40}", p.stem))
    if not files:
        print(f"STOPPED: no <40-hex id>.md reply files in {a.reply_dir}")
        return 1
    stopped = []
    for p in files:
        try:
            r = record(p.stem, p.read_text(encoding="utf-8"), scorer=a.scorer, model=a.model)
            print(f"{p.stem[:8]} score {r['score']} -> {r['note'][:100]}")
        except ScorerReplyError as e:
            stopped.append(p.stem)
            print(f"STOPPED: {p.stem[:8]} not recorded, malformed scorer reply: {e}")
    print(f"recorded {len(files) - len(stopped)} of {len(files)}")
    if stopped:
        print("relaunch once: " + " ".join(stopped))
    return 1 if stopped else 0


def main() -> int:
    if sys.argv[1:2] == ["record"]:
        return record_main(sys.argv[2:])
    if sys.argv[1:2] == ["record-dir"]:
        return record_dir_main(sys.argv[2:])
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true", help="write scores (default: dry run)")
    args = ap.parse_args()

    conn = db.connect()
    p = ingest(conn, apply=args.apply)
    filled = backfill(conn, apply=args.apply)
    orphans = sweep_orphans(apply=args.apply)

    print(f"sidecars in scores/: {sum(len(p[k]) for k in ('ingest', 'already', 'unknown'))}"
          f" valid, {len(p['malformed'])} malformed")
    print(f"  ingest into DB (row is 'new')   : {len(p['ingest'])}")
    print(f"  already past scoring (no-op)    : {len(p['already'])}")
    print(f"  id not in this DB (ignored)     : {len(p['unknown'])}")
    print(f"  orphaned (queue file gone)      : {len(orphans)}")
    print(f"  backfilled from local scores    : {len(filled)}"
          "   (so the cloud does not re-score them)")
    for path, why in p["malformed"]:
        print(f"  MALFORMED {path.name}: {why}")
    if p["ingest"]:
        lanes: dict[str, int] = {}
        for rec in p["ingest"]:
            lane = (rec.get("note") or "").split(";")[0].strip() or "(no lane)"
            lanes[lane] = lanes.get(lane, 0) + 1
        print("\n  lanes arriving:")
        for lane, n in sorted(lanes.items(), key=lambda kv: -kv[1]):
            print(f"    {lane:<22}{n:>4}")

    if not args.apply:
        print("\nDRY RUN. Re-run with --apply to write scores and remove orphans.")
        return 0
    print(f"\ningested {len(p['ingest'])} score(s); removed {len(orphans)} orphan sidecar(s)")
    print("db totals:", db.counts_by_status(conn))
    return 0


if __name__ == "__main__":
    sys.exit(main())
