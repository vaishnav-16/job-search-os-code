"""Tests for score-sidecar ingest — the cloud Routine's only interface to the DB.

The contract being guarded: the Routine writes JSON to scores/ and NOTHING
else; this side folds those into jobs.sqlite. Two rules carry the design and
both are easy to break by accident:

  1. Ingest is a no-op for any row past 'new'. A sidecar arriving for a job
     already tailored must not reset it to 'scored'.
  2. Sidecars survive ingest. "Needs scoring" means queue-file-without-sidecar,
     so deleting at ingest would make the Routine re-score every digest job on
     its next run, forever.

Run: python pipeline/test_ingest.py   (temp dirs + temp DB; no network, no LLM)
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import db  # noqa: E402
import ingest_scores  # noqa: E402

PASS = FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"FAIL  {name}" + (f" — {detail}" if detail else ""))


def fresh_env(tmp: Path):
    """Point the module at throwaway scores/ and queue/ dirs and a temp DB."""
    (tmp / "scores").mkdir(exist_ok=True)
    (tmp / "queue").mkdir(exist_ok=True)
    ingest_scores.SCORES_DIR = tmp / "scores"
    ingest_scores.QUEUE_DIR = tmp / "queue"
    conn = db.connect(tmp / "t.sqlite")
    return conn


def add_job(conn, jid: str, status: str, tmp: Path, with_queue: bool = True):
    conn.execute(
        "INSERT OR REPLACE INTO jobs (id, company, company_norm, title, title_norm, "
        "location, url, source, jd_text, posted_at, first_seen, status) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (jid, "Acme", "acme", "PM", "pm", "NY", "u", "greenhouse", "jd",
         "2026-08-15", db.now_iso(), status))
    conn.commit()
    if with_queue:
        (tmp / "queue" / f"{jid}.json").write_text("{}", encoding="utf-8")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ingest-test-"))
    conn = fresh_env(tmp)

    print("-- ingest respects row status --")
    add_job(conn, "a" * 40, "new", tmp)
    ingest_scores.write_sidecar("a" * 40, 9, "auto-tailor; strong fit")
    p = ingest_scores.ingest(conn, apply=True)
    row = conn.execute("SELECT score, status, notes FROM jobs WHERE id=?", ("a" * 40,)).fetchone()
    check("sidecar for a 'new' row is ingested", len(p["ingest"]) == 1)
    check("row moves to scored with score + lane note",
          (row[0], row[1], row[2]) == (9, "scored", "auto-tailor; strong fit"), str(tuple(row)))
    check("SIDECAR SURVIVES INGEST (else the Routine re-scores it forever)",
          ingest_scores.sidecar_path("a" * 40).exists())

    p2 = ingest_scores.ingest(conn, apply=True)
    check("second ingest is a no-op (idempotent)", len(p2["ingest"]) == 0 and len(p2["already"]) == 1)

    add_job(conn, "b" * 40, "tailored", tmp)
    ingest_scores.write_sidecar("b" * 40, 3, "digest; weak")
    p3 = ingest_scores.ingest(conn, apply=True)
    st = conn.execute("SELECT status, score FROM jobs WHERE id=?", ("b" * 40,)).fetchone()
    check("sidecar for a 'tailored' row does NOT reset it",
          st[0] == "tailored" and st[1] is None and len(p3["already"]) == 2, str(tuple(st)))

    print("-- unknown ids and malformed files --")
    ingest_scores.write_sidecar("c" * 40, 7, "digest; unknown job")
    p4 = ingest_scores.plan(conn)
    check("sidecar for an id not in this DB is ignored, not fatal", len(p4["unknown"]) == 1)

    (ingest_scores.SCORES_DIR / "bad1.json").write_text("{not json", encoding="utf-8")
    (ingest_scores.SCORES_DIR / ("d" * 40 + ".json")).write_text(
        json.dumps({"id": "e" * 40, "score": 5}), encoding="utf-8")
    (ingest_scores.SCORES_DIR / ("f" * 40 + ".json")).write_text(
        json.dumps({"id": "f" * 40, "score": 99}), encoding="utf-8")
    p5 = ingest_scores.plan(conn)
    reasons = " ".join(why for _, why in p5["malformed"])
    check("malformed sidecars are reported, never crash", len(p5["malformed"]) == 3, reasons)
    check("id/filename mismatch caught", "does not match filename" in reasons)
    check("out-of-range score caught", "out of range" in reasons)
    for junk in ("bad1.json", "d" * 40 + ".json", "f" * 40 + ".json"):
        (ingest_scores.SCORES_DIR / junk).unlink()

    print("-- lifecycle: sidecar dies with its queue file --")
    check("drop_sidecar removes it", ingest_scores.drop_sidecar("c" * 40) is True)
    check("drop_sidecar on a missing one is False, not an error",
          ingest_scores.drop_sidecar("z" * 40) is False)

    (tmp / "queue" / f"{'b' * 40}.json").unlink()
    orphans = ingest_scores.sweep_orphans(apply=False)
    check("orphan detected when queue file is gone", [o.stem for o in orphans] == ["b" * 40],
          str([o.stem for o in orphans]))
    check("dry-run sweep does not delete", ingest_scores.sidecar_path("b" * 40).exists())
    ingest_scores.sweep_orphans(apply=True)
    check("apply sweep deletes the orphan", not ingest_scores.sidecar_path("b" * 40).exists())
    check("non-orphan is untouched by the sweep",
          ingest_scores.sidecar_path("a" * 40).exists())

    print("-- backfill keeps the cloud from re-scoring local work --")
    add_job(conn, "g" * 40, "scored", tmp)
    conn.execute("UPDATE jobs SET score=6, notes='digest; local call' WHERE id=?", ("g" * 40,))
    conn.commit()
    todo = ingest_scores.backfill(conn, apply=False)
    check("a locally-scored job with a queue file needs a sidecar", todo == ["g" * 40], str(todo))
    ingest_scores.backfill(conn, apply=True)
    rec_g = json.loads(ingest_scores.sidecar_path("g" * 40).read_text(encoding="utf-8"))
    check("backfilled sidecar carries the DB score and lane note",
          (rec_g["score"], rec_g["note"], rec_g["scorer"]) == (6, "digest; local call", "local"),
          str(rec_g))
    check("backfill is idempotent", ingest_scores.backfill(conn, apply=False) == [])
    check("backfilled job leaves the routine work list",
          ingest_scores.sidecar_path("g" * 40).exists()
          and (tmp / "queue" / f"{'g' * 40}.json").exists())

    print("-- a routine judgment skip survives the sync --")
    add_job(conn, "h" * 40, "scored", tmp)
    conn.execute("UPDATE jobs SET score=9, notes='auto-tailor; strong fit' WHERE id=?", ("h" * 40,))
    add_job(conn, "i" * 40, "scored", tmp)
    conn.execute("UPDATE jobs SET score=8, notes='auto-tailor; promoted via dashboard 2026-09-27; x'"
                 " WHERE id=?", ("i" * 40,))
    conn.commit()
    ingest_scores.write_sidecar("h" * 40, 9, "skip; PERM ad, not a vacancy")
    ingest_scores.write_sidecar("i" * 40, 8, "skip; part-time")
    p3 = ingest_scores.ingest(conn, apply=True)
    row_h = conn.execute("SELECT score, status, notes FROM jobs WHERE id=?", ("h" * 40,)).fetchone()
    row_i = conn.execute("SELECT notes FROM jobs WHERE id=?", ("i" * 40,)).fetchone()
    check("skip sidecar on an auto-tailor row is folded",
          tuple(row_h) == (9, "scored", "skip; PERM ad, not a vacancy"), str(tuple(row_h)))
    check("a promoted row keeps its lane", row_i[0].startswith("auto-tailor"), row_i[0])
    check("the fold is reported", [r["id"] for r in p3["relane"]] == ["h" * 40])
    check("the fold is idempotent", ingest_scores.plan(conn)["relane"] == [])

    print("-- a local re-score survives the sync (10-10: 37 rows reverted) --")
    add_job(conn, "p" * 40, "scored", tmp)
    conn.execute("UPDATE jobs SET score=9, notes='auto-tailor; old rubric' WHERE id=?", ("p" * 40,))
    add_job(conn, "q" * 40, "scored", tmp)
    conn.execute("UPDATE jobs SET score=8, notes='digest-stretch; preferred 5+' WHERE id=?",
                 ("q" * 40,))
    add_job(conn, "r" * 40, "scored", tmp)
    conn.execute("UPDATE jobs SET score=8, notes='digest; same lane' WHERE id=?", ("r" * 40,))
    conn.commit()
    ingest_scores.write_sidecar("p" * 40, 7, "digest; staffing agency, (d)=0", scorer="local")
    ingest_scores.write_sidecar("q" * 40, 10, "auto-tailor; no minimum years", scorer="local")
    ingest_scores.write_sidecar("r" * 40, 9, "digest; reworded note", scorer="local")
    p4 = ingest_scores.ingest(conn, apply=True)
    got = {j: tuple(conn.execute("SELECT score, notes FROM jobs WHERE id=?", (j * 40,)).fetchone())
           for j in "pqr"}
    check("auto-tailor -> digest re-score folds", got["p"] == (7, "digest; staffing agency, (d)=0"),
          str(got["p"]))
    check("digest-stretch -> auto-tailor re-score folds",
          got["q"] == (10, "auto-tailor; no minimum years"), str(got["q"]))
    check("same lane is a no-op (DB keeps its row)", got["r"] == (8, "digest; same lane"),
          str(got["r"]))
    check("both re-scores reported", sorted(r["id"][0] for r in p4["relane"]) == ["p", "q"],
          str([r["id"][:4] for r in p4["relane"]]))
    check("re-score fold is idempotent", ingest_scores.plan(conn)["relane"] == [])

    print("-- write_sidecar shape --")
    rec = json.loads(ingest_scores.sidecar_path("a" * 40).read_text(encoding="utf-8"))
    check("carries id/score/note/scored_at/scorer/model",
          set(rec) == {"id", "score", "note", "scored_at", "scorer", "model"}, str(set(rec)))
    check("defaults to scorer=routine model=sonnet",
          rec["scorer"] == "routine" and rec["model"] == "sonnet")

    print("-- record-dir: reply files written by the scorers, read verbatim (W4) --")
    rd = tmp / "replies"
    rd.mkdir()
    ok_reply = ("**Acme - PM**\n\n**Total: 8/10 \u2013 auto-tailor**\nFit.\n\n"
                "GATES: requires US citizenship\nSTRETCH: no\nNOTE: PM, (b)=2\nSCORE: 8\n")
    (rd / ("7" * 40 + ".md")).write_text(ok_reply, encoding="utf-8")
    (rd / ("9" * 40 + ".md")).write_text("**Acme - PM**\nno total line\nSCORE: 7\n",
                                         encoding="utf-8")
    (rd / "notes.md").write_text("not a reply", encoding="utf-8")
    rc = ingest_scores.record_dir_main([str(rd)])
    side_s = json.loads(ingest_scores.sidecar_path("7" * 40).read_text(encoding="utf-8"))
    check("record-dir records a good reply verbatim (en dash in Total is fine)",
          (side_s["score"], side_s["note"].split(";")[0]) == (8, "skip"), str(side_s))
    check("a gate in the file becomes a skip (no model re-typing in between)",
          "requires US citizenship" in side_s["note"], side_s["note"])
    check("a malformed file writes nothing", not ingest_scores.sidecar_path("9" * 40).exists())
    check("record-dir exits 1 when any file is malformed", rc == 1, str(rc))
    check("a non-id file is ignored", not ingest_scores.sidecar_path("notes").exists())
    check("an empty dir is a STOPPED, not a silent success",
          ingest_scores.record_dir_main([str(tmp / "nowhere")]) == 1)

    print("-- record(): one job-scorer reply -> its sidecar (2026-10-08) --")

    def reply(total, lane, gates="none", stretch="no",
              note="PM II, 3+ years (b)=2, API fit"):
        return (f"**Acme - PM II**\n\n| Criterion | Score | Rationale |\n|---|---|---|\n"
                f"| (a) role type | 2 | PM |\n\n**Total: {total}/10 - {lane}**\n"
                f"Strong fit, (b)=2.\n\nGATES: {gates}\nSTRETCH: {stretch}\nNOTE: {note}\n"
                f"SCORE: {total}\n")

    ingest_scores.record("k" * 40, reply(9, "auto-tailor"))
    side = json.loads(ingest_scores.sidecar_path("k" * 40).read_text(encoding="utf-8"))
    check("a clean reply writes '<lane>; <note>'",
          (side["score"], side["note"]) == (9, "auto-tailor; PM II, 3+ years (b)=2, API fit"),
          str(side))
    r = ingest_scores.record("l" * 40, reply(9, "auto-tailor",
                                             gates="requires US citizenship; MBA required"))
    check("any gate makes the lane skip, score kept",
          r["note"] == "skip; gate: requires US citizenship, MBA required; "
                       "PM II, 3+ years (b)=2, API fit" and r["score"] == 9, r["note"])
    r = ingest_scores.parse_reply(reply(8, "digest", stretch="yes",
                                        note="Sr PM, 5+ years (b)=1, AI platform fit"))
    check("a stretch digest is lane 'digest-stretch' (db.LANES, the dashboard chip)",
          r["note"] == "digest-stretch; Sr PM, 5+ years (b)=1, AI platform fit", r["note"])
    r = ingest_scores.parse_reply(reply(8, "auto-tailor", stretch="yes"))
    check("STRETCH on a non-digest lane is ignored", r["note"].startswith("auto-tailor; "),
          r["note"])
    r = ingest_scores.parse_reply(reply(8, "digest", stretch="yes", gates="Mandarin required"))
    check("a gate beats stretch", r["note"].startswith("skip; gate: Mandarin required; "),
          r["note"])
    r = ingest_scores.parse_reply(reply(7, "digest", note="Sr PM; 5 yrs; payments"))
    check("semicolons in the note cannot fake a lane",
          r["note"] == "digest; Sr PM, 5 yrs, payments", r["note"])
    r = ingest_scores.parse_reply(reply(4, "skip").replace("GATES: none", "**GATES:** None."))
    check("bold or 'None.' still parses as no gate", r["note"].startswith("skip; PM II"),
          r["note"])
    for label, bad in (
            ("missing SCORE", reply(9, "auto-tailor").replace("SCORE: 9\n", "")),
            ("missing GATES", reply(9, "auto-tailor").replace("GATES: none\n", "")),
            ("empty NOTE", reply(9, "auto-tailor", note="")),
            ("missing Total", reply(9, "auto-tailor").replace("Total: 9/10", "Sum: 9")),
            ("score 11", reply(11, "auto-tailor"))):
        try:
            ingest_scores.record("m" * 40, bad)
            check(f"{label} raises", False)
        except ingest_scores.ScorerReplyError:
            check(f"{label} raises and writes nothing",
                  not ingest_scores.sidecar_path("m" * 40).exists())

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
