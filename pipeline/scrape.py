"""Layer 1 detection: poll ATS boards + JobSpy discovery, dedupe, write queue/.

Deterministic, zero-LLM. Survivors of the targets.yaml title/location filters
that pass dedupe are inserted with status 'new' and written to queue/<id>.json
for Layer 2 to score. Hard content filters (sponsorship/YOE/etc.) are milestone 2.

Usage:
    python pipeline/scrape.py                 # full run (ATS + JobSpy, last 24h)
    python pipeline/scrape.py --no-jobspy     # ATS boards only (fast)
    python pipeline/scrape.py --hours-old 72  # widen JobSpy window (first run)
"""

import argparse
import html
import json
import math
import os
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db
import filter as hardfilter
import ingest_scores
import bigtech
import vc_boards

REPO_ROOT = Path(__file__).resolve().parent.parent
TARGETS_YAML = REPO_ROOT / "profile" / "targets.yaml"
QUEUE_DIR = REPO_ROOT / "queue"

# Ashby's API silently hangs without a real User-Agent (learned 2026-08-11).
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) job-search-os/1.0"}
TIMEOUT = 20

# term -> LinkedIn results_wanted. Indeed was dropped 2026-10-09 (Vaish's call).
# 2026-10-08: LinkedIn's relevance sort plus a 25-result cap hid most fresh
# postings (see jobspy_window). Unique unseen on-target rows each term added in
# a 4h LinkedIn window that day: product owner 38, product manager 29, TPM 26,
# FDE 24, APM 20, AI PM 17, platform PM 17, technical PM 13, developer
# relations 6, product analyst 4. Depth is per term because every 10 results is
# a list page behind a 3-7s JobSpy sleep. "program manager" (+26) was left out:
# mostly construction, defence and healthcare programs the scorer would only
# skip. FDE, developer relations and product analyst are the adjacent families
# Vaish approved 09-28.
SEARCH_TERMS = {
    "product manager": 100,
    "product owner": 60,
    "associate product manager": 50,
    "technical program manager": 50,
    "AI product manager": 40,
    "platform product manager": 40,
    "technical product manager": 40,
    "forward deployed engineer": 40,
    "product analyst": 30,
    "developer relations": 30,
}
# Reposts (Vaish 2026-10-09: skip them). Logged out, LinkedIn hides its
# "Reposted" tag and shows the repost time as the posting time, but its job IDs
# are issued in order (~410K a day, measured from first_seen maxima 09-25 to
# 10-09), so a listing whose ID trails the newest one this run saw by more than
# ~3 days is an older posting relisted. On 10-09 about half the rows LinkedIn
# returned for "past 24h" trailed by a week or more (Atlassian's Rovo PM by ~7
# weeks, confirmed on Atlassian's own careers API).
LINKEDIN_REPOST_ID_GAP = 1_200_000
LINKEDIN_REPOST_MIN_SAMPLE = 20   # below this the run's newest ID is no anchor
_LINKEDIN_ID = re.compile(r"/view/(?:[^/?#]*-)?(\d{8,})")


def linkedin_job_id(url: str) -> int | None:
    m = _LINKEDIN_ID.search(url or "")
    return int(m.group(1)) if m else None


def linkedin_reposts(urls) -> set:
    """The URLs among `urls` whose job ID trails the newest by more than the gap."""
    ids = {u: linkedin_job_id(u) for u in urls}
    known = [i for i in ids.values() if i]
    if len(known) < LINKEDIN_REPOST_MIN_SAMPLE:
        return set()
    floor = max(known) - LINKEDIN_REPOST_ID_GAP
    return {u for u, i in ids.items() if i and i < floor}


def jobright_is_repost(item_or_html) -> bool:
    """Jobright's own flag: `jobResult.repost` in the app feed and in every
    detail page's __NEXT_DATA__ (`"repost":false` on all 15 probed 10-09)."""
    if isinstance(item_or_html, dict):
        return bool(item_or_html.get("repost"))
    return bool(re.search(r'"repost"\s*:\s*true', item_or_html or ""))


def _record_repost(conn, s: dict, company: str, title: str, location: str,
                   url: str, source: str, posted: str, why: str) -> None:
    """Recorded as skipped, never dropped, so dedupe holds and it is never refetched."""
    db.insert_job(conn, company, title, location, url, source, posted_at=posted,
                  status="skipped", notes=f"repost ({why})")
    s["repost"] = s.get("repost", 0) + 1


# Aggregators relist old roles with a fresh date (Vaish 2026-10-15). On 10-08 to
# 10-10 LinkedIn and Jobright brought in Anthropic's TPM Hardware (first published
# 08-05), Vercel's PM Compute (09-28), a fintech's Procurement ProdOps (09-23) and
# Perplexity's PM Builder (04-21) as new; each company's own board, polled earlier
# in the same run, had the real date and had rightly skipped them. poll_ats now
# notes the NEWEST posting date of every on-target role on a full board, and an
# aggregator row whose role's newest copy there is past the fetch window is a
# repost. Full boards only: a search or a capped page walk (Workday, the big-tech
# sites) can miss a fresh copy of a same-titled role, which would turn a new
# requisition into a false repost. A role absent from the board says nothing.
_BOARD_NEWEST: dict = {}


def note_board_posting(board: str, company: str, title: str, posted_at: str) -> None:
    if board not in FULL_BOARD_FETCHERS:
        return
    when = db.parse_posted_at(posted_at or "")
    if when is None:
        return
    key = db.role_key(company, title)
    prev = _BOARD_NEWEST.get(key)
    if prev is None or when > prev[0]:
        _BOARD_NEWEST[key] = (when, posted_at)


def board_repost(company: str, title: str) -> str | None:
    """The repost reason when the company's own full board dates this role old."""
    seen = _BOARD_NEWEST.get(db.role_key(company, title))
    if seen and db.is_provably_stale(seen[1], db.FETCH_WINDOW_HOURS):
        return f"on the company's own board since {seen[1][:10]}"
    return None
# LinkedIn cannot be date-sorted logged out (sortBy is ignored), and its window
# is a firehose ("product manager": 128 listings in 10 min, 364 in 30), so it is
# read relevance-first inside a recent window, Vaish's call: at least 8h.
LINKEDIN_WINDOW_MIN, LINKEDIN_WINDOW_MAX = 8, 24
LINKEDIN_WORKERS = 3
LINKEDIN_DETAIL_WORKERS = 2
LINKEDIN_DETAIL_TRIES = 4
LINKEDIN_429_WAIT = 3     # seconds, times the attempt number

_TAG_RE = re.compile(r"<[^>]+>")

# "Remote" alone used to pass the US filter, which let "Remote - Canada" through.
# High-confidence non-US markers only: a posting listing both a US city and a
# foreign one still passes, because the US role is real. Ambiguity favors keeping
# the job (a wrongly dropped posting is invisible forever; a wrongly kept one
# costs one cheap scoring call).
# A hand-picked shortlist was the wrong shape here: "Remote - Colombia" (a real
# Twilio posting) slipped through simply because Colombia was not on it. Listing
# countries exhaustively is the only version that does not quietly leak.
_NON_US_PLACES = [
    # Americas (excl. US)
    "canada", "toronto", "vancouver", "montreal", "ontario", "quebec", "calgary",
    "mexico", "brazil", "sao paulo", "argentina", "chile", "colombia", "bogota",
    "peru", "costa rica", "panama", "uruguay", "ecuador", "guatemala",
    # Europe
    "united kingdom", "england", "scotland", "wales", "london", "manchester",
    "ireland", "dublin", "germany", "berlin", "munich", "hamburg", "france",
    "paris", "spain", "madrid", "barcelona", "portugal", "lisbon", "italy",
    "rome", "milan", "netherlands", "amsterdam", "belgium", "brussels",
    "switzerland", "zurich", "geneva", "austria", "vienna", "sweden",
    "stockholm", "norway", "oslo", "denmark", "copenhagen", "finland",
    "helsinki", "poland", "warsaw", "krakow", "czech", "prague", "hungary",
    "budapest", "romania", "bucharest", "bulgaria", "greece", "athens",
    "ukraine", "serbia", "croatia", "estonia", "lithuania", "latvia",
    # Middle East / Africa
    "israel", "tel aviv", "united arab emirates", "dubai", "abu dhabi",
    "saudi arabia", "qatar", "egypt", "cairo", "south africa", "cape town",
    "johannesburg", "nigeria", "lagos", "kenya", "nairobi", "morocco", "turkey",
    "istanbul",
    # Asia-Pacific
    "india", "bangalore", "bengaluru", "hyderabad", "pune", "mumbai", "delhi",
    "gurgaon", "gurugram", "noida", "chennai", "china", "beijing", "shanghai",
    "shenzhen", "hong kong", "taiwan", "taipei", "japan", "tokyo", "osaka",
    "korea", "seoul", "singapore", "malaysia", "kuala lumpur", "indonesia",
    "jakarta", "thailand", "bangkok", "vietnam", "hanoi", "philippines",
    "manila", "australia", "sydney", "melbourne", "brisbane", "perth",
    "new zealand", "auckland", "pakistan", "bangladesh", "sri lanka",
]
NON_US = re.compile(
    r"(?i)(" + r"|".join(r"\b" + p.replace(" ", r"\s+") + r"\b" for p in _NON_US_PLACES)
    + r"|\bEMEA\b|\bAPAC\b|\bLATAM\b|\bLATM\b)")


# A US signal that deliberately excludes the word "remote" — targets.yaml's
# location_filter counts "remote" as a keep signal, which would make
# "Remote - Canada" look American.
# W3a (2026-10-09): every state by code and by name, and D.C. The old list held
# 25 codes and no names, so "Mountain View, California", "Philadelphia,
# Pennsylvania", "Washington, D.C." and "Honolulu, HI" were all dropped. Codes
# match upper case only: matched case-insensitively, "OR" and "IN" hit English
# words, so "London or Remote" read as a US posting.
_US_STATE_CODES = (
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT "
    "NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY PR").split()
_US_STATE_NAMES = [
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut",
    "delaware", "district of columbia", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi", "missouri",
    "montana", "nebraska", "nevada", "new hampshire", "new jersey", "new mexico",
    "new york", "north carolina", "north dakota", "ohio", "oklahoma", "oregon",
    "pennsylvania", "rhode island", "south carolina", "south dakota", "tennessee",
    "texas", "utah", "vermont", "virginia", "washington", "west virginia",
    "wisconsin", "wyoming", "puerto rico"]
US_SIGNAL = re.compile(
    r"(?i)(united states|\bU\.?S\.?A?\b|\bD\.C\.|"
    r"(?-i:\b(" + "|".join(_US_STATE_CODES) + r")\b)|"
    + "|".join(r"\b" + n.replace(" ", r"\s+") + r"\b" for n in _US_STATE_NAMES)
    + r"|san francisco|seattle|austin|boston|"
    r"chicago|denver|atlanta|los angeles|salt lake|portland|miami|dallas|phoenix|"
    r"raleigh|nashville|columbus|charlotte)")

# Beside a foreign place, a bare code is often a country prefix, not a state:
# Visa's Workday writes "IN - Bengaluru, India", and IN read as Indiana (3 rows
# leaked 10-10). There a code counts only as "City, ST".
US_SIGNAL_BESIDE_FOREIGN = re.compile(
    US_SIGNAL.pattern.replace(r"(?-i:\b(", r"(?-i:,\s*(", 1))


def foreign_only(location: str) -> bool:
    """A foreign place is named and no US place is named beside it."""
    return bool(NON_US.search(location)) and not US_SIGNAL_BESIDE_FOREIGN.search(location)

# A location that names no place: Cloudflare's "Hybrid" / "Distributed" /
# "In-Office", Linear's "North America", "Multiple Locations". Before W3a these
# fell through to location_filter and were dropped, so Cloudflare's own board had
# never produced a row. They are unknown, and unknown is kept (the scorer judges).
_PLACELESS_WORDS = re.compile(
    r"(?i)\b(hybrid|remote|distributed|in[\s-]?office|on[\s-]?site|office|flexible|"
    r"multiple|various|several|locations?|north\s+america|americas|anywhere|global|"
    r"worldwide|\d+)\b")


def placeless(location: str) -> bool:
    """True when the string holds only work-mode words, counts and punctuation."""
    return not re.sub(r"[\W_]+", "", _PLACELESS_WORDS.sub("", location))


