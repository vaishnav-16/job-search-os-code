# Job Search OS

An agentic pipeline that finds fresh product-management postings within hours of them going
up, scores each one against a structured evidence base, and writes a tailored one-page resume
for the ones worth applying to. Four independent reviewer agents grade every page before a
human reads it and submits it by hand, because nothing in this system submits, sends, or marks
a job applied.

There is no server. Detection runs in GitHub Actions, judgment runs in two scheduled Claude
cloud agents, and a GitHub Actions job folds their output back into one database and
republishes a dashboard. Nothing has to run on a laptop. This README explains the whole thing
without requiring you to read any code, and ends with a setup guide if you want to run it for
yourself.

This is a public, code-only copy. The evidence base, master resume, template document and
application history stay private. See [What is deliberately not here](#what-is-deliberately-not-here).

---

## Contents

- [What it produces](#what-it-produces)
- [Why it is built this way](#why-it-is-built-this-way)
- [How it runs: one chain, no server](#how-it-runs-one-chain-no-server)
- [Layer 1: detection](#layer-1-detection)
- [Layer 2: scoring](#layer-2-scoring)
- [Layer 2: tailoring](#layer-2-tailoring)
- [The ATS screen](#the-ats-screen)
- [The fold](#the-fold)
- [The agents](#the-agents)
- [Integrity design](#integrity-design)
- [Data model](#data-model)
- [Model policy](#model-policy)
- [Scale](#scale)
- [Repository map](#repository-map)
- [Setting it up yourself](#setting-it-up-yourself)
- [Tests](#tests)
- [What is deliberately not here](#what-is-deliberately-not-here)

---

## What it produces

Three times a day a dashboard fills with rows like this, sorted by the worst of the two human
reviewer scores (recruiter and hiring manager):

```
Acme Corp  ·  Product Manager, Payments Platform  ·  posted 6h ago
Screen B · 7/9 basic   recruiter 8   hiring manager 8
Inferred beyond the written record: none
[ resume.docx ]  [ resume.pdf ]  [ review.md ]  [ screening answers ]
[ mark applied ]  [ reject ]  [ skip ]
```

"Screen B · 7/9 basic" is the requirement screen described in [The ATS screen](#the-ats-screen):
the tier a 2026 AI screener would give the page, and how many of the posting's basic
qualifications the page proves.

Behind each row is a folder holding everything the run produced:

| File | What it is |
|---|---|
| `jd.md` | the job description as captured, so a later reader can check the tailoring against the source |
| `requirements.json` | the posting's qualifications, split into basic and preferred by code before any drafting, and fixed for the whole run |
| `brief.md` | what this hiring manager actually needs, a requirement map from each qualification to the bullet that proves it, the load-bearing terms in the posting, a sketch of the ideal candidate, the lines a reviewer flagged for the human to defend, and the revision notes |
| `<name>_Resume_<Company>.docx` | the tailored resume, built into the real template |
| `<name>_Resume_<Company>.pdf` | the rendered page, exactly one page, the file that gets submitted |
| `review.md` | all four reviewers' write-ups, a `READABILITY: N` line, and as the last line a machine-readable `SCORES: ats NN / recruiter N / hm N` |
| `screen.json` | the last round's requirement screen: grade A to D, score, basic and preferred met, title search, findability. The dashboard reads it |
| `screening-draft.md` | prefilled answers for the application form's standard questions |

A human opens the PDF, strikes anything they cannot defend in an interview, applies by hand,
and clicks a button. That click is the only thing that marks a job applied.

---

## Why it is built this way

Job search automation usually fails in one of two directions. Fully manual means every
posting is found late and every resume is a rushed edit of the last one. Fully automated
produces generic output, and the tools that auto-submit are making claims on your behalf in a
channel where being caught is terminal.

The split here is deliberate and it is the main design idea:

- **Detection is a dumb job with no model in the loop.** Polling job boards and rejecting
  postings that say "8+ years required" is regex work. Spending an LLM call on it would cost
  money and add a failure mode without improving a single decision.
- **Judgment is a pair of scheduled agents.** Deciding whether a posting is worth a tailored
  resume, and then writing that resume, is the part that actually needs a model.
- **The decision to send is always a person's.** No exceptions, no configuration flag.

Everything else follows from those three.

---

## How it runs: one chain, no server

This is the part most worth understanding. Independent schedulers hand work to each other
through a git repository, and each stage writes only the files it owns.

```
  cron-job.org, every 2h                    (GitHub's own schedule: sparse backup)
        │  workflow_dispatch
        ▼
  detect.yml            GitHub Actions, no model, a few minutes
        │  poll ~90 company boards, 6 big-tech sites, VC portfolio boards, feeds
        │  dedupe, hard filters, 72h freshness
        ▼
  queue/*.json + jobs.sqlite  ─────────────────────────────▶  main
        │
        ▼
  score-queue           Claude cloud Routine, Sonnet, every 4h
        │  one job-scorer subagent per queue file
        ▼
  scores/  ──────────────────────────────────────────────▶  claude/judge-score-<run>
        │
        ▼
  tailor-queue          Claude cloud Routine, Opus coordinator, 3x a day
        │  one fresh resume-tailorer per job, four reviewers per round
        ▼
  scores/, tailored/, applications/  ────────────────────▶  claude/judge-tailor-<run>
        │
        ▼
  fold.yml              GitHub Actions, no model, on every claude/judge* push
        │  sync.py --ci: sidecars and folders into jobs.sqlite and main,
        │  delete the folded run branches, publish the dashboard
        ▼
  dashboard on Vercel, behind authentication
        │
        ▼
  a human reads, edits, applies by hand, clicks
        │  decisions/pending.json on main (a click starts no workflow)
        ▼
  applied by the next fold: a routine push, or one detect.yml dispatches
```

### The timing chain

Everything is anchored to three daily **review windows**, the times a human actually sits down
to read resumes. Work is scheduled backwards from those.

| Stage | Runs on | When (UTC) | Why then |
|---|---|---|---|
| `detect.yml` | GitHub Actions, dispatched by cron-job.org | `:17` past every odd hour, 12 a day | continuous, so nothing ages out, and each run lands before a routine fire |
| `score-queue` | Claude cloud Routine | every 4h at `:55` | 38 minutes after a detect run, so fresh arrivals are scored first |
| `tailor-queue` | Claude cloud Routine | 08:30 / 17:30 / 22:30 | 3h30 before each review window, long enough to tailor and review a full batch |
| `fold.yml` | GitHub Actions | minutes after each routine push or local command | folds the cloud's work and republishes the dashboard |
| decisions | `fold.yml`, dispatched by `detect.yml` | within about 2h of a click | a click costs no Actions time of its own |
| review | a human | 08:00 / 17:00 / 22:00 Eastern | reads, edits, applies, clicks |

### Why each scheduler

**GitHub Actions for detection, triggered from outside.** Actions is free at this volume and a
closed laptop at 3am still detects jobs. GitHub's own `schedule:` turned out not to be good
enough: measured over 30 days it delivered about 6 of 48 requested fires a day (12%), with gaps
up to 6.5 hours, and the median posting was 23 hours old when detection first saw it. Adding
cron entries bought nothing, because GitHub absorbed the extra asks. So the primary trigger is
now a free cron-job.org job that calls the `workflow_dispatch` API every two hours at `:17` past
odd UTC hours. The GitHub `schedule:` stays as a sparse backup (`17 */4`), so detection limps on
if the external caller dies. Every scheduled or externally dispatched run passes a 40-minute
gate that reads the database's own runs table, so a backup fire that lands right after an
external one does no work. A human dispatch from the Actions tab skips the gate and always
runs, because that is nearly always someone checking a change. The workflow file carries all of
these measurements in its own comments.

**Claude cloud Routines for judgment.** A Routine is a scheduled agent session: a prompt, a cron
expression, a model, and a git branch it is allowed to push to. It clones the repo, works,
commits, and exits. Because it runs in Anthropic's cloud rather than on a laptop, the laptop can
be shut. There are two, split on purpose (see [Layer 2: scoring](#layer-2-scoring)). Both prompts
are versioned in this repo under `.claude/routines/`, and `pipeline/routine_parity.py` exists to
prove the live configured prompt still matches the file.

**GitHub Actions for the fold.** Folding used to run on the laptop from Windows Task Scheduler.
It was retired after two real failures. First, with the laptop off for four days, 32 reviewed
resumes sat on routine branches while detection's age sweep, which runs in the cloud, aged their
rows to stale, and the late fold refused them: a quarter of that week's output never reached the
dashboard. Second, once the cloud fold started winning push races, the laptop's recovery step
(`reset --hard origin/main`) wiped uncommitted edits. Now `fold.yml` folds every routine push in
Actions, and the laptop is only a client: local commands pull before they run and publish plain
text records afterwards (`pipeline/localgit.py`). Text rebases. A binary database does not, which
is the whole point.

The dashboard publish is a step at the end of `detect.yml` and `fold.yml`
(`.github/actions/publish-dashboard`), not a workflow of its own. As a separate workflow it was
about 22 billed jobs a day, a third of the Actions budget, and it rebuilt even after gated
detect runs that changed nothing.

### One owner for the clock

Several schedulers means several places for times to drift apart, so none of them owns the
times. `pipeline/schedule.py` owns all of them and derives every routine cron from the review
windows:

```bash
python pipeline/schedule.py --profile home --print   # show the whole chain, pure, no side effects
```

`docs/schedule.md` is that `--print` output pasted verbatim, and `pipeline/test_schedule.py`
diffs the document against the code on every test run, so the documentation cannot rot.

Routine crons are fixed UTC while the review windows are local time, so daylight saving moves
one and not the other. Twice a year the offset in `schedule.py` changes and both routine crons
are re-sent. The schedule document says so, with the date.

---

## Layer 1: detection

`.github/workflows/detect.yml`, every two hours, no model.

Sources:

- **About 90 company boards** listed in `profile/targets.yaml`, read through the public APIs of
  Greenhouse, Lever, Ashby and Workday. These are the backbone.
- **Six big-tech career sites** that run their own portals (`pipeline/bigtech.py`): each fetcher
  reads the JSON or server-rendered data the site's own front end uses, with a plain GET and no
  key, and `robots.txt` was checked for every path used. Where `robots.txt` disallows the data
  path, the fetcher is not built, and the file says why.
- **VC portfolio job boards** (`pipeline/vc_boards.py`): Getro and Consider boards, which carry
  most startup postings, read through their public board pages with no login.
- **JobSpy**, for breadth, and a **curated aggregator feed** of public new-grad PM tables.

Two more sources exist in the code and are **switched off in this public copy's workflow, for
terms-of-service reasons**: JobSpy's LinkedIn search (`scrape.py --linkedin`) and a reader for a
logged-in Jobright feed (it runs only when a `JOBRIGHT_COOKIE` session is supplied). Automated
collection from LinkedIn is against its terms, and reading a logged-in session is a decision an
operator has to make for themselves. The workflow does not pass either one, so both are skipped.

Then, per posting:

- **Dedupe** is a SHA1 of company, title and a location bucket, checked against the committed
  database. There is also a separate *role key*, because the identity of a piece of work is not
  the identity of a posting: one role re-listed three times should cost one tailoring run, not
  three. That rule exists because a single role once sat in the database three times and burned
  two Opus tailoring runs on itself.
- **Reposts are recorded as skipped**, not queued. An aggregator row whose role's newest copy on
  a polled company board is already past 72 hours is an old role relisted with a fresh date.
  A fresh posting of a role whose earlier row aged out unworked is revived instead
  (`db.is_revivable`).
- **Hard filters** (`pipeline/filter.py`) are pure string and regex work: 6+ years required,
  Director/Staff/Principal, a Senior title whose posting states no years, marketing or sales PM,
  non-US, explicit no-sponsorship, security clearance. The filter is careful about false
  positives. "Clearing" in a trade-settlement posting is not a security clearance, "citizenship"
  inside equal-opportunity boilerplate is not a citizenship requirement, "8+ years preferred"
  passes, and "for more than 75 years we have been advancing..." is company history, not a
  years ask.
- **Freshness**: anything older than 72 hours is dropped, and a freshness sweep prunes provably
  stale queue entries in the same run.

Net-new jobs are committed to `queue/` as JSON along with an updated `pipeline/jobs.sqlite`.

Two failure modes are handled explicitly. A lost push race recovers by replaying its one commit
when the remote database is unchanged, and otherwise by resetting to the remote and detecting
again, never by rebasing the binary database. And a run where every live source errors and
nothing is found fails on purpose, because otherwise "detection is down" and "a quiet hour" show
the same green check.

---

## Layer 2: scoring

`score-queue` is a cloud Routine on Sonnet that fires every four hours. Its only output is
`scores/`.

1. Overlay every earlier routine branch, oldest first, so it never re-scores a job a run that is
   not yet folded already scored.
2. `python pipeline/worklist.py score` prints the work list as JSON: unscored, not tailored, not
   provably stale, one posting per role, capped at 120.
3. Launch one `job-scorer` subagent per queue file, at most 10 at once. Each scores fit out of 10
   across five criteria (role type, years asked, domain and skills fit, company and product,
   overlap with the evidence base), names any gate it found, and assigns a lane: `auto-tailor`,
   `digest-stretch` (a strong fit that asks 5 years), `digest` or `skip`. One subagent per job
   means each judgment happens in a fresh context, so job 40 is scored the same way job 1 was.
4. **Each scorer writes its own reply to a named file, and code ingests it**
   (`ingest_scores.py record-dir`). That rule came from a real failure: a coordinator told to
   "save each reply verbatim" re-typed the replies instead, once hard-coding "no gates" for a
   whole batch. Anything the pipeline must keep verbatim is now written by the agent that
   produced it and read by code.
5. Copy each role's score onto its duplicate postings, re-check the scored jobs' text for
   sponsorship, citizenship and part-time lines that a scorer called generic, and push
   `claude/judge-score-<run>`.

Scoring was split out of a single combined routine (`judge-queue`, now retired) so that the
tailoring thread never carries 120 scorer replies in its context.

---

## Layer 2: tailoring

`tailor-queue` is a cloud Routine with an Opus main thread that fires three times a day. **The
main thread only coordinates.** It never drafts, revises or reviews a resume itself.

**Work selection is code, not prose.** `python pipeline/worklist.py tailor` returns the selected,
deferred and blocked jobs as JSON: the `auto-tailor` lane (or jobs a human promoted from the
dashboard), not already worked, no recent brief with the same title, not stale, one posting per
role, at most 2 per company per fire and 4 per rolling week, promoted jobs first, up to 15.
Spare slots go to `digest-stretch` jobs. Until that file existed the routine prompt described
these rules in a paragraph and the model re-derived the list by hand every fire, and two bugs
lived in that prose for a week. Rules in code get tests. Rules in prose get paraphrased.

**One fresh tailorer per job, three at a time.** Each selected job gets its own
`resume-tailorer` subagent in a fresh context, so the fifteenth resume of a run gets the same
attention as the first. That fixed a measured effect: when one thread tailored a whole batch,
jobs it reached late in a fire reviewed lower than the first few at the same scorer score
(`pipeline/fire_decay.py` is the instrument).

A subagent in the cloud has no Agent tool of its own, so it cannot launch reviewers. The
coordinator does that between rounds and then resumes the **same** tailorer with SendMessage,
which keeps its context. Per job, the tailorer follows `.claude/skills/resume-tailor/SKILL.md`:

1. Extract the posting's qualifications into a fixed basic and preferred list
   (`requirements.py extract`, no model), and print the tailor's view of it (`requirements.py plan`).
2. Write `brief.md`: what this hiring manager needs, a requirement map from each qualification to
   the bullet that will prove it (or GAP), the load-bearing terms in the posting, and the ideal
   candidate.
3. Draft the resume from the master resume and the evidence files in `profile/knowledge/`, in
   the posting's own vocabulary, written for the posting's reader rather than for the
   candidate's past employers (`pipeline/reader_terms.py` flags domain terms that reader may not
   know).
4. Build it into the template via `pipeline/docx_engine.py`. This operates on the OOXML as a
   string, byte-level surgery on `word/document.xml`, rather than through a document library,
   because the template's exact formatting is what makes the page fit.
5. Render with LibreOffice to exactly one page.
6. Run `pipeline/preflight.py`: mechanical checks only. Page fill measured against the
   document's own bottom margin, widow and hyphen checks, bullet length, repeated opening verbs,
   the fixed page shape and section order, words a PDF parser would never see, and the
   parse-back check described below.
7. Hand back to the coordinator, which launches the **four reviewers in parallel, each in a fresh
   context that sees only the job description and the rendered file**.
8. Revise by writing, then review again. Two rounds is normal and three is the cap, against
   targets of screen grade B or better (every basic qualification met), recruiter 8, hiring
   manager 8 and readability 7. A round that meets every target ends the run. A third round runs
   only when a target is missed and a reviewer ask is still one the record can answer. There is
   no FAIL state: the human reads the scores and decides.
9. Ship the folder listed at the top of this README, plus a sidecar `tailored/<id>.json`.

The coordinator commits each job as it finishes, starts no new job after five hours, and pushes
`claude/judge-tailor-<run>`. It never touches the database.

Both routines are told, in the prompt, that they operate autonomously: nobody can answer a
question mid-run, so they must never stop to ask one. They make the call, record it in the job's
`brief.md` or in the run report, and keep going.

---

## The ATS screen

The `ats` number used to be keyword coverage, and it was wrong in a measurable way. Across several hundred
tailored resumes it had a median of 85 and 78% of pages cleared the target, while literal
coverage of the must-haves on the same pages had a median of about 68%. On one games-industry
resume, "ran it live" counted as live-service games experience and an Interests line counted as
gaming experience: 94%, while the recruiter and hiring manager reviewers both gave it 4 out of 10.

The screeners that read a resume in 2026 (Workday HiredScore, Greenhouse Talent Matching, Ashby
AI-assisted review, LinkedIn Hiring Assistant) do not count keywords. They split the posting's
qualifications into basic and preferred items, judge each one against the resume with cited
evidence, and show the recruiter a coarse tier. So the screen now works the same way, split
between code and a model:

- **`requirements.py extract` (no model)** pulls the qualification bullets out of the posting
  before any drafting and tags each one basic or preferred. The list is fixed for the whole run,
  so the tailor cannot pick easier targets and every round grades the same items.
- **The `ats-scanner` agent (Sonnet)** judges each item met, partial or not met, with a verbatim
  quote from the page, and in round 1 picks the strings a recruiter would search.
- **`requirements.py grade` (no model)** checks that every quote is really on the page (a quote
  that is not there earns nothing), counts a "met" that rests only on a Skills or Interests line
  as partial, and computes a tier modelled on HiredScore's documented grades: A is all basic and
  most preferred, B is all basic, C is most basic, D is fewer. Basic items weigh double. Title
  and keyword findability are reported beside the grade, never blended into it.

The grade means "how a requirement-by-requirement screener would rate this page", not a
prediction of any one employer's outcome, since nobody outside can see whether an employer turned
on the AI add-on or how a recruiter weighted it. `docs/ats-screen.md` has the research, with
sources, and the known limits. Scores from before 2026-10-14 measure something else and are never
compared with scores after it.

---

## The fold

`pipeline/sync.py --ci`, run by `.github/workflows/fold.yml`. No model runs here.

It runs on every push to a `claude/judge*` branch, on every push to `main` that carries records
to fold (sidecars or application folders published by a local `/tailor` or `/scan`), and when
`detect.yml` dispatches it because dashboard decisions are waiting.

1. Take the `claude/judge*` branch family's sidecars, new application folders and the routine's
   notes file, and fold them into `pipeline/jobs.sqlite` and `main`.
2. Fold `decisions/pending.json`, which is what the dashboard's buttons and `/mark` write:
   applied, rejected, skipped, interview, offer, promote, cancel.
3. Freshness sweep, push `main`, delete the run branches that were folded, and point
   `claude/judge` back at `main`.
4. Publish the dashboard, if the fold moved `main`.

`applications/` is never checked out wholesale. The fold takes only folders that are absent on
`main` and vouched for by a sidecar on the branch. Submitted folders are history and are never
rewritten.

**A dashboard click starts no workflow.** It used to: one busy day of clicks started 86 folds and
cost more Actions minutes than detection. Now a click writes `decisions/pending.json`, every
fold applies whatever is pending, and each detect run asks `decisions.py --foldable` whether a
fold would change anything and dispatches one only then. A click lands within about two hours.

### Two renderers, two different questions

The PDF that gets submitted is always rendered by LibreOffice, because Word's PDF text layer
sprays spaces inside compound keywords ("real -time", "cross -functional") and an applicant
tracking system parses that text layer. Word's *pagination*, though, is what a human recruiter
sees when they open the docx, so a disagreement between the two is worth knowing about.

The retired laptop sync re-rendered each shipped resume with Word and raised a flag when the page
counts disagreed. It never downgraded a score and never overwrote the canonical PDF. The cloud
fold has no Word, so that cross-check is now a hand step: open the docx in Word before uploading
it. `pipeline/compare_renderers.py` is the tool that measured where the two engines disagree.

---

## The agents

| Agent | Model | Sees | Job |
|---|---|---|---|
| `score-queue` (main thread) | Sonnet 5.5, medium | the repo | runs a scoring pass, writes only `scores/` |
| `tailor-queue` (main thread) | Opus 5.5, high | the repo | coordinates a tailoring pass, never writes a resume |
| `job-scorer` | Sonnet, medium | one queue JSON, nothing else | fit out of 10, gates, a lane |
| `resume-tailorer` | Opus, high | one job, the evidence base, the template | writes one resume end to end and revises it between review rounds |
| `ats-scanner` | Sonnet, medium | the posting, the docx, the PDF | the requirement screen: met, partial or not met per qualification, with quotes, plus parse risks |
| `recruiter-screen` | Opus, medium | the posting and the PDF | a 6-second skim and a 60-second read, and the top fixes |
| `hiring-manager` | Opus, medium | the posting and the PDF | does this person solve my problem |
| `readability-screen` | Sonnet, medium | the posting and the PDF | does each line land in one pass for a reader who knows the posting's field but not the candidate's past employers |

The four reviewers are isolated on purpose. A reviewer that watched the resume being written
rates the intent rather than the artifact, so each one is launched fresh and handed only the
files a real reviewer would have. They run in parallel, and each writes a scored section that the
tailorer then has to answer with a rewrite or a one-line reason for refusing.

The readability reader is the newest. It was added after a count showed pipeline pages carrying
about 11 bank and enterprise terms each that their own posting never used: a strong result the
reader cannot parse on the first pass is worth nothing.

---

## Integrity design

This is the part that matters most, because a resume is a claim you have to defend out loud in
an interview.

- **The model may reframe, never invent.** It writes the strongest true framing of real work in
  the posting's own language, with an ownership verb where the record shows the person did the
  work. It states a result only when the record supports it, and names a role's routine duties
  plainly when the role makes them certain, without adding outcomes or numbers. It never fabricates numbers, employers, titles, dates, degrees or credentials. A figure
  that is the person's own estimate is written as one ("~N", "about N"), and nothing that never
  shipped is described as running.
- **Lines to defend.** When a reviewer calls a line an overclaim or a stretch, the tailor does not
  quietly soften it if the record supports it, and it does not quietly keep it either. The line
  stays on the page and is listed under `## Lines to defend` in `brief.md`, at most five per
  resume, so the human sees every flagged claim in one place and keeps or strikes each one
  before applying. Every reviewer ask also gets a line under `## Revision notes`, applied or
  refused with the file or rule behind the refusal, so nothing is dropped silently.
- **Inferred beyond the written record.** Every brief carries one line naming each claim that
  goes past what the evidence files literally say, or "none". The dashboard shows that line on
  the card, as the thing the human must verify.
- **A fixed list and verified quotes.** The qualification list is extracted by code before
  drafting and cannot be edited by the tailor, and the screen gives no credit for a quote that is
  not on the rendered page.
- **Parse-back.** `pipeline/parse_back.py` reads the *rendered* page back out of the PDF and
  diffs it against `record.json`, the canonical record of employers, dates and sanctioned title
  variants. A resume that invents a title, coarsens a date, or drops a role fails preflight. It
  checks the artifact that actually gets sent, not the draft the model believes it wrote.
  Preflight also reads a list of retired claims from the record and prints the sanctioned
  wording. `pipeline/testdata/record.example.json` shows the format.
- **The record beats the resume, and a person beats the record.** A real incident is recorded in
  `LEARNINGS.md`: a job title was wrong in `record.json` for five weeks and every generated
  resume inherited it. The rule that came out of it is that when the human states a title, fix
  `record.json` first, because parse-back reads it.
- **Lint hook.** `.claude/hooks/lint_generated.py` runs before any agent write to a generated
  file and blocks em-dashes, a ban list of filler vocabulary, and leaked `[FILL]` or `[VERIFY]`
  markers. It fires before the write lands, not after.
- **Self-linting writers.** A hook only covers agent tool calls, so `pipeline/screening_draft.py`
  imports the real hook module, never a copy of its regexes, and refuses to write a violation
  from a plain script.
- **Compliance answers are never auto-filled.** Work authorization answers come from a
  preferences file verbatim, through code rather than a model, and the visa line is deliberately
  left as a marker that a human has to complete at submission, rather than a model deciding to
  type a real immigration status into a committed file.
- **A human is the gate.** Nothing submits, sends, or marks a job applied.
- **The dashboard cannot leak.** The publish step asserts the deploy directory holds exactly one
  file, greps the built page for private-profile markers, and runs `pipeline/vercel_guard.py`
  before upload and again after, proving with an unauthenticated fetch that no production URL is
  anonymously readable. That guard exists because a canary page proved Vercel Authentication does
  not cover a project's short alias on the Hobby plan. Resume files are never uploaded: the page
  links to blob URLs in the private repository.

---

## Data model

There is no database server. State is files in a git repository, plus one committed SQLite file.

| Store | Who writes it | What it holds |
|---|---|---|
| `queue/<id>.json` | `detect.yml` only | one detected posting |
| `pipeline/jobs.sqlite` | exactly two writers, both GitHub Actions: `detect.yml` and `fold.yml` | the source of truth |
| `scores/<id>.json` | `score-queue`, `tailor-queue` when it re-lanes a job on its posting, or a local `/scan` | a score, a lane, a one-line rationale |
| `tailored/<id>.json` | `tailor-queue`, or a local `/tailor` | what was produced for a job, with its review scores |
| `applications/<folder>/` | `tailor-queue` or `/tailor`, then folded | the resume folder |
| `decisions/pending.json` | the dashboard's buttons and `/mark` | applied by the next fold, which rewrites it with whatever is still waiting |
| `.claude/routines/judge-queue.notes.md` | `tailor-queue` | short traps a later run should know |

The rule that keeps this coherent: **cloud stages and the laptop are sidecar-shaped.** They never
write the database. They write text files that the fold reconciles. A committed binary is only
safe because it has two writers that both run in Actions and both resolve a push race by
reset-and-redo, never by a rebase. Any new stage has to follow the same shape. `worklist.py` and
the audit tools open the database read-only.

`pipeline/test_sync_consistency.py` is where the invariant is defended, including push races,
retry that refuses to destroy hand-made commits, and orphaned autostash recovery.
`pipeline/test_localgit.py` covers the laptop side: a pull that never resets the working tree and
a commit stranded by a failed push.

---

## Model policy

Cost and latency are part of the design, so every agent file pins its model and effort
explicitly rather than inheriting from whatever session invoked it.

| Component | Model | Effort |
|---|---|---|
| `tailor-queue` main thread | Opus 5.5 | high, set in the Routines UI |
| `resume-tailorer` | Opus | high |
| `recruiter-screen`, `hiring-manager` | Opus | medium |
| `score-queue` main thread | Sonnet 5.5 | medium, set in the Routines UI |
| `job-scorer`, `ats-scanner`, `readability-screen` | Sonnet | medium |
| screening-draft prose | Sonnet | medium |

Writing and judgment a person will act on get the expensive model. Triage, the per-requirement
screen and the plain-language read get the cheaper one, and the arithmetic around them is code.
Pinning matters: without it, a hand-run tailor inherits whatever the interactive session was set
to and the reviewer scores stop being comparable between jobs.

---

## Scale

As of 2026-10-16, from the private database:

- 47,000+ postings screened since 2026-08-12, across 13,000+ companies
- about 9,500 postings scored by the `job-scorer` agent
- about 1,800 review rounds, each with four (earlier three) fresh-context reviewers
- about 1,900 commits in the private repository
- 24 test suites

---

## Repository map

Where to look, and what each thing is. The prompts are as much a part of this system as the
Python is, which is why they are versioned here rather than living only in a web UI.

| Path | What it is |
|---|---|
| `CLAUDE.md` | the operating brief the agent reads at the start of every session: the mission, the architecture, the rules that keep the data safe. Read this second, after this README |
| `.claude/routines/score-queue.md`, `tailor-queue.md` | the two cloud Routine prompts and their configuration, versioned. The fenced prompt block is the literal text configured at claude.ai |
| `.claude/skills/resume-tailor/SKILL.md` | the tailoring contract: how a resume gets written, rendered, reviewed and revised |
| `.claude/agents/*.md` | the six subagents. Each pins its own model and effort |
| `.claude/commands/*.md` | hand-run slash commands (`/scan`, `/tailor`, `/digest`, `/mark`) |
| `.claude/hooks/lint_generated.py` | the pre-write hook that blocks banned vocabulary in generated files |
| `.github/workflows/detect.yml` | Layer 1. The comments carry the measurements behind every scheduling decision |
| `.github/workflows/fold.yml` | the fold: `sync.py --ci` on every routine push, then the dashboard publish |
| `.github/actions/publish-dashboard/action.yml` | builds the dashboard and publishes it, with the leak guards. A step inside detect and fold |
| `.github/workflows/dashboard.yml` | rebuilds the dashboard by hand or when its own code changes |
| `pipeline/scrape.py`, `filter.py`, `db.py` | detection, hard filters, the database and dedupe keys |
| `pipeline/bigtech.py`, `vc_boards.py` | the six big-tech career-site fetchers, and the Getro and Consider VC board readers |
| `pipeline/freshness.py` | the 72-hour and delisting sweep |
| `pipeline/worklist.py` | both routines' work lists (score, siblings, tailor), with the caps, in tested code |
| `pipeline/ingest_scores.py` | turns a scorer's reply file into its sidecar, and folds sidecars into the database |
| `pipeline/requirements.py` | the requirement screen: extract, plan, packet, grade |
| `pipeline/docx_engine.py` | builds the resume into the template by editing OOXML directly |
| `pipeline/preflight.py`, `parse_back.py` | the mechanical gate and the parse-back integrity check |
| `pipeline/reader_terms.py` | domain terms a posting's reader may not know, with a true plain gloss for each |
| `pipeline/ats_score.py`, `screening_draft.py` | the old keyword counter (kept as a reference), and the prefilled screening answers |
| `pipeline/sync.py` | the fold: sidecars, folders and decisions into the database, sweep, push |
| `pipeline/decisions.py`, `mark.py` | folding dashboard decisions, and `/mark`, which writes the same entry a button writes |
| `pipeline/localgit.py` | keeps the laptop a client of `main`: pull before a local command, publish text records after |
| `pipeline/schedule.py` | the single owner of every schedule in the chain |
| `pipeline/register_sync_task.ps1` | the retired laptop sync's Task Scheduler registration, kept for reference |
| `pipeline/routine_parity.py` | prints the exact prompt to send to a Routine, and diffs a live prompt against the file |
| `pipeline/dashboard.py`, `dashboard_assets/` | the published page and its source CSS and JS |
| `pipeline/vercel_deploy.py`, `vercel_guard.py` | the dashboard deploy through Vercel's REST API, and the guard that proves the page is not public |
| `pipeline/audit.py` | the health check as one command: detection cadence and lag, expired jobs by reason, company and role-family shares |
| `pipeline/usage_deltas.py` | the quota cost of each routine fire, from a local rate-limit log, since nothing else attributes plan usage to a Routine |
| `pipeline/fire_decay.py` | whether resumes tailored later in a fire review lower than earlier ones at the same scorer score |
| `pipeline/evidence_asks.py` | ranks the facts reviewers keep asking for across all briefs, so the next fact-gathering session starts from the most frequent gaps |
| `pipeline/test_*.py` | the suite. `test_sync_consistency.py` is where the git and database invariants are defended |
| `docs/schedule.md` | the live schedule, generated from `schedule.py` and diffed against it by a test, plus the external cron setup |
| `docs/ats-screen.md` | why the ATS number changed from keyword coverage to a requirement screen, with sources |
| `docs/dashboard-actions.md` | how the dashboard buttons work and how to set up the token they use |
| `docs/layer2-v3.md` | the Layer 2 flow and output contract in one page |
| `docs/rebuild-plan.md` | the real build tracker, including the measured results of each test run |
| `LEARNINGS.md` | operational traps, append-only. Short, and the most useful file in the repo |

---

## Setting it up yourself

Nothing here is specific to one person except the content you supply. Expect an afternoon.

### Prerequisites

| Need | Why | Optional? |
|---|---|---|
| A GitHub account and a fork of this repo | detection and the fold run on Actions, and the repo is the message bus | required |
| Python 3.12 | JobSpy pins a NumPy version with no 3.13 wheel | required |
| A Claude subscription with Routines | Layer 2 is two scheduled cloud agents | required for automation |
| A free cron-job.org account | GitHub's own scheduler delivers too few fires to rely on | strongly recommended |
| LibreOffice Writer | renders every shipped PDF. The tailoring Routine installs it in the cloud itself, so you need it locally only for `/tailor` | optional locally |
| Microsoft Word | a hand page check before uploading a docx | optional |
| A Vercel account | hosts the dashboard behind authentication | optional, the page builds locally |

Nothing has to run on a schedule on your own machine.

**Make your fork private if you are putting real career history in it.** The dashboard links to
blob URLs in the repo, and the access control is that the repo is private.

### 1. Supply your own profile

None of this is in the public copy. Create these:

| Path | What goes in it |
|---|---|
| `profile/knowledge/` | one Markdown file per role or project, plus a skills file and attested facts. This is the evidence base the tailor draws on, and it should be far larger than a resume |
| `profile/knowledge/record.json` | canonical employers, dates and sanctioned title variants. Start from `pipeline/testdata/record.example.json` |
| `profile/master-resume/*.md` | a superset resume, everything you might ever claim, from which each tailored page is cut down |
| `profile/template/*.docx` | your one-page resume template. The only starting document, and the formatting is load-bearing |
| `profile/preferences.md` | targeting, availability, and verbatim screening answers. Start from `pipeline/testdata/preferences.md` |
| `profile/voice.md` | how the writing should read, so the output sounds like you |
| `profile/targets.yaml` | the board list. This one IS in the repo. Edit it |

Two filenames are hardcoded to the original author and need changing: `pipeline/docx_engine.py`
globs for a resume filename prefix built from the author's name, and `pipeline/render_master_md.py`
points at a named master resume. The prompts in `.claude/` also name the author and the template
file, so read them through once and swap in your own.

### 2. Install and check locally

```bash
pip install -r pipeline/requirements.txt          # Layer 1
pip install -r pipeline/requirements-tailor.txt   # tailoring
pip install -r pipeline/requirements-dev.txt      # tests

# Linux: apt-get install -y libreoffice-writer
# Windows: install LibreOffice normally. docx_engine.py finds soffice.exe in Program Files

python -m pytest pipeline -q -rs --ignore=pipeline/test_usage_deltas.py
python pipeline/scrape.py                         # one detection pass by hand
python pipeline/dashboard.py --out publish/index.html
```

Open `publish/index.html`. If the board renders, Layer 1 and the dashboard work.

### 3. Turn on detection

**Edit `profile/targets.yaml` before the first run.** The list shipped here is somebody else's
target companies, and detection will happily fill your queue with them.

Then push your fork. Give workflows write permission to contents (Settings, Actions, General,
Workflow permissions). `detect.yml` also asks for `actions: write` in its own file, which it uses
to dispatch a fold when dashboard decisions are waiting. Run `detect` once from the Actions tab
and confirm files land in `queue/`.

LinkedIn and the logged-in Jobright reader stay off unless you turn them on yourself. Whether a
site allows that is your call to make, under its terms, not this repo's.

### 4. Trigger detection from outside

`docs/schedule.md` has the full steps. In short: create a fine-grained GitHub token for your fork
with Actions read and write and nothing else, then a cron-job.org job that POSTs to

```
https://api.github.com/repos/<owner>/<repo>/actions/workflows/detect.yml/dispatches
```

at minute 17 of every odd UTC hour, with the headers `Authorization: Bearer <token>`,
`Accept: application/vnd.github+json` and `X-GitHub-Api-Version: 2022-11-28`, and the body
`{"ref":"main","inputs":{"respect_gate":"true"}}`. Success is HTTP 204. After a day,
`python pipeline/audit.py` should show 10 or more detect runs a day. When the token expires,
detection falls back to the sparse GitHub backup and the dashboard's Layer 1 clock goes red.

### 5. Create the two cloud Routines

At `claude.ai/code/routines`, create two Routines pointed at your fork. Each one's prompt is the
fenced prompt block in its file, and `python pipeline/routine_parity.py <name> --emit` prints the
exact bytes to paste.

```
name              score-queue
cron_expression   55 3,7,11,15,19,23 * * *
model             claude-sonnet-5-5
effort            medium                      (set in the Routines UI)
allowed_tools     Bash, Read, Write, Edit, Glob, Grep, Agent, Task
branch            claude/judge-score

name              tailor-queue
cron_expression   30 8,17,22 * * *            (from schedule.py --print for your windows)
model             claude-opus-5-5
effort            high                        (set in the Routines UI)
allowed_tools     Bash, Read, Write, Edit, Glob, Grep, Agent, Task, SendMessage, WebFetch, WebSearch
branch            claude/judge-tailor
```

Routine crons are fixed UTC. Trigger each one once by hand and check its branch diff:
`score-queue` should touch only `scores/`, and `tailor-queue` only `scores/`, `tailored/`,
`applications/` and its notes file. If either touches anything else, stop and fix it before
scheduling.

When you later edit a prompt, edit the file, send it with `RemoteTrigger update` and push `main`
in the same session, because the next fire clones remote `main` and the two have to agree. The
skill file and the `tailor-queue` prompt must name the same reviewer set at fire time, or a
tailorer waits for a review the coordinator never launches.

### 6. The fold

Nothing to set up beyond the workflow permissions in step 3. `fold.yml` triggers itself on every
routine push. Set a calendar reminder for the daylight saving change: the offset in
`pipeline/schedule.py` moves and both Routine crons have to be re-sent, or every window drifts by
an hour.

### 7. Publish the dashboard (optional)

Add `VERCEL_TOKEN`, `VERCEL_ORG_ID` and `VERCEL_PROJECT_ID` as repository secrets, and set
`PROD_ALIAS` to your team-scoped Vercel alias in `detect.yml`, `fold.yml` and `dashboard.yml`. The
placeholder shipped here is not a real host and `vercel_guard.py` will refuse to publish until
you replace it.

Configure the Vercel project with **zero project domains** and Vercel Authentication on. The
guard asserts that on every deploy, and it is the only thing standing between a page listing
your entire job search and the open internet.

The buttons on the page write to your repo through a fine-grained token that you paste into the
page once and that stays in your browser. `docs/dashboard-actions.md` has the steps.

Without any of this the workflows still build the page and attach it as a run artifact.

### 8. Day to day

```
/scan                    one local Layer 2 pass by hand
/tailor <id>             one job end to end
/digest                  status counts and the ready list
/mark <id> applied       update a job's status
```

Local commands never write the database. They start with `python pipeline/localgit.py begin`
(refuse dirty pipeline paths, pull) and end with `localgit.py publish`, which pushes their text
records to `main`, where the fold picks them up.

---

## Tests

```bash
pip install -r pipeline/requirements-dev.txt
python -m pytest pipeline -q -rs --ignore=pipeline/test_usage_deltas.py
```

Most suites are hand-rolled scripts you can also run one at a time, for example
`python pipeline/test_filter.py`. Some expose `test_*` functions that pytest collects directly.
Twelve script-style suites keep most of their checks in a `main()`, so `pipeline/test_script_suites.py`
runs each in its own subprocess and reports the exit code. Without that bridge a bare pytest run
collects nothing from those checks and still prints green, which is exactly the kind of false pass
this repository is otherwise built to prevent.

Four newer script suites are not in the bridge's list yet, so run them directly:

```bash
python pipeline/test_bigtech.py
python pipeline/test_localgit.py
python pipeline/test_worklist.py
python pipeline/test_usage_deltas.py   # runs its checks at import, which is why pytest skips the file
```

Suites that need a private file skip with a stated reason rather than passing quietly. In this
public copy that is the docx render-fidelity suite (needs your template plus a renderer), the
record-anchored diff legs of `test_parse_back.py`, the page-margin reader and the retired-claims
leg in `test_preflight.py`, the template legs of `test_ats_score.py`, and the `applications/`
sweeps in `test_screening_draft.py`. Supply your own profile files and they light up.

---

## What is deliberately not here

Removed because it is personal data, not code:

- `profile/knowledge/`, `profile/master-resume/`, `profile/template/`, `profile/preferences.md`,
  `profile/voice.md`, `profile/network/`, `profile/archive/`
- `queue/`, `scores/`, `tailored/`, `applications/`, `decisions/`, `pipeline/jobs.sqlite`
- `pipeline/keyword_corpus/`, generated from a scraped corpus of job descriptions
- `pipeline/.env`, `pipeline/sync.log`, `pipeline/sync_state.json`
- `docs/history/`, the improvement-plan tracker, and the Routine's live notes files

Contact details and the private dashboard URL are scrubbed. The dashboard is access-controlled
and its URL is not useful to anyone else.

Because those paths are gone, a few documents reference files you will not find here:
`docs/dashboard-hosting.md`, `docs/improvement-plan-2026-10.md`, `docs/calibration-2026-08-22.md`,
`docs/history/routines/` (where the retired `judge-queue` prompt lives), `profile/attestations.md`,
`profile/keywords.md`. Dashboard hosting is covered by step 7 above.

Some documents also still describe earlier stages of the system, because they are dated records
rather than live contracts: `LEARNINGS.md` is append-only, and parts of `docs/dashboard-actions.md`,
`docs/layer2-v3.md` and the `sync.py` docstring predate the move of the fold to Actions. Where they
disagree with this README, the routine files, the skill, the agent files and the workflows are the
source of truth.

The prompts and documentation still refer to `profile/` paths and to the author by name. That is
the product's design, not an oversight: they are instructions to an agent about whose evidence
base to read. The employment history that appears in `CLAUDE.md`, the agent prompts and some test
fixtures is public information from the resume this repository is linked from, and it is kept so
the prompts read as the real thing rather than as a hollow template.

`docs/rebuild-plan.md` is the real build tracker, kept because the measurements in it are the
useful part. Company names in its test-run entries are replaced with `Company A/B/C`.
