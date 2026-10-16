"""Startup jobs from VC portfolio job boards (Layer 1 source, no login).

Two hosted platforms carry most VC portfolio boards, and both were probed on
2026-10-08:

GETRO (Accel, General Catalyst, Insight, 8VC, Techstars, ...). The board page
server-renders its first 20 jobs, newest first, honouring a base64 `filter`
query: {"job_functions": ["Product"], "searchable_locations": ["United States"]}
cut Accel's 29,788 jobs to 1,047, and the newest 20 spanned ~9 hours, so one
page per board per 2h run is enough. Read through the board page, not
api.getro.com: that host's robots.txt disallows everything, the boards allow
crawling. A job's own board page server-renders its full description.

CONSIDER (Sequoia, Lightspeed, Bessemer, GV, ...). GET /jobs sets a session
and carries a csrfToken; POST /api-boards/search-jobs with it takes
jobFunctions ["Product Management"] and postedSince P1D. robots.txt allows /.
No description in the feed, so the JD comes from the employer's ATS
(Greenhouse, Ashby or Lever public APIs), else the apply page's JSON-LD.

Pure fetch-and-normalise: rows out, no DB. scrape.poll_vc_boards inserts them.
Each row: company, title, location, url (the employer's apply link where the
board gives one), posted_at, jd_fetch (a callable for the JD, called only for
rows that reach the queue) and board.
"""

from __future__ import annotations

import base64
import html
import json
import re
from datetime import datetime, timezone

import requests

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
TIMEOUT = 20

GETRO_BOARDS = {
    "accel": "jobs.accel.com",
    "general-catalyst": "jobs.generalcatalyst.com",
    "insight": "jobs.insightpartners.com",
    "8vc": "jobs.8vc.com",
    "techstars": "jobs.techstars.com",
    "sapphire": "jobs.sapphireventures.com",
    "menlo": "jobs.menlovc.com",
    "ggv": "jobs.ggvc.com",
    "upfront": "jobs.upfront.com",
    "signalfire": "jobs.signalfire.com",
    "foundation": "jobs.foundationcapital.com",
    "nfx": "jobs.nfx.com",
}
CONSIDER_BOARDS = {
    "sequoia": "jobs.sequoiacap.com",
    "lightspeed": "jobs.lsvp.com",
    "bessemer": "jobs.bvp.com",
    "gv": "jobs.gv.com",
    "kleiner-perkins": "jobs.kleinerperkins.com",
    "battery": "jobs.battery.com",
    "felicis": "jobs.felicis.com",
    "usv": "jobs.usv.com",
    "crv": "jobs.crv.com",
    "initialized": "jobs.initialized.com",
    "first-round": "jobs.firstround.com",
}
GETRO_FILTER = {"job_functions": ["Product"], "searchable_locations": ["United States"]}
CONSIDER_QUERY = {"jobFunctions": ["Product Management"], "postedSince": "P1D"}

