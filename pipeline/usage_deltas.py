"""Per-fire quota cost of the tailoring routine, from the rate-limit log.

Nothing in Claude Code attributes plan usage to a cloud routine, so the only
routine-level number is a difference: the plan percentage just before a fire
minus the percentage after the run ends. The status line script appends
`{ts, five_hour, seven_day, ...}` to ~/.claude/rate-limit-log.jsonl whenever
the values change (installed 2026-09-23), so the readings exist whenever a
local session was open. This script pairs them around each cron fire.

A fire is "clean" when no reading fell inside the run window: any reading
there means a local session was active and its own usage is mixed into the
delta. Jobs per fire come from tailored/*.json sidecars whose `tailored_at`
lies inside the window, so the last column is the per-job cost.

    python pipeline/usage_deltas.py            # every fire the log covers
    python pipeline/usage_deltas.py --days 3   # recent fires only
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = Path(os.path.expanduser("~/.claude/rate-limit-log.jsonl"))
# tailor-queue since W4 (10-11; judge-queue before, same cron). score-queue fires
# every 4h at :55 and stays outside these windows, so its cost is not in them.
ROUTINE = ROOT / ".claude" / "routines" / "tailor-queue.md"
TAILORED = ROOT / "tailored"
RUN_MINUTES = 70  # measured 09-19 to 09-22: about 57 minutes wall, plus slack


def read_log(path: Path = LOG) -> list[dict]:
    rows = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d.get("ts"), (int, float)):
            rows.append(d)
    rows.sort(key=lambda d: d["ts"])
    return rows


def cron_hours(routine: Path = ROUTINE) -> tuple[int, list[int]]:
    """(minute, [hours]) from the `cron_expression` line of the routine file."""
    m = re.search(r"^cron_expression\s+(\S+)\s+(\S+)\s+\*", routine.read_text(encoding="utf-8"), re.M)
    if not m:
        return 30, [8, 14, 21]
    return int(m.group(1)), [int(h) for h in m.group(2).split(",")]


def fires_between(start: datetime, end: datetime, minute: int, hours: list[int]) -> list[datetime]:
    out = []
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    while day <= end:
        for h in hours:
            t = day.replace(hour=h, minute=minute)
            if start <= t <= end:
                out.append(t)
        day += timedelta(days=1)
    return out


def jobs_in(window_start: datetime, window_end: datetime, tailored: Path = TAILORED) -> int:
    n = 0
    for f in tailored.glob("*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if d.get("tailorer") != "routine":
            continue
        try:
            t = datetime.fromisoformat((d.get("tailored_at") or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        if window_start <= t <= window_end:
            n += 1
    return n


def deltas(rows: list[dict], minute: int, hours: list[int], run_minutes: int = RUN_MINUTES,
           tailored: Path = TAILORED) -> list[dict]:
    if len(rows) < 2:
        return []
    first = datetime.fromtimestamp(rows[0]["ts"], timezone.utc)
    last = datetime.fromtimestamp(rows[-1]["ts"], timezone.utc)
    out = []
    for fire in fires_between(first, last, minute, hours):
        end = fire + timedelta(minutes=run_minutes)
        before = [r for r in rows if r["ts"] <= fire.timestamp()]
        after = [r for r in rows if r["ts"] >= end.timestamp()]
        if not before or not after:
            continue
        b, a = before[-1], after[0]
        inside = [r for r in rows if fire.timestamp() < r["ts"] < end.timestamp()]
        reset = b.get("seven_day_resets_at") != a.get("seven_day_resets_at")
        d7 = (a.get("seven_day") - b.get("seven_day")) if (a.get("seven_day") is not None
                                                             and b.get("seven_day") is not None) else None
        f5 = (a.get("five_hour") - b.get("five_hour")) if (a.get("five_hour") is not None
                                                            and b.get("five_hour") is not None) else None
        jobs = jobs_in(fire, end, tailored)
        out.append({
            "fire": fire, "before_at": datetime.fromtimestamp(b["ts"], timezone.utc),
            "after_at": datetime.fromtimestamp(a["ts"], timezone.utc),
            "seven_day_delta": d7, "five_hour_delta": f5, "jobs": jobs,
            "clean": not inside and not reset,
            "note": "reset in between" if reset else ("local session active" if inside else ""),
        })
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--days", type=float, default=None, help="only fires in the last N days")
    ap.add_argument("--log", type=Path, default=LOG)
    ap.add_argument("--run-minutes", type=int, default=RUN_MINUTES)
    args = ap.parse_args(argv)
    rows = read_log(args.log)
    if len(rows) < 2:
        print(f"{args.log}: fewer than two readings; open a Claude Code session before and after a fire.")
        return 0
    minute, hours = cron_hours()
    result = deltas(rows, minute, hours, args.run_minutes)
    if args.days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
        result = [r for r in result if r["fire"] >= cutoff]
    if not result:
        print("no fire is bracketed by readings yet")
        return 0
    print(f"{'fire (UTC)':17} {'7d before':>10} {'7d after':>9} {'delta':>6} {'jobs':>4} {'per job':>8}  note")
    clean = []
    for r in result:
        d7 = r["seven_day_delta"]
        per = (d7 / r["jobs"]) if (d7 is not None and r["jobs"]) else None
        print(f"{r['fire']:%m-%d %H:%M}        "
              f"{r['before_at']:%H:%M}      {r['after_at']:%H:%M}   "
              f"{'' if d7 is None else f'{d7:+.1f}%':>6} {r['jobs']:>4} "
              f"{'' if per is None else f'{per:.2f}%':>8}  {r['note']}")
        if r["clean"] and per is not None:
            clean.append(per)
    if clean:
        print(f"\nclean fires: {len(clean)}; mean per-job cost {sum(clean) / len(clean):.2f}% of the weekly bar")
    else:
        print("\nno clean fire yet: every bracketed run had a local session active or a reset in between")
    return 0


if __name__ == "__main__":
    sys.exit(main())
