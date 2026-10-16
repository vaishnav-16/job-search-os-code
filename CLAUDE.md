# job-search-os — Claude Code instructions (v3, 2026-09-16)

## Mission
Find fresh PM and product-adjacent roles every day, tailor the strongest honest one-page
resume for each one Vaish can realistically get (0-4 years asked), get each resume reviewed
by an ATS screen, a recruiter and a hiring manager, and hand him a dashboard of
ready-to-submit resumes with their scores. He reads every resume, fixes what he cannot
defend, and applies by hand. Target: a full-time PM offer by December 2026. Progress tracker for the rebuild: `docs/rebuild-plan.md`. The improvement plan and
history docs it works from are private and not in this copy.

## How it works
- **Layer 1, detection (GitHub Actions, no LLM).** `.github/workflows/detect.yml` polls ~70
  ATS boards (`profile/targets.yaml`), six big-tech career sites (`pipeline/bigtech.py`) plus JobSpy and a curated feed every 2h (cron-job.org dispatch; the GitHub `schedule:` is a
  sparse backup), dedupes
  (SHA1 of company + title + location bucket), hard-filters (`pipeline/filter.py`: 6+ years
  required, Director/Staff/Principal, a Senior/Sr/III title whose JD states no years,
  marketing/sales PM, non-US, no-sponsorship, clearance),
  keeps postings under 72h, and commits net-new jobs to `queue/` plus `pipeline/jobs.sqlite`.
  Indeed was dropped 10-09. Reposts are recorded as skipped: Jobright by its own `repost`
  flag, LinkedIn by a job ID trailing the run's newest by ~3+ days (the tag is hidden
  logged out), and any aggregator row whose role's newest copy on a polled full board
  (Greenhouse/Lever/Ashby) is past 72h (10-15). Since 2026-10-08: LinkedIn (off in this public copy, with no date sort logged out) is read relevance-first, up to 100 a term, in an 8h+
  window, fetching JD pages only for new on-target rows; startup roles come from 12 Getro and
  11 Consider VC portfolio boards (`pipeline/vc_boards.py`); Vaish's own Jobright feed is read
  with his session cookie (`JOBRIGHT_COOKIE` secret; skipped when unset, warns when expired; not wired up in this public copy).
  A fresh posting of a role whose row aged out unworked is revived into the queue
  (`db.is_revivable`). detect.yml and fold.yml publish the dashboard as their last step.
- **Layer 2, judgment (two cloud routines since W4, 2026-10-11; prompts in `.claude/routines/`).**
  `score-queue` (Sonnet 5.5, every 4h) scores unscored queue files with `job-scorer`
  subagents (each reply goes through `ingest_scores.py record`, which turns its GATES
  line into a skip and a strong 5-year or 3-years-of-PM ask into `digest-stretch`; its company
  criterion ranks tech, startups, fintech and banks above other enterprises) and writes only
  `scores/`. `tailor-queue` (Opus 5.5, 3x/day) only coordinates: it tailors up to 15 jobs from
  the `auto-tailor` lane (spare slots go to `digest-stretch` jobs), each in a fresh
  `resume-tailorer` subagent that follows the `resume-tailor` skill, up to 3 at once. A subagent
  has no Agent tool in the cloud, so the coordinator runs the four reviewers between rounds and
  resumes the tailorer with SendMessage. Both work lists, including the company cap, come from
  `pipeline/worklist.py`. Each routine pushes to its designated branch (`claude/judge-score-<run>`,
  `claude/judge-tailor-<run>`; the family `claude/judge*` is one branch to every reader) and
  overlays that family at start, so it never depends on the laptop. `judge-queue`, which did
  both in one thread, is retired (`docs/history/routines/`).
- **Fold (`pipeline/sync.py`).** `.github/workflows/fold.yml` runs `sync.py --ci` on every routine
  push, on every push to main that carries sidecars or folders (`/tailor`, `/scan` from the laptop),
  and when detect.yml dispatches it because dashboard or `/mark` decisions are waiting
  (`decisions.py --foldable`; since W7, 10-13, a click starts no workflow and lands within ~2h). The laptop Task Scheduler sync is retired (task disabled 10-06; do not
  re-enable: its push-race `reset --hard` wipes uncommitted edits); a fold has no Word page check, so open a .docx in Word before uploading one. It folds the
  branch's sidecars, new folders and the routine's notes file into the DB and main, folds `decisions/pending.json` (applied,
  rejected, skipped, interview, offer, promote, cancel), freshness sweep, push `main`, then
  deletes the folded run branches and points `claude/judge` back at `main`, then publishes the
  dashboard in the same job. `docs/schedule.md` owns the times.
- **Dashboard** (`pipeline/dashboard.py`, published to Vercel): "Ready to submit" sorted by
  review scores; buttons write `decisions/pending.json`, which the fold applies (mark applied /
  rejected / skipped / interview / offer, or promote a digest job into the tailor lane).

