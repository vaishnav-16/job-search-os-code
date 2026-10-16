# Routine: score-queue (cloud scoring, every 4h)

Versioned copy of the prompt configured at claude.ai/code/routines. Edit here, then send
with `RemoteTrigger update` in the same session and push `main` in the same session (the
next fire clones remote main). `python pipeline/routine_parity.py score-queue --emit`
prints the exact bytes to send; after the update, save a `RemoteTrigger get` read-back to
`score-queue.live.txt`.

```
name              score-queue
cron_expression   55 3,7,11,15,19,23 * * *   # from `python pipeline/schedule.py --profile home --print`
model             claude-sonnet-5-5
effort            medium                     # set in the routines UI, not exposed by the API
sources           https://github.com/<owner>/<private-repo>
allowed_tools     Bash, Read, Write, Edit, Glob, Grep, Agent, Task
branch            claude/judge-score         # the run pushes claude/judge-score-<run>; fold.yml takes claude/judge*
```

Split out of `judge-queue` in W4 (2026-10-11) so the tailoring thread never carries 120
scorer replies. It writes only `scores/`; `tailor-queue` reads them (folded to main, or by
overlaying this routine's branch).

## Prompt

```text
You are running score-queue for Vaish's PM job-search pipeline: score the jobs detected since the last run and publish the score sidecars to your branch. Another routine (tailor-queue) tailors from them. Operate autonomously; never stop to ask a question. Your last message is a short report.

BOUNDARIES (breaking one corrupts the pipeline)
- Write only inside scores/. Never modify pipeline/jobs.sqlite, queue/, tailored/, applications/, profile/ or .claude/. Only GitHub Actions writes the database; it folds your branch minutes after you push.
- Commit to the branch this session designates (claude/judge-score-<run>). Never push to main.
- Never run pipeline/scrape.py, sync.py, dashboard.py, freshness.py --apply, ingest_scores.py --apply, or anything else that writes the DB. If the repo looks wrong, say so in the report and stop.
- Subagents: job-scorer only, one queue file each. Do not score jobs yourself.

STEP 0 - record the UTC start time.

STEP 1 - overlay earlier routine output, oldest first, so you never re-score what a run not yet folded already scored:
for b in $(git ls-remote --heads origin 'claude/judge*' | awk '{print $2}' | sed 's#refs/heads/##'); do git fetch origin $b 2>/dev/null && echo "$(git log -1 --format=%ct origin/$b) $b"; done | sort -n | while read -r _ b; do git checkout origin/$b -- scores tailored applications 2>/dev/null; done

STEP 2 - score. python pipeline/worklist.py score prints the work list as JSON (unscored, not tailored, not provably stale, one posting per role, capped at 120). If it is empty, report that and stop. Run mkdir -p /tmp/score-replies. Launch one job-scorer subagent per listed queue file, in the background, at most 10 running at once, each with exactly this prompt and nothing else:
queue/<id>.json
Reply file: /tmp/score-replies/<id>.md
Each scorer writes its own reply to that file. Never read, copy, summarize or re-type a scorer's reply yourself: the files are the record. When every scorer has finished, run python pipeline/ingest_scores.py record-dir /tmp/score-replies: it records each file verbatim (the Total, GATES, STRETCH, NOTE and SCORE lines) into scores/<id>.json and prints the ids to relaunch. A listed job with no reply file also counts as failed. Relaunch each failed scorer once (delete its file first), then run record-dir again; a job that fails twice stays unscored and is named in the report.

STEP 3 - python pipeline/worklist.py siblings (copies each role's score onto its duplicate postings).

STEP 4 - gate re-check. For every sidecar you wrote this run whose note starts auto-tailor or digest-stretch, grep its queue file's jd_markdown (case-insensitive) for: sponsor, visa, citizen, clearance, part-time, intern. Scorers have called work-authorization lines generic. If a line rules Vaish out (a work-authorization term that conflicts with profile/preferences.md, clearance only, part-time, an internship starting before January 2027 or in summer 2027 or later), re-lane it. Contract, fixed-term and staffing-agency roles are wanted (Vaish 10-15) and are never re-laned for that. An internship starting January 2027 or rolling is wanted (Vaish 10-13) and stays as scored; one with no start date stated is re-laned the same way with 'digest; <reason>' in place of 'skip; <reason>':
python -c "import sys; sys.path.insert(0,'pipeline'); from ingest_scores import write_sidecar; write_sidecar('<id>', <score>, 'skip; <reason>')"
and also rewrite every scores/*.json whose note says "duplicate role of <id>" the same way.

STEP 5 - publish:
git add scores && git commit -q -m "score: <N> scored @ <UTC>" || true
git push -u origin HEAD:<designated branch>
If the push is rejected, fetch, re-run STEP 1, and push once more; if it fails a second time, report it and stop.

STEP 6 - report in at most ten lines: jobs scored and the lane split (auto-tailor, digest-stretch, digest, skip), jobs gated by the scorer, jobs re-laned in STEP 4 and why, scorers that failed, the branch pushed, wall time.
```
