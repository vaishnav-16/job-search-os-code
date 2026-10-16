"""In-fire decay: do resumes tailored later in one routine fire review lower?

WHY. The judge-queue routine tailors up to 12 jobs per fire in one Opus thread.
The 10-09 audit found that, at equal scorer score, the jobs it reached late in a
fire reviewed lower than the first few (scored 8: floor 7.0 for jobs 1-4 vs 6.6
for 5-12; scored 10: 7.4 vs 7.1). W4 in docs/improvement-plan-2026-10.md
restructures the routine to remove that, and its "done when" is two days of
fires with no in-fire decay at equal scorer score. This script is that
instrument, so the check is a command and not a one-off notebook.

How a row is built (read only; nothing here writes):
  - tailored/<id>.json with tailorer == "routine". Local tailors are excluded.
  - Review scores: the sidecar's `scores` {recruiter, hm}; else the last
    `SCORES: ats NN / recruiter N / hm N` line of applications/<folder>/review.md.
    Floor = min(recruiter, hm). A row without both stays in the fire (it still
    took a position) but is left out of every mean.
  - Scorer score and lane: scores/<id>.json (`score`, lane = note up to the
    first ";"); else the committed pipeline/jobs.sqlite, opened read only
    (`jobs.score`, lane from `jobs.notes`), because the fold removes most score
    sidecars. Lane `digest-stretch` is bucketed as "stretch"; a note marked
    "promoted via dashboard" as "promoted" (Vaish's hand picks, mostly 5-year
    stretches: at their scorer score they read as decay on 10-13); no scorer
    score is "unscored" (hand imports such as the new-grad list).
  - Fire: the latest routine cron time (usage_deltas.cron_hours /
    fires_between) at or before tailored_at, within FIRE_HOURS. Anything else
    is counted as unattributed.
  - Position: 1-based order by (tailored_at, id) inside the fire. Since 10-10
    the routine overlaps jobs (it drafts the next while reviewers run), so
    tailored_at order approximates the order work started, not exactly.
  - Family, from the title (queue/<id>.json, else jobs.title, else the folder
    slug), first match wins: FDE "forward deployed"; PO "product owner";
    PM "product manager", "product management", "product lead"; else other.

--since/--until select fires by their start date (UTC), so a fire is never
split.

    python pipeline/fire_decay.py                       # fires of the last 7 days
    python pipeline/fire_decay.py --since 2026-09-25 --json out.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import db  # noqa: E402
from usage_deltas import cron_hours, fires_between  # noqa: E402

ROOT = HERE.parent
DB_PATH = HERE / "jobs.sqlite"
FIRE_HOURS = 6        # a fire runs well under this; the next cron is >= 6.5h later
EARLY = 4             # positions 1..EARLY are "early", the rest "late"

SCORES_RE = re.compile(r"SCORES:\s*ats\s*(\d+)\s*/\s*recruiter\s*(\d+)\s*/\s*hm\s*(\d+)", re.I)
FAMILIES = [
    ("FDE", re.compile(r"forward[\s-]*deployed", re.I)),
    ("PO", re.compile(r"product\s+owner", re.I)),
    ("PM", re.compile(r"product\s+(manager|management|lead)", re.I)),
]


def family(title: str) -> str:
    for name, rx in FAMILIES:
        if rx.search(title or ""):
            return name
    return "other"


def _read_json(path: Path) -> dict | None:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def _parse_ts(s: str) -> datetime | None:
    try:
        t = datetime.fromisoformat((s or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def review_scores(side: dict, root: Path) -> tuple[int | None, int | None]:
    """(recruiter, hm) from the sidecar, else the last SCORES line of review.md."""
    sc = side.get("scores")
    if isinstance(sc, dict) and isinstance(sc.get("recruiter"), int) and isinstance(sc.get("hm"), int):
        return sc["recruiter"], sc["hm"]
    folder = side.get("folder")
    if folder:
        try:
            text = (root / folder / "review.md").read_text(encoding="utf-8")
        except OSError:
            return None, None
        hits = SCORES_RE.findall(text)
        if hits:
            return int(hits[-1][1]), int(hits[-1][2])
    return None, None


def fire_of(t: datetime, minute: int, hours: list[int]) -> datetime | None:
    fires = fires_between(t - timedelta(hours=FIRE_HOURS), t, minute, hours)
    return fires[-1] if fires else None


def collect(root: Path, minute: int, hours: list[int], db_path: Path | None = None) -> list[dict]:
    """One dict per routine tailor, with fire and position (fire None = unattributed)."""
    conn = None
    if db_path is not None and Path(db_path).exists():
        conn = db.connect_readonly(db_path)
    rows = []
    for f in sorted((root / "tailored").glob("*.json")):
        side = _read_json(f)
        if not side or side.get("tailorer") != "routine":
            continue
        t = _parse_ts(side.get("tailored_at"))
        if t is None:
            continue
        jid = side.get("id") or f.stem
        rec, hm = review_scores(side, root)
        score, note, title = None, "", ""
        sc = _read_json(root / "scores" / f"{jid}.json")
        if sc and isinstance(sc.get("score"), int):
            score, note = sc["score"], sc.get("note") or ""
        dbrow = conn.execute("SELECT score, notes, title FROM jobs WHERE id = ?", (jid,)).fetchone() if conn else None
        if score is None and dbrow is not None and dbrow["score"] is not None:
            score, note = dbrow["score"], dbrow["notes"] or ""
        lane = note.split(";")[0].strip()
        q = _read_json(root / "queue" / f"{jid}.json")
        title = (q or {}).get("title") or (dbrow["title"] if dbrow is not None else "") \
            or Path(side.get("folder") or "").name.replace("-", " ")
        bucket = ("stretch" if lane == "digest-stretch" else "promoted" if "promoted via dashboard" in note
                  else score if score is not None else "unscored")
        rows.append({
            "id": jid, "tailored_at": t, "fire": fire_of(t, minute, hours),
            "recruiter": rec, "hm": hm,
            "floor": min(rec, hm) if rec is not None and hm is not None else None,
            "score": score, "lane": lane, "bucket": bucket,
            "title": title, "family": family(title),
        })
    if conn is not None:
        conn.close()
    by_fire: dict[datetime, list[dict]] = {}
    for r in rows:
        r["position"] = None
        if r["fire"] is not None:
            by_fire.setdefault(r["fire"], []).append(r)
    for group in by_fire.values():
        group.sort(key=lambda r: (r["tailored_at"], r["id"]))
        for i, r in enumerate(group, 1):
            r["position"] = i
    return rows


def _stats(rows: list[dict]) -> dict:
    scored = [r for r in rows if r["floor"] is not None]
    n = len(scored)
    return {
        "n": n,
        "floor": round(sum(r["floor"] for r in scored) / n, 2) if n else None,
        "both8": round(sum(1 for r in scored if r["recruiter"] >= 8 and r["hm"] >= 8) / n, 3) if n else None,
    }


def _bucket_order(b) -> tuple:
    return (0, -b) if isinstance(b, int) else (1, {"stretch": 0, "promoted": 1}.get(b, 2))


def summarize(rows: list[dict], since: date, until: date) -> dict:
    """Stats for routine tailors whose fire started in [since, until] (UTC dates)."""
    picked = [r for r in rows if r["fire"] is not None and since <= r["fire"].date() <= until]
    by_score = {}
    for b in sorted({r["bucket"] for r in picked} - {"unscored"}, key=_bucket_order):
        inb = [r for r in picked if r["bucket"] == b]
        by_score[str(b)] = {
            "early": _stats([r for r in inb if r["position"] <= EARLY]),
            "late": _stats([r for r in inb if r["position"] > EARLY]),
        }
    by_score["all"] = {
        "early": _stats([r for r in picked if r["position"] <= EARLY]),
        "late": _stats([r for r in picked if r["position"] > EARLY]),
    }
    by_family = {fam: _stats([r for r in picked if r["family"] == fam])
                 for fam in ("PM", "PO", "FDE", "other")}
    by_family["all"] = _stats(picked)
    lo = datetime.combine(since, datetime.min.time(), timezone.utc)
    hi = datetime.combine(until + timedelta(days=1), datetime.min.time(), timezone.utc)
    return {
        "since": since.isoformat(), "until": until.isoformat(),
        "fires": len({r["fire"] for r in picked}),
        "resumes": len(picked),
        "without_review_scores": sum(1 for r in picked if r["floor"] is None),
        "unscored": sum(1 for r in picked if r["bucket"] == "unscored"),
        "unattributed": sum(1 for r in rows if r["fire"] is None and lo <= r["tailored_at"] < hi),
        "by_score": by_score, "by_family": by_family,
    }


def _cell(s: dict) -> str:
    return f"{s['n']:>3}  {'-' if s['floor'] is None else format(s['floor'], '.1f'):>5}"


def render(s: dict) -> str:
    out = [f"in-fire decay, routine tailors, fires {s['since']} .. {s['until']} (UTC)",
           f"{s['fires']} fires, {s['resumes']} resumes ({s['without_review_scores']} without review "
           f"scores, {s['unscored']} without a scorer score, {s['unattributed']} outside any fire)",
           "",
           f"{'scorer score':<14}{'jobs 1-4':>11}{'jobs 5-12':>13}",
           f"{'':<14}{'n  floor':>11}{'n  floor':>13}"]
    for b, v in s["by_score"].items():
        out.append(f"{b:<14}{_cell(v['early']):>11}{_cell(v['late']):>13}")
    out += ["", f"{'family':<14}{'n':>4}{'floor':>7}{'both 8+':>9}"]
    for fam, v in s["by_family"].items():
        floor = "-" if v["floor"] is None else f"{v['floor']:.1f}"
        both = "-" if v["both8"] is None else f"{v['both8']:.0%}"
        out.append(f"{fam:<14}{v['n']:>4}{floor:>7}{both:>9}")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    today = datetime.now(timezone.utc).date()
    ap.add_argument("--since", type=date.fromisoformat, default=today - timedelta(days=7),
                    help="first fire date, UTC (default: 7 days ago)")
    ap.add_argument("--until", type=date.fromisoformat, default=today,
                    help="last fire date, UTC, inclusive (default: today)")
    ap.add_argument("--json", type=Path, default=None, help="also write the summary here")
    args = ap.parse_args(argv)
    minute, hours = cron_hours()
    s = summarize(collect(ROOT, minute, hours, DB_PATH), args.since, args.until)
    print(render(s))
    if args.json:
        args.json.write_text(json.dumps(s, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