# Indeed matches loosely, so a "product manager" search returns propulsion
# engineers and automotive technicians. This keeps anything plausibly in the
# PM/TPM family, including the adjacent lanes preferences.md names (product
# operations, product delivery). Non-matches are NOT discarded: they are stored
# with status 'skipped' so dedupe still holds and the decision stays auditable.
JOBSPY_RELEVANT = re.compile(
    r"(?i)((product|program|platform)\s*(manager|owner|management|lead)"
    r"|\b(APM|TPM)\b"
    r"|product\s+(manage|operations|delivery|analyst)"
    r"|program\s+manage"
    r"|forward[\s-]+deployed|developer\s+(relations|advocate)|\bdevrel\b)")


def cell(row, key: str) -> str:
    """pandas leaves NaN in empty cells, and NaN is truthy, so `x or ''` keeps it
    and str() turns it into the literal string 'nan'. Guard on the repr instead."""
    v = row.get(key)
    if v is None:
        return ""
    s = str(v).strip()
    return "" if s.lower() in ("nan", "none", "<na>", "nat") else s


def location_ok(location: str, loc_re: re.Pattern) -> bool:
    """Keep US and unknown locations; drop only clearly-foreign-only ones."""
    if not location.strip():
        return True                      # unknown location: let the scorer judge
    if NON_US.search(location):
        # A foreign location is named. Keep it only if a US location is named too
        # (multi-region postings like "US-Chicago; CA-Toronto" are real US roles).
        return not foreign_only(location)
    return (bool(loc_re.search(location)) or "remote" in location.lower()
            or bool(US_SIGNAL.search(location)) or placeless(location))


