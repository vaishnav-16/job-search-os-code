"""SQLite store + dedupe logic for Layer 1 detection.

Dedupe key (per CLAUDE.md): SHA1(normalized company + "|" + normalized title
+ "|" + location bucket) — NOT URL-based. Seen jobs never resurface.

Seed rule: rows imported from profile/seed_applied.csv have no location, so a
seeded company+title pair blocks that pairing in EVERY location (wildcard).
"""

import argparse
import csv
import hashlib
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

PIPELINE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PIPELINE_DIR.parent
DB_PATH = PIPELINE_DIR / "jobs.sqlite"
SEED_CSV = REPO_ROOT / "profile" / "seed_applied.csv"

STATUSES = {"new", "scored", "tailored", "needs_review", "stale_skip",
            "applied", "rejected", "skipped",
            "interview", "offer", "baseline_skip"}

# Statuses whose transitions are worth remembering (Phase 6). A transition is
# recorded when EITHER side is tracked, so `tailored -> stale_skip` is kept --
# a finished resume swept before its sidecar folded is exactly the loss worth
# measuring -- while `new -> skipped`, `new -> scored` and `new -> stale_skip`
# record nothing at all.
#
# The exclusion is the whole design, not laziness. Recording every status write
# is ~500 rows/day (the hard filter's ~380 `skipped`, the freshness sweep's
# ~123 `stale_skip`, plus baseline and delisted) inside a binary committed ~25x
# a day -- the shape that took .git to 166 MB in four days (LEARNINGS
# 2026-08-18). Under this rule the table starts at 27 rows and grows ~20/day.
#
# Consequence worth stating positively: no path outside set_status can produce a
# tracked transition, so filter.py's raw `UPDATE jobs SET status='skipped'` is a
# non-case rather than an exemption. test_outcomes asserts that rather than
# leaving it to a comment.
TRACKED_STATUSES = ("tailored", "needs_review", "applied",
                    "rejected", "interview", "offer")

# An application with no recorded movement for this long is worth a nudge.
STALE_APPLIED_DAYS = 14
# A per-channel rate needs this many applications recorded THROUGH the pipeline
# (via != 'backfill') before it is printed. "0 of N" rendered as "0% interview rate"
# is a measurement artifact, not a finding.
FUNNEL_RATE_MIN_N = 10

# The lane the job-scorer assigns, written as the FIRST `;`-clause of `notes`
# (see set_lane). Closed set, same as STATUSES: a typo'd lane is invisible to
# every reader downstream, which is how a job silently stops being tailorable.
LANES = ("auto-tailor", "digest-stretch", "digest", "skip")

COMPANY_ALIASES = {
    "meta platforms": "meta",
    "google llc": "google",
    "alphabet": "google",
    "microsoft corporation": "microsoft",
    "relay pro": "relay",
    "relaypro": "relay",
    "postman labs": "postman",
    "postmanlabs": "postman",
}

# Roman numerals / spelled numbers folded so "PM I"/"PM II" == "PM 1"/"PM 2" reposts dedupe.
_NUMERAL_MAP = {"i": "1", "ii": "2", "iii": "3", "iv": "4", "one": "1", "two": "2", "three": "3"}

_COMPANY_SUFFIXES = re.compile(r"\b(inc|llc|ltd|corp|corporation|co)\b\.?$")

US_STATES = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id", "il", "in",
    "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms", "mo", "mt", "ne", "nv",
    "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok", "or", "pa", "ri", "sc", "sd", "tn",
    "tx", "ut", "vt", "va", "wa", "wv", "wi", "wy", "dc",
}

_STATE_NAMES = {
    "alabama": "al", "alaska": "ak", "arizona": "az", "arkansas": "ar", "california": "ca",
    "colorado": "co", "connecticut": "ct", "delaware": "de", "florida": "fl", "georgia": "ga",
    "hawaii": "hi", "idaho": "id", "illinois": "il", "indiana": "in", "iowa": "ia",
    "kansas": "ks", "kentucky": "ky", "louisiana": "la", "maine": "me", "maryland": "md",
    "massachusetts": "ma", "michigan": "mi", "minnesota": "mn", "mississippi": "ms",
    "missouri": "mo", "montana": "mt", "nebraska": "ne", "nevada": "nv",
    "new hampshire": "nh", "new jersey": "nj", "new mexico": "nm", "new york": "ny",
    "north carolina": "nc", "north dakota": "nd", "ohio": "oh", "oklahoma": "ok",
    "oregon": "or", "pennsylvania": "pa", "rhode island": "ri", "south carolina": "sc",
    "south dakota": "sd", "tennessee": "tn", "texas": "tx", "utah": "ut", "vermont": "vt",
    "virginia": "va", "washington": "wa", "west virginia": "wv", "wisconsin": "wi",
    "wyoming": "wy",
}


def norm_company(company: str) -> str:
    s = re.sub(r"[^\w\s]", " ", (company or "").lower())
    s = re.sub(r"\s+", " ", s).strip()
    s = _COMPANY_SUFFIXES.sub("", s).strip()
    return COMPANY_ALIASES.get(s, s)


def norm_title(title: str) -> str:
    s = re.sub(r"[^\w\s]", " ", (title or "").lower())
    words = [_NUMERAL_MAP.get(w, w) for w in s.split()]
    return " ".join(words)


