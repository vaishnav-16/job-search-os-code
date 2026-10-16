"""Unit tests for the Layer 1 source fetchers. Run: python pipeline/test_scrape_sources.py

All pure / no network. The three things worth pinning here are the ones where a
silent wrong answer is expensive and invisible:

  * the Workday `postedOn` mapper, because collapsing a date-only value into a
    fabricated timestamp would hand db.job_age() a precision it does not have;
  * the Jobright markdown-row parser, because an upstream column reshuffle
    yields plausible-looking garbage rather than an exception;
  * the delisted-sweep decision, because its failure mode is deleting live
    queue entries — the one bug in Layer 1 that destroys work instead of
    merely missing it.
"""

import json
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db
import ingest_scores
import scrape

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}  (got {got!r}, want {want!r})")
        FAILS.append(label)


def test_titles_and_linkedin_depth():
    """2026-10-08: the adjacent families reach the queue, and LinkedIn is read
    deep with a JD fetched only where it can change the outcome."""
    import re
    import pandas as pd
    import jobspy
    from datetime import timedelta

    print("title gates: the shipped targets.yaml filter and JOBSPY_RELEVANT")
    defaults, _ = scrape.load_targets()
    ats = re.compile(defaults["title_filter"])
    table = {
        "Associate Product Manager": True, "Technical Product Owner": True,
        "Product Analyst II": True, "Product Operations Manager": True,
        "Forward Deployed Engineer": True, "Forward-Deployed Engineer, AI": True,
        "Developer Advocate": True, "Developer Relations Engineer": True,
        "TPM II, Infrastructure": True, "Program Manager, AI Platform": True,
        "Software Engineer": False, "Sales Engineer": False, "Data Engineer": False,
        "Laptop Repair Technician": False,
    }
    for title, want in table.items():
        check(f"ATS title_filter: {title}", bool(ats.search(title)), want)
        check(f"JOBSPY_RELEVANT: {title}", bool(scrape.JOBSPY_RELEVANT.search(title)), want)

    tmp = Path(tempfile.mkdtemp(prefix="jobspy-test-"))
    conn = db.connect(tmp / "t.sqlite")
    saved = (scrape.QUEUE_DIR, ingest_scores.SCORES_DIR, scrape.fetch_linkedin_jd,
             jobspy.scrape_jobs)
    scrape.QUEUE_DIR = tmp / "queue"
    scrape.QUEUE_DIR.mkdir()
    ingest_scores.SCORES_DIR = tmp / "scores"
    try:
        print("windows: since the last run plus 2h; LinkedIn at least 8h")
        now = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
        check("no runs row reads 24", scrape.jobspy_window(conn, now), 24)
        for gap_h, li in ((0.5, 8), (3.2, 8), (7.5, 10), (30, 24)):
            conn.execute("DELETE FROM runs")
            conn.execute("INSERT INTO runs (ts, kind) VALUES (?, 'layer1')",
                         ((now - timedelta(hours=gap_h)).strftime("%Y-%m-%dT%H:%M:%SZ"),))
            check(f"last run {gap_h}h ago -> LinkedIn {li}h", scrape.jobspy_window(conn, now), li)

        print("LinkedIn: JD pages fetched only for new, on-target, title-clean rows")
        # The freshness sweep reads the wall clock, not `now`: a fixed date here
        # made every row stale 72h after the test was written (10-08 -> 10-11).
        posted = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        db.insert_job(conn, "Seen Co", "Product Manager", "Boston, MA",
                      "https://www.linkedin.com/jobs/view/1", "jobspy:linkedin")

        def row(i, company, title, site="linkedin", location="Austin, TX"):
            return {"site": site, "company": company, "title": title,
                    "location": location, "job_url": f"https://www.linkedin.com/jobs/view/{i}",
                    "date_posted": posted, "description": None, "is_remote": False}

        frames = {
            "product manager": [row(1, "Seen Co", "Product Manager", location="Boston, MA"),
                                row(2, "Acme", "Product Manager"),
                                row(3, "Acme", "Director of Product"),
                                row(4, "Acme", "Software Engineer"),
                                row(5, "Beta", "Product Owner")],
            "product owner": [row(5, "Beta", "Product Owner")],     # same job, 2nd term
        }

        def fake_scrape_jobs(site_name, search_term, **kw):
            if site_name != ["linkedin"]:
                return pd.DataFrame([])
            check(f"LinkedIn list has no description fetch ({search_term})",
                  kw.get("linkedin_fetch_description"), False)
            return pd.DataFrame(frames.get(search_term, []))

        fetched = []

        def fake_fetch(url, session=None):
            fetched.append(url)
            return None if url.endswith("/5") else "Own the roadmap. 2+ years."

        jobspy.scrape_jobs = fake_scrape_jobs
        scrape.fetch_linkedin_jd = fake_fetch
        stats = scrape.poll_jobspy(conn, 24, True, linkedin_hours=4)
        check("only the two new on-target title-clean rows were fetched",
              sorted(u.rsplit("/", 1)[1] for u in fetched), ["2", "5"])
        status = dict(conn.execute("SELECT title, status FROM jobs WHERE company != 'Seen Co'"))
        check("a fetched row is queued", status.get("Product Manager"), "new")
        check("a title-rejected row is stored without a fetch",
              status.get("Director of Product"), "skipped")
        check("an off-target row is stored without a fetch", status.get("Software Engineer"),
              "skipped")
        check("a refused page still queues the row, with an empty JD",
              [tuple(r) for r in conn.execute(
                  "SELECT status, jd_text FROM jobs WHERE title='Product Owner'")],
              [("new", "")])
        check("the refusal is counted", stats["jobspy:product manager"].get("jd_missing"), 1)
        check("a job under two terms is inserted once",
              conn.execute("SELECT COUNT(*) FROM jobs WHERE title='Product Owner'")
              .fetchone()[0], 1)

        print("a repost of a role that aged out unworked goes back to the queue")
        aged = db.insert_job(conn, "Gamma", "Product Manager", "Denver, CO",
                             "https://old", "jobspy:linkedin")
        db.set_status(conn, aged, "stale_skip", via="freshness")
        gone = db.insert_job(conn, "Delta", "Product Manager", "Denver, CO",
                             "https://old2", "jobspy:linkedin")
        db.set_status(conn, gone, "stale_skip", via="delisted")
        conn.execute("UPDATE jobs SET notes='delisted: no longer on board' WHERE id=?", (gone,))
        worked = db.insert_job(conn, "Epsilon", "Product Manager", "Denver, CO",
                               "https://old3", "jobspy:linkedin")
        db.set_status(conn, worked, "tailored", via="sync")
        conn.commit()
        frames.clear()
        frames["product manager"] = [
            row(20, "Gamma", "Product Manager", location="Denver, CO"),
            row(21, "Delta", "Product Manager", location="Denver, CO"),
            row(22, "Epsilon", "Product Manager", location="Denver, CO")]
        fetched.clear()
        stats = scrape.poll_jobspy(conn, 24, True, linkedin_hours=8)
        st = dict(conn.execute("SELECT company, status FROM jobs WHERE company IN "
                               "('Gamma','Delta','Epsilon')"))
        check("the aged-out role is new again", st.get("Gamma"), "new")
        check("with a fresh first_seen and its new link",
              conn.execute("SELECT url, first_seen >= ? FROM jobs WHERE id=?",
                           (now.strftime("%Y-%m-%d"), aged)).fetchone()[0],
              "https://www.linkedin.com/jobs/view/20")
        check("and a queue file", (scrape.QUEUE_DIR / f"{aged}.json").exists(), True)
        check("a delisted role stays gone", st.get("Delta"), "stale_skip")
        check("a tailored role is never touched", st.get("Epsilon"), "tailored")
        check("only the revivable one cost a JD fetch",
              sorted(u.rsplit("/", 1)[1] for u in fetched), ["20"])
        check("the revive is counted", stats["jobspy:product manager"].get("reposted"), 1)
    finally:
        (scrape.QUEUE_DIR, ingest_scores.SCORES_DIR, scrape.fetch_linkedin_jd,
         jobspy.scrape_jobs) = saved


