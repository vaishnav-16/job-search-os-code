"""Keep the laptop a client of main: pull before a local command, publish after.

    python pipeline/localgit.py begin                       # refuse dirty pipeline paths, pull
    python pipeline/localgit.py publish -m MSG PATH [PATH ...]

WHY (2026-10-08). The laptop sync is retired: fold.yml folds every routine push and
local command in GitHub Actions (dashboard clicks wait for a detect-dispatched
fold since W7, 2026-10-13), so the DB has one writer
kind, Actions. The laptop never writes pipeline/jobs.sqlite any more. Local
commands write the same text-shaped records the cloud does (score and tailor
sidecars, application folders, decisions/pending.json), publish them here, and
the push to main triggers the fold. Text rebases; a binary DB does not, which is
the whole point.

What this guards against, each seen for real:
  - a scheduled sync's `reset --hard origin/main` wiped uncommitted knowledge
    edits (10-06). Nothing here resets the working tree; a rebase runs with
    --autostash, which keeps the stash on any conflict.
  - a pull that half-fails because something holds jobs.sqlite open (10-06,
    "unable to unlink"). begin() says so instead of leaving a mess.
  - a stale local copy: every command pulls at the moment it runs, not on a timer.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# What the pipeline owns. A dirty file here at begin() is a half-finished local
# command; anything else (profile/, docs, code) is Vaish's and is left alone.
PIPELINE_PATHS = ("pipeline/jobs.sqlite", "queue", "scores", "tailored",
                  "applications", "decisions")
PENDING = "decisions/pending.json"


class LocalGitError(RuntimeError):
    pass


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if check and proc.returncode != 0:
        raise LocalGitError(f"git {' '.join(args)} failed:\n{(proc.stderr or proc.stdout).strip()}")
    return proc


def dirty_pipeline_paths() -> list[str]:
    out = git("status", "--porcelain", "--", *PIPELINE_PATHS).stdout
    return [line[3:] for line in out.splitlines() if line.strip()]


def begin() -> None:
    """Refuse a dirty pipeline tree, then bring main up to date.

    A fast-forward is the normal case. A local commit that never reached origin
    (a publish that lost the network) is rebased on top, which is safe because
    publish() only ever commits text records.
    """
    dirty = dirty_pipeline_paths()
    if dirty:
        raise LocalGitError(
            "pipeline files have uncommitted changes, probably from a command that "
            "stopped halfway:\n  " + "\n  ".join(dirty[:10])
            + "\nPublish them (python pipeline/localgit.py publish -m '...' <paths>) "
              "or discard them (git restore -- <paths>), then re-run.")
    git("fetch", "-q", "origin", "main")
    ahead = git("rev-list", "--count", "origin/main..HEAD").stdout.strip()
    if ahead == "0":
        proc = git("merge", "--ff-only", "-q", "origin/main", check=False)
    else:
        proc = git("rebase", "--autostash", "origin/main", check=False)
        if proc.returncode != 0:
            git("rebase", "--abort", check=False)
    if proc.returncode != 0:
        why = (proc.stderr or proc.stdout).strip()
        hint = ("\nSomething has pipeline/jobs.sqlite open (a DB browser, a running "
                "script); close it and re-run." if "unlink" in why or "Permission" in why
                else "")
        raise LocalGitError(f"could not update main:\n{why}{hint}")


def _commit(paths: list[str], message: str) -> bool:
    git("add", "-A", "--", *paths)
    staged = git("diff", "--cached", "--name-only", "--", *paths).stdout.split()
    if not staged:
        return False
    # Pathspec form commits ONLY these paths, whatever else Vaish has staged.
    git("commit", "-q", "-m", message, "--", *paths)
    return True


def publish(paths: list[str], message: str, redo=None, attempts: int = 3) -> bool:
    """Commit exactly `paths` and push main. Returns False when nothing changed.

    On a rejected push the commit is rebased onto origin/main. The one expected
    conflict is decisions/pending.json (a dashboard click or the fold rewrote it
    meanwhile); `redo` re-applies this command's change to the fresh file, so the
    caller's intent survives and nobody else's is overwritten.
    """
    if not _commit(paths, message):
        return False
    for _ in range(attempts):
        if git("push", "-q", "origin", "HEAD:main", check=False).returncode == 0:
            return True
        git("fetch", "-q", "origin", "main")
        if git("rebase", "--autostash", "origin/main", check=False).returncode == 0:
            continue
        conflicted = git("diff", "--name-only", "--diff-filter=U").stdout.split()
        git("rebase", "--abort", check=False)
        if conflicted != [PENDING] or redo is None:
            raise LocalGitError(
                f"publish could not rebase onto origin/main (conflict in {conflicted}). "
                "The commit is kept locally; the next command's begin() retries it.")
        # Drop our commit, take origin's file, and re-apply our change to it.
        git("reset", "-q", "--soft", "HEAD~1")
        git("restore", "--staged", "--worktree", "--source", "origin/main", "--", PENDING)
        git("merge", "--ff-only", "-q", "origin/main")
        redo()
        _commit(paths, message)
    raise LocalGitError(f"push still rejected after {attempts} attempts; the commit is "
                        "kept locally and the next command's begin() retries it.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("begin")
    p = sub.add_parser("publish")
    p.add_argument("-m", "--message", required=True)
    p.add_argument("paths", nargs="+")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "begin":
            begin()
            print("main is up to date")
        else:
            print("published" if publish(args.paths, args.message) else "nothing to publish")
    except LocalGitError as e:
        print(f"STOPPED: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
