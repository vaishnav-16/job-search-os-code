"""Tests for schedule.py -- the derived Layer 2 stage chain.

Two failure modes are worth pinning here, and neither is arithmetic for its own
sake.

The first is the one that already happened once: register_sync_task.ps1 carried
7:45am/3:45pm while the live task had four triggers, and nothing compared them,
so the drift was only found by looking. docs/schedule.md is the new copy of the
same fact, so the doc has to carry `--print` output VERBATIM and a test has to
diff them -- otherwise the doc rots the same way, just in markdown.

The second is subtler: sync.py short-circuits a run whose predecessor succeeded
inside RETRY_WINDOW_MIN. Two trigger slots closer together than that window
would mean the second slot silently never does work, which looks exactly like a
healthy schedule from outside. So the spacing is asserted against sync.py's own
constant rather than against a number retyped here.

Run: python pipeline/test_schedule.py   (pure computation; no clock, no network)
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import schedule  # noqa: E402
import sync  # noqa: E402

PASS = FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"FAIL  {name}" + (f" - {detail}" if detail else ""))


def times_of(profile: str, stage: str) -> set[str]:
    return {schedule.fmt_hhmm(t) for t in schedule.stage_utc(profile, stage)}


def local_of(profile: str, stage: str) -> set[str]:
    return {schedule.fmt_hhmm(t) for t in schedule.stage_laptop(profile, stage)}


def main() -> int:
    # --- home: every derived time, against the numbers the design fixed ---
    check("home windows are 12/21/02 UTC",
          {schedule.fmt_hhmm(t) for t in schedule.windows_utc("home")}
          == {"12:00", "21:00", "02:00"})
    check("home tailor-queue cron", schedule.cron_for("home", "tailor") == "30 8,17,22 * * *",
          schedule.cron_for("home", "tailor"))
    check("home score-queue cron",
          schedule.cron_for("home", "score") == "55 3,7,11,15,19,23 * * *",
          schedule.cron_for("home", "score"))
    check("home sync UTC", times_of("home", "sync") == {"01:00", "11:00", "20:00"})
    check("home sync laptop", local_of("home", "sync") == {"07:00", "16:00", "21:00"})
    check("home has exactly three sync triggers",
          len(schedule.sync_trigger_times("home")) == 3)

    # --- the chain's ORDER is the design; assert it rather than assume it ---
    for profile in sorted(schedule.SCHEDULES):
        order = ["tailor", "sync"]
        offs = [schedule.STAGE_OFFSETS_MIN[s] for s in order]
        check(f"{profile}: stages run tailor -> sync",
              offs == sorted(offs, reverse=True), str(offs))
        check(f"{profile}: tailor has at least 120 min before the sync",
              schedule.STAGE_OFFSETS_MIN["tailor"] - schedule.STAGE_OFFSETS_MIN["sync"] >= 120)
        # score-queue feeds tailor-queue: some score fire lands 30-60 min before
        # every tailor fire would be ideal, but a 4h grid cannot hit three
        # unevenly spaced fires; it must at least precede each within 4h, and
        # land 30+ min after a detect run (:17 past odd UTC hours).
        score = schedule.stage_utc(profile, "score")
        for t in schedule.stage_utc(profile, "tailor"):
            lead = min((t - s) % schedule.DAY for s in score)
            check(f"{profile}: a score fire precedes tailor {schedule.fmt_hhmm(t)} "
                  f"within 4h (lead {lead} min)", 0 < lead <= 240)
        check(f"{profile}: every score fire is 30+ min after a :17 odd-hour detect",
              all(((s - 17) % 120) >= 30 for s in score),
              str([schedule.fmt_hhmm(s) for s in score]))

    # --- no two sync slots inside sync.py's own short-circuit window ---
    for profile in sorted(schedule.SCHEDULES):
        mins = sorted(schedule.parse_hhmm(t)
                      for t in schedule.sync_trigger_times(profile))
        gaps = [(b - a) for a, b in zip(mins, mins[1:])]
        gaps.append(mins[0] + schedule.DAY - mins[-1])  # wrap
        check(f"{profile}: every sync gap exceeds sync.RETRY_WINDOW_MIN "
              f"({sync.RETRY_WINDOW_MIN} min)",
              min(gaps) > sync.RETRY_WINDOW_MIN, f"min gap {min(gaps)} min")

    # --- routine runs must not share a five-hour usage window ---
    for profile in sorted(schedule.SCHEDULES):
        mins = sorted(schedule.stage_utc(profile, "tailor"))
        gaps = [(b - a) for a, b in zip(mins, mins[1:])]
        gaps.append(mins[0] + schedule.DAY - mins[-1])
        check(f"{profile}: tailor runs are >= 5h apart (five-hour usage window)",
              min(gaps) >= 300, f"min gap {min(gaps)} min")

    # --- --print is pure: same input, same bytes, or the doc diff is a coin flip
    for profile in sorted(schedule.SCHEDULES):
        check(f"{profile}: --print is idempotent",
              schedule.render(profile) == schedule.render(profile))
        check(f"{profile}: --print carries no catch-up trigger",
              "catch-up" not in schedule.render(profile).lower())

    # --- docs/schedule.md carries both blocks VERBATIM (the anti-drift check) ---
    doc = ROOT / "docs" / "schedule.md"
    if not doc.exists():
        check("docs/schedule.md exists", False, "missing")
    else:
        text = doc.read_text(encoding="utf-8").replace("\r\n", "\n")
        for profile in sorted(schedule.SCHEDULES):
            check(f"docs/schedule.md carries the {profile} block verbatim",
                  schedule.render(profile) in text)

    # --- registration has one owner, and it refuses to guess times ---
    ps1 = (ROOT / "pipeline" / "register_sync_task.ps1").read_text(encoding="utf-8")
    check("register_sync_task.ps1 takes -At", "[string]$At" in ps1)
    check("register_sync_task.ps1 refuses to register without -At",
          "-At is required to register" in ps1)
    # Comments legitimately NAME the old times (the drift is documented there);
    # what must not survive is a time in executable code.
    code = "\n".join(ln for ln in ps1.splitlines()
                     if not ln.lstrip().startswith("#"))
    check("register_sync_task.ps1 hardcodes no trigger times in code",
          "7:45am" not in code and "3:45pm" not in code)

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