# Boards often give a bare city with no state ("San Francisco HQ", "NYC, Chicago").
# Without this, those land in the empty bucket, which costs dedupe precision.
_CITY_STATES = {
    "san francisco": "ca", "sf bay": "ca", "los angeles": "ca", "san jose": "ca",
    "palo alto": "ca", "mountain view": "ca", "menlo park": "ca", "san diego": "ca",
    "oakland": "ca", "sunnyvale": "ca", "santa clara": "ca", "irvine": "ca",
    "new york": "ny", "nyc": "ny", "brooklyn": "ny", "manhattan": "ny",
    "seattle": "wa", "bellevue": "wa", "redmond": "wa",
    "austin": "tx", "dallas": "tx", "houston": "tx", "san antonio": "tx",
    "boston": "ma", "cambridge": "ma", "merrimack": "nh", "hanover": "nh",
    "chicago": "il", "denver": "co", "boulder": "co", "atlanta": "ga",
    "miami": "fl", "orlando": "fl", "tampa": "fl", "phoenix": "az", "tempe": "az",
    "portland": "or", "salt lake": "ut", "nashville": "tn", "raleigh": "nc",
    "durham": "nc", "charlotte": "nc", "columbus": "oh", "detroit": "mi",
    "minneapolis": "mn", "philadelphia": "pa", "pittsburgh": "pa",
    "washington dc": "dc", "arlington": "va", "mclean": "va", "reston": "va",
}


def location_bucket(location: str) -> str:
    """'remote' if remote; else 2-letter US state if extractable; else ''."""
    s = (location or "").lower()
    if not s.strip():
        return ""
    if "remote" in s:
        return "remote"
    # Scan tokens RIGHT to LEFT: boards write "City, ST, US", so the state sits
    # near the end. Left-to-right misreads city words as state codes -- "La Vista,
    # NE" became Louisiana, and "Orange, CA" would hit OR (Oregon) first.
    tokens = [t for t in re.split(r"[^\w]+", s) if t]
    for token in reversed(tokens):
        if token in ("us", "usa"):
            continue
        if token in US_STATES:
            return token
    for name, code in _STATE_NAMES.items():
        if name in s:
            return code
    for city, code in _CITY_STATES.items():
        if city in s:
            return code
    return ""