_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
_TAGS = re.compile(r"<[^>]+>")
_LD_JSON = re.compile(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', re.S)


class BoardError(RuntimeError):
    """The board answered but not in the shape this module reads."""


def text_of(html_text: str) -> str:
    t = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h\d>", "\n", html_text or "")
    t = html.unescape(_TAGS.sub(" ", t))
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", t)).strip()


def _next_data(page: str) -> dict:
    m = _NEXT_DATA.search(page or "")
    if not m:
        raise BoardError("no __NEXT_DATA__ on the page")
    return json.loads(m.group(1))


def _iso(epoch_or_str) -> str:
    """Epoch seconds -> ISO Z. A midnight-exact stamp is a date (day precision):
    Consider writes date-only postings as T00:00:00Z, which is not a time."""
    if isinstance(epoch_or_str, (int, float)):
        return datetime.fromtimestamp(epoch_or_str, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    s = str(epoch_or_str or "")
    if re.match(r"\d{4}-\d{2}-\d{2}T00:00:00(\.0+)?Z?$", s):
        return s[:10]
    return s


def _us_location(locs) -> str:
    """The first location naming the US, else the first one, as one string."""
    names = [str(x) for x in (locs or []) if x]
    for n in names:
        if re.search(r"(?i)\b(USA?|United States)\b|remote", n):
            return n
    return names[0] if names else ""


# --- Getro ---------------------------------------------------------------

def getro_jobs(host: str, session=None) -> list[dict]:
    s = session or requests
    f = base64.b64encode(json.dumps(GETRO_FILTER).encode()).decode()
    r = s.get(f"https://{host}/jobs", params={"filter": f}, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    try:
        found = _next_data(r.text)["props"]["pageProps"]["initialState"]["jobs"]["found"]
    except (KeyError, TypeError) as e:
        raise BoardError(f"jobs list missing ({e})")
    rows = []
    for j in found:
        org = j.get("organization") or {}
        if not j.get("title") or not org.get("name"):
            continue
        page = f"https://{host}/companies/{org.get('slug')}/jobs/{j.get('slug')}"
        rows.append({
            "company": org["name"].strip(), "title": j["title"].strip(),
            "location": _us_location(j.get("locations")),
            "url": j.get("url") or page,
            "posted_at": _iso(j.get("createdAt")),
            "jd_fetch": (lambda page=page, apply=j.get("url") or "": getro_or_employer_jd(page, apply)),
        })
    return rows


def getro_jd(job_page: str) -> str:
    r = requests.get(job_page, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    job = _next_data(r.text)["props"]["pageProps"]["initialState"]["jobs"].get("currentJob") or {}
    return text_of(job.get("description") or "")


def getro_or_employer_jd(page: str, apply: str) -> str:
    """Getro's own page first, then the employer's posting behind its apply link.
    Measured 2026-10-15: 16 of 23 queued Getro rows had no JD, because Getro's
    page is empty for postings it links out (HPE, Palo Alto, Workday tenants)."""
    try:
        jd = getro_jd(page)
    except Exception:
        jd = ""
    if len(jd) >= 500 or not apply or apply == page:
        return jd
    try:
        return ats_jd(apply) or jd
    except Exception:
        return jd


# --- Consider ------------------------------------------------------------

def consider_jobs(host: str) -> list[dict]:
    s = requests.Session()
    s.headers.update(HEADERS)
    page = s.get(f"https://{host}/jobs", timeout=TIMEOUT)
    page.raise_for_status()
    tok = re.search(r'csrfToken":"([^"]+)"', page.text)
    board = re.search(r'"board":\{"id":"([^"]+)","isParent":(true|false)', page.text)
    if not tok or not board:
        raise BoardError("no csrfToken or board id on /jobs")
    r = s.post(f"https://{host}/api-boards/search-jobs",
               json={"meta": {"size": 100},
                     "board": {"id": board.group(1), "isParent": board.group(2) == "true"},
                     "query": CONSIDER_QUERY},
               headers={"csrf-token": tok.group(1), "Origin": f"https://{host}",
                        "Referer": f"https://{host}/jobs"},
               timeout=TIMEOUT)
    r.raise_for_status()
    body = r.json()
    if "jobs" not in body:
        raise BoardError(f"no jobs key in the search response ({list(body)[:5]})")
    rows = []
    for j in body["jobs"]:
        if not j.get("title") or not j.get("companyName"):
            continue
        apply = j.get("applyUrl") or j.get("url") or ""
        rows.append({
            "company": j["companyName"].strip(), "title": j["title"].strip(),
            "location": _us_location(j.get("locations") or j.get("normalizedLocations")),
            "url": apply,
            "posted_at": _iso(j.get("timeStamp")),
            "jd_fetch": (lambda apply=apply: ats_jd(apply)),
        })
    return rows


# --- a JD from the employer's own ATS ----------------------------------------

_GREENHOUSE = re.compile(r"greenhouse\.io/(?:embed/job_app\?for=)?([\w-]+)/jobs/(\d+)")
_GH_JID = re.compile(r"[?&]gh_jid=(\d+)")
_ASHBY = re.compile(r"jobs\.ashbyhq\.com/([^/?#]+)/([0-9a-f-]{36})")
_LEVER = re.compile(r"jobs\.lever\.co/([^/?#]+)/([0-9a-f-]{36})")


def ats_jd(url: str) -> str:
    """The JD behind an apply link, from the ATS's public API, else JSON-LD.
    "" when nothing readable is there (the scorer scores what it can see)."""
    if m := _GREENHOUSE.search(url):
        r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{m.group(1)}/jobs/{m.group(2)}",
                         headers=HEADERS, timeout=TIMEOUT)
        if r.ok:
            return text_of(html.unescape(r.json().get("content") or ""))
    if m := _ASHBY.search(url):
        r = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{m.group(1)}",
                         headers=HEADERS, timeout=TIMEOUT)
        if r.ok:
            for j in r.json().get("jobs") or []:
                if j.get("id") == m.group(2):
                    return (j.get("descriptionPlain") or text_of(j.get("descriptionHtml") or "")).strip()
    if m := _LEVER.search(url):
        r = requests.get(f"https://api.lever.co/v0/postings/{m.group(1)}/{m.group(2)}",
                         headers=HEADERS, timeout=TIMEOUT)
        if r.ok:
            j = r.json()
            parts = [j.get("descriptionPlain") or ""] + [
                f"{x.get('text', '')}\n{text_of(x.get('content', ''))}" for x in j.get("lists") or []]
            return "\n\n".join(p for p in parts if p).strip()
    for pattern, resolve in _RESOLVERS:
        if m := pattern.search(url):
            return resolve(m)
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    if not r.ok:
        return ""
    return ld_job_description(r.text)


def ld_job_description(page: str) -> str:
    """The JobPosting description in a page's JSON-LD, or "". Tolerates the
    shapes Phenom and iCIMS sites use: @graph wrappers, a list @type, and raw
    control characters inside strings (strict=False)."""
    def walk(node):
        if isinstance(node, list):
            for x in node:
                yield from walk(x)
        elif isinstance(node, dict):
            yield node
            yield from walk(node.get("@graph") or [])
    for block in _LD_JSON.findall(page or ""):
        try:
            data = json.loads(block, strict=False)
        except ValueError:
            continue
        for obj in walk(data):
            kind = obj.get("@type")
            if "JobPosting" in (kind if isinstance(kind, list) else [kind]):
                return text_of(obj.get("description") or "")
    return ""


# --- per-ATS readers for apply links that carry no JSON-LD (2026-10-15) --------
# Each was probed live on 2026-10-15 against a real posting from the queue and
# returned the full JD: Workday's cxs API (Capital One, Adobe, Litera), Oracle
# Recruiting Cloud's REST API (Oracle), Eightfold (PayPal), Microsoft's pcsx API,
# SmartRecruiters (Canva), Workable, Rippling, TikTok's server-rendered page and
# Google's detail page. iCIMS (405 to scripts) and IBM (202, empty) did not
# answer and are left to the JSON-LD fallback.

def _json(url: str, **kw) -> dict:
    r = requests.get(url, headers={**HEADERS, "Accept": "application/json"},
                     timeout=TIMEOUT, **kw)
    return r.json() if r.ok else {}


def _workday(m) -> str:
    tenant, host, site, rest = m.groups()
    j = _json(f"https://{tenant}.{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/job/{rest}")
    return text_of((j.get("jobPostingInfo") or {}).get("jobDescription") or "")


def _oracle(m) -> str:
    host, site, jid = m.groups()
    j = _json(f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
              f"?expand=all&onlyData=true&finder=ById;Id=%22{jid}%22,siteNumber={site}")
    item = (j.get("items") or [{}])[0]
    return text_of("\n".join(item.get(k) or "" for k in (
        "ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr")))


def _eightfold(m) -> str:
    sub, jid = m.groups()
    j = _json(f"https://{sub}.eightfold.ai/api/apply/v2/jobs/{jid}", params={"domain": f"{sub}.com"})
    return text_of(j.get("job_description") or "")


def _microsoft(m) -> str:
    j = _json("https://apply.careers.microsoft.com/api/pcsx/position_details",
              params={"position_id": m.group(1), "domain": "microsoft.com", "hl": "en"})
    return text_of((j.get("data") or {}).get("jobDescription") or "")


def _smartrecruiters(m) -> str:
    company, jid = m.groups()
    j = _json(f"https://api.smartrecruiters.com/v1/companies/{company}/postings/{jid}")
    sections = (j.get("jobAd") or {}).get("sections") or {}
    return text_of("\n".join((v or {}).get("text") or "" for v in sections.values()))


def _workable(m) -> str:
    account, code = m.groups()
    j = _json(f"https://apply.workable.com/api/v2/accounts/{account}/jobs/{code}")
    return text_of("\n".join(j.get(k) or "" for k in ("description", "requirements", "benefits")))


def _rippling(m) -> str:
    board, jid = m.groups()
    d = _json(f"https://api.rippling.com/platform/api/ats/v1/board/{board}/jobs/{jid}").get("description")
    if isinstance(d, dict):
        d = "\n".join(v for v in d.values() if isinstance(v, str))
    return text_of(d or "")


def _tiktok(m) -> str:
    """TikTok's page is server-rendered with no JSON-LD: the JD is the text from
    "Responsibilities" up to "Job Information" (pay and boilerplate follow)."""
    r = requests.get(f"https://lifeattiktok.com/search/{m.group(1)}", headers=HEADERS,
                     timeout=TIMEOUT)
    if not r.ok:
        return ""
    page = text_of(r.text)
    start = page.find("Responsibilities")
    if start < 0:
        return ""
    ends = [i for i in (page.find("Job Information", start), page.find("About TikTok", start)) if i > 0]
    return page[start:min(ends) if ends else len(page)].strip()


def _google(m) -> str:
    """Google's detail page carries the job as its own AF_initDataCallback row,
    the same shape bigtech.google_row already reads off the search page."""
    import bigtech  # lazy: bigtech imports this module
    r = requests.get(m.group(0), headers=HEADERS, timeout=TIMEOUT)
    if not r.ok:
        return ""
    for _key, raw in bigtech._AF_BLOB.findall(r.text):
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        # 'ds:0' on a detail page is [row, ...]; the row's [0] is the job id.
        row = data[0] if isinstance(data, list) and data and isinstance(data[0], list) else data
        if bigtech._google_job_shaped(row) and row[0] == m.group(1):
            return bigtech.google_row(row)["jd"]
    return ""


_RESOLVERS = [
    (re.compile(r"https://([\w-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?"
                r"([^/?#]+)/(?:job|details)/([^?#]+)"), _workday),
    (re.compile(r"https://([\w.-]+\.oraclecloud\.com)/hcmUI/CandidateExperience/[a-z]{2}(?:-[A-Z]{2})?"
                r"/sites/([^/?#]+)/jobs?/(\d+)"), _oracle),
    (re.compile(r"https://([\w-]+)\.eightfold\.ai/careers/job/(\d+)"), _eightfold),
    (re.compile(r"(?:apply\.careers\.microsoft\.com/careers/job|jobs\.careers\.microsoft\.com/"
                r"global/en/job)/(\d+)"), _microsoft),
    (re.compile(r"jobs\.smartrecruiters\.com/([^/?#]+)/(\d+)"), _smartrecruiters),
    (re.compile(r"apply\.workable\.com/([^/?#]+)/j/(\w+)"), _workable),
    (re.compile(r"ats\.rippling\.com/([^/?#]+)/jobs/([0-9a-f-]{36})"), _rippling),
    (re.compile(r"(?:lifeattiktok\.com/search|careers\.tiktok\.com/position)/(\d+)"), _tiktok),
    (re.compile(r"https://www\.google\.com/about/careers/applications/jobs/results/(\d+)[^?#]*"), _google),
]
