"""Big-tech career sites that run their own portal (Layer 1 source, no login).

These companies sit in targets.yaml as `board: custom`, and before this module
their only route into queue/ was an incidental JobSpy hit. Each fetcher below
reads the JSON (or the server-rendered data) that the site's OWN front end
uses, with a plain GET and no key. Probed live on 2026-10-10; robots.txt
checked for every path used.

WORKING
  amazon     GET www.amazon.jobs/en/search.json. robots.txt blocks only
             /internal. Filters: base_query, normalized_country_code[]=USA,
             sort=recent (echoed back as CREATED_DATE DESCENDING), up to 100 a
             page. Full JD (description + basic + preferred qualifications) in
             the list. posted_date is a day ("October  9, 2026").
  microsoft  GET apply.careers.microsoft.com/api/pcsx/search (Eightfold PCSX;
             robots.txt: "Allow: /api/pcsx"). filter_profession = product
             management + program management, location United States,
             sort_by=timestamp: 140 results, 10 a page (num is ignored). JD from
             /api/pcsx/position_details. postedTs and creationTs are exact.
             (/api/apply/v2 answers 403 "Not authorized for PCSX" here.)
  google     GET www.google.com/about/careers/applications/jobs/results, the
             results PAGE: it server-renders the 20 rows inside an
             AF_initDataCallback('ds:1') blob, full JD and exact timestamps
             included. robots.txt (the `*` group) disallows every `page=`
             variant of that path, so ONLY PAGE 1 IS READ, once per query.
             sort_by=date, target_level=EARLY,MID and a QUOTED phrase are what
             make page 1 enough. Measured for the 20 newest US rows: "product
             manager" quoted, all levels ~1.5 days; quoted at EARLY+MID ~3.5
             days; unquoted at EARLY+MID under a day, with 5 of 20 rows in the
             PM family. "program manager" quoted at EARLY+MID spans ~2 days.
  apple      GET jobs.apple.com/en-us/search, server-rendered: the page carries
             window.__staticRouterHydrationData with 20 rows and totalRecords.
             jobs.apple.com has no robots.txt (it 301s to apple.com/pagenotfound),
             read as unrestricted. JD from the job page's own hydration data.
             postDateInGMT is exact.
  netflix    GET explore.jobs.netflix.net/api/apply/v2/jobs (Eightfold; robots.txt
             "Allow: /api/apply"). query + location filters, 10 a page; "product
             manager" in the US = 105 results. JD from /api/apply/v2/jobs/{id}.
             t_create is a midnight-exact epoch, i.e. a date. (PCSX answers 403
             "PCSX is not enabled for this user" on this tenant.)
  intuit     jobs.intuit.com is a Radancy (TalentBrew) site. Its JSON search,
             /search-jobs/, is DISALLOWED by robots.txt, so this reads the
             server-rendered category pages instead (allowed; 15 a page,
             "/category/product-management-jobs/27595/68351/1/{page}", 51 jobs
             in 4 pages). The list carries no date and is not date-sorted
             (measured: rows 4, 9 and 15 were posted 07-29, 03-06 and 09-23), so
             the date and the JD come from the job page's JSON-LD only.

NO PUBLIC ENDPOINT (not built)
  uber       The list page (/us/en/careers/list/?query=...) is a JS shell: it
             server-renders nav and one featured JD, not the results, and names
             no API path in its HTML, so results load client-side. robots.txt
             says "disallow: */api/" (and "*/custom-api/"), which covers any
             data call under www.uber.com. A plain GET without an Accept header
             answers 406.
             Uber Freight is a separate public Greenhouse board: `uberfreight`
             (78 jobs on 2026-10-10).
  meta       metacareers.com/robots.txt opens with "Collection of data on
             Facebook through automated means is prohibited unless you have
             express written permission". Not probed further.

NONE OF THESE RETURNS A WHOLE BOARD. Every fetcher is searched, filtered and/or
paginated, so none belongs in scrape.FULL_BOARD_FETCHERS: absence from a result
means "not in this slice", never "delisted". Each returns a BoardResult whose
`truncated` names a coverage gap (page cap hit while rows were still fresh),
the same duck-typed attribute poll_ats already reads from fetch_workday. Each
raises BoardError when the response loses its shape or a search that always
has hits comes back with none (a moved endpoint answering 200), the same split
fetch_workday makes between "nothing fresh" (normal) and "nothing at all".

Contract (scrape.FETCHERS): fetch_x(slug) -> [{title, location, url, posted_at,
jd}], plus `_detail` where the JD comes from a second call (the matching
fetch_x_jd(job) -> str goes in scrape.JD_DETAIL_FETCHERS). `slug` is optional
per-site config (a query, a category); None means the defaults below. `session`
is for tests. Pure fetch-and-normalise: no DB writes.
"""