def job_id(company: str, title: str, location: str) -> str:
    key = f"{norm_company(company)}|{norm_title(title)}|{location_bucket(location)}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # The two-writer rule is a convention, not a lock: nothing stops a /scan
    # session and the scheduled sync from opening this file a second apart. Wait
    # for the other writer instead of raising "database is locked" — the writes
    # here are short, and a failed sync is far more expensive than a 10s wait.
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id            TEXT PRIMARY KEY,
            company       TEXT NOT NULL,
            company_norm  TEXT NOT NULL,
            title         TEXT NOT NULL,
            title_norm    TEXT NOT NULL,
            location      TEXT DEFAULT '',
            loc_bucket    TEXT DEFAULT '',
            url           TEXT DEFAULT '',
            source        TEXT NOT NULL,
            jd_text       TEXT DEFAULT '',
            posted_at     TEXT DEFAULT '',
            first_seen    TEXT NOT NULL,
            status        TEXT NOT NULL DEFAULT 'new',
            score         INTEGER,
            notes         TEXT DEFAULT ''
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_pair ON jobs (company_norm, title_norm)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs (status)")
    # Layer 1 heartbeat. The dashboard shows the last run's timestamp because a
    # silently dead cron looks exactly like a quiet job market; a run with zero
    # new jobs still writes a row.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            ts      TEXT NOT NULL,
            summary TEXT DEFAULT ''
        )
    """)
    # Two writers stamp this table and the clock only means anything for one of
    # them. Added 2026-08-18; existing rows are tagged from the summary prefix
    # sync.py has always written, so the backfill is exact rather than assumed.
    if not any(r[1] == "kind" for r in conn.execute("PRAGMA table_info(runs)")):
        conn.execute("ALTER TABLE runs ADD COLUMN kind TEXT DEFAULT 'layer1'")
        conn.execute("UPDATE runs SET kind = 'sync' WHERE summary LIKE 'sync:%'")
    # Outcome capture (Phase 6, 2026-09-01). A single `status_changed_at` column
    # cannot answer applied -> interview -> offer per channel and is destroyed by
    # a re-mark, so transitions get their own table. Only TRACKED_STATUSES
    # transitions are written -- see set_status for why, and for how much this
    # table would weigh if every status write landed here.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS status_history (
            id     TEXT NOT NULL,
            status TEXT NOT NULL,
            at     TEXT NOT NULL,
            via    TEXT NOT NULL DEFAULT '',
            UNIQUE (id, status, at)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_hist_id ON status_history (id, at)")
    # Backfill: every `applied` row predates this table. Guarded on "no applied
    # history for THIS id" rather than "no history at all", so a row that later
    # gains a real transition is never re-backfilled.
    #
    # `first_seen`, never now(): it is a stored column, so this statement
    # produces byte-identical rows in the local seat, the Actions cron and CI.
    # A now()-stamped backfill would make the committed binary differ per seat,
    # which on a file three writers commit is an unmergeable diff waiting to
    # happen. It is also a LOWER bound on the application time (detection
    # precedes applying), which is why `via='backfill'` rows are barred from the
    # nudge -- the same interval doctrine as job_age: never collapse a bound to
    # a number and then act on it.
    #
    # Deliberately NOT extended to `rejected`/`interview`/`offer` rows: a row at
    # `rejected` may be Vaish rejecting the POSTING (one live row's note reads
    # "duplicate posting"), not an employer rejecting him, and inventing an
    # `applied` transition for it would put a fictional application in the funnel.
    conn.execute("""
        INSERT OR IGNORE INTO status_history (id, status, at, via)
        SELECT id, 'applied', first_seen, 'backfill' FROM jobs
         WHERE status = 'applied'
           AND id NOT IN (SELECT id FROM status_history WHERE status = 'applied')
    """)
    conn.commit()
    return conn


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def record_run(conn, summary: str = "", kind: str = "layer1") -> None:
    """Stamp a completed run. `kind` is 'layer1' (detection) or 'sync' (Layer 2).

    The distinction is load-bearing, not bookkeeping: the dashboard clock exists
    to tell a dead detection cron from a quiet market, and both writers share
    this table. Untagged, the newest row was often a sync — so every Layer 2 run
    reset the clock to green and the cron could stay dead for a day without the
    page ever saying so (found 2026-08-18).
    """
    conn.execute("INSERT INTO runs (ts, summary, kind) VALUES (?, ?, ?)",
                 (now_iso(), summary, kind))
    conn.commit()


def last_run(conn, kind: str = "layer1") -> tuple[str, str] | None:
    """Most recent run of `kind`; pass kind=None for the newest row of any kind."""
    if kind is None:
        row = conn.execute(
            "SELECT ts, summary FROM runs ORDER BY ts DESC LIMIT 1").fetchone()
    else:
        row = conn.execute("SELECT ts, summary FROM runs WHERE kind = ? "
                           "ORDER BY ts DESC LIMIT 1", (kind,)).fetchone()
    return (row[0], row[1]) if row else None


def parse_posted_at(value: str):
    """Boards report posting dates in four shapes. Returns a UTC datetime or None.

    Greenhouse/Ashby: ISO 8601 with offset. Lever: epoch milliseconds as a string.
    JobSpy: plain YYYY-MM-DD. Unparseable stays None so the caller can treat
    unknown age as unknown rather than silently assuming "fresh" or "stale".
    """
    s = (value or "").strip()
    if not s:
        return None
    if s.isdigit():  # Lever epoch ms (seconds if it is a shorter number)
        n = int(s)
        try:
            return datetime.fromtimestamp(n / 1000 if n > 10**11 else n, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def days_since_posted(value: str):
    """Age in days, or None when the posting date is unknown/unparseable."""
    dt = parse_posted_at(value)
    if dt is None:
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds() / 86400


# A date-only posting date ("2026-08-14") is not an age, it is a 24-hour-wide
# INTERVAL: the role went up somewhere between 00:00 and 23:59 that day. 82% of
# the corpus is date-only (JobSpy/Indeed), so collapsing the interval to a single
# number over-estimates age by up to 24h and would reject roles that are actually
# ~30h old. Everything downstream reasons over (best, worst) instead.
STALE_HOURS = 48        # Vaish's policy: past this, applying is not worth it
FRESH_HOURS = 24        # the target: apply inside this window
FETCH_WINDOW_HOURS = 72 # what Layer 1 will even ingest (generous on purpose)


def job_age(posted_at: str, first_seen: str = "", now=None):
    """(best_hours, worst_hours, precision) — how old is this posting?

    best_hours is the youngest the job could be, worst_hours the oldest.
    precision is "exact" (board gave a timestamp), "day" (date-only, so the
    two bounds sit 24h apart), "seen" (no posting date, but we have observed
    the row, which bounds its age from below), or "unknown" (nothing at all).

    **Observation is proof of age.** A posting recorded four days ago is at
    least four days old, whatever its posting date claims — so `first_seen` is
    a second lower bound on `best_hours` and the two are combined with max().
    It is a LOWER bound only, in both directions of use: it can prove a row is
    old and can never prove one is young, so `worst_hours` never moves down
    from it and a recent sighting tightens nothing. Before 2026-08-23 this
    parameter was accepted and ignored, which left 54 of 197 work rows (27%)
    demonstrably past the apply window and unsweepable because their posting
    date was a 24h-wide interval.
    """
    now = now or datetime.now(timezone.utc)
    dt = parse_posted_at(posted_at)
    seen = parse_posted_at(first_seen)
    # Floored at 0 for the same reason the day branch floors: a clock skew
    # must never report a negative age, which reads as "from the future".
    seen_hours = None if seen is None else max(0.0, (now - seen).total_seconds() / 3600)
    if dt is None:
        # No posting date, but a real sighting: a genuine lower bound and NO
        # upper one, so worst stays None. Reported as "seen" rather than folded
        # into "day"/"exact" so every caller can word it honestly — this row's
        # age is "at least N hours", not "N hours".
        return (None, None, "unknown") if seen_hours is None else (seen_hours, None, "seen")
    hours = (now - dt).total_seconds() / 3600
    if len((posted_at or "").strip()) == 10:      # YYYY-MM-DD, no time component
        # dt is midnight, i.e. the OLDEST the posting can be. The youngest is
        # 24h later (just before the next midnight), floored at 0.
        best, worst, precision = max(0.0, hours - 24), hours, "day"
    else:
        best, worst, precision = hours, hours, "exact"
    if seen_hours is not None and seen_hours > best:
        best = seen_hours
        # best <= worst is an invariant every caller reasons with (age_bar draws
        # worst-minus-best as a hatch, age_label prints them as a range). The
        # two bounds genuinely cross when a board forward-dates a posting or a
        # local-date/UTC skew puts our sighting before the claimed midnight;
        # clamping keeps the interval well-formed instead of emitting a
        # negative width. Only the FLOOR moves — `worst` is never loosened.
        worst = max(worst, best)
    return (best, worst, precision)


def is_provably_stale(posted_at: str, threshold_hours: float = STALE_HOURS,
                      now=None, first_seen: str = "") -> bool:
    """True only when the job is older than the threshold at the OPTIMISTIC end.

    The asymmetry that makes this safe: board dates are unreliable for proving
    something is fresh (Indeed's 24h filter leaks ~14%) but reliable for proving
    it is old. Unknown-and-never-seen is never stale, and `first_seen` does not
    weaken that: a row with neither a posting date nor a sighting still has no
    lower bound at all. (This used to cite triage_baseline.py as sharing the
    doctrine. It does not and never did: that module reasons over
    days_since_posted, a single pessimistic number, which is exactly what
    freshness.py's docstring says it supersedes — and it never gained the
    observation bound either.)

    `first_seen` is optional and defaults off so every ingest-path caller keeps
    today's behaviour exactly. That matters: a row being ingested has no prior
    sighting by construction, and a first_seen bound applied at fetch time would
    be reading the clock of the row we are about to create.

    `now` is injectable so tests can pin the clock. Without it a test asserting
    "yesterday is not stale" against a hardcoded date silently rots into a
    failure a few days later, which is exactly what happened (2026-08-17).
    """
    best, _, precision = job_age(posted_at, first_seen, now=now)
    return precision != "unknown" and best > threshold_hours


# The four freshness tiers, in the order they should be worked. Lives here
# beside job_age because it is the same interval knowledge: the dashboard and
# freshness.py both used to restate it, and a rule stated twice is a rule that
# eventually disagrees with itself.
#
# "seen" NEVER reaches `now` or `soon`. The observation bound proves a row is
# OLD; it says nothing whatever about a missing posting date being recent, so a
# row seen two hours ago stays undecidable ("nodate") rather than being promoted
# to the top of the board on no evidence.
def age_tier(best, precision: str) -> str:
    if precision == "unknown":
        return "nodate"
    if precision == "seen":
        return "stale" if best > STALE_HOURS else "nodate"
    if best <= FRESH_HOURS:
        return "now"
    return "soon" if best <= STALE_HOURS else "stale"


def _ago(hours: float) -> str:
    return f"{hours:.0f}h ago" if hours < 72 else f"{hours / 24:.0f}d ago"


def age_label(best, worst, precision: str) -> str:
    """Short human age for the dashboard and digests.

    Each precision gets the wording its evidence supports, and no more. An
    `exact` timestamp is the only one that can name a real age, so it is the
    only one that says "posted"; `day` prints its 24h-wide interval as an
    interval, because collapsing it to a number is a lie on 82% of the corpus;
    `seen` says "seen", because a sighting is when WE found it, not when it went
    up; `unknown` says nothing at all.
    """
    if precision == "unknown":
        return "age unknown"
    if precision == "day":
        return f"{best:.0f}-{worst:.0f}h" if worst < 72 else f"{worst / 24:.0f}d"
    if precision == "seen":
        return f"seen {_ago(best)}"
    return f"posted {_ago(best)}"


def is_seen(conn: sqlite3.Connection, company: str, title: str, location: str) -> bool:
    """Seen = the exact dedupe id exists, OR a SEED row matches company+title.

    The seed wildcard exists only because seed_applied.csv has no location data,
    so a pre-pipeline application must block that company+title everywhere. It is
    restricted to source='seed' on purpose: scraped rows can also land in the empty
    bucket (e.g. "San Francisco HQ"), and letting those act as wildcards would
    silently suppress the same role in every other city, forever and invisibly.
    """
    jid = job_id(company, title, location)
    if conn.execute("SELECT 1 FROM jobs WHERE id = ?", (jid,)).fetchone():
        return True
    row = conn.execute(
        "SELECT 1 FROM jobs WHERE company_norm = ? AND title_norm = ? AND source = 'seed'",
        (norm_company(company), norm_title(title)),
    ).fetchone()
    return row is not None


def is_revivable(conn: sqlite3.Connection, company: str, title: str, location: str) -> bool:
    """A seen role whose row aged out before anyone worked it (2026-10-08).

    LinkedIn and Indeed repost a role under a new listing with a fresh date, and
    dedupe maps it to the old row, so the repost used to be ignored. That is
    right for a role scored, tailored, applied or delisted, and wrong for one the
    age sweep took while it was still unscored ('stale_skip' from freshness,
    which only sweeps 'new' rows): the repost proves the role is still open, and
    nothing else would ever bring it back.
    """
    row = conn.execute("SELECT status, notes FROM jobs WHERE id = ?",
                       (job_id(company, title, location),)).fetchone()
    return (row is not None and row[0] == "stale_skip"
            and "delisted" not in (row[1] or "").lower())


def revive_repost(conn, company, title, location, url, source, jd_text="",
                  posted_at="") -> str:
    """Put an aged-out role back in the queue as a new sighting; returns its id.

    first_seen moves to now: it is the floor on a row's age, and the old one
    would prove the repost stale on arrival. The note records what happened."""
    jid = job_id(company, title, location)
    conn.execute(
        "UPDATE jobs SET url = ?, source = ?, jd_text = CASE WHEN ? != '' THEN ? ELSE jd_text END, "
        "posted_at = ?, first_seen = ?, score = NULL, notes = ? WHERE id = ?",
        (url, source, jd_text, jd_text, posted_at, now_iso(),
         f"repost: role aged out unworked, seen again {now_iso()[:10]}", jid))
    set_status(conn, jid, "new", via="repost")
    conn.commit()
    return jid


def insert_job(conn, company, title, location, url, source, jd_text="",
               posted_at="", status="new", notes="") -> str:
    if status not in STATUSES:
        raise ValueError(f"invalid status: {status}")
    jid = job_id(company, title, location)
    conn.execute(
        """INSERT OR IGNORE INTO jobs
           (id, company, company_norm, title, title_norm, location, loc_bucket,
            url, source, jd_text, posted_at, first_seen, status, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (jid, company, norm_company(company), title, norm_title(title),
         location or "", location_bucket(location), url or "", source,
         jd_text, posted_at, now_iso(), status, notes),
    )
    conn.commit()
    return jid


# Statuses whose rows still need their JD body in the DB, which is exactly the
# set sync.reconcile_queue can re-emit a lost queue file from: `new` also feeds
# filter.sweep, and `scored` holds until the job is tailored. Import this rather
# than re-listing the pair — the shed and the re-emit must never disagree about
# which rows are recoverable.
#
# `needs_review` deliberately sits outside it. Such a row keeps its queue file
# (it is human-owned: Vaish resumes, applies anyway, or skips) but its JD lives
# in applications/<folder>/jd.md by then, so the DB copy is dead weight and a
# re-emit from the DB would write an empty JD. Losing that queue file costs
# nothing — the job is already on the dashboard with its artifact.
JD_KEEP_STATUSES = ("new", "scored")


def shed_jd_text(conn, vacuum: bool = False) -> tuple[int, int]:
    """Drop stored JD bodies for rows that have left the work set.

    jd_text is the DB's only unbounded column: 15.2 MB of a 17.9 MB file on
    2026-08-18, inside a committed binary rewritten ~25x/day — while the rows
    that actually need their JD held 1.7 MB of it. Returns (rows, bytes freed).

    `vacuum` is off by default, and deliberately so: the hourly cron calls this
    every run, and VACUUM rewrites the entire file from scratch. Skipping it
    costs nothing there, because SQLite hands freed pages back to the freelist
    and the next hour's inserts reuse them — the file self-bounds without ever
    being rebuilt. Only the twice-daily sync passes vacuum=True, which is often
    enough to keep the file compact. (Measured on the one VACUUM that has run:
    git still delta-compressed the result to 6 KB on disk, so this is about not
    doing pointless work 25x/day rather than about repacking cost.)
    """
    ph = ",".join("?" * len(JD_KEEP_STATUSES))
    freed = conn.execute(
        f"SELECT COALESCE(SUM(LENGTH(jd_text)), 0) FROM jobs "
        f"WHERE status NOT IN ({ph}) AND jd_text != ''",
        JD_KEEP_STATUSES).fetchone()[0]
    shed = conn.execute(
        f"UPDATE jobs SET jd_text = '' WHERE status NOT IN ({ph}) AND jd_text != ''",
        JD_KEEP_STATUSES).rowcount
    conn.commit()
    if vacuum:
        free_bytes = (conn.execute("PRAGMA freelist_count").fetchone()[0]
                      * conn.execute("PRAGMA page_size").fetchone()[0])
        if free_bytes > 256 * 1024:
            conn.execute("VACUUM")
    return shed, freed


# A row past this point has an artifact behind it: a tailored resume, or a
# human decision made on one. `rejected` and `skipped` are deliberately absent
# — those rows never produced a resume, so they must not suppress one.
WORKED_STATUSES = ("tailored", "needs_review", "applied", "interview", "offer")

# Corporate-form words folded for the ROLE key only, never for the dedupe key.
# The asymmetry is the whole design: this key may be looser than the detection
# key, because a false match here costs one skipped duplicate (the outcome we
# want) while a false match in detection buries a real job forever. Measured
# 2026-08-25 on "TikTok USDS Joint Venture" against "TikTok USDS JV".
_ROLE_FORM_WORDS = re.compile(r"\b(joint venture|jv)\b")

# One employer under several board spellings, folded for the ROLE key only (the
# tailor dedup and the per-company cap), never for norm_company: that would
# re-key every historical dedupe id. Measured 2026-09-28 over 6,928 split keys:
# Amazon alone sat under four spellings (423 rows). Business units that are
# distinct hiring orgs (Google DeepMind, GE Vernova, Siemens Healthineers,
# TikTok USDS) stay separate on purpose.
ROLE_COMPANY_ALIASES = {
    "amazon com": "amazon",
    "amazon web services": "amazon",
    "amazon web services aws": "amazon",
    "nvidia ai": "nvidia",
    "anduril industries": "anduril",
    "l3harris technologies": "l3harris",
    "hewlett packard enterprise hpe": "hewlett packard enterprise",
    "pnc financial services group": "pnc",
    "general motors gm": "general motors",
    "microsoft ai": "microsoft",
    "walmart global tech": "walmart",
    "honeywell aerospace": "honeywell",
    "honeywell technologies": "honeywell",
    "intuitive intuitive surgical": "intuitive surgical",
    "intuitive": "intuitive surgical",
    "nimble robotics": "nimble",           # LinkedIn vs Indeed, one req (09-28)
    "google deepmind": "deepmind",         # one req posted under both names (10-08)
    "huron consulting group": "huron",    # same pair, same run
}

# A trailing requisition code, folded for the ROLE key only: a repost under a new
# code is the same work ("Macro (7B48D45)" vs "Macro (45EB295)", byte-identical
# JD, 09-29). One token, 5+ characters, holding a digit AND a letter, so
# "(DICOM)", "(2026)" and "(Level 2)" survive.
_REQ_CODE = re.compile(
    r"\s*[(\[]\s*(?=[A-Za-z0-9-]*\d)(?=[A-Za-z0-9-]*[A-Za-z])[A-Za-z0-9-]{5,}\s*[)\]]\s*$")


def role_title(title: str) -> str:
    """norm_title minus a trailing requisition code (role key only, never job_id)."""
    return norm_title(_REQ_CODE.sub("", title or ""))


def role_company_key(company: str) -> str:
    """norm_company, plus folding that detection must not do (corporate form, aliases)."""
    key = re.sub(r"\s+", " ", _ROLE_FORM_WORDS.sub("", norm_company(company or ""))).strip()
    return ROLE_COMPANY_ALIASES.get(key, key)


def role_key(company: str, title: str) -> tuple:
    """The identity of a piece of WORK, as opposed to a posting.

    Location is deliberately not part of it: `preferences.md` records Vaish as
    open to relocation anywhere in the US, so two rows differing only by city
    are one job and would produce a byte-identical resume.
    """
    return (role_company_key(company), role_title(title))


def connect_readonly(db_path: Path = DB_PATH) -> sqlite3.Connection:
    """Open the DB with SQLite's own read-only mode, for readers that must not write.

    `connect()` is the wrong API for a reader: it runs CREATE TABLE / CREATE
    INDEX / ALTER IF NOT EXISTS and then `commit()`, which opens the file
    read-write and takes a write lock even when every statement is a no-op on an
    already-migrated database. The cloud tailor routine is told in as many words
    never to modify `jobs.sqlite` — it is a committed binary with exactly two
    writers — so it needs a connection that CANNOT, not one that merely does not.
    `mode=ro` makes any write attempt raise instead of silently succeeding.
    """
    conn = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def worked_role_keys(conn) -> set:
    """(norm_company, norm_title) for every role an artifact already exists for.

    The de-duplication key is SHA1(norm_company + norm_title + location bucket),
    which is right for DETECTION and wrong for deciding what to tailor: one
    posting legitimately produces several rows, and the resume for each would be
    byte-identical. Measured 2026-08-25 — one TikTok role sat in the DB three
    times ("TikTok USDS Joint Venture"/Seattle, "TikTok USDS JV"/San Jose,
    "TikTok USDS JV"/Seattle), and two separate Opus tailor runs were spent on
    it: the cloud routine took one row at 06:30 UTC and a local session took
    another 90 minutes later.

    Location is excluded on purpose. It is part of the detection key by design,
    but `preferences.md` records Vaish as open to relocation anywhere in the US,
    so two rows differing only by city are the same work. Folding the company
    suffix instead was measured and rejected: it would not have merged those two
    rows (their cities differ) and it would have re-keyed 7 already-seen rows,
    re-surfacing jobs that the "seen jobs never resurface" rule exists to bury.
    """
    return {role_key(c, t) for c, t in conn.execute(
        "SELECT company, title FROM jobs WHERE status IN "
        f"({','.join('?' * len(WORKED_STATUSES))})", WORKED_STATUSES)}


def _record_transition(conn, jid: str, current: str, status: str,
                       at: str | None, via: str) -> None:
    """Append one row to status_history, if this transition is worth keeping.

    Two guards, and it is worth being precise about which does the work:

    `current != status` is the real idempotency. It covers a re-mark to the
    status a row already holds, and it covers the case UNIQUE cannot -- an `at`
    stamped from now_iso() differs by seconds between two attempts, so UNIQUE
    would happily store both. The UNIQUE(id,status,at) constraint is cheap
    insurance for a caller passing a fixed `at` twice (the dashboard fold does
    exactly that, replaying the browser's own timestamp).

    Note what does NOT need guarding: sync.push_with_retry recovers a lost push
    with `git reset --hard origin/main` BEFORE refold(), which restores
    jobs.sqlite -- the status change and its history row are restored together,
    so a replayed fold writes one row, not two.
    """
    if current == status:
        return
    if status not in TRACKED_STATUSES and current not in TRACKED_STATUSES:
        return
    conn.execute("INSERT OR IGNORE INTO status_history (id, status, at, via)"
                 " VALUES (?, ?, ?, ?)", (jid, status, at or now_iso(), via))


def set_status(conn, jid: str, status: str, at: str | None = None,
               via: str = "") -> None:
    """Move a job's status, recording the transition when it is a tracked one.

    `at` is when the DECISION was made, not when the DB caught up. A dashboard
    click travels through git and lands at the next sync -- up to hours later --
    so decisions.py passes the browser's own timestamp and everything else
    defaults to now. Getting this wrong would make every dashboard-recorded
    outcome look like it happened at sync time, which is the one thing a funnel
    measuring elapsed time cannot tolerate.

    `via` names the writer: dashboard / mark / sync / freshness / delisted /
    baseline / backfill. It is what lets a reader tell a recorded outcome from
    an inferred one.
    """
    if status not in STATUSES:
        raise ValueError(f"invalid status: {status}")
    row = conn.execute("SELECT status FROM jobs WHERE id = ?", (jid,)).fetchone()
    if row is None:
        # Was a silent no-op until 2026-09-01. Every caller reads its ids out of
        # the DB first, so nothing depended on that -- and a status write that
        # quietly lands nowhere is this repo's most-repeated failure shape.
        # set_lane has raised here since it was written; this now matches it.
        raise ValueError(f"no such job: {jid}")
    conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, jid))
    _record_transition(conn, jid, row[0], status, at, via)
    conn.commit()


