---
description: Update a job's status (applied/skipped/rejected/interview/offer)
argument-hint: <job-id> <applied|skipped|rejected|interview|offer>
---

Update one job's status: `$ARGUMENTS` = `<job-id> <status>`.

Run `python pipeline/mark.py $ARGUMENTS` and relay its one line (company, title, old -> new).

It pulls main, records the change in `decisions/pending.json` (the same entry a dashboard
button writes, tagged `via: mark`), and pushes. Since W7 (10-13) that push starts no workflow:
the next fold applies it (a routine push, or the one `detect.yml` dispatches when decisions are
waiting), so within about 2h. The fold clears the job's queue file and score
sidecar for applied/skipped/rejected. The laptop never writes `pipeline/jobs.sqlite`.

If it prints `STOPPED:`, relay the reason; it has changed nothing it could not finish.
This command records what Vaish did by hand; it never submits anything.

Transitions the dashboard has no entry for (un-skipping a job) are not supported here;
say so rather than editing the DB.