from __future__ import annotations

import html
import json
import math
import re
import time
from datetime import datetime, timezone

import requests

import db
from vc_boards import _us_location, text_of

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
HTML_HEADERS = {**HEADERS, "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9"}
JSON_HEADERS = {**HEADERS, "Accept": "application/json"}
TIMEOUT = 20


class BoardError(RuntimeError):
    """The site answered but not in the shape this module reads."""


class BoardResult(list):
    """A list of jobs that can also say the fetch stopped short (see scrape.BoardResult)."""
    truncated = ""


def _stale(posted_at: str) -> bool:
    return db.is_provably_stale(posted_at, db.FETCH_WINDOW_HOURS)


def _epoch_iso(ts) -> str:
    """Epoch seconds -> ISO Z; a midnight-exact stamp is a date, not a time."""
    if not isinstance(ts, (int, float)) or ts <= 0:
        return ""
    dt = datetime.fromtimestamp(ts, timezone.utc)
    if (dt.hour, dt.minute, dt.second) == (0, 0, 0):
        return dt.date().isoformat()
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _queries(slug, default) -> list[str]:
    """A slug of 'a|b' runs two searches; None runs the defaults."""
    if not slug:
        return list(default)
    return [q.strip() for q in str(slug).split("|") if q.strip()]


def _sections(*pairs) -> str:
    """('Heading', html) pairs -> one plain-text JD, empty sections dropped."""
    parts = []
    for head, body in pairs:
        text = text_of(body or "")
        if text:
            parts.append(f"{head}\n{text}" if head else text)
    return "\n\n".join(parts)


# --- Amazon -----------------------------------------------------------------

AMAZON_URL = "https://www.amazon.jobs/en/search.json"
AMAZON_QUERIES = ("product manager",)
AMAZON_PAGE = 100
# sort=recent orders by CREATED_DATE, but the posted_date it shows is NOT
# monotonic in that order: measured over the first 100 "product manager" US rows
# on 2026-10-10, rows dated 10-08/10-09 sat at positions 73, 95 and 96 behind
# rows from late September. So the walk stops only on a whole page (100 rows)
# with nothing fresh on it, and 3 pages is the cap: page 1 alone held ~33 rows
# inside 72h, and the search's whole set was 523 rows.
AMAZON_MAX_PAGES = 3


def amazon_date(value: str) -> str:
    """'October  9, 2026' (double space before a 1-digit day) -> '2026-10-09'.

    Day precision, emitted as a bare date so db.job_age reasons over the whole
    day. Unparseable -> '' (unknown, never stale)."""
    s = re.sub(r"\s+", " ", (value or "").strip())
    try:
        return datetime.strptime(s, "%B %d, %Y").date().isoformat()
    except ValueError:
        return ""