def set_lane(conn, jid: str, lane: str, marker: str | None = None) -> None:
    """Rewrite the lane clause of `notes`, keeping the scorer's rationale.

    `notes` is a `;`-separated line whose FIRST clause is the lane the job-scorer
    assigned ("auto-tailor; strong platform fit, 9 total"). Everything downstream
    reads it that way: sync.lane_of, reconcile_sidecars, the dashboard, the
    digest. Re-laning a job by hand therefore means rewriting clause one and
    nothing else -- set_score is unsuitable because it also force-sets the status
    to 'scored', and a promotion must not resurrect a row that has moved on.

    `marker` records WHO re-laned it and when, as a second clause. It is skipped
    when an identical clause is already there, so replaying the same promotion
    (which sync.py's push retry does verbatim) reproduces the string byte for
    byte instead of stacking markers.
    """
    if lane not in LANES:
        raise ValueError(f"invalid lane: {lane}")
    if marker is not None:
        if not marker.isascii():
            raise ValueError(f"non-ASCII marker: {marker!r}")
        if ";" in marker:
            # A marker holding the clause separator would split into two clauses
            # and never match itself again -- every replay would insert it anew.
            raise ValueError(f"marker may not contain ';': {marker!r}")
    row = conn.execute("SELECT notes FROM jobs WHERE id = ?", (jid,)).fetchone()
    if row is None:
        raise ValueError(f"no such job: {jid}")
    rest = [c.strip() for c in (row[0] or "").split(";")[1:] if c.strip()]
    if marker and marker not in rest:
        rest.insert(0, marker)
    conn.execute("UPDATE jobs SET notes = ? WHERE id = ?",
                 ("; ".join([lane, *rest]), jid))
    conn.commit()