def test_locations():
    print("locations (W3a, 2026-10-09): US by state name or code, placeless kept")
    import re
    import yaml
    cfg = yaml.safe_load((Path(__file__).resolve().parent.parent / "profile" /
                          "targets.yaml").read_text(encoding="utf-8"))
    loc_re = re.compile((cfg.get("defaults") or {}).get("location_filter", ""))
    keep = ["Mountain View, California", "Philadelphia, Pennsylvania", "Washington, D.C.",
            "Honolulu, HI", "North America", "Hybrid", "Distributed", "In-Office",
            "Multiple Locations", "New Mexico", "US-Chicago; CA-Toronto", "Remote",
            "Remote, US", "San Juan, Puerto Rico", "Austin, TX",
            "Toronto, ON; Bentonville, AR", "London; New York, NY"]
    drop = ["Remote - Canada", "London or Remote", "Paris, France or Remote",
            "Hybrid - Lyon", "Bengaluru, Karnataka", "Lisbon, Portugal", "Toronto, ON",
            "Remote - EMEA", "in office in Berlin", "Sydney, Australia", "Seoul",
            "IN - Bengaluru, India", "CA - Toronto, Canada", "IL - Tel Aviv, Israel"]
    for x in keep:
        check(f"keeps {x!r}", scrape.location_ok(x, loc_re), True)
    for x in drop:
        check(f"drops {x!r}", scrape.location_ok(x, loc_re), False)
    # "or" and "in" are words, not Oregon and Indiana: codes match upper case only
    check("'or' is not Oregon", bool(scrape.US_SIGNAL.search("London or Remote")), False)
    check("'OR' is Oregon", bool(scrape.US_SIGNAL.search("Portland, OR")), True)
    # a country prefix beside a foreign place is not a state (Visa Workday, 10-10)
    for x in ("IN - Bengaluru, India", "CA - Toronto, Canada"):
        check(f"hard filter drops {x!r}", scrape.hardfilter.hard_filter("Product Manager", "", x),
              "non_us")


def test_jobright_jd():
    print("jobright JD (W3b, 2026-10-09): feed text, real page, employer posting; never the challenge")
    challenge = "<html><title>Security check</title>Verify you are human</html>"
    real = '<script type="application/ld+json">{"@type":"JobPosting"}</script> security check'
    check("a challenge page is recognised", scrape.jobright_challenge(challenge), True)
    check("a real page that mentions security is not", scrape.jobright_challenge(real), False)
    jr = {"jobId": "x", "jobTitle": "PM", "jobSummary": "Own the roadmap for payments. " * 3,
          "coreResponsibilities": ["Ship the API", "Run discovery"], "jobLocation": "NY"}
    jd = scrape.jobright_item_jd(jr)
    check("summary text is used", "Own the roadmap" in jd, True)
    check("responsibility lists become bullets", "- Ship the API" in jd, True)
    check("location is not description", jd.endswith("NY"), False)
    check("the employer link is found",
          scrape.jobright_apply_url({"applyLink": "https://boards.greenhouse.io/x/jobs/1"}),
          "https://boards.greenhouse.io/x/jobs/1")
    check("a jobright link is not the employer's",
          scrape.jobright_apply_url({"url": "https://jobright.ai/jobs/info/1"}), "")


def test_relisted():
    print("big-tech relisting (W3c, 2026-10-10): requisition long before the post")
    check("a 60-day gap is a relisting",
          scrape.relisted({"posted_at": "2026-10-09T16:05:15Z", "_created_at": "2026-08-04T10:00:00Z"}), 66)
    check("a week's approval wait is not",
          scrape.relisted({"posted_at": "2026-10-09T23:33:58Z", "_created_at": "2026-10-07T09:00:00Z"}), None)
    check("no creation date, no judgement", scrape.relisted({"posted_at": "2026-10-09"}), None)
    check("the big-tech boards are wired", all(b in scrape.FETCHERS for b in
          ("amazon", "microsoft", "google", "apple", "netflix", "intuit")), True)
    check("none of them is a whole-board fetcher",
          not (set(scrape.FETCHERS) - {"greenhouse", "lever", "ashby", "workday"})
          & scrape.FULL_BOARD_FETCHERS, True)