def amazon_row(j: dict) -> dict:
    return {
        "title": (j.get("title") or "").strip(),
        "location": j.get("normalized_location") or j.get("location") or "",
        "url": "https://www.amazon.jobs" + (j.get("job_path") or ""),
        "posted_at": amazon_date(j.get("posted_date", "")),
        # The years ask lives in basic_qualifications ("5+ years of ..."), and
        # the hard filter and the scorer both read the JD for it.
        "jd": _sections(("", j.get("description")),
                        ("Basic qualifications", j.get("basic_qualifications")),
                        ("Preferred qualifications", j.get("preferred_qualifications"))),
    }


def fetch_amazon(slug=None, session=None) -> list[dict]:
    s = session or requests
    jobs, seen = BoardResult(), set()
    for query in _queries(slug, AMAZON_QUERIES):
        for page in range(AMAZON_MAX_PAGES):
            r = s.get(AMAZON_URL, params={
                "base_query": query, "normalized_country_code[]": "USA",
                "result_limit": AMAZON_PAGE, "offset": page * AMAZON_PAGE,
                "sort": "recent"}, headers=JSON_HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            body = r.json()
            if "jobs" not in body or body.get("error"):
                raise BoardError(f"amazon: no jobs key or an error ({body.get('error')!r})")
            if page == 0 and not body.get("hits"):
                raise BoardError(f"amazon: 0 hits for {query!r} in the US; "
                                 "the search parameters may have changed")
            fresh = 0
            for j in body["jobs"]:
                row = amazon_row(j)
                if _stale(row["posted_at"]):
                    continue
                fresh += 1
                if row["url"] not in seen and row["title"]:
                    seen.add(row["url"])
                    jobs.append(row)
            if not fresh or (page + 1) * AMAZON_PAGE >= int(body.get("hits") or 0):
                break
        else:
            jobs.truncated = (f"amazon {query!r}: page cap ({AMAZON_MAX_PAGES} x "
                              f"{AMAZON_PAGE}) hit with rows still fresh")
    return jobs


# --- Microsoft (Eightfold PCSX) ---------------------------------------------

MICROSOFT_HOST = "https://apply.careers.microsoft.com"
MICROSOFT_PROFESSIONS = ("product management", "program management")
MICROSOFT_PAGE = 10           # fixed by the API; `num` is ignored
# 140 results for the two professions in the US on 2026-10-10, strictly sorted
# by postedTs. 30 rows covered 10-10 back to 10-06, so a 72h poll is ~3 fresh
# pages plus the stale one that stops it. The cap covers the whole filtered set.
MICROSOFT_MAX_PAGES = 15
# Paced: back-to-back pages drew a 429 on 10-10 after an hour of probing.
PAGE_PAUSE = 1.5               # seconds between list pages on every paged site


# Microsoft answered the FIRST list page with a 429 on 11 of 14 detect runs
# (10-14 19Z to 10-15 17Z), so its roles only arrived via LinkedIn, hours late.
# A 429 is waited out and retried: Retry-After when the site sends one (capped),
# else the fixed waits below. The board fetches run concurrently, so the wait
# mostly overlaps the other boards' polls.
RETRY_429_WAITS = (15, 30)     # seconds
RETRY_AFTER_CAP = 60


def _get_429_retry(s, url, **kw):
    """GET that waits out a 429 up to len(RETRY_429_WAITS) times, then raises."""
    for wait in (*RETRY_429_WAITS, None):
        r = s.get(url, **kw)
        if r.status_code != 429 or wait is None:
            r.raise_for_status()
            return r
        after = str((getattr(r, "headers", None) or {}).get("Retry-After", ""))
        time.sleep(min(int(after), RETRY_AFTER_CAP) if after.isdigit() else wait)


def microsoft_row(p: dict) -> dict:
    return {
        "title": (p.get("name") or "").strip(),
        "location": _us_location(p.get("standardizedLocations") or p.get("locations")),
        "url": MICROSOFT_HOST + (p.get("positionUrl") or ""),
        # postedTs is what the site sorts on and shows. creationTs (the
        # requisition's creation) runs far earlier on many rows: of 30 PM/program
        # rows on 2026-10-10, 13 had a gap of 7 days or less (approval lag on a
        # new posting) and 9 had more than 60 days (one 253), which reads as a
        # refreshed repost. postedTs keeps genuinely new roles that waited a week
        # for approval; _created_at is carried so a caller can treat a long gap
        # as a repost.
        "posted_at": _epoch_iso(p.get("postedTs")),
        "_created_at": _epoch_iso(p.get("creationTs")),
        "jd": "",
        "_detail": (f"{MICROSOFT_HOST}/api/pcsx/position_details?"
                    f"position_id={p.get('id')}&domain=microsoft.com&hl=en"),
    }


def fetch_microsoft(slug=None, session=None) -> list[dict]:
    s = session or requests
    professions = _queries(slug, MICROSOFT_PROFESSIONS)
    jobs, seen = BoardResult(), set()
    for page in range(MICROSOFT_MAX_PAGES):
        if page:
            time.sleep(PAGE_PAUSE)
        params = [("domain", "microsoft.com"), ("location", "United States"),
                  ("start", page * MICROSOFT_PAGE), ("sort_by", "timestamp")]
        params += [("filter_profession", p) for p in professions]
        r = _get_429_retry(s, f"{MICROSOFT_HOST}/api/pcsx/search", params=params,
                           headers=JSON_HEADERS, timeout=TIMEOUT)
        data = (r.json() or {}).get("data") or {}
        if "positions" not in data:
            raise BoardError("microsoft: no data.positions in the search response")
        count = int(data.get("count") or 0)
        if page == 0 and not count:
            raise BoardError("microsoft: 0 results for the profession filter; "
                             "filter values may have been renamed")
        fresh = 0
        for p in data["positions"]:
            row = microsoft_row(p)
            if _stale(row["posted_at"]):
                continue
            fresh += 1
            if row["url"] not in seen and row["title"]:
                seen.add(row["url"])
                jobs.append(row)
        if not fresh or (page + 1) * MICROSOFT_PAGE >= count:
            break
    else:
        jobs.truncated = (f"microsoft: page cap ({MICROSOFT_MAX_PAGES} x "
                          f"{MICROSOFT_PAGE}) hit with rows still fresh")
    return jobs


def fetch_microsoft_jd(job: dict, session=None) -> str:
    r = _get_429_retry(session or requests, job["_detail"], headers=JSON_HEADERS,
                       timeout=TIMEOUT)
    return text_of(((r.json() or {}).get("data") or {}).get("jobDescription") or "")


# --- Google -----------------------------------------------------------------

GOOGLE_URL = "https://www.google.com/about/careers/applications/jobs/results"
GOOGLE_QUERIES = ('"product manager"', '"program manager"')
# Google's level facet. EARLY+MID is the 0-4 year band Vaish targets, and it is
# also what keeps page 1 (the only page robots.txt allows) wider than 72h.
GOOGLE_LEVELS = ("EARLY", "MID")
GOOGLE_PAGE = 20
# Page 2 is off limits, so the gap to watch is "more than 20 postings arrived
# between two polls". Page 1 always ends inside 72h for "program manager"
# (measured: ~2 days at EARLY+MID), so a 72h test would alarm on every poll.
# Detection runs every 2h; 12h allows for a missed night of cron dispatches.
GOOGLE_GAP_HOURS = 12
_AF_BLOB = re.compile(r"AF_initDataCallback\(\{key: '(ds:\d+)',.*?data:(.*?), sideChannel: \{\}\}\);",
                      re.S)
_SLUG = re.compile(r"[^a-z0-9]+")


def _google_html(cell) -> str:
    """Google wraps every text field as [null, "<html>"]."""
    if isinstance(cell, list) and len(cell) > 1 and isinstance(cell[1], str):
        return cell[1]
    return ""


def _at(j: list, i: int):
    return j[i] if len(j) > i else None


def _google_job_shaped(row) -> bool:
    """[id, title, ..., [9] locations, ..., [12] [epoch, nanos], ...]. 'ds:0' is
    the company facet ([[path, "DeepMind", "deepmind"], ...]) and also starts
    with two strings, so the check reaches the location list and the stamp."""
    return (isinstance(row, list) and len(row) > 12 and isinstance(row[0], str)
            and isinstance(row[1], str) and isinstance(row[9], list)
            and isinstance(row[12], list))


def google_payload(page: str) -> list:
    """The jobs blob: a list whose [0] is the rows and [2] the total match count.

    A blob with an empty row list is accepted only under key 'ds:1' (where the
    jobs have always been), so a genuinely empty search is told apart from a
    page that no longer carries the jobs at all."""
    for key, raw in _AF_BLOB.findall(page or ""):
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        rows = data[0] if isinstance(data, list) and data else None
        if not isinstance(rows, list):
            continue
        if (rows and _google_job_shaped(rows[0])) or (not rows and key == "ds:1"):
            return data
    raise BoardError("google: no jobs blob (AF_initDataCallback) on the results page")


def google_row(j: list) -> dict:
    jid, title = j[0], (j[1] or "").strip()
    places = [loc[0] for loc in (_at(j, 9) or []) if isinstance(loc, list) and loc]
    stamp = _at(j, 12)
    return {
        "title": title,
        "location": _us_location(places),
        "url": f"{GOOGLE_URL}/{jid}-{_SLUG.sub('-', title.lower()).strip('-')}",
        # [12], [13] and [14] are three epoch stamps; they were identical on
        # every row probed, so which is "created" and which "published" could
        # not be told apart. [12] is used.
        "posted_at": _epoch_iso(stamp[0]) if isinstance(stamp, list) and stamp else "",
        "jd": _sections(("", _google_html(_at(j, 10))),
                        ("Responsibilities", _google_html(_at(j, 3))),
                        ("Qualifications", _google_html(_at(j, 4)))),
    }


def fetch_google(slug=None, session=None) -> list[dict]:
    s = session or requests
    jobs, seen, gaps = BoardResult(), set(), []
    for query in _queries(slug, GOOGLE_QUERIES):
        params = [("q", query), ("location", "United States"), ("sort_by", "date")]
        params += [("target_level", lv) for lv in GOOGLE_LEVELS]
        r = s.get(GOOGLE_URL, params=params, headers=HTML_HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        data = google_payload(r.text)
        rows, total = data[0], (_at(data, 2) or 0)
        if not rows:
            raise BoardError(f"google: 0 results for {query!r}; the page shape or "
                             "the filters may have changed")
        oldest = ""
        for j in rows:
            row = google_row(j)
            oldest = row["posted_at"] or oldest
            if _stale(row["posted_at"]) or row["url"] in seen or not row["title"]:
                continue
            seen.add(row["url"])
            jobs.append(row)
        # A full page whose oldest row is younger than GOOGLE_GAP_HOURS means
        # postings may have scrolled off page 1 between polls; reported, not
        # chased, because page 2 is disallowed.
        if (len(rows) >= GOOGLE_PAGE and total > len(rows)
                and not db.is_provably_stale(oldest, GOOGLE_GAP_HOURS)):
            gaps.append(f"{query!r} page 1 ends at {oldest}, under {GOOGLE_GAP_HOURS}h old")
    if gaps:
        jobs.truncated = "google: " + "; ".join(gaps) + " (robots.txt allows page 1 only)"
    return jobs


# --- Apple ------------------------------------------------------------------

APPLE_HOST = "https://jobs.apple.com"
# QUOTED on purpose: measured 2026-10-10, search=product manager (unquoted)
# returned 4,268 of the 4,401 US jobs, i.e. no filter at all, while the quoted
# phrase returned 60, all product roles.
APPLE_QUERIES = ('"product manager"', '"program manager"')
APPLE_PAGE = 20
APPLE_MAX_PAGES = 4
_HYDRATION = re.compile(
    r'window\.__staticRouterHydrationData\s*=\s*JSON\.parse\(("(?:[^"\\]|\\.)*")\)', re.S)
_ISO_SECONDS = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})")


