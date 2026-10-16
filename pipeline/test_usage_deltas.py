"""Self-executing checks for usage_deltas.py (no pytest)."""
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import usage_deltas as ud  # noqa: E402

failures = 0


def check(name, cond, detail=""):
    global failures
    print(f"  {'ok ' if cond else 'FAIL'} {name}{'' if cond or not detail else ': ' + detail}")
    failures += 0 if cond else 1


def ts(s):
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()


with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    tailored = tmp / "tailored"
    tailored.mkdir()
    # two routine jobs inside the 08:30 window, one local job, one outside
    for i, (when, who) in enumerate([("2026-09-23T08:41:00", "routine"), ("2026-09-23T09:10:00", "routine"),
                                     ("2026-09-23T09:00:00", "local"), ("2026-09-23T13:00:00", "routine")]):
        (tailored / f"{i}.json").write_text(json.dumps({"tailored_at": when + "Z", "tailorer": who}))
    log = tmp / "log.jsonl"
    rows = [
        {"ts": ts("2026-09-23T07:50:00"), "five_hour": 10, "seven_day": 40.0, "seven_day_resets_at": 1},
        {"ts": ts("2026-09-23T10:05:00"), "five_hour": 14, "seven_day": 42.5, "seven_day_resets_at": 1},
        # 14:30 fire: a reading inside the run window marks it mixed
        {"ts": ts("2026-09-23T14:00:00"), "five_hour": 20, "seven_day": 45.0, "seven_day_resets_at": 1},
        {"ts": ts("2026-09-23T15:00:00"), "five_hour": 22, "seven_day": 46.0, "seven_day_resets_at": 1},
        {"ts": ts("2026-09-23T16:00:00"), "five_hour": 24, "seven_day": 47.0, "seven_day_resets_at": 1},
        # 21:30 fire: the weekly reset happened between the readings
        {"ts": ts("2026-09-23T21:00:00"), "five_hour": 30, "seven_day": 50.0, "seven_day_resets_at": 1},
        {"ts": ts("2026-09-23T23:00:00"), "five_hour": 31, "seven_day": 2.0, "seven_day_resets_at": 2},
    ]
    log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    got = ud.deltas(ud.read_log(log), 30, [8, 14, 21], 70, tailored)
    check("three fires bracketed", len(got) == 3, str([g["fire"] for g in got]))
    a, b, c = got
    check("08:30 fire is clean with +2.5% and 2 routine jobs",
          a["clean"] and abs(a["seven_day_delta"] - 2.5) < 1e-9 and a["jobs"] == 2, str(a))
    check("the local job inside the window is not counted", a["jobs"] == 2)
    check("14:30 fire is marked mixed (local session active)",
          not b["clean"] and b["note"] == "local session active", str(b))
    check("21:30 fire is marked reset in between", not c["clean"] and c["note"] == "reset in between", str(c))

    check("cron parsed from the routine file", ud.cron_hours() == (30, [8, 17, 22]), str(ud.cron_hours()))
    check("fewer than two readings is handled", ud.deltas([], 30, [8], 70, tailored) == [])

print(f"\n{'all usage_deltas tests pass' if not failures else f'{failures} FAILED'}")
sys.exit(1 if failures else 0)
