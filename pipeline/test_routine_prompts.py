"""Tests for the versioned cloud-routine prompts (.claude/routines/<name>.md).

Pins the few things that would silently break a cloud run if they went missing:
the prompt block exists and extracts cleanly, it stays short enough to send by
hand, and the load-bearing boundaries and steps are still named. When a
<name>.live.txt read-back exists, the prompt must match it byte for byte
(that snapshot is only ever saved from a `RemoteTrigger get`, never from --emit).

Run: python pipeline/test_routine_prompts.py
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import routine_prompt  # noqa: E402

PASS = FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"FAIL  {name}" + (f" - {detail}" if detail else ""))


# Split from judge-queue in W4 (2026-10-11); judge-queue's copy is in docs/history/routines/.
COMMON = [
    "Commit to the branch this session designates",
    "claude/judge*",
    "designated branch",
    "Never push to main",
]
REQUIRED = {
    "score-queue": COMMON + [
        "Write only inside scores/",
        "job-scorer",
        "python pipeline/worklist.py score",
        "python pipeline/ingest_scores.py record-dir /tmp/score-replies",
        "Reply file: /tmp/score-replies/<id>.md",
        "Never read, copy, summarize or re-type",
        "python pipeline/worklist.py siblings",
        "skip; <reason>",
        "duplicate role of <id>",
    ],
    "tailor-queue": COMMON + [
        "Write only inside scores/, tailored/, applications/",
        "Never submit an application",
        "writer.xcd",
        "python -m pip install -q --upgrade cffi",
        "RESUME_RENDERER=soffice",
        "python pipeline/worklist.py tailor",
        "digest-stretch",
        "deferred",
        "resume-tailorer",
        "REVIEW ROUND <N>",
        "SendMessage",
        "ats-scanner",
        "recruiter-screen",
        "hiring-manager",
        "readability-screen",
        "READABILITY_SCORE",
        "at most 3 in flight",
        "5 hours",
        "tailored/<id>.json",
        "judge-queue.notes.md",
    ],
}


def main() -> int:
    check("every versioned routine has a REQUIRED list",
          set(routine_prompt.names()) == set(REQUIRED), str(routine_prompt.names()))
    for name, required in REQUIRED.items():
        print(f"-- {name} --")
        body = routine_prompt.prompt_body(name)
        check("prompt body extracts", bool(body and body.strip()))
        check("prompt is under 9000 characters (hand-sendable)", len(body) < 9000,
              str(len(body)))
        check("prompt has no em-dash (the routine writes linted files)", "\u2014" not in body)
        for clause in required:
            check(f"carries: {clause}", clause in body)

        live = ROOT / ".claude" / "routines" / f"{name}.live.txt"
        if live.exists():
            snap = live.read_text(encoding="utf-8").replace("\r\n", "\n")
            check("matches the live read-back snapshot", snap.strip() == body.strip())
        else:
            print("  --  no live.txt snapshot yet (created after the first RemoteTrigger get)")

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
