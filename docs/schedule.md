# Layer 2 schedule

**Generated facts live in `pipeline/schedule.py`, not here.** The block below is
`python pipeline/schedule.py --profile home --print` pasted verbatim, and
`pipeline/test_schedule.py` diffs it against the code on every run.

## The chain (rebuilt 2026-09-16)

| Stage | Where | When | What |
|---|---|---|---|
| `detect.yml` | GitHub Actions, triggered by cron-job.org | every 2h at :17 past odd UTC hours (backup: GitHub cron `17 */4`) | feeds `queue/` on `main`; lands 15-75 min before each routine fire; publishes the dashboard as its last step (10-08) |
| `score-queue` | cloud Routine (Sonnet 5.5) | every 4h at :55 (03-23 UTC, odd hours), 38 min after a detect run | scores unscored queue files with one `job-scorer` per file, copies scores to sibling postings, re-checks gates; writes only `scores/` to `claude/judge-score-<run>` (W4, 10-11) |
| `tailor-queue` | cloud Routine (Opus 5.5 coordinator) | W-3:30 | overlays every `claude/judge*` branch, takes `worklist.py tailor`, runs one fresh `resume-tailorer` subagent per job (up to 3 at once) and the three reviewers between its rounds; pushes `claude/judge-tailor-<run>`. Replaced `judge-queue` 10-11 |
| `fold.yml` | GitHub Actions, on every `claude/judge**` push, every push to main carrying `scores/`, `tailored/` or `applications/`, and a dispatch from `detect.yml` when `decisions/pending.json` has something to fold | minutes after each routine run or local command; dashboard clicks and `/mark` within ~2h (W7, 10-13: a click starts no workflow) | `sync.py --ci`: folds sidecars, folders and decisions into the DB, sweeps, pushes; no Word check; publishes the dashboard when it moved main (added 10-06; sole DB fold since 10-08) |
| `sync` | laptop (Task Scheduler) | RETIRED 10-08 (task disabled, kept as a backup) | its `reset --hard` push-race recovery wiped uncommitted laptop edits once `fold.yml` started winning the race. Local commands now pull and publish themselves (`pipeline/localgit.py`) |
| Vaish | dashboard | W | reviews "Ready to submit", applies, marks |

Each sync trigger repeats every 15 min for an hour; that repetition is the retry
mechanism (Windows does not restart a task on a nonzero exit), and `sync.py` exits in
~0.2 s when a run inside `RETRY_WINDOW_MIN` already succeeded.

Two clocks: routine crons are fixed UTC; Task Scheduler follows the laptop clock,
which stays Eastern.

## Profile `home` -- ACTIVE

```
profile: home  (America/New_York, UTC-04:00; laptop clock UTC-04:00)
note:    ACTIVE. EDT; after 2026-11-01 set LAPTOP_UTC_OFFSET_H=-5 and re-send the cron (see docs/schedule.md)

windows (local): 08:00 / 17:00 / 22:00
windows (UTC):   12:00 / 21:00 / 02:00

cloud Routines (cron is fixed UTC):
  tailor-queue  W-3:30  UTC 08:30 / 17:30 / 22:30     cron  30 8,17,22 * * *
  score-queue   every 4h  UTC 03:55 / 07:55 / 11:55 / 15:55 / 19:55 / 23:55  cron  55 3,7,11,15,19,23 * * *

Task Scheduler triggers for "job-search-os Layer 2 sync"
(laptop clock; each repeats every 15 min for 1h):
  sync          W-1:00  UTC 01:00 / 11:00 / 20:00     local 07:00 / 16:00 / 21:00
  all, sorted:  07:00  16:00  21:00
```

## External detect cron (set up once, by Vaish)

GitHub's own scheduler delivered about 12% of detect fires for 30 days (6 of 48 a day,
gaps up to 6.5h), so detection is triggered from outside. Two parts:

1. **Token.** github.com -> Settings -> Developer settings -> Fine-grained tokens ->
   Generate. Repository access: only `<owner>/<repo>`. Permissions:
   Actions **Read and write** (nothing else). Expiry: the longest offered; note the date.
2. **cron-job.org job.** Create a free account, then a cronjob:
   - URL `https://api.github.com/repos/<owner>/<repo>/actions/workflows/detect.yml/dispatches`
   - Schedule: custom, timezone UTC, minute `17`, hours `1,3,5,7,9,11,13,15,17,19,21,23`
   - Advanced: method `POST`; headers `Authorization: Bearer <token>`,
     `Accept: application/vnd.github+json`, `X-GitHub-Api-Version: 2022-11-28`;
     body `{"ref":"main","inputs":{"respect_gate":"true"}}`
   - Success is HTTP 204. Turn on failure notifications.
3. **Check.** Run the job once by hand, then `gh run list --workflow detect.yml --limit 3`
   shows a `workflow_dispatch` run. After a day, `python pipeline/audit.py` should show
   10+ detect runs a day.

`respect_gate=true` sends these calls through the 40-minute gate like scheduled fires; a
manual dispatch from the Actions tab leaves it off and always runs. When the token
expires, detection falls back to the sparse GitHub backup (about 1-2 runs a day) and the
dashboard's Layer 1 clock goes red: renew the token and paste it into the job.

## Applying a change

1. Edit the windows or offsets in `pipeline/schedule.py`.
2. `python pipeline/schedule.py --profile home --apply` (rewrites the Task Scheduler triggers).
3. In a Claude session: `RemoteTrigger update` tailor-queue and score-queue with the printed crons.
4. Paste the new `--print` block above.

## DST

`LAPTOP_UTC_OFFSET_H` is **-4 (EDT)**. On **2026-11-01** America/New_York returns to
EST (UTC-5): set it to -5, re-run `--apply`, and re-send the routine cron, or the
windows drift an hour late.
