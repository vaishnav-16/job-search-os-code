"""Tests for tailor-sidecar ingest — the judge-queue Routine's interface to the DB.

The contract being guarded, and why each rule exists:

  1. Three fields are required and they are the three the DB needs: id, folder,
     status_intent. Everything else is report data, and a shape problem in
     report data must cost the field, never the sidecar — the sidecar is the
     receipt for a completed cloud tailor.
  2. Word no longer gates. The cloud reviewed the resume, so a page-count
     disagreement is a `word_pages=` note on the row and a printed WORD CHECK
     line, not a status downgrade a script cannot act on.
  3. Ingest never outranks Vaish. A row at 'applied' is not touched by a sidecar.
  4. Idempotent: running twice changes nothing the second time.

Run: python pipeline/test_sync.py   (temp dirs + temp DB; no network, no LLM)
The Word-render checks are skipped automatically when Word is unavailable, so
this suite is green in the cloud environment too.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import db  # noqa: E402
import ingest_scores  # noqa: E402
import sync  # noqa: E402
from docx_engine import PASS1_SUFFIX  # noqa: E402

PASS = FAIL = SKIP = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"FAIL  {name}" + (f" — {detail}" if detail else ""))


def skip(name: str, why: str) -> None:
    global SKIP
    SKIP += 1
    print(f"skip  {name} — {why}")


def fresh_env(tmp: Path):
    (tmp / "tailored").mkdir(exist_ok=True)
    sync.TAILORED_DIR = tmp / "tailored"
    sync.ROOT = tmp
    return db.connect(tmp / "t.sqlite")


def add_job(conn, jid: str, status: str, notes: str = "auto-tailor; strong fit") -> None:
    conn.execute(
        "INSERT OR REPLACE INTO jobs (id, company, company_norm, title, title_norm, "
        "location, url, source, jd_text, posted_at, first_seen, status, notes) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (jid, "TestCo", "testco", "Product Manager", "product manager", "NY",
         "http://x", "test", "", "", db.now_iso(), status, notes))
    conn.commit()


def make_folder(tmp: Path, name: str, marker: str = "brief.md") -> str:
    """A stand-in application folder with a .docx so folder checks pass."""
    folder = tmp / "applications" / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "Vaishnav_Ajai_Resume_TestCo.docx").write_bytes(b"not a real docx")
    (folder / marker).write_text("**id:** `x`\n", encoding="utf-8")
    return f"applications/{name}"


def write_sidecar(tmp: Path, jid: str, **over) -> Path:
    rec = {"id": jid, "folder": make_folder(tmp, f"testco-pm-{jid[:6]}"),
           "status_intent": "tailored",
           "scores": {"ats": 82, "recruiter": 8, "hm": 7},
           "rounds": 1, "pages_soffice": 1,
           "resume": "Vaishnav_Ajai_Resume_TestCo.docx",
           "tailored_at": db.now_iso(), "tailorer": "routine", "model": "test"}
    rec.update(over)
    path = sync.TAILORED_DIR / f"{jid}.json"
    path.write_text(json.dumps(rec), encoding="utf-8")
    return path


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="sync_test_"))

    # -- validation ----------------------------------------------------------
    print("-- sidecar validation --")
    conn = fresh_env(tmp)

    write_sidecar(tmp, "a" * 40)
    good, bad, notes = sync.load_tailor_sidecars()
    check("a well-formed sidecar loads", len(good) == 1 and not bad, f"{good} {bad}")

    write_sidecar(tmp, "b" * 40, id="c" * 40)
    good, bad, notes = sync.load_tailor_sidecars()
    check("id/filename mismatch is rejected",
          any("does not match filename" in why for _, why in bad), str(bad))
    (sync.TAILORED_DIR / f"{'b' * 40}.json").unlink()

    write_sidecar(tmp, "e" * 40, status_intent="applied")
    good, bad, notes = sync.load_tailor_sidecars()
    check("status_intent outside tailored/needs_review is rejected",
          any("not tailored/needs_review" in why for _, why in bad), str(bad))
    (sync.TAILORED_DIR / f"{'e' * 40}.json").unlink()

    write_sidecar(tmp, "f" * 40, folder="applications/does-not-exist")
    good, bad, notes = sync.load_tailor_sidecars()
    check("nonexistent folder is rejected",
          any("does not exist" in why for _, why in bad), str(bad))
    (sync.TAILORED_DIR / f"{'f' * 40}.json").unlink()

    (sync.TAILORED_DIR / f"{'g' * 40}.json").write_text("{not json", encoding="utf-8")
    good, bad, notes = sync.load_tailor_sidecars()
    check("unreadable JSON is reported, not raised",
          any("unreadable" in why for _, why in bad), str(bad))
    (sync.TAILORED_DIR / f"{'g' * 40}.json").unlink()

    # A sidecar from before the v3 rebuild: verdict/converged/ats are the retired
    # vocabulary of the critic loop. They describe a resume that still exists, so
    # they must be IGNORED rather than treated as a shape the loader can reject —
    # rejecting one would make its folder invisible to the DB forever.
    legacy = "5" * 40
    legacy_path = write_sidecar(tmp, legacy, verdict="FAIL", converged="no (cap)",
                                ats={"raw": 61, "achievable": None,
                                     "closeable_open": 4},
                                status_intent="tailored")
    del_scores = json.loads(legacy_path.read_text(encoding="utf-8"))
    del_scores.pop("scores")
    legacy_path.write_text(json.dumps(del_scores), encoding="utf-8")
    good, bad, notes = sync.load_tailor_sidecars()
    check("a legacy sidecar (verdict/converged/ats, no scores) still loads",
          any(r["id"] == legacy for r in good)
          and not any(p.stem == legacy for p, _ in bad), f"{len(good)} {bad}")
    check("a FAIL verdict is no longer a reason to reject 'tailored'",
          not any("FAIL" in why for _, why in bad), str(bad))
    check("its retired fields are carried, not interpreted",
          next(r for r in good if r["id"] == legacy).get("verdict") == "FAIL")
    (sync.TAILORED_DIR / f"{legacy}.json").unlink()

    # -- the OPTIONAL `scores` block -----------------------------------------
    # Report data written by an unattended cloud writer. Nothing gates on it, so
    # a shape problem must cost the field and never the sidecar.
    print("\n-- optional scores field --")
    good, bad, notes = sync.load_tailor_sidecars()
    rec = next(r for r in good if r["id"] == "a" * 40)
    check("a valid scores block parses",
          rec["scores"] == {"ats": 82, "recruiter": 8, "hm": 7}, str(rec["scores"]))
    check("a valid scores block raises no note", not notes, str(notes))

    no_scores = "7" * 40
    write_sidecar(tmp, no_scores, scores=None)
    good, bad, notes = sync.load_tailor_sidecars()
    check("a sidecar with no scores is fine (old sidecars)",
          next(r for r in good if r["id"] == no_scores)["scores"] is None
          and not any(p.stem == no_scores for p, _ in bad + notes), str(bad + notes))

    partial = "8" * 40
    write_sidecar(tmp, partial, scores={"ats": 74, "recruiter": None})
    good, bad, notes = sync.load_tailor_sidecars()
    check("a missing or null member is tolerated, not malformed",
          next(r for r in good if r["id"] == partial)["scores"]
          == {"ats": 74, "recruiter": None, "hm": None},
          str(next(r for r in good if r["id"] == partial)["scores"]))

    bad_scores = "9" * 40
    write_sidecar(tmp, bad_scores, scores={"ats": "eighty-two", "recruiter": 8,
                                           "hm": 7})
    good, bad, notes = sync.load_tailor_sidecars()
    check("a malformed scores block does NOT reject the sidecar",
          any(r["id"] == bad_scores for r in good)
          and not any(p.stem == bad_scores for p, _ in bad), f"{len(good)} {bad}")
    check("the malformed scores field is dropped from the record",
          next(r for r in good if r["id"] == bad_scores)["scores"] is None)
    check("dropping it appends a note to the run summary",
          any(p.stem == bad_scores and "scores.ats" in why for p, why in notes),
          str(notes))

    out_of_range = "4" * 40
    write_sidecar(tmp, out_of_range, scores={"ats": 82, "recruiter": 80, "hm": 7})
    good, bad, notes = sync.load_tailor_sidecars()
    check("a score outside its range is dropped with a note, sidecar kept",
          next(r for r in good if r["id"] == out_of_range)["scores"] is None
          and any(p.stem == out_of_range and "outside 0-10" in why
                  for p, why in notes), str(notes))

    shape = "6" * 40
    write_sidecar(tmp, shape, scores=[82, 8, 7])
    good, bad, notes = sync.load_tailor_sidecars()
    check("a non-object scores is dropped, not raised",
          any(r["id"] == shape for r in good)
          and any(p.stem == shape and "not an object" in why
                  for p, why in notes), str(notes))

    for jid in (no_scores, partial, bad_scores, out_of_range, shape):
        (sync.TAILORED_DIR / f"{jid}.json").unlink()

    # -- the notes clause helper ---------------------------------------------
    # `notes` is a `;`-separated line whose first clause is the scorer's lane.
    # Anything the dashboard needs to read off a row without a schema change
    # lands after it, and a re-run must rewrite its clause rather than stack a
    # second copy — sync.py's push retry replays the whole fold verbatim.
    print("\n-- set_note_clause --")
    note_id = "c" * 40
    add_job(conn, note_id, "scored", "auto-tailor; strong fit")
    after = sync.set_note_clause(conn, note_id, "scores", "82/8/7")
    check("a new clause is appended after the lane",
          after == "auto-tailor; strong fit; scores=82/8/7", after)
    after = sync.set_note_clause(conn, note_id, "word_pages", 2)
    check("a second key appends rather than replacing",
          after == "auto-tailor; strong fit; scores=82/8/7; word_pages=2", after)
    after = sync.set_note_clause(conn, note_id, "scores", "90/9/8")
    check("an existing key is replaced in place, not stacked",
          after == "auto-tailor; strong fit; scores=90/9/8; word_pages=2", after)
    check("the lane is still clause one", sync.lane_of(after), "auto-tailor")
    check("it is written through to the row",
          conn.execute("SELECT notes FROM jobs WHERE id=?",
                       (note_id,)).fetchone()[0] == after)
    check("replaying it is a no-op",
          sync.set_note_clause(conn, note_id, "scores", "90/9/8") == after)
    check("an unknown row is reported as empty, not raised",
          sync.set_note_clause(conn, "0" * 40, "scores", "1/1/1") == "")
    # A row with no notes at all: the clause lands first, where lane_of looks.
    # Pinned because it is the one shape that could ever be mistaken for a lane,
    # and because it is why no clause key may be given a lane's spelling.
    bare = "e" * 40
    add_job(conn, bare, "new", "")
    check("an empty notes field takes the clause as-is",
          sync.set_note_clause(conn, bare, "scores", "82/8/7") == "scores=82/8/7")
    check("and it is not a lane any reader recognises",
          sync.lane_of("scores=82/8/7") not in ("auto-tailor", "digest",
                                                "digest-stretch", "skip"))
    conn.execute("DELETE FROM jobs WHERE id IN (?, ?)", (note_id, bare))
    conn.commit()

    # -- status transitions (Word stubbed: page logic under test, not COM) ----
    print("\n-- status transitions --")
    real_check = sync.word_page_check

    def stub(pages, note=""):
        # `apply` gates whether a MISSING canonical PDF is rebuilt; --dry-run must
        # be able to reach a page count without touching a shipped artifact.
        return lambda rec, apply=True: (pages, note)

    jid = "a" * 40
    add_job(conn, jid, "scored")
    sync.word_page_check = stub(1)
    res = sync.ingest_tailors(conn, apply=True)
    check("scored -> tailored when Word agrees on 1 page",
          conn.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()[0]
          == "tailored", str(res["applied"]))
    check("the sidecar's scores land on the row as a notes clause",
          "scores=82/8/7" in conn.execute(
              "SELECT notes FROM jobs WHERE id=?", (jid,)).fetchone()[0],
          conn.execute("SELECT notes FROM jobs WHERE id=?", (jid,)).fetchone()[0])
    check("ingest carries the scores through to the run summary",
          res["scores"].get(jid) == {"ats": 82, "recruiter": 8, "hm": 7},
          str(res["scores"]))

    res2 = sync.ingest_tailors(conn, apply=True)
    check("second run is a no-op (idempotent)",
          not res2["applied"] and len(res2["skipped"]) == 1, str(res2))
    check("and the notes clause did not stack",
          conn.execute("SELECT notes FROM jobs WHERE id=?",
                       (jid,)).fetchone()[0].count("scores=") == 1)

    # Word disagreeing used to force needs_review. It now flags the row and lets
    # the Routine's own intent stand: the cloud reviewed this resume, and a
    # script that cannot reflow one has no business overruling that.
    jid2 = "1" * 40
    write_sidecar(tmp, jid2)
    add_job(conn, jid2, "scored")
    sync.word_page_check = stub(2)
    res = sync.ingest_tailors(conn, apply=True)
    status = conn.execute("SELECT status FROM jobs WHERE id=?", (jid2,)).fetchone()[0]
    check("Word saying 2 pages no longer downgrades", status == "tailored",
          f"got {status}")
    check("the disagreement is reported as a word check",
          [(j, p) for j, p, _ in res["word_check"]] == [(jid2, 2)],
          str(res["word_check"]))
    check("and lands on the row as word_pages=2",
          "word_pages=2" in conn.execute(
              "SELECT notes FROM jobs WHERE id=?", (jid2,)).fetchone()[0],
          conn.execute("SELECT notes FROM jobs WHERE id=?", (jid2,)).fetchone()[0])

    # The flag must not latch. A folder re-tailored down to one page keeps the
    # old `word_pages=2` forever unless the clause is rewritten whenever the
    # check runs — and by then the row sits at `tailored`, so nothing else would
    # ever clear it. Same rule the sidecar reconcile obeys: permanent is fine,
    # stale is not.
    db.set_status(conn, jid2, "scored", via="test")
    sync.word_page_check = stub(1)
    sync.ingest_tailors(conn, apply=True)
    notes2 = conn.execute("SELECT notes FROM jobs WHERE id=?", (jid2,)).fetchone()[0]
    check("a resume that now renders on one page loses the 2-page flag",
          "word_pages=2" not in notes2 and "word_pages=1" in notes2, notes2)
    check("and the clause was replaced, not duplicated",
          notes2.count("word_pages=") == 1, notes2)

    jid3 = "2" * 40
    write_sidecar(tmp, jid3)
    add_job(conn, jid3, "scored")
    sync.word_page_check = stub(None, "Word render failed: no COM")
    res = sync.ingest_tailors(conn, apply=True)
    status = conn.execute("SELECT status FROM jobs WHERE id=?", (jid3,)).fetchone()[0]
    check("an unavailable Word check does not hold the row back",
          status == "tailored", f"got {status}")
    check("an unrenderable resume is flagged with ?",
          "word_pages=?" in conn.execute(
              "SELECT notes FROM jobs WHERE id=?", (jid3,)).fetchone()[0],
          conn.execute("SELECT notes FROM jobs WHERE id=?", (jid3,)).fetchone()[0])

    jid4 = "3" * 40
    write_sidecar(tmp, jid4)
    add_job(conn, jid4, "applied")
    sync.word_page_check = stub(1)
    res = sync.ingest_tailors(conn, apply=True)
    status = conn.execute("SELECT status FROM jobs WHERE id=?", (jid4,)).fetchone()[0]
    check("a sidecar never outranks 'applied'", status == "applied", f"got {status}")
    check("outranked row is reported as skipped",
          any(j == jid4 for j, _ in res["skipped"]), str(res["skipped"]))
    check("and an outranked row gets no notes clause either",
          "scores=" not in (conn.execute(
              "SELECT notes FROM jobs WHERE id=?", (jid4,)).fetchone()[0] or ""))

    jid5 = "4" * 40
    write_sidecar(tmp, jid5, status_intent="needs_review")
    add_job(conn, jid5, "scored")
    res = sync.ingest_tailors(conn, apply=True)
    status = conn.execute("SELECT status FROM jobs WHERE id=?", (jid5,)).fetchone()[0]
    check("status_intent needs_review is applied as given", status == "needs_review",
          f"got {status}")

    jid6 = "6" * 40
    write_sidecar(tmp, jid6)
    res = sync.ingest_tailors(conn, apply=True)
    check("a sidecar with no DB row is reported, not written",
          jid6 in res["missing_row"], str(res["missing_row"]))

    # An age sweep raced the fold (10-01..10-04: laptop off, detect.yml swept
    # every 2h, 32 reviewed resumes refused). Age is not a verdict on a finished
    # resume; a delisted posting is gone and stays refused.
    print("\n-- stale_skip vs a tailor sidecar --")
    aged = "7" * 40
    write_sidecar(tmp, aged)
    add_job(conn, aged, "stale_skip", "")
    gone = "8" * 40
    write_sidecar(tmp, gone)
    add_job(conn, gone, "stale_skip", "auto-tailor; fit; delisted: no longer on greenhouse board")
    sync.word_page_check = stub(1)
    res = sync.ingest_tailors(conn, apply=True)
    st = lambda j: conn.execute("SELECT status FROM jobs WHERE id=?", (j,)).fetchone()[0]
    check("an age-swept row takes its finished resume", st(aged) == "tailored", st(aged))
    check("and the summary names the real prior status",
          any(j == aged and prior == "stale_skip" for j, prior, _, _ in res["applied"]),
          str(res["applied"]))
    check("a delisted row is still refused", st(gone) == "stale_skip", st(gone))
    month = "5" * 40
    write_sidecar(tmp, month, tailored_at="2026-08-23T20:05:00Z")
    add_job(conn, month, "stale_skip", "")
    sync.ingest_tailors(conn, apply=True)
    check("a resume older than a week is not revived", st(month) == "stale_skip", st(month))

    # --ci: no Word on a runner, and no permanent `word_pages=?` either.
    ci = "9" * 40
    write_sidecar(tmp, ci)
    add_job(conn, ci, "scored")
    sync.word_page_check = lambda rec, apply=True: (_ for _ in ()).throw(
        AssertionError("Word called in CI"))
    sync.CI = True
    try:
        res = sync.ingest_tailors(conn, apply=True)
    finally:
        sync.CI = False
    notes_ci = conn.execute("SELECT notes FROM jobs WHERE id=?", (ci,)).fetchone()[0]
    check("CI folds without calling Word", st(ci) == "tailored", st(ci))
    check("and leaves no word_pages clause", "word_pages" not in notes_ci, notes_ci)
    check("but the scores still land", "scores=" in notes_ci, notes_ci)

    sync.word_page_check = real_check

    # -- review.md is what a sidecar's scores are reconciled against ----------
    # The sidecar is written once; review.md is rewritten by every review round.
    # A sidecar frozen at round 1 is what the cloud reads on its next run, so the
    # last SCORES line in review.md wins.
    print("\n-- tailor sidecar reconciliation against review.md --")
    rec_id = "d" * 40
    rel = make_folder(tmp, "testco-pm-review")
    (sync.TAILORED_DIR / f"{rec_id}.json").write_text(json.dumps(
        {"id": rec_id, "folder": rel, "status_intent": "tailored",
         "scores": {"ats": 61, "recruiter": 6, "hm": 6}}), encoding="utf-8")
    (tmp / rel / "review.md").write_text(
        "## ats-scanner\n...\nSCORES: ats 61 / recruiter 6 / hm 6\n"
        "## round 2\n...\nSCORES: ats 88 / recruiter 9 / hm 8\n", encoding="utf-8")
    changed = sync.reconcile_tailor_sidecars(apply=True)
    after = json.loads((sync.TAILORED_DIR / f"{rec_id}.json").read_text(
        encoding="utf-8"))
    check("the LAST SCORES line wins",
          after["scores"] == {"ats": 88, "recruiter": 9, "hm": 8},
          str(after.get("scores")))
    check("the refresh is reported", changed == [(rec_id, "scores", "88/9/8")],
          str(changed))
    second = sync.reconcile_tailor_sidecars(apply=True)
    check("second pass is a no-op", second == [], str(second))
    (tmp / rel / "review.md").write_text(
        "no scores line here at all\n", encoding="utf-8")
    check("a review with no SCORES line leaves the sidecar alone",
          sync.reconcile_tailor_sidecars(apply=True) == []
          and json.loads((sync.TAILORED_DIR / f"{rec_id}.json").read_text(
              encoding="utf-8"))["scores"] == {"ats": 88, "recruiter": 9, "hm": 8})

    # The regex itself: it anchors at column zero on purpose (the tailor writes
    # the line as the file's last line, unmarked), and it must not match a
    # sentence that merely mentions the word.
    print("\n-- the SCORES line --")
    cases = [
        ("SCORES: ats 88 / recruiter 9 / hm 8", [("88", "9", "8")]),
        ("SCORES:ats 7/recruiter 1/hm 0", [("7", "1", "0")]),
        ("SCORES:   ats 100  /  recruiter 10  /  hm 10",
         [("100", "10", "10")]),
        ("intro\nSCORES: ats 70 / recruiter 7 / hm 6\n", [("70", "7", "6")]),
        ("  SCORES: ats 70 / recruiter 7 / hm 6", []),
        ("**SCORES: ats 70 / recruiter 7 / hm 6**", []),
        ("the SCORES: ats line explains the format", []),
        ("SCORES: ats 70 / hm 6", []),
    ]
    for text, want in cases:
        check(f"{text.splitlines()[0][:44]!r} -> {want}",
              sync.SCORES_LINE.findall(text) == want,
              str(sync.SCORES_LINE.findall(text)))

    # -- the folder marker: brief.md OR score.md -----------------------------
    # v3 folders carry the id in brief.md; folders tailored before the rebuild
    # carry it in score.md. detect_partial_tailors is the net that catches a run
    # that died before writing its sidecar, so a marker it cannot read makes the
    # one case that net exists for invisible.
    print("\n-- brief.md / score.md folder marker --")
    tmp4 = Path(tempfile.mkdtemp(prefix="sync_marker_"))
    (tmp4 / "tailored").mkdir()
    old_root, old_tailored = sync.ROOT, sync.TAILORED_DIR
    sync.ROOT, sync.TAILORED_DIR = tmp4, tmp4 / "tailored"
    conn4 = db.connect(tmp4 / "m.sqlite")
    try:
        new_id, old_id, done_id = "a" * 40, "b" * 40, "c" * 40
        for jid, name, marker in ((new_id, "newco-pm", "brief.md"),
                                  (old_id, "oldco-pm", "score.md"),
                                  (done_id, "doneco-pm", "brief.md")):
            folder = tmp4 / "applications" / name
            folder.mkdir(parents=True)
            (folder / marker).write_text(f"- **id:** `{jid}`\n", encoding="utf-8")
            add_job(conn4, jid, "scored")
        (tmp4 / "applications" / "no-marker-here").mkdir()
        (sync.TAILORED_DIR / f"{done_id}.json").write_text("{}", encoding="utf-8")

        folders = sync.application_folders()
        check("a brief.md folder is found", folders.get(new_id) is not None,
              str(folders))
        check("a score.md folder is still found",
              folders.get(old_id) is not None, str(folders))
        check("a folder with neither marker is not a folder",
              all(p.name != "no-marker-here" for p in folders.values()))

        found = sync.detect_partial_tailors(conn4, apply=True)
        check("both marker styles are caught as half-finished runs",
              sorted(j for j, _ in found) == sorted([new_id, old_id]), str(found))
        check("a folder whose sidecar exists is not partial",
              all(j != done_id for j, _ in found), str(found))
        check("and they are surfaced as needs_review",
              conn4.execute("SELECT status FROM jobs WHERE id=?",
                            (new_id,)).fetchone()[0] == "needs_review")
        # Both markers in one folder: the id must be read once, from brief.md.
        both = tmp4 / "applications" / "newco-pm"
        (both / "score.md").write_text("- **id:** `" + "f" * 40 + "`\n",
                                       encoding="utf-8")
        check("brief.md is read first when a folder has both",
              sync.application_folders().get(new_id) == both,
              str(sync.application_folders()))
    finally:
        conn4.close()
        sync.ROOT, sync.TAILORED_DIR = old_root, old_tailored
        shutil.rmtree(tmp4, ignore_errors=True)

    # -- sidecar/DB reconciliation -------------------------------------------
    # The cloud reads sidecars; everything local reads the DB. Step 2 restores
    # sidecars from the cloud branch, which is how new scores arrive and also how
    # local corrections got silently reverted (2026-08-17: 11 sidecars still said
    # auto-tailor for jobs the local side had de-laned).
    print("\n-- sidecar/DB reconciliation --")
    tmp2 = Path(tempfile.mkdtemp(prefix="sync_reconcile_"))
    (tmp2 / "scores").mkdir()
    (tmp2 / "queue").mkdir()
    sync.ROOT = tmp2
    ingest_scores.SCORES_DIR = tmp2 / "scores"
    ingest_scores.QUEUE_DIR = tmp2 / "queue"
    conn2 = db.connect(tmp2 / "r.sqlite")

    def put(jid, status, notes, side_note):
        conn2.execute(
            "INSERT OR REPLACE INTO jobs (id, company, company_norm, title, "
            "title_norm, location, url, source, jd_text, posted_at, first_seen, "
            "status, score, notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (jid, "TestCo", "testco", "Product Manager", "product manager", "NY",
             "u", "test", "", "", db.now_iso(), status, 8, notes))
        conn2.commit()
        (tmp2 / "scores" / f"{jid}.json").write_text(json.dumps(
            {"id": jid, "score": 8, "note": side_note, "scored_at": db.now_iso(),
             "scorer": "routine", "model": "sonnet"}), encoding="utf-8")
        (tmp2 / "queue" / f"{jid}.json").write_text("{}", encoding="utf-8")

    a, b, c2 = "a" * 40, "b" * 40, "c" * 40
    put(a, "scored", "digest-stretch; senior title, (a)=1", "auto-tailor; strong fit")
    put(b, "skipped", "hard-filter:clearance_required", "auto-tailor; strong fit")
    put(c2, "scored", "auto-tailor; clean fit (a)=2", "auto-tailor; clean fit (a)=2")

    res = sync.reconcile_sidecars(conn2, apply=True)
    check("a de-laned job's sidecar is rewritten from the DB",
          any(j == a for j, *_ in res["relaned"]), str(res["relaned"]))
    check("the rewritten sidecar no longer says auto-tailor",
          json.loads((tmp2 / "scores" / f"{a}.json").read_text(
              encoding="utf-8"))["note"].startswith("digest-stretch"))
    check("a job that left the work set has its sidecar dropped",
          not (tmp2 / "scores" / f"{b}.json").exists(), "sidecar survived")
    check("an agreeing sidecar is left alone",
          (tmp2 / "scores" / f"{c2}.json").exists()
          and not any(j == c2 for j, *_ in res["relaned"] + res["dropped"]))
    res2 = sync.reconcile_sidecars(conn2, apply=True)
    check("reconcile is idempotent",
          not res2["relaned"] and not res2["dropped"], str(res2))

    # A branch overlaid main's sidecars at the start of its run; a re-score that
    # landed on main meanwhile is newer than the branch's copy (10-10 re-score).
    print("\n-- an older branch copy never replaces a newer sidecar --")
    def side(jid, at, note):
        (tmp2 / "scores" / f"{jid}.json").write_text(json.dumps(
            {"id": jid, "score": 8, "note": note, "scored_at": at}), encoding="utf-8")
    d, e, f = "d" * 40, "e" * 40, "f" * 40
    side(d, "2026-10-11T01:31:21Z", "digest; re-scored")
    side(e, "2026-10-10T09:00:00Z", "auto-tailor; old")
    before = sync.sidecar_times()
    side(d, "2026-10-09T22:00:00Z", "auto-tailor; branch copy")      # older: undone
    side(e, "2026-10-10T21:45:00Z", "skip; routine re-lane")         # newer: kept
    side(f, "2026-10-10T21:50:00Z", "digest; new score")             # new file: kept
    restored = sync.keep_newer_sidecars(before)
    note = lambda j: json.loads((tmp2 / "scores" / f"{j}.json").read_text(encoding="utf-8"))["note"]
    check("the older branch copy is undone", note(d) == "digest; re-scored", note(d))
    check("a newer branch copy is kept", note(e) == "skip; routine re-lane", note(e))
    check("a sidecar new on the branch is kept", note(f) == "digest; new score", note(f))
    check("only the stale one is reported", restored == [f"{d}.json"], str(restored))

    # 10-11: the probe branch carried reverted copies with the SAME scored_at.
    g, h = "1" * 40, "2" * 40
    side(g, "2026-10-11T01:31:21Z", "auto-tailor; re-scored to 10")
    side(h, "2026-10-11T01:31:21Z", "digest; main")
    before = sync.sidecar_times()
    side(g, "2026-10-11T01:31:21Z", "digest-stretch; carried, not changed by the branch")
    side(h, "2026-10-11T01:31:21Z", "skip; the branch's own re-lane")
    restored = sync.keep_newer_sidecars(before, changed={f"{h}.json"})
    check("a copy the branch only carried keeps main's version (same scored_at)",
          note(g) == "auto-tailor; re-scored to 10", note(g))
    check("a sidecar the branch changed is taken", note(h) == "skip; the branch's own re-lane",
          note(h))
    check("the carried one is reported", restored == [f"{g}.json"], str(restored))
    shutil.rmtree(tmp2, ignore_errors=True)

    # -- the real Word path, against a COPY of a real shipped resume ---------
    # A copy, not the original: a test suite must never mutate a shipped
    # artifact. The first version of this test did, and dirtied the Microsoft
    # resume. word_page_check no longer overwrites a canonical PDF at all, but
    # it does rebuild a MISSING one (the sandbox has no PDF), so the copy still
    # earns its keep.
    print("\n-- Word page cross-check --")
    # `resume.pass1.docx` is excluded for the same reason dashboard.deliverable_files
    # excludes it: it is a banked draft, never rendered to PDF, and it sorts
    # FIRST in most folders -- so taking shipped[0] blindly picked a docx that
    # has no sibling PDF by design and failed "the real shipped PDF was not
    # touched" against a correct repo.
    shipped = [p for p in sorted((ROOT / "applications").glob("*/*.docx"))
               if not p.name.startswith("~$")
               and not p.name.endswith(PASS1_SUFFIX)]
    if not shipped:
        skip("Word re-renders a shipped resume to 1 page", "no shipped resume found")
    else:
        docx = shipped[0]
        sandbox = tmp / "applications" / docx.parent.name
        sandbox.mkdir(parents=True, exist_ok=True)
        shutil.copy2(docx, sandbox / docx.name)
        sync.ROOT = tmp
        rec = {"folder": f"applications/{docx.parent.name}", "resume": docx.name}

        check("resume_docx resolves the named file",
              sync.resume_docx(rec) == sandbox / docx.name, str(sync.resume_docx(rec)))
        pages, note = sync.word_page_check(rec)
        if pages is None and "render failed" in note:
            skip("Word counts a shipped resume at 1 page", note[:60])
        else:
            check("Word counts a shipped resume at exactly 1 page", pages == 1,
                  f"got {pages} ({note})")
            check("a missing canonical PDF is rebuilt beside the docx",
                  (sandbox / docx.name).with_suffix(".pdf").is_file(), note)
        check("the real shipped PDF was not touched",
              docx.with_suffix(".pdf").is_file()
              and (ROOT / "applications").exists())

    # ---- a folder the cloud is still writing is DEFERRED, never taken -------
    # A run commits after every job, so a folder reaches the branch long before
    # its last review round. Taken mid-run it lands on main frozen at that round,
    # every later sync skips it as present-locally, the sidecar arrives
    # separately and stamps a status onto stale content, and the cloud never
    # revisits it. The sidecar is the Routine's own "this job is finished" mark.
    print("\n-- deferred application folders --")
    done_rel = "applications/zz-test-finished-2026-08-24"
    wip_rel = "applications/zz-test-inflight-2026-08-24"
    check("fixture folders do not exist locally",
          not (ROOT / done_rel).exists() and not (ROOT / wip_rel).exists())

    real_run = sync.run
    checked_out = []
    NL = chr(10)

    def fake_run(cmd, *a, **k):
        class P:
            returncode = 0
            stdout = ""
        joined = " ".join(cmd)
        if "ls-tree" in joined and "tailored/" in joined:
            P.stdout = "tailored/aaa.json" + NL
        elif "ls-tree" in joined and "applications/" in joined:
            P.stdout = done_rel + "/brief.md" + NL + wip_rel + "/brief.md" + NL
        elif "show" in joined and "tailored/aaa.json" in joined:
            P.stdout = json.dumps({"id": "a" * 40, "folder": done_rel})
        elif "checkout" in joined:
            checked_out.append(cmd[-1])
        return P()

    sync.run = fake_run
    try:
        taken, kept, unfinished = sync.fetch_new_application_folders(
            sync.JUDGE_BRANCH, apply=True)
    finally:
        sync.run = real_run

    check("one branch family is what the fold reads",
          sync.JUDGE_BRANCH == "claude/judge" and sync.JUDGE_BRANCH_PREFIX == "claude/judge")

    # The cloud names each run's branch `claude/judge-<run>` and lets it push
    # nowhere else (first run, 2026-09-18). The family is read oldest first so a
    # later run's sidecar wins, and the reset step deletes the suffixed ones
    # while pointing the bare one at main.
    calls = []

    def family_run(cmd, *a, **k):
        class P:
            returncode = 0
            stdout = ""
        calls.append(cmd)
        if cmd[:3] == ["git", "ls-remote", "--heads"]:
            P.stdout = ("aaa\trefs/heads/claude/judge-new" + NL
                        + "bbb\trefs/heads/claude/judge" + NL
                        + "ccc\trefs/heads/claude/judge-old" + NL)
        elif cmd[:2] == ["git", "log"]:
            P.stdout = {"origin/claude/judge-new": "300", "origin/claude/judge": "100",
                        "origin/claude/judge-old": "200"}[cmd[-1]]
        return P()

    sync.run = family_run
    try:
        fam = sync.judge_branches()
        done = sync.reset_cloud_branches({"claude/judge": "bbb", "claude/judge-old": "ccc"})
    finally:
        sync.run = real_run
    check("every claude/judge* branch is read, oldest commit first",
          fam == ["claude/judge", "claude/judge-old", "claude/judge-new"], str(fam))
    pushes = [c for c in calls if c[:2] == ["git", "push"]]
    check("the bare branch is pointed at main",
          any(c[-1] == "main:refs/heads/claude/judge" for c in pushes), str(pushes))
    check("a run-suffixed branch is deleted under its lease",
          any(c[-1] == ":refs/heads/claude/judge-old"
              and "--force-with-lease=refs/heads/claude/judge-old:ccc" in c for c in pushes),
          str(pushes))
    check("both resets reported", sorted(done) == ["claude/judge", "claude/judge-old"], str(done))

    # Both sidecar sets come off the same branch in one checkout now, and one
    # pathspec matching nothing fails the WHOLE checkout. A run that scored the
    # queue and tailored nothing pushes scores/ with no tailored/ — so the fold
    # must fall back per path or every new score is dropped in silence.
    attempts = []

    def half_branch_run(cmd, *a, **k):
        class P:
            returncode = 0
            stdout = ""
        if cmd[:2] == ["git", "checkout"]:
            wanted = cmd[cmd.index("--") + 1:]
            attempts.append(wanted)
            P.returncode = 0 if wanted == ["scores/"] else 1
        return P()

    sync.run = half_branch_run
    try:
        got = sync.fetch_branch_paths(sync.JUDGE_BRANCH, ["scores/", "tailored/"])
    finally:
        sync.run = real_run
    check("a branch with only scores/ still yields its scores", got, str(attempts))
    check("it tried both together first, then one at a time",
          attempts == [["scores/", "tailored/"], ["scores/"], ["tailored/"]],
          str(attempts))
    check("a folder its sidecar vouches for is taken", taken == [done_rel], str(taken))
    check("a folder with no sidecar on the branch is deferred",
          unfinished == [wip_rel], str(unfinished))
    check("the deferred folder is never checked out",
          wip_rel not in checked_out, str(checked_out))
    check("the finished folder IS checked out", done_rel in checked_out, str(checked_out))

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{PASS} passed, {FAIL} failed, {SKIP} skipped")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
