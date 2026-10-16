"""Tests for what the dashboard is allowed to publish.
Run: python pipeline/test_dashboard.py

The contract being guarded: `.github/workflows/dashboard.yml` greps the built
page for `master-doc`, `PRIVATE_PROFILE` and phone-number patterns and REFUSES TO DEPLOY
on a hit. That guard is aimed at a code change embedding one of those files. It
cannot tell that apart from cloud-authored prose merely naming one, and on
2026-08-23 it did not: a routine-written entry whose teaser opened with
"**User stories at bullet level.** `master-doc.md`" reached the page, the run
failed, the deploy step never ran, and the published dashboard froze while
every rebuild repeated the failure. `dashboard.routine_text()` is the fix, and
this pins it. The panel that carried that particular string is gone; the class
of failure is not, which is why these tests stayed.

Two rules carry it:

  1. Every field the cloud routine writes goes through `routine_text` before it
     reaches the page. Today that is the cloud-sidecar note and the scorer's
     notes column. A third one added later without it re-arms the same freeze --
     which is why the second test renders the WHOLE page from the real repo
     state rather than checking the two known fields.
  2. The pattern this test enforces is READ OUT OF the workflow file, never
     restated here. A copy would drift from CI silently, and a test that passes
     while CI fails is worth less than no test.

Read-only: the page is built to a temp dir and `pipeline/jobs.sqlite` is only
ever opened for reading, exactly as the workflow does it.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import dashboard  # noqa: E402

WORKFLOW = ROOT / ".github" / "actions" / "publish-dashboard" / "action.yml"
# Read, never restated: the null-handling this test relies on lives in the
# shipped script, and a copy here would keep passing after the script changed.
APP_JS = (dashboard.ASSETS / "app.js").read_text(encoding="utf-8")

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}  (got {got!r}, want {want!r})")
        FAILS.append(label)


def ci_guard_pattern() -> re.Pattern:
    """The exact regex the publish step greps with, lifted from the workflow.

    Failing loudly when it cannot be found is the point: if the guard is renamed
    or rewritten, this test must stop rather than quietly enforce a stale copy.
    """
    text = WORKFLOW.read_text(encoding="utf-8")
    m = re.search(r"grep -[a-zA-Z]*E '([^']+)' publish/index\.html", text)
    if not m:
        raise SystemExit(
            f"FATAL: no publish-guard grep found in {WORKFLOW}. The guard this "
            "test mirrors has moved or changed shape; update ci_guard_pattern().")
    return re.compile(m.group(1), re.I)


# --- 1. the helper ---------------------------------------------------------

def test_routine_text_strips_markdown():
    got = dashboard.routine_text("**Bold.** some `code` and __also bold__ text")
    check("markdown markers gone", got, "Bold. some code and also bold text")


def test_single_underscores_are_left_alone():
    """Deliberate, not an oversight. The Routines write `**` and backticks and
    zero single-underscore emphasis (measured against profile/attestations.md
    2026-08-23: 0 occurrences), but they name snake_case identifiers constantly
    (18 there). Stripping `_` would turn `needs_review` into `needsreview`."""
    check("identifier intact", dashboard.routine_text("status is `needs_review` now"),
          "status is needs_review now")


def test_routine_text_defuses_every_ci_pattern():
    """One string carrying each thing the workflow greps for."""
    guard = ci_guard_pattern()
    raw = ("see `master-doc.md` and master-doc Section 0, plus "
           "`PRIVATE_PROFILE_Master.md` (PRIVATE_PROFILE §15), call +1-555-0123456, "
           "see profile/knowledge/20-citi-product-owner.md and profile/archive/x.md")
    check("raw sample would fail CI", bool(guard.search(raw)), True)
    check("sanitized sample passes CI",
          bool(guard.search(dashboard.routine_text(raw))), False)


def test_routine_text_reads_as_prose():
    check("no doubled article", dashboard.routine_text("the `master-doc.md` file"),
          "the master doc file")
    check("idempotent", dashboard.routine_text(dashboard.routine_text("`master-doc.md`")),
          "master doc")


def test_routine_text_truncates_on_a_word_boundary():
    raw = "**" + " ".join(["alpha"] * 40) + "**"
    got = dashboard.routine_text(raw, 110)
    check("truncated", len(got) <= 111, True)
    check("no half word", got.endswith("alpha\u2026"), True)
    check("markup did not eat the budget", got.startswith("alpha alpha"), True)
    check("under the limit is left alone", dashboard.routine_text("short", 110), "short")


def test_routine_text_survives_empty():
    check("none", dashboard.routine_text(None), "")
    check("empty", dashboard.routine_text("", 110), "")


# --- 2. the whole page, against CI's own pattern ---------------------------

def test_the_built_page_passes_the_ci_guard():
    """Render the real page the way the workflow does, then grep it the way the
    workflow does. This is the test that catches a NEW Routine-written field."""
    guard = ci_guard_pattern()
    with tempfile.TemporaryDirectory(prefix="dashboard-test-") as tmp:
        out = Path(tmp) / "index.html"
        r = subprocess.run(
            [sys.executable, str(HERE / "dashboard.py"), "--links", "github",
             "--out", str(out)],
            capture_output=True, text=True, cwd=str(ROOT))
        check("dashboard.py exited 0", r.returncode, 0)
        if r.returncode != 0:
            print(r.stderr.strip()[:2000])
            return
        html = out.read_text(encoding="utf-8", errors="replace")

    hits = guard.findall(html)
    if hits:
        for m in re.finditer(r".{70}" + guard.pattern + r".{40}", html, re.I | re.S):
            print(f"    leak context: ...{m.group(0)}...")
    check("built page matches nothing the CI guard greps for", hits, [])

    # The screening draft is the one file a submission cannot be completed
    # without -- the resume deliberately carries no work-authorization or
    # location line (ruled 2026-08-24), so those answers live nowhere else.
    # Asserted against the REAL page, not the link-building code: "a link
    # exists" and "a link is emitted" are different claims.
    #
    # Scoped to the folders the page actually RENDERS, which is not every
    # folder on disk -- rows past the apply window are archived out of the
    # table unless their status outranks it, so a `skipped` folder with a
    # draft correctly contributes no link. Counting drafts on disk would fail
    # for a reason that has nothing to do with this link.
    # brief.md is the current name; score.md is what pre-Phase-4 folders wrote.
    slug = re.compile(r"applications/([^/\"]+)/(?:brief|score)\.md")
    shown = set(slug.findall(html))
    with_draft = {s for s in shown
                  if (ROOT / "applications" / s / "screening-draft.md").is_file()}
    linked = set(re.findall(r"applications/([^/\"]+)/screening-draft\.md", html))
    check("every rendered folder that has a screening draft links to it",
          sorted(with_draft - linked), [])
    check("no screening link is emitted for a folder with no draft",
          sorted(linked - with_draft), [])
    if with_draft:
        check("the screening link is labelled, not a bare url",
              ">screening</a>" in html, True)


def a_card_row(**over) -> dict:
    """A complete job dict as build_page() assembles one, for card()."""
    row = {"id": "a" * 40, "company": "Acme", "title": "Product Manager",
           "location": "Boston, MA", "bucket": "ma", "url": "https://x/y",
           "posted": "", "status": "new", "score": None, "notes": "",
           "source": "greenhouse", "best": None, "worst": None,
           "precision": "unknown", "tier": "nodate", "lane": "", "links": [],
           "verdict": "", "review": None, "review_href": "", "word_pages": None,
           "ats": None, "has_folder": False,
           "pending": "", "title_norm": "product manager", "age": "age unknown",
           "decision": "", "actions": False, "verify": None}
    row.update(over)
    return row


def best_attr(j: dict) -> str:
    m = re.search(r'data-best="([^"]*)"', dashboard.card(j))
    return m.group(1) if m else "<absent>"


def test_only_posting_evidence_gets_a_sort_number():
    # data-best drives the client-side "Newest first" sort, which compares the
    # bare number. A `seen` row's best is a FLOOR on its age with no ceiling --
    # "we recorded this 2h ago" says nothing about when it went up -- so a
    # number there would let it outrank a posting proven 10h old. db.age_tier
    # bars `seen` from now/soon for exactly this reason; the attribute has to
    # apply the same bar or the two surfaces disagree.
    check("an exact timestamp sorts",
          best_attr(a_card_row(best=6.4, worst=6.4, precision="exact", tier="now")), "6")
    check("a date-only posting date sorts (its optimistic end is real evidence)",
          best_attr(a_card_row(best=17.0, worst=41.0, precision="day", tier="now")), "17")
    check("a row known only by when we SAW it carries no number",
          best_attr(a_card_row(best=2.0, worst=None, precision="seen", tier="nodate")), "")
    check("nor does one with no age evidence at all",
          best_attr(a_card_row(best=None, worst=None, precision="unknown")), "")
    # app.js sorts nulls last in BOTH directions, which is where a seen row sat
    # before first_seen started bounding job_age().
    check("the JS still sorts missing values last in both directions",
          "if (x === null && y === null)" in APP_JS
          and "else if (x === null) return 1;" in APP_JS, True)


def test_the_degraded_amber_has_a_threshold():
    # `errors=` was designed when Layer 1 polled ~10 sources. At 69 boards
    # polled hourly one transient 5xx would hold the clock amber forever -- and
    # since ruling R9 collapsed the timestamp amber band, this is the ONLY
    # degraded signal the page has, so an always-on amber leaves it with none.
    clean = "new=3 stale=12 offtarget=40"
    one = clean + " errors=ashby/notion"
    two = clean + " errors=ashby/notion,greenhouse/brex"
    check("a clean run names no degraded source", dashboard.degraded_sources(clean), [])
    check("one errored source is parsed but is not degradation",
          (dashboard.degraded_sources(one), dashboard.is_degraded(one)),
          (["ashby/notion"], False))
    check("two is", dashboard.is_degraded(two), True)
    check("all of them are named, not just the count",
          dashboard.degraded_sources(two), ["ashby/notion", "greenhouse/brex"])
    check("the threshold is the documented constant",
          dashboard.DEGRADED_MIN_SOURCES, 2)
    # known_degraded= is a separate field for acknowledged-dead sources, and it
    # must never be read as a fresh error -- that is what keeps a source which
    # fails every hour from pinning the alarm on permanently.
    check("an acknowledged-dead source is not counted",
          dashboard.is_degraded(clean + " known_degraded=jobspy[google],jobright:h1b"),
          False)
    check("and does not sneak in beside one real error",
          dashboard.is_degraded(one + " known_degraded=jobspy[google]"), False)
    check("a summary with no errors= at all is safe",
          dashboard.is_degraded(""), False)


def test_the_id_line_may_omit_backticks():
    """The fence is formatting; the id is identity. Only one of them matters.

    The cloud tailor writes this line by hand and does not always fence it.
    When the pattern required the fence, 8 of 57 folders were unmappable and
    lost every link on the page -- 6 of them written in one run, so the failure
    arrives in batches rather than one folder at a time.
    """
    tmp = Path(tempfile.mkdtemp(prefix="dashboard-idmap-"))
    apps = tmp / "applications"
    cases = {
        "bare-role-2026-09-01": "**id:** " + "a" * 40,
        "fenced-role-2026-09-01": "**id:** `" + "b" * 40 + "`",
        # A Greenhouse posting number, not a job hash: there is no id here to
        # find, so this folder stays unmappable and the screening-draft sweep
        # reaches it only because it already carries a draft.
        "greenhouse-role-2026-09-01": "**id:** `8108241`",
        # A longer hex run must not match on its first 40 characters -- that
        # would map the folder to an id that is not its own, which is worse
        # than not mapping it at all.
        "long-hex-role-2026-09-01": "**id:** " + "c" * 64,
    }
    for name, line in cases.items():
        d = apps / name
        d.mkdir(parents=True)
        (d / "score.md").write_text(f"# Co - Product Manager\n\n{line}\n",
                                    encoding="utf-8")
    # A folder written under the new contract: brief.md is the id line's home,
    # and a score.md a rerun left behind must not win. Same id in both would
    # prove nothing, so they differ.
    both = apps / "rewritten-role-2026-09-16"
    both.mkdir(parents=True)
    (both / "brief.md").write_text("**id:** " + "d" * 40 + "\n", encoding="utf-8")
    (both / "score.md").write_text("**id:** " + "e" * 40 + "\n", encoding="utf-8")
    real = dashboard.ROOT
    try:
        dashboard.ROOT = tmp
        found = {jid: p.name for jid, p in dashboard.application_folders().items()}
    finally:
        dashboard.ROOT = real

    check("an unfenced **id:** maps to its folder",
          found.get("a" * 40), "bare-role-2026-09-01")
    check("a fenced **id:** still maps to its folder",
          found.get("b" * 40), "fenced-role-2026-09-01")
    check("a 64-char hex string maps to nothing",
          "c" * 40 in found, False)
    check("brief.md is read before score.md",
          found.get("d" * 40), "rewritten-role-2026-09-16")
    check("...and the stale score.md id is not mapped at all",
          "e" * 40 in found, False)
    check("only the three real ids are mapped", len(found), 3)


# --- the review contract ---------------------------------------------------

def _folder(**files) -> Path:
    """A throwaway application folder holding the named files.

    Every `_` in a keyword becomes a `.`, so `review_md` writes `review.md`.
    Only usable for names with one dot.
    """
    d = Path(tempfile.mkdtemp(prefix="dashboard-review-"))
    for name, text in files.items():
        (d / name.replace("_", ".")).write_text(text, encoding="utf-8")
    return d


def test_review_scores_reads_the_scores_line():
    f = _folder(review_md="prose\n\nSCORES: ats 82 / recruiter 8 / hm 7\n")
    check("the three numbers are parsed", dashboard.review_scores(f),
          {"ats": 82, "recruiter": 8, "hm": 7})


def test_review_scores_takes_the_last_line():
    # Same rule the VERDICT parser had, and for the same reason: a re-judged
    # folder APPENDS, and only the newest line describes the file on disk. A
    # first-match read would pin the page to the draft that was rewritten.
    f = _folder(review_md=("SCORES: ats 41 / recruiter 4 / hm 5\n"
                           "\n(second pass)\n\n"
                           "SCORES: ats 88 / recruiter 9 / hm 8\n"))
    check("the newest line wins", dashboard.review_scores(f),
          {"ats": 88, "recruiter": 9, "hm": 8})


def test_a_missing_or_unreadable_line_is_absent_not_zero():
    # "Never scored" and "scored zero" are different claims, and only one of
    # them says a resume is bad. The badge's whole "Not measured yet" state
    # hangs on this returning None rather than a dict of zeros.
    check("no folder at all", dashboard.review_scores(None), None)
    check("a folder with no review.md",
          dashboard.review_scores(_folder(brief_md="x")), None)
    check("a review.md with no SCORES line",
          dashboard.review_scores(_folder(review_md="prose only\n")), None)
    # Column zero and no markup is the contract, and this parser is strict
    # about it: the judge writes the line itself from its own prompt, unlike
    # the retired ledger lines that tailors decorated by hand.
    check("an indented line does not count",
          dashboard.review_scores(
              _folder(review_md="  SCORES: ats 8 / recruiter 8 / hm 8\n")), None)
    check("a partial line does not count",
          dashboard.review_scores(
              _folder(review_md="SCORES: ats 82 / recruiter 8\n")), None)


def test_an_old_folder_still_shows_its_verdict():
    # Every folder written before the SCORES line has a VERDICT and nothing
    # else. Rendering those blank would read as "never reviewed", which is the
    # one thing they are not.
    f = _folder(review_md="long review\n\nVERDICT: FAIL\n")
    check("no scores to find", dashboard.review_scores(f), None)
    check("the legacy verdict is still read", dashboard.review_verdict(f), "FAIL")
    out = dashboard.review_html(None, "FAIL", "")
    check("and it renders as the legacy text",
          "critic: FAIL" in out and 'class="verdict fail"' in out, True)
    check("a folder with neither renders nothing",
          dashboard.review_html(None, "", ""), "")


def test_the_review_readout_colours_on_the_weaker_human():
    # ats is vocabulary, not judgement: a resume with 95% coverage that neither
    # simulated human would advance is not a green card. min(recruiter, hm) is
    # the gate, so a 9 beside a 5 must read red.
    def cls(recruiter, hm):
        out = dashboard.review_html(
            {"ats": 70, "recruiter": recruiter, "hm": hm}, "", "")
        return re.search(r'class="verdict (\w+)"', out).group(1)

    check("both strong is green", cls(9, 8), "pass")
    check("the weaker one decides", cls(9, 6), "warn")
    check("...even when the other is excellent", cls(10, 5), "fail")
    check("the green boundary is 8", cls(8, 8), "pass")
    check("and 6 is the amber floor", cls(6, 6), "warn")

    linked = dashboard.review_html(
        {"ats": 82, "recruiter": 8, "hm": 7}, "", "../applications/x/review.md")
    check("the three numbers are printed",
          "ATS 82 &middot; R 8 &middot; HM 7" in linked, True)
    check("...and link to the review they came from",
          'href="../applications/x/review.md"' in linked, True)


def test_the_review_readout_is_on_the_card_face():
    # Vaish 10-16: with R and HM folded into Details, only the scorer's number was
    # in view and his apply calls followed it (W8). The floor that gates a send
    # has to be readable at a glance, outside the <details>.
    out = dashboard.card(a_card_row(
        status="tailored", review={"ats": 92, "recruiter": 8, "hm": 7}))
    face = out.split('<details class="more">')[0]
    check("the three numbers are on the face",
          "ATS 92 &middot; R 8 &middot; HM 7" in face, True)
    check("...and not repeated inside Details",
          out.count("R 8 &middot; HM 7"), 1)


def test_the_word_page_count_becomes_a_badge():
    # The page count is the one defect a reader cannot see from the PDF link,
    # so it is on the card rather than buried in the notes prose.
    notes = "auto-tailor; rendered ok; word_pages=2; scores=82/8/7"
    check("the clause is parsed", dashboard.word_pages(notes), 2)
    check("a row without one has no badge",
          dashboard.word_pages("auto-tailor"), None)
    check("the badge says pages", dashboard.word_badge(2),
          '<div class="wpg">Word: 2 pages</div>')
    check("one page is singular", "1 page</div>" in dashboard.word_badge(1), True)
    check("no count, no badge", dashboard.word_badge(None), "")
    check("and it reaches the card",
          'class="wpg">Word: 3 pages' in dashboard.card(a_card_row(word_pages=3)),
          True)


def test_notes_carry_the_scores_when_review_md_is_not_in_this_checkout():
    # The cloud writes review.md on its own branch; sync.py folds the numbers
    # into `notes`. Without this fallback a just-synced row would render
    # "Not measured yet" until the next branch overlay.
    check("the clause is parsed",
          dashboard.notes_scores("tailored; word_pages=1; scores=76/9/8"),
          {"ats": 76, "recruiter": 9, "hm": 8})
    check("a row without one yields nothing",
          dashboard.notes_scores("auto-tailor; strong fit"), None)


def test_routine_text_strips_knowledge_citations():
    """W6: brief prose cites its sources by path or by knowledge-file name."""
    check("a bare file name reads as the record",
          dashboard.routine_text("refused, not launched (20-citi-product-owner.md, "
                                 "Facts to keep straight)"),
          "refused, not launched (the record, Facts to keep straight)")
    check("a list of files is one record",
          dashboard.routine_text("SQL (50-skills.md, 21-citi-product-analyst.md)."),
          "SQL (the record).")
    check("a path goes, the sentence stays",
          dashboard.routine_text("see profile/knowledge/60-attested-facts.md for it"),
          "see the record for it")
    check("a stripped phone leaves no brackets",
          dashboard.routine_text("call (+1-555-0123456) now"), "call now")
    check("version numbers are not file names", dashboard.routine_text("C# .NET 4.8"),
          "C# .NET 4.8")


BRIEF_HEAD = "# Acme - PM\n**id:** " + "b" * 40 + "\n\n## The problem\nx\n\n"
ASK = "\n## Would raise the score if true\n- Any Ops feedback after release?\n"


def verify_of(body: str) -> dict:
    with tempfile.TemporaryDirectory(prefix="dashboard-brief-") as tmp:
        (Path(tmp) / "brief.md").write_text(BRIEF_HEAD + body, encoding="utf-8")
        return dashboard.brief_verify(Path(tmp))


def test_brief_verify_reads_every_inferred_shape():
    """The shapes measured over all 225 brief lines on 10-11 (dashboard.py W6)."""
    v = verify_of("## Tailoring notes\n- Citi carries the page.\n"
                  '- Inferred beyond the written record: "sprint-ready" stories. '
                  "Strike if not true.\n" + ASK)
    check("labelled line, boilerplate stripped", v["inferred"], ['"sprint-ready" stories.'])
    check("first would-raise line", v["ask"], "Any Ops feedback after release?")
    v = verify_of("## Tailoring notes\n- Inferred beyond the record: UAT with Ops. "
                  "Vaish should strike any he cannot defend.\n" + ASK)
    check("'beyond the record', other boilerplate", v["inferred"], ["UAT with Ops."])
    v = verify_of('## Tailoring notes\n- Inferences beyond the record: "Monitor" '
                  "for the dashboards.\n" + ASK)
    check("'Inferences' label", v["inferred"], ['"Monitor" for the dashboards.'])
    v = verify_of("## Tailoring notes\n- **Inferred beyond the written record.** "
                  '"Walking students through the models".\n' + ASK)
    check("bold label with a full stop", v["inferred"],
          ['"Walking students through the models".'])
    v = verify_of("## Tailoring notes\nSAP and Networking OS as projects; Citi PO\n"
                  'first. Inferred: "wrote the test cases" as PO. Scores after round 1:\n'
                  "ATS 80 / recruiter 8.\n" + ASK)
    check("mid-paragraph, wrapped, scores tail cut", v["inferred"],
          ['"wrote the test cases" as PO.'])
    v = verify_of("## Revision notes\n- r1: SQL named on the data-quality bullet "
                  "(inferred: query work).\n- r1: Kafka bullet kept; $0 inference stays.\n"
                  '\n## Tailoring notes\n- Inferred: "distributed" for Kafka.\n' + ASK)
    check("labelled line plus a parenthetical, ML 'inference' ignored",
          sorted(v["inferred"]),
          sorted(['"distributed" for Kafka.',
                  "r1: SQL named on the data-quality bullet (inferred: query work)."]))
    v = verify_of("## Tailoring notes\n- Inferred beyond the written record: UAT with Ops "
                  "on the Citi PO bullet | SQL on the data-quality bullet\n" + ASK)
    check("fixed line (10-13): one claim per ' | '", v["inferred"],
          ["UAT with Ops on the Citi PO bullet", "SQL on the data-quality bullet"])
    v = verify_of("## Tailoring notes\n- Inferred beyond the written record: none\n" + ASK)
    check("fixed line 'none' is none", v["inferred"], [])
    v = verify_of("## Tailoring notes\n- Nothing inferred beyond the record.\n" + ASK)
    check("a negation is none, not unstated", v["inferred"], [])
    v = verify_of("## Tailoring notes\n- Inferred beyond the written record: none beyond "
                  "phrasing; 13 of 14 is the recorded denominator.\n" + ASK)
    check("'none beyond phrasing' is none", v["inferred"], [])
    v = verify_of("## Tailoring notes\n- Citi carries the page; $0 inference on "
                  "Networking OS.\n" + ASK)
    check("never mentioned is None, not none", v["inferred"], None)
    v = verify_of("## Tailoring notes\n- x\n\n## Would raise the score if true\n- none open\n")
    check("'none open' is an empty ask", v["ask"], "")
    v = verify_of("## Tailoring notes\n- x\n")
    check("no would-raise section is None", v["ask"], None)
    check("no folder, no verify", dashboard.brief_verify(None), None)


def test_a_real_brief_citing_paths_passes_the_guard():
    """A real brief whose inferred line names profile/ paths. None is on the
    board on 10-11, so the built-page test below cannot see one."""
    guard = ci_guard_pattern()
    hits = [f for f in sorted((ROOT / "applications").glob("*/brief.md"))
            if re.search(r"Infer[^\n]*profile/knowledge",
                         f.read_text(encoding="utf-8", errors="replace"))]
    if not hits:
        print("  SKIP  no brief cites a profile/ path in an inferred line")
        return
    v = dashboard.brief_verify(hits[0].parent)
    raw = " ".join(v["inferred"] or [])
    check("the raw claim would trip the guard", bool(guard.search(raw)), True)
    check("the drawn block does not", bool(guard.search(dashboard.verify_html(v))), False)


def test_the_ready_card_says_what_to_verify():
    def tailored(v):
        return dashboard.card(a_card_row(status="tailored", verify=v))
    got = tailored({"inferred": ['"sprint-ready" (50-skills.md)'], "ask": "Any CSAT?"})
    check("claims are listed, citation reads as the record",
          "<li>&quot;sprint-ready&quot; (the record)</li>" in got, True)
    check("the ask is drawn", "Any CSAT?" in got, True)
    got = tailored({"inferred": [], "ask": ""})
    check("none says none", "none inferred" in got and "none open" in got, True)
    got = tailored({"inferred": None, "ask": None})
    check("unstated is not none",
          "not stated in brief, open it" in got and "none inferred" not in got, True)
    check("unstated is marked for colour", 'class="vf unstated"' in got, True)
    check("no block before tailoring",
          'class="verify"' in dashboard.card(a_card_row(
              status="scored", verify={"inferred": ["x"], "ask": "y"})), False)


def test_the_ready_column_counts_what_the_window_archived():
    def row(status, h):
        return {"status": status, "posted_h": h, "seen_h": h}
    gone = [row("tailored", 100), row("tailored", 160), row("tailored", 500),
            row("tailored", None), row("scored", 100)]
    check("week share and total",
          "<b>2</b> more from the last 7 days (<b>4</b> in all)"
          in dashboard.old_ready_line(gone), True)
    check("zero is still drawn", "<b>0</b> more" in dashboard.old_ready_line([]), True)


def test_deadlines():
    """10-15: a JD deadline goes on the card and holds it on the board until it passes."""
    from datetime import date, datetime, timedelta, timezone
    T = date(2026, 10, 15)
    cases = {
        "Applications must be submitted by Wednesday, October 21st, 2026 at 5:00pm PT": date(2026, 10, 21),
        "You must complete your application *before* end of day on October 6, 2025": date(2025, 10, 6),
        "incentive bonus.Application deadline is 11/20/2026.": date(2026, 11, 20),
        "Application Close Date: 10/12/2026": date(2026, 10, 12),
        "**The application window is expected to close on:** 10/02/2026Meet the team": date(2026, 10, 2),
        "Apply by Nov 2 for priority review.": date(2026, 11, 2),
        "**Application Deadline:** This will be posted for a minimum of 2 business days": None,
        "Applications will be accepted on an ongoing basis until the position is filled.": None,
        "Ability to prioritize and meet deadlines by Oct 3": None,
    }
    for text, want in cases.items():
        check(f"deadline in {text[:48]!r}", dashboard.deadline_of(text, T), want)
    check("badge", dashboard.deadline_badge({"deadline": date(2026, 10, 21)}, T),
          '<div class="deadline">Apply by Oct 21</div>')
    check("badge, 1 day left", 'class="deadline soon"' in dashboard.deadline_badge(
        {"deadline": date(2026, 10, 16)}, T), True)
    check("badge, passed", "Deadline passed Oct 6" in dashboard.deadline_badge(
        {"deadline": date(2025, 10, 6)}, T), True)
    check("no deadline, no badge", dashboard.deadline_badge({}, T), "")
    today = datetime.now(timezone.utc).date()
    old = {"status": "tailored", "tier": "stale", "posted_h": 200, "seen_h": 200}
    check("a week-old resume with a deadline ahead stays",
          dashboard.is_archived({**old, "deadline": today + timedelta(days=6)}), False)
    check("a passed deadline does not hold it",
          dashboard.is_archived({**old, "deadline": today - timedelta(days=1)}), True)
    check("a resume tailored 5h ago from an old posting stays",
          dashboard.is_archived({**old, "tailored_h": 5}), False)
    check("tailored 4 days ago from an old posting is archived",
          dashboard.is_archived({**old, "tailored_h": 96}), True)


def test_the_board_shows_only_the_last_72_hours():
    """Vaish 10-08: posted OR found in the last 72h, tailored resumes included."""
    row = lambda status, posted_h, seen_h, tier="soon": {
        "status": status, "tier": tier, "posted_h": posted_h, "seen_h": seen_h}
    check("a resume posted 30h ago stays", dashboard.is_archived(row("tailored", 30, 30)), False)
    check("a resume posted 5 days ago but found 20h ago stays",
          dashboard.is_archived(row("tailored", 120, 20)), False)
    check("a resume posted and found 4 days ago is archived",
          dashboard.is_archived(row("tailored", 96, 96, "stale")), True)
    check("an old render problem is archived too",
          dashboard.is_archived(row("needs_review", 200, 200, "stale")), True)
    check("an application stays at any age",
          dashboard.is_archived(row("applied", 900, 900, "stale")), False)
    check("an interview stays at any age",
          dashboard.is_archived(row("interview", 900, 900, "stale")), False)
    check("no date at all is not proof of age",
          dashboard.is_archived(row("tailored", None, None, "nodate")), False)


def test_a_stretch_role_says_so_on_its_card():
    check("a digest-stretch card carries the stretch tag",
          'class="stretch"' in dashboard.card(a_card_row(lane="digest-stretch")), True)
    check("an in-band card does not",
          'class="stretch"' in dashboard.card(a_card_row(lane="auto-tailor")), False)
    check("the scorer's note maps to that lane",
          dashboard.lane_of("digest-stretch; Sr PM, 5+ years (b)=1"), "digest-stretch")


def test_the_ready_order_leads_on_the_weaker_human():
    # The one column that does NOT lead on freshness: everything in it is
    # already tailored, so the question is which to send first.
    check("unreviewed sorts last", dashboard.review_floor(None), -1)
    check("the weaker of the two is the key",
          dashboard.review_floor({"ats": 99, "recruiter": 9, "hm": 6}), 6)


def test_the_keyword_badge_reads_the_review_percentage():
    badge = dashboard.ats_badge(82, True)
    check("the percentage is printed", "Keywords 82%" in badge, True)
    check("the meter is drawn from it", "--r:82" in badge, True)
    check("a folder with no score says so, in words",
          "Not measured yet" in dashboard.ats_badge(None, True), True)
    check("a role with no folder gets nothing at all",
          dashboard.ats_badge(None, False), "")
    check("zero is a measurement, not an absence",
          "Keywords 0%" in dashboard.ats_badge(0, True), True)
    check("high coverage is graded green", dashboard.ats_bucket(88)[0], "low")
    check("...and thin coverage amber", dashboard.ats_bucket(40),
          ("high", "Worth another pass"))


def test_the_screen_badge_reads_screen_json():
    f = _folder(screen_json=json.dumps({
        "version": 1, "round": 2, "grade": "C", "score": 56,
        "basic": {"met": 4, "partial": 2, "total": 8},
        "preferred": {"met": 0, "partial": 0, "total": 2}}))
    sc = dashboard.screen_summary(f)
    check("screen.json is read", sc, {"grade": "C", "score": 56, "basic_met": 4,
                                       "basic_total": 8, "pref_met": 0, "pref_total": 2})
    badge = dashboard.ats_badge(56, True, sc)
    check("the grade and the basic count are printed",
          "Screen C" in badge and "4/8 basic" in badge, True)
    check("the meter carries the screen score", "--r:56" in badge, True)
    check("the old keyword wording is gone from a graded card", "Keywords" in badge, False)
    check("a grade-B card reads green",
          'data-b="low"' in dashboard.screen_badge(dict(sc, grade="B")), True)
    check("a grade-D card says why", "Gaps in the basics" in dashboard.screen_badge(dict(sc, grade="D")), True)
    check("a folder without screen.json keeps the keyword badge",
          "Keywords 82%" in dashboard.ats_badge(82, True, dashboard.screen_summary(_folder(brief_md="x"))), True)
    check("a malformed screen.json costs the grade, not the page",
          dashboard.screen_summary(_folder(screen_json='{"grade": "Z", "score": 1}')), None)
    check("the card shows it",
          "Screen C" in dashboard.card(a_card_row(has_folder=True, ats=56, screen=sc)), True)


def test_the_render_problem_column_says_what_it_means():
    # `needs_review` is a RENDER failure now -- the docx would not build, or the
    # Word page check said two pages -- not a request for a human opinion. The
    # status token is unchanged everywhere; only the word on the column moved.
    titles = {key: title for key, title, _s, _h in dashboard.BOARD_COLUMNS}
    check("the column is named for the failure", titles["review"], "Render problem")
    check("the status token is untouched",
          dashboard.COL_OF_STATUS["needs_review"], "review")


def test_every_live_folder_with_an_id_is_mapped():
    """The regression guard: written against the corpus, not a fixture.

    This is the assertion that was missing when the fence was mandatory. A
    folder the map cannot see fails silently -- it simply renders no links --
    so nothing but a count of the corpus itself catches it.
    """
    folders = dashboard.application_folders()
    mapped = {p.resolve() for p in folders.values()}
    has_id = re.compile(r"\*\*id:\*\*\s*`?([0-9a-f]{40})`?(?![0-9a-f])")
    briefs = [b for d in sorted((ROOT / "applications").glob("*"))
              for b in [dashboard.brief_file(d)] if b]
    # A re-tailored job legitimately has two folders with the same id (the
    # legacy one and the -v3 one); the map keeps one per id, so a folder whose
    # id IS mapped elsewhere is superseded, not missed.
    unmapped = []
    for b in briefs:
        m = has_id.search(b.read_text(encoding="utf-8", errors="replace"))
        if m and b.parent.resolve() not in mapped and m.group(1) not in folders:
            unmapped.append(b.parent.name)
    unmapped.sort()
    check(f"live folders with a 40-hex id that the map misses {unmapped}",
          unmapped, [])


def test_an_empty_funnel_renders_without_a_rate_or_a_crash():
    """The state the page ships in: applications exist, outcomes do not.

    Two things have to hold at once and they pull against each other. There must
    be no rate -- zero outcomes from applications nobody was tracking measures
    the absence of a RECORD, not the absence of an interview -- and there must be
    no silent omission either, so the panel has to say which of the two it is.
    A bare missing number is the hidden number this page keeps promising not to
    have (LEARNINGS 2026-08-20).
    """
    empty = dashboard.funnel_panel({}, 0)
    check("a wholly empty funnel does not divide by zero", "%" in empty, False)
    check("...and says so rather than rendering nothing",
          "no funnel to draw" in empty, True)

    backfilled = dashboard.funnel_panel(
        {"aggregator": {"applied": 16, "recorded": 0, "interview": 0,
                        "offer": 0, "rejected": 0}}, 27)
    check("a backfill-only channel prints no rate", "%" in backfilled, False)
    check("...and names why the rate is missing",
          "predate outcome tracking" in backfilled, True)
    check("...and states the tracked-vs-applied gap",
          "16 of the 27 rows" in backfilled, True)
    check("the counts themselves are always shown",
          "<b>16</b> applied" in backfilled, True)

    real = dashboard.funnel_panel(
        {"ats": {"applied": 12, "recorded": 12, "interview": 3,
                 "offer": 1, "rejected": 4}}, 12)
    check("a channel over the floor does print a rate",
          "25% interview rate" in real, True)

    check("the nudge renders its empty state rather than vanishing",
          "Nothing has gone quiet" in dashboard.nudge_panel([]), True)


def test_the_outcome_panels_reach_the_built_page():
    """Panel functions passing in isolation says nothing about the page.

    Same reason test_the_built_page_passes_the_ci_guard renders for real: a
    panel wired into the wrong branch of main() looks perfect from a unit test.
    """
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "index.html"
        subprocess.run([sys.executable, str(ROOT / "pipeline" / "dashboard.py"),
                        "--out", str(out)], check=True, capture_output=True)
        page = out.read_text(encoding="utf-8")
    check("the funnel panel is on the page", "Outcomes by channel" in page, True)
    check("the nudge panel is on the page", "No outcome yet" in page, True)


def main() -> int:
    print("card sort attributes")
    test_only_posting_evidence_gets_a_sort_number()

    print("detection clock: degraded threshold")
    test_the_degraded_amber_has_a_threshold()

    print("routine_text")
    test_routine_text_strips_markdown()
    test_single_underscores_are_left_alone()
    test_routine_text_defuses_every_ci_pattern()
    test_routine_text_reads_as_prose()
    test_routine_text_truncates_on_a_word_boundary()
    test_routine_text_survives_empty()

    print("id map")
    test_the_id_line_may_omit_backticks()
    test_every_live_folder_with_an_id_is_mapped()

    print("review contract")
    test_the_screen_badge_reads_screen_json()
    test_review_scores_reads_the_scores_line()
    test_review_scores_takes_the_last_line()
    test_a_missing_or_unreadable_line_is_absent_not_zero()
    test_an_old_folder_still_shows_its_verdict()
    test_the_review_readout_colours_on_the_weaker_human()
    test_the_review_readout_is_on_the_card_face()
    test_the_word_page_count_becomes_a_badge()
    test_notes_carry_the_scores_when_review_md_is_not_in_this_checkout()
    test_routine_text_strips_knowledge_citations()
    test_brief_verify_reads_every_inferred_shape()
    test_a_real_brief_citing_paths_passes_the_guard()
    test_the_ready_card_says_what_to_verify()
    test_the_ready_column_counts_what_the_window_archived()
    test_the_board_shows_only_the_last_72_hours()
    test_deadlines()
    test_a_stretch_role_says_so_on_its_card()
    test_the_ready_order_leads_on_the_weaker_human()
    test_the_keyword_badge_reads_the_review_percentage()
    test_the_render_problem_column_says_what_it_means()

    print("outcome panels")
    test_an_empty_funnel_renders_without_a_rate_or_a_crash()
    test_the_outcome_panels_reach_the_built_page()

    print("published page")
    test_the_built_page_passes_the_ci_guard()

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILURE(S): {', '.join(FAILS)}")
        return 1
    print("all dashboard publish-guard tests pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
