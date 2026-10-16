"""Tests for the big-tech career-site fetchers (2026-10-10).

Run: python pipeline/test_bigtech.py   (fakes only; no network)

What is pinned, per site: the payload parse against a small fixture shaped like
the live response, the posted_at reading (Amazon's double-spaced day, Netflix's
midnight epoch as a date, Apple's request-time stamp on managed-pipeline roles,
Microsoft's postedTs with creationTs carried alongside), the JD carrying the
qualifications, the stop rule for each pagination scheme, the coverage-gap
flag, and the "response lost its shape" alarm.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bigtech  # noqa: E402

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


class FakeSession:
    """Answers every GET with handler(url, params) and records the calls."""

    def __init__(self, handler):
        self.handler, self.calls = handler, []

    def get(self, url, params=None, **kw):
        self.calls.append((url, params))
        return self.handler(url, params)


def raises(fn) -> str:
    try:
        fn()
    except bigtech.BoardError as e:
        return "BoardError"
    except Exception as e:                      # noqa: BLE001
        return type(e).__name__
    return ""


def param(params, key):
    """A value from a dict or a list of pairs."""
    if isinstance(params, dict):
        return params.get(key)
    return [v for k, v in params if k == key]


def main() -> int:
    now = datetime.now(timezone.utc)
    ts = lambda hours: int((now - timedelta(hours=hours)).timestamp())          # noqa: E731
    day = lambda days: (now - timedelta(days=days)).strftime("%B %d, %Y").replace(" 0", "  ")  # noqa: E731

    # --- Amazon ---------------------------------------------------------------
    print("amazon: the list carries the JD, a day-precision date, and pages of 100")
    check("a double-spaced day parses", bigtech.amazon_date("October  9, 2026"), "2026-10-09")
    check("garbage is unknown, never a guess", bigtech.amazon_date("about 7 hours"), "")

    def az_job(i, days_ago):
        return {"title": f"Product Manager - Technical {i}", "normalized_location": "Seattle, Washington, USA",
                "job_path": f"/en/jobs/{i}/pm", "posted_date": day(days_ago),
                "description": "Own the roadmap.<br/>Ship it.",
                "basic_qualifications": "- 3+ years of product management experience",
                "preferred_qualifications": "- MBA"}
    pages = {0: [az_job(1, 0), az_job(2, 1), az_job(3, 30)],
             100: [az_job(4, 1), az_job(5, 20)],
             200: [az_job(6, 25), az_job(7, 40)]}
    sess = FakeSession(lambda url, p: Resp(payload={
        "error": None, "hits": 300, "jobs": pages.get(p["offset"], [])}))
    rows = bigtech.fetch_amazon(session=sess)
    check("a stale row mid-page does not end the walk; the all-stale page does",
          [c[1]["offset"] for c in sess.calls], [0, 100, 200])
    check("only rows inside the window come back", [r["url"].split("/")[-2] for r in rows],
          ["1", "2", "4"])
    check("the US filter and newest-first sort ride along",
          (sess.calls[0][1]["normalized_country_code[]"], sess.calls[0][1]["sort"]), ("USA", "recent"))
    check("the years ask (basic qualifications) is in the JD",
          "3+ years of product management" in rows[0]["jd"], True)
    check("posted_at is a bare date", len(rows[0]["posted_at"]), 10)
    check("not truncated when the walk ended on freshness", rows.truncated, "")
    sess = FakeSession(lambda url, p: Resp(payload={"error": None, "hits": 900,
                                                    "jobs": [az_job(p["offset"], 0)]}))
    check("the page cap with fresh rows still coming is a named gap",
          "page cap" in bigtech.fetch_amazon(session=sess).truncated, True)
    check("0 hits is an alarm, not a quiet board", raises(lambda: bigtech.fetch_amazon(
        session=FakeSession(lambda u, p: Resp(payload={"error": None, "hits": 0, "jobs": []})))),
        "BoardError")

    # --- Microsoft --------------------------------------------------------------
    print("microsoft: PCSX search sorted by postedTs, 10 a page, JD by detail call")

    def ms_pos(i, posted_h, created_h):
        return {"id": i, "name": f"Product Manager {i}", "standardizedLocations": ["Bengaluru, KA, IN", "Redmond, WA, US"],
                "postedTs": ts(posted_h), "creationTs": ts(created_h), "positionUrl": f"/careers/job/{i}"}
    ms_pages = {0: [ms_pos(1, 2, 24 * 65), ms_pos(2, 30, 40)], 10: [ms_pos(3, 100, 120)]}
    sess = FakeSession(lambda url, p: Resp(payload={"data": {"count": 25, "positions": ms_pages.get(
        dict(p)["start"], [])}}))
    rows = bigtech.fetch_microsoft(session=sess)
    check("both professions are sent as repeated filters",
          param(sess.calls[0][1], "filter_profession"), ["product management", "program management"])
    check("the walk stops on the first all-stale page", len(sess.calls), 2)
    check("fresh rows by postedTs", [r["title"] for r in rows], ["Product Manager 1", "Product Manager 2"])
    check("postedTs is the posted_at, exact", rows[0]["posted_at"].endswith("Z"), True)
    check("creationTs rides along for a repost check", rows[0]["_created_at"] < rows[0]["posted_at"], True)
    check("the US location is picked over the first one", rows[0]["location"], "Redmond, WA, US")
    check("the detail URL names the position", "position_id=1&domain=microsoft.com" in rows[0]["_detail"], True)
    jd = bigtech.fetch_microsoft_jd(rows[0], session=FakeSession(lambda u, p: Resp(payload={
        "data": {"jobDescription": "<p>Requires 2+ years.</p>"}})))
    check("the detail JD is plain text", jd, "Requires 2+ years.")
    check("a missing data.positions is an alarm", raises(lambda: bigtech.fetch_microsoft(
        session=FakeSession(lambda u, p: Resp(payload={"status": 403})))), "BoardError")

    print("microsoft: a 429 is waited out and retried (11 of 14 runs, 10-14/15)")
    waits, real_sleep = [], bigtech.time.sleep
    bigtech.time.sleep = waits.append
    try:
        answers = iter([Resp(status=429), Resp(payload={"data": {"count": 1, "positions": [ms_pos(9, 2, 3)]}})])
        sess = FakeSession(lambda u, p: next(answers))
        rows = bigtech.fetch_microsoft(session=sess)
        check("one 429, then the page: the rows come through", [r["title"] for r in rows], ["Product Manager 9"])
        check("it waited the first fixed wait", waits, [bigtech.RETRY_429_WAITS[0]])
        waits.clear()
        limited = Resp(status=429)
        limited.headers = {"Retry-After": "7"}
        answers = iter([limited, Resp(payload={"data": {"jobDescription": "<p>JD</p>"}})])
        jd = bigtech.fetch_microsoft_jd(rows[0], session=FakeSession(lambda u, p: next(answers)))
        check("Retry-After is honoured on the detail fetch", (jd, waits), ("JD", [7]))
        waits.clear()
        sess = FakeSession(lambda u, p: Resp(status=429))
        check("still limited after every wait: the fetch fails loudly",
              raises(lambda: bigtech.fetch_microsoft(session=sess)), "RuntimeError")
        check("tries once per wait plus once", len(sess.calls), len(bigtech.RETRY_429_WAITS) + 1)
    finally:
        bigtech.time.sleep = real_sleep

    # --- Google -----------------------------------------------------------------
    print("google: page 1 of the server-rendered results, one per query")

    def g_job(i, hours_ago, title="Product Manager II, Ads"):
        stamp = [ts(hours_ago), 0]
        row = [str(i), title, "https://www.google.com/about/careers/applications/signin?jobId=x",
               [None, "<ul><li>Own the roadmap.</li></ul>"],
               [None, "<h3>Minimum qualifications:</h3><ul><li>2 years of experience in product management.</li></ul>"],
               "projects/x", None, "Google", "en-US",
               [["Mountain View, CA, USA", ["1600 Amphitheatre"], "Mountain View", "94043", "CA", "US"]],
               [None, "<p>About the job.</p>"], [2], stamp, stamp, stamp, [None, ""], None, None, [None, ""],
               [None, ""], 2]
        return row

    def g_page(rows, total):
        facet = [[["projects/x/companies/1", "DeepMind", "deepmind"]]]
        return ("<html><script nonce=\"n\">AF_initDataCallback({key: 'ds:0', hash: '1', data:"
                + json.dumps(facet) + ", sideChannel: {}});</script>"
                "<script nonce=\"n\">AF_initDataCallback({key: 'ds:1', hash: '2', data:"
                + json.dumps([rows, None, total, 20]) + ", sideChannel: {}});</script></html>")
    sess = FakeSession(lambda u, p: Resp(text=g_page([g_job(1, 2), g_job(2, 100)], 2)))
    rows = bigtech.fetch_google(session=sess)
    check("the company facet blob is not mistaken for the jobs", rows[0]["title"], "Product Manager II, Ads")
    check("one request per query, page 1 only", [param(c[1], "q")[0] for c in sess.calls],
          ['"product manager"', '"program manager"'])
    check("no page parameter is ever sent", any(param(c[1], "page") for c in sess.calls), False)
    check("EARLY and MID levels are requested", param(sess.calls[0][1], "target_level"), ["EARLY", "MID"])
    check("the same job from two queries is one row", len(rows), 1)
    check("the public URL is id-slug", rows[0]["url"].endswith("/1-product-manager-ii-ads"), True)
    check("the qualifications are in the JD", "2 years of experience in product management" in rows[0]["jd"], True)
    full = [g_job(i, 1 + i * 0.2) for i in range(20)]
    gap = bigtech.fetch_google(session=FakeSession(lambda u, p: Resp(text=g_page(full, 80)))).truncated
    check("a full page ending under 12h old is a named gap", "page 1 only" in gap, True)
    spread = [g_job(i, 1 + i * 3) for i in range(20)]
    check("a full page reaching past 12h is not",
          bigtech.fetch_google(session=FakeSession(lambda u, p: Resp(text=g_page(spread, 80)))).truncated, "")
    check("a page without the blob is an alarm", raises(lambda: bigtech.fetch_google(
        session=FakeSession(lambda u, p: Resp(text="<html>consent</html>")))), "BoardError")

    # --- Apple ------------------------------------------------------------------
    print("apple: hydration data on the search and job pages")

    def a_item(pid, hours_ago, loc="Cupertino", managed=False, kind="REQ", title="Product Manager, Apple Pay"):
        stamp = (now - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S.123456789Z")
        return {"positionId": pid, "postingTitle": title, "transformedPostingTitle": "product-manager-apple-pay",
                "postDateInGMT": stamp, "managedPipelineRole": managed, "type": kind,
                "locations": [{"city": "", "stateProvince": "", "countryName": "United States of America",
                               "name": loc}]}

    def a_page(loader):
        blob = json.dumps(json.dumps({"loaderData": loader}))
        return f"<script>window.__staticRouterHydrationData = JSON.parse({blob});</script>"
    a_pages = {1: [a_item("1", 3), a_item("1", 3, loc="Sunnyvale"), a_item("2", 5, kind="PIPE"),
                   a_item("9", 0, managed=True, title="US - Specialist")],
               2: [a_item("3", 200), a_item("4", 300)]}
    sess = FakeSession(lambda u, p: Resp(text=a_page({"search": {
        "totalRecords": 60, "searchResults": a_pages.get(p["page"], [])}})))
    rows = bigtech.fetch_apple(slug='"product manager"', session=sess)
    check("the query goes out quoted, newest first, US only",
          (sess.calls[0][1]["search"], sess.calls[0][1]["sort"], sess.calls[0][1]["location"]),
          ('"product manager"', "newest", "united-states-USA"))
    check("the walk stops on the all-stale page", [c[1]["page"] for c in sess.calls], [1, 2])
    check("one posting listed in two cities is one row", [r["_id"] for r in rows], ["1", "2", "9"])
    check("a bare city gets its country so location_ok can read it",
          rows[0]["location"], "Cupertino, United States of America")
    check("the stamp is cut to seconds", rows[0]["posted_at"].endswith("Z") and len(rows[0]["posted_at"]), 20)
    check("a PIPE row without the managed flag keeps its date", rows[1]["posted_at"] != "", True)
    check("a managed-pipeline row's request-time stamp is unknown", rows[2]["posted_at"], "")
    detail = a_page({"jobDetails": {"jobsData": {
        "jobSummary": "Build Apple Pay.", "description": "Lead the roadmap.",
        "minimumQualifications": "3+ years of product management", "preferredQualifications": "MBA"}}})
    jd = bigtech.fetch_apple_jd(rows[0], session=FakeSession(lambda u, p: Resp(text=detail)))
    check("the detail JD holds the minimum qualifications", "3+ years of product management" in jd, True)
    check("a page without hydration data is an alarm", raises(lambda: bigtech.fetch_apple(
        session=FakeSession(lambda u, p: Resp(text="<html></html>")))), "BoardError")

    # --- Netflix ----------------------------------------------------------------
    print("netflix: Eightfold apply/v2, relevance order, the whole search set")
    midnight = int(datetime(now.year, now.month, now.day, tzinfo=timezone.utc).timestamp())

    def n_pos(i, t):
        return {"id": i, "name": f"Product Manager {i}", "locations": ["Los Gatos,California,United States of America"],
                "t_create": t, "canonicalPositionUrl": f"https://explore.jobs.netflix.net/careers/job/{i}"}
    n_pages = {0: [n_pos(1, midnight), n_pos(2, midnight - 86400 * 90)],
               10: [n_pos(3, midnight - 86400 * 120), n_pos(4, midnight - 86400)],
               20: [n_pos(5, midnight)]}
    sess = FakeSession(lambda u, p: Resp(payload={"count": 25, "positions": n_pages.get(p["start"], [])}))
    rows = bigtech.fetch_netflix(session=sess)
    check("no early stop: a fresh row behind stale ones is still found",
          [c[1]["start"] for c in sess.calls], [0, 10, 20])
    check("fresh rows from every page", [r["title"][-1] for r in rows], ["1", "4", "5"])
    check("a midnight-exact epoch is a date", rows[0]["posted_at"], now.date().isoformat())
    big = FakeSession(lambda u, p: Resp(payload={"count": 400, "positions": [n_pos(p["start"], midnight)]}))
    check("past the page cap is a named gap", "page cap" in bigtech.fetch_netflix(session=big).truncated, True)
    check("the page cap holds", len(big.calls), bigtech.NETFLIX_MAX_PAGES)
    jd = bigtech.fetch_netflix_jd(rows[0], session=FakeSession(lambda u, p: Resp(payload={
        "job_description": "<p>5+ years in product.</p>"})))
    check("the detail JD is plain text", jd, "5+ years in product.")

    # --- Intuit -----------------------------------------------------------------
    print("intuit: category pages for the list, JSON-LD for the date and JD")

    def i_page(total_pages, items):
        lis = "".join(
            f'<li data-intuit-jobid="1">\n  <a href="/job/mountain-view/pm-{i}/27595/{i}" class="sr-item">'
            f"<h2>Product Manager {i} &amp; Payments</h2>"
            f'<span class="job-location">Mountain View, California</span></a></li>' for i in items)
        return (f'<section id="search-results" data-total-pages="{total_pages}" data-total-results="9">'
                f'<ul class="search-list">{lis}</ul></section>')
    sess = FakeSession(lambda u, p: Resp(text=i_page(2, [1, 2] if u.endswith("/1/1") else [3])))
    rows = bigtech.fetch_intuit(session=sess)
    check("pages are walked by the /1/{page} path", [c[0][-4:] for c in sess.calls], ["/1/1", "/1/2"])
    check("titles are unescaped", rows[0]["title"], "Product Manager 1 & Payments")
    check("the full state name is kept for location_ok", rows[0]["location"], "Mountain View, California")
    check("the list date is unknown", rows[0]["posted_at"], "")
    ld = ('<script type="application/ld+json">{"@type": "JobPosting", "datePosted": "2026-03-06",'
          ' "description": "<p>2+ years of PM.</p>"}</script>')
    jd = bigtech.fetch_intuit_jd(rows[0], session=FakeSession(lambda u, p: Resp(text=ld)))
    check("the JD comes from JSON-LD", jd, "2+ years of PM.")
    check("and the posting date is written onto the row", rows[0]["posted_at"], "2026-03-06")
    check("intuit is flagged as dated only by its detail call", "intuit" in bigtech.DETAIL_DATED, True)
    check("a page without the results section is an alarm", raises(lambda: bigtech.fetch_intuit(
        session=FakeSession(lambda u, p: Resp(text="<html>moved</html>")))), "BoardError")

    print("registry")
    check("every detail fetcher has a list fetcher",
          set(bigtech.JD_DETAIL_FETCHERS) <= set(bigtech.FETCHERS), True)
    check("amazon and google need no detail call",
          {"amazon", "google"} & set(bigtech.JD_DETAIL_FETCHERS), set())

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
        return 1
    print("all bigtech tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
