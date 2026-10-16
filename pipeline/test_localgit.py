"""Tests for localgit (the laptop as a client of main) and mark.py.

Run: python pipeline/test_localgit.py   (real git, temp repos, no network)

A bare "origin", a "laptop" clone and a "cloud" clone standing in for Actions.
The cases are the ones that bit: a push race, a race on decisions/pending.json,
uncommitted knowledge edits that must survive everything, a half-finished
command, and a commit stranded by a failed push.
"""

from __future__ import annotations

import json
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import decisions  # noqa: E402
import localgit  # noqa: E402
import mark  # noqa: E402

PASS = FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"FAIL  {name}" + (f" - {detail}" if detail else ""))


def sh(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          check=True).stdout


def clone(origin: Path, dest: Path) -> Path:
    subprocess.run(["git", "clone", "-q", "-c", "core.autocrlf=false", str(origin), str(dest)],
                   check=True, capture_output=True)
    sh(dest, "config", "user.name", "t")
    sh(dest, "config", "user.email", "t@t")
    return dest


def write(repo: Path, rel: str, text: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))


def commit_push(repo: Path, msg: str) -> None:
    sh(repo, "add", "-A")
    sh(repo, "commit", "-q", "-m", msg)
    sh(repo, "push", "-q", "origin", "HEAD:main")


def remote_file(repo: Path, rel: str) -> str:
    sh(repo, "fetch", "-q", "origin")
    return sh(repo, "show", f"origin/main:{rel}")


def use(repo: Path) -> None:
    localgit.ROOT = repo
    decisions.PENDING_PATH = repo / "decisions" / "pending.json"


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="localgit_"))
    try:
        origin = tmp / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
        seed = clone(origin, tmp / "seed")
        write(seed, "decisions/pending.json", "[]\n")
        write(seed, "profile/knowledge/a.md", "v1\n")
        write(seed, "scores/.keep", "")
        commit_push(seed, "seed")
        laptop = clone(origin, tmp / "laptop")
        cloud = clone(origin, tmp / "cloud")
        use(laptop)

        print("-- begin --")
        write(cloud, "queue/q1.json", "{}\n")
        commit_push(cloud, "layer1")
        write(laptop, "profile/knowledge/a.md", "v2 uncommitted\n")
        localgit.begin()
        check("begin fast-forwards main", (laptop / "queue" / "q1.json").exists())
        check("and leaves an uncommitted knowledge edit alone",
              (laptop / "profile/knowledge/a.md").read_text() == "v2 uncommitted\n")
        write(laptop, "scores/half.json", "{}\n")
        try:
            localgit.begin()
            check("begin refuses a dirty pipeline path", False)
        except localgit.LocalGitError as e:
            check("begin refuses a dirty pipeline path", "scores/half.json" in str(e), str(e))
        (laptop / "scores" / "half.json").unlink()

        print("-- publish --")
        write(laptop, "tailored/t1.json", '{"id": "t1"}\n')
        write(laptop, "applications/acme-pm/brief.md", "brief\n")
        sh(laptop, "add", "profile/knowledge/a.md")          # Vaish staged his own edit
        ok = localgit.publish(["tailored/t1.json", "applications/acme-pm"], "tailor: acme")
        check("publish pushes the records", ok and "t1" in remote_file(laptop, "tailored/t1.json"))
        check("but not Vaish's staged knowledge edit",
              remote_file(laptop, "profile/knowledge/a.md") == "v1\n")
        check("which is still staged and on disk",
              "profile/knowledge/a.md" in sh(laptop, "diff", "--cached", "--name-only")
              and (laptop / "profile/knowledge/a.md").read_text() == "v2 uncommitted\n")
        check("nothing to publish is a no-op",
              localgit.publish(["tailored/t1.json"], "again") is False)

        print("-- push race on other files --")
        sh(cloud, "pull", "-q")
        write(cloud, "queue/q2.json", "{}\n")
        commit_push(cloud, "layer1 again")
        write(laptop, "scores/s1.json", '{"id": "s1"}\n')
        localgit.publish(["scores/s1.json"], "score s1")
        log = sh(laptop, "log", "--format=%s", "origin/main", "-3")
        check("a race rebases and pushes, cloud commit kept",
              "score s1" in log and "layer1 again" in log, log)
        check("knowledge edit survives the race",
              (laptop / "profile/knowledge/a.md").read_text() == "v2 uncommitted\n")

        print("-- push race on decisions/pending.json --")
        jid_a, jid_b = "a" * 40, "b" * 40
        sh(cloud, "pull", "-q")
        write(cloud, "decisions/pending.json",
              json.dumps([{"id": jid_b, "action": "skipped", "at": "2026-10-08T10:00:00Z"}]) + "\n")
        commit_push(cloud, "dashboard click")
        mark.record(jid_a, "applied", "2026-10-08T10:05:00Z")
        redone = []
        localgit.publish([localgit.PENDING], "mark a",
                         redo=lambda: (redone.append(1),
                                       mark.record(jid_a, "applied", "2026-10-08T10:05:00Z")))
        check("the conflict took the redo path", redone == [1], str(redone))
        final = json.loads(remote_file(laptop, "decisions/pending.json"))
        check("both decisions survive the conflict",
              sorted(e["id"] for e in final) == [jid_a, jid_b], str(final))
        check("the mark carries via=mark",
              any(e["id"] == jid_a and e.get("via") == "mark" for e in final))
        mark.record(jid_a, "interview", "2026-10-08T11:00:00Z")
        entries = json.loads((laptop / "decisions/pending.json").read_text())
        check("a second mark replaces the first for that id",
              [e["action"] for e in entries if e["id"] == jid_a] == ["interview"], str(entries))
        sh(laptop, "checkout", "-q", "--", "decisions/pending.json")

        print("-- a commit stranded by a failed push --")
        write(laptop, "tailored/t2.json", '{"id": "t2"}\n')
        localgit._commit(["tailored/t2.json"], "tailor t2 (push failed)")
        sh(cloud, "pull", "-q")
        write(cloud, "queue/q3.json", "{}\n")
        commit_push(cloud, "layer1 third")
        localgit.begin()
        check("begin rebases a stranded local commit onto main",
              "layer1 third" in sh(laptop, "log", "--format=%s", "-3")
              and (laptop / "tailored/t2.json").exists())
        check("and the next publish carries it", localgit.publish(["tailored"], "x") is False
              and sh(laptop, "push", "-q", "origin", "HEAD:main") == ""
              and "t2" in remote_file(laptop, "tailored/t2.json"))
        check("knowledge edit survives everything",
              (laptop / "profile/knowledge/a.md").read_text() == "v2 uncommitted\n")
    finally:
        def unlock(func, path, _):
            Path(path).chmod(stat.S_IWRITE)
            func(path)
        shutil.rmtree(tmp, onerror=unlock)

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