def test_reposts():
    print("reposts: LinkedIn by job-ID gap, Jobright by its own flag")
    base = 4477872002
    fresh = [f"https://www.linkedin.com/jobs/view/{base - i * 1000}" for i in range(25)]
    old = "https://www.linkedin.com/jobs/view/4449477023"        # Atlassian Rovo, ~7 weeks
    slug = "https://www.linkedin.com/jobs/view/product-manager-at-acme-4449477023?trk=x"
    edge = f"https://www.linkedin.com/jobs/view/{base - scrape.LINKEDIN_REPOST_ID_GAP + 1}"
    got = scrape.linkedin_reposts(fresh + [old, slug, edge])
    check("a 7-week-old ID is a repost", old in got, True)
    check("the slugged URL form parses too", slug in got, True)
    check("just inside the gap is kept", edge in got, False)
    check("fresh IDs are kept", any(u in got for u in fresh), False)
    check("too few IDs to anchor: nothing flagged",
          scrape.linkedin_reposts(fresh[:5] + [old]), set())
    check("jobright item flag", scrape.jobright_is_repost({"repost": True}), True)
    check("jobright item without the flag", scrape.jobright_is_repost({"jobId": "x"}), False)
    check("jobright page flag", scrape.jobright_is_repost('"jobtargetJobId":"","repost":true,'), True)
    check("jobright page, not a repost", scrape.jobright_is_repost('"repost":false,"hiddenJob"'), False)

    print("reposts: an aggregator row the company's own full board dates old (10-15)")
    scrape._BOARD_NEWEST.clear()
    fresh_iso = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    scrape.note_board_posting("greenhouse", "Anthropic", "Technical Program Manager, Hardware Systems",
                              "2026-08-05T17:10:41-04:00")
    scrape.note_board_posting("ashby", "Perplexity", "Product Manager (Builder)",
                              "2026-04-21T17:42:48.971+00:00")
    scrape.note_board_posting("greenhouse", "Anthropic", "Product Manager, Claude", "2026-08-01T10:00:00Z")
    scrape.note_board_posting("greenhouse", "Anthropic", "Product Manager, Claude", fresh_iso)
    scrape.note_board_posting("workday", "Capital One", "Senior Associate, Product Manager",
                              "2026-08-01")
    scrape.note_board_posting("greenhouse", "Vercel", "Product Manager, Compute", "")
    check("an Anthropic role first published 08-05 is a repost",
          (scrape.board_repost("Anthropic", "Technical Program Manager, Hardware Systems") or "")
          .startswith("on the company's own board since 2026-08-05"), True)
    check("the company string's corporate form does not hide it",
          scrape.board_repost("Perplexity, Inc.", "Product Manager (Builder)") is not None, True)
    check("a role with one fresh copy on the board is not a repost",
          scrape.board_repost("Anthropic", "Product Manager, Claude"), None)
    check("a search-walk board (Workday) never vouches for age",
          scrape.board_repost("Capital One", "Senior Associate, Product Manager"), None)
    check("an undated board row says nothing", scrape.board_repost("Vercel", "Product Manager, Compute"), None)
    check("a role absent from every board says nothing", scrape.board_repost("Acme", "Product Manager"), None)
    scrape._BOARD_NEWEST.clear()