## Tailoring in one paragraph
Per job the skill extracts the JD's qualifications into a fixed basic/preferred list
(`pipeline/requirements.py extract`), writes `brief.md` (what this hiring manager needs, a
requirement map from each item to the bullet that will prove it, the JD's load-bearing terms,
the ideal candidate), drafts the resume from
`profile/master-resume/Vaishnav_Ajai_Master_PM_Resume.md` and the evidence in
`profile/knowledge/` into the template docx via `pipeline/docx_engine.py`, renders with
LibreOffice to exactly one page, runs `pipeline/preflight.py` (mechanical checks only),
then four fresh-context reviewers in parallel (`ats-scanner` Sonnet, `recruiter-screen`
Opus, `hiring-manager` Opus, `readability-screen` Sonnet, since W9 10-16: a reader who knows the JD's field, not banking; target 7), revises by writing, and ships docx + PDF + `review.md`
(ending `SCORES: ats NN / recruiter N / hm N`) + `screen.json` + `screening-draft.md` + the
sidecar. The ATS screen emulates 2026 AI screeners (HiredScore, Greenhouse, Ashby): each
qualification met / partial / not met with a quote the grader verifies on the page, tier
A-D, `ats` = weighted screen score (`docs/ats-screen.md`). Full contract:
`.claude/skills/resume-tailor/SKILL.md` and `docs/layer2-v3.md`.

## Integrity, in one paragraph
The model writes the strongest true framing of work Vaish did, in the JD's own words. It
states a result only when the record supports it. It names a role's routine duties plainly
when the role makes them certain, without adding outcomes or numbers. It mirrors the JD's
vocabulary where that is the honest word. It never fabricates numbers, employers, titles or
dates, and an estimate is written as one ("~N", "about N"). Any line a reviewer questions
goes under `## Lines to defend` for him to verify or strike before he applies. `profile/knowledge/record.json` holds the dates and the sanctioned title
variants, and preflight's parse-back check diffs the rendered page against it. Vaish is the
final gate and strikes what he cannot defend. Nothing in the pipeline submits, sends, or
marks a job applied.

## Rules that keep the data safe
- `pipeline/jobs.sqlite` is a committed binary written only by GitHub Actions (`detect.yml`, and
  `fold.yml` running `sync.py --ci`), each resolving a push race by reset-and-redo, never a rebase.
  The laptop is a client: local commands start with `python pipeline/localgit.py begin` and end by
  publishing text records (sidecars, folders, `decisions/pending.json`) with `localgit.py publish`;
  the fold applies them. Never write the DB on the laptop. Cloud routines write
  sidecars (`scores/<id>.json`, `tailored/<id>.json`), never the DB. Any new cloud stage is
  sidecar-shaped.
- On the laptop, `applications/` is never checked out wholesale: sync takes only folders
  absent locally and vouched for by a sidecar on the branch. (The routine overlays its own
  branch, which is fine: that branch holds only its own output.) Submitted folders are
  history; never rewrite them.
- A `RemoteTrigger update` to the routine and the push of `main` that carries what it
  references are one step, done in the same session.
- The lint hook (`.claude/hooks/lint_generated.py`) blocks em-dashes, ban-list words and
  `[FILL]` in generated files under `applications/` (jd.md, review*.md and brief.md exempt).

## Model policy
Routine main thread: Opus 5.5 (`claude-opus-5-5`) at effort high (set in the routines UI;
Vaish's call 09-23, cost accepted). Reviewers
`recruiter-screen` and `hiring-manager`: Opus at medium. `job-scorer`, `ats-scanner`, `readability-screen`,
screening-draft prose: Sonnet at medium. Fable is
for one-off high-leverage authoring in an interactive session (the master PM resume, the
knowledge base review), never per job. Every subagent file pins its model and effort
explicitly: Opus reviewers at medium, Sonnet agents at medium, so a local `/tailor` never
inherits the session's effort setting.

## Layout
```
profile/knowledge/      evidence base (one file per role/project, skills, attested facts, record.json)
profile/master-resume/  the master PM resume (md is canonical; docx/pdf for review)
profile/template/       the one-page docx template (the only starting point)
profile/voice.md        how the writing should read
profile/preferences.md  targeting, availability, verbatim screening answers
profile/keywords.md     market vocabulary from a JD corpus (reference)
profile/archive/        retired sources; not read by the tailor
pipeline/               scrape, filter, db, sync, dashboard, docx_engine, preflight, requirements (ATS screen), ats_score, screening_draft, schedule
queue/ scores/ tailored/ applications/ decisions/   pipeline state
.claude/agents|skills|routines|commands|hooks       the prompts
docs/rebuild-plan.md docs/layer2-v3.md docs/schedule.md docs/history/
```

## Commands
`/scan` local Layer 2 pass · `/tailor <id>` one job end to end · `/digest` status counts
and the ready list · `/mark <id> <applied|skipped|rejected|interview|offer>`.

## Learning
Health check: `python pipeline/audit.py` (detection cadence and lag, expired auto-tailor jobs
by reason, company and role-family shares). Quota per routine fire: `python pipeline/usage_deltas.py` (reads the rate-limit log the status
line keeps; nothing else attributes plan usage to a cloud routine).

`LEARNINGS.md` holds operational traps only, one or two lines each, append-only. Read it at
session start; add to it when a run hits something the next run must know.