def strip_html(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"</(p|div|li|ul|ol|h[1-6]|br)[^>]*>", "\n", text, flags=re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def fetch_greenhouse(slug: str) -> list[dict]:
    r = requests.get(
        f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
        params={"content": "true"}, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    jobs = []
    for j in r.json().get("jobs", []):
        jobs.append({
            "title": j.get("title", ""),
            "location": (j.get("location") or {}).get("name", ""),
            "url": j.get("absolute_url", ""),
            # first_published is the ORIGINAL posting date; updated_at is the
            # last edit and makes a months-old role look like it went up this
            # week. Since 2026-08-15 there is NO updated_at fallback: with a
            # freshness window at ingest, that fallback would be load-bearing
            # and would wave every edited-today ancient role straight through.
            # Absent first_published now reads as unknown, and unknown is never
            # treated as stale, so the role still reaches the scorer.
            "posted_at": j.get("first_published", ""),
            "jd": strip_html(j.get("content", "")),
        })
    return jobs


def fetch_lever(slug: str) -> list[dict]:
    r = requests.get(f"https://api.lever.co/v0/postings/{slug}",
                     params={"mode": "json"}, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    jobs = []
    for j in r.json():
        cats = j.get("categories") or {}
        jobs.append({
            "title": j.get("text", ""),
            "location": cats.get("location", "") or "",
            "url": j.get("hostedUrl", ""),
            "posted_at": str(j.get("createdAt", "")),
            "jd": j.get("descriptionPlain", "") or strip_html(j.get("description", "")),
        })
    return jobs


def fetch_ashby(slug: str) -> list[dict]:
    r = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
                     headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    jobs = []
    for j in r.json().get("jobs", []):
        jobs.append({
            "title": j.get("title", ""),
            "location": j.get("location", "") or "",
            "url": j.get("jobUrl", "") or j.get("applyUrl", ""),
            "posted_at": j.get("publishedAt", ""),
            "jd": strip_html(j.get("descriptionHtml", "")) or j.get("descriptionPlain", ""),
        })
    return jobs


# --- Workday ---------------------------------------------------------------
# Workday is the biggest ATS the target list touches (Mastercard, Capital One,
# PayPal, Visa, Adobe, Salesforce, NVIDIA), and unlike the three above it has no
# "give me the board" endpoint. Its career sites are backed by a public JSON
# API — POST /wday/cxs/{tenant}/{site}/jobs — which the site's own front-end
# calls, so no scraping and no key.
#
# The slug encodes the three things that URL needs, slash-separated and in the
# order they appear in it: "tenant/wd-host/site", e.g. "mastercard/wd1/
# CorporateCareers" -> https://mastercard.wd1.myworkdayjobs.com/wday/cxs/
# mastercard/CorporateCareers/jobs. Splitting on "/" rather than inventing a
# separator keeps the slug copy-pasteable straight out of the careers URL, which
# is where a human verifying it will be looking. The wd-host number is NOT
# guessable and differs per tenant (Capital One is wd12, Visa wd5, Mastercard
# wd1); `curl https://{tenant}.{host}.myworkdayjobs.com/robots.txt` lists the
# tenant's real site names and is the fastest way to verify a new one.
WORKDAY_SLUG_PARTS = 3

# Workday rejects limit > 20 with HTTP 400 (probed 2026-08-23 across tenants),
# so paging is the only way through a board and 20 is not a tunable.
WORKDAY_PAGE = 20
# Results come back sorted by posting date DESCENDING, which is what makes
# polling a 1,400-job board cheap: page until everything on a page is provably
# stale, then stop. The offset cap is the backstop for the case that reasoning
# cannot cover — a board whose postedOn values are missing or unparseable maps
# them all to "unknown", and unknown is never stale, so nothing would ever end
# the loop.
#
# 400 (raised from 200 on 2026-09-12). Capital One needs 15 pages = offset 300
# to reach its first all-stale page; at 200 it was TRUNCATED every hour and said
# so in a `##[warning]` that feeds neither `errors=` nor DEGRADED_MIN_SOURCES,
# so a real hole at the highest-yield employer on the list was invisible on
# every clock. Measured that day: 288 fresh rows, 18 PM/TPM-titled.
WORKDAY_MAX_OFFSET = 400

# WHY THERE IS NO searchText (removed 2026-09-12 — this is the load-bearing part).
# The old code passed searchText="product manager" / "technical program manager"
# to narrow the paging, on the belief — written into the comment above as
# "verified 2026-08-23 with and without searchText" — that the result set stays
# date-sorted either way. That was verified on a tenant where it happens to hold
# and generalised to all seven. It does not hold: searchText switches PayPal from
# a date sort to a RELEVANCE rank. Measured 2026-09-12 over 60 rows at offsets
# 0/20/40: PayPal with searchText = 25 inversions, PayPal without = 0; Capital
# One = 0 either way.
#
# A relevance-ranked page almost always carries something inside the 72h window,
# so `fresh_on_page == 0` never fires, the loop can only ever end at the offset
# cap, and it did — every hour, for both terms, while yielding ZERO PM-titled
# rows on PayPal. Raising the cap alone would have made that worse, not better:
# it would page a whole board forever to buy nothing.
#
# Dropping searchText is cheaper AND more correct AND finds at least as much:
# one stream per tenant instead of two, every tenant terminates on freshness
# (measured: 1-15 pages, 2-16 requests), and Capital One's completed poll returns
# the same 18 PM/TPM-titled rows the truncated one did, minus the truncation.
# targets.yaml's title_filter was always what actually decided — searchText only
# ever narrowed the paging, and it narrowed it wrong.

_WD_TODAY = re.compile(r"(?i)^posted\s+today$")
_WD_YESTERDAY = re.compile(r"(?i)^posted\s+yesterday$")
_WD_DAYS = re.compile(r"(?i)^posted\s+(\d+)\+?\s*days?\s+ago$")
# "3 Locations" / "2 Locations" — the board declining to name a place on a
# multi-city posting. Not a location.
_WD_MULTI_LOC = re.compile(r"(?i)^\d+\s+locations?$")


def workday_posted_date(posted_on: str, today=None) -> str:
    """Workday's human 'postedOn' string -> a date-only 'YYYY-MM-DD', or ''.

    Workday never exposes a posting TIMESTAMP on the list endpoint, only
    "Posted Today" / "Posted Yesterday" / "Posted 2 Days Ago" / "Posted 30+ Days
    Ago". That is genuinely day-resolution information, so it is emitted as a
    bare date and nothing more: db.job_age() keys off a 10-character value to
    return precision "day" and reason over a 24h-wide interval. Appending a
    fabricated "T00:00:00Z" would present it as precision "exact" and make the
    freshness gate reject roles that are actually inside the window.

    An unrecognised string returns "" (unknown), never a guess — unknown is
    never stale, so the job survives to the scorer instead of vanishing.
    """
    s = (posted_on or "").strip()
    today = today or datetime.now(timezone.utc).date()
    if _WD_TODAY.match(s):
        return today.isoformat()
    if _WD_YESTERDAY.match(s):
        return (today - timedelta(days=1)).isoformat()
    m = _WD_DAYS.match(s)
    if m:
        # "30+" is a bucket, not a number. Clamping to its lower bound is the
        # conservative read (youngest the posting could be) and lands far
        # outside the fetch window either way.
        return (today - timedelta(days=min(int(m.group(1)), 3650))).isoformat()
    return ""


def workday_location(locations_text: str) -> str:
    """'3 Locations' means the board named no place; everything else passes through.

    location_ok() keeps US and UNKNOWN locations and drops the rest, so a cell
    that is not a place has to arrive as empty. Left as-is it matches neither
    the US signal nor "remote" and the row is silently dropped — which would
    lose exactly the multi-city postings a national search wants most.
    """
    s = (locations_text or "").strip()
    return "" if _WD_MULTI_LOC.match(s) else s


def _workday_urls(slug: str) -> tuple[str, str]:
    """(cxs base, public site base) for a 'tenant/wd-host/site' slug."""
    parts = [p for p in (slug or "").split("/") if p]
    if len(parts) != WORKDAY_SLUG_PARTS:
        raise ValueError(
            f"workday slug must be 'tenant/wd-host/site', got {slug!r}")
    tenant, host, site = parts
    root = f"https://{tenant}.{host}.myworkdayjobs.com"
    return f"{root}/wday/cxs/{tenant}/{site}", f"{root}/{site}"


class BoardResult(list):
    """A list of jobs that can also report that the fetch stopped short.

    A paginated fetcher has two ways to end its loop and they mean opposite
    things: reaching the freshness boundary means coverage is COMPLETE, while
    hitting the page cap means there were more recent jobs it never asked for.
    A plain list cannot tell the caller which happened, so the second one would
    be an invisible coverage gap repeating every hour — the failure CLAUDE.md
    singles out ("a coverage gap nobody can see is one nobody closes"), and the
    same reason `custom` boards get a stats row instead of a bare `continue`.
    Still a list, so every existing caller is unaffected.
    """
    truncated = ""


def fetch_workday(slug: str) -> list[dict]:
    """Fresh-window slice of a Workday board. JD is fetched later, per surviving row.

    Unlike the other three fetchers this does NOT return the whole board — the
    boards are 500-1,400 jobs and the API caps a page at 20. It walks the
    date-sorted pages until they fall out of the fetch window. That is why
    `workday` is absent from FULL_BOARD_FETCHERS: absence from this result means
    "not recent", not "delisted", and the sweep must never confuse the two.
    """
    cxs, public = _workday_urls(slug)
    jobs, seen = BoardResult(), set()
    postings_seen = 0
    offset, stale_pages = 0, 0
    while offset < WORKDAY_MAX_OFFSET:
        r = requests.post(f"{cxs}/jobs", headers={**HEADERS,
                          "Content-Type": "application/json",
                          "Accept": "application/json"},
                          json={"appliedFacets": {}, "limit": WORKDAY_PAGE,
                                "offset": offset},
                          timeout=TIMEOUT)
        r.raise_for_status()
        page = r.json().get("jobPostings", [])
        if not page:
            break
        postings_seen += len(page)
        fresh_on_page = 0
        for j in page:
            posted = workday_posted_date(j.get("postedOn", ""))
            if db.is_provably_stale(posted, db.FETCH_WINDOW_HOURS):
                continue
            fresh_on_page += 1
            path = j.get("externalPath", "")
            if not path or path in seen:
                continue
            seen.add(path)
            jobs.append({
                "title": j.get("title", ""),
                "location": workday_location(j.get("locationsText", "")),
                "url": f"{public}{path}",
                "posted_at": posted,
                "jd": "",              # filled by fetch_workday_jd, if the
                                       # row survives the cheap filters
                "_detail": f"{cxs}{path}",
            })
        # Date-sorted, so a page with nothing fresh on it means every later page
        # is older still. This is the loop's real exit; the offset cap only
        # catches boards whose dates do not parse.
        #
        # TWO consecutive all-stale pages, not one. The sort is descending but
        # not strictly so — measured 2026-09-12 without searchText, 4 of 7
        # tenants carry a few inversions (NVIDIA 19 in 60 rows), because Workday
        # orders by a posting date it then renders to day precision. One unlucky
        # page would end the walk early and drop the fresh rows behind it, and a
        # wrongly-dropped job is invisible forever. A second page costs one
        # request; the asymmetry is the whole rule.
        stale_pages = stale_pages + 1 if fresh_on_page == 0 else 0
        if stale_pages >= 2:
            break
        offset += WORKDAY_PAGE
    else:
        # The `while` condition ended the loop, not the freshness break: we ran
        # out of page budget while rows were STILL inside the window, so there
        # are recent postings on this board we never asked for. Says so out loud
        # rather than looking like a complete poll.
        jobs.truncated = (f"offset cap ({WORKDAY_MAX_OFFSET}) hit; fresh window "
                          f"NOT fully covered - raise WORKDAY_MAX_OFFSET")
    if postings_seen == 0:
        # An empty RETURN is normal here — a small board with nothing posted in
        # the last 72h (PayPal, measured 2026-08-23) legitimately yields zero,
        # which is why poll_ats's "0 jobs = error" rule is scoped to full-board
        # fetchers. An empty BOARD is not normal: it is the renamed-tenant /
        # moved-site failure that answers 200 and looks like quiet hiring. Only
        # this function can tell the two apart, so it raises rather than letting
        # the caller guess, and lands in the ordinary per-source error report.
        raise RuntimeError("board returned 0 postings on its first page; "
                           "tenant/site may have moved (check robots.txt)")
    return jobs


def fetch_workday_jd(job: dict) -> str:
    """Per-job description. Deliberately NOT called for every row on the board.

    The list endpoint carries no description, and these boards run to four
    figures, so fetching detail up front would mean ~1,400 requests an hour per
    tenant. poll_ats calls this only after a row has passed the title filter,
    the location filter, the freshness window AND dedupe — by then it is a
    handful of genuinely new PM roles.
    """
    r = requests.get(job["_detail"], headers={**HEADERS, "Accept": "application/json"},
                     timeout=TIMEOUT)
    r.raise_for_status()
    return strip_html((r.json().get("jobPostingInfo") or {}).get("jobDescription", ""))


FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever,
            "ashby": fetch_ashby, "workday": fetch_workday}

# Boards whose fetcher returns the ENTIRE board in one poll. The delisted sweep
# (sweep_delisted) reads "in the DB but not in this fetch" as "the employer
# closed the posting", and that inference is sound ONLY for these — which is
# also why it is a named set rather than an `!= "workday"` check.
#
# READ THIS BEFORE ADDING A NAME: if one of these fetchers ever gains
# pagination, a freshness cut-off, or a search term, it stops returning the
# whole board and the sweep silently turns into a mass-deleter of every job past
# the first page. A fetcher belongs here only while it can honestly answer "is
# this posting still up?" for every row it has ever produced.
FULL_BOARD_FETCHERS = {"greenhouse", "lever", "ashby"}

# JD arrives with the listing on every board except Workday, whose list endpoint
# has no description field at all.
JD_DETAIL_FETCHERS = {"workday": fetch_workday_jd}

# W3c (2026-10-10): Amazon, Microsoft, Google, Apple, Netflix and Intuit career
# sites, read through the public endpoints their own pages use (pipeline/bigtech.py
# has each endpoint, its robots.txt check and the probe date). Every one is a
# search or a capped page walk, so none may join FULL_BOARD_FETCHERS.
FETCHERS.update(bigtech.FETCHERS)
JD_DETAIL_FETCHERS.update(bigtech.JD_DETAIL_FETCHERS)

# Microsoft stamps a relisted requisition with a fresh posting time and keeps its
# creation time: of 30 PM rows probed 10-10, 9 were created 60+ days before they
# were "posted" (253 at most). Reposts are skipped (Vaish 2026-10-09); a month
# leaves room for a requisition that waited on approval before going up.
RELIST_DAYS = 30


def relisted(job: dict) -> int | None:
    """Days between requisition and posting, when that says the post is a relisting."""
    created, posted = job.get("_created_at") or "", job.get("posted_at") or ""
    try:
        gap = (datetime.fromisoformat(posted.replace("Z", "+00:00"))
               - datetime.fromisoformat(created.replace("Z", "+00:00"))).days
    except ValueError:
        return None
    return gap if gap > RELIST_DAYS else None


def load_targets():
    with open(TARGETS_YAML, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return cfg.get("defaults", {}), cfg.get("targets", [])


def acknowledged_degraded() -> set[str]:
    """Sources already known dead, so a standing failure stops raising an alarm.

    An alarm that is on permanently is as informative as one that is never on,
    and it teaches the reader to ignore the area it lives in. These still appear
    in the run table and in the summary as `known_degraded=`; they just do not
    colour the clock or emit a warning annotation.
    """
    try:
        with open(TARGETS_YAML, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except OSError:
        return set()
    return {e.get("source", "") for e in (cfg.get("acknowledged_degraded") or [])}


def row_first_seen(conn, jid: str) -> str:
    """The row's own `first_seen` stamp, read straight back out of the DB.

    Read back rather than assumed: `insert_job` is INSERT OR IGNORE, so on a row
    that already existed the stamp is the ORIGINAL sighting and `now_iso()`
    would be a fresh lie about it. The whole value of first_seen is that it is
    the oldest observation, so the one path that must never invent it is the one
    that writes it into a file other processes reason from.
    """
    row = conn.execute("SELECT first_seen FROM jobs WHERE id = ?", (jid,)).fetchone()
    return row[0] if row else ""


def write_queue_entry(jid: str, company: str, job: dict, source: str,
                      queue_dir: Path = None, first_seen: str = "") -> None:
    """Write one queue entry. This function owns the queue-file schema.

    `queue_dir` is a parameter rather than a module global read at call time
    because sync.py re-emits entries through here when it repairs a row whose
    queue file went missing — and a caller working against a different tree must
    be able to say so instead of silently writing into this one.

    `first_seen` and `detected_at` are two different facts and both are written
    (2026-08-23). `detected_at` is when THIS FILE was written, so a repair
    re-emit legitimately re-stamps it; `first_seen` is when the ROW was first
    recorded, which never moves and is the second lower bound `db.job_age()`
    reasons with. Without it the queue file cannot express the rule the cloud
    Routine's ordering states — `queue/<id>.json` was the only input it has, and
    it carried no observation at all.

    It is optional, and an empty value is the honest answer rather than a
    fallback: a caller with no row to read it from lands on
    `db.job_age(posted_at, "")`, which is exactly the pre-2026-08-23 behaviour
    — a missing lower bound, never a wrong one. Do NOT be tempted to default it
    to `now_iso()`: this function is also the repair path, and stamping "first
    seen just now" on a row recorded two days ago would not be a missing bound
    but a false one, which is the failure the whole observation rule exists to
    avoid. `sync.reconcile_queue` passes the row's real value.
    """
    queue_dir = QUEUE_DIR if queue_dir is None else Path(queue_dir)
    queue_dir.mkdir(parents=True, exist_ok=True)
    entry = {
        "id": jid,
        "company": company,
        "title": job["title"],
        "location": job["location"],
        "url": job["url"],
        "source": source,
        "posted_at": job.get("posted_at", ""),
        "detected_at": db.now_iso(),
        "first_seen": first_seen or job.get("first_seen", ""),
        "jd_markdown": job.get("jd", ""),
    }
    (queue_dir / f"{jid}.json").write_text(
        json.dumps(entry, indent=2, ensure_ascii=False), encoding="utf-8")


# Statuses a delisted-posting sweep may touch: the pre-artifact ones, exactly
# as the freshness sweep uses. A row past these has a tailored resume behind it
# (a full tailor slot, 18-30 min of Opus) or a human decision on it, and the board going quiet is
# not grounds to reach back and rewrite either.
SWEEPABLE_STATUSES = ("new", "scored")


def delisted_ids(db_rows, fetched_ids, fetch_ok: bool, fetched_count: int) -> list[str]:
    """Which stored rows the employer has taken down. Pure: no DB, no network.

    A queued job whose posting is already closed wastes a scoring call and, if
    it reaches the tailor lane, 18-30 minutes of Opus time on a resume nobody can
    submit. For a board we re-fetch in full every hour, spotting that is free.

    The two guards are the whole design. A fetch that ERRORED and a fetch that
    came back EMPTY both sweep nothing, because absence-caused-by-failure is
    indistinguishable from absence-caused-by-delisting from the inside — the
    same reasoning that makes poll_ats treat "board returned 0 jobs" as an error
    rather than as a company that stopped hiring. Getting this wrong deletes
    live queue entries, which is the only Layer 1 bug that destroys work instead
    of merely missing it.

    `fetched_ids` are dedupe ids (company_norm + title_norm + loc_bucket), never
    URLs: boards rewrite URLs on edit, and a re-slugged posting is the same job.
    """
    if not fetch_ok or fetched_count == 0:
        return []
    return [jid for jid, status in db_rows
            if status in SWEEPABLE_STATUSES and jid not in fetched_ids]


def sweep_delisted(conn, company: str, board: str, jobs: list[dict],
                   queue_dir: Path = None) -> int:
    """Mark this company's board rows that are no longer posted, and unqueue them."""
    queue_dir = QUEUE_DIR if queue_dir is None else Path(queue_dir)
    # Every job on the board counts as present, including ones the title and
    # location filters reject: they were never queued, but they are also plainly
    # not delisted, and a filter change must not read as a mass takedown.
    fetched_ids = {db.job_id(company, j["title"], j["location"]) for j in jobs}
    rows = list(conn.execute(
        "SELECT id, status FROM jobs WHERE company_norm = ? AND source = ?",
        (db.norm_company(company), board)))
    gone = delisted_ids(rows, fetched_ids, fetch_ok=True, fetched_count=len(jobs))
    reason = f"delisted: no longer on {board} board"
    for jid in gone:
        # APPEND, never replace. `notes` is a `;`-separated line whose FIRST
        # clause is the lane the job-scorer assigned, and later clauses can hold
        # a `promoted via dashboard` marker — Vaish's own explicit pick.
        # Overwriting it wholesale (what this did until 2026-08-23) dropped both:
        # the dashboard's "Recently closed" list reads lane_of(notes), so a swept
        # `scored` row silently lost its lane badge, and a promotion left no
        # trace that it had ever happened. freshness.sweep goes through
        # db.set_status for exactly this reason; this now matches it.
        db.set_status(conn, jid, "stale_skip", via="delisted")
        prior = conn.execute("SELECT notes FROM jobs WHERE id = ?", (jid,)).fetchone()[0]
        clauses = [c.strip() for c in (prior or "").split(";") if c.strip()]
        if reason not in clauses:
            clauses.append(reason)
        conn.execute("UPDATE jobs SET notes = ? WHERE id = ?",
                     ("; ".join(clauses), jid))
        (queue_dir / f"{jid}.json").unlink(missing_ok=True)
        # A sidecar outliving its queue file tells the cloud scorer this job is
        # already handled, forever. The two die together, everywhere.
        ingest_scores.drop_sidecar(jid)
    conn.commit()
    return len(gone)


# Concurrency for the board LISTING fetch only (see _prefetch_boards). Six, not
# more, because the boards are not six independent hosts: 36 of the 69 are
# Greenhouse and 25 are Ashby, so a naive pool would aim its whole width at one
# API. Measured 2026-09-12 across all 69 live boards, interleaved by provider:
# 24.3s at six workers with ZERO errors. The critical path is the Workday
# tenants (NVIDIA 16.5s, Mastercard 12.9s, Capital One 10.7s), which page
# internally and cannot be split further.
ATS_FETCH_WORKERS = 6


def _prefetch_boards(targets) -> dict:
    """Fetch every pollable board's listing concurrently. NETWORK ONLY.

    poll_ats was strictly sequential until 2026-09-12, which made the detection
    step 124s of a 149s run. Nothing about the ingest is parallelised here and
    nothing should be: this returns raw listings keyed by the target's index,
    and poll_ats then walks `targets` in their ORIGINAL order doing every
    filter, dedupe check and DB write exactly as before, on one thread. The
    two-writer rule is a rule about the DB, and the DB is not touched here.

    Exceptions are captured rather than raised so the caller can re-raise each
    one inside its own try/except and keep per-board error handling identical —
    one bad board still never kills the run.

    Targets are interleaved round-robin by provider so that concurrent workers
    land on different hosts rather than six-at-a-time on boards-api.greenhouse.io.
    """
    pollable = [(i, t) for i, t in enumerate(targets) if t.get("board") in FETCHERS]
    by_board: dict[str, list] = {}
    for i, t in pollable:
        by_board.setdefault(t["board"], []).append((i, t))
    order, queues = [], list(by_board.values())
    while any(queues):
        for q in queues:
            if q:
                order.append(q.pop(0))

    def one(pair):
        i, t = pair
        try:
            return i, FETCHERS[t["board"]](t.get("slug")), None
        except Exception as e:                      # captured, re-raised by caller
            return i, None, e

    out = {}
    if order:
        with ThreadPoolExecutor(max_workers=ATS_FETCH_WORKERS) as ex:
            for i, jobs, err in ex.map(one, order):
                out[i] = (jobs, err)
    return out


def poll_ats(conn, defaults, targets) -> dict:
    stats = {}
    _BOARD_NEWEST.clear()
    targets = list(targets)
    prefetched = _prefetch_boards(targets)
    for idx, t in enumerate(targets):
        company, board = t.get("company"), t.get("board")
        if board not in FETCHERS:
            # 'custom' boards have no poller. They used to `continue` before any
            # stats row existed, so five named target companies (Intuit,
            # Mastercard, Meta, Google, Microsoft) were absent from the summary
            # entirely — not even a zero. Their only route into queue/ is an
            # incidental JobSpy hit, and a coverage gap nobody can see is one
            # nobody closes. Reported, never counted as an error: this is a
            # known shape of the config, not a failure of this run.
            stats[f"{board}/{company}"] = {
                "fetched": 0, "stale": 0, "filtered": 0, "new": 0, "skipped": 0,
                "error": None, "note": "no poller for this board; JobSpy only"}
            continue
        slug = t.get("slug")
        title_re = re.compile(t.get("title_filter", defaults.get("title_filter", "")))
        loc_re = re.compile(t.get("location_filter", defaults.get("location_filter", "")))
        s = {"fetched": 0, "stale": 0, "filtered": 0, "new": 0, "skipped": 0, "error": None}
        jobs = []
        try:
            # Fetched concurrently up front by _prefetch_boards; its captured
            # exception is re-raised HERE so this board's error handling, its
            # stats row and the delisted sweep below all behave exactly as they
            # did when the fetch happened on this line.
            jobs, fetch_error = prefetched[idx]
            if fetch_error is not None:
                raise fetch_error
            s["fetched"] = len(jobs)
            # A paginated fetcher that ran out of page budget mid-window has a
            # coverage gap; it must not read as a clean poll. Deliberately NOT
            # `note`, which excludes a source from main()'s blind-run quorum:
            # this board polled fine and found jobs, so it still gets to vouch
            # that detection is alive. And deliberately not `error`, which would
            # colour the dashboard clock — the clock answers "is the cron
            # alive", and this is "the cap needs raising".
            if getattr(jobs, "truncated", ""):
                s["coverage_gap"] = jobs.truncated
            for job in jobs:
                if not title_re.search(job["title"]):
                    continue
                if not location_ok(job["location"], loc_re):
                    continue
                note_board_posting(board, company, job["title"], job.get("posted_at", ""))
                # Freshness window. The board APIs ignore date parameters
                # (verified 2026-08-15: Greenhouse returns all 578 jobs with
                # updated_after/created_after/since, Ashby all 136 with
                # publishedAfter), so every poll gets the whole board and the
                # window has to be applied here. Not inserting means no dedupe
                # row, so the job is simply re-evaluated next poll: cheap, and
                # nothing is permanently hidden. The window is deliberately
                # wider than the apply policy — this is the irreversible step.
                if db.is_provably_stale(job.get("posted_at", ""),
                                        db.FETCH_WINDOW_HOURS):
                    s["stale"] += 1
                    continue
                s["filtered"] += 1
                if db.is_seen(conn, company, job["title"], job["location"]):
                    continue
                if (gap := relisted(job)) is not None:
                    _record_repost(conn, s, company, job["title"], job["location"],
                                   job["url"], board, job.get("posted_at", ""),
                                   f"requisition created {gap} days before posting")
                    continue
                # Boards that ship a JD only on a per-job endpoint (Workday) are
                # resolved HERE, at the narrowest point in the funnel: past the
                # title filter, the location filter, the freshness window and
                # dedupe, so it costs one request per genuinely new role rather
                # than one per posting on a four-figure board.
                if board in JD_DETAIL_FETCHERS and not job["jd"]:
                    try:
                        job["jd"] = JD_DETAIL_FETCHERS[board](job)
                    except Exception as e:
                        # SKIP THE ROW ENTIRELY, do not insert it JD-less.
                        # Inserting would write a dedupe row, and a dedupe row
                        # satisfies db.is_seen forever — so a transient 502 on
                        # one detail page would permanently cost this posting
                        # its description, despite ~72 free retries inside the
                        # fetch window. What that buys downstream is worse than
                        # a miss: the row still costs a full Sonnet scoring call
                        # with nothing to read for criterion (c) JD overlap or
                        # (d) the YOE ask, and if it scores it can reach a
                        # ~26-minute tailor blind. Skipping leaves no row, so
                        # the next hourly poll simply tries again.
                        # (Jobright's detail fetch does the same, for the same
                        # reason — see poll_jobright_feed. Both are error paths
                        # with a cheap hourly retry; neither is a source that is
                        # JD-less by nature.)
                        s["jd_missing"] = s.get("jd_missing", 0) + 1
                        print(f"[layer1] {board}/{company}: JD fetch failed for "
                              f"{job['title']!r}: {type(e).__name__}: {e} "
                              f"(not ingested; retried next poll)")
                        continue
                # Intuit's list carries no date; the real one arrives with the
                # JD. A stale role is recorded (stale_skip, so a later repost can
                # revive it) and never detail-fetched again.
                if (board in bigtech.DETAIL_DATED
                        and db.is_provably_stale(job.get("posted_at", ""),
                                                 db.FETCH_WINDOW_HOURS)):
                    db.insert_job(conn, company, job["title"], job["location"],
                                  job["url"], board, jd_text=job["jd"],
                                  posted_at=job.get("posted_at", ""),
                                  status="stale_skip", notes="stale on detail")
                    s["stale"] += 1
                    continue
                # Hard filters run pre-insert so rejected jobs never reach the
                # queue, but they are still recorded (never dropped) so dedupe
                # holds and the decision stays auditable.
                rule = hardfilter.hard_filter(job["title"], job["jd"], job["location"])
                if rule:
                    db.insert_job(conn, company, job["title"], job["location"],
                                  job["url"], board, jd_text=job["jd"],
                                  posted_at=job.get("posted_at", ""),
                                  status="skipped", notes=f"hard-filter:{rule}")
                    s["skipped"] += 1
                    continue
                jid = db.insert_job(conn, company, job["title"], job["location"],
                                    job["url"], board, jd_text=job["jd"],
                                    posted_at=job.get("posted_at", ""))
                write_queue_entry(jid, company, job, board,
                                  first_seen=row_first_seen(conn, jid))
                s["new"] += 1
        except Exception as e:  # one bad board never kills the run
            s["error"] = f"{type(e).__name__}: {e}"
        else:
            # A board answering 200 with an empty list is the failure mode that
            # raise_for_status cannot see: a renamed slug that still resolves, an
            # emptied board, a changed JSON key. It has already happened once
            # here (targets.yaml records Lever returning [] on 2026-08-11) and
            # looked exactly like a company that simply is not hiring.
            # Scoped to full-board fetchers: for those, 0 rows means the board
            # itself came back empty. A window-limited fetcher (workday) returns
            # only what is inside the 72h window, so 0 is its normal answer on a
            # quiet board and flagging it would pin a permanent alarm on the
            # dashboard clock. Those fetchers raise for their own empty-board
            # case instead, which arrives through `except` above.
            if board in FULL_BOARD_FETCHERS and s["fetched"] == 0:
                s["error"] = ("board returned 0 jobs; slug may have moved or the"
                              " response shape changed")

        # Delisted sweep. Deliberately OUTSIDE the try/except/else above: an
        # exception raised in an `else:` clause is not caught by that clause's
        # own `except`, and nothing upstream catches it either, so a sweep bug
        # would escape poll_ats and kill the run before detect.yml's commit
        # step — losing every job the run had already found. A sweep is a
        # cleanup; it never gets to take down detection. Its failures are
        # reported separately from `error` for the same reason: they say
        # nothing about whether this board was successfully polled, and only
        # `error` feeds the dashboard clock and the blind-run gate.
        if board in FULL_BOARD_FETCHERS and s["error"] is None and s["fetched"]:
            try:
                s["delisted"] = sweep_delisted(conn, company, board, jobs)
            except Exception as e:
                s["sweep_error"] = f"{type(e).__name__}: {e}"
        stats[f"{board}/{slug or company}"] = s
    return stats


def jobspy_window(conn, now=None, lo: int | None = None, hi: int | None = None) -> int:
    """LinkedIn's posting-age window: hours since the last detection run, plus 2.

    LinkedIn ranks by relevance (JobSpy sends no sort), so a fixed 24h window
    with 25 results re-read the same top of the list every 2h: on 2026-10-08 a
    probe found 56 of the top 100 "product manager" results missing from the
    DB, nearly all ranked past 25, and 220 unseen on-target postings across the
    terms in a 4h window. A window just wider than the gap since the last run
    slices the list by time instead, and a missed run widens it by itself.
    Clamped to [lo, hi] (LinkedIn's bounds by default); unknown (no runs row)
    is hi.
    """
    lo = LINKEDIN_WINDOW_MIN if lo is None else lo
    hi = LINKEDIN_WINDOW_MAX if hi is None else hi
    now = now or datetime.now(timezone.utc)
    try:
        row = conn.execute("SELECT ts FROM runs WHERE kind='layer1' "
                           "ORDER BY ts DESC LIMIT 1").fetchone()
        last = datetime.strptime(row[0], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        return hi
    gap_h = (now - last).total_seconds() / 3600
    return int(min(hi, max(lo, math.ceil(gap_h + 2))))


def fetch_linkedin_jd(url: str, session=None) -> str | None:
    """The JD from a LinkedIn job page, as markdown, or None if the page refused.

    Ours rather than JobSpy's private _get_job_details, so a JobSpy upgrade can
    only break the parse, which shows as `jd_missing` on the run page."""
    from bs4 import BeautifulSoup
    from jobspy.util import markdown_converter
    # LinkedIn answers 429 to bursts (10-08: 163 of ~200 pages refused with 4
    # workers and no backoff), so a 429 waits and retries.
    for attempt in range(LINKEDIN_DETAIL_TRIES):
        try:
            r = (session or requests).get(url, headers=HEADERS, timeout=TIMEOUT)
        except requests.RequestException:
            return None
        if r.status_code != 429:
            break
        time.sleep(LINKEDIN_429_WAIT * (attempt + 1) + random.uniform(0, 1))
    if r.status_code != 200 or "linkedin.com/signup" in r.url:
        return None
    div = BeautifulSoup(r.text, "html.parser").find(
        "div", class_=lambda c: c and "show-more-less-html__markup" in c)
    if div is None:
        return None
    return (markdown_converter(div.prettify(formatter="html")) or "").strip() or None


def _seen_for_good(conn, company: str, title: str, location: str) -> bool:
    """Seen, and not a repost of a role that aged out unworked (db.is_revivable)."""
    return (db.is_seen(conn, company, title, location)
            and not db.is_revivable(conn, company, title, location))


def _ingest_jobspy_row(conn, s: dict, company: str, title: str, location: str,
                       source: str, url: str, posted: str, jd: str) -> None:
    """Insert one fresh row: off-target, hard-filtered or queued.

    A row already in the DB reaches here only as a revivable repost; it goes back
    to the queue if it would qualify today, and is otherwise left as it was."""
    repost = db.is_seen(conn, company, title, location)
    if repost:
        if (JOBSPY_RELEVANT.search(title)
                and not hardfilter.hard_filter(title, jd, location)):
            jid = db.revive_repost(conn, company, title, location, url, source,
                                   jd_text=jd, posted_at=posted)
            write_queue_entry(jid, company, {"title": title, "location": location,
                                             "url": url, "posted_at": posted, "jd": jd},
                              source, first_seen=row_first_seen(conn, jid))
            s["new"] += 1
            s["reposted"] = s.get("reposted", 0) + 1
        return
    if not JOBSPY_RELEVANT.search(title):
        # Recorded, not dropped: the row still blocks future duplicates and stays
        # inspectable, but never reaches scoring.
        db.insert_job(conn, company, title, location, url, source, jd_text=jd,
                      posted_at=posted, status="skipped",
                      notes="off-target title (jobspy breadth)")
        s["skipped"] += 1
        return
    rule = hardfilter.hard_filter(title, jd, location)
    if rule:
        db.insert_job(conn, company, title, location, url, source, jd_text=jd,
                      posted_at=posted, status="skipped", notes=f"hard-filter:{rule}")
        s["skipped"] += 1
        return
    jid = db.insert_job(conn, company, title, location, url, source,
                        jd_text=jd, posted_at=posted)
    write_queue_entry(jid, company, {"title": title, "location": location, "url": url,
                                     "posted_at": posted, "jd": jd},
                      source, first_seen=row_first_seen(conn, jid))
    s["new"] += 1


def _fresh_rows(conn, df, s: dict, per_site: dict, seen_now: set):
    """Yield (company, title, location, site, url, posted, row) for the US rows
    inside the fetch window that this DB has not seen, once per run."""
    for _, row in df.iterrows():
        company, title = cell(row, "company"), cell(row, "title")
        if not company or not title:
            continue
        location = cell(row, "location")
        if str(row.get("is_remote")) == "True" and "remote" not in location.lower():
            location = (location + " Remote").strip()
        if NON_US.search(location) and not US_SIGNAL.search(location):
            continue
        site = cell(row, "site") or "unknown"
        per_site[site] = per_site.get(site, 0) + 1
        # The window is enforced here too: JobSpy sites honour hours_old loosely
        # (Indeed, measured 2026-08-15: ~14% of returned rows were 4-16 days old).
        if db.is_provably_stale(cell(row, "date_posted"), db.FETCH_WINDOW_HOURS):
            s["stale"] = s.get("stale", 0) + 1
            continue
        s["filtered"] += 1
        key = db.job_id(company, title, location)
        if key in seen_now or _seen_for_good(conn, company, title, location):
            continue
        seen_now.add(key)
        yield (company, title, location, site, cell(row, "job_url"),
               cell(row, "date_posted"), row)


def poll_jobspy(conn, hours_old: int, include_linkedin: bool,
                linkedin_hours: int | None = None, include_google: bool = False) -> dict:
    try:
        from jobspy import scrape_jobs
    except ImportError:
        return {"jobspy": {"error": "python-jobspy not installed", "fetched": 0,
                           "filtered": 0, "new": 0}}
    # google is PROBED on one term, not queried on all (2026-09-12): its JobSpy
    # scraper has returned 0 rows since 2026-08-11 (acknowledged_degraded), and
    # one term is enough to notice the day upstream comes back. JobSpy ignores
    # search_term for google and pastes google_search_term verbatim, so it has
    # to read as a natural-language query. OFF by default like LinkedIn: Google's
    # terms forbid automated scraping, so --google is the operator's own call.
    stats = {}
    per_site = {s: 0 for s in (["google"] if include_google else [])
                + (["linkedin"] if include_linkedin else [])}
    blank = lambda: {"fetched": 0, "filtered": 0, "new": 0, "skipped": 0, "error": None}
    seen_now: set = set()

    # Indeed was dropped 2026-10-09 (Vaish's call: loose matching, reposts,
    # mostly scorer skips). Google stays a one-term revival probe.
    term = next(iter(SEARCH_TERMS))
    if include_google:
        s = stats.setdefault(f"jobspy:{term}", blank())
        try:
            df = scrape_jobs(site_name=["google"], search_term=term,
                             google_search_term=f"{term} jobs in United States since yesterday",
                             location="USA", results_wanted=50, hours_old=hours_old or 24,
                             verbose=0)
            s["fetched"] += len(df)
            for company, title, location, site, url, posted, row in _fresh_rows(
                    conn, df, s, per_site, seen_now):
                _ingest_jobspy_row(conn, s, company, title, location, f"jobspy:{site}",
                                   url, posted, cell(row, "description"))
        except Exception as e:
            s["error"] = f"google {type(e).__name__}: {e}"

    if include_linkedin:
        # LinkedIn (2026-10-08): list pages only, deep, in a narrow window, terms
        # in parallel (JobSpy sleeps 3-7s between list pages of 10, so sequential
        # depth would cost minutes). The JD is then fetched only for rows that are
        # new and on-target: linkedin_fetch_description=True fetched a page for
        # every result, most of them already in the DB.
        window = linkedin_hours or jobspy_window(conn)
        print(f"[layer1] LinkedIn window {window}h")

        def listing(term):
            return scrape_jobs(site_name=["linkedin"], search_term=term, location="USA",
                               results_wanted=SEARCH_TERMS[term], hours_old=window,
                               job_type="fulltime", verbose=0,
                               linkedin_fetch_description=False)

        frames = {}
        with ThreadPoolExecutor(max_workers=LINKEDIN_WORKERS) as pool:
            futs = {term: pool.submit(listing, term) for term in SEARCH_TERMS}
            for term, fut in futs.items():
                try:
                    frames[term] = fut.result()
                except Exception as e:
                    stats.setdefault(f"jobspy:{term}", blank())["error"] = f"linkedin {type(e).__name__}: {e}"
        todo = []      # (term, company, title, location, url, posted)
        listed = []    # every URL the lists returned, seen or not: the ID anchor
        for term, df in frames.items():
            s = stats.setdefault(f"jobspy:{term}", blank())
            s["fetched"] += len(df)
            if len(df):
                listed += [str(u) for u in df["job_url"]]
            for company, title, location, _, url, posted, _ in _fresh_rows(
                    conn, df, s, per_site, seen_now):
                todo.append((term, company, title, location, url, posted))
        reposts = linkedin_reposts(listed)
        kept = []
        for t in todo:
            if t[4] in reposts:
                _record_repost(conn, stats[f"jobspy:{t[0]}"], t[1], t[2], t[3], t[4],
                               "jobspy:linkedin", t[5], "linkedin id trails the run's newest")
            elif why := board_repost(t[1], t[2]):
                _record_repost(conn, stats[f"jobspy:{t[0]}"], t[1], t[2], t[3], t[4],
                               "jobspy:linkedin", t[5], why)
            else:
                kept.append(t)
        todo = kept
        # A page is fetched only when its JD can change the outcome: on-target
        # and not already rejected on its title alone (Director, Principal, ...).
        wanted = [t[4] for t in todo if JOBSPY_RELEVANT.search(t[2])
                  and hardfilter.hard_filter(t[2], "", t[3]) is None]
        session = requests.Session()
        t_fetch = time.perf_counter()
        with ThreadPoolExecutor(max_workers=LINKEDIN_DETAIL_WORKERS) as pool:
            jds = dict(zip(wanted, pool.map(lambda u: fetch_linkedin_jd(u, session), wanted)))
        print(f"[layer1] LinkedIn: {len(wanted)} JD page(s) fetched in "
              f"{time.perf_counter() - t_fetch:.0f}s, "
              f"{sum(1 for v in jds.values() if not v)} refused")
        for term, company, title, location, url, posted in todo:
            s = stats[f"jobspy:{term}"]
            jd = jds.get(url) or ""
            if url in jds and not jd:
                # Kept with an empty JD, as JobSpy did: the scorer scores what it
                # can see and the tailor fetches the page itself. Counted so a
                # LinkedIn layout change shows on the run page.
                s["jd_missing"] = s.get("jd_missing", 0) + 1
            _ingest_jobspy_row(conn, s, company, title, location, "jobspy:linkedin",
                               url, posted, jd)

    # A site that returns nothing is dead, not quiet: say so rather than letting
    # the breadth layer silently shrink. Google's row is its revival readout.
    for site, n in per_site.items():
        if n == 0:
            scope = "on its probe term" if site == "google" else "across every term"
            stats[f"jobspy[{site}]"] = {"fetched": 0, "filtered": 0, "new": 0,
                                        "error": f"site returned 0 rows {scope}; "
                                                 "scraper likely broken upstream"}
    return stats


# --- Jobright (markdown-table aggregators) ----------------------------------
# Jobright has no API, but publishes daily-rebuilt markdown tables of tech
# roles, one repo per track. Each repo is a FEED here rather than a hard-coded
# URL, because the tables do not share a shape: the H1B list is 7 columns with
# its link in a Link column, the new-grad PM list is 5 with its link inside the
# title cell and no sponsorship column at all. The shape lives in `JOBRIGHT_
# SHAPES` as one small parser each; everything else about polling a feed is the
# same code, so adding a third track is a dict entry and a row parser.
#
# Both run LAST, after ATS and JobSpy, because first insert wins dedupe and
# these are aggregators: when the same role is also on a company board, we want
# the employer-side URL and the real JD, not a jobright.ai tracking redirect.
JOBRIGHT_SOURCE = "jobright:h1b"   # kept: the H1B feed's source string is in the DB
JOBRIGHT_COLUMNS = 7  # Company | Job Title | Level | Location | H1B | Link | Date
JOBRIGHT_NEWGRAD_COLUMNS = 5  # Company | Job Title | Location | Work Model | Date

# The H1B feed (jobright-ai/Daily-H1B-Jobs-In-Tech, shape "h1b") was dropped
# 2026-10-09: its newest row was 2026-05-06, so every run parsed 1,424 stale
# rows for nothing. Its parser stays in JOBRIGHT_SHAPES for the day it revives.
JOBRIGHT_FEEDS = (
    {
        "source": "jobright:pm-newgrad",
        "repo": "jobright-ai/2026-Product-Management-New-Grad",
        "shape": "newgrad",
        "note": "aggregator, new-grad PM",
        # NOT an H1B claim. This table has no sponsorship column, and copying
        # the other feed's note would put a sponsorship signal on rows that
        # carry none — a claim invented by a copy-paste.
        "row_note": "new-grad PM list (Jobright)",
    },
    {
        # 2026-10-08: the one adjacent Jobright list worth its fetch (probed
        # that day: ~15 rows a day, some product-facing like "Business Analyst
        # I, Healthcare Product"). Rows pass JOBSPY_RELEVANT before any detail
        # fetch, so its pure-BA rows cost a dedupe row and nothing else. The
        # Consultant, Data-Analysis and Management lists were judged mostly
        # off-target by the same probe.
        "source": "jobright:ba-newgrad",
        "repo": "jobright-ai/2026-Business-Analyst-New-Grad",
        "shape": "newgrad",
        "note": "aggregator, new-grad business analyst",
        "row_note": "new-grad business analyst list (Jobright)",
    },
)
# Both feeds carry a `note`, which keeps them OUT of main()'s blind-run quorum.
# Deliberate for the live one too: an aggregator is not evidence that detection
# is working. On a run where every employer-side source threw, a jobright feed
# answering 200 must not be able to vouch that the hour was merely quiet.

JOBRIGHT_URL = "https://raw.githubusercontent.com/{repo}/master/README.md"
# A year-less date more than this far ahead of today is a parse error, not a
# posting. Two days of slack covers a feed built on a timezone ahead of UTC.
JOBRIGHT_FUTURE_TOLERANCE_DAYS = 2

# Link text may hold one bracket pair: "[[2027] Associate PM](url)" made the
# old pattern match nothing and hand the detail fetch an empty URL every run.
_MD_LINK = re.compile(r"\[((?:[^\[\]]|\[[^\]]*\])*)\]\(([^)]*)\)")
_MD_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MD_MONTHDAY = re.compile(r"^([A-Za-z]{3,9})\.?\s+(\d{1,2})$")
_MD_SEPARATOR = re.compile(r"^[\s:-]+$")
# A row whose company cell is this inherits the company above it: one employer
# posting the same role in many cities. 374 of 497 rows in the live new-grad
# file are continuations, so treating them as empty-company drops three
# quarters of the feed without a single error anywhere.
_MD_CONTINUATION = "↳"
# The two badges the H1B file's own legend defines: a gold medal means the JD
# says outright that the company sponsors, silver means it has sponsored this
# category in the past three years.
_H1B_FLAGS = {"\U0001F3C5": "explicit", "\U0001F948": "history"}


def _md_cell(cell: str) -> tuple[str, str]:
    """(visible text, href) for a markdown table cell that may hold a link."""
    m = _MD_LINK.search(cell)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return cell.replace("*", "").strip(), ""


def _jobright_cells(line: str, columns: int):
    """The cells of a markdown table row of exactly `columns` width, or None.

    Returning None rather than raising on a malformed line is what lets the
    caller walk the whole README — prose, HTML `<details>` wrappers, several
    category tables and their headers — without knowing the document's shape.
    The caller decides that ZERO parsed rows means the format moved; a single
    unparseable line never does, because the file is mostly not a table. The
    width check is also what stops one feed's parser silently accepting the
    other feed's rows and mapping the wrong column onto the wrong field.
    """
    line = (line or "").strip()
    if not line.startswith("|"):
        return None
    cells = [c.strip() for c in line.strip("|").split("|")]
    return cells if len(cells) == columns else None


def _jobright_company(cell: str, last_company: str) -> str:
    """The row's employer, resolving a continuation marker against the row above.

    A continuation with nothing above it yields "" and the row is dropped: it
    would otherwise be attributed to whatever company the walker happened to
    see last, which on a re-ordered file is a different employer entirely.
    """
    name, _ = _md_cell(cell)
    if name == _MD_CONTINUATION:
        return last_company
    return "" if _MD_SEPARATOR.match(cell) else name


def jobright_month_day(value: str, today=None) -> str:
    """"Aug 23" -> "2026-08-23": the most recent occurrence that is not future.

    The new-grad feed writes dates without a year, and guessing wrong moves a
    row by 365 days — which either hides a live posting behind the staleness
    gate forever or ingests one a year old. "Most recent occurrence not in the
    future" is the only reading that survives the Dec/Jan boundary, where a
    "Dec 30" row read on 3 January is four days old and not 361 days away.

    Returns "" for anything unrecognised, never a guess: db.job_age() treats an
    empty posting date as unknown, and unknown is never stale.
    """
    today = today or datetime.now(timezone.utc).date()
    m = _MD_MONTHDAY.match((value or "").strip())
    if not m:
        return ""
    month, day = m.group(1)[:3].title(), m.group(2)
    # Newest year first, so the first candidate inside tolerance is the most
    # recent one. Four years back covers a leap day landing on a non-leap year.
    for year in range(today.year, today.year - 5, -1):
        try:
            d = datetime.strptime(f"{month} {day} {year}", "%b %d %Y").date()
        except ValueError:
            continue        # Feb 29 in a non-leap year, or a bogus month name
        if (d - today).days <= JOBRIGHT_FUTURE_TOLERANCE_DAYS:
            return d.isoformat()
    return ""


def parse_jobright_row(line: str, last_company: str = "", today=None):
    """One row of the H1B feed: Company | Title | Level | Location | H1B | Link | Date.

    `today` is unused here and that is not an oversight: this feed's dates
    already carry a year, so there is nothing to infer. It is in the signature
    because JOBRIGHT_SHAPES dispatches every shape through one call, and a
    parser that took a different signature would push a per-shape branch back
    into the walker — which is the copy-pasted function this structure exists
    to avoid.
    """
    cells = _jobright_cells(line, JOBRIGHT_COLUMNS)
    if cells is None:
        return None
    company = _jobright_company(cells[0], last_company)
    title, _ = _md_cell(cells[1])
    location, _ = _md_cell(cells[3])
    _, url = _md_cell(cells[5])
    posted = cells[6].strip()
    if not company or not title:
        return None
    if not _MD_DATE.match(posted):
        # Also how the header row is rejected: "Date Posted" is not a date.
        return None
    return {
        "company": company,
        "title": title,
        "location": location,
        "url": url,
        # Already YYYY-MM-DD, i.e. day precision, which db.job_age() reads as a
        # 24h-wide interval. Passed through untouched for that reason.
        "posted_at": posted,
        "h1b": _H1B_FLAGS.get(cells[4].strip(), ""),
        "jd": "",   # the table carries no description; see poll_jobright
    }


def parse_jobright_newgrad_row(line: str, last_company: str = "", today=None):
    """One row of the new-grad PM feed: Company | Title | Location | Model | Date.

    Three differences from the H1B shape, every one a way to get it wrong: the
    URL lives inside the TITLE cell rather than in a Link column, the date has
    no year, and there is NO H1B column — so no row from this feed may carry a
    sponsorship claim.
    """
    cells = _jobright_cells(line, JOBRIGHT_NEWGRAD_COLUMNS)
    if cells is None:
        return None
    company = _jobright_company(cells[0], last_company)
    title, url = _md_cell(cells[1])
    location, _ = _md_cell(cells[2])
    posted = jobright_month_day(cells[4], today)
    if not company or not title or not posted:
        # No posted date also rejects the header ("Date Posted") and the
        # separator. Unlike the H1B feed a row here is DROPPED rather than
        # ingested date-less: this table is rebuilt daily and every real row
        # carries a date, so a missing one means the column moved.
        return None
    return {
        "company": company,
        "title": title,
        "location": location,
        "url": url,
        "posted_at": posted,
        "h1b": "",   # this feed carries no sponsorship signal at all
        "jd": "",
    }


JOBRIGHT_SHAPES = {"h1b": parse_jobright_row, "newgrad": parse_jobright_newgrad_row}


# --- Jobright detail pages --------------------------------------------------
# The tables carry no description and a year-less date; the job's own page
# carries both, as a schema.org JobPosting in an ld+json block. Verified live
# 2026-08-23: `description` runs 2.4-4.6k characters with Responsibilities and
# Qualifications intact, and `datePosted` is a full timestamp
# ("2026-08-23 13:47:07") rather than the table's "Aug 23".
#
# THE REGEX TRAP: the tag is `<script id="job-posting" type="application/ld+json">`.
# A pattern anchored on `<script type="application/ld+json"` matches NOTHING and
# fails silently, leaving every row JD-less exactly as before. Attributes are
# therefore matched order-independently, and every ld+json block on the page is
# walked rather than just the first — the page is free to add an Organization or
# BreadcrumbList block ahead of the one we want.
_LD_JSON = re.compile(
    r"<script\b(?=[^>]*\btype\s*=\s*[\"']application/ld\+json[\"'])[^>]*>(.*?)</script>",
    re.I | re.S)

# Per feed, per run. The first production run after this shipped faces the whole
# 72h window unseen (~400 rows on the new-grad feed), and hammering a site that
# owes us nothing is both rude and the fastest way to get blocked. Rows past the
# cap are simply not ingested, so they have no dedupe row and the next hourly
# poll takes the next slice: a backlog drains in a few hours, well inside the
# window, and nothing is lost. Steady state never reaches it.
JOBRIGHT_DETAIL_CAP = 80


def _ld_objects(payload):
    """Every JSON object inside one ld+json payload, list and @graph included."""
    if isinstance(payload, list):
        for item in payload:
            yield from _ld_objects(item)
    elif isinstance(payload, dict):
        graph = payload.get("@graph")
        if isinstance(graph, (list, dict)):
            yield from _ld_objects(graph)
        yield payload


def jobright_job_posting(page_html: str) -> dict:
    """The schema.org JobPosting on a Jobright detail page, or {}.

    A block that does not parse is skipped rather than fatal: one malformed
    payload must not hide a good one further down the page, and the whole
    enrichment is best-effort by design — the row's own facts (company, title,
    location, URL, table date) are already known and correct without it.
    """
    for m in _LD_JSON.finditer(page_html or ""):
        try:
            payload = json.loads(m.group(1))
        except ValueError:
            continue
        for obj in _ld_objects(payload):
            types = obj.get("@type")
            types = types if isinstance(types, list) else [types]
            if "JobPosting" in types:
                return obj
    return {}


def jobright_posted_at(date_posted, table_date: str) -> str:
    """Prefer the page's timestamp over the table's year-less date, or keep the date.

    Why prefer it: a real timestamp gives db.job_age() precision "exact", and an
    exact age is the only kind Vaish's 24h apply rule can actually be enforced
    against — the table's "Aug 23" is a 24h-wide interval whose optimistic end
    is almost never provably inside the window.

    The two values are the same instant written differently, not two claims: the
    timestamp is naive UTC and the table renders its date in a US timezone, so
    they can disagree by a timezone offset (measured 2026-08-23: a row the table
    dated Aug 19 carries "2026-08-20 03:32:28", i.e. 20:32 Pacific on the 19th).
    Jobright asserts this field as the posting date and we take the source at its
    word, exactly as we take Greenhouse's `first_published`.

    NOTHING IS FABRICATED. A missing, date-only or unparseable value keeps the
    table's date and stays at day precision; the space separator is normalised
    to "T" (a format change, not a claim) and no "Z" is invented, since
    db.parse_posted_at already reads a naive stamp as UTC.
    """
    s = str(date_posted or "").strip().replace(" ", "T")
    if len(s) > 10 and db.parse_posted_at(s) is not None:
        return s
    return table_date


def jobright_detail_fields(page_html: str, table_date: str) -> tuple[str, str]:
    """(jd_text, posted_at) for one row, from its detail page HTML."""
    posting = jobright_job_posting(page_html)
    return (strip_html(posting.get("description") or ""),
            jobright_posted_at(posting.get("datePosted"), table_date))


def fetch_jobright_detail(url: str) -> str:
    """One detail page. Called only at the narrowest point in the funnel."""
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text


def parse_jobright_table(markdown: str, shape: str = "h1b", today=None) -> list[dict]:
    """Walk a whole README, carrying the company across continuation rows."""
    parse = JOBRIGHT_SHAPES[shape]
    out, last_company = [], ""
    for line in (markdown or "").splitlines():
        row = parse(line, last_company, today)
        if row:
            out.append(row)
            last_company = row["company"]
    return out


def poll_jobright_feed(conn, feed: dict) -> dict:
    source = feed["source"]
    s = {"fetched": 0, "stale": 0, "filtered": 0, "new": 0, "skipped": 0,
         "error": None}
    try:
        r = requests.get(JOBRIGHT_URL.format(repo=feed["repo"]),
                         headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        rows = parse_jobright_table(r.text, feed["shape"])
        s["fetched"] = len(rows)
        # ZERO PARSED ROWS is the error, not zero fresh rows — and the
        # difference is the whole reporting contract for these feeds. A file
        # can legitimately span months, so almost everything in it is outside
        # the 72h window and `new` is legitimately 0; calling that an error
        # would pin a permanent alarm on the dashboard clock, which is exactly
        # what acknowledged_degraded exists to prevent. A table that parses to
        # nothing, though, means the columns moved or the file was renamed.
        if not rows:
            s["error"] = ("no job rows parsed; the README's table layout or "
                          "filename likely changed upstream")
        else:
            # Surfaced every run so an abandoned upstream feed is VISIBLE in
            # the table without colouring the clock — which is exactly how the
            # H1B feed's death (last upstream commit 2026-05-06) reads now.
            s["note"] = f"{feed['note']}; newest row {max(r['posted_at'] for r in rows)}"
        for row in rows:
            if db.is_provably_stale(row["posted_at"], db.FETCH_WINDOW_HOURS):
                s["stale"] += 1
                continue
            if NON_US.search(row["location"]) and not US_SIGNAL.search(row["location"]):
                continue
            s["filtered"] += 1
            if db.is_seen(conn, row["company"], row["title"], row["location"]):
                continue
            note = feed["row_note"] + (f": {row['h1b']}" if row["h1b"] else "")
            if not JOBSPY_RELEVANT.search(row["title"]):
                db.insert_job(conn, row["company"], row["title"], row["location"],
                              row["url"], source, posted_at=row["posted_at"],
                              status="skipped", notes="off-target title (jobright breadth)")
                s["skipped"] += 1
                continue
            if why := board_repost(row["company"], row["title"]):
                _record_repost(conn, s, row["company"], row["title"], row["location"],
                               row["url"], source, row["posted_at"], why)
                continue
            # Detail-page enrichment, at the NARROWEST point in the funnel —
            # past the freshness window, the location filter, dedupe and the
            # title filter — for the same reason fetch_workday_jd is called
            # here: the file holds ~500 rows and this is a handful of genuinely
            # new PM roles. It buys the two things the table cannot give: the
            # JD text (without which the scorer has nothing to read for
            # criterion (c) keyword overlap or (d) the YOE ask, and hard_filter
            # can only apply its title and location rules) and a real posting
            # TIMESTAMP, which is the only kind of date the 24h apply rule can
            # be enforced against.
            if s.get("detail_fetched", 0) >= JOBRIGHT_DETAIL_CAP:
                # Politeness cap, not an error. No row is inserted, so there is
                # no dedupe row and the next hourly poll takes the next slice.
                s["detail_capped"] = s.get("detail_capped", 0) + 1
                continue
            try:
                page = fetch_jobright_detail(row["url"])
                s["detail_fetched"] = s.get("detail_fetched", 0) + 1
            except Exception as e:
                # Same rule as the Workday JD path: SKIP, never ingest blind.
                # Inserting would write a dedupe row, and a dedupe row satisfies
                # is_seen forever, so one transient failure would cost this
                # posting its description permanently despite ~72 free retries
                # inside the fetch window.
                s["jd_missing"] = s.get("jd_missing", 0) + 1
                print(f"[layer1] {source}: detail fetch failed for "
                      f"{row['title']!r}: {type(e).__name__}: {e} "
                      f"(not ingested; retried next poll)")
                continue
            if jobright_is_repost(page):
                _record_repost(conn, s, row["company"], row["title"], row["location"],
                               row["url"], source, row["posted_at"], "jobright flag")
                continue
            row["jd"], row["posted_at"] = jobright_detail_fields(page, row["posted_at"])
            if not row["jd"]:
                # The page ANSWERED and carried no JobPosting description. That
                # is not a transient failure, so retrying it 72 times would just
                # lose the job — a wrongly rejected posting is invisible forever.
                # Ingested JD-less exactly as before, and counted out loud,
                # because a climbing number here is how "upstream changed the
                # page shape" looks from the outside.
                s["jd_empty"] = s.get("jd_empty", 0) + 1
            # A better date can prove what the table's could not. Re-check it,
            # or we would have proved the row stale and ingested it anyway.
            if db.is_provably_stale(row["posted_at"], db.FETCH_WINDOW_HOURS):
                s["stale"] += 1
                continue
            rule = hardfilter.hard_filter(row["title"], row["jd"], row["location"])
            if rule:
                db.insert_job(conn, row["company"], row["title"], row["location"],
                              row["url"], source, jd_text=row["jd"],
                              posted_at=row["posted_at"],
                              status="skipped", notes=f"hard-filter:{rule}")
                s["skipped"] += 1
                continue
            jid = db.insert_job(conn, row["company"], row["title"], row["location"],
                                row["url"], source, jd_text=row["jd"],
                                posted_at=row["posted_at"], notes=note)
            write_queue_entry(jid, row["company"], row, source,
                              first_seen=row_first_seen(conn, jid))
            s["new"] += 1
    except Exception as e:
        s["error"] = f"{type(e).__name__}: {e}"
    return {source: s}


def poll_jobright(conn) -> dict:
    """Every declared feed. One feed's failure never touches another's."""
    stats = {}
    for feed in JOBRIGHT_FEEDS:
        stats.update(poll_jobright_feed(conn, feed))
    stats.update(poll_jobright_app(conn))
    return stats


# --- VC portfolio boards (startups) -----------------------------------------
# pipeline/vc_boards.py reads 12 Getro and 11 Consider boards (Accel, General
# Catalyst, Sequoia, Lightspeed, Bessemer, GV, ...), product function, US, newest
# first. Run right after the ATS boards: their links are the employer's own, so
# they should win dedupe over a LinkedIn copy of the same role.
VC_WORKERS = 6
VC_JD_WORKERS = 4


def poll_vc_boards(conn) -> dict:
    boards = ([("getro", n, h) for n, h in vc_boards.GETRO_BOARDS.items()]
              + [("consider", n, h) for n, h in vc_boards.CONSIDER_BOARDS.items()])
    fetch = {"getro": vc_boards.getro_jobs, "consider": vc_boards.consider_jobs}

    def one(b):
        kind, name, host = b
        try:
            return b, fetch[kind](host), None
        except Exception as e:
            return b, [], f"{name}: {type(e).__name__}: {str(e)[:80]}"

    with ThreadPoolExecutor(max_workers=VC_WORKERS) as pool:
        results = list(pool.map(one, boards))
    stats = {k: {"fetched": 0, "filtered": 0, "new": 0, "skipped": 0, "error": None}
             for k in ("vc:getro", "vc:consider")}
    errors = {k: [] for k in stats}
    seen_now, todo = set(), []
    for (kind, name, _), rows, err in results:
        st = stats[f"vc:{kind}"]
        if err:
            errors[f"vc:{kind}"].append(err)
        elif kind == "getro" and not rows:
            # A Getro board always has US product jobs (12 of 12 returned 20 on
            # 10-08); none means the page shape moved, not a quiet hour.
            errors["vc:getro"].append(f"{name}: board returned 0 jobs")
        st["fetched"] += len(rows)
        for r in rows:
            if NON_US.search(r["location"]) and not US_SIGNAL.search(r["location"]):
                continue
            if db.is_provably_stale(r["posted_at"], db.FETCH_WINDOW_HOURS):
                st["stale"] = st.get("stale", 0) + 1
                continue
            st["filtered"] += 1
            key = db.job_id(r["company"], r["title"], r["location"])
            if key in seen_now or _seen_for_good(conn, r["company"], r["title"], r["location"]):
                continue
            seen_now.add(key)
            if why := board_repost(r["company"], r["title"]):
                _record_repost(conn, st, r["company"], r["title"], r["location"], r["url"],
                               f"{kind}:{name}", r["posted_at"], why)
                continue
            todo.append((kind, name, r))
    wanted = [t for t in todo if JOBSPY_RELEVANT.search(t[2]["title"])
              and hardfilter.hard_filter(t[2]["title"], "", t[2]["location"]) is None]

    def jd_of(t):
        try:
            return t[2]["jd_fetch"]() or ""
        except Exception:
            return ""
    with ThreadPoolExecutor(max_workers=VC_JD_WORKERS) as pool:
        jds = dict(zip([id(t) for t in wanted], pool.map(jd_of, wanted)))
    for t in todo:
        kind, name, r = t
        st = stats[f"vc:{kind}"]
        jd = jds.get(id(t), "")
        if id(t) in jds and not jd:
            st["jd_missing"] = st.get("jd_missing", 0) + 1
        _ingest_jobspy_row(conn, st, r["company"], r["title"], r["location"],
                           f"{kind}:{name}", r["url"], r["posted_at"], jd)
    for k, errs in errors.items():
        if errs:
            stats[k]["error"] = "; ".join(errs)
    return stats


# --- Jobright app feed (Vaish's own account) ----------------------------------
# The public README lists are new-grad and internship only (35 of them, checked
# 2026-10-08). Experienced roles live only in the logged-in app, whose job feed
# is this endpoint. It applies Vaish's saved filters server-side (US, his role
# list, levels, past 24h). Read it in sortCondition=0, the app's own
# "Recommended" order, which is what Vaish sees: sortCondition=1 (newest first)
# loosens the role match into a firehose of project managers, analysts and
# technical writers (20 jobs per ~8 minutes, 3 of them product roles, 10-08),
# while the top 100 Recommended were PM and FDE roles. Relevance order has no
# time edge, so every run reads the same few pages and dedupe drops what was
# seen. The app answers 403 / errorCode 43004 after ~20 requests in a burst,
# hence few pages, paced.
# The cookie is his session, stored by him as the JOBRIGHT_COOKIE secret (the
# whole Cookie header of a /swan/ request); it expires, and the run says so.
JOBRIGHT_APP_SOURCE = "jobright:app"
JOBRIGHT_APP_URL = ("https://jobright.ai/swan/recommend/list/jobs?refresh=false"
                    "&sortCondition=0&position={pos}&count=20&syncRerank=false")
JOBRIGHT_APP_PAGES = 5          # past ~100 the matches drift to solutions engineers
JOBRIGHT_APP_PAUSE = 3          # seconds between pages
JOBRIGHT_APP_OK = 10000         # the API's success errorCode


def jobright_app_page(session, pos: int) -> list[dict]:
    try:
        r = session.get(JOBRIGHT_APP_URL.format(pos=pos), timeout=TIMEOUT)
    except requests.ConnectionError:
        # The JD fetches between pages leave the kept-alive connection idle long
        # enough for Jobright to close it; every CI run on 10-08 died on page 2
        # with RemoteDisconnected. One retry opens a fresh connection.
        time.sleep(JOBRIGHT_APP_PAUSE)
        r = session.get(JOBRIGHT_APP_URL.format(pos=pos), timeout=TIMEOUT)
    if r.status_code in (401, 403):
        raise RuntimeError(f"HTTP {r.status_code}: session cookie expired or rate-limited; "
                           "refresh the JOBRIGHT_COOKIE secret")
    r.raise_for_status()
    body = r.json()
    if body.get("errorCode") != JOBRIGHT_APP_OK:
        raise RuntimeError(f"errorCode {body.get('errorCode')}: {body.get('errorMsg')}")
    return [x for x in ((body.get("result") or {}).get("jobList") or [])
            if isinstance(x, dict) and x.get("jobResult")]


# W3b (2026-10-09): Jobright's detail pages now answer with a "Security check"
# bot challenge (8 of 8 refetches), so 56 of 149 scored feed rows reached the
# scorer with no JD. The challenge is never worked around. The JD comes, in
# order, from the feed item itself, the detail page when it is a real page, and
# the employer's own posting behind the item's apply link (vc_boards.ats_jd:
# Greenhouse / Ashby / Lever APIs, else the page's JSON-LD).
_JOBRIGHT_TEXT_KEYS = re.compile(
    r"(?i)(summary|description|responsibilit|qualification|requirement|skill)")
_JOBRIGHT_URL_KEYS = ("applyLink", "originalUrl", "applyUrl", "jobUrl", "externalUrl",
                      "sourceUrl", "url")
JD_MIN_CHARS = 500   # below this the scorer and the tailor are working blind


def jobright_challenge(page: str) -> bool:
    """A bot-check page instead of the job: no JobPosting, a security-check title."""
    return bool(re.search(r"(?i)security check|verify you are human|just a moment", page or "")
                and '"JobPosting"' not in (page or ""))


def jobright_item_jd(jr: dict) -> str:
    """Whatever description text the feed item itself carries."""
    parts = []
    for k, v in jr.items():
        if not _JOBRIGHT_TEXT_KEYS.search(k):
            continue
        if isinstance(v, str) and len(v.strip()) > 40:
            parts.append(strip_html(v))
        elif isinstance(v, list):
            items = [strip_html(x) if isinstance(x, str) else "" for x in v]
            items = [x for x in items if x]
            if items:
                parts.append("\n".join(f"- {x}" for x in items))
    return "\n\n".join(parts).strip()


def jobright_apply_url(jr: dict) -> str:
    """The employer's own posting, when the feed item names one."""
    for k in _JOBRIGHT_URL_KEYS:
        v = jr.get(k)
        if isinstance(v, str) and v.startswith("http") and "jobright.ai" not in v:
            return v
    return ""


def poll_jobright_app(conn, cookie: str | None = None) -> dict:
    s = {"fetched": 0, "filtered": 0, "new": 0, "skipped": 0, "error": None,
         "note": "Vaish's Jobright feed"}
    cookie = (cookie if cookie is not None else os.environ.get("JOBRIGHT_COOKIE", "")).strip()
    if not cookie:
        s["note"] = "JOBRIGHT_COOKIE not set; skipped"
        return {JOBRIGHT_APP_SOURCE: s}
    session = requests.Session()
    session.headers.update({**HEADERS, "Cookie": cookie})
    seen_now: set = set()
    try:
        for page in range(JOBRIGHT_APP_PAGES):
            if page:
                time.sleep(JOBRIGHT_APP_PAUSE)
            items = jobright_app_page(session, page * 20)
            if not items:
                break
            for item in items:
                jr, co = item["jobResult"], item.get("companyResult") or {}
                if "payload_keys" not in s:
                    # Field NAMES only, never values: tells the next reader what
                    # the feed carries without a cookie on the laptop.
                    s["payload_keys"] = ",".join(sorted(jr))[:400]
                s["fetched"] += 1
                title = (jr.get("jobTitle") or "").strip()
                company = (co.get("companyName") or "").strip()
                location = (jr.get("jobLocation") or "").strip()
                published = str(jr.get("publishTime") or "")
                posted = jobright_posted_at(published, published[:10])
                if not title or not company:
                    continue
                if NON_US.search(location) and not US_SIGNAL.search(location):
                    continue
                if db.is_provably_stale(posted, db.FETCH_WINDOW_HOURS):
                    s["stale"] = s.get("stale", 0) + 1
                    continue
                s["filtered"] += 1
                key = db.job_id(company, title, location)
                if key in seen_now or _seen_for_good(conn, company, title, location):
                    continue
                seen_now.add(key)
                url = f"https://jobright.ai/jobs/info/{jr.get('jobId')}"
                if jobright_is_repost(jr):
                    _record_repost(conn, s, company, title, location, url,
                                   JOBRIGHT_APP_SOURCE, posted, "jobright flag")
                    continue
                if why := board_repost(company, title):
                    _record_repost(conn, s, company, title, location, url,
                                   JOBRIGHT_APP_SOURCE, posted, why)
                    continue
                jd = ""
                if JOBSPY_RELEVANT.search(title) and hardfilter.hard_filter(title, "", location) is None:
                    # The employer's own posting first (2026-10-15): the feed
                    # item's text is Jobright's AI rewrite with the headings
                    # stripped, so a Preferred line reads as required (Google
                    # TPM I: "designing cages" is preferred live, and the scorer
                    # gated on it). Jobright's text stays the fallback.
                    if apply := jobright_apply_url(jr):
                        try:
                            jd = vc_boards.ats_jd(apply)
                        except Exception:
                            jd = ""
                        if len(jd) >= JD_MIN_CHARS:
                            s["jd_from_employer"] = s.get("jd_from_employer", 0) + 1
                        else:
                            # Which employer sites still need a reader: the run
                            # stats name each host that fell back to Jobright.
                            host = urlparse(apply).netloc or "?"
                            s[f"jd_rewrite_host:{host}"] = s.get(f"jd_rewrite_host:{host}", 0) + 1
                            jd = ""
                    else:
                        s["jd_no_apply_link"] = s.get("jd_no_apply_link", 0) + 1
                    if len(jd) < JD_MIN_CHARS:
                        jd = jobright_item_jd(jr)
                    if len(jd) < JD_MIN_CHARS:
                        try:
                            page = fetch_jobright_detail(url)
                        except Exception:
                            page = None
                        if page is not None and jobright_challenge(page):
                            s["challenge"] = s.get("challenge", 0) + 1
                            page = None          # never retried, never worked around
                        if page is not None:
                            if jobright_is_repost(page):
                                _record_repost(conn, s, company, title, location, url,
                                               JOBRIGHT_APP_SOURCE, posted, "jobright flag")
                                continue
                            jd, posted = jobright_detail_fields(page, posted)
                    if len(jd) < JD_MIN_CHARS:
                        s["jd_missing"] = s.get("jd_missing", 0) + 1
                _ingest_jobspy_row(conn, s, company, title, location, JOBRIGHT_APP_SOURCE,
                                   url, posted, jd)
    except Exception as e:
        s["error"] = f"{type(e).__name__}: {e}"
    return {JOBRIGHT_APP_SOURCE: s}


def main():
    ap = argparse.ArgumentParser(description="Layer 1 detection run")
    ap.add_argument("--hours-old", type=int, default=None,
                    help="Google probe window in hours (default 24; Indeed dropped 2026-10-09)")
    ap.add_argument("--linkedin-hours", type=int, default=None,
                    help="LinkedIn window in hours (default: since the last run + 2, "
                         "clamped 4-24)")
    ap.add_argument("--db", type=Path, default=None,
                    help="a scratch copy of jobs.sqlite; queue files go to a queue/ "
                         "beside it. For measuring a change without touching the repo.")
    ap.add_argument("--no-jobspy", action="store_true", help="skip the JobSpy breadth layer")
    ap.add_argument("--no-jobright", action="store_true",
                    help="skip the Jobright H1B aggregator (one HTTP GET)")
    # OFF by default, and the detection workflow does not pass it. Automated
    # collection from LinkedIn is against their terms of service, so enabling
    # this is a decision the operator makes for themselves. The Greenhouse,
    # Lever, Ashby and Workday APIs are the supported path and carry the run.
    ap.add_argument("--linkedin", action="store_true",
                    help="include LinkedIn in JobSpy (off by default; see their ToS)")
    # OFF by default for the same reason: Google's terms forbid automated scraping.
    ap.add_argument("--google", action="store_true",
                    help="include the Google Jobs probe in JobSpy (off by default; see their ToS)")
    ap.add_argument("--no-heartbeat", action="store_true",
                    help="skip the runs-table stamp (for a re-detect after a push race, "
                         "which is the same logical run and must not stamp it twice)")
    args = ap.parse_args()

    if args.db:
        global QUEUE_DIR
        QUEUE_DIR = args.db.resolve().parent / "queue"
        QUEUE_DIR.mkdir(exist_ok=True)
        ingest_scores.SCORES_DIR = args.db.resolve().parent / "scores"
    conn = db.connect(args.db) if args.db else db.connect()
    defaults, targets = load_targets()

    # Phase timings. There was NO instrumentation in this file until 2026-09-12,
    # which is exactly how the detection step drifted from the ~35s detect.yml's
    # own header still claimed to a measured 149s without anyone noticing: the
    # board count went 10 -> 69 and nothing reported what that cost. These
    # numbers put the next drift on the run page instead of behind a stopwatch.
    phases = {}
    t_phase = time.perf_counter()
    print(f"[layer1] polling {sum(1 for t in targets if t.get('board') in FETCHERS)} "
          f"ATS boards ({len(targets)} targets total, "
          f"{ATS_FETCH_WORKERS} fetch workers)...")
    stats = poll_ats(conn, defaults, targets)
    phases["ats"] = time.perf_counter() - t_phase
    print("[layer1] VC portfolio boards (Getro, Consider)...")
    t_phase = time.perf_counter()
    stats.update(poll_vc_boards(conn))
    phases["vc"] = time.perf_counter() - t_phase
    if not args.no_jobspy:
        print("[layer1] JobSpy sweep...")
        t_phase = time.perf_counter()
        stats.update(poll_jobspy(conn, args.hours_old, args.linkedin,
                                 linkedin_hours=args.linkedin_hours,
                                 include_google=args.google))
        phases["jobspy"] = time.perf_counter() - t_phase
    if not args.no_jobright:
        # Last on purpose: first insert wins dedupe, so the employer-side
        # sources get to claim a role before the aggregator does.
        print("[layer1] Jobright H1B sweep...")
        t_phase = time.perf_counter()
        stats.update(poll_jobright(conn))
        phases["jobright"] = time.perf_counter() - t_phase

    total_new = sum(s["new"] for s in stats.values())
    total_skipped = sum(s.get("skipped", 0) for s in stats.values())
    total_stale = sum(s.get("stale", 0) for s in stats.values())
    total_delisted = sum(s.get("delisted", 0) for s in stats.values())
    print("\n[layer1] phase timings: "
          + "  ".join(f"{k}={v:.1f}s" for k, v in phases.items())
          + f"  TOTAL={sum(phases.values()):.1f}s")
    print(f"\n{'source':<40}{'fetched':>8}{'stale':>7}{'matched':>8}{'new':>6}"
          f"{'offtgt':>8}{'delist':>8}")
    for src, s in stats.items():
        print(f"{src:<40}{s['fetched']:>8}{s.get('stale', 0):>7}{s['filtered']:>8}"
              f"{s['new']:>6}{s.get('skipped', 0):>8}{s.get('delisted', 0):>8}"
              + (f"   ERROR: {s['error']}" if s.get("error") else "")
              + (f"   SWEEP-ERROR: {s['sweep_error']}" if s.get("sweep_error") else "")
              + (f"   COVERAGE: {s['coverage_gap']}" if s.get("coverage_gap") else "")
              + (f"   [{s['jd_missing']} JD fetch(es) failed]" if s.get("jd_missing") else "")
              + (f"   [{s['reposted']} repost(s) revived]" if s.get("reposted") else "")
              + (f"   [{s['repost']} repost(s) skipped]" if s.get("repost") else "")
              # A climbing jd_empty is how "upstream changed the detail page"
              # looks from outside: every fetch succeeds and every JD is blank.
              + (f"   [{s['jd_empty']} page(s) carried no JD]" if s.get("jd_empty") else "")
              + (f"   [{s['detail_capped']} left for the next poll"
                 f" (detail cap {JOBRIGHT_DETAIL_CAP})]" if s.get("detail_capped") else "")
              + (f"   [{s['challenge']} bot-check page(s), not retried]" if s.get("challenge") else "")
              + (f"   [{s['jd_from_employer']} JD(s) from the employer posting]"
                 if s.get("jd_from_employer") else "")
              + (f"   ({s['note']})" if s.get("note") else ""))
        if s.get("payload_keys"):
            print(f"    {src} item fields: {s['payload_keys']}")
        hosts = {k.split(":", 1)[1]: v for k, v in s.items() if k.startswith("jd_rewrite_host:")}
        if hosts or s.get("jd_no_apply_link"):
            # Read with: gh run view <id> --log | grep "rewrite kept"
            print(f"    {src} rewrite kept (no employer reader), by host: "
                  + ", ".join(f"{h} {n}" for h, n in sorted(hosts.items(), key=lambda x: -x[1]))
                  + f"; no apply link: {s.get('jd_no_apply_link', 0)}")
    # Annotations surface on the run's summary page instead of only in the log,
    # which nobody opens on a run that reports success. Acknowledged-dead sources
    # are deliberately silent here: see acknowledged_degraded().
    ack = acknowledged_degraded()
    for src, s in stats.items():
        if s.get("error") and src not in ack:
            print(f"::warning title=Layer 1 source degraded::{src}: {s['error']}")
        if s.get("coverage_gap"):
            # Loud on purpose: unlike a dead source this one is silently
            # INCOMPLETE, which no downstream count reveals, and the fix is a
            # one-line constant change rather than waiting for upstream.
            print(f"::warning title=Layer 1 incomplete coverage::"
                  f"{src}: {s['coverage_gap']}")
        if s.get("sweep_error"):
            # Never acknowledged-degraded-able and never folded into `errors=`:
            # a broken sweep is a cleanup defect, not a dead source, and the
            # clock must keep meaning "is detection alive".
            print(f"::warning title=Layer 1 delisted sweep failed::"
                  f"{src}: {s['sweep_error']}")
    print(f"\n[layer1] {total_new} net-new job(s) written to queue/")
    if total_stale:
        print(f"[layer1] {total_stale} posting(s) older than the "
              f"{db.FETCH_WINDOW_HOURS}h window, not ingested "
              f"(no dedupe row: re-checked free on the next poll)")
    if total_skipped:
        print(f"[layer1] {total_skipped} off-target title(s) stored as 'skipped' "
              f"(kept for dedupe, never scored)")
    if total_delisted:
        print(f"[layer1] {total_delisted} posting(s) no longer on their board swept "
              f"to 'stale_skip' (queue entry + score sidecar dropped)")
    print(f"[layer1] db totals: {db.counts_by_status(conn)}")
    errors = [src for src, s in stats.items() if s.get("error")]
    fresh = [e for e in errors if e not in ack]
    known = [e for e in errors if e in ack]
    if not args.no_heartbeat:
        # `errors=` drives the dashboard clock's colour, so only NEWLY degraded
        # sources belong in it. Known-dead ones are recorded separately, which
        # keeps them on the page without making the clock permanently amber.
        db.record_run(conn, f"new={total_new} stale={total_stale} offtarget={total_skipped}"
                            + (f" errors={','.join(fresh)}" if fresh else "")
                            + (f" known_degraded={','.join(known)}" if known else ""))

    shed, freed = db.shed_jd_text(conn)
    if shed:
        print(f"[layer1] shed jd_text from {shed} settled row(s) ({freed / 1e3:.0f} KB)")

    # Exit code, not an exception: a partial scrape must still reach the commit
    # step and keep whatever it found. Only a run where EVERY live source failed
    # AND nothing new was found is a blind run, and that one must not show a
    # green check — "all 15 sources threw" and "a quiet hour" are indistinguishable
    # otherwise. detect.yml checks this after committing.
    live = [s for s in stats.values() if not s.get("note")]
    if live and all(s.get("error") for s in live) and total_new == 0:
        print("::error title=Layer 1 blind::every source failed and nothing was found")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