def set_score(conn, jid: str, score: int, note: str = "", at: str | None = None,
              via: str = "score") -> None:
    """Record a job-scorer result and move the row to status 'scored'.

    The status write goes through _record_transition for the same reason
    set_status does. It records nothing today -- `new -> scored` has no tracked
    side -- but a tracked row that is ever re-scored records correctly, and that
    retires an "X only ever does Y" invariant this repo has been bitten by twice
    (the applications/ checkout, the in-flight folder fold).
    """
    if not 0 <= int(score) <= 10:
        raise ValueError(f"score out of range: {score}")
    row = conn.execute("SELECT status FROM jobs WHERE id = ?", (jid,)).fetchone()
    conn.execute("UPDATE jobs SET score = ?, status = 'scored', notes = ? WHERE id = ?",
                 (int(score), note, jid))
    if row is not None:
        _record_transition(conn, jid, row[0], "scored", at, via)
    conn.commit()


def import_seed(conn, csv_path: Path = SEED_CSV) -> int:
    """One-time (idempotent) import of pre-pipeline applications."""
    imported = 0
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(line for line in f if not line.lstrip().startswith("#")):
            company = (row.get("company") or "").strip()
            title = (row.get("title") or "").strip()
            if not company or not title:
                continue
            status = (row.get("status") or "applied").strip() or "applied"
            before = conn.total_changes
            insert_job(conn, company, title, (row.get("location") or "").strip(),
                       url="", source="seed", posted_at=(row.get("date_applied") or "").strip(),
                       status=status, notes=(row.get("notes") or "").strip())
            if conn.total_changes > before:
                imported += 1
    return imported


