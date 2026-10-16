"""Storage-consistency tests for sync.py. Run: python pipeline/test_sync_consistency.py

Every case here is a failure that actually happened or was one scheduled run
away from happening on 2026-08-18, so each test names the thing it protects:

  - a branch checkout silently reverting a folder main had moved past
  - a push race leaving the repo mid-rebase and wedging every later run
  - --dry-run rewriting a shipped resume's PDF
  - a queue file and a DB row losing each other, invisibly to both layers
  - a Vercel config that reads protected while one URL is world-readable
  - a resume open in Acrobat killing the pull half-way and stranding the stash
  - a push race resetting --hard over commits only a human could recreate

The git tests build a real repo with a real remote, because the bugs they cover
live in git's behaviour (checkout is not a merge; a binary cannot be rebased),
not in ours.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}  (got {got!r}, want {want!r})")
        FAILS.append(label)


def _render_dashboard() -> str:
    """The page dashboard.py actually writes, into a throwaway path.

    A subprocess rather than a call to main(): main() parses sys.argv and writes
    a file, and shelling out is the same path CI and sync.py both take. Rendered
    to a temp dir so a test run never touches dashboard/index.html.
    """
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "index.html"
        p = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parent / "dashboard.py"),
             "--out", str(out)],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        if p.returncode != 0:
            raise RuntimeError(f"dashboard.py failed: {p.stderr[:400]}")
        return out.read_text(encoding="utf-8")


def git(cwd, *args, check_rc=True):
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if check_rc and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {p.stderr[:300]}")
    return p


# ------------------------------------------------------------- queue <-> DB

def test_queue_reconcile():
    import db
    import sync
    print("queue/DB reconcile (both directions)")

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        qdir = td / "queue"
        qdir.mkdir()
        conn = db.connect(td / "t.sqlite")
        old_q = sync.QUEUE_DIR
        sync.QUEUE_DIR = qdir
        try:
            # (a) a row with no queue file: scrape.py commits the row first, so a
            # crash between the two writes strands it at 'new' forever.
            jid = db.insert_job(conn, "Acme", "Product Manager", "Boston, MA",
                                "http://x", "greenhouse", jd_text="jd body",
                                posted_at="2026-08-18")
            res = sync.reconcile_queue(conn, apply=False)
            check("row without queue file is found", [r[0] for r in res["requeued"]], [jid])
            check("nothing invented in dry run", (qdir / f"{jid}.json").exists(), False)

            # (b) a queue file with no row: the worst case, because the cloud
            # rescores it every run while ingest drops the sidecar as 'unknown'.
            oid = db.job_id("Globex", "TPM", "Remote - US")
            orphan = {"id": oid, "company": "Globex", "title": "TPM",
                      "location": "Remote - US", "url": "http://y",
                      "source": "jobspy:indeed", "posted_at": "2026-08-18",
                      "detected_at": "2026-08-18T00:00:00Z", "jd_markdown": "jd"}
            (qdir / f"{oid}.json").write_text(json.dumps(orphan), encoding="utf-8")
            res = sync.reconcile_queue(conn, apply=False)
            check("queue file without a row is found",
                  [r[0] for r in res["inserted"]], [oid])

            # Applying repairs both, and a second pass finds nothing left.
            sync.reconcile_queue(conn, apply=True)
            check("queue file re-emitted", (qdir / f"{jid}.json").exists(), True)
            check("orphan queue file now has a row",
                  conn.execute("SELECT status FROM jobs WHERE id=?", (oid,)).fetchone()[0],
                  "new")
            again = sync.reconcile_queue(conn, apply=True)
            check("converges: nothing left to repair",
                  (again["requeued"], again["inserted"], again["failed"]), ([], [], []))
            body = json.loads((qdir / f"{jid}.json").read_text(encoding="utf-8"))
            check("re-emitted entry carries the JD", body["jd_markdown"], "jd body")
            check("re-emitted entry keeps its id", body["id"], jid)

            # A queue file whose id is not the hash of its own contents cannot be
            # repaired into a matching row, and must be reported rather than
            # leaving a row and a file that will never find each other.
            (qdir / f"{'f' * 40}.json").write_text(
                json.dumps(dict(orphan, id="f" * 40, title="Staff TPM")), encoding="utf-8")
            bad = sync.reconcile_queue(conn, apply=True)
            check("hash mismatch reported, not hidden",
                  [j for j, _ in bad["failed"]], ["f" * 40])
        finally:
            sync.QUEUE_DIR = old_q
            conn.close()


# --------------------------------------------------------- jd_text shedding

def test_jd_text_shed():
    import db
    import sync
    print("jd_text shed keeps the work set, empties the rest")

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        qdir = td / "queue"
        qdir.mkdir()
        conn = db.connect(td / "t.sqlite")
        old_q = sync.QUEUE_DIR
        sync.QUEUE_DIR = qdir
        try:
            live = db.insert_job(conn, "Acme", "Product Manager", "Boston, MA",
                                 "http://x", "greenhouse", jd_text="live jd body",
                                 posted_at="2026-08-18")
            scored = db.insert_job(conn, "Globex", "TPM", "Remote - US",
                                   "http://y", "lever", jd_text="scored jd body",
                                   posted_at="2026-08-18")
            db.set_score(conn, scored, 8)
            dead = db.insert_job(conn, "Initech", "Senior Director PM", "Austin, TX",
                                 "http://z", "jobspy:indeed", jd_text="X" * 400_000,
                                 status="skipped")

            shed, freed = db.shed_jd_text(conn)
            check("only the settled row is shed", shed, 1)
            check("freed bytes counted", freed, 400_000)
            check("new row keeps its jd",
                  conn.execute("SELECT jd_text FROM jobs WHERE id=?", (live,)).fetchone()[0],
                  "live jd body")
            check("scored row keeps its jd",
                  conn.execute("SELECT jd_text FROM jobs WHERE id=?", (scored,)).fetchone()[0],
                  "scored jd body")
            check("settled row is emptied",
                  conn.execute("SELECT jd_text FROM jobs WHERE id=?", (dead,)).fetchone()[0],
                  "")
            check("idempotent", db.shed_jd_text(conn), (0, 0))

            # The invariant the shed must never break: reconcile_queue can still
            # re-emit a work-set queue file from the DB afterwards.
            res = sync.reconcile_queue(conn, apply=True)
            check("re-emit still works post-shed",
                  sorted(r[0] for r in res["requeued"]), sorted([live, scored]))
            body = json.loads((qdir / f"{scored}.json").read_text(encoding="utf-8"))
            check("re-emitted entry still carries the JD",
                  body["jd_markdown"], "scored jd body")
        finally:
            sync.QUEUE_DIR = old_q
            conn.close()


# ------------------------------------------------------ publish-safety guard

def test_vercel_guard_refuses_public_configs():
    """The dashboard is only safe to publish while every production URL is gated.

    Live-tested against the real project on 2026-08-18 (a canary page proved
    both the leak and the detection), but the decision table itself has to stay
    covered here: the failure is silent, and Vercel accepting or renaming a
    deploymentType value must not quietly turn the check into a no-op.
    """
    import vercel_guard
    print("vercel guard refuses configs that leave production public")

    os.environ.setdefault("VERCEL_TOKEN", "t")
    os.environ.setdefault("VERCEL_ORG_ID", "team_x")
    os.environ.setdefault("VERCEL_PROJECT_ID", "prj_x")
    real_api = vercel_guard.api

    def stub(sso, domains):
        def _api(path):
            if path.endswith("/domains"):
                return 200, {"domains": [{"name": d} for d in domains]}
            return 200, {"ssoProtection": sso}
        return _api

    cases = [
        ({"deploymentType": "all_except_custom_domains"}, [], 0, "the shipped config"),
        ({"deploymentType": "all"}, [], 0, "Pro-grade protection"),
        (None, [], 1, "protection switched off"),
        ({"deploymentType": "only_preview"}, [], 1, "valid, truthy, production-public"),
        ({"deploymentType": "some_future_value"}, [], 1, "unrecognised is not assumed safe"),
        ({"deploymentType": "all_except_custom_domains"},
         ["your-dashboard.vercel.app"], 1, "a project domain exists"),
    ]
    try:
        for sso, domains, want, why in cases:
            vercel_guard.api = stub(sso, domains)
            check(f"{why} -> {'refuse' if want else 'allow'}",
                  vercel_guard.preflight(), want)
    finally:
        vercel_guard.api = real_api

    # Postflight's pass condition is the ABSENCE of the sentinel, so that the
    # check does not depend on guessing which of 401/404/interstitial Vercel
    # returns. That asymmetry is why this test matters: if the sentinel ever
    # stops appearing on the page, nothing goes red -- the leak check simply
    # never fires again, on every future deploy.
    #
    # This used to compare the constant against a hand-written copy of the
    # markup, which meant it could not see a redesign that reworded the heading.
    # Build the real page and look in it.
    import dashboard
    page = _render_dashboard()
    check("sentinel is the string the dashboard actually renders",
          dashboard.PUBLIC_SENTINEL in page, True)
    # It must survive as contiguous bytes: an anonymous fetch does not run JS and
    # reads only the first 600 KB, so markup splitting the title, or a page that
    # buries it under a large inline payload, defeats the check silently.
    head = page[:600_000]
    check("sentinel is inside the anonymously-fetched window",
          dashboard.PUBLIC_SENTINEL in head, True)

    # ...which is exactly why "not leaking" cannot be the only postflight test:
    # a dead page passes it too. Vercel BLOCKED four deployments on 2026-08-18
    # and the alias started answering 404 while the leak check stayed green.
    def state_stub(ready):
        def _api(path):
            return 200, {"targets": {"production": {"readyState": ready, "id": "dpl_x"}}}
        return _api
    try:
        for ready, want, why in [("READY", 0, "serving"),
                                 ("BLOCKED", 1, "Vercel blocked the deployment"),
                                 ("ERROR", 1, "build failed"),
                                 ("UNKNOWN", 1, "state could not be read")]:
            vercel_guard.api = state_stub(ready)
            check(f"{why} -> {'fail' if want else 'pass'}",
                  len(vercel_guard.check_serving()), want)
    finally:
        vercel_guard.api = real_api


# ------------------------------------------------- partial tailors / sidecars

def test_partial_and_sidecar_drift():
    import db
    import sync
    print("partial tailors and tailor-sidecar drift")

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        (td / "applications" / "acme-pm-2026-08-18").mkdir(parents=True)
        (td / "tailored").mkdir()
        conn = db.connect(td / "t.sqlite")
        jid = db.insert_job(conn, "Acme", "Product Manager", "Boston, MA",
                            "http://x", "greenhouse")
        db.set_score(conn, jid, 9, "auto-tailor; strong fit")
        folder = td / "applications" / "acme-pm-2026-08-18"
        # brief.md is the v3 marker; a folder written before the rebuild carries
        # the same id line in score.md and must stay findable either way.
        (folder / "brief.md").write_text(f"- **id:** `{jid}`\n", encoding="utf-8")

        old = (sync.TAILORED_DIR, sync.ROOT)
        sync.TAILORED_DIR, sync.ROOT = td / "tailored", td
        try:
            # A folder with an artifact, no sidecar, and a row still at 'scored'
            # is a cloud run that died partway. Nothing used to notice.
            found = sync.detect_partial_tailors(conn, apply=False)
            check("half-finished cloud run detected", [f[0] for f in found], [jid])
            check("dry run leaves the status alone",
                  conn.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()[0],
                  "scored")
            sync.detect_partial_tailors(conn, apply=True)
            check("surfaced as needs_review",
                  conn.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()[0],
                  "needs_review")

            # The pre-rebuild marker still works, because those folders exist and
            # nothing rewrites them.
            legacy = td / "applications" / "oldco-pm-2026-08-01"
            legacy.mkdir(parents=True)
            lid = db.insert_job(conn, "OldCo", "Product Manager", "Austin, TX",
                                "http://y", "lever")
            db.set_score(conn, lid, 8, "auto-tailor; strong fit")
            (legacy / "score.md").write_text(f"- **id:** `{lid}`\n", encoding="utf-8")
            check("a score.md folder is still found",
                  [f[0] for f in sync.detect_partial_tailors(conn, apply=False)],
                  [lid])

            # A permanent sidecar is fine; a permanent LIE is not. review.md is
            # rewritten by every review round, the sidecar is written once, and
            # the sidecar is what the cloud reads on its next run.
            (folder / "review.md").write_text(
                "## hiring-manager\nnotes\n\n"
                "SCORES: ats 88 / recruiter 9 / hm 8\n", encoding="utf-8")
            (td / "tailored" / f"{jid}.json").write_text(json.dumps({
                "id": jid, "folder": "applications/acme-pm-2026-08-18",
                "status_intent": "tailored",
                "scores": {"ats": 61, "recruiter": 6, "hm": 6},
            }), encoding="utf-8")
            changed = sync.reconcile_tailor_sidecars(apply=True)
            rec = json.loads((td / "tailored" / f"{jid}.json").read_text(encoding="utf-8"))
            check("stale scores corrected from review.md", rec["scores"],
                  {"ats": 88, "recruiter": 9, "hm": 8})
            check("the correction is reported", changed, [(jid, "scores", "88/9/8")])
            check("the status intent is left to the cloud", rec["status_intent"],
                  "tailored")
            check("second pass is a no-op", sync.reconcile_tailor_sidecars(apply=True), [])
        finally:
            sync.TAILORED_DIR, sync.ROOT = old
            conn.close()


# ------------------------------------------------------------------ dry run

def test_dry_run_leaves_pdf_alone():
    """Word never writes the canonical PDF — in a dry run OR a real run.

    Word's page count still gates readiness, but its PDF text layer sprays
    spaces inside compound keywords, so the shipped artifact stays the soffice
    render. The old contract (real run refreshes the PDF from Word) is exactly
    the defect; this test now pins the inverse.
    """
    import docx_engine
    import sync
    print("dry-run purity / Word never writes the shipped PDF")

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        folder = td / "applications" / "acme-pm-2026-08-18"
        folder.mkdir(parents=True)
        (folder / "Resume.docx").write_bytes(b"not really a docx")
        pdf = folder / "Resume.pdf"
        pdf.write_bytes(b"THE SHIPPED PDF")

        real = docx_engine.render_pdf
        seen = []

        def fake(d, o, renderer=None):
            seen.append(renderer)
            Path(o).write_bytes(f"rendered by {renderer}".encode())
            return Path(o), 1

        docx_engine.render_pdf = fake
        old_root = sync.ROOT
        sync.ROOT = td
        try:
            rec = {"id": "a" * 40, "folder": "applications/acme-pm-2026-08-18"}
            pages, note = sync.word_page_check(rec, apply=False)
            check("page count still returned", pages, 1)
            check("shipped PDF untouched in dry run", pdf.read_bytes(), b"THE SHIPPED PDF")
            sync.word_page_check(rec, apply=True)
            check("shipped PDF untouched in a REAL run too",
                  pdf.read_bytes(), b"THE SHIPPED PDF")
            check("the page check did render with Word", "word" in seen, True)

            # Only when no submittable artifact exists at all does sync rebuild
            # one — and with soffice, never by copying Word's scratch render.
            pdf.unlink()
            seen.clear()
            sync.word_page_check(rec, apply=True)
            check("a missing PDF is rebuilt with soffice",
                  pdf.read_bytes(), b"rendered by soffice")
            check("Word was still the counter", seen[0], "word")
        finally:
            docx_engine.render_pdf = real
            sync.ROOT = old_root


# --------------------------------------------------------------- git behaviour

def test_selective_application_checkout():
    import sync
    print("selective application-folder checkout")

    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "repo"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        git(repo, "config", "user.email", "t@t.t")
        git(repo, "config", "user.name", "t")
        (repo / "applications" / "tiktok-pm-2026-08-17").mkdir(parents=True)
        ledger = repo / "applications" / "tiktok-pm-2026-08-17" / "brief.md"
        ledger.write_text("must-haves: the cloud's first draft\n", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "cloud tailor")
        git(repo, "branch", "cloud")

        # main moves on: the correction made locally after the cloud wrote it.
        ledger.write_text("must-haves: corrected locally\n", encoding="utf-8")
        (repo / "applications" / "new-co-pm-2026-08-18").mkdir(parents=True)
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "local reclassification")

        # Simulate the branch being on a remote named origin.
        git(repo, "remote", "add", "origin", str(repo))
        git(repo, "fetch", "-q", "origin")

        old_root = sync.ROOT
        sync.ROOT = repo
        try:
            folders = sync.branch_application_folders("cloud")
            check("branch folder enumerated",
                  "applications/tiktok-pm-2026-08-17" in folders, True)

            # `git checkout <tree-ish> -- <path>` writes the tree AND stages it,
            # so it is a mutation and --dry-run must not do it. A dry run used to
            # stage ~250 sidecars while claiming to be report-only.
            sync.fetch_branch_paths("cloud", ["applications/"], apply=False)
            check("dry run stages nothing",
                  git(repo, "diff", "--cached", "--name-only").stdout.strip(), "")
            check("dry run leaves the ledger alone",
                  ledger.read_text(encoding="utf-8").strip(),
                  "must-haves: corrected locally")

            # Three-valued since 2026-08-24: a branch folder with no
            # `tailored/<id>.json` vouching for it is DEFERRED (a cloud run may
            # still be writing it) rather than kept or taken. This unpacked two
            # and had been crashing the suite ever since.
            taken, skipped, unfinished = sync.fetch_new_application_folders("cloud")
            check("existing folder skipped",
                  "applications/tiktok-pm-2026-08-17" in skipped, True)
            check("nothing taken (branch has no new folders)", taken, [])
            check("an existing folder is skipped, not deferred", unfinished, [])
            # The whole point: the older cloud copy did NOT overwrite main's.
            check("local ledger survived",
                  ledger.read_text(encoding="utf-8").strip(),
                  "must-haves: corrected locally")
        finally:
            sync.ROOT = old_root


def test_overlaid_folder_links_do_not_404():
    import dashboard
    print("blob links for overlaid folders")

    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "repo"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        git(repo, "config", "user.email", "t@t.t")
        git(repo, "config", "user.name", "t")
        (repo / "applications" / "onmain-pm-2026-08-10").mkdir(parents=True)
        (repo / "applications" / "onmain-pm-2026-08-10" / "score.md").write_text(
            "x\n", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "on main")

        # A folder that exists ONLY on the Routine branch, which is exactly what
        # CI's overlay step produces.
        git(repo, "checkout", "-q", "-b", "cloud")
        (repo / "applications" / "onbranch-pm-2026-08-18").mkdir(parents=True)
        (repo / "applications" / "onbranch-pm-2026-08-18" / "score.md").write_text(
            "y\n", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "cloud tailor")
        branch_sha = git(repo, "rev-parse", "HEAD").stdout.strip()
        git(repo, "checkout", "-q", "main")
        git(repo, "remote", "add", "origin", str(repo))
        git(repo, "fetch", "-q", "origin")
        # Overlay it the way dashboard.yml does.
        git(repo, "checkout", "origin/cloud", "--", "applications/")

        old = (dashboard.ROOT, dashboard.overlay_refs, dict(dashboard._blob_cache))
        dashboard.ROOT = repo
        dashboard.overlay_refs = lambda: ("origin/cloud",)
        dashboard._blob_cache.clear()
        try:
            on_main = dashboard.blob_base("applications/onmain-pm-2026-08-10")
            overlaid = dashboard.blob_base("applications/onbranch-pm-2026-08-18")
            check("a folder on main links to /blob/main/", "/blob/main/" in on_main, True)
            # Linking an overlaid folder under /blob/main/ 404s on exactly the
            # build the overlay exists to improve.
            check("an overlaid folder does NOT link to /blob/main/",
                  "/blob/main/" in overlaid, False)
            check("it links to the branch commit instead",
                  f"/blob/{branch_sha}/" in overlaid, True)
        finally:
            dashboard.ROOT, dashboard.overlay_refs = old[0], old[1]
            dashboard._blob_cache.clear()
            dashboard._blob_cache.update(old[2])


def test_push_race_never_wedges():
    import sync
    print("push race recovery")

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        bare = td / "remote.git"
        git(td, "init", "-q", "--bare", "-b", "main", str(bare))
        work = td / "work"
        other = td / "other"

        git(td, "clone", "-q", str(bare), str(work))
        git(work, "config", "user.email", "t@t.t")
        git(work, "config", "user.name", "t")
        git(work, "checkout", "-q", "-b", "main")
        # A committed BINARY, which is what makes a rebase unresolvable.
        (work / "jobs.sqlite").write_bytes(bytes(range(256)) * 8)
        git(work, "add", "-A")
        git(work, "commit", "-qm", "base")
        git(work, "push", "-q", "origin", "main")

        git(td, "clone", "-q", str(bare), str(other))
        git(other, "config", "user.email", "o@o.o")
        git(other, "config", "user.name", "o")
        (other / "jobs.sqlite").write_bytes(bytes(range(255, -1, -1)) * 8)
        (other / "queue_from_cron.json").write_text("{}", encoding="utf-8")
        git(other, "add", "-A")
        git(other, "commit", "-qm", "the cron got there first")
        git(other, "push", "-q", "origin", "main")

        # The baseline main() would capture: HEAD right after the pull, BEFORE
        # this run commits anything. In production that is always a commit
        # origin/main already has, so origin/main..baseline is empty and the
        # anti-clobber guard stays out of the way. Capturing it any later would
        # make this run's own commit look foreign and refuse every race.
        git(work, "fetch", "-q", "origin", "main")
        baseline = git(work, "rev-parse", "HEAD").stdout.strip()
        check("baseline is an ancestor of origin/main (so the guard is silent)",
              git(work, "rev-list", "--count",
                  f"origin/main..{baseline}").stdout.strip(), "0")

        # Our side commits a conflicting change to the same binary and pushes.
        (work / "jobs.sqlite").write_bytes(b"\x00\xff" * 512)
        git(work, "add", "-A")
        git(work, "commit", "-qm", "sync")

        old_root = sync.ROOT
        sync.ROOT = work
        refolds = {"n": 0}

        def refold():
            refolds["n"] += 1
            # What the real fold does after a reset: rewrite the DB and re-stage.
            (work / "jobs.sqlite").write_bytes(b"\x11\x22" * 512)
            git(work, "add", "-A")
            git(work, "commit", "-qm", "sync (refolded)")

        try:
            sync.push_with_retry(refold, baseline=baseline)
            pushed = True
        except RuntimeError:
            pushed = False
        finally:
            sync.ROOT = old_root

        check("push eventually succeeded", pushed, True)
        check("the fold was redone exactly once", refolds["n"], 1)
        check("no rebase left in progress",
              (work / ".git" / "rebase-merge").exists()
              or (work / ".git" / "rebase-apply").exists(), False)
        # The cron's work must survive: recovery takes the remote wholesale.
        check("the other writer's file survived",
              (work / "queue_from_cron.json").exists(), True)
        remote_log = git(bare, "log", "--oneline").stdout
        check("cron commit still on the remote", "the cron got there first" in remote_log, True)


def test_push_retry_refuses_to_destroy_handmade_commits():
    """A push race must never `reset --hard` over commits sync did not make.

    The recovery takes origin/main wholesale and calls refold(), which only
    redoes sync's own deterministic work -- so a hand-made unpushed commit is
    destroyed with no way back. Nearly happened 2026-08-19: a lock-blocked pull
    left three session commits unpushed, which is exactly this window.
    """
    import sync
    print("push retry refuses to clobber foreign commits")

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        bare = td / "remote.git"
        git(td, "init", "-q", "--bare", "-b", "main", str(bare))
        work, other = td / "work", td / "other"
        git(td, "clone", "-q", str(bare), str(work))
        git(work, "config", "user.email", "t@t.t")
        git(work, "config", "user.name", "t")
        git(work, "checkout", "-q", "-b", "main")
        (work / "jobs.sqlite").write_bytes(bytes(range(256)) * 8)
        git(work, "add", "-A")
        git(work, "commit", "-qm", "base")
        git(work, "push", "-q", "origin", "main")

        # A hand-made commit that never got pushed -- the thing to protect.
        (work / "LEARNINGS.md").write_text("a hard-won lesson\n", encoding="utf-8")
        git(work, "add", "-A")
        git(work, "commit", "-qm", "learnings: something only a human knows")
        baseline = git(work, "rev-parse", "HEAD").stdout.strip()

        # The cron gets to the remote first, so our push is rejected.
        git(td, "clone", "-q", str(bare), str(other))
        git(other, "config", "user.email", "o@o.o")
        git(other, "config", "user.name", "o")
        (other / "jobs.sqlite").write_bytes(bytes(range(255, -1, -1)) * 8)
        git(other, "add", "-A")
        git(other, "commit", "-qm", "the cron got there first")
        git(other, "push", "-q", "origin", "main")

        # ...and this run then makes its own commit on top.
        (work / "jobs.sqlite").write_bytes(b"\x00\xff" * 512)
        git(work, "add", "-A")
        git(work, "commit", "-qm", "sync")

        old_root = sync.ROOT
        sync.ROOT = work
        refolds = {"n": 0}
        try:
            sync.push_with_retry(lambda: refolds.__setitem__("n", refolds["n"] + 1),
                                 baseline=baseline)
            refused = False
        except RuntimeError as exc:
            refused = "refusing to reset" in str(exc)
        finally:
            sync.ROOT = old_root

        check("the reset was refused", refused, True)
        check("the fold was never redone", refolds["n"], 0)
        check("the hand-made commit is still reachable",
              "only a human knows" in git(work, "log", "--oneline").stdout, True)
        check("its content survived intact",
              (work / "LEARNINGS.md").read_text(encoding="utf-8"),
              "a hard-won lesson\n")
        check("no rebase left in progress",
              (work / ".git" / "rebase-merge").exists()
              or (work / ".git" / "rebase-apply").exists(), False)
        # Parked remotely, so the work survives even if this clone is lost.
        check("the commits were parked on a rescue ref",
              "rescue/" in git(bare, "branch", "--list", "rescue/*").stdout
              or bool(git(bare, "for-each-ref", "--format=%(refname)",
                          "refs/heads/rescue").stdout.strip()), True)


def _stranded_repo(td: Path, local_subjects: list[str]):
    """A clone whose unpushed commits rewrite the binary the cron has moved.

    Each subject in `local_subjects` becomes one local commit; a subject starting
    "sync:" rewrites jobs.sqlite (what sync commits), anything else writes a
    text file (what a hand-made session commit looks like).
    """
    bare = td / "remote.git"
    git(td, "init", "-q", "--bare", "-b", "main", str(bare))
    work, other = td / "work", td / "other"
    git(td, "clone", "-q", str(bare), str(work))
    git(work, "config", "user.email", "t@t.t")
    git(work, "config", "user.name", "t")
    git(work, "checkout", "-q", "-b", "main")
    (work / "jobs.sqlite").write_bytes(bytes(range(256)) * 8)
    (work / "notes.md").write_text("base\n", encoding="utf-8")
    git(work, "add", "-A")
    git(work, "commit", "-qm", "base")
    git(work, "push", "-q", "origin", "main")

    for i, subject in enumerate(local_subjects):
        if subject.startswith("sync:"):
            (work / "jobs.sqlite").write_bytes(bytes([i]) * 2048)
        else:
            (work / f"hand{i}.md").write_text("only a human knows\n", encoding="utf-8")
        git(work, "add", "-A")
        git(work, "commit", "-qm", subject)

    git(td, "clone", "-q", str(bare), str(other))
    git(other, "config", "user.email", "o@o.o")
    git(other, "config", "user.name", "o")
    (other / "jobs.sqlite").write_bytes(bytes(range(255, -1, -1)) * 8)
    (other / "queue_from_cron.json").write_text("{}", encoding="utf-8")
    git(other, "add", "-A")
    git(other, "commit", "-qm", "layer1: the cron moved the DB")
    git(other, "push", "-q", "origin", "main")
    git(work, "fetch", "-q", "origin", "main")
    return bare, work


def test_stranded_sync_commit_never_wedges_the_pull():
    """A sync commit whose push died must not be replayed by the next pull.

    2026-10-02: DNS failed through all three push attempts, so the commit stayed
    local. Every later run aborted the stray rebase, then `pull --rebase`
    replayed the same commit onto the cron's newer jobs.sqlite and re-created
    the identical binary conflict. Sync was down from 10-01 to 10-04.
    """
    import sync
    print("stranded sync commit is dropped, not replayed")

    with tempfile.TemporaryDirectory() as td:
        bare, work = _stranded_repo(Path(td), ["sync: 17 file(s) @ 2026-10-02T16:51:52Z"])
        # Uncommitted work on a path the drop does not touch must survive it.
        (work / "notes.md").write_text("an edit Vaish left open\n", encoding="utf-8")

        old_root = sync.ROOT
        sync.ROOT = work
        try:
            dropped = sync.drop_stranded_sync_commits()
            sync.git_pull()
            pulled = True
        except RuntimeError:
            pulled = False
        finally:
            sync.ROOT = old_root

        check("the stranded commit was reported as dropped", len(dropped), 1)
        check("the pull succeeded", pulled, True)
        check("no rebase left in progress",
              (work / ".git" / "rebase-merge").exists()
              or (work / ".git" / "rebase-apply").exists(), False)
        check("HEAD is origin/main",
              git(work, "rev-parse", "HEAD").stdout.strip(),
              git(work, "rev-parse", "origin/main").stdout.strip())
        check("the cron's work arrived", (work / "queue_from_cron.json").exists(), True)
        check("the open edit survived",
              (work / "notes.md").read_text(encoding="utf-8"), "an edit Vaish left open\n")


def test_stranded_sync_commit_beside_a_handmade_one_is_parked_not_reset():
    """Mixed with a hand-made commit, nothing is reset: park and stop loudly."""
    import sync
    print("stranded sync commit beside a hand-made one is refused")

    with tempfile.TemporaryDirectory() as td:
        bare, work = _stranded_repo(Path(td), ["learnings: a hard-won lesson",
                                               "sync: 3 file(s) @ 2026-10-02T16:51:52Z"])
        before = git(work, "rev-parse", "HEAD").stdout.strip()
        old_root = sync.ROOT
        sync.ROOT = work
        try:
            sync.drop_stranded_sync_commits()
            refused = False
        except RuntimeError as exc:
            refused = "hand-made" in str(exc)
        finally:
            sync.ROOT = old_root

        check("the drop was refused", refused, True)
        check("HEAD did not move", git(work, "rev-parse", "HEAD").stdout.strip(), before)
        check("the commits were parked on a rescue ref",
              bool(git(bare, "for-each-ref", "--format=%(refname)",
                       "refs/heads/rescue").stdout.strip()), True)


def test_handmade_commits_alone_are_left_to_the_pull():
    import sync
    print("hand-made unpushed commits are not touched by the drop")

    with tempfile.TemporaryDirectory() as td:
        bare, work = _stranded_repo(Path(td), ["learnings: a hard-won lesson"])
        before = git(work, "rev-parse", "HEAD").stdout.strip()
        old_root = sync.ROOT
        sync.ROOT = work
        try:
            dropped = sync.drop_stranded_sync_commits()
        finally:
            sync.ROOT = old_root
        check("nothing dropped", dropped, [])
        check("HEAD did not move", git(work, "rev-parse", "HEAD").stdout.strip(), before)


def test_baseline_none_fails_closed():
    """No baseline means "I don't know whose commits these are" -- so refuse.

    main() always threads a real one, so None can only reach here from a caller
    that never considered the question, and the safe answer to that is never
    "go ahead and reset --hard".
    """
    import sync
    print("baseline=None fails closed")
    check("None is treated as 'foreign commits present'",
          bool(sync.unpushed_foreign_commits(None)), True)


def test_rewrite_path_set_covers_what_a_rebase_replays():
    """The lock gate must see paths the tree diff cannot show.

    `git diff --name-only origin/main..HEAD` is NOT the set of paths a rebase
    writes: for git diff, `A..B` means the endpoints `A B`, so it returns the
    same thing as `git diff HEAD origin/main`. A local commit that adds a file
    and a later one that deletes it leaves nothing in the tree diff, yet the
    rebase still writes that path -- and that write is what a lock blocks.
    """
    import sync
    print("rewrite path set covers replayed paths")

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        bare = td / "remote.git"
        git(td, "init", "-q", "--bare", "-b", "main", str(bare))
        work = td / "work"
        git(td, "clone", "-q", str(bare), str(work))
        git(work, "config", "user.email", "t@t.t")
        git(work, "config", "user.name", "t")
        git(work, "checkout", "-q", "-b", "main")
        (work / "base.txt").write_text("base\n", encoding="utf-8")
        git(work, "add", "-A")
        git(work, "commit", "-qm", "base")
        git(work, "push", "-q", "origin", "main")

        # add-then-delete: invisible to the tree diff, replayed by the rebase
        (work / "transient.txt").write_text("x\n", encoding="utf-8")
        git(work, "add", "-A")
        git(work, "commit", "-qm", "add transient")
        git(work, "rm", "-q", "transient.txt")
        git(work, "commit", "-qm", "delete transient")
        # a dirty tracked file, and an untracked one that must NOT appear
        (work / "base.txt").write_text("edited\n", encoding="utf-8")
        (work / "untracked.txt").write_text("scratch\n", encoding="utf-8")
        git(work, "fetch", "-q", "origin", "main")

        old_root = sync.ROOT
        sync.ROOT = work
        try:
            paths = sync.paths_git_may_rewrite()
            replayed = sync.local_commit_paths()
        finally:
            sync.ROOT = old_root

        check("the tree diff really is blind to it",
              git(work, "diff", "--name-only", "HEAD",
                  "origin/main").stdout.strip(), "")
        check("but local_commit_paths sees it", "transient.txt" in replayed, True)
        check("dirty tracked file is in the set", "base.txt" in paths, True)
        check("untracked file is NOT (autostash never stashes it)",
              "untracked.txt" in paths, False)
        check("every returned path exists on disk",
              all((work / p).is_file() for p in paths), True)


def test_lock_gate_defers_before_mutating():
    """A locked file must stop the run BEFORE anything is touched.

    The 2026-08-19 incident: git got as far as unlinking, died half-applied, and
    stranded the autostash. The invariant is that a blocked run leaves the repo
    byte-identical to how it found it.
    """
    import sync
    import winlock
    print("lock gate defers before mutating")

    if not winlock.IS_WINDOWS:
        print("  SKIP  (Windows-only: no mandatory file locking elsewhere)")
        return

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        bare = td / "remote.git"
        git(td, "init", "-q", "--bare", "-b", "main", str(bare))
        work, other = td / "work", td / "other"
        git(td, "clone", "-q", str(bare), str(work))
        git(work, "config", "user.email", "t@t.t")
        git(work, "config", "user.name", "t")
        git(work, "checkout", "-q", "-b", "main")
        pdf_rel = "applications/acme-pm-2026-08-19/Resume.pdf"
        (work / pdf_rel).parent.mkdir(parents=True)
        (work / pdf_rel).write_bytes(b"%PDF-1.7 v1")
        git(work, "add", "-A")
        git(work, "commit", "-qm", "base")
        git(work, "push", "-q", "origin", "main")

        # Someone else changes that very PDF, so the pull must rewrite it.
        git(td, "clone", "-q", str(bare), str(other))
        git(other, "config", "user.email", "o@o.o")
        git(other, "config", "user.name", "o")
        (other / pdf_rel).write_bytes(b"%PDF-1.7 v2-from-the-cloud")
        git(other, "add", "-A")
        git(other, "commit", "-qm", "cloud retailored it")
        git(other, "push", "-q", "origin", "main")

        old = (sync.ROOT, sync.LOCK_WAIT_ATTEMPTS, sync.LOCK_WAIT_DELAY_S,
               sync.STATE_PATH)
        sync.ROOT = work
        sync.LOCK_WAIT_ATTEMPTS, sync.LOCK_WAIT_DELAY_S = 2, 0
        sync.STATE_PATH = td / "sync_state.json"
        before_head = git(work, "rev-parse", "HEAD").stdout.strip()
        before_pdf = (work / pdf_rel).read_bytes()
        try:
            with winlock.hold(work / pdf_rel):
                locked = sync.lock_gate()
                check("the gate fires on the locked file", locked, [pdf_rel])
                check("HEAD did not move", git(work, "rev-parse",
                      "HEAD").stdout.strip(), before_head)
                check("the file is byte-identical",
                      (work / pdf_rel).read_bytes(), before_pdf)
                check("no stash was created",
                      git(work, "stash", "list").stdout.strip(), "")
                check("no rebase in progress",
                      (work / ".git" / "rebase-merge").exists()
                      or (work / ".git" / "rebase-apply").exists(), False)
            # Proving it RELEASES matters as much as proving it fires: a gate
            # stuck closed would stall the pipeline forever.
            check("the gate clears once the file is released", sync.lock_gate(), [])
        finally:
            (sync.ROOT, sync.LOCK_WAIT_ATTEMPTS, sync.LOCK_WAIT_DELAY_S,
             sync.STATE_PATH) = old


def test_lock_outside_the_rewrite_set_does_not_block():
    """A resume Vaish is reading must not stall a sync that never touches it.

    This is why the gate asks git what it will rewrite instead of scanning
    applications/ for open PDFs: in steady state a resume under review is not in
    that set, so the common case proceeds with Acrobat open. A blunter check
    would defer nearly every run and train Vaish to ignore it.
    """
    import sync
    import winlock
    print("a lock outside the rewrite set does not block")

    if not winlock.IS_WINDOWS:
        print("  SKIP  (Windows-only)")
        return

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        bare = td / "remote.git"
        git(td, "init", "-q", "--bare", "-b", "main", str(bare))
        work, other = td / "work", td / "other"
        git(td, "clone", "-q", str(bare), str(work))
        git(work, "config", "user.email", "t@t.t")
        git(work, "config", "user.name", "t")
        git(work, "checkout", "-q", "-b", "main")
        reading = "applications/acme-pm-2026-08-19/Resume.pdf"
        (work / reading).parent.mkdir(parents=True)
        (work / reading).write_bytes(b"%PDF-1.7 under review")
        (work / "queue").mkdir()
        (work / "queue" / "a.json").write_text("{}", encoding="utf-8")
        git(work, "add", "-A")
        git(work, "commit", "-qm", "base")
        git(work, "push", "-q", "origin", "main")

        # The cron changes only queue/, never the resume being read.
        git(td, "clone", "-q", str(bare), str(other))
        git(other, "config", "user.email", "o@o.o")
        git(other, "config", "user.name", "o")
        (other / "queue" / "b.json").write_text("{}", encoding="utf-8")
        git(other, "add", "-A")
        git(other, "commit", "-qm", "layer1: 1 new")
        git(other, "push", "-q", "origin", "main")

        old = (sync.ROOT, sync.LOCK_WAIT_ATTEMPTS, sync.LOCK_WAIT_DELAY_S)
        sync.ROOT = work
        sync.LOCK_WAIT_ATTEMPTS, sync.LOCK_WAIT_DELAY_S = 2, 0
        try:
            with winlock.hold(work / reading):
                check("the held resume is genuinely locked",
                      winlock.is_locked(work / reading), True)
                check("but the gate stays open (git will not rewrite it)",
                      sync.lock_gate(), [])
        finally:
            sync.ROOT, sync.LOCK_WAIT_ATTEMPTS, sync.LOCK_WAIT_DELAY_S = old


def test_orphaned_autostash_recovery_is_reversible():
    """Recover what a dead --autostash stranded, without ever half-applying.

    `git stash pop` on a conflict keeps the entry AND leaves a half-merged tree
    with no rollback point -- the state that made the incident read as "my edits
    vanished". apply -> verify -> drop (or reset --hard) always has a way back.
    """
    import sync
    print("orphaned autostash recovery is reversible")

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        work = td / "work"
        work.mkdir()
        git(work, "init", "-q", "-b", "main", ".")
        git(work, "config", "user.email", "t@t.t")
        git(work, "config", "user.name", "t")
        (work / "f.txt").write_text("base\n", encoding="utf-8")
        git(work, "add", "-A")
        git(work, "commit", "-qm", "base")

        old_root = sync.ROOT
        sync.ROOT = work
        try:
            # A stash of Vaish's is not ours to touch.
            (work / "f.txt").write_text("his work in progress\n", encoding="utf-8")
            git(work, "stash", "push", "-q", "-m", "wip")
            check("a hand-made stash is not mistaken for an autostash",
                  sync.orphaned_autostash(), None)
            check("and it is left alone", sync.recover_orphaned_autostash(), [])
            git(work, "stash", "drop", "-q")

            # Now one shaped like git's own autostash.
            (work / "f.txt").write_text("stranded work\n", encoding="utf-8")
            git(work, "stash", "push", "-q", "-m", "autostash")
            check("an autostash IS recognised",
                  sync.orphaned_autostash(), "stash@{0}")

            # Dirty tree -> skip, because the rollback would destroy that work.
            (work / "f.txt").write_text("something else\n", encoding="utf-8")
            check("recovery is skipped on a dirty tree",
                  sync.recover_orphaned_autostash(), [])
            check("and the stash survives for a human",
                  sync.orphaned_autostash(), "stash@{0}")

            # Clean tree, applies cleanly -> restore and drop.
            git(work, "checkout", "-q", "--", "f.txt")
            restored = sync.recover_orphaned_autostash()
            check("clean apply restores the paths", restored, ["f.txt"])
            check("the work is actually back",
                  (work / "f.txt").read_text(encoding="utf-8"), "stranded work\n")
            check("and the stash entry is gone",
                  git(work, "stash", "list").stdout.strip(), "")

            # A conflicting autostash must roll back, not half-apply.
            git(work, "checkout", "-q", "--", "f.txt")
            (work / "f.txt").write_text("stash side\n", encoding="utf-8")
            git(work, "stash", "push", "-q", "-m", "autostash")
            (work / "f.txt").write_text("committed side\n", encoding="utf-8")
            git(work, "add", "-A")
            git(work, "commit", "-qm", "diverged")
            check("a conflicting autostash restores nothing",
                  sync.recover_orphaned_autostash(), [])
            check("the tree is left clean, not half-merged",
                  git(work, "diff", "--name-only").stdout.strip(), "")
            check("no unmerged paths remain",
                  git(work, "diff", "--name-only",
                      "--diff-filter=U").stdout.strip(), "")
            check("and the stash is preserved for a human",
                  sync.orphaned_autostash(), "stash@{0}")
        finally:
            sync.ROOT = old_root


def test_defer_records_state_and_never_touches_the_db():
    """A defer records where Vaish looks, and never writes jobs.sqlite.

    Writing run outcomes into the DB would dirty a committed binary that
    origin/main also changes hourly, so the NEXT run's --autostash would stash a
    modified binary and conflict on reapply -- manufacturing the very failure
    this gate exists to prevent.
    """
    import sync
    print("defer records state without touching the DB")

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        old_state = sync.STATE_PATH
        sync.STATE_PATH = td / "sync_state.json"
        try:
            sync.record_outcome("deferred", "locked: a.pdf", ["a.pdf"])
            st = json.loads(sync.STATE_PATH.read_text(encoding="utf-8"))
            check("status recorded", st["status"], "deferred")
            check("first defer counts 1", st["consecutive"], 1)
            check("the locked path is named", st["paths"], ["a.pdf"])

            sync.record_outcome("deferred", "locked: a.pdf", ["a.pdf"])
            st = json.loads(sync.STATE_PATH.read_text(encoding="utf-8"))
            check("consecutive defers accumulate", st["consecutive"], 2)

            sync.record_outcome("ok", "0 need_review")
            st = json.loads(sync.STATE_PATH.read_text(encoding="utf-8"))
            check("a good run resets the counter", st["consecutive"], 0)
            check("and stamps last_ok", bool(st["last_ok"]), True)

            check("no jobs.sqlite was created anywhere near it",
                  list(td.glob("**/*.sqlite")), [])
        finally:
            sync.STATE_PATH = old_state


def test_repeat_trigger_short_circuits_only_after_success():
    """Repeats are free after a good run, and never skip a retry.

    The scheduled slot repeats every 15 min for an hour because that is the ONLY
    retry Windows gives us: -RestartCount does not fire on a nonzero action exit
    code (measured 2026-08-19 -- a probe exiting 2 recorded LastTaskResult=2 and
    was never restarted). So repeats must be cheap after success, and must still
    run after a defer, which is the whole point of having them.
    """
    import sync
    print("repeat trigger short-circuits only after success")

    with tempfile.TemporaryDirectory() as td:
        old_state = sync.STATE_PATH
        sync.STATE_PATH = Path(td) / "sync_state.json"
        try:
            check("no state at all -> run", sync.already_succeeded_recently(), "")

            sync.record_outcome("ok", "clean")
            check("just succeeded -> skip",
                  bool(sync.already_succeeded_recently()), True)

            sync.record_outcome("deferred", "locked: a.pdf", ["a.pdf"])
            check("deferred -> MUST run (this is the retry)",
                  sync.already_succeeded_recently(), "")

            sync.record_outcome("failed", "boom")
            check("failed -> MUST run", sync.already_succeeded_recently(), "")

            # An old success is a new slot, not a repeat of this one.
            sync.record_outcome("ok", "clean")
            state = json.loads(sync.STATE_PATH.read_text(encoding="utf-8"))
            state["last_ok"] = "2020-01-01T00:00:00Z"
            sync.STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
            check("a stale success -> run", sync.already_succeeded_recently(), "")

            # A clock that jumped backwards must not wedge the sync shut.
            state["last_ok"] = "2099-01-01T00:00:00Z"
            sync.STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
            check("a future timestamp -> run, never skip forever",
                  sync.already_succeeded_recently(), "")
        finally:
            sync.STATE_PATH = old_state


def test_layer1_clock_is_not_reset_by_sync():
    import db
    print("Layer 1 clock separation")

    import sqlite3
    with tempfile.TemporaryDirectory() as td:
        conn = db.connect(Path(td) / "t.sqlite")
        # Explicit timestamps: the detection run is OLDER than the sync, which is
        # exactly the arrangement that used to hide a dead cron.
        conn.execute("INSERT INTO runs (ts, summary, kind) VALUES "
                     "('2026-08-18T01:00:00Z', 'new=5 stale=1', 'layer1')")
        conn.execute("INSERT INTO runs (ts, summary, kind) VALUES "
                     "('2026-08-18T09:00:00Z', 'sync: tailors=0', 'sync')")
        conn.commit()

        # The clock's whole job is telling a dead cron from a quiet market.
        # Sharing a reading with sync meant every Layer 2 run reset it to green.
        check("Layer 1 clock ignores the newer sync row",
              db.last_run(conn)[1].startswith("new="), True)
        check("sync is still readable on its own",
              db.last_run(conn, kind="sync")[1].startswith("sync:"), True)
        check("kind=None still gives the newest of any kind",
              db.last_run(conn, kind=None)[1].startswith("sync:"), True)
        conn.close()

        # A DB written before the column existed: rows are tagged from the
        # summary prefix sync.py has always used, so the backfill is exact.
        legacy = Path(td) / "legacy.sqlite"
        raw = sqlite3.connect(legacy)
        raw.execute("CREATE TABLE runs (ts TEXT NOT NULL, summary TEXT DEFAULT '')")
        raw.execute("INSERT INTO runs VALUES ('2020-01-01T00:00:00Z', 'sync: ancient')")
        raw.execute("INSERT INTO runs VALUES ('2020-01-02T00:00:00Z', 'new=7 stale=0')")
        raw.commit()
        raw.close()

        migrated = db.connect(legacy)
        kinds = dict(migrated.execute("SELECT summary, kind FROM runs"))
        check("legacy sync row backfilled to kind=sync", kinds["sync: ancient"], "sync")
        check("legacy detection row stays layer1", kinds["new=7 stale=0"], "layer1")
        check("clock reads the legacy detection row",
              db.last_run(migrated)[1], "new=7 stale=0")
        migrated.close()


def test_the_dashboard_reads_exactly_what_sync_writes():
    """The seam between the two halves of Layer 2, in both directions.

    sync.py is the only writer of the DB and of `tailored/`; dashboard.py is the
    only reader that Vaish ever sees. Three strings cross that seam and none of
    them is a schema: the `SCORES:` line in review.md, and the `scores=` and
    `word_pages=` clauses sync folds into `notes` so the page can show them
    without a migration. A regex that drifted on one side would leave the card
    showing numbers the sidecar contradicts, or - worse - silently showing
    nothing while the fold kept working. Both modules are asserted against the
    SAME literals, which is the only way this test can fail on drift rather than
    on a copy of the drift.
    """
    import dashboard
    import db
    import sync
    print("sync writes what the dashboard reads")

    line = "SCORES: ats 88 / recruiter 9 / hm 8"
    check("sync reads the SCORES line", sync.SCORES_LINE.findall(line),
          [("88", "9", "8")])
    check("and the dashboard reads the same one",
          dashboard.SCORES_LINE.findall(line), [("88", "9", "8")])

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        (td / "tailored").mkdir()
        rel = "applications/acme-pm-2026-09-16"
        folder = td / rel
        folder.mkdir(parents=True)
        conn = db.connect(td / "t.sqlite")
        jid = db.insert_job(conn, "Acme", "Product Manager", "Boston, MA",
                            "http://x", "greenhouse")
        db.set_score(conn, jid, 9, "auto-tailor; strong fit")
        (folder / "brief.md").write_text(f"- **id:** `{jid}`" + chr(10),
                                         encoding="utf-8")
        (folder / "review.md").write_text(
            "## hiring-manager" + chr(10) + line + chr(10), encoding="utf-8")
        (td / "tailored" / f"{jid}.json").write_text(json.dumps({
            "id": jid, "folder": rel, "status_intent": "tailored",
            "scores": {"ats": 88, "recruiter": 9, "hm": 8},
            "resume": "Resume.docx", "pages_soffice": 1}), encoding="utf-8")

        old = (sync.ROOT, sync.TAILORED_DIR, dashboard.ROOT)
        sync.ROOT, sync.TAILORED_DIR, dashboard.ROOT = td, td / "tailored", td
        real_check = sync.word_page_check
        sync.word_page_check = lambda rec, apply=True: (2, "")
        try:
            # The fold, with Word disagreeing about the page count.
            res = sync.ingest_tailors(conn, apply=True)
            check("the row advanced on the Routine's own intent",
                  conn.execute("SELECT status FROM jobs WHERE id=?",
                               (jid,)).fetchone()[0], "tailored")
            check("the page disagreement was reported, not acted on",
                  [(j, p) for j, p, _ in res["word_check"]], [(jid, 2)])

            notes = conn.execute("SELECT notes FROM jobs WHERE id=?",
                                 (jid,)).fetchone()[0]
            check("the dashboard reads the scores clause sync wrote",
                  dashboard.notes_scores(notes),
                  {"ats": 88, "recruiter": 9, "hm": 8})
            check("and the word_pages clause",
                  dashboard.word_pages(notes), 2)
            check("the lane is still the first clause, for both readers",
                  (sync.lane_of(notes), dashboard.lane_of(notes)),
                  ("auto-tailor", "auto-tailor"))

            # review.md is the other half: the dashboard prefers it, and sync
            # repairs a stale sidecar from it. Same file, same three numbers.
            check("the dashboard reads the review file directly",
                  dashboard.review_scores(folder),
                  {"ats": 88, "recruiter": 9, "hm": 8})
            check("and sync sees nothing left to repair",
                  sync.reconcile_tailor_sidecars(apply=True), [])

            # The folder marker: both modules must find a folder by the same
            # two names, or one of them loses the folder entirely.
            check("both halves map the folder from brief.md",
                  (sync.application_folders().get(jid),
                   dashboard.application_folders().get(jid)), (folder, folder))
            (folder / "brief.md").unlink()
            (folder / "score.md").write_text(f"- **id:** `{jid}`" + chr(10),
                                             encoding="utf-8")
            check("and both still map it from a pre-rebuild score.md",
                  (sync.application_folders().get(jid),
                   dashboard.application_folders().get(jid)), (folder, folder))
        finally:
            sync.word_page_check = real_check
            sync.ROOT, sync.TAILORED_DIR, dashboard.ROOT = old
            conn.close()


def test_a_closed_row_still_appears_somewhere_on_the_page():
    """A fresh `rejected`/`skipped` row stands in no column, so it needs a list.

    The seam this closes: those two statuses reach the board's `shown` set and
    then match none of BOARD_COLUMNS, so from the moment sync folds a mark the
    card was simply gone -- the only trace was a whole-database tally that
    counts 4,772 off-target rows in the same number. A mark that produces no
    visible confirmation is indistinguishable from a mark that never landed,
    which is this page's oldest failure mode wearing new clothes.

    The freshness half of the expectation is DERIVED, through the same two
    helpers the page uses (`db.job_age`, `dashboard.age_tier`), so the age
    arithmetic is never restated here. The rest is a restatement and should be
    read as one: the status pair and the `score IS NOT NULL` gate are spelled
    out as a SQL literal, so this test would keep passing if the row loader's
    own WHERE clause changed which closed rows reach the page at all.
    """
    import dashboard
    import db
    print("recently closed rows are visible")
    page = _render_dashboard()
    check("the section header is unconditional", "Recently closed" in page, True)
    conn = db.connect()
    fresh = 0
    for jid, posted, first_seen in conn.execute(
            "SELECT id, posted_at, first_seen FROM jobs WHERE"
            " status IN ('rejected', 'skipped') AND score IS NOT NULL"):
        best, _worst, precision = db.job_age(posted, first_seen)
        if dashboard.age_tier(best, precision) != "stale":
            fresh += 1
    listed = len(re.findall(r'<li class="gone closed"', page))
    check("every fresh closed row is listed, up to the cap",
          listed, min(fresh, dashboard.CLOSED_CAP))
    if fresh > dashboard.CLOSED_CAP:
        check("and the remainder is stated rather than dropped",
              f"and {fresh - dashboard.CLOSED_CAP} more" in page, True)
    elif fresh:
        check("a listed row carries its status word",
              bool(re.search(r'<li class="gone closed"[^>]*>.*?'
                             r'<b>(skipped|rejected)</b>', page)), True)
    else:
        check("an empty list says so rather than disappearing",
              "Nothing is waiting here" in page, True)


def test_assets_load_from_a_relocated_root():
    """The stylesheets are found next to dashboard.py, never next to the repo.

    `ASSETS` is HERE-relative and `ROOT` is HERE.parent, which look alike until
    something moves. The published build runs from a checkout whose ROOT the CI
    job overlays; a ROOT-relative asset path would fail there and nowhere else.
    Rendered in-process with ROOT pointed at an empty directory: every
    ROOT-derived read (applications, scores, attestations) then returns nothing,
    and the page must still come out fully styled.
    """
    import dashboard
    print("assets are HERE-relative")
    td = Path(tempfile.mkdtemp(prefix="relocated_root_"))
    old_root, old_argv = dashboard.ROOT, sys.argv
    try:
        out = td / "index.html"
        dashboard.ROOT = td
        sys.argv = ["dashboard.py", "--links", "local", "--out", str(out)]
        rc = dashboard.main()
        check("dashboard.main() returned 0", rc, 0)
        page = out.read_text(encoding="utf-8")
        check("the token system is inlined", "--ats-low:" in page, True)
        check("the board stylesheet is inlined", ".atsm" in page, True)
        check("the client is inlined", "jsosTray" in page, True)
        # No folders under the temp root means no card has anything to
        # measure, so no badge of any state is drawn. Asserted on the CLASS,
        # not on the sentence: the explainer quotes "Not measured yet" while
        # defining it, and matching that would pass on a page with no badges
        # for the wrong reason.
        check("no folder under the temp root means no badge at all",
              'class="ats' in page, False)
        # Every badge carries aria-describedby="ats-help". The target lives in
        # the controls panel via one `{ATS_HELP}` in an f-string, and dropping
        # it leaves every badge pointing at nothing while a test on the
        # CONSTANT still passes -- so this asserts the rendered page, and it
        # holds on this root precisely because the explainer ships whether or
        # not any badge does.
        check("the explainer reaches the page", 'id="ats-help"' in page, True)
        # The archive aside renders unconditionally, and so does its
        # recently-closed section: on this root there are no rows at all, which
        # is exactly the state where a section that appears only when it has
        # content teaches a reader that its absence means "nothing happened".
        check("the recently-closed section renders with no rows to show",
              "Recently closed" in page, True)
    finally:
        dashboard.ROOT, sys.argv = old_root, old_argv
        shutil.rmtree(td, ignore_errors=True)


def test_the_rendered_page_says_nothing_the_publish_guard_greps_for():
    """The badge and its explainer are new PROSE on a page CI greps.

    `test_dashboard.py` runs this check against the github build; this runs it
    against the local one, which is the build every other test here renders and
    the one a new hand-written sentence lands in first. The pattern is imported
    from that module, which reads it out of the workflow -- restating it here
    would let a test pass while CI fails, which is worth less than no test.
    """
    import test_dashboard
    print("no private-path prose on the page")
    guard = test_dashboard.ci_guard_pattern()
    check("the rendered page matches nothing CI greps for",
          guard.findall(_render_dashboard()), [])


def test_pass1_draft_is_never_offered_as_a_deliverable():
    """`resume.pass1.docx` must be invisible to every reader of the folder.

    It is the JD-blind, unconverged, never-critiqued draft sitting beside the
    shipped resume. Two readers would otherwise mistake it for one: the
    dashboard labels file links by SUFFIX, so it renders as a second
    indistinguishable "docx" one click from an employer upload; and
    sync.resume_docx falls back to "the only .docx here", which counting it
    breaks forever -- turning a repairable name drift into a permanent silent
    downgrade to needs_review.
    """
    import compare_renderers
    import dashboard
    import docx_engine
    import sync
    print("pass-1 draft is not a deliverable")
    td = Path(tempfile.mkdtemp(prefix="pass1_"))
    try:
        rel = "applications/testco-pm-2026-08-22"
        folder = td / rel
        folder.mkdir(parents=True)
        shipped = folder / "Vaishnav_Ajai_Resume_TestCo.docx"
        shipped.write_bytes(b"shipped")
        (folder / "Vaishnav_Ajai_Resume_TestCo.pdf").write_bytes(b"shipped pdf")
        pass1 = folder / f"resume{docx_engine.PASS1_SUFFIX}"
        pass1.write_bytes(b"pass1 draft")

        check("the dashboard offers only the shipped pair",
              [p.name for p in dashboard.deliverable_files(folder)],
              ["Vaishnav_Ajai_Resume_TestCo.pdf",
               "Vaishnav_Ajai_Resume_TestCo.docx"])
        check("the pass-1 draft is on disk, so this is a filter and not an absence",
              pass1.is_file(), True)

        old_sync_root, old_cr_root = sync.ROOT, compare_renderers.ROOT
        sync.ROOT, compare_renderers.ROOT = td, td
        try:
            rec = {"folder": rel, "resume": "Name_That_Drifted.docx"}
            check("resume_docx still resolves by elimination beside a pass1 file",
                  sync.resume_docx(rec), shipped)
            check("and by name when the name is right",
                  sync.resume_docx(dict(rec, resume=shipped.name)), shipped)
            check("the renderer parity audit does not count the pass-1 draft",
                  [p.name for p in compare_renderers.default_targets()],
                  [shipped.name])
        finally:
            sync.ROOT, compare_renderers.ROOT = old_sync_root, old_cr_root
    finally:
        shutil.rmtree(td, ignore_errors=True)


def main():
    test_a_closed_row_still_appears_somewhere_on_the_page()
    test_assets_load_from_a_relocated_root()
    test_the_rendered_page_says_nothing_the_publish_guard_greps_for()
    test_pass1_draft_is_never_offered_as_a_deliverable()
    test_layer1_clock_is_not_reset_by_sync()
    test_queue_reconcile()
    test_jd_text_shed()
    test_vercel_guard_refuses_public_configs()
    test_partial_and_sidecar_drift()
    test_the_dashboard_reads_exactly_what_sync_writes()
    test_dry_run_leaves_pdf_alone()
    test_selective_application_checkout()
    test_overlaid_folder_links_do_not_404()
    test_push_race_never_wedges()
    test_push_retry_refuses_to_destroy_handmade_commits()
    test_stranded_sync_commit_never_wedges_the_pull()
    test_stranded_sync_commit_beside_a_handmade_one_is_parked_not_reset()
    test_handmade_commits_alone_are_left_to_the_pull()
    test_baseline_none_fails_closed()
    test_rewrite_path_set_covers_what_a_rebase_replays()
    test_lock_gate_defers_before_mutating()
    test_lock_outside_the_rewrite_set_does_not_block()
    test_orphaned_autostash_recovery_is_reversible()
    test_defer_records_state_and_never_touches_the_db()
    test_repeat_trigger_short_circuits_only_after_success()

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILURE(S): {', '.join(FAILS)}")
        return 1
    print("all storage-consistency tests pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
