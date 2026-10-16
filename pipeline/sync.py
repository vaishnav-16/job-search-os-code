"""Local Layer 2 sync - the scheduled local writer of pipeline/jobs.sqlite (`/tailor` and
`/mark`, run by hand, are the other two; the cloud never writes it).

The cloud does the judgment: one Routine (`judge-queue`) scores the queue,
tailors the resumes it selects, and publishes `scores/`, `tailored/` and
`applications/<folder>/` on ONE branch, `claude/judge`. This script is the other
half: it folds that work into the DB, cross-checks every cloud-tailored resume's
page count with Word, sweeps stale rows, rebuilds the dashboard, and pushes.

Deliberately contains NO LLM call. It runs from Windows Task Scheduler, so it
must be dull and idempotent: run it twice and the second run is a no-op. That is
also why it is not a Claude session - a scheduled session would spend
subscription quota re-deciding things the Routine already decided.

Word CHECKS, it does not WRITE, and since the v3 rebuild it does not GATE
either. Word's pagination is what a recruiter sees and LibreOffice justifies
marginally tighter, so a disagreement is worth saying out loud - but the cloud
already reviewed the resume, so a second page is a flag on the row
(`word_pages=` in `notes`), not a status downgrade. The shipped PDF stays the
soffice render the tailor produced, because Word's PDF text layer sprays spaces
inside compound keywords ("real -time") and that text layer is what an ATS
parses.

Two-writer rule: jobs.sqlite is a committed binary with exactly two writers, the
Layer 1 cron and this local side. Step 1 is always `git pull`, before the first
DB write, not merely before the push (LEARNINGS 2026-08-15).

Ownership, which is what the reconcile passes encode: the DB owns status, the
judge branch owns whatever the Routine just produced, and main owns everything
that has already been folded in. Step 2 takes sidecar sets wholesale because
steps 3.5 and 4.5 immediately re-check them against the DB - but it takes only
NEW application folders, because a checkout is not a merge and nothing
re-derives a resume folder (see fetch_new_application_folders).

Step 8 then points the judge branch back at main. It is append-only otherwise,
so it drifts without limit - measured 2026-08-18 on its two predecessors, 346
sidecars against 83 live ones. The reset is safe because everything the Routine
writes has reached main by then, copied rather than merged.

Usage:
    python pipeline/sync.py                    # full run: pull, ingest, verify, push
    python pipeline/sync.py --dry-run          # report only: no writes anywhere
    python pipeline/sync.py --no-push          # write locally, skip the push
    python pipeline/sync.py --no-branch-reset  # push, but leave the cloud branch
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

PIPELINE_DIR = Path(__file__).resolve().parent
ROOT = PIPELINE_DIR.parent
sys.path.insert(0, str(PIPELINE_DIR))

import db  # noqa: E402
import decisions  # noqa: E402
import docx_engine  # noqa: E402
import freshness  # noqa: E402
import ingest_scores  # noqa: E402
import winlock  # noqa: E402

TAILORED_DIR = ROOT / "tailored"
QUEUE_DIR = ROOT / "queue"
# One cloud branch since the v3 rebuild: the judge-queue Routine writes scores/,
# tailored/ and applications/ on it. It replaced claude/scores + claude/tailors,
# which could disagree with each other about the same job.
JUDGE_BRANCH = "claude/judge"
# The cloud gives each routine run a designated branch named after the outcome
# branch plus a run suffix (`claude/judge-j99h5u` on the first run, 2026-09-18),
# and the run can push nowhere else. So the family `claude/judge*` is one
# branch as far as this script is concerned: every member is folded, then the
# suffixed ones are deleted and the bare one is pointed back at main.
JUDGE_BRANCH_PREFIX = JUDGE_BRANCH
ROUTINE_NOTES = ".claude/routines/judge-queue.notes.md"
PUSH_ATTEMPTS = 3


def judge_branches() -> list[str]:
    """Every `claude/judge*` branch on origin, oldest commit first.

    Oldest first so that a later run's sidecar wins the wholesale checkouts
    when two runs scored the same job. A branch we cannot fetch or date sorts
    last, so one bad ref never hides the others.
    """
    proc = run(["git", "ls-remote", "--heads", "origin", f"{JUDGE_BRANCH_PREFIX}*"],
               check=False, quiet=True)
    if proc.returncode != 0:
        return []
    names = []
    for line in (proc.stdout or "").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].startswith("refs/heads/"):
            names.append(parts[1][len("refs/heads/"):])
    dated = []
    for name in names:
        run(["git", "fetch", "origin", name], check=False, quiet=True)
        p = run(["git", "log", "-1", "--format=%ct", f"origin/{name}"],
                check=False, quiet=True)
        try:
            when = int((p.stdout or "").strip())
        except ValueError:
            when = 1 << 62
        dated.append((when, name))
    return [name for _, name in sorted(dated)]

# Lock gate. Short on purpose: a viewer lock lasts as long as Vaish is reading,
# so this loop is not what outlasts it -- the scheduler's restart ladder is
# (RestartCount=2 x 15min). This only absorbs a transient toucher.
LOCK_WAIT_ATTEMPTS = 5
LOCK_WAIT_DELAY_S = 20

# Run outcomes live in a GITIGNORED sidecar, never in jobs.sqlite. Recording a
# failure in the DB would dirty a committed binary that origin/main also changes
# hourly, so the next run's --autostash would stash a modified binary and
# conflict on reapply -- which is the second half of the very failure this gate
# exists to prevent. A PDF locked by Acrobat is also a fact about this laptop,
# not about the repo, so it has no business in shared state.
STATE_PATH = PIPELINE_DIR / "sync_state.json"
# Set by --ci: the fold runs in GitHub Actions (fold.yml) on every routine push,
# so finished resumes reach the DB while the laptop is shut down. A runner has no
# Word and does not publish the dashboard (dashboard.yml does).
CI = False

# Each scheduled slot repeats every 15 min for an hour, because that repetition
# is the only retry Windows actually gives us: -RestartCount does NOT fire on a
# nonzero action exit code (measured 2026-08-19 with a probe task that exited 2,
# recorded LastTaskResult=2, and was never restarted -- "the task failed" means
# it failed to LAUNCH). Repeats must therefore be cheap: a run whose predecessor
# in this window already succeeded exits immediately instead of re-pulling,
# re-folding and re-pushing four times a slot for nothing. A deferred or failed
# predecessor is exactly what we DO want retried, so it never short-circuits.
RETRY_WINDOW_MIN = 70

EXIT_OK, EXIT_FAIL, EXIT_DEFERRED = 0, 1, 2


class LockedArtifacts(Exception):
    """Files git must rewrite are held by another process. Nothing was mutated."""

    def __init__(self, locked: list[str]):
        self.locked = locked
        super().__init__(", ".join(locked))


def run(cmd: list[str], check: bool = True, quiet: bool = False) -> subprocess.CompletedProcess:
    # encoding is explicit: bare text=True decodes with the Windows locale
    # codepage (cp1252), which double-encoded an em-dash out of `git show` into
    # a markdown file on 2026-08-17. Every file this repo writes is UTF-8, so
    # every subprocess reading one must be told so.
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if not quiet and (proc.stdout or "").strip():
        for line in proc.stdout.strip().splitlines():
            print(f"    | {line}")
    if proc.returncode != 0 and (proc.stderr or "").strip():
        # Failures used to be entirely silent unless check=True: six git calls
        # here pass check=False, so a broken fetch/checkout/push printed nothing
        # at all and the log showed a clean run that had quietly done nothing.
        for line in proc.stderr.strip().splitlines()[:6]:
            print(f"    ! {line}")
    if check and proc.returncode != 0:
        err = ((proc.stderr or "") + (proc.stdout or "")).strip()[:400]
        raise RuntimeError(f"{' '.join(cmd)} failed (rc={proc.returncode}): {err}")
    return proc


def step(n: str, title: str) -> None:
    print(f"\n[{n}] {title}")


# ---------------------------------------------------------------- git plumbing

def dirty_paths() -> list[str]:
    proc = run(["git", "status", "--porcelain"], quiet=True)
    return [l for l in (proc.stdout or "").splitlines() if l.strip()]


def _lines(proc) -> list[str]:
    return [l.strip() for l in (proc.stdout or "").splitlines() if l.strip()]


def local_commit_paths() -> list[str]:
    """Paths a rebase would REPLAY, which is not the same as the tree diff.

    `git diff --name-only origin/main..HEAD` is NOT this: for git diff, `A..B`
    means the two endpoints `A B`, so it returns the same set as
    `git diff HEAD origin/main` and adds nothing. The difference is real -- a
    local commit that adds a file and a later one that deletes it leaves no
    trace in the tree diff, yet the rebase still writes that path on the way
    through, and that write is what a lock blocks (proved 2026-08-19).
    """
    return _lines(run(["git", "log", "--name-only", "--pretty=format:",
                       "origin/main..HEAD"], check=False, quiet=True))


def orphaned_autostash() -> str | None:
    """stash@{0} if git left an autostash behind, else None.

    Only entry 0, and only git's own subject: a hand-made stash of Vaish's is
    not ours to touch.
    """
    for line in _lines(run(["git", "stash", "list", "--format=%gd:%gs"],
                           check=False, quiet=True)):
        ref, _, subject = line.partition(":")
        if ref.strip() != "stash@{0}":
            break
        subject = subject.strip()
        if subject == "autostash" or subject.endswith(": autostash"):
            return ref.strip()
        break
    return None


def stash_paths(ref: str | None) -> list[str]:
    if not ref:
        return []
    return _lines(run(["git", "stash", "show", "--name-only", ref],
                      check=False, quiet=True))


def paths_git_may_rewrite() -> list[str]:
    """Every tracked path the next pull/rebase could write. Existing files only.

    Union of four sources, each covering a case the others miss:
      - `git diff --name-only HEAD origin/main` : what the pull brings in
      - `git diff --name-only HEAD`             : locally modified tracked files
        (autostash reverts these; untracked files are correctly excluded
        because --autostash never stashes them, and this form sidesteps
        --porcelain's rename arrows and quoted paths)
      - local_commit_paths()                    : what a rebase replays
      - stash_paths(orphaned_autostash())       : what a recovery would restore
    """
    paths = set(_lines(run(["git", "diff", "--name-only", "HEAD", "origin/main"],
                           check=False, quiet=True)))
    paths |= set(_lines(run(["git", "diff", "--name-only", "HEAD"],
                            check=False, quiet=True)))
    paths |= set(local_commit_paths())
    paths |= set(stash_paths(orphaned_autostash()))
    return sorted(p for p in paths if (ROOT / p).is_file())


def read_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def already_succeeded_recently(window_min: int = RETRY_WINDOW_MIN) -> str:
    """Reason to skip this run, or "" to proceed.

    Only a SUCCESSFUL predecessor short-circuits. A deferred or failed one is
    precisely what the repeating trigger exists to retry.
    """
    state = read_state()
    if state.get("status") != "ok":
        return ""
    try:
        last = datetime.datetime.strptime(state.get("last_ok", ""),
                                          "%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        return ""
    # utcnow() is deprecated in 3.12+; now_iso() writes naive-UTC strings, so
    # compare against an explicitly-UTC clock stripped back to naive.
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    age_min = (now - last).total_seconds() / 60
    if 0 <= age_min < window_min:
        return f"a run succeeded {age_min:.0f} min ago (repeat trigger); nothing to redo"
    return ""


def record_outcome(status: str, detail: str = "", paths: list[str] | None = None) -> None:
    """Record how this run ended. status in {'ok','deferred','failed'}.

    Never raises. An outcome recorder that can throw would mask the very failure
    it is describing.
    """
    try:
        prev = read_state()
        state = {
            "status": status,
            "at": db.now_iso(),
            "detail": detail[:400],
            "paths": (paths or [])[:20],
            "consecutive": 0 if status == "ok" else int(prev.get("consecutive", 0)) + 1,
            "last_ok": db.now_iso() if status == "ok" else prev.get("last_ok", ""),
        }
        tmp = STATE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(tmp, STATE_PATH)
    except Exception as exc:
        print(f"    (could not record run outcome: {str(exc)[:100]})")


def lock_gate() -> list[str]:
    """Which files git must rewrite are held by another process. [] = clear.

    Runs BEFORE any mutation. The fetch is read-only, so reaching this verdict
    costs nothing even when the answer is "blocked".

    A failed fetch skips the gate rather than deferring: no network means the
    pull mutates nothing anyway, and a transient blip must not stall the
    pipeline.
    """
    proc = run(["git", "fetch", "origin", "main"], check=False, quiet=True)
    if proc.returncode != 0:
        print("    fetch failed; skipping the lock gate (a pull without network "
              "mutates nothing)")
        return []
    candidates = paths_git_may_rewrite()
    if not candidates:
        print("    nothing to rewrite; no lock risk")
        return []
    locked = winlock.wait_for_unlock(candidates, ROOT, LOCK_WAIT_ATTEMPTS,
                                     LOCK_WAIT_DELAY_S)
    print(f"    {len(candidates)} path(s) git may rewrite; "
          f"{len(locked) or 'none'} locked")
    for p in locked:
        print(f"      LOCKED  {p}")
    return locked


def rescue_push(rev: str, label: str) -> str | None:
    """Park `rev` on a remote rescue branch. Returns the ref, or None.

    A ref push needs no working-tree checkout, so a file lock cannot block it.
    That is what makes unpushed work durable even when this laptop cannot
    complete another sync for days (the manual move that saved the 2026-08-19
    incident, now automatic).
    """
    ref = f"refs/heads/rescue/{label}-{db.now_iso()[:19].replace(':', '').replace('-', '')}"
    proc = run(["git", "push", "origin", f"{rev}:{ref}"], check=False, quiet=True)
    if proc.returncode == 0:
        print(f"    parked unpushed work at {ref}")
        return ref
    return None


SYNC_SUBJECT = re.compile(r"^sync: \d+ file\(s\) @ ")


def drop_stranded_sync_commits() -> list[str]:
    """Discard this script's own commits that a failed push left on main.

    A push that dies outright (no network, so the reset-and-redo in
    push_with_retry cannot even fetch) leaves the sync commit local. The next
    pull is `--rebase`, which replays that commit's jobs.sqlite onto whatever
    the cron committed since: a binary conflict, re-created identically on
    every run however often abort_stray_rebase clears it (2026-10-02, DNS
    failure; sync was down until 10-04). Dropping is lossless for the same
    reason the push-race reset is: the fold is recomputed from the cloud
    branches, the queue and the remote DB on every run.

    `reset --keep`, not `--hard`: uncommitted edits survive, and git refuses
    rather than clobbers if one overlaps the reset. Hand-made commits are never
    dropped; mixed with a stranded sync commit (which no rebase can replay),
    everything is parked on a rescue ref and the run stops loudly.
    """
    if run(["git", "fetch", "origin", "main"], check=False, quiet=True).returncode != 0:
        return []  # no network: the pull fails too, and mutates nothing
    rows = _lines(run(["git", "log", "--format=%h %s", "origin/main..HEAD"],
                      check=False, quiet=True))
    ours = [r for r in rows if SYNC_SUBJECT.match(r.partition(" ")[2])]
    if not ours:
        return []
    if len(ours) != len(rows):
        ref = rescue_push("HEAD", "stranded")
        raise RuntimeError(
            f"{len(ours)} unpushed sync commit(s) sit beside {len(rows) - len(ours)} "
            f"hand-made one(s) ({'; '.join(r for r in rows if r not in ours)[:200]}); "
            "a rebase cannot replay the sync commit's jobs.sqlite, and resetting "
            "would destroy the hand-made work. Push the hand-made commits on their "
            "own, then rerun." + (f" Parked at {ref}." if ref else ""))
    print(f"    dropping {len(ours)} sync commit(s) a failed push stranded "
          "(the fold redoes them)")
    for r in ours:
        print(f"      {r}")
    run(["git", "reset", "--keep", "origin/main"])
    return ours


def git_pull() -> None:
    """Pull with --autostash, because a scheduled task meets a dirty tree.

    Plain `git pull --rebase` aborts on ANY unstaged tracked change, so an edit
    Vaish left open on a Sunday afternoon silently stopped every sync until he
    noticed (observed 2026-08-17: exit 1, visible only in this log and in the
    task's LastTaskResult). --autostash stashes, rebases, and reapplies, so
    in-progress work survives and the sync still runs.
    """
    dirty = dirty_paths()
    if dirty:
        print(f"    working tree has {len(dirty)} change(s); pulling with --autostash")
        for line in dirty[:5]:
            print(f"      {line}")
        if len(dirty) > 5:
            print(f"      ... and {len(dirty) - 5} more")
    run(["git", "pull", "--rebase", "--autostash"])


def branch_files(branch: str, path: str) -> list[str]:
    """Paths under `path` in a branch's tree. Read-only: touches no file."""
    proc = run(["git", "ls-tree", "-r", "--name-only", f"origin/{branch}", path],
               check=False, quiet=True)
    if proc.returncode != 0:
        return []
    return [l.strip() for l in (proc.stdout or "").splitlines() if l.strip()]


def fetch_branch_paths(branch: str, paths: list[str], apply: bool = True) -> bool:
    """Bring a cloud branch's output into the working tree. False if absent.

    A branch that does not exist yet is normal (no Routine run has pushed), not
    an error worth stopping on.

    Wholesale checkout is correct for `scores/` and `tailored/` and ONLY for
    those: both are id-keyed sidecar sets that the reconcile passes (3.5, 4.5)
    immediately re-check against the DB, so overwriting them costs nothing and
    is what makes step 8's branch reset provably lossless. `applications/` has
    no such reconcile and is handled by fetch_new_application_folders instead.

    `git checkout <tree-ish> -- <path>` writes the working tree AND stages the
    result, so this is a mutation like any other and a dry run must not do it.
    Under --dry-run it reports what the branch holds and takes nothing.
    """
    fetch = run(["git", "fetch", "origin", branch], check=False, quiet=True)
    if fetch.returncode != 0:
        print(f"    branch {branch} not on origin yet - skipping")
        return False
    if not apply:
        for p in paths:
            have = {Path(f).name for f in branch_files(branch, p)}
            local = {q.name for q in (ROOT / p).glob("*")} if (ROOT / p).is_dir() else set()
            print(f"    would take {len(have)} file(s) from {branch}:{p}"
                  f" ({len(have - local)} not present locally)")
        return True
    checkout = run(["git", "checkout", f"origin/{branch}", "--", *paths],
                   check=False, quiet=True)
    if checkout.returncode == 0:
        return True
    if len(paths) > 1:
        # One pathspec that matches nothing fails the WHOLE checkout, and on one
        # branch that is a live case rather than a corner: a Routine run that
        # scored the queue and tailored nothing pushes `scores/` with no
        # `tailored/`, and taking neither would silently drop every new score.
        # So retry per path and take what exists.
        took = [p for p in paths
                if run(["git", "checkout", f"origin/{branch}", "--", p],
                       check=False, quiet=True).returncode == 0]
        if took:
            print(f"    took {', '.join(took)} from {branch}"
                  f" ({len(paths) - len(took)} path(s) not on the branch yet)")
            return True
    # An empty branch, or one that never created these paths.
    print(f"    nothing to take from {branch}")
    return False


def sidecar_times() -> dict[str, tuple[str, bytes]]:
    """{file name: (scored_at, raw bytes)} for every score sidecar in the tree."""
    out = {}
    for p in sorted((ROOT / "scores").glob("*.json")):
        raw = p.read_bytes()
        try:
            out[p.name] = (json.loads(raw).get("scored_at") or "", raw)
        except (ValueError, AttributeError):
            continue
    return out


def branch_changed(branch: str, path: str) -> set[str] | None:
    """File names under `path` the branch added or changed since it forked from
    HEAD (main). None when there is no merge base (a shallow clone)."""
    base = run(["git", "merge-base", "HEAD", f"origin/{branch}"], check=False, quiet=True)
    if base.returncode != 0 or not (base.stdout or "").strip():
        return None
    diff = run(["git", "diff", "--name-only", "--diff-filter=AMR", base.stdout.strip(),
                f"origin/{branch}", "--", path], check=False, quiet=True)
    if diff.returncode != 0:
        return None
    return {Path(l.strip()).name for l in (diff.stdout or "").splitlines() if l.strip()}


def keep_newer_sidecars(before: dict[str, tuple[str, bytes]],
                        changed: set[str] | None = None) -> list[str]:
    """Undo a branch checkout that replaced a score sidecar with a STALE copy.

    A routine branch carries every sidecar it overlaid at the start of its run.
    A re-score that lands on main while the run is going is newer than the
    branch's copy, and the wholesale checkout would put the old lane back. Now
    that a changed lane folds into the DB (ingest_scores.is_relane), that stale
    copy would be taken as a new call. 10-11: the w4-probe branch, cloned before
    a re-publish, carried the reverted copies of 12 re-scored sidecars with the
    SAME scored_at, and its fold re-laned them back. So the rule is the branch's
    own diff: a sidecar the branch did not add or change since it forked from
    main (`changed`, from branch_changed) keeps main's copy. An older scored_at
    is the second guard, for a clone with no merge base.
    """
    restored = []
    for name, (was_at, raw) in before.items():
        p = ROOT / "scores" / name
        if not p.exists():
            continue
        now_raw = p.read_bytes()
        if now_raw == raw:
            continue
        try:
            now_at = json.loads(now_raw).get("scored_at") or ""
        except (ValueError, AttributeError):
            now_at = ""
        carried = changed is not None and name not in changed
        if carried or (was_at and now_at < was_at):
            p.write_bytes(raw)
            restored.append(name)
    return restored


def branch_application_folders(branch: str) -> list[str]:
    """Top-level `applications/<folder>` paths present in a branch's tree."""
    proc = run(["git", "ls-tree", "-r", "--name-only", f"origin/{branch}",
                "applications/"], check=False, quiet=True)
    if proc.returncode != 0:
        return []
    folders = set()
    for line in (proc.stdout or "").splitlines():
        parts = line.strip().split("/")
        if len(parts) >= 2 and parts[0] == "applications" and parts[1]:
            folders.add(f"applications/{parts[1]}")
    return sorted(folders)


def branch_finished_folders(branch: str) -> set[str]:
    """Folders on `branch` that a `tailored/<id>.json` on that branch vouches for.

    The routine's own contract: it writes the sidecar LAST, and never writes one
    for a job with no usable artifact ("a sidecar means an artifact exists;
    writing one for a job with no usable artifact makes that job invisible
    forever"). So a folder with no sidecar is an unfinished job -- either in
    flight right now, or abandoned by a suspended run that will resume it.
    """
    finished = set()
    proc = run(["git", "ls-tree", "-r", "--name-only", f"origin/{branch}",
                "tailored/"], check=False, quiet=True)
    if proc.returncode != 0:
        return finished
    for path in (proc.stdout or "").splitlines():
        path = path.strip()
        if not path.endswith(".json"):
            continue
        blob = run(["git", "show", f"origin/{branch}:{path}"],
                   check=False, quiet=True)
        if blob.returncode != 0:
            continue
        try:
            folder = json.loads(blob.stdout or "{}").get("folder", "")
        except (ValueError, TypeError):
            continue
        if folder:
            finished.add(folder.rstrip("/"))
    return finished


def fetch_new_application_folders(branch: str,
                                  apply: bool = True) -> tuple[list[str], list[str], list[str]]:
    """Check out ONLY application folders that do not exist locally.

    A branch checkout is not a merge. `git checkout origin/<branch> -- applications/`
    overwrites every file it finds, so a folder main has since moved on from is
    reverted to the cloud's older copy — and step 7 then commits that regression
    back to main. Caught 2026-08-18 with a live example queued to happen on the
    next run: a folder's ledger reclassified locally versus the branch's older
    copy, and a Word-rendered PDF versus the branch's soffice one.

    Taking only NEW folders loses nothing, because the cloud tailor only ever
    creates them: its work list excludes any job that already has a
    `tailored/<id>.json` or a folder. A folder that exists here is one the cloud
    will never write again, so main is authoritative for it.

    That argument holds for a FINISHED run and is false in the middle of one --
    which is the second guard (added 2026-08-24). A run commits and pushes after
    every round, so a folder appears on the branch long before its last round.
    Take it mid-run and it lands on main frozen at that round; every later sync
    then skips it (it exists locally now), the sidecar arrives separately via the
    wholesale tailored/ checkout, ingest_tailors stamps a status onto stale
    content, and the cloud never revisits it because its work list excludes any
    id that already has a folder. Nothing anywhere would say so.
    Caught before it fired: a catch-up sync was scheduled while a run was
    mid-job-3 with no sidecar for it.

    So a folder is taken only when a tailored/<id>.json on the SAME branch
    vouches for it. Deferring costs one sync cycle; taking early is permanent.
    """
    finished = branch_finished_folders(branch)
    taken, skipped, unfinished = [], [], []
    for rel in branch_application_folders(branch):
        if (ROOT / rel).exists():
            skipped.append(rel)
            continue
        if rel not in finished:
            # In flight, or a suspended run's in-progress job. Either way the
            # next sync will find it again, with its sidecar.
            unfinished.append(rel)
            continue
        if not apply:
            taken.append(rel)          # report only; the tree is not touched
            continue
        if run(["git", "checkout", f"origin/{branch}", "--", rel],
               check=False, quiet=True).returncode == 0:
            taken.append(rel)
    return taken, skipped, unfinished


def rebase_in_progress() -> bool:
    git_dir = ROOT / ".git"
    return (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists()


def abort_stray_rebase() -> bool:
    """Clear a rebase an earlier crashed run left in progress.

    A wedged rebase is silent and permanent: step 1's pull is check=True and
    dies on it, so every later scheduled run fails before doing any work and the
    only trace is a log nobody opens. Clearing it is safe because this script
    owns no local commits worth preserving — the fold is recomputed from the
    cloud branches and the queue on every run.
    """
    if not rebase_in_progress():
        return False
    print("    a rebase was left in progress by an earlier run; aborting it")
    # Fatal if the abort itself fails. It used to be check=False, so a failed
    # abort fell through into a pull that could only die, with nothing to
    # distinguish the two. The abort also rewrites the working tree, so it is
    # itself lock-exposed -- and it must run BEFORE the lock gate, because a
    # detached mid-rebase HEAD makes `git diff HEAD origin/main` meaningless.
    run(["git", "rebase", "--abort"])
    return True


def recover_orphaned_autostash() -> list[str]:
    """Restore work git stranded when an --autostash pull died mid-checkout.

    Runs AFTER a successful pull, so the tree is at the tip the stash was taken
    against. Preconditions: an autostash at stash@{0} and a clean tree -- if the
    tree is dirty, the rollback below would destroy that work, so we skip and
    say so.

    `apply` + verify + explicit `drop`, never `pop`: a conflicting pop keeps the
    entry AND leaves a half-applied tree with no rollback point, which is the
    state that made the 2026-08-19 incident read as "my edits vanished".
    """
    ref = orphaned_autostash()
    if not ref:
        return []
    if dirty_paths():
        print("    an orphaned autostash exists but the tree is dirty; leaving it "
              "alone (restoring it could clobber the dirty work). Recover by hand: "
              "git stash list")
        return []
    print(f"    recovering the autostash an earlier run stranded ({ref})")
    proc = run(["git", "stash", "apply", ref], check=False, quiet=True)
    conflicts = _lines(run(["git", "diff", "--name-only", "--diff-filter=U"],
                           check=False, quiet=True))
    if proc.returncode != 0 or conflicts:
        # Safe: the tree was clean, so HEAD is a real rollback point.
        run(["git", "reset", "--hard", "HEAD"], check=False, quiet=True)
        print(f"    the autostash does not apply cleanly ({len(conflicts)} "
              "conflicting path(s)); rolled back and LEFT the stash in place")
        for p in conflicts[:5]:
            print(f"      conflict  {p}")
        return []
    restored = _lines(run(["git", "stash", "show", "--name-only", ref],
                          check=False, quiet=True))
    run(["git", "stash", "drop", ref], check=False, quiet=True)
    print(f"    restored {len(restored)} path(s) from the stranded autostash")
    for p in restored[:5]:
        print(f"      restored  {p}")
    # Loud on purpose: anything under step 7's add-list is now uncommitted work
    # that THIS run will commit and push, and Vaish never staged it.
    print("      ^^ recovered work in queue/ scores/ tailored/ applications/ "
          "or jobs.sqlite will be committed by this run")
    return restored


def unpushed_foreign_commits(baseline: str | None) -> list[str]:
    """Commits on main at `baseline` that origin/main does not have.

    `baseline` is HEAD immediately after this run's pull, so anything in
    `origin/main..baseline` predates the run and is therefore NOT ours to
    destroy. `refold()` only redoes sync's own deterministic work, so a
    `reset --hard` would delete such a commit with no way back.

    baseline=None FAILS CLOSED (returns non-empty). main() always threads a real
    one, so None means a caller that never thought about it, and the safe answer
    for "I don't know whose commits these are" is "don't destroy them".
    """
    if baseline is None:
        return ["<no baseline supplied; refusing to reset>"]
    return _lines(run(["git", "rev-list", "--oneline", f"origin/main..{baseline}"],
                      check=False, quiet=True))


def push_with_retry(refold, baseline: str | None = None) -> None:
    """Reset-and-redo on rejection. NEVER rebase: jobs.sqlite is a binary.

    detect.yml learned this the expensive way (commit e843821, after run
    32065471397 lost 16 queue files): `git pull --rebase` onto a committed
    binary DB stops at a conflict it cannot resolve. The loop that used to live
    here did exactly that with both output streams suppressed, so a push race
    left the repo mid-rebase, silently, and wedged every subsequent run.

    Recovery is to take origin/main wholesale and redo the fold. That is cheap
    and exactly correct: every input still exists (sidecars on the cloud
    branches, queue files, the remote DB), and the fold is idempotent by
    construction. `refold` recomputes the fold and re-stages the commit.
    """
    for attempt in range(1, PUSH_ATTEMPTS + 1):
        proc = run(["git", "push"], check=False, quiet=True)
        if proc.returncode == 0:
            print(f"    pushed (attempt {attempt})")
            return
        tail = ((proc.stderr or "") + (proc.stdout or "")).strip().splitlines()
        print(f"    push attempt {attempt} rejected: {tail[-1][:120] if tail else '?'}")
        if attempt == PUSH_ATTEMPTS:
            break
        print("    taking origin/main wholesale and redoing the fold")
        if rebase_in_progress():
            run(["git", "rebase", "--abort"], check=False, quiet=True)
        run(["git", "fetch", "origin", "main"], check=False, quiet=True)

        # Both guards sit AFTER the fetch and BEFORE the reset, and that order is
        # load-bearing: before the fetch, origin/main is stale, so commits it
        # already has would read as foreign and every race would refuse.
        foreign = unpushed_foreign_commits(baseline)
        if foreign:
            ref = rescue_push(baseline or "HEAD", "pre-reset")
            raise RuntimeError(
                f"refusing to reset --hard over {len(foreign)} unpushed commit(s) "
                f"this run did not make: {'; '.join(foreign[:3])}. The reset would "
                f"destroy them and refold() cannot recreate work it never did."
                + (f" Parked at {ref}." if ref else
                   " Could NOT park them remotely -- do not reset by hand."))

        relocked = winlock.locked_among(
            _lines(run(["git", "diff", "--name-only", "HEAD", "origin/main"],
                       check=False, quiet=True)), ROOT)
        if relocked:
            raise LockedArtifacts(relocked)

        run(["git", "reset", "--hard", "origin/main"], check=False, quiet=True)
        refold()
    raise RuntimeError(f"git push failed after {PUSH_ATTEMPTS} attempts")


def reset_cloud_branches(leases: dict[str, str]) -> list[str]:
    """Point the cloud branch back at main once its output is folded in.

    The branch is an append-only worktree that nothing ever prunes, so it drifts
    without limit: measured 2026-08-18 on its predecessor, `claude/scores`
    carried 346 sidecars against 83 live ones, 170 of them for jobs long since
    stale or skipped. Every sync restored all of them and re-deleted the dead
    ones, CI overlaid all of them into the published dashboard, and the
    Routine's own prescribed boundary check (`git diff origin/main <branch>`)
    had degraded to 181 false-positive paths - a safety check that no longer
    signals anything.

    Resetting is lossless because everything the Routine writes has already
    reached main by this point, copied rather than merged: sidecars via the
    wholesale checkouts, folders via fetch_new_application_folders.

    --force-with-lease pins the push to the tip this run actually read, so a
    Routine that pushed mid-run is never overwritten; a failed lease just means
    the next sync does it instead.
    """
    reset = []
    for branch, tip in leases.items():
        if not tip:
            continue
        # One refspec per call: a failed lease on one branch cannot take another
        # down with it. The bare branch is pointed at main; a run-suffixed one
        # (`claude/judge-<run>`) is deleted, since the next run gets a fresh one
        # and a family that only grows would repeat the drift described above.
        target = f"main:refs/heads/{branch}" if branch == JUDGE_BRANCH else f":refs/heads/{branch}"
        proc = run(["git", "push", f"--force-with-lease=refs/heads/{branch}:{tip}",
                    "origin", target], check=False, quiet=True)
        if proc.returncode == 0:
            reset.append(branch)
        else:
            print(f"    {branch} not reset (lease failed - a Routine pushed"
                  " mid-run); next sync will do it")
    return reset


# ------------------------------------------------------- tailor sidecar ingest

# The three review scores the judge-queue Routine reports per resume, with the
# range each one is written on. They are REPORT data: nothing in the pipeline
# gates on them, so a shape problem here must cost the field and never the
# sidecar, which is the receipt for a completed cloud tailor.
SCORE_FIELDS = (("ats", 0, 100), ("recruiter", 0, 10), ("hm", 0, 10))

# `review.md`'s last line. The dashboard reads the same one; sync reads it to
# repair a sidecar whose scores went stale after a later review round.
SCORES_LINE = re.compile(
    r"^SCORES:\s*ats\s+(\d+)\s*/\s*recruiter\s+(\d+)\s*/\s*hm\s+(\d+)", re.M)

# The id line every application folder carries. `brief.md` is where the v3
# tailor writes it; `score.md` is where folders tailored before the rebuild
# carry it, and those folders still have to be findable.
ID_LINE = re.compile(r"\*\*id:\*\*\s*`?([0-9a-f]{40})`?(?![0-9a-f])")
MARKER_FILES = ("brief.md", "score.md")


def folder_marker(folder: Path) -> Path | None:
    """The file that makes `folder` a real application folder, or None.

    brief.md first, score.md second, because a folder written by an older run
    has only the latter and one written today has only the former.
    """
    for name in MARKER_FILES:
        if (folder / name).is_file():
            return folder / name
    return None


def application_folders() -> dict[str, Path]:
    """Map job id -> application folder, via the id line its marker carries."""
    out: dict[str, Path] = {}
    apps = ROOT / "applications"
    if not apps.is_dir():
        return out
    for folder in sorted(p for p in apps.iterdir() if p.is_dir()):
        marker = folder_marker(folder)
        if marker is None:
            continue
        m = ID_LINE.search(marker.read_text(encoding="utf-8", errors="replace"))
        if m:
            out[m.group(1)] = folder
    return out


def parse_scores_field(rec: dict) -> tuple[dict | None, str]:
    """Validate the sidecar's OPTIONAL `scores` block. Returns (value, note).

    Any problem drops the whole field and returns a note for the run summary;
    the sidecar itself stays valid. A missing or null member is legitimate: a
    run that could not reach one reviewer still produced a resume, and losing
    the other two numbers over that would be worse than reporting less.
    """
    if rec.get("scores") is None:
        return None, ""
    block = rec["scores"]
    if not isinstance(block, dict):
        return None, (f"scores is {type(block).__name__}, not an object:"
                      " field dropped, sidecar kept")
    out: dict = {}
    for key, low, high in SCORE_FIELDS:
        v = block.get(key)
        if v is None:
            out[key] = None
            continue
        # bool is an int subclass; True would silently become 1.
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return None, (f"scores.{key} is {v!r}, not a number or null:"
                          " field dropped, sidecar kept")
        if not (low <= int(v) <= high):
            return None, (f"scores.{key} is {v!r}, outside {low}-{high}:"
                          " field dropped, sidecar kept")
        out[key] = int(v)
    return out, ""


def format_scores(scores: dict | None) -> str:
    """`ats/recruiter/hm` for the row note, with `?` for anything missing."""
    if not scores:
        return ""
    parts = [scores.get(key) for key, _, _ in SCORE_FIELDS]
    if all(v is None for v in parts):
        return ""
    return "/".join("?" if v is None else str(v) for v in parts)


def load_tailor_sidecars() -> tuple[list[dict], list[tuple[Path, str]],
                                    list[tuple[Path, str]]]:
    """Validated tailor sidecars. Mirrors ingest_scores.load_sidecars().

    Returns (good, bad, notes). `bad` is fatal per sidecar; `notes` is the
    non-fatal channel - an optional field that could not be read, dropped from
    the record while the sidecar itself still counts.

    Three fields are required and they are the three the DB needs: the id, the
    folder holding the artifact, and the status to move the row to. Everything
    else is report data. Sidecars written before the v3 rebuild carry `verdict`,
    `converged` and an `ats` block; those are ignored, never a reason to reject
    a sidecar that still points at a real resume.
    """
    good: list[dict] = []
    bad: list[tuple[Path, str]] = []
    notes: list[tuple[Path, str]] = []
    if not TAILORED_DIR.exists():
        return good, bad, notes
    for path in sorted(TAILORED_DIR.glob("*.json")):
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            bad.append((path, f"unreadable: {exc}"))
            continue
        jid = rec.get("id")
        if jid != path.stem:
            bad.append((path, f"id {jid!r} does not match filename"))
            continue
        intent = rec.get("status_intent")
        if intent not in ("tailored", "needs_review"):
            bad.append((path, f"status_intent {intent!r} not tailored/needs_review"))
            continue
        folder = rec.get("folder") or ""
        if not folder or not (ROOT / folder).is_dir():
            bad.append((path, f"folder {folder!r} does not exist"))
            continue
        scores, why = parse_scores_field(rec)
        if why:
            notes.append((path, why))
        rec = dict(rec)
        rec["scores"] = scores   # normalized: a dict, or None for absent/dropped
        good.append(rec)
    return good, bad, notes


def set_note_clause(conn, jid: str, key: str, value) -> str:
    """Write `key=value` into a row's `notes`, replacing any existing clause.

    `notes` is a `;`-separated line whose first clause is the lane the scorer
    assigned (db.set_lane owns that one). Everything after it is free clauses,
    which is where a fact about the artifact belongs: the dashboard can read
    `word_pages=` and `scores=` from there with no schema change, and a re-run
    rewrites the clause in place instead of stacking a second copy.

    On a row with EMPTY notes the clause necessarily lands first, where
    `lane_of` looks for the lane. Harmless in both readers - dashboard.lane_of
    validates against LANES and reads "", and such a row has left the work set
    by the time reconcile_sidecars looks at lanes - but it is why nothing here
    may ever be given the shape of a lane.

    Returns the new notes string ("" if there is no such row).
    """
    row = conn.execute("SELECT notes FROM jobs WHERE id = ?", (jid,)).fetchone()
    if row is None:
        return ""
    clauses = [c.strip() for c in (row[0] or "").split(";") if c.strip()]
    clause = f"{key}={value}"
    for i, c in enumerate(clauses):
        if c.split("=", 1)[0].strip() == key:
            clauses[i] = clause
            break
    else:
        clauses.append(clause)
    notes = "; ".join(clauses)
    conn.execute("UPDATE jobs SET notes = ? WHERE id = ?", (notes, jid))
    conn.commit()
    return notes


def lane_of(note: str) -> str:
    return (note or "").split(";")[0].strip()


def reconcile_sidecars(conn, apply: bool = False) -> dict:
    """Make `scores/` agree with the DB about which jobs are still tailorable.

    Why this exists: step 2 restores sidecars with
    `git checkout origin/claude/judge -- scores/`, which is how new cloud scores
    arrive — and it blindly overwrites whatever the local side had decided since.
    The cloud tailor reads SIDECARS while everything local reads the DB, so a
    correction that lands only in the DB is invisible to the cloud.

    Observed 2026-08-17 before the first tailor run: 11 sidecars still said
    `auto-tailor` for jobs the local side had de-laned or skipped (two Senior PM
    roles, two clearance-gated postings, seven duplicates of one Google role).
    At the run cap (6 since 2026-08-24, held at 6 by Phase 8's measurement) the cloud would have tailored them.

    The DB wins, because it is where hard filters, de-duplication and Vaish's own
    calls are recorded:
      - the job left the work set (status past `scored`)  -> drop the sidecar
      - the lane disagrees                                -> rewrite from the DB
    """
    result = {"dropped": [], "relaned": []}
    rows = {r[0]: r for r in conn.execute(
        "SELECT id, company, title, status, score, notes FROM jobs")}

    for path in sorted((ROOT / "scores").glob("*.json")):
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        row = rows.get(rec.get("id"))
        if row is None:
            continue                     # not in this clone's DB; leave it alone
        _, company, title, status, score, notes = row

        if status not in ("new", "scored"):
            result["dropped"].append((rec["id"], company, title, status))
            if apply:
                ingest_scores.drop_sidecar(rec["id"])
            continue

        if notes and lane_of(notes) != lane_of(rec.get("note")):
            result["relaned"].append(
                (rec["id"], company, title, lane_of(rec.get("note")), lane_of(notes)))
            if apply:
                # Carry the original provenance through. Rewriting the lane used
                # to reset `model` to the "sonnet" default and `scored_at` to
                # now, so a correction silently reattributed the score to a model
                # and a time that had nothing to do with it.
                ingest_scores.write_sidecar(
                    rec["id"], score if score is not None else rec.get("score", 0),
                    notes, scorer=rec.get("scorer", "routine"),
                    model=rec.get("model", "sonnet"),
                    scored_at=rec.get("scored_at", ""))
    return result


def resume_docx(rec: dict) -> Path | None:
    """The shipped resume, by name, or by elimination if the name drifted.

    The fallback exists so a sidecar naming a file that was since renamed still
    gets its Word page check instead of reporting an unreadable artifact. It
    only works while exactly one candidate remains, so a banked draft such as
    `resume.pass1.docx` MUST be excluded: counting it would make the fallback
    return None forever, turning a repairable name drift into a permanently
    uncheckable resume.
    """
    folder = ROOT / rec["folder"]
    named = rec.get("resume")
    if named and (folder / named).is_file():
        return folder / named
    candidates = [p for p in folder.glob("*.docx")
                  if not p.name.startswith("~$")
                  and not p.name.endswith(docx_engine.PASS1_SUFFIX)]
    return candidates[0] if len(candidates) == 1 else None


def word_page_check(rec: dict, apply: bool = True) -> tuple[int | None, str]:
    """Cross-check the page count with Word COM. Returns (pages, note).

    Word renders to scratch and NOTHING here is ever copied over the canonical
    PDF. Word's pagination is what a recruiter sees, so a disagreement with
    soffice is worth flagging - but Word's PDF text layer sprays spaces inside
    compound keywords ("real -time", "cross -functional"), which is what an ATS
    parses, so it must not write a file anyone submits (measured 2026-08-19;
    see docx_engine.default_renderer). The caller records the disagreement on
    the row; it no longer changes the status.

    The shipped PDF is the soffice render the tailor already produced. `apply`
    only gates the one repair case below, where no canonical PDF exists at all.
    """
    docx = resume_docx(rec)
    if docx is None:
        return None, "no unambiguous .docx in the folder"
    scratch = Path(os.environ.get("TEMP", "/tmp")) / "sync_render"
    scratch.mkdir(parents=True, exist_ok=True)
    tmp_pdf = scratch / (docx.stem + ".pdf")
    try:
        _, pages = docx_engine.render_pdf(docx, tmp_pdf, renderer="word")
    except Exception as exc:
        return None, f"Word render failed: {str(exc)[:160]}"

    note = ""
    target = docx.with_suffix(".pdf")
    if not target.exists():
        # The submittable artifact is missing entirely. Rebuild it with soffice
        # (never by copying Word's scratch render, whose text layer is the
        # defect this split exists to avoid).
        if not apply:
            note = "no canonical PDF; dry run left it missing"
        else:
            try:
                docx_engine.render_pdf(docx, target, renderer="soffice")
                note = "canonical PDF was missing; re-rendered with soffice"
            except Exception as exc:
                note = f"canonical PDF missing and soffice re-render failed: {str(exc)[:120]}"
    return pages, note


REVIVE_DAYS = 7


def recent_tailor(rec: dict, days: float = REVIVE_DAYS) -> bool:
    """The sidecar's resume was finished within `days` (unparseable = no)."""
    try:
        at = datetime.datetime.strptime(rec.get("tailored_at", ""), "%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        return False
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    return (now - at).total_seconds() < days * 86400


def ingest_tailors(conn, apply: bool = False) -> dict:
    """Fold tailor sidecars into the DB and record what the artifact reports.

    Only rows still at `scored`/`new` are advanced, plus rows already sitting at
    `tailored`/`needs_review` whose intent changed. Rows at `applied` or beyond
    are never touched: Vaish's judgment outranks a Routine's.

    The `status_intent` the Routine wrote is applied as given. Word's page count
    no longer overrides it (v3): the cloud reviewed the resume and every
    finished one is submittable, so a second Word page is a flag on the row
    (`word_pages=` in `notes`) for Vaish to look at, not a status change made by
    a script that cannot reflow a resume. The review scores ride along the same
    way (`scores=ats/recruiter/hm`), which is how the dashboard reads them
    without a schema change.
    """
    good, bad, notes = load_tailor_sidecars()
    result = {"valid": len(good), "malformed": bad, "notes": notes,
              "applied": [], "skipped": [], "word_check": [], "missing_row": [],
              "scores": {}}

    for rec in good:
        jid = rec["id"]
        # Report-only, and deliberately carried for EVERY valid sidecar rather
        # than only the ones whose status moves: the numbers describe the
        # artifact, not the transition, and a re-run that skips the row must not
        # make them disappear from the summary.
        if rec.get("scores"):
            result["scores"][jid] = rec["scores"]
        row = conn.execute("SELECT status, notes FROM jobs WHERE id = ?", (jid,)).fetchone()
        if row is None:
            result["missing_row"].append(jid)
            continue
        current = row[0]
        # An age sweep is not a verdict on a finished resume. detect.yml sweeps
        # every 2h, and a sidecar that waits on the fold (laptop shut down
        # 10-01 to 10-04) found its row already `stale_skip` and was refused:
        # 32 reviewed resumes never reached the dashboard. A delisted posting is
        # gone, so that one stays refused, and so does a resume older than a
        # week: that posting is past applying, and reviving it would fill
        # "Ready to submit" with August.
        if (current == "stale_skip" and "delisted" not in (row[1] or "")
                and recent_tailor(rec)):
            current = "scored"
        if current not in ("new", "scored", "tailored", "needs_review"):
            result["skipped"].append((jid, f"status {current} outranks a tailor sidecar"))
            continue

        # Cheap, idempotent and independent of the transition, so it happens
        # before the short-circuit below: a row whose status is already right
        # still needs its numbers if an earlier run predates them.
        line = format_scores(rec.get("scores"))
        if line and apply:
            set_note_clause(conn, jid, "scores", line)

        intent = rec["status_intent"]
        # Short-circuit BEFORE the render. A sidecar whose intent the row already
        # holds was ingested on an earlier run and has already been checked;
        # re-rendering it every run spends a Word COM round-trip to learn
        # something already recorded. Word now runs only where the answer can
        # still change something on the row.
        if current == intent:
            result["skipped"].append((jid, f"already {current}"))
            continue

        if CI:
            # No Word on a runner. Record nothing rather than `word_pages=?`: the
            # cloud already rendered one page under soffice, and a permanent "?"
            # would read as a disagreement nobody can clear.
            result["applied"].append((jid, row[0], intent, "folded in CI (no Word check)"))
            if apply:
                db.set_status(conn, jid, intent, via="sync")
            continue
        pages, note = word_page_check(rec, apply=apply)
        # Recorded whatever the answer is, so the clause SELF-CORRECTS: a folder
        # flagged at two pages and re-tailored to one would otherwise keep the
        # flag forever (the row is at `tailored` by then, so the check never
        # runs again to clear it). Permanent is fine here; stale is not.
        if apply:
            set_note_clause(conn, jid, "word_pages",
                            "?" if pages is None else pages)
        if pages != 1:
            # Not a gate any more, but never silent: the number a recruiter's
            # Word would show disagrees with the one the cloud rendered.
            result["word_check"].append((jid, pages, note))
            reason = (f"Word could not render ({note})" if pages is None else
                      f"Word renders {pages} pages (cloud saw "
                      f"{rec.get('pages_soffice', '?')} under soffice)")
        else:
            reason = note or "Word confirms 1 page"

        result["applied"].append((jid, row[0], intent, reason))
        if apply:
            db.set_status(conn, jid, intent, via="sync")

    return result


def reconcile_tailor_sidecars(apply: bool = False) -> list[tuple[str, str, str]]:
    """Keep `tailored/` sidecars honest about state the folder moved past.

    These sidecars are permanent by design - they are the proof an artifact
    exists, and unlike score sidecars nothing sweeps them. Permanent is fine;
    stale is not. A sidecar written after round 1 and a `review.md` rewritten by
    round 2 disagree about the same resume, and the sidecar is what the cloud
    reads on its next run.

    `review.md` wins, because it is written last and by the reviewers
    themselves. Only `scores` is rewritten; the page count, the status intent
    and the provenance are the cloud's to report and are never touched.
    """
    changed: list[tuple[str, str, str]] = []
    if not TAILORED_DIR.exists():
        return changed
    for path in sorted(TAILORED_DIR.glob("*.json")):
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        review = ROOT / (rec.get("folder") or "") / "review.md"
        if not review.is_file():
            continue
        found = SCORES_LINE.findall(review.read_text(encoding="utf-8",
                                                     errors="replace"))
        if not found:
            continue
        ats, recruiter, hm = (int(v) for v in found[-1])   # last line wins
        scores = {"ats": ats, "recruiter": recruiter, "hm": hm}
        if rec.get("scores") == scores:
            continue
        changed.append((rec.get("id", path.stem), "scores",
                        format_scores(scores)))
        if apply:
            path.write_text(json.dumps(dict(rec, scores=scores), indent=2),
                            encoding="utf-8")
    return changed


def detect_partial_tailors(conn, apply: bool = False) -> list[tuple[str, str]]:
    """Application folders with an artifact, no sidecar, and a row still waiting.

    The cloud contract says a run that dies partway writes NO sidecar, because a
    sidecar means a usable artifact exists. But the half-written folder is still
    committed, and its id in brief.md permanently disqualifies the job from the
    cloud work list. So the job ends up with an artifact nobody vouched for, no
    sidecar, and a row that still reads `scored` - invisible on the dashboard and
    never retried by anything. Surfacing it as needs_review is the whole fix:
    Vaish sees it, and decides whether to re-run `/tailor <id>` or drop it.
    """
    have_sidecar = {p.stem for p in TAILORED_DIR.glob("*.json")}         if TAILORED_DIR.exists() else set()
    found: list[tuple[str, str]] = []
    for jid, folder in application_folders().items():
        if jid in have_sidecar:
            continue
        row = conn.execute("SELECT status FROM jobs WHERE id = ?", (jid,)).fetchone()
        if row is None or row[0] not in ("new", "scored"):
            continue
        found.append((jid, folder.name))
        if apply:
            db.set_status(conn, jid, "needs_review", via="sync")
    return found


def reconcile_queue(conn, apply: bool = False) -> dict:
    """Repair the queue/DB pair in BOTH directions.

    scrape.py commits the DB row before writing the queue file, so a crash
    between the two leaves a `new` row with no queue file: the cloud scorer's
    work list is queue-shaped, so that job is never scored, and freshness can
    only sweep rows whose date parses — an unknown-date row then sits at `new`
    forever. The mirror case is worse: a queue file with no row gets re-scored
    every cloud run while ingest classifies the sidecar `unknown` and drops it,
    and sweep_orphans (which keys off the queue file) never fires. That one is
    invisible to both layers at once.

    Repairing here beats reordering the writes in scrape.py, which would only
    trade the visible failure for the invisible one. Note the dependency: the
    re-emit path reads `jd_text` out of the DB, so it queries db.JD_KEEP_STATUSES
    directly — the same constant db.shed_jd_text preserves the column for. Those
    two must never drift apart, or this re-emits queue files with an empty JD.
    """
    result = {"requeued": [], "inserted": [], "failed": []}
    queued = {p.stem for p in QUEUE_DIR.glob("*.json")} if QUEUE_DIR.exists() else set()

    keep = ",".join("?" * len(db.JD_KEEP_STATUSES))
    # `first_seen` rides along because a re-emitted queue file must carry the
    # same observation bound as an original one. Without it a repaired row's
    # file says nothing about when the row was first recorded, and the cloud
    # Routine — whose only input is the queue file — would bucket a row it has
    # been holding for two days as if the posting date were all we knew.
    rows = conn.execute(
        "SELECT id, company, title, location, url, source, jd_text, posted_at, first_seen "
        f"FROM jobs WHERE status IN ({keep})", db.JD_KEEP_STATUSES).fetchall()
    missing = [r for r in rows if r[0] not in queued]
    for jid, company, title, location, url, source, jd_text, posted_at, first_seen in missing:
        result["requeued"].append((jid, company or "", title or ""))
        if apply:
            try:
                import scrape          # lazy: only a repair run needs it
                scrape.write_queue_entry(
                    jid, company or "",
                    {"title": title or "", "location": location or "",
                     "url": url or "", "posted_at": posted_at or "",
                     "jd": jd_text or ""},
                    source or "recovered", queue_dir=QUEUE_DIR,
                    first_seen=first_seen or "")
            except Exception as exc:
                result["failed"].append((jid, f"re-emit failed: {str(exc)[:80]}"))

    known = {r[0] for r in conn.execute("SELECT id FROM jobs")}
    for jid in sorted(queued - known):
        try:
            entry = json.loads((QUEUE_DIR / f"{jid}.json").read_text(encoding="utf-8"))
        except Exception as exc:
            result["failed"].append((jid, f"unreadable queue file: {str(exc)[:80]}"))
            continue
        result["inserted"].append((jid, entry.get("company", ""), entry.get("title", "")))
        if apply:
            got = db.insert_job(conn, entry.get("company", ""), entry.get("title", ""),
                                entry.get("location", ""), entry.get("url", ""),
                                entry.get("source", "recovered"),
                                jd_text=entry.get("jd_markdown", ""),
                                posted_at=entry.get("posted_at", ""))
            if got != jid:
                # The id is a content hash, so a mismatch means the normalizers
                # changed since this file was written. Say so instead of leaving
                # a row and a queue file that will never find each other.
                result["failed"].append(
                    (jid, f"id hashes to {got[:8]} now; queue file left for review"))
    return result


# ------------------------------------------------------------------------ main

def fold(args, apply: bool) -> dict:
    """Steps 2-6: take the cloud's output and fold it into the DB.

    Split out of main() so the push retry can redo it verbatim. After a losing
    push race the recovery is `reset --hard origin/main`, which replaces the DB
    with the remote's copy and throws this run's writes away — so the fold has
    to be recomputable, and every input it reads (branch sidecars, queue files,
    the DB) still exists at that point. Opens its own connection because the
    file underneath it is what the reset replaces.
    """
    summary: dict = {}
    conn = db.connect()

    step("2", "take cloud branch output")
    branches = judge_branches()
    if not branches:
        print(f"    no {JUDGE_BRANCH_PREFIX}* branch on origin yet")
    taken, kept, unfinished = [], [], []
    for branch in branches:
        # The notes file is the routine's memory: it appends to what main had,
        # so the branch copy is a superset and wholesale is right. Left out of
        # this list, the first run's notes were lost when its branch was deleted.
        before = sidecar_times() if apply else {}
        fetch_branch_paths(branch, ["scores/", "tailored/", ROUTINE_NOTES], apply=apply)
        if apply:
            for name in keep_newer_sidecars(before, branch_changed(branch, "scores/")):
                print(f"    kept main's scores/{name} over {branch}'s stale copy")
        t, k, u = fetch_new_application_folders(branch, apply=apply)
        taken += t
        kept += k
        unfinished += u
    for rel in taken:
        print(f"    new folder from the cloud: {rel}"
              f"{'' if apply else ' (dry run: not taken)'}")
    for rel in unfinished:
        # Never silent: a deferred folder looks exactly like a missing one.
        print(f"    DEFERRED (no tailored/ sidecar on the branch yet, so the"
              f" cloud is still working on it): {rel}")
    if kept:
        print(f"    {len(kept)} existing application folder(s) left alone"
              " (main is authoritative once a folder exists here)")

    step("2.5", "reconcile queue files against the DB, both directions")
    q = reconcile_queue(conn, apply=apply)
    if not any(q.values()):
        print("    queue and DB agree")
    for jid, company, title in q["requeued"]:
        print(f"    re-emit queue file for {jid[:8]}  {company} | {title[:38]}")
    for jid, company, title in q["inserted"]:
        print(f"    insert missing row for {jid[:8]}  {company} | {title[:38]}")
    for jid, why in q["failed"]:
        print(f"    COULD NOT REPAIR {jid[:8]}: {why}")

    step("3", "ingest score sidecars")
    plan = ingest_scores.ingest(conn, apply=apply)
    print(f"    ingest into DB (row is 'new') : {len(plan['ingest'])}")
    print(f"    re-laned (routine or re-score): {len(plan['relane'])}")
    print(f"    already past scoring (no-op)  : {len(plan['already'])}")
    print(f"    id not in this DB (ignored)   : {len(plan['unknown'])}")
    for path, why in plan["malformed"]:
        print(f"    MALFORMED {path.name}: {why}")
    # Keeps "queue file without a sidecar == needs scoring" true for the cloud,
    # so locally-scored jobs are not re-scored on the next Routine run.
    backfilled = ingest_scores.backfill(conn, apply=apply)
    if backfilled:
        print(f"    backfilled {len(backfilled)} local score(s) as sidecars")
    # Sidecars whose queue file is gone are dead: the job left the work set.
    orphans = ingest_scores.sweep_orphans(apply=apply)
    if orphans:
        print(f"    swept {len(orphans)} orphan sidecar(s)")

    # After step 3 and before step 3.5, and neither side of that is arbitrary. A
    # promote can only land once the score it promotes has been ingested, and a
    # re-laned note has to be in the DB before 3.5 copies notes into the sidecar
    # the cloud tailor reads -- otherwise the promotion is invisible until the
    # NEXT sync. Same for a mark: the status has to move before 3.5 decides
    # whether the row still belongs in the work set.
    step("3.2", "fold dashboard decisions")
    dec = decisions.apply(conn, apply=apply)
    print(f"    applied to the DB              : {len(dec['applied'])}")
    print(f"    still pending (row not ready)  : {len(dec['pending'])}")
    print(f"    obsolete (row already moved on): {len(dec['obsolete'])}")
    for entry, what in dec["applied"]:
        print(f"    {entry['id'][:8]}  {entry['action']:<9} {what}")
    for entry, why in dec["obsolete"]:
        print(f"    {entry['id'][:8]}  {entry['action']:<9} DROPPED: {why}")
    for entry, hours in dec["wedged"]:
        print(f"    WEDGED {entry['id'][:8]}  {entry['action']} recorded"
              f" {hours / 24:.1f} day(s) ago and still cannot land")
    for entry, why in dec["malformed"]:
        print(f"    MALFORMED {entry}: {why}")
    summary["decisions_applied"] = len(dec["applied"])
    summary["decisions_pending"] = len(dec["pending"])
    summary["decisions_obsolete"] = len(dec["obsolete"])
    summary["decisions_wedged"] = len(dec["wedged"])

    step("3.5", "reconcile sidecars with the DB (the cloud tailor reads these)")
    rec = reconcile_sidecars(conn, apply=apply)
    if not rec["dropped"] and not rec["relaned"]:
        print("    sidecars already agree with the DB")
    for jid, company, title, status in rec["dropped"]:
        print(f"    drop  {jid[:8]}  {company} | {title[:38]}  (now {status})")
    for jid, company, title, was, now in rec["relaned"]:
        print(f"    lane  {jid[:8]}  {company} | {title[:38]}  {was} -> {now}")

    step("4", "ingest tailor sidecars (Word page count is a flag, not a gate)")
    tailors = ingest_tailors(conn, apply=apply)
    print(f"    valid sidecars: {tailors['valid']}")
    for path, why in tailors["malformed"]:
        print(f"    MALFORMED {path.name}: {why}")
    for path, why in tailors["notes"]:
        print(f"    NOTE {path.name}: {why}")
    for jid, s in tailors["scores"].items():
        print(f"    scores {jid[:8]}: ats {s.get('ats')} / recruiter"
              f" {s.get('recruiter')} / hm {s.get('hm')}")
    for jid, was, now, why in tailors["applied"]:
        print(f"    {jid[:8]}  {was} -> {now}   ({why})")
    for jid, pages, note in tailors["word_check"]:
        print(f"    WORD CHECK  {jid[:8]}  {'?' if pages is None else pages}"
              f" pages{f' ({note})' if note else ''}")
    for jid, why in tailors["skipped"]:
        print(f"    skip {jid[:8]}: {why}")
    for jid in tailors["missing_row"]:
        print(f"    no DB row for {jid[:8]} (sidecar from another clone?)")

    step("4.5", "reconcile tailor sidecars + catch half-finished cloud runs")
    drift = reconcile_tailor_sidecars(apply=apply)
    for jid, field, now in drift:
        print(f"    {jid[:8]}  {field} -> {now}")
    partial = detect_partial_tailors(conn, apply=apply)
    for jid, folder in partial:
        print(f"    PARTIAL {jid[:8]}: {folder} has an artifact but no sidecar"
              f" -> needs_review{'' if apply else ' (dry run)'}")
    if not drift and not partial:
        print("    tailor sidecars agree with the folders")

    step("5", "freshness sweep")
    stale = freshness.sweep(conn, hours=args.hours, apply=apply)
    print(f"    {len(stale)} row(s) provably past {args.hours:g}h")
    if apply:
        # Idempotent, so safe under the push-retry refold. This is the seat
        # that VACUUMs: twice a day, not once an hour.
        shed, freed = db.shed_jd_text(conn, vacuum=True)
        if shed:
            print(f"    shed jd_text from {shed} settled row(s) ({freed / 1e3:.0f} KB)")

    step("6", "regenerate dashboard")
    if CI:
        print("    skipped in CI (dashboard.yml publishes)")
    elif apply:
        run([sys.executable, str(PIPELINE_DIR / "dashboard.py")])
    else:
        print("    skipped in dry run")

    if apply:
        # kind='sync' so this never masquerades as a detection run on the
        # dashboard clock: a Layer 2 run says nothing about whether Layer 1 is alive.
        db.record_run(conn, kind="sync", summary=(
            f"sync: tailors={len(tailors['applied'])} "
            f"stale={len(stale)} word_check={len(tailors['word_check'])}"))
    conn.commit()
    print(f"\n    db totals: {db.counts_by_status(conn)}")
    conn.close()
    return summary


def stage_and_commit() -> int:
    """Stage the fold's output and commit it. Returns the file count (0 = none)."""
    # decisions/pending.json is named as a FILE, not a directory: the fold
    # rewrites it in place (down to `[]`) and must never delete it, because a
    # missing path would fail this add and wedge every later run.
    run(["git", "add", "-A", "queue/", "scores/", "tailored/", "applications/",
         "pipeline/jobs.sqlite", "decisions/pending.json"])
    staged = run(["git", "diff", "--cached", "--name-only"], quiet=True)
    lines = [l for l in (staged.stdout or "").splitlines() if l.strip()]
    if not lines:
        print("    nothing to commit")
        return 0
    run(["git", "commit", "-q", "-m", f"sync: {len(lines)} file(s) @ {db.now_iso()}"])
    print(f"    committed {len(lines)} file(s)")
    return len(lines)


def fail_banner(exc: Exception) -> None:
    print("\n" + "!" * 70)
    print(f"SYNC FAILED: {exc}")
    print("Nothing was pushed. The DB may hold partial writes from this run;")
    print("they are idempotent, so re-running after fixing the cause is safe.")
    print("!" * 70)


def defer_banner(locked: list[str], consecutive: int) -> None:
    print("\n" + "!" * 70)
    print("SYNC DEFERRED: another process is holding files git must rewrite.")
    for p in locked:
        print(f"  LOCKED  {p}")
    print("")
    print("NOTHING was changed - no pull, no stash, no commit, no push. Close the")
    print("file (a resume open in Acrobat is the usual cause) and the next run")
    print("picks up where this one stopped.")
    if consecutive >= 2:
        print(f"")
        print(f"This is defer #{consecutive} in a row. Cloud work is piling up "
              "unfolded;")
        print("nothing is lost, but nothing is landing either.")
    print("!" * 70)


def defer(locked: list[str]) -> int:
    """End the run having mutated nothing. Returns the process exit code."""
    state = read_state()
    consecutive = int(state.get("consecutive", 0)) + 1
    defer_banner(locked, consecutive)
    record_outcome("deferred", f"locked: {', '.join(locked[:5])}", locked)

    # Park anything unpushed before walking away: a ref push needs no
    # working-tree checkout, so the lock cannot block it, and the work then
    # survives even if this laptop never syncs again.
    ahead = _lines(run(["git", "rev-list", "origin/main..HEAD"],
                       check=False, quiet=True))
    if ahead:
        rescue_push("HEAD", "pre-defer")

    # Local dashboard only. dashboard/ is gitignored, and db.connect()+read does
    # not dirty the binary (sync.py has read it after every push for months).
    run([sys.executable, str(PIPELINE_DIR / "dashboard.py")], check=False, quiet=True)

    body = f"{len(locked)} file(s) locked - {Path(locked[0]).name}"
    if consecutive >= 2:
        body += f" (defer #{consecutive})"
    winlock.notify("Layer 2 sync deferred", body + ". Nothing was changed.")
    return EXIT_DEFERRED


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="report only: no DB writes, no file writes, no push")
    ap.add_argument("--no-push", action="store_true", help="write locally, skip the push")
    ap.add_argument("--hours", type=float, default=db.STALE_HOURS,
                    help=f"stale cutoff in hours (default {db.STALE_HOURS})")
    ap.add_argument("--no-branch-reset", action="store_true",
                    help="skip pointing the cloud branch back at main")
    ap.add_argument("--force", action="store_true",
                    help="run even if a run already succeeded in this retry window")
    ap.add_argument("--ci", action="store_true",
                    help="GitHub Actions fold: no Word check, no local dashboard")
    args = ap.parse_args(argv)
    apply = not args.dry_run
    global CI
    CI = args.ci

    # ASCII only in this script's output: Task Scheduler pipes it through cmd.exe,
    # whose console codepage mangles non-ASCII into replacement characters in the
    # log that is the only record of an unattended run.
    print("=" * 70)
    print(f"Layer 2 sync - {'DRY RUN' if args.dry_run else 'applying'}")
    print("=" * 70)

    # The scheduled slot repeats every 15 min for an hour so a deferred run gets
    # retried at all. Repeats after a good run are a no-op by design.
    if apply and not args.force:
        skip = already_succeeded_recently()
        if skip:
            print(f"\nnothing to do: {skip}")
            return EXIT_OK

    step("1", "git pull (before the first DB write, per the two-writer rule)")
    baseline = None
    # The abort comes first: a detached mid-rebase HEAD makes the gate's
    # `git diff HEAD origin/main` meaningless.
    if apply:
        abort_stray_rebase()
    locked = lock_gate()
    if locked:
        if apply:
            return defer(locked)
        print("    a real run would DEFER here; nothing to protect in a dry run")
    if apply:
        drop_stranded_sync_commits()
        git_pull()
        recover_orphaned_autostash()
        # HEAD right after the pull. Everything this run commits comes after it,
        # so anything in origin/main..baseline predates the run and is not ours
        # to destroy in the push-race reset.
        baseline = (run(["git", "rev-parse", "HEAD"], check=False,
                        quiet=True).stdout or "").strip() or None
    else:
        print("    pull skipped in dry run")

    # The tip the branch had when this run read it. The reset in step 8 leases
    # against exactly this, so a Routine that pushes while we work is safe.
    leases = {}
    for branch in judge_branches():
        run(["git", "fetch", "origin", branch], check=False, quiet=True)
        p = run(["git", "rev-parse", f"origin/{branch}"], check=False, quiet=True)
        leases[branch] = (p.stdout or "").strip() if p.returncode == 0 else ""

    summary = fold(args, apply)

    step("7", "commit and push")
    if args.dry_run:
        print("    skipped in dry run")
    elif args.no_push:
        stage_and_commit()
        print("    push skipped (--no-push)")
    else:
        stage_and_commit()
        push_with_retry(lambda: (fold(args, True), stage_and_commit()),
                        baseline=baseline)

    step("8", "point the cloud branch back at main, drop run branches")
    if args.dry_run or args.no_push or args.no_branch_reset:
        print("    skipped")
    else:
        done = reset_cloud_branches(leases)
        print(f"    reset {', '.join(done) if done else 'nothing'}")

    conn = db.connect()
    # Anything the cloud could not finish is Vaish's call, so say it out loud
    # rather than leaving it in a status column nobody reads.
    needs = conn.execute(
        "SELECT id, company, title FROM jobs WHERE status='needs_review'").fetchall()
    if needs:
        print(f"\n  {len(needs)} job(s) need review before submitting:")
        for jid, company, title in needs:
            print(f"    {jid[:8]}  {(company or '')[:24]:24s} {(title or '')[:40]}")
    conn.close()

    if apply:
        detail = f"{len(needs)} need_review"
        # A decision that cannot land is invisible on the dashboard (the entry
        # just sits there), so the run's own outcome has to say it out loud.
        if summary.get("decisions_wedged"):
            detail += f", {summary['decisions_wedged']} wedged decision(s)"
        record_outcome("ok", detail)
    print("\ndone.")
    return EXIT_OK


if __name__ == "__main__":
    # An unattended run's only trace is this log, so a crash must announce itself
    # on the last line rather than as a bare traceback the reader has to decode.
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except LockedArtifacts as exc:
        # A lock that appeared mid-run (the push-race reset re-checks). Same
        # verdict as the gate: nothing to fix, try again later.
        raise SystemExit(defer(exc.locked))
    except Exception as exc:
        import traceback
        traceback.print_exc()
        # record_outcome never raises, so it cannot mask the failure it describes.
        record_outcome("failed", str(exc))
        winlock.notify("Layer 2 sync FAILED", str(exc)[:180])
        fail_banner(exc)
        raise SystemExit(EXIT_FAIL)
