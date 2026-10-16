"""Regenerate dashboard/index.html from pipeline/jobs.sqlite.

Run after every Layer 2 pass (and by /digest, /mark). Self-contained HTML, no
external assets. The last Layer 1 run timestamp is shown prominently: a
silently dead cron looks exactly like a quiet job market, and the only way to
tell them apart is a stale clock.

Layer 2 contract (Phase 4): an application folder carries `brief.md` (older
ones `score.md`) and `review.md`, whose last `SCORES: ats NN / recruiter N /
hm N` line is the whole quality readout. The critic-convergence line, the
`ATS:`/`GAP:` ledger lines and the attestation queue are retired and no longer
read from anywhere on this page.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import db  # noqa: E402
# Stdlib-only at import time, so this stays cheap for the CI dashboard job.
from docx_engine import PASS1_SUFFIX  # noqa: E402

ROOT = HERE.parent
OUT = ROOT / "dashboard" / "index.html"

ASSETS = HERE / "dashboard_assets"

def _asset(name: str) -> str:
    return (ASSETS / name).read_text(encoding="utf-8")


# Backticks OPTIONAL, and that is the whole point of this pattern. The cloud
# tailor writes the id line by hand from its prompt and does not always fence
# it; requiring the fence made 8 of 57 folders unmappable, 6 of them written in
# a single run on 2026-08-30. An unmappable folder loses every link on this page
# (resume, review, screening draft), and it is also invisible to
# `sync.detect_partial_tailors` -- the net that catches a folder written by a
# run that died before its sidecar -- so the one case that net exists for is the
# one it could not see. The id is what identifies the row; the formatting around
# it never was. The trailing lookahead keeps a longer hex string from matching
# on its first 40 characters.
ID_LINE = re.compile(r"\*\*id:\*\*\s*`?([0-9a-f]{40})`?(?![0-9a-f])")

# The file that carries the id line. `brief.md` is the current name; `score.md`
# is what every folder written before Phase 4 has, and both are read forever --
# a folder whose brief cannot be found loses every link on this page.
BRIEF_NAMES = ("brief.md", "score.md")


def brief_file(folder: Path) -> Path | None:
    """The folder's brief, new name first."""
    for name in BRIEF_NAMES:
        p = folder / name
        if p.is_file():
            return p
    return None


def application_folders() -> dict[str, Path]:
    """Map job id -> application folder, via the id line the brief carries."""
    out = {}
    for folder in sorted((ROOT / "applications").glob("*")):
        f = brief_file(folder)
        if not f:
            continue
        m = ID_LINE.search(f.read_text(encoding="utf-8", errors="replace"))
        if m:
            out[m.group(1)] = folder
    return out


def deliverable_files(folder: Path) -> list[Path]:
    """The resume files this page is allowed to hand a human, PDFs first.

    `resume.pass1.docx` is excluded and that is not cosmetic. Links here are
    labelled by SUFFIX, so it would render as a second, indistinguishable
    "docx" beside the shipped one -- and it is the JD-blind pass-1 draft: the
    unconverged, un-keyword-fitted, never-critiqued version. One wrong click
    uploads it to an employer. It is an audit artifact for calibration, not a
    deliverable, and this page's whole purpose is "grab the file and submit it".
    """
    return [f for f in sorted(folder.glob("*.pdf")) + sorted(folder.glob("*.docx"))
            if not f.name.endswith(PASS1_SUFFIX)]


# The judge's exit line, written at column zero with no markup. Last match
# wins: a re-judged folder appends, and only the newest line describes the
# artifact that is actually on disk.
SCORES_LINE = re.compile(
    r"^SCORES:\s*ats\s+(\d+)\s*/\s*recruiter\s+(\d+)\s*/\s*hm\s+(\d+)", re.M)


def review_scores(folder: Path | None) -> dict | None:
    """{"ats", "recruiter", "hm"} from the LAST `SCORES:` line in review.md.

    None when there is no folder, no review, or no parsable line -- absent,
    never zero. A zero here would read as a failed resume rather than as a
    review that has not run.
    """
    if not folder:
        return None
    review = folder / "review.md"
    if not review.is_file():
        return None
    m = SCORES_LINE.findall(review.read_text(encoding="utf-8", errors="replace"))
    if not m:
        return None
    ats, recruiter, hm = m[-1]
    return {"ats": int(ats), "recruiter": int(recruiter), "hm": int(hm)}


def screen_summary(folder: Path | None) -> dict | None:
    """The requirement screen's summary (`screen.json`, written by
    requirements.py grade), or None for a folder reviewed before it existed.

    Only the fields the badge prints are kept, and each is shape-checked: the
    file comes from a cloud run, and a malformed one must cost the badge its
    grade, never the page its build.
    """
    if not folder:
        return None
    path = folder / "screen.json"
    if not path.is_file():
        return None
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        grade, score = d["grade"], int(d["score"])
        b, p = d["basic"], d["preferred"]
        if grade not in ("A", "B", "C", "D"):
            return None
        return {"grade": grade, "score": score,
                "basic_met": int(b["met"]), "basic_total": int(b["total"]),
                "pref_met": int(p["met"]), "pref_total": int(p["total"])}
    except Exception:
        return None


def review_verdict(folder: Path | None) -> str:
    """The pre-Phase-4 `VERDICT:` line. Fallback only, for folders with no SCORES."""
    if not folder:
        return ""
    review = folder / "review.md"
    if not review.exists():
        return "no review"
    text = review.read_text(encoding="utf-8")
    m = re.findall(r"VERDICT:\s*(PASS|FAIL)", text)
    return m[-1] if m else "malformed"


# W6 (2026-10-11): what Vaish must check before he sends a resume, lifted from
# the brief so he does not open it per job. SKILL.md asks the tailor to note
# "anything you inferred beyond the written record" in free prose, so this reads
# the shapes measured over all 225 brief lines that mention it (since 10-13 the
# skill asks for one fixed line, "Inferred beyond the written record: a | b" or
# "none"; the older shapes stay readable for briefs already on the board): a labelled line
# ("Inferred beyond the written record:" 95, "Inferred:" 65, "... the record:"
# 39, "Inferences beyond the record:" 5, a bold "**Inferred beyond the written
# record.**"), a parenthetical inside a revision bullet ("SQL named on the
# data-quality bullet (inferred: query work)"), and negations ("Nothing inferred
# beyond the record"). Half the briefs on the board never mention it at all,
# which is NOT the same as "none".
VERIFY_SECTIONS = ("tailoring notes", "revision notes")
ASK_SECTION = "would raise the score if true"
_INFER_NONE = re.compile(r"\b(?:nothing\s+(?:was\s+)?inferred|inferred\s+nothing|"
                         r"no\s+inference)\b", re.I)
_INFER_LABEL = re.compile(r"\*{0,2}\bInfer(?:red|ences?)\b([^:.()]{0,40})[:.]\*{0,2}\s*(.*)",
                          re.S)
# "inference" alone is mostly the ML word ("$0 inference"), so only its
# claim-marking uses count: "(inference from the specs)", "(role inference)",
# "an inference from", "near-certain inference".
_INFER_MARK = re.compile(r"\binferred\b|\(inference\b|\binference\)|"
                         r"\b(?:an|role|near-certain)\s+inference\b", re.I)
# A labelled line that runs on into the scores sentence, in older paragraph briefs.
_INFER_TAIL = re.compile(r"(?<=[.;])\s+(?:Scores|Final|Round)\b.*$", re.S)
_STRIKE_BOILERPLATE = re.compile(
    r"\s*(?:Vaish (?:should|to) strike[^.]*|Strike (?:if not true|what is not true|"
    r"any he cannot defend)[^.]*)\.?\s*$", re.I)
_ITEM_START = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+")


def brief_sections(text: str) -> dict[str, list[str]]:
    """`## Heading` -> its items: bullets and paragraphs, wrapped lines joined."""
    out: dict[str, list[str]] = {}
    items: list[str] | None = None
    cur: list[str] = []

    def flush():
        if items is not None and cur:
            items.append(" ".join(s.strip() for s in cur))
        cur.clear()

    for line in text.splitlines():
        if line.startswith("## "):
            flush()
            items = out.setdefault(line[3:].strip().lower(), [])
        elif not line.strip():
            flush()
        elif _ITEM_START.match(line):
            flush()
            cur.append(_ITEM_START.sub("", line))
        else:
            cur.append(line)
    flush()
    return out


def inferred_claims(items: list[str]) -> list[str] | None:
    """The claims to verify; [] when the brief says none; None when it never says."""
    claims: list[str] = []
    mentioned = False
    for item in items:
        m = _INFER_LABEL.search(item)
        if not (m or _INFER_MARK.search(item) or _INFER_NONE.search(item)):
            continue
        mentioned = True
        if _INFER_NONE.search(item):
            continue
        if m:
            text = _INFER_TAIL.sub("", m.group(2)).strip()
            text = _STRIKE_BOILERPLATE.sub("", text).strip()
            if text and not re.match(r"none\b", text, re.I):
                # SKILL.md's fixed line (10-13) separates claims with " | ".
                claims.extend(c.strip() for c in text.split(" | ") if c.strip())
        else:
            # A parenthetical mark inside a revision bullet: the bullet IS the
            # claim, and it is one the labelled line usually leaves out.
            claims.append(item.strip())
    if not mentioned:
        return None
    seen: set[str] = set()
    return [c for c in claims if not (c in seen or seen.add(c))]


def brief_verify(folder: Path | None) -> dict | None:
    """{"inferred": [...] | [] | None, "ask": str | "" | None} from the brief.

    `ask` is the first "would raise the score if true" line: by the skill's
    contract, the reviewer asks nobody has put to Vaish yet. "" is the brief's
    own "none open"; None is a brief with no such section.
    """
    f = brief_file(folder) if folder else None
    if not f:
        return None
    secs = brief_sections(f.read_text(encoding="utf-8", errors="replace"))
    items = [i for name in VERIFY_SECTIONS for i in secs.get(name, [])]
    ask_items = secs.get(ASK_SECTION)
    if not ask_items:
        ask = None
    elif re.match(r"\W*none\b", ask_items[0], re.I):
        ask = ""
    else:
        ask = ask_items[0]
    return {"inferred": inferred_claims(items), "ask": ask}


# What sync.py records in the DB `notes` when the local Word page check runs.
# The page count is the whole point of that check: a two-page resume is the one
# defect a reader cannot see from the PDF link alone.
WORD_PAGES_LINE = re.compile(r"\bword_pages=(\d+)")
# The judge's three numbers as sync.py folds them into `notes`. Used only when
# review.md is absent from this checkout (the cloud wrote it on its own branch).
NOTES_SCORES_LINE = re.compile(r"\bscores=(\d+)/(\d+)/(\d+)")


def word_pages(notes: str) -> int | None:
    m = WORD_PAGES_LINE.search(notes or "")
    return int(m.group(1)) if m else None


def notes_scores(notes: str) -> dict | None:
    m = NOTES_SCORES_LINE.search(notes or "")
    if not m:
        return None
    return {"ats": int(m.group(1)), "recruiter": int(m.group(2)),
            "hm": int(m.group(3))}


def ats_bucket(pct: int) -> tuple[str, str]:
    """(css key, plain word) for the keyword percentage the judge reported.

    Re-keyed 2026-09-16: the number is a COVERAGE PERCENTAGE now, not the old
    count of terms still addable, so the scale runs the other way -- high is
    good. The css keys are the three colour slots (green / neutral / amber) and
    keep their old spelling so board.css and tokens.css are untouched.

    The middle band gets NO word, exactly as before: most resumes land there,
    and a middling adjective on two thirds of the board grades nothing.
    """
    if pct >= 80:
        return ("low", "Well covered")
    if pct >= 60:
        return ("mid", "")
    return ("high", "Worth another pass")


# The one-line meter: a percentage against 100, which is a METER and not a
# chart (a two-slice pie of "covered / not covered" would be the same number
# told worse). Five discrete cells rather than a smooth bar because the honest
# precision here is coarse -- a reviewer's coverage figure moves several points
# between runs on the same resume -- and a continuous fill invites reading a
# 3-point difference that does not exist. The exact number is printed beside it
# for anyone who wants one.
#
# The fill carries the BUCKET's colour (ats_bucket), so colour answers "is this
# resume speaking the posting's language", which is the thing another tailor
# pass can move. The cells are geometry, drawn in board.css from one custom
# property, and cost no per-card markup beyond the number itself.
#
# The badge is the ONE place a card carries a second colour family. Freshness
# stays a CATEGORICAL ramp across hues; coverage is a three-step SEMANTIC ramp
# -- green "well covered", neutral teal for the unjudged middle, amber "worth
# another pass". Amber means the same thing it means everywhere else on this
# page (there is something to do), so the two families still cannot be read as
# each other.
#
# Two structural decisions in board.css, recorded here because this is the file
# that ships no bytes to the reader:
#   - The cell dividers are an OVERLAY (a repeating gradient over the whole
#     track) rather than borders on five child boxes. Inside the box model each
#     divider would eat 2px of its own cell, compressing the fill's scale.
#   - The track carries its own 1px --rule-strong outline and 2px dividers
#     because the FILL cannot be relied on to read: measured against --sunk,
#     --ats-low is 5.44:1 light / 7.24:1 dark and --ats-high 4.85 / 7.10, but
#     --ats-mid is 2.80 / 3.46 -- under the 3:1 a graphical object wants.
#     Structure is what makes the meter legible at that middle step; the number
#     beside it is what actually carries the reading. The weak step is also the
#     one band that prints NO word, so it is the number and the geometry that
#     have to hold there -- the reason the outline is not optional.
def ats_meter(pct: int) -> str:
    return f'<span class="atsm" style="--r:{min(pct, 100)}"></span>'


