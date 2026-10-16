"""Derive the Layer 2 schedule from the review windows and apply it to Task Scheduler.

Chain per review window W (rebuilt 2026-09-16, one cloud routine):

    detect.yml (GitHub Actions, twice hourly)   -> queue/ on main
      -> judge-queue   cloud Routine at W-3:30: scores new queue files, then tailors
                       up to the cap; reads its OWN branch (claude/judge) for what it
                       already did, so it never depends on a laptop sync
      -> sync          laptop at W-1:00: folds scores/ + tailored/ + new application
                       folders into the DB, Word page cross-check (flag only),
                       dashboard, push -> Vercel
      -> Vaish reviews at W

Two clocks: routine crons are fixed UTC; Task Scheduler follows the laptop clock,
which stays Eastern (LAPTOP_UTC_OFFSET_H). `--print` is pure and its output is
carried verbatim in docs/schedule.md; test_schedule.py diffs the two.

Usage:
    python pipeline/schedule.py --profile home --print
    python pipeline/schedule.py --profile home --apply
    python pipeline/schedule.py --profile home --apply --catchup 17:30
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REGISTER_PS1 = HERE / "register_sync_task.ps1"
TASK_NAME = "job-search-os Layer 2 sync"

# The laptop clock is Eastern no matter which profile is active (Vaish's call:
# one clock to reason about while travelling beats a clock that moves under the
# scheduled task). EDT = UTC-4; see docs/schedule.md for the November switch.
LAPTOP_UTC_OFFSET_H = -4

# Minutes BEFORE the review window each stage starts. tailor-queue (split from
# judge-queue in W4, 2026-10-11) runs up to 3 fresh tailor subagents at a time
# and starts no new job after 5 hours, so a fire is done well before the next.
STAGE_OFFSETS_MIN = {
    "tailor": 210,   # W-3:30
    "sync": 60,      # W-1:00
}

# Which stages are cloud Routines (emit a cron) vs laptop syncs (emit triggers).
ROUTINE_STAGES = (("tailor", "tailor-queue"),)
SYNC_STAGES = ("sync",)

# score-queue is not tied to the windows: it scores every SCORE_EVERY_H hours
# (Vaish, 10-11), anchored SCORE_LEAD_MIN before the day's first tailor fire so
# that fire always finds fresh scores. Detection runs at :17 past odd UTC hours,
# so a :55 fire on odd hours lands 38 minutes after a detect run.
SCORE_ROUTINE = "score-queue"
SCORE_EVERY_H = 4
SCORE_LEAD_MIN = 35

SCHEDULES = {
    "home": {
        "utc_offset_h": -4,
        "windows_local": ["08:00", "17:00", "22:00"],
        "tz": "America/New_York",
        "note": "ACTIVE. EDT; after 2026-11-01 set LAPTOP_UTC_OFFSET_H=-5 and re-send the cron (see docs/schedule.md)",
    },
}

DAY = 24 * 60


def parse_hhmm(text: str) -> int:
    """'08:00' -> minutes since midnight. Raises on anything else."""
    hh, _, mm = text.partition(":")
    h, m = int(hh), int(mm)
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f"not a time of day: {text!r}")
    return h * 60 + m


def fmt_hhmm(minutes: int) -> str:
    """Minutes since midnight, wrapped into a day, as HH:MM."""
    minutes %= DAY
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def windows_utc(profile: str) -> list[int]:
    cfg = SCHEDULES[profile]
    off = cfg["utc_offset_h"] * 60
    return [(parse_hhmm(w) - off) % DAY for w in cfg["windows_local"]]


def score_utc(profile: str) -> list[int]:
    """score-queue's UTC times: every SCORE_EVERY_H hours, anchored before the
    first tailor fire of the UTC day."""
    anchor = stage_utc(profile, "tailor")[0] - SCORE_LEAD_MIN
    step = SCORE_EVERY_H * 60
    return sorted((anchor + k * step) % DAY for k in range(DAY // step))


def stage_utc(profile: str, stage: str) -> list[int]:
    """UTC times for one stage, sorted, as minutes since midnight."""
    if stage == "score":
        return score_utc(profile)
    back = STAGE_OFFSETS_MIN[stage]
    return sorted((w - back) % DAY for w in windows_utc(profile))


def stage_laptop(profile: str, stage: str) -> list[int]:
    """Same stage on the laptop clock, sorted on the laptop clock."""
    off = LAPTOP_UTC_OFFSET_H * 60
    return sorted((t + off) % DAY for t in stage_utc(profile, stage))


def cron_for(profile: str, stage: str) -> str:
    """A 5-field cron for a stage whose runs all share one minute-of-hour."""
    times = stage_utc(profile, stage)
    minutes = {t % 60 for t in times}
    if len(minutes) != 1:
        # Not a limitation worth engineering around -- it would mean the window
        # spacing no longer maps to a single cron, which is a schedule redesign,
        # not a formatting problem. Fail loudly rather than emit a wrong cron.
        raise ValueError(
            f"{stage} lands on more than one minute-of-hour ({sorted(minutes)}); "
            "cron cannot express that -- adjust the windows or split the stage")
    minute = minutes.pop()
    hours = ",".join(str(t // 60) for t in times)
    return f"{minute} {hours} * * *"


def sync_trigger_times(profile: str) -> list[str]:
    """All sync trigger times on the laptop clock, sorted, as HH:MM."""
    mins: list[int] = []
    for stage in SYNC_STAGES:
        mins.extend(stage_laptop(profile, stage))
    return [fmt_hhmm(m) for m in sorted(mins)]


def _join(times: list[int]) -> str:
    return " / ".join(fmt_hhmm(t) for t in times)


def render(profile: str) -> str:
    """The stable, clock-free block that docs/schedule.md carries verbatim."""
    cfg = SCHEDULES[profile]
    off = cfg["utc_offset_h"]
    lines = [
        f"profile: {profile}  ({cfg['tz']}, UTC{off:+03d}:00; "
        f"laptop clock UTC{LAPTOP_UTC_OFFSET_H:+03d}:00)",
        f"note:    {cfg['note']}",
        "",
        f"windows (local): {' / '.join(cfg['windows_local'])}",
        f"windows (UTC):   {_join(windows_utc(profile))}",
        "",
        "cloud Routines (cron is fixed UTC):",
    ]
    for stage, name in ROUTINE_STAGES:
        off_min = STAGE_OFFSETS_MIN[stage]
        lines.append(
            f"  {name:<13} W-{off_min // 60}:{off_min % 60:02d}  "
            f"UTC {_join(stage_utc(profile, stage)):<25} "
            f"cron  {cron_for(profile, stage)}")
    lines.append(
        f"  {SCORE_ROUTINE:<13} every {SCORE_EVERY_H}h  "
        f"UTC {_join(stage_utc(profile, 'score'))}  "
        f"cron  {cron_for(profile, 'score')}")
    lines += [
        "",
        f'Task Scheduler triggers for "{TASK_NAME}"',
        "(laptop clock; each repeats every 15 min for 1h):",
    ]
    for stage in SYNC_STAGES:
        off_min = STAGE_OFFSETS_MIN[stage]
        label = "sync"
        lines.append(
            f"  {label:<13} W-{off_min // 60}:{off_min % 60:02d}  "
            f"UTC {_join(stage_utc(profile, stage)):<25} "
            f"local {_join(stage_laptop(profile, stage))}")
    lines.append(f"  all, sorted:  {'  '.join(sync_trigger_times(profile))}")
    return "\n".join(lines)


def apply(profile: str, catchup: str = "") -> int:
    """Rewrite the task's triggers via register_sync_task.ps1.

    Registration deliberately lives in ONE place. This function computes times
    and hands them over; it does not build triggers itself. register_sync_task
    already owns the working -Principal and the repetition helper, and it is the
    file that drifted two triggers behind the live task by hardcoding them.
    """
    times = sync_trigger_times(profile)
    cmd = [
        "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", str(REGISTER_PS1),
        "-At", ",".join(times),
    ]
    if catchup:
        parse_hhmm(catchup)  # validate before handing it to PowerShell
        cmd += ["-Once", catchup]
    print(f"  registering {len(times)} daily triggers: {' '.join(times)}")
    if catchup:
        print(f"  plus one catch-up trigger today at {catchup}")
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if proc.stdout:
        print("".join(f"    {ln}\n" for ln in proc.stdout.splitlines()), end="")
    if proc.returncode != 0:
        print(f"    FAILED (exit {proc.returncode})")
        if proc.stderr:
            print("".join(f"    {ln}\n" for ln in proc.stderr.splitlines()), end="")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--profile", default="home", choices=sorted(SCHEDULES),
                    help="which window set to use (default: home)")
    ap.add_argument("--print", dest="show", action="store_true",
                    help="emit the stable schedule block and exit")
    ap.add_argument("--apply", action="store_true",
                    help="rewrite the Task Scheduler triggers (Windows only)")
    ap.add_argument("--catchup", default="",
                    help="HH:MM (laptop clock): add ONE extra trigger today, for "
                         "a cloud run already in flight. Windows keeps a fired "
                         "one-time trigger in the list as an inert entry, so it "
                         "is excluded from --print and cleared by the next --apply")
    args = ap.parse_args(argv)

    block = render(args.profile)
    print(block)

    if not args.apply:
        if not args.show:
            print("\nDRY RUN. Re-run with --apply to rewrite the Task Scheduler "
                  "triggers. The routine cron above is applied separately, from a "
                  "Claude session (RemoteTrigger update) -- the routine API is "
                  "not reachable from a plain shell.")
        return 0

    print("\napplying:")
    rc = apply(args.profile, args.catchup)
    if rc == 0:
        print("\n  Task Scheduler done. Still to do, by hand, in a Claude session:")
        for stage, name in (*ROUTINE_STAGES, ("score", SCORE_ROUTINE)):
            print(f"    RemoteTrigger update {name}: "
                  f"cron_expression = {cron_for(args.profile, stage)!r}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
