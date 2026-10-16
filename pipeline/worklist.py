"""The judge-queue work lists, computed in code instead of re-derived in prose.

    python pipeline/worklist.py score               # JSON: which queue files to score
    python pipeline/worklist.py siblings            # copy scores onto duplicate postings
    python pipeline/worklist.py tailor [--cap 15]   # JSON: selected, deferred, blocked

WHY THIS EXISTS. Until 2026-09-28 the routine prompt described STEP 3 and STEP 4
in a paragraph each and the Opus thread re-derived the lists by hand every fire.
Two bugs lived in that prose for a week, both flagged in the routine's own notes
and never fixed: the 30-day title guard matched generic titles across companies
("Product Manager" matched 113 briefs) and silently hid four 9-point jobs until
they expired, and role_key was never checked against brief headings, which let
one role be tailored twice. Rules in code get tests; rules in prose get
paraphrased.

READ-ONLY apart from `siblings`, which writes score sidecars (the same kind of
file the routine already writes). The DB is opened with connect_readonly only:
it has exactly two writers and this is never one of them.

THE RULES
score:    queue files with no score sidecar, no tailor sidecar and no application
          folder carrying the id, not provably stale. Collapsed by role_key: one
          representative per role is scored and `siblings` copies its score onto
          the rest. A role that already has a scored posting is not scored again.
          Ordered by score_priority (in-band years first, then 5-year asks, then
          6+ or a Senior title with no years), newest first within each, capped
          at 120: since 10-08 LinkedIn supplies more than three fires can score,
          and the cap should drop the jobs least likely to be tailored.
tailor:   lane auto-tailor (or promoted), no tailor sidecar, no folder carrying the
          id, role not worked (DB statuses AND brief headings), no 4+ word title
          match in a brief from the last 30 days, not provably stale. Collapsed by
          role_key keeping the best. Ordered promoted, score, tier, recency. Then
          the company cap: 2 per company per fire and 4 per rolling 7 days
          (promoted jobs are exempt but still count). Everything that qualifies
          and is not selected comes back as `deferred` with its reason, and the
          next call re-derives it, so nothing is lost between fires.
          Stretch (2026-10-08, Vaish's call): a job the scorer laned
          "digest-stretch" (5 years asked, strong fit, no gate) is ranked after
          every in-band job and fills only the slots they leave empty, under the
          same guards and company cap. Picks carry "stretch": true.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import db  # noqa: E402
import ingest_scores  # noqa: E402

ROOT = HERE.parent
SCORE_CAP = 120
TAILOR_CAP = 15
COMPANY_PER_FIRE = 2
COMPANY_PER_WEEK = 4
TITLE_GUARD_DAYS = 30
TITLE_GUARD_MIN_WORDS = 4     # below this a title is generic ("product manager 2")
JD_MIN_CHARS = 500            # a shorter JD means the tailor works blind (W3b)
TIER_ORDER = {"now": 0, "soon": 1, "nodate": 2}

_HEX_ID = re.compile(r"\b[0-9a-f]{40}\b")
_FOLDER_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})$")
PROMOTE_MARK = "promoted via dashboard"
# decisions.MARK_ACTIONS: a pending one of these takes the job off both lists.
CLOSING_ACTIONS = ("applied", "rejected", "skipped", "interview", "offer")


def lane_of(note: str) -> str:
    return (note or "").split(";")[0].strip()


def _read_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _parse_ts(value) -> datetime | None:
    dt = db.parse_posted_at(value or "")
    if dt is not None and dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------- repo state

class Repo:
    """Everything the lists are computed from, read once."""

    def __init__(self, root: Path = ROOT, conn=None):
        self.root = Path(root)
        self.conn = conn
        self.queue = {}                        # id -> queue record
        for p in sorted((self.root / "queue").glob("*.json")):
            rec = _read_json(p)
            if isinstance(rec, dict):
                rec.setdefault("id", p.stem)
                self.queue[p.stem] = rec
        self.scores = {}                       # id -> score sidecar
        for p in sorted((self.root / "scores").glob("*.json")):
            rec = _read_json(p)
            if isinstance(rec, dict) and rec.get("id") == p.stem and "score" in rec:
                self.scores[p.stem] = rec
        self.tailored = {}                     # id -> tailor sidecar
        for p in sorted((self.root / "tailored").glob("*.json")):
            rec = _read_json(p)
            if isinstance(rec, dict):
                self.tailored[rec.get("id") or p.stem] = rec
        self.db_rows = {}                      # id -> (company, title, status, notes)
        self.worked = set()
        if conn is not None:
            for r in conn.execute("SELECT id, company, title, status, notes FROM jobs"):
                self.db_rows[r[0]] = (r[1], r[2], r[3], r[4] or "")
            self.worked = db.worked_role_keys(conn)
        # W7 (2026-10-13): a dashboard click no longer starts a fold, so a decision
        # can sit in pending.json for hours. The newest entry per id wins (a cancel
        # retracts). A pending close keeps a job off both lists, or a skip clicked
        # at 14:00 would still be tailored at 15:00.
        self.pending_promotes = set()
        self.pending_closed = set()
        pend = _read_json(self.root / "decisions" / "pending.json")
        latest = {}
        for e in pend if isinstance(pend, list) else []:
            if isinstance(e, dict) and e.get("id") and isinstance(e.get("action"), str):
                if str(e.get("at", "")) >= str(latest.get(e["id"], {}).get("at", "")):
                    latest[e["id"]] = e
        for jid, e in latest.items():
            if e["action"] == "promote":
                self.pending_promotes.add(jid)
            elif e["action"] in CLOSING_ACTIONS:
                self.pending_closed.add(jid)
        self._load_folders()

    def _load_folders(self):
        """Ids and headings from every applications/*/brief.md (and score.md)."""
        self.folder_ids = set()
        self.briefs = []                       # (folder name, company, title, date)
        folder_dates = {}
        for rec in self.tailored.values():
            name = Path(rec.get("folder") or "").name
            ts = _parse_ts(rec.get("tailored_at"))
            if name and ts:
                folder_dates[name] = ts
        apps = self.root / "applications"
        if not apps.is_dir():
            return
        for d in sorted(apps.iterdir()):
            if not d.is_dir():
                continue
            heading = ""
            for fname in ("brief.md", "score.md"):
                f = d / fname
                if not f.is_file():
                    continue
                text = f.read_text(encoding="utf-8-sig", errors="replace")
                self.folder_ids.update(_HEX_ID.findall(text))
                if fname == "brief.md":
                    first = text.lstrip().split("\n", 1)[0]
                    heading = first.lstrip("#").strip() if first.startswith("#") else ""
            if " - " not in heading:
                continue
            company, title = heading.split(" - ", 1)   # titles carry dashes, companies rarely
            when = folder_dates.get(d.name)
            if when is None:
                m = _FOLDER_DATE.search(d.name)
                when = (datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)
                        if m else None)
            self.briefs.append((d.name, company.strip(), title.strip(), when))

    def company_of(self, jid: str) -> str:
        if jid in self.db_rows:
            return self.db_rows[jid][0]
        if jid in self.queue:
            return self.queue[jid].get("company", "")
        folder = Path((self.tailored.get(jid) or {}).get("folder") or "").name
        for name, company, _, _ in self.briefs:
            if name == folder:
                return company
        return ""

    def is_promoted(self, jid: str) -> bool:
        if jid in self.pending_promotes:
            return True
        side = (self.scores.get(jid) or {}).get("note", "")
        if PROMOTE_MARK in side:
            return True
        row = self.db_rows.get(jid)
        # sync writes every promote into the DB with its marker, and the routine's
        # branch overlay can put the old digest sidecar back on top of it. A skip
        # sidecar is the routine's own later judgment: until sync folds it, the DB
        # still says auto-tailor, and reading that as a promotion would bring the
        # job back sorted first and cap-exempt.
        return bool(row and lane_of(row[3]) == "auto-tailor" and PROMOTE_MARK in row[3]
                    and lane_of(side) != "skip")

    def stale(self, rec: dict, now=None) -> bool:
        return db.is_provably_stale(rec.get("posted_at", ""), now=now,
                                    first_seen=rec.get("first_seen", ""))


# ---------------------------------------------------------------- score

def score_list(repo: Repo, cap: int = SCORE_CAP, now=None) -> dict:
    excluded = Counter()
    scored_roles = {db.role_key(repo.queue[j].get("company"), repo.queue[j].get("title"))
                    for j in repo.scores if j in repo.queue}
    groups = defaultdict(list)
    for jid, rec in repo.queue.items():
        if jid in repo.scores:
            continue
        if jid in repo.pending_closed:
            excluded["pending decision"] += 1
            continue
        if jid in repo.tailored or jid in repo.folder_ids:
            excluded["already tailored"] += 1
            continue
        if repo.stale(rec, now):
            excluded["stale"] += 1
            continue
        groups[db.role_key(rec.get("company"), rec.get("title"))].append(rec)

    reps, fill = [], 0
    for key, recs in groups.items():
        if key in scored_roles:
            fill += len(recs)
            continue
        recs.sort(key=_recency_key)
        reps.append((recs[0], len(recs) - 1))
    reps.sort(key=lambda x: (score_priority(x[0]), _recency_key(x[0])))
    return {
        "score": [{"id": r["id"], "queue": f"queue/{r['id']}.json",
                   "company": r.get("company", ""), "title": r.get("title", ""),
                   "duplicates": n} for r, n in reps[:cap]],
        "over_cap": max(0, len(reps) - cap),
        "fill_from_siblings": fill,
        "excluded": dict(excluded),
    }


_SENIOR = re.compile(r"(?i)\b(senior|sr\.?|lead|iii|iv)\b|\bmanager,\s*product")


def score_priority(rec: dict) -> int:
    """0 = likely in band, 1 = a possible 5-year stretch, 2 = digest at best.

    From the JD's own years figure (filter.min_required_years, the same reader
    the 7+ gate uses) and, with no figure, the title: a Senior or Lead title
    with no years stated scores (b)=1 and is never a stretch. Imported lazily:
    filter.py pulls in scrape's network dependencies, which the cloud routine's
    environment need not have, and without it the title alone decides.
    """
    years = None
    try:
        import filter as hardfilter
        years = hardfilter.min_required_years(rec.get("jd_markdown") or "")
    except Exception:
        pass
    if years is not None:
        return 0 if years <= 4 else 1 if years == 5 else 2
    return 2 if _SENIOR.search(rec.get("title") or "") else 0


def _recency_key(rec: dict):
    """Newest first; a posting with no date sorts after every dated one."""
    dt = _parse_ts(rec.get("posted_at")) or _parse_ts(rec.get("first_seen"))
    return (0, -dt.timestamp()) if dt else (1, 0)


def fill_siblings(repo: Repo, scorer: str = "routine", apply: bool = True,
                  now=None) -> list:
    """Give every unscored duplicate posting its role's score. Returns [(id, source id)]."""
    by_role = defaultdict(list)
    for jid, side in repo.scores.items():
        rec = repo.queue.get(jid)
        if rec:
            by_role[db.role_key(rec.get("company"), rec.get("title"))].append(side)
    done = []
    for jid, rec in repo.queue.items():
        if jid in repo.scores or jid in repo.tailored or repo.stale(rec, now):
            continue
        sources = by_role.get(db.role_key(rec.get("company"), rec.get("title")))
        if not sources:
            continue
        src = max(sources, key=lambda s: (int(s["score"]), s.get("scored_at", "")))
        note = f"{lane_of(src.get('note'))}; duplicate role of {src['id']}"
        done.append((jid, src["id"]))
        if apply:
            ingest_scores.write_sidecar(jid, int(src["score"]), note, scorer=scorer,
                                        model=src.get("model", "sonnet"))
    return done


# ---------------------------------------------------------------- tailor

DUP_OF = re.compile(r"duplicate role of ([0-9a-f]{40})")


def effective_lane(repo: Repo, side: dict) -> str:
    """A sibling's lane is its source's CURRENT lane, not the copy siblings made.

    The routine re-lanes a job skip on reading the JD after `siblings` already
    copied auto-tailor onto its duplicate postings; without this the duplicate
    resurfaced next fire (Aristocrat, 10-06).
    """
    note = side.get("note") or ""
    m = DUP_OF.search(note)
    src = repo.scores.get(m.group(1)) if m else None
    return lane_of((src or side).get("note"))


def is_stretch(repo: Repo, side: dict) -> bool:
    """The scorer's stretch lane, read through a sibling like every lane."""
    return effective_lane(repo, side) == "digest-stretch"

# Early-career programs go first in every fire (Vaish 2026-10-15). They are his
# best fit as a December 2026 graduate and they close on dates, not on the 72h
# clock: three APM programs aged out behind higher-scored roles, and a fourth
# closed while its resume sat tailored. A "Senior Associate"
# title is a level, not a program.
EARLY_CAREER = re.compile(
    r"(?i)(\bassociate product manager\b|\bAPM\b|\brotational\b|\bnew[- ]grad"
    r"|\buniversity grad|\bearly[- ]career\b|\bgraduate\b|\b20\d\d start\b"
    r"|\blaunchpad\b|\bentry[- ]level\b)")
NOT_EARLY_CAREER = re.compile(
    r"(?i)\b(senior|sr\.?|lead|principal|staff|intern(ship)?|mba)\b")


def is_early_career(title: str) -> bool:
    return bool(EARLY_CAREER.search(title or "")) and not NOT_EARLY_CAREER.search(title or "")


def tailor_list(repo: Repo, cap: int = TAILOR_CAP, now=None) -> dict:
    now = now or datetime.now(timezone.utc)
    excluded, blocked, cands = Counter(), [], []

    brief_roles = {db.role_key(c, t) for _, c, t, _ in repo.briefs}
    recent_titles = {}
    for name, _, t, when in repo.briefs:
        nt = db.norm_title(t)
        if (when is not None and now - when <= timedelta(days=TITLE_GUARD_DAYS)
                and len(nt.split()) >= TITLE_GUARD_MIN_WORDS):
            recent_titles.setdefault(nt, name)

    for jid, side in repo.scores.items():
        promoted = repo.is_promoted(jid)
        stretch = not promoted and is_stretch(repo, side)
        if effective_lane(repo, side) != "auto-tailor" and not promoted and not stretch:
            continue
        rec = repo.queue.get(jid)
        if rec is None:
            excluded["no queue file"] += 1
            continue
        company, title = rec.get("company", ""), rec.get("title", "")
        item = {"id": jid, "company": company, "title": title,
                "score": int(side["score"]), "promoted": promoted}
        if stretch:
            item["stretch"] = True
        if jid in repo.pending_closed:
            excluded["pending decision"] += 1
            continue
        if jid in repo.tailored:
            excluded["tailored"] += 1
            continue
        if jid in repo.folder_ids:
            excluded["folder carries id"] += 1
            continue
        if repo.stale(rec, now):
            excluded["stale"] += 1
            continue
        # W3b (2026-10-09): never tailor blind. 18 of 109 auto-tailor rows had a
        # JD under 500 characters (Jobright's bot check); the brief, the ATS terms
        # and every reviewer read the JD. Vaish's own promotion is his call.
        if not promoted and len((rec.get("jd_markdown") or "").strip()) < JD_MIN_CHARS:
            blocked.append({**item, "reason": "no JD (under 500 characters)"})
            continue
        key = db.role_key(company, title)
        nt = db.norm_title(title)
        if key in repo.worked:
            blocked.append({**item, "reason": "role already worked (DB)"})
            continue
        if key in brief_roles:
            blocked.append({**item, "reason": "role already worked (brief heading)"})
            continue
        if nt in recent_titles:
            blocked.append({**item, "reason": f"title matches brief {recent_titles[nt]}"})
            continue
        best, worst, precision = db.job_age(rec.get("posted_at", ""),
                                            rec.get("first_seen", ""), now=now)
        item.update(tier=db.age_tier(best, precision), age=db.age_label(best, worst, precision),
                    queue=f"queue/{jid}.json", _key=key, _best=best)
        cands.append(item)

    # One per role: the best of each set of postings.
    order = lambda c: (not c["promoted"], c.get("stretch", False),
                       not is_early_career(c["title"]), -c["score"],
                       TIER_ORDER.get(c["tier"], 3),
                       c["_best"] if c["_best"] is not None else float("inf"))
    cands.sort(key=order)
    seen_roles, ranked = set(), []
    for c in cands:
        if c["_key"] in seen_roles:
            excluded["duplicate posting"] += 1
            continue
        seen_roles.add(c["_key"])
        ranked.append(c)

    week = Counter()
    for jid, rec in repo.tailored.items():
        ts = _parse_ts(rec.get("tailored_at"))
        if ts and now - ts <= timedelta(days=7):
            week[db.role_company_key(repo.company_of(jid))] += 1

    fire, selected, deferred = Counter(), [], []
    for c in ranked:
        co = db.role_company_key(c["company"])
        reason = None
        if not c["promoted"]:
            if fire[co] >= COMPANY_PER_FIRE:
                reason = f"company cap: {COMPANY_PER_FIRE} this fire"
            elif week[co] + fire[co] >= COMPANY_PER_WEEK:
                reason = f"company cap: {week[co]} tailored in 7 days"
        if reason is None and len(selected) >= cap:
            # Not "run cap" for a stretch: the routine refills a freed slot from
            # the first "run cap" deferral, and that must be an in-band job.
            reason = "stretch: no spare slot" if c.get("stretch") else f"run cap {cap}"
        out = {k: v for k, v in c.items() if not k.startswith("_")}
        if reason:
            deferred.append({**out, "reason": reason})
        else:
            fire[co] += 1
            selected.append(out)
    return {"selected": selected, "deferred": deferred, "blocked": blocked,
            "excluded": dict(excluded)}


# ---------------------------------------------------------------- cli

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("score")
    s.add_argument("--cap", type=int, default=SCORE_CAP)
    b = sub.add_parser("siblings")
    b.add_argument("--scorer", default="routine", choices=("routine", "local"))
    b.add_argument("--dry-run", action="store_true")
    t = sub.add_parser("tailor")
    t.add_argument("--cap", type=int, default=TAILOR_CAP)
    args = ap.parse_args(argv)

    conn = db.connect_readonly() if db.DB_PATH.exists() else None
    repo = Repo(ROOT, conn)
    if args.cmd == "score":
        out = score_list(repo, cap=args.cap)
    elif args.cmd == "siblings":
        done = fill_siblings(repo, scorer=args.scorer, apply=not args.dry_run)
        out = {"written" if not args.dry_run else "would_write": len(done),
               "pairs": [{"id": j, "duplicate_of": s} for j, s in done]}
    else:
        out = tailor_list(repo, cap=args.cap)
    print(json.dumps(out, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