def counts_by_status(conn) -> dict:
    return {r["status"]: r["n"] for r in
            conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")}


# --- outcome capture (Phase 6) ---------------------------------------------

# The employer-board fetchers: the four ATS platforms, plus the six big-tech
# career sites W3c added (2026-10-10, pipeline/bigtech.py), which are the
# employer's own board too. Held as a literal because db.py cannot import
# scrape.py or bigtech.py (both import db), so the coupling is asserted in
# test_outcomes instead: this tuple must equal set(scrape.FETCHERS). A literal
# nobody checks is how a new board silently becomes channel "other" -- which is
# what happened to these six until 10-11.
ATS_SOURCES = ("greenhouse", "lever", "ashby", "workday",
               "amazon", "microsoft", "google", "apple", "netflix", "intuit")

CHANNEL_ORDER = ("ats", "aggregator", "curated", "startup", "seed", "other")


def channel_of(source: str) -> str:
    """Detection family a row came from. Computed at read time, never a column.

    Prefix-matched on purpose for the two families that grow: a new JOBRIGHT_FEEDS
    entry or a fourth JobSpy site maps correctly without anyone remembering to
    edit this. Note the literals carry their prefixes -- the sources are
    `jobspy:indeed` and `jobright:pm-newgrad`, not `indeed` and `jobright`.
    """
    s = (source or "").strip().lower()
    if s in ATS_SOURCES:
        return "ats"
    if s.startswith("jobspy:"):
        return "aggregator"
    if s.startswith("jobright:"):
        return "curated"
    if s.startswith(("getro:", "consider:")):
        return "startup"   # VC portfolio boards (vc_boards.py, 2026-10-08)
    if s == "seed":
        return "seed"
    return "other"


def outcome_funnel(conn) -> dict:
    """Per-channel applied -> interview -> offer, plus post-application rejections.

    Membership is read off status_history, never off the row's current status: a
    job that reached `interview` and then `rejected` still belongs in the applied
    and interview counts, which a status column cannot express.

    `recorded` is the subset whose `applied` transition is not a backfill. It is
    the only honest denominator for a rate -- an application the pipeline never
    watched cannot evidence the absence of an interview.

    `rejected` requires an `applied` transition too. Without that guard it would
    count Vaish rejecting a POSTING as an employer rejecting him; both live
    `rejected` rows today are the former.

    source='seed' is excluded: those are pre-pipeline applications with no
    detection channel to attribute an outcome to.
    """
    out = {}
    rows = conn.execute("""
        SELECT j.source AS source, h.status AS status, h.via AS via, j.id AS id
          FROM jobs j JOIN status_history h ON h.id = j.id
         WHERE j.source <> 'seed'
    """).fetchall()
    by_job = {}
    for r in rows:
        by_job.setdefault(r["id"], {"source": r["source"], "seen": {}})
        by_job[r["id"]]["seen"].setdefault(r["status"], r["via"])
    for rec in by_job.values():
        seen = rec["seen"]
        if "applied" not in seen:
            continue
        ch = out.setdefault(channel_of(rec["source"]),
                            {"applied": 0, "recorded": 0, "interview": 0,
                             "offer": 0, "rejected": 0})
        ch["applied"] += 1
        if seen["applied"] != "backfill":
            ch["recorded"] += 1
        for st in ("interview", "offer", "rejected"):
            if st in seen:
                ch[st] += 1
    return out


def no_outcome_yet(conn, days: int = STALE_APPLIED_DAYS, now=None) -> list:
    """Applied rows with no recorded movement for `days` -- the nudge list.

    Backfilled rows are excluded, and that is why this list is EMPTY the day it
    ships. A backfill stamps `first_seen`, which is when the job was detected,
    not when it was applied to -- a lower bound. Nudging off a lower bound would
    announce that a 3-day-old application has been silent for three weeks. The
    honest version stays quiet until an application is recorded through the
    pipeline, which is exactly what the dashboard buttons now do.

    `now` is injectable: a test about relative time must pin the clock, or it
    becomes a scheduled false alarm (LEARNINGS 2026-08-17).
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    out = []
    for r in conn.execute("""
        SELECT j.id AS id, j.company AS company, j.title AS title, j.url AS url,
               j.source AS source, MAX(h.at) AS last_at
          FROM jobs j JOIN status_history h ON h.id = j.id
         WHERE j.status = 'applied' AND h.via <> 'backfill'
         GROUP BY j.id
    """):
        stamp = parse_posted_at(r["last_at"])
        if stamp is None or stamp > cutoff:
            continue
        out.append({"id": r["id"], "company": r["company"], "title": r["title"],
                    "url": r["url"], "source": r["source"],
                    "last_at": r["last_at"],
                    "days": int((now - stamp).total_seconds() // 86400)})
    return sorted(out, key=lambda d: -d["days"])


def outcomes_report(conn, now=None) -> str:
    """ASCII text report of both readouts. One implementation, two surfaces.

    ASCII because Task Scheduler pipes a run's only record through cmd.exe,
    whose console codepage turns anything else into replacement characters
    (LEARNINGS 2026-08-17).
    """
    lines = []
    funnel = outcome_funnel(conn)
    applied_rows = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE status = 'applied'").fetchone()[0]
    counted = sum(c["applied"] for c in funnel.values())
    recorded = sum(c["recorded"] for c in funnel.values())
    lines.append("OUTCOME FUNNEL (by detection channel)")
    lines.append(f"  {counted} application(s) tracked; the DB holds {applied_rows}"
                 " row(s) at 'applied'. The difference is seed rows (pre-pipeline"
                 " applications with no detection channel) and any row whose"
                 " application predates this table AND is not still at 'applied'.")
    if not funnel:
        lines.append("  nothing to show yet: no job carries an 'applied' transition.")
    for ch in CHANNEL_ORDER:
        c = funnel.get(ch)
        if not c:
            continue
        lines.append(f"  {ch:<11} applied {c['applied']:<4} interview"
                     f" {c['interview']:<4} offer {c['offer']:<4}"
                     f" rejected-after-applying {c['rejected']}")
        if c["recorded"] >= FUNNEL_RATE_MIN_N:
            pct = 100.0 * c["interview"] / c["recorded"]
            lines.append(f"  {'':<11} interview rate {pct:.0f}%"
                         f" of {c['recorded']} recorded application(s)")
        else:
            lines.append(f"  {'':<11} no rate yet: {c['applied'] - c['recorded']}"
                         f" of {c['applied']} predate outcome tracking, so only"
                         f" {c['recorded']} carry a recorded application date"
                         f" (need {FUNNEL_RATE_MIN_N}).")
    lines.append("")
    nudge = no_outcome_yet(conn, now=now)
    lines.append(f"NO OUTCOME YET (applied, silent {STALE_APPLIED_DAYS}+ days)")
    if not nudge:
        lines.append("  empty. Backfilled applications are excluded on purpose:"
                     " their timestamp is first_seen, a lower bound, so nudging"
                     " off it would overstate every wait. This list fills as"
                     " applications are recorded through the dashboard or /mark.")
    for d in nudge:
        lines.append(f"  {d['days']:>4}d  {d['company']} - {d['title']}"
                     f"  ({d['id'][:8]})")
    return "\n".join(lines)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Layer 1 job store")
    ap.add_argument("--seed", action="store_true", help="import profile/seed_applied.csv")
    ap.add_argument("--counts", action="store_true", help="print status counts")
    ap.add_argument("--outcomes", action="store_true",
                    help="print the outcome funnel + the no-outcome-yet nudge")
    args = ap.parse_args()
    conn = connect()
    if args.outcomes:
        print(outcomes_report(conn))
    if args.seed:
        n = import_seed(conn)
        print(f"seed import: {n} new rows (idempotent; re-runs add 0)")
    if args.counts or args.seed:
        print("status counts:", counts_by_status(conn))
