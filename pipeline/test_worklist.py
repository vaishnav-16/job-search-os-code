"""Tests for pipeline/worklist.py, the judge-queue score and tailor lists.

Each case replays something a routine run actually hit (the notes file names
the run): the generic-title over-block (run 13), a bank's posting filed
under a sponsor forum's name (09-22), a role tailored twice because its
brief was not yet in the DB (run 11), multi-city twins scored six times (run 10),
plus the company cap and the promise that a deferred job comes back next fire.

Run: python pipeline/test_worklist.py   (temp dirs + temp DB; no network, no LLM)
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import db  # noqa: E402
import ingest_scores  # noqa: E402
import worklist  # noqa: E402

PASS = FAIL = 0
NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"FAIL  {name}" + (f" - {detail}" if detail else ""))


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class Env:
    """A throwaway repo: queue/, scores/, tailored/, applications/, a DB."""

    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix="worklist-test-"))
        for d in ("queue", "scores", "tailored", "applications", "decisions"):
            (self.root / d).mkdir()
        ingest_scores.SCORES_DIR = self.root / "scores"
        self.conn = db.connect(self.root / "t.sqlite")
        self.n = 0

    def job(self, company, title, score=None, lane="auto-tailor", location="NY",
            hours_old=6, status="new", notes=None, jd="A real JD. " * 60):
        self.n += 1
        jid = hashlib.sha1(f"{company}|{title}|{location}|{self.n}".encode()).hexdigest()
        posted = iso(NOW - timedelta(hours=hours_old))
        (self.root / "queue" / f"{jid}.json").write_text(json.dumps({
            "id": jid, "company": company, "title": title, "location": location,
            "posted_at": posted, "first_seen": posted, "jd_markdown": jd}), encoding="utf-8")
        self.conn.execute(
            "INSERT INTO jobs (id, company, company_norm, title, title_norm, location, "
            "source, posted_at, first_seen, status, score, notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (jid, company, db.norm_company(company), title, db.norm_title(title), location,
             "greenhouse", posted, posted, status, score, notes or ""))
        self.conn.commit()
        if score is not None:
            ingest_scores.write_sidecar(jid, score, notes or f"{lane}; test")
        return jid

    def brief(self, company, title, jid="0" * 40, days_ago=1, sidecar=False):
        when = NOW - timedelta(days=days_ago)
        slug = f"{db.norm_company(company).replace(' ', '-')}-{self.n}-{when.date()}"
        self.n += 1
        d = self.root / "applications" / slug
        d.mkdir()
        (d / "brief.md").write_text(f"# {company} - {title}\n\n**id:** {jid}\n", encoding="utf-8")
        if sidecar:
            self.tailor(jid, slug, when)
        return slug

    def tailor(self, jid, folder, when):
        (self.root / "tailored" / f"{jid}.json").write_text(json.dumps({
            "id": jid, "folder": f"applications/{folder}", "tailored_at": iso(when)}),
            encoding="utf-8")

    def tailor_list(self, cap=12):
        return worklist.tailor_list(worklist.Repo(self.root, self.conn), cap=cap, now=NOW)

    def score_list(self):
        return worklist.score_list(worklist.Repo(self.root, self.conn), now=NOW)


def ids(items):
    return [x["id"] for x in items]


def reasons(out):
    return {x["id"]: x["reason"] for x in out["deferred"] + out["blocked"]}


def main() -> int:
    print("-- company aliases fold for the role key, never for dedupe --")
    check("Amazon spellings share one role company",
          {db.role_company_key(c) for c in ("Amazon", "Amazon.com", "Amazon Web Services",
                                            "Amazon Web Services (AWS)")} == {"amazon"})
    check("norm_company (the dedupe key) is untouched",
          db.norm_company("Amazon.com") == "amazon com"
          and db.job_id("Amazon.com", "PM", "Seattle, WA")
          == db.job_id("amazon com", "pm", "Seattle, WA"))
    check("distinct orgs stay distinct",
          db.role_company_key("Google DeepMind") != db.role_company_key("Google"))

    print("-- no JD, no tailor (W3b, 2026-10-09) --")
    e = Env()
    blind = e.job("Blindco", "Product Manager", 9, jd="")
    thin = e.job("Thinco", "Product Manager", 9, jd="Apply now.")
    real = e.job("Realco", "Product Manager", 9)
    picked = e.job("Pickedco", "Product Manager", 6, lane="digest", jd="")
    (e.root / "decisions" / "pending.json").write_text(
        json.dumps([{"id": picked, "action": "promote", "at": iso(NOW)}]), encoding="utf-8")
    out = e.tailor_list()
    sel = [x["id"] for x in out["selected"]]
    why = {x["id"]: x["reason"] for x in out["blocked"]}
    check("an empty JD is blocked", why.get(blind, "").startswith("no JD"), str(why))
    check("a thin JD is blocked", why.get(thin, "").startswith("no JD"), str(why))
    check("a real JD is selected", real in sel, str(sel))
    check("Vaish's promotion is selected without a JD", len(sel) == 2, str(sel))

    print("-- title guard: 4+ words only (run 13) --")
    e = Env()
    e.brief("OtherCo", "Product Manager")
    e.brief("Leader Bank", "Associate Product Manager")
    generic = e.job("Superblocks", "Product Manager", 9)
    apm = e.job("Elsevier", "Associate Product Manager", 9)
    out = e.tailor_list()
    check("a generic title is not blocked by another company's brief",
          generic in ids(out["selected"]), str(reasons(out)))
    check("nor a three-word one", apm in ids(out["selected"]), str(reasons(out)))

    e = Env()
    e.brief("Indeed Sponsor Forum", "Senior Associate Product Manager Card")
    misfiled = e.job("Example Bank", "Senior Associate Product Manager Card", 10)
    e.brief("Acme", "Senior Product Manager Payments Platform", days_ago=45)
    old = e.job("Beta", "Senior Product Manager Payments Platform", 9)
    out = e.tailor_list()
    check("a 4-word title matching a recent brief is blocked (Example Bank under two ids)",
          "title matches brief" in reasons(out).get(misfiled, ""), str(reasons(out)))
    check("a brief older than 30 days does not block by title", old in ids(out["selected"]))

    print("-- role_key against brief headings (run 11) --")
    e = Env()
    first = "1" * 40
    e.brief("Initech", "Product Manager, Payments", jid=first)
    twin = e.job("Initech", "Product Manager, Payments", 9, location="")
    out = e.tailor_list()
    check("a second id for a role whose brief exists is blocked",
          "brief heading" in reasons(out).get(twin, ""), str(reasons(out)))
    carried = e.job("Other", "PM", 9)
    (e.root / "applications" / "x-2026-09-27").mkdir()
    (e.root / "applications" / "x-2026-09-27" / "score.md").write_text(carried, encoding="utf-8")
    check("an id carried by any folder is excluded",
          carried not in ids(e.tailor_list()["selected"]))

    print("-- DB-worked roles, lanes, staleness --")
    e = Env()
    e.job("Acme", "Product Manager, Growth", 8, status="tailored", location="CA")
    worked = e.job("Acme", "Product Manager, Growth", 9, location="TX")
    digest = e.job("Beta", "PM", 8, lane="digest")
    stale = e.job("Gamma", "PM", 10, hours_old=80)
    out = e.tailor_list()
    check("a role worked in the DB is blocked",
          "(DB)" in reasons(out).get(worked, ""), str(reasons(out)))
    check("digest lane is not tailored", digest not in ids(out["selected"]))
    check("a provably stale job is excluded", stale not in ids(out["selected"])
          and out["excluded"].get("stale") == 1, str(out["excluded"]))

    print("-- collapse and order --")
    e = Env()
    lo = e.job("Acme", "Product Manager, Search", 7, location="CA")
    hi = e.job("Acme", "Product Manager, Search", 9, location="WA")
    fresh7 = e.job("Beta", "PM", 7, hours_old=2)
    old9 = e.job("Gamma", "PM", 9, hours_old=40)
    out = e.tailor_list()
    sel = ids(out["selected"])
    check("postings of one role collapse to the best score",
          hi in sel and lo not in sel and out["excluded"].get("duplicate posting") == 1)
    check("score outranks age", sel.index(old9) < sel.index(fresh7), str(sel))

    print("-- company cap --")
    e = Env()
    c1 = e.job("Example Bank", "PM A", 10)
    c2 = e.job("Example Bank", "PM B", 10)
    c3 = e.job("Example Bank", "PM C", 9)
    other = e.job("Beta", "PM", 7)
    out = e.tailor_list()
    check("at most 2 per company per fire", ids(out["selected"]) == [c1, c2, other],
          str(ids(out["selected"])))
    check("the third is deferred with its reason",
          reasons(out).get(c3) == "company cap: 2 this fire", str(reasons(out)))

    e = Env()
    for i, days in enumerate((1, 3, 6, 8)):
        jid = e.job("Example Bank", f"Done {i}", 9, status="tailored")
        e.tailor(jid, f"cap-{i}", NOW - timedelta(days=days))
    wk = e.job("Example Bank", "PM New", 10)
    wk2 = e.job("Example Bank", "PM Newer", 10)
    out = e.tailor_list()
    check("3 in the last 7 days leaves one slot (the 8-day-old one does not count)",
          wk in ids(out["selected"]) and "3 tailored in 7 days" in reasons(out).get(wk2, ""),
          str(reasons(out)))
    check("Amazon aliases share one cap",
          worklist.db.role_company_key("Amazon.com")
          == worklist.db.role_company_key("Amazon Web Services (AWS)"))

    print("-- promoted jobs --")
    e = Env()
    a = e.job("Example Bank", "PM A", 10)
    b = e.job("Example Bank", "PM B", 10)
    promo = e.job("Example Bank", "PM Promoted", 6, lane="digest")
    (e.root / "decisions" / "pending.json").write_text(
        json.dumps([{"id": promo, "action": "promote", "at": iso(NOW)}]), encoding="utf-8")
    via_db = e.job("Delta", "PM", 5, lane="digest")
    e.conn.execute("UPDATE jobs SET notes='auto-tailor; promoted via dashboard 2026-09-27; x' "
                   "WHERE id=?", (via_db,))
    e.conn.commit()
    out = e.tailor_list()
    sel = ids(out["selected"])
    check("a pending promote sorts first despite a low score", set(sel[:2]) == {promo, via_db}, str(sel))
    check("a DB-folded promote under a stale digest sidecar still counts", via_db in sel)
    check("promoted jobs are exempt from the cap but count toward it",
          promo in sel and a in sel and b not in sel, str(reasons(out)))

    e = Env()
    judged = e.job("Acme", "PM", 9, status="scored", notes="auto-tailor; strong")
    ingest_scores.write_sidecar(judged, 9, "skip; PERM ad")
    out = e.tailor_list()
    check("a routine skip not yet folded by sync stays out (not read as a promote)",
          judged not in ids(out["selected"]) + ids(out["deferred"]), str(out))

    print("-- a pending close waits hours for its fold (W7, 2026-10-13) --")
    e = Env()
    skipped = e.job("Acme", "PM Skipped", 9)
    retracted = e.job("Beta", "PM Retracted", 9)
    unscored = e.job("Gamma", "PM Unscored")
    (e.root / "decisions" / "pending.json").write_text(json.dumps([
        {"id": skipped, "action": "skipped", "at": iso(NOW)},
        {"id": retracted, "action": "skipped", "at": iso(NOW - timedelta(minutes=5))},
        {"id": retracted, "action": "cancel", "at": iso(NOW)},
        {"id": unscored, "action": "rejected", "at": iso(NOW)}]), encoding="utf-8")
    out = e.tailor_list()
    sel = ids(out["selected"])
    check("a pending skip is never tailored", skipped not in sel and skipped not in reasons(out),
          str(out))
    check("a cancel after the skip retracts it", retracted in sel, str(sel))
    check("the exclusion is counted", out["excluded"].get("pending decision") == 1,
          str(out["excluded"]))
    sl = e.score_list()
    check("a pending reject is never scored", unscored not in ids(sl["score"])
          and sl["excluded"].get("pending decision") == 1, str(sl))

    print("-- deferred jobs come back next fire --")
    e = Env()
    jobs = [e.job(f"Co{i}", "PM", 9) for i in range(5)]
    first = e.tailor_list(cap=3)
    check("run cap defers the rest", len(first["selected"]) == 3
          and [x["reason"] for x in first["deferred"]] == ["run cap 3"] * 2)
    for x in first["selected"]:
        e.brief(x["company"], x["title"], jid=x["id"], days_ago=0, sidecar=True)
    second = e.tailor_list(cap=3)
    check("the next call selects what the first deferred",
          ids(second["selected"]) == ids(first["deferred"]), str(ids(second["selected"])))

    print("-- score list and siblings (run 10) --")
    e = Env()
    t1 = e.job("U.S. Bank", "Technical Program Manager", location="MN", hours_old=3)
    t2 = e.job("U.S. Bank", "Technical Program Manager", location="OH", hours_old=5)
    t3 = e.job("U.S. Bank", "Technical Program Manager", location="NC", hours_old=7)
    done = e.job("Acme", "PM", status="tailored")
    e.tailor(done, "acme", NOW)
    known = e.job("Beta", "PM", 8)
    later = e.job("Beta", "PM", location="TX")
    old = e.job("Gamma", "PM", hours_old=90)
    sc = e.score_list()
    check("one representative per role, the newest", ids(sc["score"]) == [t1], str(sc))
    check("its twins are counted", sc["score"][0]["duplicates"] == 2)
    check("a role with a scored posting waits for siblings",
          later not in ids(sc["score"]) and sc["fill_from_siblings"] == 1)
    check("tailored and stale files are not scored",
          sc["excluded"] == {"already tailored": 1, "stale": 1}, str(sc["excluded"]))
    ingest_scores.write_sidecar(t1, 9, "auto-tailor; strong")
    pairs = worklist.fill_siblings(worklist.Repo(e.root, e.conn), now=NOW)
    check("siblings fill every twin", sorted(pairs) == sorted(
        [(t2, t1), (t3, t1), (later, known)]), str(pairs))
    side = json.loads((e.root / "scores" / f"{t2}.json").read_text(encoding="utf-8"))
    check("sibling sidecar copies score and lane, names its source",
          (side["score"], side["note"]) == (9, f"auto-tailor; duplicate role of {t1}"), str(side))
    check("a second fill is a no-op",
          worklist.fill_siblings(worklist.Repo(e.root, e.conn), now=NOW) == [])
    check("nothing left to score", e.score_list()["score"] == [])
    out = e.tailor_list()
    check("the twins still collapse to one tailor slot",
          sum(x["company"] == "U.S. Bank" for x in out["selected"]) == 1)
    # The routine re-lanes the source skip on reading the JD, after siblings ran.
    ingest_scores.write_sidecar(t1, 9, "skip; MDP caps experience at 1 year")
    out = e.tailor_list()
    check("a judgment skip on the source takes its twins with it (Aristocrat 10-06)",
          not any(x["company"] == "U.S. Bank" for x in out["selected"]), str(ids(out["selected"])))

    print("-- score order: likely in-band first (2026-10-08) --")
    P = worklist.score_priority
    for label, rec, want in (
            ("3+ years asked is in band",
             {"title": "Senior PM", "jd_markdown": "3+ years of product management experience"}, 0),
            ("5 years asked is a possible stretch",
             {"title": "PM", "jd_markdown": "5+ years of product experience required"}, 1),
            ("6 years asked is digest at best",
             {"title": "PM", "jd_markdown": "6+ years of product experience required"}, 2),
            ("a Senior title with no years is digest at best", {"title": "Senior PM"}, 2),
            ("a plain title with no years is in band", {"title": "Product Manager II"}, 0)):
        got = P(rec)
        check(label, got == want, f"got {got}")
    e = Env()
    sr = e.job("Acme", "Senior Product Manager", hours_old=1)
    apm = e.job("Beta", "Associate Product Manager", hours_old=20)
    check("an older in-band title outranks a newer Senior one",
          ids(e.score_list()["score"]) == [apm, sr], str(ids(e.score_list()["score"])))

    print("-- early-career programs go first (2026-10-15) --")
    e = Env()
    big = [e.job(f"Big{i}", "Product Manager, Payments", 10) for i in range(2)]
    prog = e.job("Umbrella", "Product Manager 2 - Rotational Product Management (RPM) Program", 8)
    apm = e.job("Hooli", "Associate Product Manager - Launchpad", 7)
    level = e.job("Vandelay", "Senior Associate, Product Manager", 10)
    out = e.tailor_list(cap=3)
    sel = ids(out["selected"])
    check("programs outrank higher scores", sel[:2] == [prog, apm], str(sel))
    check("then score order resumes", sel[2] in big + [level], str(sel))
    check("a 'Senior Associate' level is not a program", level not in sel[:2], str(sel))

    print("-- stretch fills spare slots only (2026-10-08) --")
    e = Env()
    inband = [e.job(f"Co{i}", "PM", 7) for i in range(2)]
    s10 = e.job("Big", "Senior PM, AI", 10, notes="digest-stretch; 5+ years (b)=1, AI fit")
    s8 = e.job("Mid", "Senior PM, Data", 8, notes="digest-stretch; 5+ years (b)=1, data fit")
    plain = e.job("Dig", "Senior PM", 9, notes="digest; 6+ years (b)=1")
    out = e.tailor_list(cap=3)
    sel = ids(out["selected"])
    check("in-band jobs come first even at a lower score", sel[:2] == inband, str(sel))
    check("one spare slot takes the best stretch", sel[2:] == [s10], str(sel))
    check("a stretch pick is marked", out["selected"][2].get("stretch") is True)
    check("a stretch past the cap is not a 'run cap' deferral",
          reasons(out).get(s8) == "stretch: no spare slot", str(reasons(out)))
    check("a plain digest job never fills a slot",
          plain not in sel and plain not in reasons(out))
    out = e.tailor_list(cap=2)
    check("a full fire takes no stretch", ids(out["selected"]) == inband)
    e.job("Big", "Senior PM, Platform", 9, notes="digest-stretch; 5+ years (b)=1")
    e.job("Big", "Senior PM, Agents", 9, notes="digest-stretch; 5+ years (b)=1")
    out = e.tailor_list(cap=12)
    check("stretch picks count toward the company cap",
          sum(x["company"] == "Big" for x in out["selected"]) == 2
          and any("company cap" in r for r in reasons(out).values()), str(reasons(out)))
    twin = e.job("Mid", "Senior PM, Data", 8, location="TX",
                 notes=f"digest-stretch; duplicate role of {s8}")
    check("a sibling of a stretch is the same role, never a second pick",
          twin not in ids(e.tailor_list(cap=12)["selected"]))

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