def main():
    TODAY = date(2026, 8, 23)

    print("workday postedOn -> date-only mapper")
    check("today", scrape.workday_posted_date("Posted Today", TODAY), "2026-08-23")
    check("yesterday", scrape.workday_posted_date("Posted Yesterday", TODAY), "2026-08-22")
    check("2 days ago", scrape.workday_posted_date("Posted 2 Days Ago", TODAY), "2026-08-21")
    check("singular day", scrape.workday_posted_date("Posted 1 Day Ago", TODAY), "2026-08-22")
    check("30+ clamps to 30", scrape.workday_posted_date("Posted 30+ Days Ago", TODAY),
          "2026-07-24")
    check("month boundary", scrape.workday_posted_date("Posted 5 Days Ago", date(2026, 9, 2)),
          "2026-08-28")
    # Unrecognised must be "" (unknown), never a guess: db.is_provably_stale
    # treats unknown as NOT stale, so an unparseable string keeps the job.
    check("unrecognised is unknown", scrape.workday_posted_date("Posted a while back", TODAY), "")
    check("empty is unknown", scrape.workday_posted_date("", TODAY), "")

    print("workday dates keep DAY precision (never a fabricated time of day)")
    # The whole point of the mapper: Workday says "2 Days Ago", which is a
    # 24h-wide interval. Emitting YYYY-MM-DD is what makes db.job_age() report
    # precision "day" and reason over (best, worst) instead of one number.
    for s in ("Posted Today", "Posted 2 Days Ago", "Posted 30+ Days Ago"):
        check(f"{s!r} -> day precision",
              db.job_age(scrape.workday_posted_date(s, TODAY))[2], "day")
    now = datetime(2026, 8, 23, 19, 0, tzinfo=timezone.utc)
    check("'Posted Today' is not stale",
          db.is_provably_stale(scrape.workday_posted_date("Posted Today", TODAY),
                               db.FETCH_WINDOW_HOURS, now=now), False)
    check("'Posted 2 Days Ago' survives the 72h fetch window",
          db.is_provably_stale(scrape.workday_posted_date("Posted 2 Days Ago", TODAY),
                               db.FETCH_WINDOW_HOURS, now=now), False)
    check("'Posted 30+ Days Ago' is provably stale",
          db.is_provably_stale(scrape.workday_posted_date("Posted 30+ Days Ago", TODAY),
                               db.FETCH_WINDOW_HOURS, now=now), True)

    print("workday multi-location cell")
    # "3 Locations" is the board declining to name a place. location_ok() would
    # read that as a non-US location and drop the row, so it maps to unknown --
    # the documented doctrine is "keep US and unknown, let the scorer judge".
    check("'3 Locations' is unknown", scrape.workday_location("3 Locations"), "")
    check("'2 Locations' is unknown", scrape.workday_location("2 Locations"), "")
    check("a real place passes through",
          scrape.workday_location("Purchase, New York"), "Purchase, New York")
    check("unknown location passes location_ok",
          scrape.location_ok("", scrape.US_SIGNAL), True)

    print("jobright markdown row parser")
    row = ("| **[Prosper Marketplace](http://www.prosper.com)** | "
           "Product Manager (Agent Platform) |  Mid-Level  | San Francisco, CA | "
           "\U0001F3C5 | [apply](https://jobright.ai/jobs/info/abc123?utm_source=1008) "
           "| 2026-05-06 |")
    r = scrape.parse_jobright_row(row)
    check("company un-linked", r and r["company"], "Prosper Marketplace")
    check("title", r and r["title"], "Product Manager (Agent Platform)")
    check("location", r and r["location"], "San Francisco, CA")
    check("apply url", r and r["url"], "https://jobright.ai/jobs/info/abc123?utm_source=1008")
    check("posted_at is YYYY-MM-DD", r and r["posted_at"], "2026-05-06")
    check("posted_at keeps day precision", db.job_age(r["posted_at"])[2], "day")
    check("h1b flag explicit", r and r["h1b"], "explicit")

    silver = row.replace("\U0001F3C5", "\U0001F948")
    check("h1b flag from sponsorship history",
          scrape.parse_jobright_row(silver)["h1b"], "history")

    print("jobright parser rejects everything that is not a job row")
    check("header row", scrape.parse_jobright_row(
        "| Company | Job Title |  Level  | Location | H1B status | Link | Date Posted |"), None)
    check("separator row", scrape.parse_jobright_row(
        "| ------- | --------- |  -----  | -------- | ---------- | ---- | ----------- |"), None)
    check("prose line", scrape.parse_jobright_row("This job repository provides daily"), None)
    check("html line", scrape.parse_jobright_row("<summary> Product Manager</summary>"), None)
    check("too few columns", scrape.parse_jobright_row("| a | b | c |"), None)
    check("empty company", scrape.parse_jobright_row(
        "|  | Product Manager |  Mid  | Boston, MA | \U0001F3C5 | [apply](https://x/y) "
        "| 2026-05-06 |"), None)
    check("missing date", scrape.parse_jobright_row(
        "| **[Acme](http://a.com)** | Product Manager |  Mid  | Boston, MA | \U0001F3C5 "
        "| [apply](https://x/y) |  |"), None)

    print("jobright table extraction from a full document")
    doc = "\n".join([
        "# Daily H1B Jobs in Tech",
        "Some prose about \U0001F3C5 badges.",
        "<details>", "<summary>Product Manager</summary>", "",
        "| Company | Job Title |  Level  | Location | H1B status | Link | Date Posted |",
        "| ------- | --------- |  -----  | -------- | ---------- | ---- | ----------- |",
        row,
        silver,
        "</details>",
    ])
    got = scrape.parse_jobright_table(doc)
    check("parses both rows, skips prose/header/separator", len(got), 2)
    check("row order preserved", [g["company"] for g in got],
          ["Prosper Marketplace", "Prosper Marketplace"])

    print("jobright feeds are declared, not hard-coded")
    # Two feeds with genuinely different tables. The shape difference has to
    # live in data plus a small per-shape parser; a second copy of the poller
    # would drift the moment one upstream renamed a column.
    check("every feed names a shape the parser registry knows",
          all(f["shape"] in scrape.JOBRIGHT_SHAPES for f in scrape.JOBRIGHT_FEEDS), True)
    check("every feed has a distinct source string",
          len({f["source"] for f in scrape.JOBRIGHT_FEEDS}), len(scrape.JOBRIGHT_FEEDS))
    check("the new-grad PM feed is wired",
          any(f["source"] == "jobright:pm-newgrad" for f in scrape.JOBRIGHT_FEEDS), True)
    # Vaish graduates Nov 2026 and wants FULL-TIME work. The internship siblings
    # of these repos are live and tempting and deliberately excluded.
    check("no internship feed", any("nternship" in f["repo"] for f in scrape.JOBRIGHT_FEEDS), False)
    check("the dead H1B feed is not polled",
          any("H1B" in f["repo"] for f in scrape.JOBRIGHT_FEEDS), False)
    check("a bracketed title keeps its link",
          scrape._md_cell("[[2027] Associate PM, Early Career](https://jobright.ai/jobs/info/x1)"),
          ("[2027] Associate PM, Early Career", "https://jobright.ai/jobs/info/x1"))

    print("jobright year-less dates: the most recent occurrence, never the future")
    # "Aug 23" carries no year. Inferring the wrong one moves a row by 365 days,
    # which either hides a live posting forever or ingests a year-old one.
    check("this month resolves to this year",
          scrape.jobright_month_day("Aug 23", date(2026, 8, 23)), "2026-08-23")
    check("a few days back is still this year",
          scrape.jobright_month_day("Aug 16", date(2026, 8, 23)), "2026-08-16")
    # The Dec/Jan boundary is the whole reason this needs a function: on 3 Jan a
    # "Dec 30" row is four days old, not 361 days in the future.
    check("December read in January is LAST year",
          scrape.jobright_month_day("Dec 30", date(2027, 1, 3)), "2026-12-30")
    check("a later month read mid-year is last year's, never next year's",
          scrape.jobright_month_day("Dec 30", date(2026, 6, 1)), "2025-12-30")
    check("a leap day resolves to a year that has one",
          scrape.jobright_month_day("Feb 29", date(2027, 3, 5)), "2024-02-29")
    check("garbage yields unknown, never a guess",
          scrape.jobright_month_day("soon", date(2026, 8, 23)), "")
    check("empty yields unknown", scrape.jobright_month_day("", date(2026, 8, 23)), "")
    # Never more than a couple of days ahead of today: a feed running on a
    # timezone ahead of UTC is normal, a week-ahead date is a parse error.
    for probe in ("Aug 23", "Dec 30", "Jan 01", "Feb 29"):
        got_d = scrape.jobright_month_day(probe, date(2026, 8, 23))
        check(f"{probe!r} ({got_d or 'unknown'}) is never far-future",
              not got_d or got_d <= "2026-08-25", True)
    check("an inferred date keeps day precision",
          db.job_age(scrape.jobright_month_day("Aug 23", date(2026, 8, 23)))[2], "day")

    print("jobright new-grad shape: 5 columns, link in the TITLE cell")
    ng = ("| **[Veeva Systems](http://www.veeva.com)** | "
          "**[Associate Product Manager - Vault CRM](https://jobright.ai/jobs/info/abc)** "
          "| Massachusetts - Boston | Remote | Aug 23 |")
    r = scrape.parse_jobright_newgrad_row(ng, today=date(2026, 8, 23))
    check("company un-linked", r and r["company"], "Veeva Systems")
    check("title un-linked", r and r["title"], "Associate Product Manager - Vault CRM")
    check("url comes from the TITLE cell, not a Link column",
          r and r["url"], "https://jobright.ai/jobs/info/abc")
    check("location", r and r["location"], "Massachusetts - Boston")
    check("year-less date resolved", r and r["posted_at"], "2026-08-23")
    # No H1B column in this feed, so no sponsorship claim may come out of it.
    check("no sponsorship signal is invented", r and r["h1b"], "")

    print("the continuation row inherits the company above it")
    # 374 of 497 rows in the live file are continuations. Rejected as
    # empty-company, three quarters of the feed vanishes silently.
    cont = ("|" + "↳" + "| **[Associate Product Manager - Vault CRM]"
            "(https://jobright.ai/jobs/info/def)** | California - Pleasanton "
            "| Remote | Aug 23 |")
    r2 = scrape.parse_jobright_newgrad_row(cont, last_company="Veeva Systems",
                                           today=date(2026, 8, 23))
    check("company carried forward", r2 and r2["company"], "Veeva Systems")
    check("its own location is kept", r2 and r2["location"], "California - Pleasanton")
    check("a continuation with nothing above it is rejected",
          scrape.parse_jobright_newgrad_row(cont, last_company="",
                                            today=date(2026, 8, 23)), None)
    check("new-grad parser rejects the header",
          scrape.parse_jobright_newgrad_row(
              "| Company | Job Title | Location | Work Model | Date Posted |"), None)
    check("new-grad parser rejects the separator",
          scrape.parse_jobright_newgrad_row(
              "| ----- | --------- |  --------- | ---- | ------- |"), None)
    check("new-grad parser rejects the 7-column H1B shape",
          scrape.parse_jobright_newgrad_row(row), None)

    print("new-grad table extraction carries state across the whole document")
    ng_doc = "\n".join([
        "## Daily Job List",
        "| Company | Job Title | Location | Work Model | Date Posted |",
        "| ----- | --------- |  --------- | ---- | ------- |",
        ng, cont, cont,
    ])
    got = scrape.parse_jobright_table(ng_doc, shape="newgrad", today=date(2026, 8, 23))
    check("all three rows parse", len(got), 3)
    check("every row names the same company",
          {g["company"] for g in got}, {"Veeva Systems"})
    check("each row keeps its own url",
          len({g["url"] for g in got}), 2)

    print("jobright detail page: the ld+json JobPosting")
    # The tag on the live page is EXACTLY this shape (captured 2026-08-23):
    #   <script id="job-posting" type="application/ld+json" data-next-head="">
    # A pattern anchored on `<script type="application/ld+json"` matches nothing
    # here and fails silently, which leaves every row JD-less exactly as before.
    # The decoy blocks are synthetic on purpose: the live page carries only one,
    # so nothing about it would catch a parser that just takes the first block.
    real_tag = ('<script id="job-posting" type="application/ld+json" '
                'data-next-head="">')
    posting = json.dumps({
        "@context": "https://schema.org", "@type": "JobPosting",
        "title": "Product Operations Associate, Tier 1",
        "datePosted": "2026-08-23 13:47:07",
        "validThrough": "2026-09-23T13:47:07",
        "employmentType": "FULL_TIME",
        "description": "<p>Anduril is seeking a Product Operations Associate.</p>"
                       "<br><p>Responsibilities</p><ul><li>Assist in the intake"
                       " of hardware</li></ul>",
        "hiringOrganization": {"@type": "Organization", "name": "Anduril Industries",
                               "sameAs": "https://www.anduril.com"},
    })
    decoy = ('<script type="application/ld+json">'
             + json.dumps({"@type": "BreadcrumbList", "itemListElement": []})
             + "</script>")
    malformed = '<script type="application/ld+json">{not json at all</script>'
    page = ("<html><head>" + decoy + malformed + real_tag + posting
            + "</script></head><body>ignored</body></html>")

    got = scrape.jobright_job_posting(page)
    check("finds the JobPosting past a decoy and a malformed block",
          got.get("title"), "Product Operations Associate, Tier 1")
    check("a block of another @type is never returned",
          got.get("@type"), "JobPosting")
    # Attribute order is not guaranteed by anything, so it must not be relied on.
    swapped = page.replace(real_tag,
                           '<script type="application/ld+json" id="job-posting">')
    check("attribute order does not matter",
          scrape.jobright_job_posting(swapped).get("title"),
          "Product Operations Associate, Tier 1")
    check("single-quoted attributes parse too",
          scrape.jobright_job_posting(
              page.replace(real_tag, "<script type='application/ld+json'>")
          ).get("title"), "Product Operations Associate, Tier 1")
    check("a page with no ld+json yields {}",
          scrape.jobright_job_posting("<html><body>nothing here</body></html>"), {})
    check("a page with only decoys yields {}",
          scrape.jobright_job_posting("<html>" + decoy + malformed + "</html>"), {})
    check("empty input is safe", scrape.jobright_job_posting(""), {})
    # Publishers legitimately wrap objects in a list or an @graph.
    check("a list payload is walked",
          scrape.jobright_job_posting(
              '<script type="application/ld+json">'
              + json.dumps([{"@type": "WebSite"}, json.loads(posting)])
              + "</script>").get("title"),
          "Product Operations Associate, Tier 1")
    check("an @graph payload is walked",
          scrape.jobright_job_posting(
              '<script type="application/ld+json">'
              + json.dumps({"@graph": [json.loads(posting)]})
              + "</script>").get("title"),
          "Product Operations Associate, Tier 1")
    check("a list-valued @type still matches",
          scrape.jobright_job_posting(
              '<script type="application/ld+json">'
              + json.dumps({"@type": ["JobPosting", "Thing"], "title": "T"})
              + "</script>").get("title"), "T")

    print("jobright detail: the timestamp is preferred, the table date is the floor")
    jd, posted = scrape.jobright_detail_fields(page, "2026-08-23")
    check("the JD is real text, HTML stripped",
          "Responsibilities" in jd and "<p>" not in jd, True)
    check("the timestamp replaces the table's year-less date",
          posted, "2026-08-23T13:47:07")
    # This is the point of the whole exercise: a timestamp is precision "exact",
    # which is the only kind of date the 24h apply rule can be enforced against.
    check("an exact timestamp yields precision 'exact'", db.job_age(posted)[2], "exact")
    check("the table's own date could only ever be day precision",
          db.job_age("2026-08-23")[2], "day")
    # NOTHING IS FABRICATED: every unusable value keeps the table's date.
    check("a missing datePosted keeps the table date",
          scrape.jobright_posted_at(None, "2026-08-19"), "2026-08-19")
    check("an empty datePosted keeps the table date",
          scrape.jobright_posted_at("", "2026-08-19"), "2026-08-19")
    check("an unparseable datePosted keeps the table date",
          scrape.jobright_posted_at("last Tuesday", "2026-08-19"), "2026-08-19")
    check("a DATE-only datePosted keeps the table date (same information)",
          scrape.jobright_posted_at("2026-08-20", "2026-08-19"), "2026-08-19")
    check("an already-ISO timestamp passes through",
          scrape.jobright_posted_at("2026-08-20T03:32:28", "2026-08-19"),
          "2026-08-20T03:32:28")
    # The table renders a US-local date and the timestamp is naive UTC, so the
    # two legitimately disagree by a timezone offset (measured 2026-08-23: a row
    # the table dated Aug 19 carries "2026-08-20 03:32:28"). The timestamp wins;
    # no "Z" is invented, since db.parse_posted_at reads a naive stamp as UTC.
    check("a timezone-offset disagreement resolves to the timestamp",
          scrape.jobright_posted_at("2026-08-20 03:32:28", "2026-08-19"),
          "2026-08-20T03:32:28")
    check("no timezone marker is invented",
          "Z" in scrape.jobright_posted_at("2026-08-20 03:32:28", "2026-08-19"), False)
    check("a page with no JobPosting leaves the row exactly as the table had it",
          scrape.jobright_detail_fields("<html></html>", "2026-08-19"),
          ("", "2026-08-19"))

    print("delisted sweep decision (pure)")
    rows = [("id-a", "new"), ("id-b", "scored"), ("id-c", "tailored"), ("id-d", "new")]
    check("absent pre-artifact rows are swept",
          scrape.delisted_ids(rows, {"id-a"}, fetch_ok=True, fetched_count=5),
          ["id-b", "id-d"])
    check("a row still on the board is never swept",
          scrape.delisted_ids(rows, {"id-a", "id-b", "id-d"}, fetch_ok=True, fetched_count=5),
          [])
    # The two guards that make this safe. Absence-due-to-failure must never read
    # as delisted -- same reasoning as poll_ats's "0 jobs = error" rule.
    check("an ERRORED fetch sweeps nothing",
          scrape.delisted_ids(rows, set(), fetch_ok=False, fetched_count=0), [])
    check("an EMPTY fetch sweeps nothing",
          scrape.delisted_ids(rows, set(), fetch_ok=True, fetched_count=0), [])
    check("an errored fetch that still returned rows sweeps nothing",
          scrape.delisted_ids(rows, {"id-a"}, fetch_ok=False, fetched_count=5), [])
    check("a tailored row is NEVER swept",
          "id-c" in scrape.delisted_ids(rows, set(), fetch_ok=True, fetched_count=5), False)
    check("every other status is left alone",
          scrape.delisted_ids([("x", s) for s in
                               ("tailored", "needs_review", "applied", "rejected",
                                "skipped", "interview", "offer", "baseline_skip",
                                "stale_skip")],
                              set(), fetch_ok=True, fetched_count=5), [])

    print("the sweep only runs for fetchers that return the WHOLE board")
    # Workday is paginated and deliberately stops at the freshness window, so
    # "absent from the fetch" there means "not fresh", not "delisted".
    check("greenhouse/lever/ashby are full-board",
          scrape.FULL_BOARD_FETCHERS, {"greenhouse", "lever", "ashby"})
    check("workday is NOT full-board", "workday" in scrape.FULL_BOARD_FETCHERS, False)
    check("every full-board name is a real fetcher",
          scrape.FULL_BOARD_FETCHERS <= set(scrape.FETCHERS), True)

    print("delisted sweep against a real (temp) DB")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        conn = db.connect(Path(tmp) / "t.sqlite")
        qdir = Path(tmp) / "queue"
        # sweep_delisted calls ingest_scores.drop_sidecar, so without this the
        # test reaches into the REAL repo scores/ directory. test_age.py's sweep
        # block already redirects it; these two must not disagree about whether
        # a unit test may touch live pipeline state.
        saved_sc = ingest_scores.SCORES_DIR
        ingest_scores.SCORES_DIR = Path(tmp) / "scores"
        live = {"title": "Product Manager", "location": "Boston, MA",
                "url": "https://x/live", "posted_at": "2026-08-23", "jd": ""}
        gone = {"title": "Platform Product Manager", "location": "Boston, MA",
                "url": "https://x/gone", "posted_at": "2026-08-23", "jd": ""}
        for j in (live, gone):
            jid = db.insert_job(conn, "Acme", j["title"], j["location"], j["url"],
                                "greenhouse", posted_at=j["posted_at"])
            scrape.write_queue_entry(jid, "Acme", j, "greenhouse", queue_dir=qdir)
        gone_id = db.job_id("Acme", gone["title"], gone["location"])
        live_id = db.job_id("Acme", live["title"], live["location"])
        # A row from ANOTHER source for the same company must be untouched: the
        # greenhouse board is no oracle for what Indeed listed.
        other = db.insert_job(conn, "Acme", "AI Product Manager", "Boston, MA",
                              "https://x/other", "jobspy:indeed", posted_at="2026-08-23")

        n = scrape.sweep_delisted(conn, "Acme", "greenhouse", [live], queue_dir=qdir)
        check("one row swept", n, 1)
        status = dict(conn.execute("SELECT id, status FROM jobs"))
        check("delisted row -> stale_skip", status[gone_id], "stale_skip")
        check("live row untouched", status[live_id], "new")
        check("other source untouched", status[other], "new")
        notes = conn.execute("SELECT notes FROM jobs WHERE id = ?", (gone_id,)).fetchone()[0]
        check("delisted row is annotated",
              notes, "delisted: no longer on greenhouse board")

        print("  the sweep APPENDS to notes; it never clobbers the lane")
        # notes is a `;`-separated line whose FIRST clause is the scorer's lane,
        # and a later clause can hold Vaish's own `promoted via dashboard`
        # marker. The dashboard's "Recently closed" list reads lane_of(notes),
        # so overwriting the line silently strips a swept row's lane badge and
        # erases the record that a human promoted it.
        laned = db.insert_job(conn, "Acme", "Senior Product Manager", "Boston, MA",
                              "https://x/laned", "greenhouse", posted_at="2026-08-23")
        db.set_score(conn, laned, 8, "auto-tailor; promoted via dashboard; strong fit")
        scrape.sweep_delisted(conn, "Acme", "greenhouse", [live], queue_dir=qdir)
        laned_notes = conn.execute("SELECT notes FROM jobs WHERE id = ?",
                                   (laned,)).fetchone()[0]
        check("the lane clause survives", laned_notes.split(";")[0].strip(),
              "auto-tailor")
        check("the promotion marker survives",
              "promoted via dashboard" in laned_notes, True)
        check("the delist reason is appended, not substituted",
              laned_notes.endswith("delisted: no longer on greenhouse board"), True)
        check("and the row is still swept",
              conn.execute("SELECT status FROM jobs WHERE id = ?",
                           (laned,)).fetchone()[0], "stale_skip")
        check("delisted queue file removed", (qdir / f"{gone_id}.json").exists(), False)
        check("live queue file kept", (qdir / f"{live_id}.json").exists(), True)

        print("  re-running the sweep is a no-op (already swept rows are not sweepable)")
        check("second sweep changes nothing",
              scrape.sweep_delisted(conn, "Acme", "greenhouse", [live], queue_dir=qdir), 0)

        print("a queue entry carries first_seen as well as detected_at")
        # Two different facts. detected_at is when the FILE was written (a
        # repair re-emit re-stamps it); first_seen is when the ROW was first
        # recorded and never moves. The cloud tailor's ordering reads only the
        # queue file, so without this it had no observation bound at all.
        fs_id = db.insert_job(conn, "Acme", "Group Product Manager", "Boston, MA",
                              "https://x/fs", "greenhouse", posted_at="")
        scrape.write_queue_entry(fs_id, "Acme",
                                 {"title": "Group Product Manager",
                                  "location": "Boston, MA", "url": "https://x/fs",
                                  "posted_at": "", "jd": ""},
                                 "greenhouse", queue_dir=qdir,
                                 first_seen=scrape.row_first_seen(conn, fs_id))
        entry = json.loads((qdir / f"{fs_id}.json").read_text(encoding="utf-8"))
        stored = conn.execute("SELECT first_seen FROM jobs WHERE id = ?",
                              (fs_id,)).fetchone()[0]
        check("first_seen is the row's own stamp", entry["first_seen"], stored)
        check("detected_at is still written too", bool(entry["detected_at"]), True)
        # The whole point: a no-date row is 'unknown' without the bound and
        # 'seen' with it, which is what makes it decidable at all.
        check("the entry alone now yields precision 'seen'",
              db.job_age(entry["posted_at"], entry["first_seen"])[2], "seen")
        check("without it the same entry is undecidable",
              db.job_age(entry["posted_at"], "")[2], "unknown")
        # sync.reconcile_queue re-emits from a SELECT that does not carry the
        # column, and every queue file written before 2026-08-23 lacks it. ""
        # is the honest input there: a missing lower bound, never a wrong one.
        scrape.write_queue_entry(fs_id, "Acme",
                                 {"title": "Group Product Manager",
                                  "location": "Boston, MA", "url": "https://x/fs",
                                  "posted_at": "", "jd": ""},
                                 "greenhouse", queue_dir=qdir)
        legacy = json.loads((qdir / f"{fs_id}.json").read_text(encoding="utf-8"))
        check("a caller that cannot supply first_seen writes '', never a guess",
              legacy["first_seen"], "")

        print("a failed detail-JD fetch skips the row instead of poisoning it")
        # Inserting JD-less would write a dedupe row, and a dedupe row satisfies
        # is_seen FOREVER -- so one transient 502 costs this posting its
        # description permanently, despite ~72 free retries inside the fetch
        # window. The row then buys a full Sonnet call with nothing to read.
        defaults = {"title_filter": "(?i)product manager",
                    "location_filter": "(?i)(united states|remote)"}
        saved_q = scrape.QUEUE_DIR
        saved_fetch, saved_jd = dict(scrape.FETCHERS), dict(scrape.JD_DETAIL_FETCHERS)
        try:
            scrape.QUEUE_DIR = qdir
            wd_job = {"title": "Technical Program Manager",
                      "location": "Austin, TX, United States",
                      "url": "https://x/wd", "posted_at": "", "jd": "",
                      "_detail": "https://x/wd/detail"}

            def _boom(job):
                raise RuntimeError("502 Bad Gateway")

            scrape.FETCHERS["workday"] = lambda slug: [dict(wd_job)]
            scrape.JD_DETAIL_FETCHERS["workday"] = _boom
            st = scrape.poll_ats(conn, defaults, [
                {"company": "Widget", "board": "workday", "slug": "t/wd1/S",
                 "title_filter": "(?i)program manager"}])
            row = st["workday/t/wd1/S"]
            wd_id = db.job_id("Widget", wd_job["title"], wd_job["location"])
            check("the miss is counted", row.get("jd_missing"), 1)
            check("no row was inserted (so the next poll retries it)",
                  conn.execute("SELECT 1 FROM jobs WHERE id = ?", (wd_id,)).fetchone(),
                  None)
            check("it is not counted as new", row["new"], 0)
            check("no queue file was written", (qdir / f"{wd_id}.json").exists(), False)
            check("a JD miss is not a source error", row["error"], None)
        finally:
            scrape.QUEUE_DIR = saved_q
            scrape.FETCHERS.clear()
            scrape.FETCHERS.update(saved_fetch)
            scrape.JD_DETAIL_FETCHERS.clear()
            scrape.JD_DETAIL_FETCHERS.update(saved_jd)

        print("jobright enrichment is wired into the poll (no network)")
        today_md = datetime.now(timezone.utc).strftime("%b %d")
        readme = "\n".join([
            "## Daily Job List",
            "| Company | Job Title | Location | Work Model | Date Posted |",
            "| ----- | --------- | --------- | ---- | ------- |",
            f"| **[Anduril](http://a.com)** | **[Product Operations Associate]"
            f"(https://jobright.ai/jobs/info/aaa)** | Irvine, CA | Onsite | {today_md} |",
            f"| **[Beta Corp](http://b.com)** | **[Associate Product Manager]"
            f"(https://jobright.ai/jobs/info/bbb)** | Boston, MA | Remote | {today_md} |",
            f"| **[Gamma Inc](http://g.com)** | **[Line Cook]"
            f"(https://jobright.ai/jobs/info/ccc)** | Boston, MA | Onsite | {today_md} |",
        ])
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        detail_ok = ('<script id="job-posting" type="application/ld+json">'
                     + json.dumps({"@type": "JobPosting", "datePosted": stamp,
                                   "description": "<p>Own the roadmap.</p>"})
                     + "</script>")

        class _Resp:
            def __init__(self, text):
                self.text = text

            def raise_for_status(self):
                pass

        class _Fake:
            def __init__(self):
                self.urls = []

            def get(self, url, **kw):
                self.urls.append(url)
                if "raw.githubusercontent.com" in url:
                    return _Resp(readme)
                if url.endswith("aaa"):
                    return _Resp(detail_ok)
                raise RuntimeError("503 Service Unavailable")

        fake = _Fake()
        saved_req, saved_q2 = scrape.requests, scrape.QUEUE_DIR
        try:
            scrape.requests, scrape.QUEUE_DIR = fake, qdir
            st = scrape.poll_jobright_feed(conn, {
                "source": "jobright:test", "repo": "x/y", "shape": "newgrad",
                "note": "test feed", "row_note": "test list"})["jobright:test"]
            ok_id = db.job_id("Anduril", "Product Operations Associate", "Irvine, CA")
            bad_id = db.job_id("Beta Corp", "Associate Product Manager", "Boston, MA")
            cook_id = db.job_id("Gamma Inc", "Line Cook", "Boston, MA")
            r_ok = conn.execute("SELECT jd_text, posted_at FROM jobs WHERE id = ?",
                                (ok_id,)).fetchone()
            check("the enriched row carries a real JD", r_ok and r_ok[0], "Own the roadmap.")
            check("its posting date is the page's TIMESTAMP, not the table's date",
                  r_ok and r_ok[1], stamp.replace(" ", "T"))
            check("which is precision 'exact', the only kind the 24h rule can use",
                  db.job_age(r_ok[1])[2], "exact")
            entry = json.loads((qdir / f"{ok_id}.json").read_text(encoding="utf-8"))
            check("the queue file carries the JD too", entry["jd_markdown"],
                  "Own the roadmap.")
            check("and its first_seen", bool(entry["first_seen"]), True)
            # The failed one must leave NO dedupe row, or a 503 costs this
            # posting its JD forever.
            check("a failed detail fetch ingests nothing",
                  conn.execute("SELECT 1 FROM jobs WHERE id = ?", (bad_id,)).fetchone(),
                  None)
            check("and is counted", st.get("jd_missing"), 1)
            check("only the survivor is new", st["new"], 1)
            # Off-target titles are recorded without ever costing a request:
            # that filter is upstream of the fetch on purpose.
            check("an off-target title is still stored for dedupe",
                  bool(conn.execute("SELECT 1 FROM jobs WHERE id = ?",
                                    (cook_id,)).fetchone()), True)
            check("and its detail page is never fetched",
                  any(u.endswith("ccc") for u in fake.urls), False)
            check("two PM rows were tried, one succeeded",
                  (sum(1 for u in fake.urls if "jobs/info" in u),
                   st.get("detail_fetched")), (2, 1))
        finally:
            scrape.requests, scrape.QUEUE_DIR = saved_req, saved_q2

        print("'board returned 0 jobs' is an error only for FULL-BOARD fetchers")
        # A window-limited fetcher returns only what is inside the 72h window,
        # so 0 is its normal answer on a quiet board (PayPal, 2026-08-23).
        # Flagging that would put a permanent alarm on the dashboard clock,
        # which is the one thing acknowledged_degraded exists to prevent.
        saved = dict(scrape.FETCHERS)
        try:
            scrape.FETCHERS["greenhouse"] = lambda slug: []
            scrape.FETCHERS["workday"] = lambda slug: []
            defaults = {"title_filter": "(?i)product manager",
                        "location_filter": "(?i)(united states|remote)"}
            st = scrape.poll_ats(conn, defaults, [
                {"company": "Full", "board": "greenhouse", "slug": "full"},
                {"company": "Windowed", "board": "workday", "slug": "t/wd1/S"},
            ])
            check("empty full board is an error",
                  bool(st["greenhouse/full"]["error"]), True)
            check("empty windowed board is NOT an error",
                  st["workday/t/wd1/S"]["error"], None)
            check("an empty fetch never runs the sweep",
                  "delisted" in st["greenhouse/full"], False)

            print("a paginated fetch that stopped short says so")
            # Two loop exits mean opposite things: hitting the freshness
            # boundary is complete coverage, hitting the page cap is a silent
            # gap that repeats every hour. It has to be visible, and it must
            # NOT masquerade as either a dead source or a clean poll.
            truncated = scrape.BoardResult()
            truncated.truncated = "offset cap hit"
            scrape.FETCHERS["workday"] = lambda slug: truncated
            st = scrape.poll_ats(conn, defaults, [
                {"company": "Windowed", "board": "workday", "slug": "t/wd1/S"}])
            row = st["workday/t/wd1/S"]
            check("coverage gap reported", row.get("coverage_gap"), "offset cap hit")
            # `note` would drop the source from main()'s blind-run quorum, and
            # `error` would colour the dashboard clock. It is neither.
            check("a coverage gap is not an error", row["error"], None)
            check("a coverage gap is not a `note`", row.get("note"), None)
        finally:
            scrape.FETCHERS.clear()
            scrape.FETCHERS.update(saved)
        conn.close()
    ingest_scores.SCORES_DIR = saved_sc

    print("ATS prefetch: concurrent fetch, sequential everything else")
    # poll_ats was strictly sequential until 2026-09-12 and that was 124s of a
    # 149s run. The contract this pins is that parallelising moved TIMING only:
    # results keyed by the target's ORIGINAL index, non-pollable boards absent,
    # and a raising board's exception CAPTURED rather than escaping (poll_ats
    # re-raises it inside that board's own try/except so one bad board still
    # cannot kill the run).
    saved_f = dict(scrape.FETCHERS)
    try:
        calls = []

        def _ok(slug):
            calls.append(slug)
            return [{"title": f"PM {slug}", "location": "Boston, MA",
                     "url": f"https://x/{slug}", "posted_at": "", "jd": ""}]

        def _boom(slug):
            calls.append(slug)
            raise RuntimeError("503 Service Unavailable")

        scrape.FETCHERS["greenhouse"] = _ok
        scrape.FETCHERS["ashby"] = _ok
        scrape.FETCHERS["lever"] = _boom
        tg = [
            {"company": "A", "board": "greenhouse", "slug": "a"},
            {"company": "B", "board": "custom"},              # no poller
            {"company": "C", "board": "ashby", "slug": "c"},
            {"company": "D", "board": "lever", "slug": "d"},  # raises
        ]
        pre = scrape._prefetch_boards(tg)
        check("keyed by original target index", sorted(pre), [0, 2, 3])
        check("non-pollable board is absent", 1 in pre, False)
        check("a good board returns its rows", len(pre[0][0]), 1)
        check("a good board carries no error", pre[2][1], None)
        check("a raising board is CAPTURED, not raised",
              type(pre[3][1]).__name__, "RuntimeError")
        check("and its rows are None", pre[3][0], None)
        check("every pollable board was fetched exactly once",
              sorted(calls), ["a", "c", "d"])

        # interleaving: consecutive workers should not all hit one provider.
        # 36 of the real 69 boards are greenhouse and 25 ashby, so a flat pool
        # would aim its whole width at one host.
        many = ([{"company": f"g{i}", "board": "greenhouse", "slug": f"g{i}"} for i in range(4)]
                + [{"company": f"a{i}", "board": "ashby", "slug": f"a{i}"} for i in range(4)])
        calls.clear()
        scrape._prefetch_boards(many)
        first4 = set(c[0] for c in calls[:4])
        check("providers are interleaved, not batched by host",
              first4 == {"g", "a"}, True, )
    finally:
        scrape.FETCHERS.clear()
        scrape.FETCHERS.update(saved_f)

    print("workday paging: no searchText, and two all-stale pages to stop")
    # Why these are pinned: the loop's exit used to trust a STRICT date sort and
    # a searchText-narrowed stream. Measured 2026-09-12, searchText switches
    # PayPal to a relevance rank (25 inversions in 60 rows) so the freshness
    # exit never fires, and even without it 4 of 7 tenants carry a few
    # inversions. Both failures are silent: one pages to the cap forever, the
    # other drops live rows behind one unlucky page.
    TODAY_S = datetime.now(timezone.utc).date()

    def _page(ages):
        """One Workday page: `ages` are 'days ago' per posting."""
        return [{"title": f"Product Manager {a}", "externalPath": f"/job/{i}-{a}",
                 "locationsText": "McLean, Virginia",
                 "postedOn": "Posted Today" if a == 0 else f"Posted {a} Days Ago"}
                for i, a in enumerate(ages)]

    class _WdResp:
        def __init__(self, payload): self._p = payload
        def raise_for_status(self): pass
        def json(self): return self._p

    class _WdFake:
        def __init__(self, pages):
            self.pages, self.bodies = pages, []
        def post(self, url, **kw):
            self.bodies.append(kw.get("json", {}))
            off = kw["json"]["offset"] // scrape.WORKDAY_PAGE
            return _WdResp({"jobPostings": self.pages[off] if off < len(self.pages) else []})

    saved_wd = scrape.requests
    try:
        # page 0 fresh, page 1 all stale, page 2 fresh again (an inversion),
        # page 3+ stale. A one-page exit would stop at page 1 and lose page 2.
        fake = _WdFake([_page([0, 0]), _page([40, 40]), _page([1, 1]),
                        _page([40, 40]), _page([40, 40])])
        scrape.requests = fake
        got = scrape.fetch_workday("tenant/wd1/Site")
        check("no searchText is ever sent",
              any("searchText" in b for b in fake.bodies), False)
        # 2 rows from page 0 + 2 from the post-inversion page 2 = 4. If the walk
        # had stopped at the first all-stale page, page 2's two live rows would
        # be gone and this would read 2.
        check("one inverted page does not end the walk", len(got), 4)
        check("stops after two consecutive all-stale pages", len(fake.bodies), 5)

        fake2 = _WdFake([_page([0, 0]), _page([40, 40]), _page([40, 40]), _page([1, 1])])
        scrape.requests = fake2
        got2 = scrape.fetch_workday("tenant/wd1/Site")
        check("two stale pages in a row DO end the walk", len(got2), 2)
        check("and it stops asking", len(fake2.bodies), 3)
    finally:
        scrape.requests = saved_wd

    test_titles_and_linkedin_depth()
    test_reposts()
    test_locations()
    test_jobright_jd()
    test_relisted()

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
        return 1
    print("all scrape-source tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
