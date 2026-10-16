"""Tests for the startup and Jobright-app sources (2026-10-08).

Run: python pipeline/test_vc_boards.py   (fakes only; no network)

What is pinned: the Getro page parse and its filter, Consider's date-only
stamps, the US-location pick, the "0 jobs from a Getro board is an error"
alarm, dedupe across boards, and the Jobright app feed's three states (no
cookie, a good feed read only as deep as the window, an expired cookie).
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db  # noqa: E402
import ingest_scores  # noqa: E402
import scrape  # noqa: E402
import vc_boards  # noqa: E402

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}  (got {got!r}, want {want!r})")
        FAILS.append(label)


class Resp:
    def __init__(self, text="", status=200, payload=None):
        self.text, self.status_code, self._payload = text, status, payload
        self.ok = status < 400

    def json(self):
        return self._payload

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(f"HTTP {self.status_code}")


def getro_page(jobs):
    data = {"props": {"pageProps": {"initialState": {"jobs": {"found": jobs, "total": len(jobs)}}}}}
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></html>'


def test_employer_readers():
    """Each per-ATS reader against a canned answer shaped like the live one
    probed on 2026-10-15; no network."""
    print("employer JD readers (10-15): the URL picks the API, the API's field is the JD")
    body = "<p>Own the roadmap.</p>" + "<p>Ship it.</p>" * 3
    google_row = ["111", "TPM I", "u", [None, "<p>Resp</p>"], [None, "<p>Minimum qualifications: 1 year.</p>"],
                  None, None, None, None, [["Austin, TX"]], [None, "<p>About</p>"], None, [1760000000]]
    tiktok_page = ("<html><p>TikTok</p><p>Responsibilities</p><p>Build lead tools.</p>"
                   "<p>Minimum Qualifications:</p><p>Chinese required.</p><p>Job Information</p>"
                   "<p>$122574</p></html>")
    answers = [  # (url fragment, response)
        ("/wday/cxs/acme/Careers/job/NY/PM_R1", Resp(payload={"jobPostingInfo": {"jobDescription": body}})),
        ("recruitingCEJobRequisitionDetails", Resp(payload={"items": [{
            "ExternalDescriptionStr": body, "ExternalQualificationsStr": "<p>3 years</p>"}]})),
        ("eightfold.ai/api/apply/v2/jobs/42", Resp(payload={"job_description": body})),
        ("api/pcsx/position_details", Resp(payload={"data": {"jobDescription": body}})),
        ("api.smartrecruiters.com/v1/companies/Canva/postings/77", Resp(payload={"jobAd": {"sections": {
            "jobDescription": {"text": body}, "qualifications": {"text": "<p>SQL</p>"}}}})),
        ("apply.workable.com/api/v2/accounts/acme/jobs/AB12", Resp(payload={"description": body})),
        ("api.rippling.com/platform/api/ats/v1/board/acme/jobs/", Resp(payload={
            "description": {"company": "<p>About us</p>", "role": body}})),
        ("lifeattiktok.com/search/555", Resp(text=tiktok_page)),
        ("google.com/about/careers/applications/jobs/results/111", Resp(
            text="AF_initDataCallback({key: 'ds:0', hash: '1', data:" + json.dumps([google_row]) +
                 ", sideChannel: {}});")),
        ("careers.example/job/9", Resp(text='<script type="application/ld+json">{"@graph": [{"@type": '
                                            '["JobPosting"], "description": "<p>Own\tthe roadmap.</p>"}]}</script>')),
    ]
    calls = []

    def fake_get(url, **kw):
        full = url + ("?" + "&".join(f"{k}={v}" for k, v in (kw.get("params") or {}).items())
                      if kw.get("params") else "")
        calls.append(full)
        for frag, resp in answers:
            if frag in full:
                return resp
        return Resp(status=404)

    saved = vc_boards.requests.get
    vc_boards.requests.get = fake_get
    try:
        cases = [
            ("Workday", "https://acme.wd5.myworkdayjobs.com/en-US/Careers/job/NY/PM_R1", "Own the roadmap"),
            ("Oracle", "https://ecxx.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/334348",
             "3 years"),
            ("Eightfold", "https://acme.eightfold.ai/careers/job/42", "Own the roadmap"),
            ("Microsoft", "https://apply.careers.microsoft.com/careers/job/1970393557026661", "Own the roadmap"),
            ("SmartRecruiters", "https://jobs.smartrecruiters.com/Canva/77-product-manager", "SQL"),
            ("Workable", "https://apply.workable.com/acme/j/AB12", "Own the roadmap"),
            ("Rippling", "https://ats.rippling.com/acme/jobs/1311a98d-008f-4517-9f9f-73ee4c9cf1cf",
             "About us"),
            ("TikTok (careers.tiktok.com form)", "https://careers.tiktok.com/position/555/detail",
             "Chinese required"),
            ("Google detail page", "https://www.google.com/about/careers/applications/jobs/results/111-tpm-i",
             "Minimum qualifications"),
            ("JSON-LD in an @graph with a list @type and a raw tab", "https://careers.example/job/9",
             "Own"),
        ]
        for name, url, want in cases:
            check(f"{name} JD is read", want in vc_boards.ats_jd(url), True)
        check("TikTok stops before the pay block", "122574" in vc_boards.ats_jd(
            "https://lifeattiktok.com/search/555"), False)
        check("Oracle asks by id on the posting's own site", any(
            'Id=%22334348%22,siteNumber=CX_1' in c for c in calls), True)
        check("an unknown page with no JSON-LD gives an empty JD",
              vc_boards.ats_jd("https://nothing.example/job/1"), "")

        print("getro: an empty Getro page falls back to the employer's posting")
        vc_boards.getro_jd = lambda page: ""
        check("the employer's JD is used", "Own the roadmap" in vc_boards.getro_or_employer_jd(
            "https://getro.example/companies/a/jobs/b", "https://acme.eightfold.ai/careers/job/42"), True)
        check("no apply link keeps Getro's answer", vc_boards.getro_or_employer_jd(
            "https://getro.example/companies/a/jobs/b", ""), "")
    finally:
        vc_boards.requests.get = saved
        vc_boards.getro_jd = _saved_getro_jd


_saved_getro_jd = vc_boards.getro_jd


def main() -> int:
    test_employer_readers()
    now = datetime.now(timezone.utc)

    print("getro: the server-rendered page, filtered, newest first")
    seen_params = {}

    class FakeSession:
        def get(self, url, params=None, **kw):
            seen_params.update(params or {})
            return Resp(getro_page([
                {"title": "Associate Product Manager", "createdAt": int(now.timestamp()) - 3600,
                 "url": "https://boards.greenhouse.io/acme/jobs/1", "slug": "1-apm",
                 "locations": ["London, UK", "New York, NY, USA"],
                 "organization": {"name": "Acme", "slug": "acme"}},
                {"title": "", "organization": {"name": "Blank"}},
            ]))
    rows = vc_boards.getro_jobs("jobs.example.com", session=FakeSession())
    check("the filter rides in the query string", "filter" in seen_params, True)
    check("a titleless job is dropped", len(rows), 1)
    check("the employer link is kept", rows[0]["url"], "https://boards.greenhouse.io/acme/jobs/1")
    check("the US location is picked over the first one", rows[0]["location"], "New York, NY, USA")
    check("createdAt becomes an exact UTC stamp", rows[0]["posted_at"].endswith("Z"), True)

    print("consider and location helpers")
    check("a midnight stamp is a date, not a time",
          vc_boards._iso("2026-10-07T00:00:00Z"), "2026-10-07")
    check("a real stamp is kept", vc_boards._iso("2026-10-07T15:09:26Z"), "2026-10-07T15:09:26Z")
    check("no US location falls back to the first", vc_boards._us_location(["Berlin"]), "Berlin")

    tmp = Path(tempfile.mkdtemp(prefix="vc-test-"))
    conn = db.connect(tmp / "t.sqlite")
    saved = (scrape.QUEUE_DIR, ingest_scores.SCORES_DIR, vc_boards.getro_jobs,
             vc_boards.consider_jobs, vc_boards.GETRO_BOARDS, vc_boards.CONSIDER_BOARDS,
             scrape.jobright_app_page, scrape.fetch_jobright_detail, scrape.JOBRIGHT_APP_PAUSE)
    scrape.QUEUE_DIR = tmp / "queue"
    scrape.QUEUE_DIR.mkdir()
    ingest_scores.SCORES_DIR = tmp / "scores"
    try:
        print("poll_vc_boards: dedupe across boards, JD only where it matters, 0-job alarm")
        stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        jd_calls = []

        def r(company, title, loc="Austin, TX, USA"):
            return {"company": company, "title": title, "location": loc,
                    "url": f"https://x/{company}", "posted_at": stamp,
                    "jd_fetch": (lambda c=company: jd_calls.append(c) or "2+ years of PM work.")}
        vc_boards.GETRO_BOARDS = {"a": "a.example", "b": "b.example", "dead": "d.example"}
        vc_boards.CONSIDER_BOARDS = {"c": "c.example"}
        vc_boards.getro_jobs = lambda host: {
            "a.example": [r("Acme", "Product Manager"), r("Acme", "Staff Product Manager")],
            "b.example": [r("Acme", "Product Manager"), r("Beta", "Software Engineer")],
            "d.example": []}[host]
        vc_boards.consider_jobs = lambda host: [r("Gamma", "Product Owner", "Berlin, Germany")]
        stats = scrape.poll_vc_boards(conn)
        rows = dict(conn.execute("SELECT title || '@' || company, status FROM jobs"))
        check("one row per role across two boards", rows.get("Product Manager@Acme"), "new")
        check("a title-rejected row is stored skipped", rows.get("Staff Product Manager@Acme"),
              "skipped")
        check("an off-target row is stored skipped", rows.get("Software Engineer@Beta"), "skipped")
        check("a non-US row never lands", "Product Owner@Gamma" in rows, False)
        check("the JD was fetched once, for the queued row", jd_calls, ["Acme"])
        check("a Getro board with 0 jobs raises the alarm",
              "dead: board returned 0 jobs" in (stats["vc:getro"]["error"] or ""), True)
        check("source names the board", conn.execute(
            "SELECT source FROM jobs WHERE title='Product Manager'").fetchone()[0], "getro:a")

        print("jobright app feed: skipped without a cookie")
        check("no cookie is a note, not an error",
              scrape.poll_jobright_app(conn, cookie="")["jobright:app"]["note"],
              "JOBRIGHT_COOKIE not set; skipped")

        print("jobright app feed: the Recommended order, a fixed few pages, stale rows dropped")
        pages = []

        def item(i, hours_ago, title="Product Manager"):
            t = (now - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")
            return {"jobResult": {"jobId": f"j{i}", "jobTitle": title, "jobLocation": "Remote, US",
                                  "publishTime": t},
                    "companyResult": {"companyName": f"Co{i}"}}

        def fake_page(session, pos):
            pages.append(pos)
            check("the session carries the cookie", session.headers.get("Cookie"), "sid=abc")
            return {0: [item(1, 1), item(2, 20)], 20: [item(3, 3), item(4, 9)],
                    40: [item(5, 80)]}.get(pos, [])
        scrape.jobright_app_page = fake_page
        scrape.fetch_jobright_detail = lambda url: ""
        scrape.JOBRIGHT_APP_PAUSE = 0
        check("it asks for the Recommended order", "sortCondition=0" in scrape.JOBRIGHT_APP_URL, True)
        st = scrape.poll_jobright_app(conn, cookie="sid=abc")["jobright:app"]
        check("an older job high in the ranking does not stop the read", pages, [0, 20, 40, 60])
        check("every row under 72h was ingested", st["new"], 4)
        check("a row past 72h was dropped as stale", st.get("stale"), 1)
        check("its link is the public job page", conn.execute(
            "SELECT url FROM jobs WHERE company='Co1'").fetchone()[0], "https://jobright.ai/jobs/info/j1")

        print("jobright app feed: the employer's posting beats Jobright's rewrite (10-15)")
        rewrite = "Proficiency in Chinese and English is required. " * 20
        employer = "Minimum Qualifications: a Bachelor's degree and some product planning. " * 20

        def jd_item(i, apply):
            it = item(i, 1)
            it["jobResult"].update({"jobSummary": rewrite, "applyLink": apply})
            return it
        scrape.jobright_app_page = lambda session, pos: (
            [jd_item(20, "https://careers.example/ok"), jd_item(21, "https://careers.example/js")]
            if pos == 0 else [])
        saved_ats = vc_boards.ats_jd
        vc_boards.ats_jd = lambda url: employer if url.endswith("/ok") else ""
        try:
            st = scrape.poll_jobright_app(conn, cookie="sid=abc")["jobright:app"]
        finally:
            vc_boards.ats_jd = saved_ats
        jd_of = lambda co: conn.execute(
            "SELECT jd_text FROM jobs WHERE company=?", (co,)).fetchone()[0] or ""
        check("a readable employer posting is the JD", jd_of("Co20").startswith("Minimum Qualifications"),
              True)
        check("Jobright's rewrite is the fallback when the employer page is empty",
              "Chinese" in jd_of("Co21"), True)
        check("the employer hit is counted", st.get("jd_from_employer"), 1)

        print("jobright app feed: a dropped kept-alive connection is retried once")
        calls = []

        class DroppingSession:
            def get(self, url, **kw):
                calls.append(url)
                if len(calls) == 1:
                    raise scrape.requests.ConnectionError("RemoteDisconnected")
                return Resp(payload={"errorCode": 10000,
                                     "result": {"jobList": [item(9, 1)]}})
        got = saved[6](DroppingSession(), 20)
        check("the page is read on the second try", (len(calls), len(got)), (2, 1))

        print("jobright app feed: an expired cookie says so")

        def expired(session, pos):
            raise RuntimeError("HTTP 401: session cookie expired or rate-limited; "
                               "refresh the JOBRIGHT_COOKIE secret")
        scrape.jobright_app_page = expired
        st = scrape.poll_jobright_app(conn, cookie="sid=old")["jobright:app"]
        check("the error names the secret to refresh", "JOBRIGHT_COOKIE" in (st["error"] or ""), True)
        check("and the source keeps its note, so it is not a blind-run vote", bool(st.get("note")), True)
    finally:
        (scrape.QUEUE_DIR, ingest_scores.SCORES_DIR, vc_boards.getro_jobs,
         vc_boards.consider_jobs, vc_boards.GETRO_BOARDS, vc_boards.CONSIDER_BOARDS,
         scrape.jobright_app_page, scrape.fetch_jobright_detail,
         scrape.JOBRIGHT_APP_PAUSE) = saved

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
        return 1
    print("all vc-board and jobright-app tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