# The grade's colour slots reuse the coverage badge's three: green when every
# basic qualification is met (A, B), the neutral middle for C, amber for D.
GRADE_BUCKET = {"A": ("low", "Meets the basics"), "B": ("low", "Meets the basics"),
                "C": ("mid", ""), "D": ("high", "Gaps in the basics")}


def screen_badge(screen: dict) -> str:
    """The requirement screen's readout: the tier first, then what it counts.

    Words before numbers, as the coverage badge does: "Screen B" is the tier an
    AI screener would show a recruiter, "7/9 basic" the evidence behind it. The
    meter carries the weighted score (basic items count double).
    """
    key, word = GRADE_BUCKET[screen["grade"]]
    tip = html.escape(
        f"Graded the way an AI screener reads it: {screen['basic_met']} of "
        f"{screen['basic_total']} basic and {screen['pref_met']} of {screen['pref_total']}"
        f" preferred qualifications shown by a line on the page. Score {screen['score']}.")
    return (f'<div class="ats" data-b="{key}" aria-describedby="ats-help"'
            f' title="{tip}"><b>Screen {screen["grade"]}</b>'
            f' {screen["basic_met"]}/{screen["basic_total"]} basic{ats_meter(screen["score"])}'
            f'<span class="atst">{word}</span></div>')


def ats_badge(pct: int | None, has_folder: bool, screen: dict | None = None) -> str:
    """The card's keyword-coverage readout, in words before numbers.

    The number is `ats` from review.md's SCORES line -- the reviewer that read
    the resume is the one that reports it now.

    Three states, and the distinction that matters is between the last two: a
    folder with no SCORES line has NEVER BEEN MEASURED, which is not the same
    claim as measuring zero. The page's recurring failure mode is a readout
    that looks like an answer while the thing it measures never ran, so the
    unmeasured state says so in words rather than rendering an empty meter that
    reads as a catastrophic score.

    No folder at all gets NOTHING. An untailored role has no resume to score,
    and "not measured" on 200 cards that were never candidates for measurement
    would drown the handful where the word means something.
    """
    if not has_folder:
        return ""
    if screen:
        return screen_badge(screen)
    if pct is None:
        return ('<div class="ats none" aria-describedby="ats-help">'
                'Not measured yet'
                ' <a class="atsq" href="#ats-help"'
                ' aria-label="What the keyword coverage badge means">'
                '&#9432;</a></div>')
    key, word = ats_bucket(pct)
    tip = html.escape(
        f"This resume uses {pct}% of this posting's own vocabulary, as scored"
        " by the reviewer that read it.")
    return (f'<div class="ats" data-b="{key}" aria-describedby="ats-help"'
            f' title="{tip}"><b>Keywords {pct}%</b>{ats_meter(pct)}'
            f'<span class="atst">{word}</span></div>')


# The badge's own footnote, and the target of every badge's aria-describedby.
# It lives in the controls panel rather than on a card because it explains a
# thing that appears on every tailored card, and a tooltip cannot hold it.
#
# Written in the reader's vocabulary: "what the review said about keywords",
# not the scorer's internal words for it.
ATS_HELP = """<details id="ats-help" class="explainer">
<summary>What "Screen B &middot; 7/9 basic" on a card means</summary>
<div class="expbody">
<p><b>The grade</b> is how an AI resume screener (Workday HiredScore, Greenhouse,
Ashby, LinkedIn's Hiring Assistant) would tier this resume. The posting's
qualifications are split into basic and preferred, and each one counts only
when a line on the page shows the work; a skill that is only listed on the
Skills line counts as partial. <b>A</b>: every basic and most preferred met.
<b>B</b>: every basic met. <b>C</b>: most basic met. <b>D</b>: fewer.</p>
<p><b>"7/9 basic"</b> is how many basic qualifications the page shows. The
meter is the weighted score (basic items count double, partial counts half).
The review file lists every qualification with the quote that met it and what
would close each gap.</p>
<p><b>"Keywords 82%"</b> is the older measure on resumes reviewed before
2026-10-14: the share of the posting's vocabulary a reviewer found, with
synonyms counted. It ran high and is not comparable to the grade.</p>
<p><b>"Not measured yet"</b> means no review has scored that folder. Nothing
here gates anything; it tells you which resumes need another look.</p>
</div>
</details>"""

SYNC_STATE = ROOT / "pipeline" / "sync_state.json"