def apple_loader(page: str) -> dict:
    """The React router's loaderData: a JSON string literal holding JSON."""
    m = _HYDRATION.search(page or "")
    if not m:
        raise BoardError("apple: no __staticRouterHydrationData on the page")
    return json.loads(json.loads(m.group(1))).get("loaderData") or {}


def apple_iso(value: str) -> str:
    """'2026-10-10T01:55:07.434Z' (up to 9 fractional digits) -> second precision Z."""
    m = _ISO_SECONDS.match(value or "")
    return m.group(1) + "Z" if m else ""


def apple_location(locs) -> str:
    """'Cupertino' alone is not a US signal for location_ok; the entry also
    carries its state and country, so the place is written out in full."""
    for loc in locs or []:
        parts = [loc.get("city") or loc.get("name"), loc.get("stateProvince"),
                 loc.get("countryName")]
        text = ", ".join(p for p in parts if p)
        if text:
            return text
    return ""


def apple_row(item: dict) -> dict:
    pid = item.get("positionId") or ""
    # A managed-pipeline role (retail "US - Specialist: Seasonal") is stamped
    # with the REQUEST time: measured, its postDateInGMT matched the second the
    # page was fetched. That is not a posting date, so it is unknown. Keyed on
    # managedPipelineRole, NOT type == "PIPE": PIPE rows with the flag false
    # ("Product Manager, AI/ML - Apple Ads", 2026-10-07T20:04) carry real dates.
    pipeline = item.get("managedPipelineRole") is True
    return {
        "title": (item.get("postingTitle") or "").strip(),
        "location": apple_location(item.get("locations")),
        "url": f"{APPLE_HOST}/en-us/details/{pid}/{item.get('transformedPostingTitle') or ''}",
        "posted_at": "" if pipeline else apple_iso(item.get("postDateInGMT", "")),
        "jd": "",
        "_detail": f"{APPLE_HOST}/en-us/details/{pid}/{item.get('transformedPostingTitle') or ''}",
        "_pipeline": bool(pipeline),
        "_id": pid,
    }


