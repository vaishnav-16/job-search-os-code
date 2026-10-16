"""The pipeline health check as one command (read-only, no LLM).

    python pipeline/audit.py              # last 14 days
    python pipeline/audit.py --days 3     # e.g. the baseline after a change

Prints what the 2026-09-28 manual audit measured, so the next check is a diff
of two outputs instead of an afternoon of queries:
  - detection cadence (the DB's own runs table; GitHub's view via `gh` if present)
    and lag from posting to detection
  - auto-tailor jobs that expired untailored, by reason
  - company share per bucket (detected, auto-tailor, tailored), role aliases applied
  - role families per bucket
  - fold cadence, applications, and resumes swept stale before they folded
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import statistics
import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import db  # noqa: E402
import worklist  # noqa: E402

ROOT = HERE.parent

FAMILIES = [
    ("fde", re.compile(r"forward[- ]deployed")),
    ("devrel", re.compile(r"developer (relations|advocate|evangelist)|devrel")),
    ("program", re.compile(r"program manager|\btpm\b")),
    ("product owner", re.compile(r"product owner")),
    ("product analyst/ops", re.compile(r"product (analyst|operations|ops)")),
    ("pm", re.compile(r"product manag|\bpm\b|\bapm\b|product lead")),
]


def family(title: str) -> str:
    t = (title or "").lower()
    for name, rx in FAMILIES:
        if rx.search(t):
            return name
    return "other"


def _ts(value):
    return worklist._parse_ts(value)


def share(counter: Counter, top: int = 8) -> str:
    total = sum(counter.values())
    if not total:
        return "(none)"
    return ", ".join(f"{k or '?'} {v / total:.1%}" for k, v in counter.most_common(top))


def detection(conn, since: datetime, days: int) -> list[str]:
    out = ["Detection"]
    per_day = Counter()
    for (ts,) in conn.execute("SELECT ts FROM runs WHERE kind='layer1' AND ts >= ?",
                              (since.strftime("%Y-%m-%dT%H:%M:%SZ"),)):
        per_day[ts[:10]] += 1
    days_list = [(since + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days + 1)]
    out.append("  layer1 runs/day (DB): " + " ".join(f"{d[5:]}:{per_day[d]}" for d in days_list))
    out.append(f"  average {sum(per_day.values()) / max(1, days):.1f}/day (target 10+)")

    if shutil.which("gh"):
        try:
            raw = subprocess.run(
                ["gh", "run", "list", "--workflow", "detect.yml", "--limit", "400",
                 "--json", "createdAt,event"], capture_output=True, text=True, timeout=60,
                cwd=ROOT).stdout
            runs = [r for r in json.loads(raw or "[]") if _ts(r["createdAt"]) >= since]
            ev = Counter(r["event"] for r in runs)
            out.append(f"  GitHub detect runs (incl. gated skips): {len(runs)} "
                       f"({', '.join(f'{k} {v}' for k, v in ev.most_common())})")
        except (subprocess.SubprocessError, ValueError, KeyError) as e:
            out.append(f"  GitHub view unavailable: {e}")
    else:
        out.append("  GitHub view skipped: gh not on PATH")

    exact, day = [], []
    for posted, seen in conn.execute("SELECT posted_at, first_seen FROM jobs WHERE first_seen >= ?",
                                     (since.strftime("%Y-%m-%dT%H:%M:%SZ"),)):
        p, s = _ts(posted), _ts(seen)
        if p is None or s is None:
            continue
        if len((posted or "").strip()) == 10:
            day.append((s - p).total_seconds() / 3600 - 12)    # midday of the posting date
        else:
            exact.append((s - p).total_seconds() / 3600)
    for label, xs in (("exact timestamps", exact), ("date-only (midday)", day)):
        xs = [x for x in xs if x >= -24]
        if xs:
            xs.sort()
            out.append(f"  lag posting->detection, {label}: n={len(xs)} "
                       f"median {statistics.median(xs):.1f}h, p90 {xs[int(len(xs) * .9)]:.1f}h")
    return out


def expired(conn, repo, since: datetime) -> list[str]:
    worked = db.worked_role_keys(conn) | {db.role_key(c, t) for _, c, t, _ in repo.briefs}
    reasons = Counter()
    examples = {}
    rows = conn.execute(
        "SELECT company, title, notes, first_seen, score FROM jobs "
        "WHERE status='stale_skip' AND first_seen >= ?", (since.strftime("%Y-%m-%dT%H:%M:%SZ"),))
    judged = 0
    for company, title, notes, seen, score in rows:
        lane = worklist.lane_of(notes)
        if lane == "skip" and (score or 0) >= 7:
            judged += 1
        if lane != "auto-tailor":
            continue
        nt = db.norm_title(title)
        seen_dt = _ts(seen)
        if db.role_key(company, title) in worked:
            why = "worked duplicate"
        elif seen_dt and any(db.norm_title(t) == nt and when is not None
                             and timedelta(0) <= seen_dt - when <= timedelta(days=30)
                             for _, _, t, when in repo.briefs):
            why = ("title block, generic (fixed 09-28)"
                   if len(nt.split()) < worklist.TITLE_GUARD_MIN_WORDS else "title block, 4+ words")
        else:
            why = "capacity (never reached)"
        reasons[why] += 1
        examples.setdefault(why, []).append(f"{company} {score}")
    out = [f"Auto-tailor jobs expired untailored: {sum(reasons.values())}"]
    for why, n in reasons.most_common():
        out.append(f"  {why:<36} {n:>4}   e.g. {', '.join(examples[why][:4])}")
    out.append(f"  (also laned skip at 7+, judgment or a years gate: {judged})")
    return out


def flow(conn, repo, since: datetime, now: datetime) -> list[str]:
    """Fold cadence and what happens to finished resumes after they ship.

    Added 10-06: a 72h fold gap let detect.yml's age sweep take 32 reviewed
    resumes before they reached the DB, and nothing measured either half.
    """
    out = ["Fold and outcomes"]
    raw = subprocess.run(["git", "log", f"--since={since:%Y-%m-%dT%H:%M:%SZ}",
                          "--format=%cI %an %s", "--grep=^sync:"],
                         capture_output=True, text=True, cwd=ROOT).stdout
    folds = sorted((_ts(line.split(" ", 1)[0]), line.split(" ")[1])
                   for line in raw.splitlines() if line.strip())
    if folds:
        times = [t for t, _ in folds] + [now]
        gap = max((b - a for a, b in zip(times, times[1:])), default=timedelta(0))
        who = Counter(a for _, a in folds)
        out.append(f"  folds: {len(folds)} ({', '.join(f'{k} {v}' for k, v in who.most_common())}); "
                   f"last {(now - folds[-1][0]).total_seconds() / 3600:.1f}h ago; "
                   f"longest gap {gap.total_seconds() / 3600:.1f}h")
    else:
        out.append("  folds: none in the window")

    per_day = Counter()
    for status, at in conn.execute(
            "SELECT status, at FROM status_history WHERE at >= ? AND status IN "
            "('applied','interview','offer','rejected')",
            (since.strftime("%Y-%m-%dT%H:%M:%SZ"),)):
        per_day[(status, at[:10])] += 1
    applied = sum(v for (s, _), v in per_day.items() if s == "applied")
    out.append(f"  applied {applied} ({applied / max(1, (now - since).days):.1f}/day); "
               + ", ".join(f"{s} {sum(v for (x, _), v in per_day.items() if x == s)}"
                           for s in ("interview", "offer", "rejected")))

    # Split the way the dashboard splits (10-15): it shows a tailored resume only
    # while the role was posted or found in the last 72h, so "unmarked" alone
    # read as a 159-resume backlog when the board held 18.
    ages = {jid: (posted, seen) for jid, posted, seen in conn.execute(
        "SELECT id, posted_at, first_seen FROM jobs WHERE status IN ('tailored', 'needs_review')")}
    lost, on_board, archived = 0, 0, 0
    for jid, rec in repo.tailored.items():
        ts = _ts(rec.get("tailored_at"))
        if ts is None or ts < since:
            continue
        row = repo.db_rows.get(jid)
        status = row[2] if row else None
        if status == "stale_skip":
            lost += 1
        elif status in ("tailored", "needs_review"):
            posted, seen = ages.get(jid, ("", ""))
            hours = [h for h in (db.job_age(posted, now=now)[0] if posted else None,
                                 db.job_age("", seen, now=now)[0] if seen else None)
                     if h is not None]
            if not hours or min(hours) <= 72:
                on_board += 1
            else:
                archived += 1
    out.append(f"  resumes shipped in window, unmarked: {on_board} on the board (role under 72h), "
               f"{archived} past 72h in its archive; "
               f"{lost} swept stale (should be 0 since the 10-06 fold fix)")
    return out


def buckets(conn, repo, since: datetime) -> tuple[dict, dict]:
    iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    detected, auto, tailored = Counter(), Counter(), Counter()
    fam = {"detected": Counter(), "auto-tailor": Counter(), "tailored": Counter()}
    titles = {}
    for jid, company, title, notes, seen in conn.execute(
            "SELECT id, company, title, notes, first_seen FROM jobs"):
        titles[jid] = (company, title)
        if seen < iso:
            continue
        key = db.role_company_key(company)
        detected[key] += 1
        fam["detected"][family(title)] += 1
        if worklist.lane_of(notes) == "auto-tailor":
            auto[key] += 1
            fam["auto-tailor"][family(title)] += 1
    for jid, rec in repo.tailored.items():
        ts = _ts(rec.get("tailored_at"))
        if ts is None or ts < since:
            continue
        company, title = titles.get(jid, (repo.company_of(jid), ""))
        tailored[db.role_company_key(company)] += 1
        fam["tailored"][family(title)] += 1
    return ({"detected": detected, "auto-tailor": auto, "tailored": tailored}, fam)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=14)
    args = ap.parse_args(argv)
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=args.days)).replace(hour=0, minute=0, second=0, microsecond=0)
    conn = db.connect_readonly()
    repo = worklist.Repo(ROOT, conn)

    lines = [f"Pipeline audit, {since:%Y-%m-%d} to {now:%Y-%m-%d %H:%M}Z", ""]
    lines += detection(conn, since, args.days) + [""]
    lines += expired(conn, repo, since) + [""]
    lines += flow(conn, repo, since, now) + [""]
    companies, fams = buckets(conn, repo, since)
    lines.append("Company share (role aliases applied)")
    for name, c in companies.items():
        lines.append(f"  {name:<12} n={sum(c.values()):<5} {share(c)}")
    lines += ["", "Role families"]
    for name, c in fams.items():
        lines.append(f"  {name:<12} n={sum(c.values()):<5} {share(c, top=7)}")
    wl = worklist.tailor_list(repo, now=now)
    lines += ["", f"Tailor list now: {len(wl['selected'])} selected, {len(wl['deferred'])} deferred, "
                  f"{len(wl['blocked'])} blocked"]
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