def sync_state() -> dict:
    """How the last local sync ended, from its gitignored sidecar.

    Deliberately NOT in jobs.sqlite: recording a failure there would dirty a
    committed binary that origin/main also changes hourly, so the next run's
    --autostash would stash a modified binary and conflict on reapply -- the
    exact failure the lock gate exists to prevent. A file locked on Vaish's
    laptop is also not repo state, so the CI build correctly never sees this and
    renders no banner; the published page keeps the 14h staleness signal.
    """
    try:
        return json.loads(SYNC_STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def commits_behind() -> int:
    """How many commits origin/main is ahead of HEAD, 0 if unknown.

    Uses the already-fetched remote ref (no network): if it says we are behind,
    that is proof; if it says 0, the ref may simply be stale, so this is only
    ever used to ADD a warning, never to suppress one.
    """
    try:
        out = subprocess.run(["git", "rev-list", "--count", "HEAD..origin/main"],
                             cwd=ROOT, capture_output=True, text=True, timeout=10)
        return int(out.stdout.strip()) if out.returncode == 0 else 0
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


# The cron fires hourly but GitHub delivers it 16-56 min late and drops roughly
# one fire a day (measured over 60 runs, 2026-08-18), so ~2h between runs is
# normal and 3h is not. The old 6h threshold was sized for the 3h cron and never
# retuned: it stayed green through six consecutive missed hourly fires.
STALE_AFTER_HOURS = 3
# Layer 2 runs six times a day from Task Scheduler (times derived by
# pipeline/schedule.py; see docs/schedule.md -- largest gap 8h), so a
# sync eight hours old is routine. Only a missed slot is worth flagging.
SYNC_STALE_AFTER_HOURS = 14

# How many sources have to fail in ONE run before the detection clock calls it
# degraded. The `errors=` signal was designed when Layer 1 polled ~10 sources
# and one of them going quiet was a real event. It polls 69 boards hourly now,
# so a single transient 5xx anywhere paints the clock amber -- and since the
# detection clock has no amber band of its own (ruling R9 collapsed it), this
# IS the entire degraded-source signal. An alarm that is on permanently is as
# informative as one that is never on, which is the same reasoning that put
# acknowledged_degraded in scrape.py; that list already covers the source which
# is dead every hour, so what is left for this threshold to judge is churn.
#
# Two out of 69 is still a low bar on purpose: it takes one unlucky hour to be
# wrong about, and the whole page is rebuilt hourly, so the cost of a false
# amber is one hour of mild noise while the cost of a false green is a detection
# layer quietly running on fewer sources than it thinks. The 3h RED dead-cron
# alarm is untouched by any of this and still fires on the timestamp alone.
DEGRADED_MIN_SOURCES = 2


def degraded_sources(summary: str) -> list[str]:
    """The sources a Layer 1 run reported as NEWLY degraded, from its summary.

    scrape.main() writes `errors=a,b,c` and keeps acknowledged-dead sources in a
    separate `known_degraded=` field precisely so this one stays meaningful.
    """
    m = re.search(r"errors=(\S+)", summary or "")
    return [s for s in m.group(1).split(",") if s] if m else []


def is_degraded(summary: str) -> bool:
    """Enough sources failed in one run to call detection degraded, not churn."""
    return len(degraded_sources(summary)) >= DEGRADED_MIN_SOURCES


def staleness(ts: str, ok_hours: float = STALE_AFTER_HOURS,
              red_hours: float | None = None) -> tuple[str, str]:
    """(human age, level). `ok_hours` is the cadence of whatever wrote it.

    THREE levels on ONE scale: green up to the cadence, amber up to `red_hours`,
    red past that. `red_hours` defaults to `2 * ok_hours`, which is what gives
    the sync clock its red at `2 * SYNC_STALE_AFTER_HOURS` without a second
    hand-tuned constant.

    The detection clock deliberately passes `red_hours=STALE_AFTER_HOURS` and so
    has NO amber band: green below 3h, red at 3h, exactly as before this
    parameter existed (ruling R9). Its twitchiness is the point. CLAUDE.md sizes
    that 3h against an hourly cron -- three missed fires -- and a dead-cron
    detector is the one alarm on this page whose whole value is going off early;
    an amber step there would soften the signal the clock exists to give. The
    parameter is still the right shape: the two clocks now differ in ONE number
    rather than in the code path they run through.
    """
    try:
        then = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return ("unparseable", "bad")
    hours = (datetime.now(timezone.utc) - then).total_seconds() / 3600
    label = f"{hours:.1f}h ago" if hours < 48 else f"{hours / 24:.1f}d ago"
    if hours <= ok_hours:
        return (label, "ok")
    return (label, "warn" if hours <= (2 * ok_hours if red_hours is None
                                       else red_hours) else "stale")


# Severity order for the health strip. "stale" and "bad" are both red: one is a
# clock that stopped, the other a timestamp that never parsed, and neither is a
# state you keep working through.
CLOCK_RANK = {"ok": 0, "warn": 1, "stale": 2, "bad": 2}


def worse(a: str, b: str) -> str:
    """The louder of two levels. A clock is as healthy as its unhealthiest input."""
    return a if CLOCK_RANK[a] >= CLOCK_RANK[b] else b


def health_clock(label: str, layer: str, level: str, age: str,
                 note: str, detail: str) -> str:
    """One entry in the health strip: a dot, a name, an age, and the whole story.

    The dot carries the state and the whole line is the <summary>, so each clock
    is exactly one tab stop and the disclosure is native. Nothing that used to be
    on the page is gone -- every string the old banners printed is inside the
    <details> verbatim; only the shouting was demoted.

    `note` is a short always-visible phrase, and it exists because colour alone
    is not a signal: a reader who cannot tell the amber dot from the green one
    still reads "the cron may be dead". It is a summary of the detail, never a
    replacement for it.
    """
    cls = level if level in ("ok", "warn") else "bad"
    return (f'<div class="hc {cls}"><details class="hd">'
            f'<summary><span class="hdot"></span>'
            f'<b>{html.escape(label)}</b>'
            f'<span class="hlayer">{html.escape(layer)}</span>'
            f'<span class="hsep">&middot;</span>'
            f'<span class="hage">{html.escape(age)}</span>'
            + (f'<span class="hnote">{note}</span>' if note else "")
            + '<span class="hmore">details</span></summary>'
            f'<div class="hbody">{detail}</div></details></div>')


GITHUB_OWNER = "your-github-user"
GITHUB_NAME = "your-private-repo"
GITHUB_REPO = f"https://github.com/{GITHUB_OWNER}/{GITHUB_NAME}"
GITHUB_BLOB = f"{GITHUB_REPO}/blob/main"
# Where the published page sends a click. The local build links here instead of
# rendering buttons of its own (see the controls block in main).
LIVE_URL = "https://your-dashboard.vercel.app"
# One cloud branch family since Phase 4: the judge routine writes scores,
# applications and tailored sidecars to `claude/judge` or to the run-suffixed
# `claude/judge-<run>` the cloud designates, and nothing else publishes.
OVERLAY_REF_PREFIX = "origin/claude/judge"


def overlay_refs() -> tuple[str, ...]:
    """Every fetched `origin/claude/judge*` ref, so a just-pushed run's folder
    can render its links before the laptop has folded it."""
    try:
        proc = subprocess.run(["git", "for-each-ref", "--format=%(refname:short)",
                               "--sort=committerdate", f"refs/remotes/{OVERLAY_REF_PREFIX}*"],
                              cwd=ROOT, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ()
    return tuple(r.strip() for r in (proc.stdout or "").splitlines() if r.strip())
# Appears in the <title> and the <h1>. vercel_guard.py fetches the published
# URLs with no credentials and fails the build if it finds this string, so it
# must stay on the page and stay distinctive.
PUBLIC_SENTINEL = "Job Search OS"
_blob_cache: dict[str, str] = {}


def _tree_has(ref: str, path: str) -> bool:
    try:
        return subprocess.run(["git", "cat-file", "-e", f"{ref}:{path}"], cwd=ROOT,
                              capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def blob_base(rel: str) -> str:
    """Blob URL for a folder, pinned to a ref that actually contains it.

    CI overlays `applications/` from the cloud branch so a just-tailored folder
    can render its links at all. Those files are then in the working tree but
    NOT in main's commit, so a `/blob/main/` link 404s on precisely the build
    the overlay exists to improve. Fall back to the branch's commit SHA, which is
    unambiguous — a branch name containing a slash is not, in a blob path.
    """
    if rel in _blob_cache:
        return _blob_cache[rel]
    # Either brief name proves the folder is in that tree.
    probes = [f"{rel}/{n}" for n in BRIEF_NAMES]
    base = f"{GITHUB_BLOB}/{rel}"
    if not any(_tree_has("HEAD", p) for p in probes):
        for ref in overlay_refs():
            sha = subprocess.run(["git", "rev-parse", ref], cwd=ROOT,
                                 capture_output=True, text=True)
            sha = (sha.stdout or "").strip() if sha.returncode == 0 else ""
            if sha and any(_tree_has(sha, p) for p in probes):
                base = f"{GITHUB_REPO}/blob/{sha}/{rel}"
                break
    _blob_cache[rel] = base
    return base


# Free text written by the cloud routine, on its way to the published page.
#
# Two different jobs, both about text this repo did not author:
#
#   1. The routine writes markdown; this page emits HTML, and html.escape does
#      not touch markup it does not understand, so the asterisks and backticks
#      rendered literally.
#   2. dashboard.yml greps the built page for `master-doc` and `PRIVATE_PROFILE` before
#      publishing. That tripwire is aimed at a CODE change that embeds one of
#      those files -- a new panel, a link, a source comment -- and it is worth
#      keeping. Routine-authored prose merely NAMING a file is not that, and on
#      2026-08-23 it froze the published dashboard for a day. The reference is
#      spelled out as prose here instead, which is also what the reader wants:
#      a file path in a teaser tells them nothing.
_MD_INLINE = re.compile(r"\*\*|__|[*`]")
_PRIVATE_REFS = (
    (re.compile(r"`?PRIVATE[_ ]PROFILE[\w.]*`?", re.I), "private profile"),
    (re.compile(r"`?master[-_ ]doc(\.md)?`?", re.I), "master doc"),
    (re.compile(r"\+1[0-9-]{9,}"), ""),
    # W6 (2026-10-11): brief.md prose reaches the cards, and the tailor cites its
    # sources by path ("profile/knowledge/20-citi-product-owner.md") or by bare
    # knowledge-file name ("(20-citi-product-owner.md, Facts to keep straight)").
    # The first trips the publish guard; the second tells the reader nothing.
    # "the record" keeps the sentence whole: "per the record", "(the record says
    # SQL was used", "(the record, Facts to keep straight)".
    (re.compile(r"`?profile/[\w./-]*\w`?", re.I), "the record"),
    (re.compile(r"`?\b\d\d-[a-z0-9-]+\.md\b`?", re.I), "the record"),
)
# Tidied after the substitutions: a list of files becomes one "the record",
# and a stripped phone number leaves no "()".
_CITE_DEBRIS = (
    (re.compile(r"\bthe record(?:\s*(?:,|;|and)\s*the record)+"), "the record"),
    (re.compile(r"\(\s*\)"), ""),
)


def routine_text(raw: str, limit: int | None = None) -> str:
    """Normalize Routine-authored free text for display. See the note above.

    Normalizing BEFORE truncating is load-bearing: `[:110]` applied first spends
    the char budget on `**` and backticks the reader never sees.
    """
    out = _MD_INLINE.sub("", raw or "")
    for pat, repl in _PRIVATE_REFS:
        out = pat.sub(repl, out)
    for pat, repl in _CITE_DEBRIS:
        out = pat.sub(repl, out)
    out = " ".join(out.split())
    if limit is not None and len(out) > limit:
        # Cut on a word boundary; the old truncation ended entries mid-word
        # ("...does the"), which reads as a rendering bug rather than a teaser.
        out = out[:limit].rsplit(" ", 1)[0].rstrip(",;:.") + "…"
    return out


def pending_sidecars(conn) -> dict[str, str]:
    """Cloud work that has not reached this DB yet, as job-id -> badge text.

    The deployed dashboard is built from whatever is on main, but the judge
    routine publishes to its own branch, so without this a job scored in the
    cloud three hours ago reads as untouched until Vaish's laptop syncs.
    Read-only and advisory: the DB stays the source of truth for status.
    """
    out: dict[str, str] = {}
    statuses = {r[0]: r[1] for r in conn.execute("SELECT id, status FROM jobs")}
    for path in sorted((ROOT / "scores").glob("*.json")):
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if statuses.get(rec.get("id")) == "new":
            note = routine_text((rec.get("note") or "").split(";")[0])
            out[rec["id"]] = f"cloud scored {rec.get('score')} ({note})"
    for path in sorted((ROOT / "tailored").glob("*.json")):
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        jid = rec.get("id")
        if jid and statuses.get(jid) in ("new", "scored"):
            # The sidecar carries the judge's three numbers now. Tolerate a
            # sidecar written without them rather than printing "?" -- an older
            # or partial run is not a scoring failure.
            sc = rec.get("scores")
            if isinstance(sc, dict) and all(
                    isinstance(sc.get(k), int) for k in ("ats", "recruiter", "hm")):
                what = (f"ats {sc['ats']} / recruiter {sc['recruiter']}"
                        f" / hm {sc['hm']}")
            else:
                what = "scores not reported"
            out[jid] = (f"cloud tailored, {what}"
                        " (awaiting local Word page check)")
    return out


DECISIONS_PATH = ROOT / "decisions" / "pending.json"
DECISIONS_REL = DECISIONS_PATH.relative_to(ROOT).as_posix()
DECISIONS_BRANCH = "main"
# The closed vocabulary decisions.py folds, minus `cancel`: that one is the
# browser's own undo, and an entry that reached the file anyway means "this row
# is NOT pending". Rendering it as pending would be exactly backwards.
DECISION_ACTIONS = ("applied", "rejected", "skipped", "interview", "offer", "promote")
JOB_ID_RE = re.compile(r"[0-9a-f]{40}")


def pending_decisions() -> dict[str, str]:
    """Decisions recorded on the page that sync.py has not folded yet: id -> action.

    Advisory display, exactly like pending_sidecars: a missing or malformed file
    renders nothing rather than failing a build. pipeline/decisions.py is the
    reader that acts on this file, and it is the one that reports junk in it --
    duplicating that here would put a second, quieter opinion on the page.

    Latest `at` wins per id, which is the fold's own rule (decisions._read). File
    order would disagree with it the moment two tabs raced, and then the badge
    would name a different action than the one that lands.
    """
    try:
        entries = json.loads(DECISIONS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(entries, list):
        return {}
    out: dict[str, str] = {}
    when: dict[str, str] = {}
    for rec in entries:
        if not isinstance(rec, dict):
            continue
        jid, action, at = str(rec.get("id", "")), rec.get("action"), str(rec.get("at", ""))
        if not JOB_ID_RE.fullmatch(jid) or action not in DECISION_ACTIONS:
            continue
        if jid not in when or at >= when[jid]:
            out[jid], when[jid] = action, at
    return out


# --- freshness tiers -------------------------------------------------------
# The words for db.age_tier's four cuts, which freshness.py's histogram reads
# too. Order is the policy order from profile/preferences.md: "freshness tier
# first, then score. A fresh 8 beats a stale 10." Unknown ranks above stale
# because an unknown date is never stale (db.is_provably_stale), only
# undecidable -- and a row known only by when we first saw it lands in "nodate"
# unless that sighting alone proves it past the window.
TIER_LABEL = {
    "now": "apply now",
    "soon": "still worth it",
    "nodate": "no posting date",
    "stale": "past the window",
}
TIER_ORDER = {"now": 0, "soon": 1, "nodate": 2, "stale": 3}
# Layer 1 ingests nothing older than 72h, so that is the natural full width of
# the age track. Anything beyond clamps to the end.
AGE_SCALE_HOURS = 72.0


# db.age_tier owns the cuts. It sits beside db.job_age because it is the same
# interval knowledge, and freshness.py's histogram reads the identical function
# -- this page and that report used to state the rule separately, which is how
# two readings of "is this still worth applying to" drift apart. Re-exported
# under the old name so every caller and test here keeps its spelling.
age_tier = db.age_tier


def age_bar(best: float | None, worst: float | None, precision: str) -> str:
    """The age interval drawn to scale, uncertainty included.

    82% of the corpus carries a date-only posting date, which is a 24h-wide
    interval rather than a number (db.job_age returns both ends for exactly this
    reason). Printing one number would be a lie on most of the page, so the bar
    draws the solid part -- the age the posting is AT LEAST -- and then a hatched
    part covering how much older it might be. A row straddling the 48h tick is
    visibly undecided instead of silently sorted.
    """
    if precision == "unknown":
        return '<span class="bar bar-none"></span>'
    solid = min(best, AGE_SCALE_HOURS) / AGE_SCALE_HOURS * 100
    # A "seen" row has a real floor (when we first recorded it) and NO ceiling:
    # nothing about it says when the employer posted it. Hatching to the end of
    # the scale is the honest drawing of "at least this old, possibly any
    # older" -- and `worst` is None there, so this branch is also what keeps
    # min() from being handed one.
    if worst is None:
        upper = 100.0
    else:
        upper = min(worst, AGE_SCALE_HOURS) / AGE_SCALE_HOURS * 100
    # Two custom properties, drawn by ::before/::after in STYLE. Emitting two
    # inline-styled child elements instead cost ~75 bytes on every one of 800
    # rows, which is most of a hundred kilobytes for markup CSS can derive.
    return f'<span class="bar" style="--a:{solid:.0f}%;--b:{upper - solid:.0f}%"></span>'


# The scorer writes its lane as the first clause of `notes`. It encodes the
# title gate, which a raw score does not: a 9 with (a)<=1 is digest-stretch, not
# auto-tailor. Longest-first so "digest-stretch" is not eaten by "digest".
LANES = ("auto-tailor", "digest-stretch", "digest", "skip")


def lane_of(notes: str) -> str:
    head = (notes or "").split(";")[0].strip().lower()
    for lane in LANES:
        if head.startswith(lane):
            return lane
    return ""


# --- archiving ------------------------------------------------------------
# The page is a work surface, not a ledger. Rows past the apply window are ones
# the pipeline has already decided about (freshness.py swept them), so drawing
# them buries the ~400 that are still live: on 2026-08-20 that was 483 of 925
# rows, 52% of the page, and the ratio worsens daily because dead rows
# accumulate while live ones do not.
#
# Archiving is a VIEW decision and never a data one. Deleting a row would make
# the job re-detect and re-score forever ("seen jobs never resurface"), and it
# would buy nothing: a shed row costs ~160 bytes, so even 10k archived rows add
# ~1.6 MB. What actually makes the DB big is jd_text on the live rows, which
# db.shed_jd_text() already governs.
#
# The cut is Vaish's own 48h policy rather than a new threshold, which makes the
# shown set SELF-LIMITING: ~123 rows arrive a day and leave after two, so it
# settles around 400 instead of growing without bound.
#
# Exempt at any age: these are his actual pipeline, and a resume he is sitting on
# does not stop mattering because the posting aged out.
ARCHIVE_EXEMPT = ("tailored", "needs_review", "applied", "interview", "offer")
# Statuses that by construction only ever exist past the window or before
# scoring. They get no filter chip, because a chip that can only ever read 0 is
# worse than no chip: it looks like a search that found nothing.
ARCHIVE_ONLY_STATUSES = ("stale_skip", "baseline_skip")
# The ones that got away: expired while still qualifying. Surfaced deliberately
# -- 65 of these in 3 days says qualifying roles arrive faster than the tailor
# cap (4/run, 8/day) clears them, which a row count alone would never show.
GOT_AWAY_MIN_SCORE = 8
GOT_AWAY_MAX_HOURS = 96
# Rows closed out and not archived yet (`not is_archived()`, a tier test and
# not a clock -- an undated row never goes stale, so it can sit in the list
# indefinitely). `rejected` and `skipped` are the two statuses that reach the
# board's `shown` set and then stand in no column, so from the moment sync folded a mark the card simply left the page --
# and a mark with no visible aftermath is indistinguishable from a mark that
# never landed, which is the failure this whole page is written against.
#
# What actually keeps this list SHORT is the row loader's WHERE clause (the
# SELECT in main(), which takes `score IS NOT NULL OR status IN (...)` and lists
# neither of these two statuses): Layer 1's own off-target skips carry no score,
# so 4,743 of them today never enter `jobs` at all and can never reach here.
# Widening that WHERE to pick up unscored rows would flood this "your mark
# landed" surface with hard-filter noise and destroy the only thing it is for.
# The cap below is a second-order limit, not the first one: it exists because
# this is a confirmation surface rather than a second archive, so past ten rows
# the count says more than the eleventh row does.
CLOSED_STATUSES = ("rejected", "skipped")
CLOSED_CAP = 10


# Vaish 2026-10-08: the board shows only roles POSTED or FOUND in the last 72h,
# tailored resumes included. The exemption above still covers outcomes he is
# tracking (applied / interview / offer), which are not open jobs. Older rows
# move to the archive below; nothing is deleted and the DB is untouched.
SHOW_WINDOW_HOURS = 72
# The Ready column's count of tailored resumes past that window names the last
# week's share beside the all-time total (W6).
OLD_READY_RECENT_HOURS = 168
OUTCOME_STATUSES = ("applied", "interview", "offer")


def in_window(j: dict, hours: float = SHOW_WINDOW_HOURS) -> bool:
    """Posted or first found within `hours`. No date at all is not proof of age."""
    ages = [h for h in (j.get("posted_h"), j.get("seen_h")) if h is not None]
    return not ages or min(ages) <= hours


def old_ready_line(gone: list) -> str:
    """The Ready column's count of tailored resumes the 72h rule archived (W6).

    Hidden rows, never a hidden number -- and the last week's share leads,
    because the all-time total alone (315 on 10-11) reads as a recent backlog.
    Drawn at zero too.
    """
    old = [j for j in gone if j["status"] == "tailored"]
    week = sum(1 for j in old if min(
        (h for h in (j.get("posted_h"), j.get("seen_h")) if h is not None),
        default=1e9) <= OLD_READY_RECENT_HOURS)
    return (f'<b>{week}</b> more from the last {OLD_READY_RECENT_HOURS // 24} days'
            f' (<b>{len(old)}</b> in all) are older than {SHOW_WINDOW_HOURS}h,'
            f' in the archive and not shown.')


# Application deadlines (Vaish 2026-10-15). Programs close on a date, not on the
# 72h clock: one APM program closed while its resume sat tailored, and another
# program's resume was already archived because the posting was a week old. A deadline read from the JD keeps the card on the board until it
# passes and puts the date on the card. Only an explicit phrase counts:
# "deadline", "close date", "must be submitted by", "complete your application
# ... on", "apply by", "accepting applications until".
_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_DEADLINE_CUE = re.compile(
    r"(?i)(application deadline|deadline to apply|deadline is|close date|closing date"
    r"|expected to close on|applications? (?:will )?close|must be submitted by"
    r"|complete your application|apply by|applications? (?:are )?due"
    r"|accepting applications (?:until|through))")
_MONTH_DATE = re.compile(
    r"(?i)\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?"
    r"(?:,?\s+(20\d\d))?")
# No closing \b: scraped text runs the date into the next word ("10/02/2026Meet").
_NUM_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(20\d\d)(?!\d)")
DEADLINE_LOOKAHEAD = 90    # characters after the cue that may hold the date
DEADLINE_SOON_DAYS = 3


def deadline_of(text: str, today: date | None = None) -> date | None:
    """The first explicit application deadline in a JD, or None.

    A month-name date with no year takes the year that puts it nearest today.
    """
    today = today or datetime.now(timezone.utc).date()
    for cue in _DEADLINE_CUE.finditer(text or ""):
        window = text[cue.end():cue.end() + DEADLINE_LOOKAHEAD]
        found = []
        if m := _NUM_DATE.search(window):
            found.append((m.start(), int(m.group(3)), int(m.group(1)), int(m.group(2))))
        if m := _MONTH_DATE.search(window):
            month, day = _MONTHS[m.group(1).lower()], int(m.group(2))
            year = int(m.group(3)) if m.group(3) else min(
                (today.year - 1, today.year, today.year + 1),
                key=lambda y: abs((date(y, month, min(day, 28)) - today).days))
            found.append((m.start(), year, month, day))
        for _, year, month, day in sorted(found):
            try:
                return date(year, month, day)
            except ValueError:
                continue
    return None


def folder_deadline(folder: Path | None) -> date | None:
    """The deadline in the folder's jd.md (the live posting the tailor read)."""
    if folder is None or not (folder / "jd.md").is_file():
        return None
    return deadline_of((folder / "jd.md").read_text(encoding="utf-8", errors="replace"))


def tailored_hours(jid: str) -> float | None:
    """Hours since the job's tailored/ sidecar says its resume shipped."""
    side = ROOT / "tailored" / f"{jid}.json"
    if not side.is_file():
        return None
    try:
        at = db.parse_posted_at(json.loads(side.read_text(encoding="utf-8")).get("tailored_at", ""))
    except (ValueError, OSError):
        return None
    return None if at is None else (datetime.now(timezone.utc) - at).total_seconds() / 3600


def deadline_badge(j: dict, today: date | None = None) -> str:
    when = j.get("deadline")
    if not when:
        return ""
    today = today or datetime.now(timezone.utc).date()
    left = (when - today).days
    label = f"{when:%b} {when.day}"
    if left < 0:
        return f'<div class="deadline past">Deadline passed {label}</div>'
    soon = " soon" if left <= DEADLINE_SOON_DAYS else ""
    return f'<div class="deadline{soon}">Apply by {label}</div>'


def is_archived(j: dict) -> bool:
    if j["status"] in OUTCOME_STATUSES:
        return False
    # Never archived ahead of its own deadline, nor in the 72h after a resume
    # ships: a hand /tailor of an older posting (a big-tech RPM program, 10-15) must still
    # reach Ready instead of landing straight in the archive.
    if j.get("deadline") and j["deadline"] >= datetime.now(timezone.utc).date():
        return False
    if (j["status"] in ("tailored", "needs_review")
            and j.get("tailored_h") is not None and j["tailored_h"] <= SHOW_WINDOW_HOURS):
        return False
    return (j["tier"] == "stale" and j["status"] not in ARCHIVE_EXEMPT) or not in_window(j)


# The scorer's own lane boundaries (CLAUDE.md, Layer 2 step 1), so a filter reads
# in the same units the rubric decides in. Unscored rows get "" and are excluded
# by every band chip rather than silently counting as zero.
def score_band(score: int | None) -> str:
    if score is None:
        return ""
    if score >= 8:
        return "8+"
    if score == 7:
        return "7"
    return "5-6" if score >= 5 else "4-"


# 97% of rows come from the same board, so a badge on every one of them carries
# no information and costs ~28 KB of page. Show it only when the row came from a
# direct ATS poll -- the fast path, and the one worth noticing.
BULK_SOURCE = "jobspy:indeed"


def source_badge(source: str) -> str:
    if not source or source == BULK_SOURCE:
        return ""
    return f'<div class="src">{html.escape(source.split(":")[-1])}</div>'


def review_floor(scores: dict | None) -> int:
    """min(recruiter, hm), or -1 when unreviewed. The number that gates a send.

    The two simulated humans are the gate; `ats` is vocabulary, not judgement,
    so the weaker of the two people is what decides whether a resume goes out.
    """
    return -1 if not scores else min(scores["recruiter"], scores["hm"])


def review_html(scores: dict | None, verdict: str, href: str) -> str:
    """The review readout: three numbers, colour-keyed on review_floor().

    Old folders (a `VERDICT:` line and no `SCORES:`) keep their legacy text --
    a blank there would read as "never reviewed", which is the one thing it is
    not.

    `href` is empty when review.md is not in THIS checkout, which is the normal
    state of a row whose numbers came from `notes` while the cloud branch still
    holds the file. The numbers render unlinked rather than pointing at a path
    that would 404.
    """
    if not scores:
        if not verdict:
            return ""
        vcls = {"PASS": "pass", "FAIL": "fail"}.get(verdict, "")
        # "critic:" names the thing that wrote a PASS/FAIL. "no review" and
        # "malformed" are this page's own words and must not wear that byline.
        word = f"critic: {verdict}" if vcls else verdict
        return f'<div class="verdict {vcls}">{html.escape(word)}</div>'
    low = review_floor(scores)
    cls = "pass" if low >= 8 else "warn" if low >= 6 else "fail"
    text = (f'ATS {scores["ats"]} &middot; R {scores["recruiter"]}'
            f' &middot; HM {scores["hm"]}')
    if href:
        text = f'<a href="{html.escape(href)}">{text}</a>'
    return f'<div class="verdict {cls}">{text}</div>'


def verify_html(v: dict) -> str:
    """W6: what to check before sending, open on the card, never folded away.

    Three states per line, and "the brief never says" is drawn differently from
    "the brief says none": half the briefs on the board predate a fixed line,
    and a card reading "none inferred" for them would clear claims nobody listed.
    Untruncated on purpose -- a cut verify list hides a claim.
    """
    claims = v.get("inferred")
    if claims:
        claims = [t for t in (routine_text(c) for c in claims) if t]
    if claims:
        body = "<ul>" + "".join(f"<li>{html.escape(c)}</li>" for c in claims) + "</ul>"
        vcls = ""
    elif claims == []:
        body, vcls = " none inferred", " none"
    else:
        body, vcls = " not stated in brief, open it", " unstated"
    ask = v.get("ask")
    if ask:
        abody, acls = " " + html.escape(routine_text(ask)), ""
    elif ask == "":
        abody, acls = " none open", " none"
    else:
        abody, acls = " not stated in brief", " unstated"
    return (f'<div class="verify">'
            f'<div class="vf{vcls}"><b>Verify:</b>{body}</div>'
            f'<div class="vf{acls}"><b>Open ask:</b>{abody}</div></div>')


def word_badge(pages: int | None) -> str:
    """`Word: N pages` -- the local render check's own count.

    Two pages is the defect a reader cannot see from a PDF link, so the number
    is on the card rather than only in the notes prose.
    """
    if pages is None:
        return ""
    return f'<div class="wpg">Word: {pages} page{"" if pages == 1 else "s"}</div>'


# Job URLs are scraped from boards and are therefore untrusted input. Once this
# page can hold a write token in its own origin, a `javascript:` href on a role
# link is a token-theft vector rather than a broken link, so the scheme is
# ALLOWLISTED rather than blacklisted: tab/newline/control noise is stripped the
# way a browser strips it, and what is left must start with http:// or https://.
_URL_NOISE = re.compile(r"[\x00-\x20]")
_URL_OK = re.compile(r"https?://", re.I)


def safe_url(url: str | None) -> str:
    """The URL if it is safe to put in an href, else "" (render plain text)."""
    u = _URL_NOISE.sub("", url or "")
    return u if _URL_OK.match(u) else ""


def role_link(j: dict) -> str:
    """The role title, linked only when the posting URL passes safe_url."""
    u = safe_url(j["url"])
    title = html.escape(j["title"])
    return f'<a href="{html.escape(u)}">{title}</a>' if u else title


# Which decisions each status offers, and the word on the button. A closed
# vocabulary on purpose: the client ships one of these tokens plus the row's own
# data-id, and never anything it read out of the page.
ROW_ACTIONS = {
    "scored": (("promote", "Tailor"), ("skipped", "Skip")),
    "tailored": (("applied", "Applied"), ("rejected", "Rejected"), ("skipped", "Skip")),
    "needs_review": (("applied", "Applied"), ("rejected", "Rejected"), ("skipped", "Skip")),
    "applied": (("interview", "Interview"), ("rejected", "Rejected")),
    "interview": (("offer", "Offer"), ("rejected", "Rejected")),
}

# The ONE action a card is really offering -- the move that carries the job
# forward through the pipeline. Everything else in ROW_ACTIONS is still there,
# one press away, in the overflow. Three same-weight pills in a card corner made
# every card a 3-way question when two of the three answers close a role out.
PRIMARY_ACTION = {
    "scored": "promote", "tailored": "applied", "needs_review": "applied",
    "applied": "interview", "interview": "offer",
}
# Closing a role out is a different KIND of act from moving it forward, and it
# gets its own (quieter) styling rather than looking like a fourth way to
# advance. Never promoted to primary, which is the other half of the same rule:
# see action_buttons() on the gated-promote case.
NEGATIVE_ACTIONS = ("rejected", "skipped")

# What a queued decision will DO, in a reader's words rather than the wire
# token. `promote` is the one that must never name a destination: the fold sends
# it to db.set_lane, so the job stays `scored` and stays in the Decide column
# after sync -- "moves to Ready" would be a promise nothing here keeps.
# app.js mirrors this map for decisions painted in the browser; the wire string
# stays `promote` in both (see DECISION_ACTIONS).
DECISION_LABEL = {
    "applied": "Applied", "rejected": "Rejected", "skipped": "Skipped",
    "interview": "Interview", "offer": "Offer",
}


def decision_note(action: str) -> str:
    """The annotation a queued card carries in the tray."""
    if action == "promote":
        return "&rarr; queued for tailoring (moves to Queued)"
    return "&rarr; will be marked " + html.escape(
        DECISION_LABEL.get(action, action))


def status_word(status: str) -> str:
    """The status as a reader says it, not as the schema spells it.

    CSS uppercases this word, so `needs_review` reached the page as
    NEEDS_REVIEW -- a database value wearing a display's clothes. The CLASS
    keeps the raw value (`st-needs_review`), because that one is a selector.
    """
    return status.replace("_", " ")


# The stage a card is AT, in the order work flows through it. Deliberately NOT
# STATUS_GROUPS: that one's "Closed" chip merges five statuses, which is right
# for a filter and wrong for a column. interview/offer may hold 0 rows and
# still get a group -- an empty STAGE says "nothing is here", which is
# information; an empty FILTER says "your query found nothing", which is not.
BOARD_COLUMNS = [
    ("ready",   "Ready to submit",   ("tailored",),     "Nothing is tailored and passing right now."),
    # `needs_review` means the local render or the Word page check failed, not
    # that a human owes an opinion. The status token is unchanged everywhere
    # else; only the word on the column moved.
    ("review",  "Render problem",    ("needs_review",), "Every tailored resume rendered and passed its page check."),
    ("decide",  "Decide",            ("scored",),       "Nothing scored is waiting on a decision."),
    ("waiting", "Waiting on scoring",("new",),          "Everything detected has been scored."),
    ("out",     "Submitted",         ("applied", "interview", "offer"), "Nothing submitted yet."),
]
OUT_GROUPS = [("applied", "Applied"), ("interview", "Interviewing"), ("offer", "Offer")]
# Decide holds every scored row, but on 2026-10-09 only ~210 of its 451 were
# Vaish's to make: 72 were already laned auto-tailor (waiting on a tailor slot)
# and 166 the scorer had laned skip. So the column splits by lane. The header
# counts what he owes an opinion on (his call + queued); the skip group sits
# collapsed below with its own count and keeps its Tailor button to overrule.
# A promoted row is laned auto-tailor by the fold, so it moves to "Queued".
DECIDE_GROUPS = [("digest", "Your call"), ("auto-tailor", "Queued for tailoring"),
                 ("skip", "Scorer skipped")]


def decide_group(j: dict) -> str:
    return j["lane"] if j["lane"] in ("auto-tailor", "skip") else "digest"
COLUMN_CAP = 25


def promotable(j: dict) -> bool:
    """Whether a Tailor button on this row would actually change anything.

    Two ways it would not, and both are common enough to matter:
      - the row is past the apply window. freshness.py already decided that one,
        and decisions.plan files the promote as obsolete.
      - the row is ALREADY in the auto-tailor lane. The scorer lanes every 8+
        and every title-gated 7 there itself (45 of 192 live scored rows today),
        and a promote is also its own aftermath: the fold leaves status at
        `scored` and only moves the lane, so the button would come straight back
        offering a second click that db.set_lane turns into a byte-identical
        write. A button that lies about what it does is the thing this page is
        written against, so Tailor shows only where promotion is the point:
        digest, digest-stretch, and a skip-laned row Vaish overrules.
    """
    return j["tier"] != "stale" and j["lane"] != "auto-tailor"


def action_buttons(j: dict, off: bool = False) -> str:
    """The row's buttons, published build only (j["actions"]).

    ONE primary button at a real touch size, and the rest of ROW_ACTIONS behind
    an overflow. The vocabulary is unchanged -- every action a status offered
    before is still offered, and the client still ships the same token -- but
    the card now asks one question instead of three, and the two that close a
    role out no longer sit at the same weight as the one that advances it.

    `off` renders them in exactly the state the client's paint() leaves them in
    after a click here: the `off` class hides the wrapper and `disabled` stops a
    second decision even if the stylesheet never loaded. One parameter for both,
    because paint() applies them as one state and unpaint() lifts them as one.
    """
    if not j["actions"]:
        return ""
    avail = [(act, label) for act, label in ROW_ACTIONS.get(j["status"], ())
             if act != "promote" or promotable(j)]
    if not avail:
        return ""
    dis = " disabled" if off else ""

    def btn(act: str, label: str, cls: str = "", tip: str = "") -> str:
        t = f' title="{tip}"' if tip else ""
        return (f'<button type="button" class="act{cls}" data-act="{act}"{t}{dis}>'
                f'{label}</button>')

    # The primary is the status's own forward move and NOTHING ELSE. When
    # promotable() gates Tailor off a scored row the card gets the overflow
    # alone rather than promoting the next action: that next action is `Skip`
    # on every scored row, and 147 of today's 192 would then wear "skip this
    # role" as the loudest thing on the card. A card with no forward move to
    # offer should look like it has none.
    want = PRIMARY_ACTION.get(j["status"])
    rest = list(avail)
    out = ""
    for i, (act, label) in enumerate(rest):
        if act == want:
            # The one tooltip worth 60 bytes on every card that has it: "Tailor"
            # is this pipeline's word, and the thing it does NOT do (move the
            # job) is the part a reader would otherwise have to guess.
            out = btn(act, label, " pri",
                      "Queue for tailoring; moves to Queued in Decide"
                      if act == "promote" else f"Mark {label}")
            rest.pop(i)
            break
    if rest:
        items = "".join(
            btn(act, label, " neg" if act in NEGATIVE_ACTIONS else "")
            for act, label in rest)
        out += ('<details class="menu"><summary aria-label="More actions">'
                f'&#8943;</summary><div class="mwrap">{items}</div></details>')
    cls = "acts off" if off else "acts"
    return f'<div class="{cls}">{out}</div>'


def status_extra(j: dict) -> str:
    """What hangs under the status word in either row builder.

    A recorded decision REPLACES the cloud-sidecar note rather than stacking
    with it. The sidecar overlay is a pre-click snapshot of branch state, so a
    row showing both would contradict itself: "cloud scored 8" under "pending:
    skipped" reads as though the skip had not registered.

    A decision the BUILD already sees (recorded on another device, or here
    before the last deploy) renders the same DOM paint() builds for a click made
    in this browser: badge, cancel anchor, and the note and buttons present but
    hidden underneath. Without that, such a row was a dead end -- no way back
    except /mark on the laptop -- and unpaint() would have nothing to restore.
    The cancel anchor is gated on j["actions"] with the buttons: the local build
    never starts the decisions client, so there is no handler for a click and
    the anchor would be a dead jump to the top of the page.

    The badge says what the decision WILL DO (decision_note) rather than
    reprinting the wire token: "pending: promote" was the page's last piece of
    schema-speak, and it named a concept the buttons call Tailor.
    """
    def cloud_note(cls: str) -> str:
        return f'<div class="{cls}">{html.escape(j["pending"])}</div>' if j["pending"] else ""

    if not j["decision"]:
        return cloud_note("pending") + action_buttons(j)
    badge = f'<div class="pending pend-d">{decision_note(j["decision"])}'
    if not j["actions"]:
        # Local build: no client, so nothing here can cancel. The badge is the
        # whole story and the sidecar note stays suppressed, as it always was.
        return badge + "</div>"
    # data-cancel sits on the anchor itself, never on a wrapper: the client's
    # delegation handler reads the attribute off the click target, and this
    # anchor has no children, so the target IS the anchor.
    badge += (f' <a href="#" class="cancel" data-cancel="{html.escape(j["id"])}">'
              "cancel</a></div>")
    # Badge last, matching paint()'s cell.appendChild.
    return cloud_note("pending off") + action_buttons(j, off=True) + badge


def card(j: dict, col: str = "", hidden: bool = False) -> str:
    """One job, as the board's single unit of work.

    Replaces table_row() and queue_row(). A job used to render TWICE, in two
    different shapes, and decisions.js had to reconcile from whichever element
    it found first. One shape now: `data-id` is unique across the board, and
    every hook -- the filter, the sort, the decision client, the tray a later
    phase adds -- has exactly one place to look.

    The data-* set is the whole contract with app.js and decisions.js, and it is
    the same set the table row carried. Everything in it is a value the DISPLAY
    cannot be read back out of -- a formatted interval ("6-30h"), a blank score
    on the unscored rows, a lane buried in prose. `data-search` is the one
    deliberate duplication: company, role and location live in three separate
    elements here, and rebuilding that string in JS on every keystroke is work
    Python has already done once. `notes` stays out of it on purpose -- it is
    the full scorer rationale on 87% of rows, and the part worth querying is the
    lane chip. `data-ats` carries the review's `ats` number (the screen score
    since 2026-10-14, keyword coverage before), which the "ATS screen" sort orders on.
    `data-id` is the ONLY thing a click ships alongside its action token.

    An element the card has nothing to say with is not emitted at all (no score
    on an unscored row, no files on an untailored one). Every child is placed by
    an explicit grid-column, so a missing one shifts nothing.
    """
    st = html.escape(j["status"])
    score = "" if j["score"] is None else str(j["score"])
    # Whole hours: the sort only needs an order, and two cards inside the same
    # hour fall back to the generator's own policy order anyway.
    #
    # Emitted ONLY for precisions that carry evidence about the POSTING, which
    # is the same bar db.age_tier applies. A `seen` row's best is a floor on its
    # age with no ceiling — "we recorded this 2h ago" says nothing about when it
    # went up — so under the client-side "Newest first" sort a bare number would
    # let it outrank a posting proven 10h old, on no evidence at all. It has no
    # number here instead, and app.js sorts nulls last in BOTH directions: the
    # same place it sat before first_seen started bounding job_age(), and the
    # Python-side sorts are already gated by TIER_ORDER ahead of `best`.
    best = ("" if j["best"] is None or j["precision"] not in ("exact", "day")
            else f"{j['best']:.0f}")
    art = "1" if j["links"] else "0"
    ats_raw = "" if j["ats"] is None else str(j["ats"])
    search = html.escape(" ".join(
        (j["company"], j["title"], j["location"])).lower())
    attrs = (
        f'data-id="{html.escape(j["id"])}" '
        f'data-tier="{j["tier"]}" data-status="{st}" '
        f'data-lane="{j["lane"]}" data-bucket="{html.escape(j["bucket"])}" '
        f'data-art="{art}" data-score="{score}" data-best="{best}" '
        f'data-ats="{ats_raw}" '
        f'data-band="{score_band(j["score"])}" '
        f'data-col="{col}" data-search="{search}"')
    # The source badge stays INSIDE .co, so the company name is the element's
    # leading text node and app.js can read it with (firstChild || el) -- the
    # same trick the table used, and the reason no data-company is emitted.
    out = [f'<div class="co">{html.escape(j["company"])}{source_badge(j["source"])}</div>']
    if score:
        out.append(f'<b class="sc">{score}</b>')
    # 2026-10-08: the routine tailors 5-year "stretch" roles into slots the
    # in-band ones leave empty. Reviewers score those lower, so say which they are.
    if j["lane"] == "digest-stretch":
        out.append('<div class="stretch" title="Asks 5 years; tailored into a spare slot">'
                   'stretch: asks 5 yrs</div>')
    out.append(f'<div class="ro">{role_link(j)}</div>')
    if j["location"]:
        out.append(f'<div class="loc">{html.escape(j["location"])}</div>')
    # The age text is db.age_label() verbatim, and each precision gets exactly
    # the wording its evidence supports. A board timestamp is the only one that
    # can name a real age, so only it reads "posted 6h ago"; a date-only posting
    # date stays the interval it is ("44-68h" -- 82% of the corpus, where a
    # single number would be a lie); a row known only by when we recorded it
    # reads "seen 3d ago", which is a fact about us and not about the employer.
    # The tier word beside it says what that reading MEANS, which is the thing
    # being decided.
    # The tier dot is drawn by .card .age-txt::before -- markup CSS can derive
    # is markup this page does not pay for 234 times.
    out.append(f'<div class="age-txt">{html.escape(j["age"])}'
               f' &middot; {TIER_LABEL[j["tier"]]}</div>')
    out.append(deadline_badge(j))
    # Plain language first, then the number, then the meter. `has_folder` and
    # not `j["ats"]` decides whether anything is drawn at all: the difference
    # between "measured 0" and "never measured" only exists on a card that has
    # an application folder to measure.
    out.append(ats_badge(j["ats"], j["has_folder"], j.get("screen")))
    # The review readout sits on the face, not in Details (Vaish 10-16): with only
    # the scorer's number in view, his apply calls tracked the scorer (W8), and
    # min(recruiter, hm) is the number that gates a send.
    out.append(review_html(j["review"], j["verdict"], j["review_href"]))
    if j["links"]:
        out.append(f'<div class="lk">{" ".join(j["links"])}</div>')
    if j["status"] == "tailored" and j.get("verify"):
        out.append(verify_html(j["verify"]))
    # The action zone. decisions.js finds it as `.stt`, appends its pending
    # badge here, and app.js lifts the whole card into the tray from the same
    # hook. The status word stays visible because the archive list and the list
    # view both need it, and because it is what the badge hangs off -- and it
    # is printed as a reader says it (status_word), not as the schema spells it.
    out.append(f'<div class="stt st-{st}"><b>{status_word(st)}</b>'
               f'{status_extra(j)}</div>')
    # Everything that answers "why" rather than "what now" folds away: the
    # scorer's rationale, the Word page count, the exact posting date and the
    # age interval drawn to scale. None of it changes the decision at a glance;
    # all of it is one click away.
    more = ""
    if j["notes"]:
        # Scorer notes are routine-authored: routine_text() strips the
        # private-path references that tripped the publish guard.
        more += f"<p>{html.escape(routine_text(j['notes']))}</p>"
    more += word_badge(j["word_pages"])
    # The bar goes full width in here rather than the 128px it had in the table:
    # its 24h and 48h ticks are the point, and a wrapper div to line it up beside
    # the date would cost more bytes on 234 cards than the layout is worth.
    more += (age_bar(j["best"], j["worst"], j["precision"])
             + f"<p>posted {html.escape(j['posted']) or 'date unknown'}</p>")
    out.append(f'<details class="more"><summary>Details</summary>{more}</details>')
    # `hidden`, not a class: it is the COLUMN CAP's state, and app.js recomputes
    # it positionally after every sort and lifts it on any filter match. A class
    # would collide with `.hide`, which means "this card does not match".
    return (f'<article class="card" {attrs}{" hidden" if hidden else ""}>'
            f'{"".join(out)}</article>')


def column(key: str, title: str, statuses: tuple, hint: str,
           jobs: list, sub: str = "") -> str:
    """One board column: a stage, its live count, and the cards standing in it.

    `jobs` arrives in the order it should be read -- the policy order from
    profile/preferences.md for four of the columns, and `ready`'s own
    tier/score/ATS order for the fifth. The column selects its own membership
    from `statuses` so BOARD_COLUMNS stays the single statement of what a stage
    contains.

    Past COLUMN_CAP the cards are still in the DOM, marked `hidden`, behind a
    real button that names how many. Hidden rows, never a hidden number: the
    header count is the whole column, and 25 + "Show the other 167" adds up to
    it in plain sight.

    The hint is server-rendered only when the column is genuinely empty. A
    column emptied by a FILTER must never show it -- "nothing is here" and "your
    query found nothing" are different sentences, and only the first is true.
    """
    picked = [j for j in jobs if j["status"] in statuses]
    aside = ([j for j in picked if decide_group(j) == "skip"]
             if key == "decide" else [])
    n = len(picked) - len(aside)
    hid = f"h-{key}"
    head = (f'<div class="colhead"><h2 id="{hid}">{html.escape(title)}</h2>'
            f'<span class="n">{n}</span></div>')
    if sub:
        head += f'<p class="csub">{sub}</p>'
    seen = 0
    body = ""
    if key == "out":
        # Three sub-stages under one column, and an empty one still draws its
        # header: "Interviewing 0" is the state of the search, not a gap.
        for status, label in OUT_GROUPS:
            grp = [j for j in picked if j["status"] == status]
            rows = ""
            for j in grp:
                rows += card(j, key, hidden=seen >= COLUMN_CAP)
                seen += 1
            body += (f'<div class="grp"><h3 class="sub">{html.escape(label)}'
                     f'<span class="n2">{len(grp)}</span></h3>'
                     f'<div class="cards" data-sub="{status}">{rows}</div></div>')
    else:
        rows = ""
        for j in picked:
            rows += card(j, key, hidden=seen >= COLUMN_CAP)
            seen += 1
        body = f'<div class="cards">{rows}</div>'
    if key == "decide":
        # Same .grp shape as Submitted so app.js counts each group; the skip
        # group is a closed <details> marked .aside, which app.js leaves out of
        # the header count and the cap.
        body = ""
        for lane, label in DECIDE_GROUPS:
            grp = [j for j in picked if decide_group(j) == lane]
            rows = ""
            for j in grp:
                rows += card(j, key, hidden=lane != "skip" and seen >= COLUMN_CAP)
                if lane != "skip":
                    seen += 1
            head3 = (f'<h3 class="sub">{html.escape(label)}'
                     f'<span class="n2">{len(grp)}</span></h3>')
            cards_html = f'<div class="cards" data-sub="{lane}">{rows}</div>'
            if lane == "skip":
                body += (f'<details class="grp aside"><summary>{head3}</summary>'
                         f'{cards_html}</details>')
            else:
                body += f'<div class="grp">{head3}{cards_html}</div>'
    if n > COLUMN_CAP:
        body += (f'<button type="button" class="showmore" data-more="{key}">'
                 f'Show the other {n - COLUMN_CAP}</button>')
    if not n:
        body += f'<p class="hint">{hint}</p>'
    return (f'<section class="col" data-col="{key}" aria-labelledby="{hid}">'
            f'{head}{body}</section>')


# Which column a status stands in, derived from BOARD_COLUMNS so the tray can
# never disagree with the board about where a card came from.
COL_OF_STATUS = {s: key for key, _t, sts, _h in BOARD_COLUMNS for s in sts}


def tray(jobs: list, can_act: bool) -> str:
    """The decisions already made, standing where they cannot be re-made.

    A clicked card LEAVES its column. That is the point: a column headed
    "Ready 3" must never mean three roles already actioned, and the count is
    only honest if the card is somewhere else. Here it keeps its cancel link,
    its files and its details -- nothing is lost, it is only no longer standing
    in the queue of things still to decide.

    Server-rendered in BOTH builds, from the decisions this build can already
    see (pending_decisions -> j["decision"]). The published build's client moves
    further cards in as they are clicked; the local build has no client at all,
    so this is the only path there -- and its cards carry no cancel link,
    because nothing local could act on one.

    The note under the heading differs by build on purpose, and on the published
    build it had to stop being a single claim (2026-08-23). "Saving in a few
    seconds" is true of a decision clicked in THIS browser -- the push is
    debounced by seconds -- and false of every card the build server-rendered
    from decisions/pending.json, which was pushed long ago and is waiting on the
    next sync run: 4 to 9 hours, observed. Both kinds sit in the same tray with
    no per-entry state to tell them apart, so the note now states the two
    timescales instead of promising the shorter one for both. The heading itself
    is just the count. The file:// copy keeps its own sentence, which was
    already the true one there.
    """
    n = len(jobs)
    cards = "".join(card(j, COL_OF_STATUS.get(j["status"], "")) for j in jobs)
    cancel_all = ('<button type="button" id="traycancel">Cancel all</button>'
                  if can_act else "")
    note = ("New clicks save within seconds; the next sync run folds saved"
            " ones into the database." if can_act else
            "Recorded &middot; the next <code>sync.py</code> run folds them"
            " into the database.")
    return (f'<section id="tray"{"" if n else " hidden"}'
            f' aria-label="Decisions queued to sync">'
            # tabindex="-1" so app.js can land focus here when the card whose
            # button was just pressed is reparented into the tray and there is
            # no cancel link to follow (the local build renders none).
            f'<div class="trayhead"><h2 tabindex="-1">Queued to sync &mdash;'
            f' <span class="trayn">{n}</span> decision'
            f'<span class="trays">{"" if n == 1 else "s"}</span></h2>'
            f'{cancel_all}</div>'
            # Outside .trayhead, which is a flex ROW holding the heading and the
            # cancel button: a third child there would either sit beside them or
            # wrap into the button's gap. The count spans app.js updates
            # (.trayn/.trays) stay exactly where they were.
            f'<p class="traynote">{note}</p>'
            f'<div class="cards" id="traycards">{cards}</div></section>')


def gone_row(j: dict) -> str:
    """One got-away role in the archive list.

    Deliberately NOT a `.card`: these rows are not on the board, and giving them
    the board's own class would put them in every count, filter and sort that
    reads `.card` -- silently inflating the number the page reports. It keeps
    `data-id` and a `.stt` zone so decisions.js can still find and paint it.
    """
    st = html.escape(j["status"])
    score = "" if j["score"] is None else str(j["score"])
    return (f'<li class="gone" data-id="{html.escape(j["id"])}"'
            f' data-tier="{j["tier"]}" data-status="{st}" data-lane="{j["lane"]}">'
            f'<span class="sc">{score}</span>'
            f'<span class="co">{html.escape(j["company"])}</span>'
            f'<span class="ro">{role_link(j)}</span>'
            f'<span class="age-txt">{html.escape(j["age"])}</span>'
            f'<span class="stt st-{st}">{status_extra(j)}</span></li>')


CHANNEL_LABEL = {"ats": "Employer boards", "aggregator": "Job aggregators",
                 "curated": "Curated feeds", "startup": "VC boards", "seed": "Pre-pipeline",
                 "other": "Unmapped"}


def nudge_panel(rows: list) -> str:
    """Applied roles that have gone quiet. Renders even when empty, and says why.

    The empty state is not boilerplate here: this list is empty on the day it
    ships and stays empty until outcomes are recorded through the buttons, so a
    panel that appeared only when populated would be indistinguishable from one
    that never worked.
    """
    if rows:
        inner = ('<ul class="gonelist">' + "".join(
            f'<li class="gone" data-id="{html.escape(r["id"])}">'
            f'<span class="co">{html.escape(r["company"])}</span>'
            f'<span class="ro">{role_link(r)}</span>'
            f'<span class="cst"><b>{r["days"]}d silent</b></span></li>'
            for r in rows) + "</ul>")
    else:
        inner = (f'<p class="anote">Nothing has gone quiet. An application'
                 f' lands here once {db.STALE_APPLIED_DAYS} days pass with no'
                 f' recorded movement, and leaves it when you press Interview,'
                 f' Rejected, or Offer. Applications made before outcome'
                 f' tracking existed are deliberately excluded: their only'
                 f' timestamp is when the job was DETECTED, which is a lower'
                 f' bound on when you applied, so counting from it would'
                 f' overstate every wait.</p>')
    return (f'<details class="archive"><summary>'
            f'<b>No outcome yet</b> <span class="n">{len(rows)}</span>'
            f' &middot; applied and silent for {db.STALE_APPLIED_DAYS}+ days'
            f'</summary>{inner}</details>')


def funnel_panel(funnel: dict, applied_rows: int) -> str:
    """Per-channel applied -> interview -> offer. Counts always, rates rarely.

    Two honesty rules carry this panel, and both exist because the alternative
    reads confident and says nothing true:

    1. A rate needs FUNNEL_RATE_MIN_N applications RECORDED through the pipeline.
       Every application predating the history table has no known application
       date, so "0 of N interviewed" measures the absence of a record, not the
       absence of an interview. When the rate is suppressed the panel says which
       of the two it is -- an omitted number with no explanation is the hidden
       number this page keeps promising not to have.
    2. The tracked total differs from the DB's `applied` count (seed rows carry
       no detection channel). Stating the gap is cheaper than having a reader
       find it and read it as a fold bug.
    """
    counted = sum(c["applied"] for c in funnel.values())
    rows = []
    for ch in db.CHANNEL_ORDER:
        c = funnel.get(ch)
        if not c:
            continue
        if c["recorded"] >= db.FUNNEL_RATE_MIN_N:
            rate = (f'<span class="cst">{100.0 * c["interview"] / c["recorded"]:.0f}%'
                    f' interview rate, {c["recorded"]} recorded</span>')
        else:
            rate = (f'<span class="cst">no rate yet &middot;'
                    f' {c["applied"] - c["recorded"]} of {c["applied"]} predate'
                    f' outcome tracking</span>')
        rows.append(
            f'<li class="gone"><span class="co">'
            f'{html.escape(CHANNEL_LABEL.get(ch, ch))}</span>'
            f'<span class="ro"><b>{c["applied"]}</b> applied &rarr;'
            f' <b>{c["interview"]}</b> interview &rarr; <b>{c["offer"]}</b> offer'
            f' &middot; {c["rejected"]} rejected after applying</span>{rate}</li>')
    if rows:
        inner = f'<ul class="gonelist">{"".join(rows)}</ul>'
    else:
        inner = ('<p class="anote">No job carries a recorded application yet,'
                 ' so there is no funnel to draw.</p>')
    inner += (f'<p class="anote">{counted} of the {applied_rows} rows at'
              f' <b>applied</b> are counted here. The rest are seed rows &mdash;'
              f' applications made before this pipeline existed, which have no'
              f' detection channel to credit. Every count is read from the'
              f' transition log, so a role that reached interview and was then'
              f' rejected still counts in both.</p>')
    return (f'<details class="archive"><summary>'
            f'<b>Outcomes by channel</b> <span class="n">{counted}</span>'
            f' &middot; which detection lane actually produces interviews'
            f'</summary>{inner}</details>')


def closed_row(j: dict) -> str:
    """One recently-closed role: what it was, what happened to it, how old.

    Shares `.gone`'s layout and adds `.closed` as its own hook, because these
    rows are NOT the archive's: they are inside the apply window and they left
    the board by a decision rather than by expiring. No action buttons and no
    `.stt` decision zone -- ROW_ACTIONS offers nothing on either status, and a
    row here is the aftermath of an action, not an invitation to another.

    The age is `db.age_label` verbatim, interval and all, for the same reason
    every other age on this page is: 82% of the corpus carries a date-only
    posting date, so a single number would be a lie on most of these rows.
    """
    st = html.escape(j["status"])
    return (f'<li class="gone closed" data-id="{html.escape(j["id"])}"'
            f' data-tier="{j["tier"]}" data-status="{st}">'
            f'<span class="co">{html.escape(j["company"])}</span>'
            f'<span class="ro">{role_link(j)}</span>'
            f'<span class="cst"><b>{status_word(st)}</b></span>'
            f'<span class="age-txt">{html.escape(j["age"])}</span></li>')


def main() -> int:
    ap = argparse.ArgumentParser(description="Regenerate the pipeline dashboard.")
    ap.add_argument("--links", choices=("local", "github"), default="local",
                    help="local: relative file:// paths for opening on this machine. "
                         "github: private blob URLs, for the deployed copy (the "
                         "deploy must never carry resume files).")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()

    conn = db.connect()
    folders = application_folders()
    pending = pending_sidecars(conn)
    decided = pending_decisions()
    # Buttons only on the copy that can actually push one. The local build is a
    # file:// document and must never hold the token that a push needs.
    can_act = args.links == "github"

    # --- the health strip ---------------------------------------------------
    # ONE nav, two clocks, one scale. Four stacked full-width banners used to sit
    # between the heading and the work, each shouting in its own colour, and the
    # two that mattered were indistinguishable from the two that were merely
    # explaining themselves. Every string they printed still exists -- each is
    # inside its clock's <details>, verbatim -- but the strip now answers "is the
    # pipeline alive?" in two dots and gets out of the way.
    run = db.last_run(conn)
    if run:
        # `red_hours=STALE_AFTER_HOURS` collapses the amber band to nothing:
        # green below 3h, red at 3h. Deliberate (ruling R9) -- see staleness().
        age, cls = staleness(run[0], red_hours=STALE_AFTER_HOURS)
        # The runs table lives in the committed DB, so this clock reads the LOCAL
        # checkout. On 2026-08-15 a 2-commit-behind checkout made a perfectly
        # healthy cron look 7h dead — the exact inverse of the failure this clock
        # exists to catch. Say so when we can prove the checkout is behind.
        behind = commits_behind()
        # A run whose sources all threw still stamps the clock and still reads
        # green: the summary carries `errors=...` as plain text nobody scans. A
        # detection layer that is up but blind is its own failure, so it gets its
        # own colour rather than hiding inside a healthy-looking timestamp --
        # but only past DEGRADED_MIN_SOURCES, because at 69 boards polled hourly
        # one transient 5xx is churn and an always-amber dot says nothing.
        errored = degraded_sources(run[1] or "")
        degraded = len(errored) >= DEGRADED_MIN_SOURCES
        cls = "stale" if behind else ("warn" if degraded and cls == "ok" else cls)
        # The bold claim is gated on the SAME threshold as the colour. Gating
        # only the dot would print "Detection is running on fewer sources than
        # it thinks" beside a green one -- a visible line disagreeing with the
        # dot above it, which is the defect the note below is built to avoid.
        # Nothing is hidden either way: `errors=` is already in the summary
        # text, printed verbatim two lines up.
        if degraded:
            source_note = (f' &mdash; <b>DEGRADED: {html.escape(",".join(errored))}'
                           ' returned nothing. Detection is running on fewer'
                           ' sources than it thinks.</b>')
        elif errored:
            source_note = (f' &mdash; {html.escape(errored[0])} returned nothing this'
                           ' run. One source out of the ~69 boards polled hourly is'
                           f' ordinary churn; the clock turns amber at'
                           f' {DEGRADED_MIN_SOURCES} in a run.')
        else:
            source_note = ""
        detect_detail = (f'Last Layer 1 run: <b>{html.escape(run[0])}</b> '
                         f'({age}) &middot; {html.escape(run[1])}'
                         + (f' &mdash; <b>local checkout is {behind} commit(s) behind origin;'
                            ' this clock is reading stale state. Run <code>git pull</code>.</b>'
                            if behind else
                            source_note if cls in ("ok", "warn") else
                            " &mdash; <b>STALE: the detection cron may be dead, not the market"
                            " quiet.</b> Check <code>git pull</code> first, then the workflow."))
        # Each note is the one-phrase reading of the branch above it, so the
        # visible line never disagrees with the folded one. Every phrase is bound
        # to its own CAUSE, not to a colour bucket: "the cron may be dead" is
        # gated on `stale` alone and can never appear over an amber dot. With
        # detection's amber band collapsed (R9) the only amber it has left is the
        # degraded-source branch, which has its own wording -- but the gate is
        # written on the class rather than on that fact, so re-widening the band
        # later cannot silently put a dead-cron claim on a live cron.
        detect_note = ("checkout is behind origin" if behind else
                       "run timestamp is unparseable" if cls == "bad" else
                       f"degraded &mdash; {len(errored)} sources returned nothing"
                       if degraded and cls == "warn" else
                       "the cron may be dead" if cls == "stale" else "")
    else:
        detect_detail = ('No Layer 1 run recorded yet '
                         '(runs table is new; the next scrape.py run stamps it).')
        age, cls, detect_note = "never", "stale", "no run recorded"

    # A second clock, never folded into the first. Both writers stamp the same
    # table, and while they shared a reading every Layer 2 run silently reset the
    # Layer 1 clock to green — a dead cron could hide behind a healthy laptop.
    sync_run = db.last_run(conn, kind="sync")
    if sync_run:
        # Red at 2 * SYNC_STALE_AFTER_HOURS, from staleness()'s own default. The
        # old expression was `"ok" if s_cls == "ok" else "warn"`, which capped
        # this clock at amber forever: a sync that had not run in four days read
        # exactly like one that was six hours late, and the whole point of a
        # clock is that it eventually says something you cannot keep ignoring.
        s_age, s_cls = staleness(sync_run[0], ok_hours=SYNC_STALE_AFTER_HOURS)
        sync_detail = (f'Last Layer 2 sync: <b>{html.escape(sync_run[0])}</b> ({s_age})'
                       f' &middot; {html.escape(sync_run[1])}'
                       + ("" if s_cls == "ok" else
                          " &mdash; cloud work stays unfolded until this runs."))
        sync_note = "" if s_cls == "ok" else "cloud work stays unfolded"
    else:
        s_age, s_cls = "never", "stale"
        sync_detail = ("No Layer 2 sync recorded yet (the runs table records one"
                       " per sync.py run; nothing has folded cloud work into this"
                       " database).")
        sync_note = "no sync recorded"

    # How the LAST attempt ended, which the clock above cannot say: it only ever
    # advances on success, so a deferred or failed run leaves it frozen and
    # looking merely quiet. Local-only by design (see sync_state). It ESCALATES
    # the sync dot rather than sitting beside it: sync.py writes `status: "ok"`
    # on every success, so a stale failure can never hold the dot amber over a
    # healthy pipeline.
    st = sync_state()
    if st.get("status") in ("deferred", "failed"):
        n = int(st.get("consecutive", 1))
        cls_st = "warn" if n < 2 else "stale"
        what = ("deferred &mdash; a file git must rewrite is open in another program"
                if st["status"] == "deferred" else "FAILED")
        banner = (f'<p class="hstate {cls_st}">Last Layer 2 sync attempt '
                  f'{what} at <b>{html.escape(str(st.get("at", "?")))}</b>')
        if n > 1:
            banner += f' &middot; {n} in a row'
        for p in (st.get("paths") or [])[:4]:
            banner += f'<br><code>{html.escape(str(p))}</code>'
        if st["status"] == "deferred":
            banner += ("<br>Nothing was changed. Close the file and the next run "
                       "picks up where it stopped.")
        else:
            banner += f'<br>{html.escape(str(st.get("detail", ""))[:200])}'
        sync_detail += banner + "</p>"
        s_cls = worse(s_cls, cls_st)
        sync_note = ("last sync deferred" if st["status"] == "deferred"
                     else "last sync FAILED")
        if n > 1:
            sync_note += f" &middot; {n} in a row"

    health = ('<nav class="health" aria-label="Pipeline health">'
              + health_clock("Detection", "Layer 1", cls, age, detect_note, detect_detail)
              + health_clock("Sync", "Layer 2", s_cls, s_age, sync_note, sync_detail)
              + "</nav>")

    counts = dict(conn.execute(
        "SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall())

    # Sorting by posted_at in SQL compares three incompatible TEXT shapes
    # lexicographically ('2026-08-14T16:19-04:00' > '2026-08-14'), so ATS rows
    # always floated above same-day JobSpy rows. Order in Python on real age.
    raw = conn.execute("""
        SELECT id, company, title, location, loc_bucket, url, posted_at, status,
               score, notes, first_seen, source, title_norm
        FROM jobs
        WHERE score IS NOT NULL OR status IN ('new', 'tailored', 'needs_review',
                                              'stale_skip', 'applied', 'interview', 'offer')
    """).fetchall()

    jobs = []
    for (jid, company, title, location, bucket, url, posted, status,
         score, notes, first_seen, source, title_norm) in raw:
        folder = folders.get(jid)
        links = []
        review_href = ""
        if folder:
            rel = folder.relative_to(ROOT).as_posix()
            # The deployed copy links into the private repo (a GitHub login is the
            # access control) because resume files are never published with it.
            base = blob_base(rel) if args.links == "github" else f"../{rel}"
            for f in deliverable_files(folder):
                links.append(f'<a href="{base}/{html.escape(f.name)}">'
                             f'{html.escape(f.suffix[1:])}</a>')
            # The brief, under whichever of the two names this folder wrote.
            brief = brief_file(folder)
            if brief:
                links.append(f'<a href="{base}/{brief.name}">{brief.stem}</a>')
            if (folder / "review.md").is_file():
                review_href = f"{base}/review.md"
                links.append(f'<a href="{review_href}">review</a>')
            # The screening draft carries the work-authorization answers, which
            # the resume deliberately does not (ruled 2026-08-24), so it is the
            # one file a submission actually cannot be completed without.
            if (folder / "screening-draft.md").is_file():
                links.append(f'<a href="{base}/screening-draft.md">screening</a>')
        best, worst, precision = db.job_age(posted, first_seen)
        tier = age_tier(best, precision)
        # review.md when this checkout has it; else the numbers sync.py folded
        # into `notes` from the cloud sidecar. The legacy VERDICT text is the
        # last resort, for folders written before the SCORES line existed.
        scores = review_scores(folder) or notes_scores(notes)
        verdict = "" if scores else review_verdict(folder)
        jobs.append({
            "id": jid, "company": company, "title": title, "location": location or "",
            "bucket": bucket or "", "url": url, "posted": (posted or "")[:10],
            "status": status, "score": score, "notes": notes or "",
            "source": source or "", "best": best, "worst": worst,
            "precision": precision, "tier": tier, "lane": lane_of(notes),
            "links": links, "verdict": verdict,
            "review": scores, "review_href": review_href,
            "word_pages": word_pages(notes),
            "verify": brief_verify(folder) if status == "tailored" else None,
            "deadline": folder_deadline(folder),
            "tailored_h": tailored_hours(jid),
            "ats": scores["ats"] if scores else None,
            "screen": screen_summary(folder),
            # Whether there is an application folder AT ALL, which is a
            # different question from whether it produced files or a coverage
            # line -- and the one the badge's "Not measured yet" hangs on.
            "has_folder": folder is not None,
            "pending": pending.get(jid, ""), "title_norm": title_norm or "",
            "age": db.age_label(best, worst, precision),
            # The 72h board window reads the two ages apart: posted OR found.
            "posted_h": db.job_age(posted)[0] if posted else None,
            "seen_h": db.job_age("", first_seen)[0] if first_seen else None,
            "decision": decided.get(jid, ""), "actions": can_act,
        })

    # The policy order from profile/preferences.md: freshness tier first, then
    # score. A fresh 8 beats a stale 10 -- which the old sort had backwards, so
    # the top of this page was showing dead roles above live ones.
    jobs.sort(key=lambda j: (
        TIER_ORDER[j["tier"]],
        -(j["score"] if j["score"] is not None else -1),
        j["best"] if j["best"] is not None else 1e9,
    ))

    # Partition, never filter in SQL: the window is computed from db.job_age()'s
    # interval, which SQL cannot express (posted_at holds three incompatible
    # TEXT shapes, and a date-only value is a 24h-wide range, not a timestamp).
    shown = [j for j in jobs if not is_archived(j)]
    gone = [j for j in jobs if is_archived(j)]

    # A decision already recorded is not a decision still to make, so its card
    # comes OUT of the board and stands in the tray. Everything downstream --
    # the column counts, the ready ordering, the chip tallies, the "N of M
    # roles" readout -- reads `shown`, so pulling them out here is what makes
    # every one of those numbers mean "still waiting on you". The archive's
    # got-away rows are left alone: they are not on the board to begin with.
    #
    # Gated on COL_OF_STATUS, not merely on j["decision"]: `rejected` and
    # `skipped` reach `shown` but stand in no column, so a decision on one would
    # put a card in the tray that unpaint() has nowhere to return -- it would
    # sit there afterwards with its buttons live and no badge. A row the board
    # never drew keeps its badge where it always was, which is nowhere.
    trayed = [j for j in shown
              if j["decision"] and j["status"] in COL_OF_STATUS]
    ids = {j["id"] for j in trayed}
    shown = [j for j in shown if j["id"] not in ids]
    tray_html = tray(trayed, can_act)

    # --- the board: five stages, one card per job ---------------------------
    # A stage is not a filter. The chips still narrow what is DRAWN inside each
    # column, but they never redefine which column a job stands in: the column
    # is where the work actually is, and a filter that could move a card between
    # stages would make the board a partial view of itself.
    ready = [j for j in shown if j["status"] == "tailored"]
    # This ONE column leads on min(recruiter, hm) rather than on freshness, and
    # that is deliberate (Phase 4): everything here is already tailored, so the
    # question is no longer "is this still worth doing" but "which of these do I
    # send first", and the weaker of the two simulated humans is the answer.
    # Freshness, then fit score, break the ties beneath it -- the policy order
    # from profile/preferences.md still governs every other column.
    ready.sort(key=lambda j: (
        -review_floor(j["review"]),
        TIER_ORDER[j["tier"]],
        j["best"] if j["best"] is not None else 1e9,
        -(j["score"] if j["score"] is not None else -1),
    ))
    unscored = [j for j in shown if j["status"] == "new"]
    # The old standalone health strip, now said where it is actionable: under
    # the heading of the column it describes. A number that keeps climbing here
    # means the cloud judge is not landing, and that is only legible next to
    # the pile it is failing to clear.
    n = len(unscored)
    waiting_sub = (
        f'<b>{n}</b> detected role{"" if n == 1 else "s"} not scored yet'
        " &middot; the cloud judge runs on a schedule. A number that keeps"
        " climbing means it is not landing." if n else "")

    subs = {"waiting": waiting_sub, "ready": old_ready_line(gone)}

    board = "".join(
        column(key, title, statuses, hint,
               ready if key == "ready" else shown,
               sub=subs.get(key, ""))
        for key, title, statuses, hint in BOARD_COLUMNS)

    # --- archive readout ----------------------------------------------------
    # Hidden rows, never a hidden number. This repo's recurring failure is a
    # check whose output reads reassuring while the thing it measures is broken
    # (the dead-cron clock, the always-amber alarm), and silently dropping half
    # the corpus would be exactly that. So the count is permanent, it says what
    # the rule was, and the qualifying near-misses are named individually.
    #
    # Unconditional since 2026-08-23. It used to render only when `gone` was
    # non-empty, which made the tallies below -- whole-database populations the
    # board never draws -- blink out on exactly the quiet day a reader would
    # wonder where 4,750 skipped rows went. An empty archive is a fact about the
    # pipeline; a missing archive is a fact about the renderer.
    never = sum(counts.values()) - len(jobs)
    # Exclude re-postings of work already done. A board that lists the
    # employer under an aggregator's name ("Information Technology Senior
    # Management Forum" for a bank's role) clears dedupe, because the
    # key is company+title+location and the company differs -- so the same
    # role reappears, scores 10, and expires. 13 of 65 on 2026-08-20. Left
    # in, this readout would claim work was missed that was in fact shipped.
    done = {j["title_norm"] for j in jobs
            if j["status"] in ("tailored", "needs_review", "applied",
                               "interview", "offer") and j["title_norm"]}
    got = sorted(
        (j for j in gone if (j["score"] or 0) >= GOT_AWAY_MIN_SCORE
         and (j["worst"] or 1e9) <= GOT_AWAY_MAX_HOURS
         and j["title_norm"] not in done),
        key=lambda j: (-(j["score"] or 0), j["best"] if j["best"] is not None else 1e9))
    inner = ""
    if got:
        n = len(got)
        inner = (f'<p class="anote"><b>{n} role{"" if n == 1 else "s"} scored'
                 f' {GOT_AWAY_MIN_SCORE}+ expired unactioned</b> in the last'
                 f' {GOT_AWAY_MAX_HOURS // 24} days. Qualifying roles are arriving'
                 f' faster than the tailor cap clears them.</p>'
                 f'<ul class="gonelist">'
                 + "".join(gone_row(j) for j in got[:6]) + "</ul>")
    # The big numbers are STATED, not implied by a row count nobody adds up.
    # These are whole populations the board never draws, and the only honest
    # way to show a hidden population is to say how large it is. Labelled as
    # whole-database status totals rather than left to look additive with the
    # counts above -- they overlap, and a reader adding them up would be
    # counting the same rows twice.
    #
    # `rejected` joined the list when the status-chip strip was retired: it is
    # the one status with no column of its own and no chip of its own (the
    # "Closed" filter merges five), so without a line here its count would have
    # died with the strip. Rendered at zero like its three neighbours, because a
    # line that appears the day the first rejection lands teaches a reader that
    # its absence meant "not tracked" rather than "none".
    inner += (f'<p class="anote">Rows the pipeline already decided about.'
              f' The board shows roles posted or found in the last'
              f' {SHOW_WINDOW_HOURS}h, tailored resumes included; applied, interview'
              f' and offer rows stay at any age. {never:,} more rows never reach this page at all'
              f' (off-target titles and first-poll backlog).</p>'
              f'<p class="anote">Status totals across the whole database'
              f' (these overlap the counts above, so they do not add up):</p>'
              f'<ul class="tally">'
              f'<li><b>{counts.get("stale_skip", 0):,}</b> swept past the'
              f' {db.STALE_HOURS:.0f}h window</li>'
              f'<li><b>{counts.get("skipped", 0):,}</b> skipped as off-target</li>'
              f'<li><b>{counts.get("baseline_skip", 0):,}</b> first-poll backlog</li>'
              f'<li><b>{counts.get("rejected", 0):,}</b> rejected</li>'
              f'</ul>')
    # --- recently closed ----------------------------------------------------
    # Its own <details>, ABOVE the archive's and never inside it: the archive
    # summary claims "past the window, still deduped", which is false of every
    # row here, and a list that sits under a heading making a claim it does not
    # meet is the same defect as a readout that reads green while blind.
    #
    # Ordered by the board's own recency key -- best-case age ascending, unknown
    # dates last -- so "newest first" means here what it means in a column.
    #
    # Filtered on STATUS alone, with no score gate, and the prose is written to
    # match: hard-filter and repost rows are `skipped` without Vaish, so
    # "you marked these" would be false on some rows. The status word carries
    # what happened; the section only claims these were closed out.
    closed = sorted(
        (j for j in shown if j["status"] in CLOSED_STATUSES),
        key=lambda j: j["best"] if j["best"] is not None else 1e9)
    if closed:
        cinner = ('<ul class="gonelist">'
                  + "".join(closed_row(j) for j in closed[:CLOSED_CAP])
                  + "</ul>")
        if len(closed) > CLOSED_CAP:
            cinner += (f'<p class="anote">and {len(closed) - CLOSED_CAP} more'
                       f' not archived yet.</p>')
    else:
        # The honest empty state, for the same reason the archive renders
        # unconditionally: a section that appears only when it has rows teaches
        # a reader that its absence means "nothing happened" -- and the day a
        # mark stops folding, "nothing happened" is exactly the wrong reading.
        cinner = ('<p class="anote">Nothing is waiting here. A role marked'
                  ' skipped or rejected lands in this list on the next sync,'
                  ' and leaves it when the posting ages into the archive'
                  ' below.</p>')
    # The summary says what the FILTER means, not what a reader might assume it
    # means. Membership is `not is_archived()`, which is a tier test rather than
    # a clock: a closed row whose posting date is unknown never becomes stale,
    # so it can sit in this list indefinitely. Naming the 48h window here would
    # therefore be a claim the code does not make -- the archive below is the
    # one that can honestly cite the window, because sweeping past it is what
    # puts a row there.
    closed_html = (
        f'<details class="archive closedlist"><summary>'
        f'<b>Recently closed</b> <span class="n">{len(closed)}</span>'
        f' &middot; decided about and not archived yet, so they stand in'
        f' no column'
        f'</summary>{cinner}</details>')

    # One <aside> holds everything that is true but not today's work: the roles
    # just closed out and the archived population.
    # --- outcomes -----------------------------------------------------------
    # Both panels read status_history, which is why they live here rather than
    # on a card: an outcome is a fact about an application over time, and the
    # board's columns only ever show where a row stands right now.
    outcomes_html = (nudge_panel(db.no_outcome_yet(conn))
                     + funnel_panel(db.outcome_funnel(conn),
                                    counts.get("applied", 0)))

    archive_html = (
        f'<aside aria-label="Recently closed and archive">'
        f'{closed_html}'
        f'{outcomes_html}'
        f'<details class="archive"><summary>'
        f'<b>Archived</b> <span class="n">{len(gone)}</span>'
        f' &middot; past the {db.STALE_HOURS:.0f}h window, still in the'
        f' database, still deduped</summary>{inner}</details>'
        f'</aside>')

    # --- filter controls ----------------------------------------------------
    # Status chips are built from db.STATUSES, not from what happens to be in the
    # table today: three legal statuses have zero rows right now, and a filter
    # that quietly lacks a value is indistinguishable from one that found none.
    STATUS_GROUPS = [
        ("Ready", ("tailored",)),
        ("Render problem", ("needs_review",)),
        ("Decide", ("scored",)),
        ("Waiting", ("new",)),
        ("Closed", ("applied", "rejected", "skipped", "interview", "offer")),
    ]
    grouped = set(ARCHIVE_ONLY_STATUSES) | {s for _, ss in STATUS_GROUPS for s in ss}
    # Anything added to db.STATUSES later gets its own chip rather than vanishing.
    STATUS_GROUPS += [(s, (s,)) for s in db.STATUSES if s not in grouped]

    def chips_for(group: str, items, key=None) -> str:
        """Chips carry their own count, which is what makes the archive honest.

        Without it a chip reading empty is ambiguous between "nothing matches"
        and "those rows are not on this page" -- and since archiving removed a
        whole tier from the table, that ambiguity would be live on every load.

        `tabindex` and `aria-pressed` are rendered HERE, not attached by app.js
        the way tabindex used to be. A control that only becomes reachable if a
        script ran is a control that is missing when the script fails, and this
        one is a `role="button"` -- the role promises operability, so the markup
        has to keep the promise on its own. paintChips() flips aria-pressed;
        false is the honest initial value in both builds, because a hash-driven
        selection is applied by readHash() before the reader sees the page.
        """
        out = []
        for label, vals in items:
            n = sum(1 for j in shown if key(j) in vals) if key else None
            tally = f'<b>{n}</b>' if n is not None else ""
            out.append(
                f'<span class="f" data-g="{group}" data-v="{html.escape(",".join(vals))}"'
                f' role="button" tabindex="0" aria-pressed="false">'
                f'{html.escape(label)}{tally}</span>')
        return "".join(out)

    buckets = sorted({j["bucket"] for j in jobs if j["bucket"]})
    loc_opts = "".join(f'<option value="{html.escape(b)}">{html.escape(b)}</option>'
                       for b in buckets)
    # The token panel exists ONLY in the published build, so the file:// copy has
    # nowhere to type a token even by accident. It gets a pointer instead.
    if can_act:
        # A real <button>, not an anchor: it operates a panel on this page and
        # goes nowhere, and aria-expanded is a promise only a button can keep.
        # app.js MIRRORS the panel's open state onto it rather than toggling the
        # attribute itself -- the decisions client opens the panel on its own
        # when a click arrives with no token stored, and a handler that only
        # counted gear clicks would start lying at exactly that moment.
        act_ui = ('<button type="button" id="gear" aria-expanded="false"'
                  ' title="Settings for the action buttons"'
                  ' aria-label="Settings for the action buttons">&#9881;</button>')
        act_panel = (
            '<div class="patbox" id="pat">'
            '<div class="patmsg">The action buttons record a decision in'
            f' <code>{DECISIONS_REL}</code> with a GitHub fine-grained token'
            ' (Contents: read and write, this repository only). It is kept in this'
            ' browser and sent to api.github.com and nowhere else. Nothing is'
            ' submitted to an employer: sync.py folds the decision into the'
            ' database on its next run.</div>'
            '<div class="patmsg" id="patstate"></div>'
            '<input id="pati" type="password" autocomplete="off" spellcheck="false"'
            ' placeholder="paste the token" aria-label="GitHub token">'
            '<button type="button" id="patsave">Save</button> '
            '<button type="button" id="patclear">Forget</button></div>')
    else:
        act_ui = f'<a id="live" href="{LIVE_URL}">act on the live dashboard</a>'
        act_panel = ""
    # A labelled region, directly above the board it governs, in the order a
    # hand reaches for it: type, narrow, order, choose a layout, then the chip
    # groups, then the way out. Reset sits LAST rather than in the top row --
    # it used to be the sixth tab stop with twenty chips after it, so the
    # keyboard order contradicted the reading order at the one control whose
    # whole job is undoing the others.
    #
    # "has resume" is its own group now. It lived inside Lane, where it looked
    # like a fifth lane and inherited the OR: picking it beside `digest` read as
    # "digest OR has-resume" when the code has always meant "digest AND
    # has-resume". Groups AND across and OR within, so the grouping IS the
    # semantics, and hiding one inside another hides the semantics.
    controls = f"""<section class="controls" aria-label="Filter and sort roles">
<div class="searchrow">
  <input id="q" type="search" placeholder="Search company, role, or location"
         autocomplete="off" aria-label="Search roles">
  <span class="slash">/</span>
  <select id="loc" aria-label="Filter by location"><option value="">All locations</option>
  {loc_opts}</select>
  <select id="sortby" aria-label="Sort roles within each column">
  <!-- The default option's VALUE stays `default` -- bookmarked #sort= URLs and
       app.js's SORT_ALIAS both name it, and it is the server order the page
       ships in. Its label says what that order actually is: TIER_ORDER first,
       then score, then age (see the jobs.sort below), which is Vaish's own
       policy from preferences.md and already makes freshness the default
       sort. It was called "Pipeline order", which named where the order came
       from rather than what it does, so the one control that answers "why is
       this card at the top" declined to answer it. -->
  <option value="default">Freshness, then fit</option>
  <option value="score">Fit score</option>
  <option value="age">Newest first</option>
  <option value="ats">ATS screen</option>
  <option value="company">Company</option></select>
  <div class="viewbar" role="group" aria-label="Layout">
  <button type="button" data-view-set="board" aria-pressed="true">Board</button>
  <button type="button" data-view-set="list" aria-pressed="false">List</button>
  </div>
  {act_ui}
</div>
{act_panel}
<div class="fgroup"><span class="lbl">Window</span>{chips_for("tier",
    [(TIER_LABEL[t], (t,)) for t in ("now", "soon", "nodate")],
    key=lambda j: j["tier"])}</div>
<div class="fgroup"><span class="lbl">Stage</span>{chips_for("status", STATUS_GROUPS,
    key=lambda j: j["status"])}</div>
<div class="fgroup"><span class="lbl">Score</span>{chips_for("band",
    [("8+", ("8+",)), ("7", ("7",)), ("5-6", ("5-6",)), ("4 or less", ("4-",))],
    key=lambda j: score_band(j["score"]))}</div>
<div class="fgroup"><span class="lbl">Lane</span>{chips_for("lane",
    [(lane, (lane,)) for lane in LANES], key=lambda j: j["lane"])}</div>
<div class="fgroup"><span class="lbl">Artifacts</span>{chips_for("art",
    [("has resume", ("1",))], key=lambda j: "1" if j["links"] else "0")}</div>
<div class="ctlfoot">
  <span id="count" aria-live="polite" aria-atomic="true"></span>
  <a href="#" id="reset">Reset</a>
</div>
{ATS_HELP}
</section>"""

    # Everything SCRIPT needs from Python that is not a per-row data-* attribute.
    # A JSON island rather than an f-string SCRIPT: the IIFE is full of braces,
    # and `pending` lets the client tell "already pushed" from "never landed"
    # without a network call on every load. Contents are our own constants plus
    # ids and action tokens that pending_decisions() has already validated, and
    # `<` is escaped anyway so no value can close this tag early.
    cfg_json = json.dumps({
        "owner": GITHUB_OWNER, "repo": GITHUB_NAME, "branch": DECISIONS_BRANCH,
        "path": DECISIONS_REL, "actions": can_act, "pending": decided,
    }, ensure_ascii=True).replace("<", "\\u003c")
    err_note = '<div id="err" role="status"></div>' if can_act else ""

    assets = {
        "tokens.css": _asset("tokens.css"),
        "base.css": _asset("base.css"),
        "board.css": _asset("board.css"),
        "controls.css": _asset("controls.css"),
        "app.js": _asset("app.js"),
        "decisions.js": _asset("decisions.js"),
    }
    for name, text in assets.items():
        assert "</script" not in text.lower() and "</style" not in text.lower(), name
    css = assets["tokens.css"] + assets["base.css"] + assets["board.css"] + assets["controls.css"]
    # The decisions client is inlined ONLY where it can act. It was in both
    # builds because the two files were two halves of one IIFE; they are two
    # IIFEs now, so the local file:// copy ships without the GitHub host name,
    # the token key, and every line that could reach either. The behavioural
    # gate (CFG.actions) still stands inside the published copy -- this removes
    # the text as well, which is the only version of "not there" that a grep,
    # or a reader worried about a token on a shared machine, can verify.
    js = assets["app.js"] + (assets["decisions.js"] if can_act else "")

    # The only script allowed above the content, and the reason a reload into a
    # remembered dark theme does not flash white: it sets data-theme on <html>
    # while the parser is still in <head>, before anything paints. Kept as a
    # constant rather than inlined in the page f-string because its braces would
    # all need doubling there. try/catch because a browser with site data
    # blocked throws on getItem itself -- a page that renders in the wrong theme
    # beats a page whose head script died.
    theme_boot = (
        "<script>try{var t=localStorage.getItem('jsos.theme');"
        "if(t==='dark'||t==='light')"
        "document.documentElement.setAttribute('data-theme',t);}catch(e){}</script>")
    # Emitted in BOTH builds and deliberately NOT can_act-gated. The decisions
    # client keeps localStorage at arm's length because of the GitHub token's
    # blast radius; a colour preference has none, and the local file:// copy is
    # the one Vaish reads at 06:00. Placed before the <h1> and floated right:
    # the heading must keep its exact markup and gain no parent, because
    # vercel_guard greps the served bytes for PUBLIC_SENTINEL contiguously.
    theme_ui = (
        '<div class="themebar" role="group" aria-label="Colour theme">'
        '<button type="button" data-theme-set="auto" aria-pressed="true">Auto</button>'
        '<button type="button" data-theme-set="light" aria-pressed="false">Light</button>'
        '<button type="button" data-theme-set="dark" aria-pressed="false">Dark</button>'
        '</div>')

    generated = db.now_iso()
    page = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{PUBLIC_SENTINEL}</title>
{theme_boot}
<style>
{css}</style></head><body>
<header>
{theme_ui}
<h1>{PUBLIC_SENTINEL}<span class="sub"> &middot; triage board</span></h1>
<div class="meta">Generated {generated} &middot; prepared by the pipeline, submitted by Vaish (never automatically)</div>
</header>
{health}
{tray_html}
{controls}
<main id="board" class="board" data-view="board" data-cap="{COLUMN_CAP}">{board}</main>
<div id="nohits"></div>
{archive_html}
{err_note}
<script id="cfg" type="application/json">{cfg_json}</script>
<script>{js}</script>
</body></html>
"""
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(page, encoding="utf-8")
    pend = f", {len(pending)} awaiting local ingest" if pending else ""
    pend += (f", {len(decided)} decision(s) awaiting the fold"
             f" ({len(trayed)} in the tray)" if decided else "")
    print(f"dashboard: {len(shown)} rows{pend} -> {args.out}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