def fetch_apple(slug=None, session=None) -> list[dict]:
    s = session or requests
    jobs, seen = BoardResult(), set()
    for query in _queries(slug, APPLE_QUERIES):
        for page in range(1, APPLE_MAX_PAGES + 1):
            r = s.get(f"{APPLE_HOST}/en-us/search",
                      params={"search": query, "sort": "newest",
                              "location": "united-states-USA", "page": page},
                      headers=HTML_HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            search = apple_loader(r.text).get("search") or {}
            if "searchResults" not in search:
                raise BoardError("apple: no searchResults in the loader data")
            total = int(search.get("totalRecords") or 0)
            if page == 1 and not total:
                raise BoardError(f"apple: 0 results for {query!r} in the US")
            fresh = 0
            for item in search["searchResults"]:
                row = apple_row(item)
                if _stale(row["posted_at"]):
                    continue
                if not row["_pipeline"]:      # a request-time stamp proves nothing
                    fresh += 1
                # One posting appears once per location; the first is kept, since
                # dedupe keys on company + title + location and two rows would
                # read as two jobs.
                if row["_id"] in seen or not row["title"]:
                    continue
                seen.add(row["_id"])
                jobs.append(row)
            if not fresh or page * APPLE_PAGE >= total:
                break
        else:
            jobs.truncated = (f"apple {query!r}: page cap ({APPLE_MAX_PAGES} x "
                              f"{APPLE_PAGE}) hit with rows still fresh")
    return jobs


def fetch_apple_jd(job: dict, session=None) -> str:
    r = (session or requests).get(job["_detail"], headers=HTML_HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    d = (apple_loader(r.text).get("jobDetails") or {}).get("jobsData") or {}
    if not d:
        raise BoardError("apple: no jobsData on the job page")
    return _sections(("", d.get("jobSummary")), ("Description", d.get("description")),
                     ("Minimum qualifications", d.get("minimumQualifications")),
                     ("Preferred qualifications", d.get("preferredQualifications")))


# --- Netflix (Eightfold apply/v2) -------------------------------------------

NETFLIX_HOST = "https://explore.jobs.netflix.net"
NETFLIX_QUERIES = ("product manager",)
NETFLIX_PAGE = 10             # the API returns 10 whatever `num` asks for
# NOT date-sorted, so there is no freshness stop: measured 2026-10-10, rows 1-20
# ran newest first (10-08 back to 06-04) and row 21 onward restarted at 05-15,
# 04-29, 03-24, 08-19 (relevance tiers, each date-sorted); sort_by=timestamp
# changed nothing. The whole search set is read: 105 rows = 11 requests. The cap
# leaves room for it to grow.
NETFLIX_MAX_PAGES = 15


def netflix_row(p: dict) -> dict:
    return {
        "title": (p.get("name") or "").strip(),
        "location": _us_location(p.get("locations") or [p.get("location")]),
        "url": (p.get("canonicalPositionUrl")
                or f"{NETFLIX_HOST}/careers/job/{p.get('id')}"),
        "posted_at": _epoch_iso(p.get("t_create")),
        "jd": "",
        "_detail": f"{NETFLIX_HOST}/api/apply/v2/jobs/{p.get('id')}?domain=netflix.com",
    }


def fetch_netflix(slug=None, session=None) -> list[dict]:
    s = session or requests
    jobs, seen = BoardResult(), set()
    for query in _queries(slug, NETFLIX_QUERIES):
        pages = NETFLIX_MAX_PAGES
        page = 0
        while page < pages:
            if page:
                time.sleep(PAGE_PAUSE)
            r = s.get(f"{NETFLIX_HOST}/api/apply/v2/jobs",
                      params={"domain": "netflix.com", "query": query,
                              "location": "United States", "start": page * NETFLIX_PAGE,
                              "num": NETFLIX_PAGE},
                      headers=JSON_HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            body = r.json() or {}
            if "positions" not in body:
                raise BoardError("netflix: no positions key in the response")
            count = int(body.get("count") or 0)
            if page == 0:
                if not count:
                    raise BoardError(f"netflix: 0 results for {query!r} in the US")
                pages = min(math.ceil(count / NETFLIX_PAGE), NETFLIX_MAX_PAGES)
                if math.ceil(count / NETFLIX_PAGE) > NETFLIX_MAX_PAGES:
                    jobs.truncated = (f"netflix {query!r}: {count} results, page cap "
                                      f"reads {NETFLIX_MAX_PAGES * NETFLIX_PAGE}")
            for p in body["positions"]:
                row = netflix_row(p)
                if _stale(row["posted_at"]) or row["url"] in seen or not row["title"]:
                    continue
                seen.add(row["url"])
                jobs.append(row)
            if not body["positions"]:
                break
            page += 1
    return jobs


def fetch_netflix_jd(job: dict, session=None) -> str:
    r = (session or requests).get(job["_detail"], headers=JSON_HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return text_of((r.json() or {}).get("job_description") or "")


# --- Intuit (Radancy category pages + JSON-LD) ------------------------------

INTUIT_HOST = "https://jobs.intuit.com"
INTUIT_CATEGORIES = ("product-management-jobs/27595/68351",)
INTUIT_MAX_PAGES = 6          # 51 PM jobs = 4 pages of 15 on 2026-10-10
_INTUIT_TOTAL = re.compile(r'data-total-pages="(\d+)"')
_INTUIT_ITEM = re.compile(r"<li[^>]*>\s*<a href=\"(/job/[^\"]+)\"(.*?)</li>", re.S)
_INTUIT_TITLE = re.compile(r"<h2>(.*?)</h2>", re.S)
_INTUIT_LOC = re.compile(r'class="job-location">(.*?)</span>', re.S)
_LD_JSON = re.compile(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", re.S)


def intuit_rows(page: str) -> tuple[list[dict], int]:
    """(rows, total pages) from one server-rendered category page."""
    if 'id="search-results"' not in (page or ""):
        raise BoardError("intuit: no search-results section on the category page")
    m = _INTUIT_TOTAL.search(page)
    rows = []
    for path, body in _INTUIT_ITEM.findall(page):
        title = _INTUIT_TITLE.search(body)
        loc = _INTUIT_LOC.search(body)
        rows.append({
            "title": html.unescape(title.group(1)).strip() if title else "",
            "location": html.unescape(loc.group(1)).strip() if loc else "",
            "url": INTUIT_HOST + path,
            # Unknown here: the list has no date. fetch_intuit_jd fills it from
            # the job page, after the cheap filters have run.
            "posted_at": "",
            "jd": "",
            "_detail": INTUIT_HOST + path,
        })
    return rows, int(m.group(1)) if m else 1


def fetch_intuit(slug=None, session=None) -> list[dict]:
    s = session or requests
    jobs, seen = BoardResult(), set()
    for category in _queries(slug, INTUIT_CATEGORIES):
        pages, page = 1, 1
        while page <= min(pages, INTUIT_MAX_PAGES):
            r = s.get(f"{INTUIT_HOST}/category/{category}/1/{page}",
                      headers=HTML_HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            rows, pages = intuit_rows(r.text)
            if page == 1 and not rows:
                raise BoardError(f"intuit: category {category!r} listed 0 jobs")
            for row in rows:
                if row["url"] not in seen and row["title"]:
                    seen.add(row["url"])
                    jobs.append(row)
            page += 1
        if pages > INTUIT_MAX_PAGES:
            jobs.truncated = (f"intuit {category!r}: {pages} pages, cap reads "
                              f"{INTUIT_MAX_PAGES}")
    return jobs


def fetch_intuit_jd(job: dict, session=None) -> str:
    """The JD from the job page's JSON-LD; ALSO sets job['posted_at'] from it.

    The category list carries no date, so this is the only place one exists.
    poll_ats inserts with job.get('posted_at') after calling the detail fetcher,
    so the real date is recorded. The freshness gate has already run by then,
    on an unknown date, which is why the first poll admits the PM backlog."""
    r = (session or requests).get(job["_detail"], headers=HTML_HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    for block in _LD_JSON.findall(r.text):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        for obj in data if isinstance(data, list) else [data]:
            if isinstance(obj, dict) and obj.get("@type") == "JobPosting":
                posted = str(obj.get("datePosted") or "")
                if re.match(r"\d{4}-\d{2}-\d{2}$", posted):
                    job["posted_at"] = posted
                return text_of(obj.get("description") or "")
    raise BoardError("intuit: no JobPosting JSON-LD on the job page")


# Boards whose posting date arrives only with the JD (fetch_intuit_jd sets
# job["posted_at"]). poll_ats checks freshness BEFORE the detail call, so for
# these the check has to be repeated after it; otherwise the first poll queues
# the whole PM backlog (51 rows on 2026-10-10, some posted in March).
DETAIL_DATED = {"intuit"}

FETCHERS = {"amazon": fetch_amazon, "microsoft": fetch_microsoft, "google": fetch_google,
            "apple": fetch_apple, "netflix": fetch_netflix, "intuit": fetch_intuit}
JD_DETAIL_FETCHERS = {"microsoft": fetch_microsoft_jd, "apple": fetch_apple_jd,
                      "netflix": fetch_netflix_jd, "intuit": fetch_intuit_jd}
