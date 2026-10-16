---
description: Local Layer 2 pass - score what the cloud missed, tailor qualifiers, publish for the cloud fold, digest
---

Run one local Layer 2 pass. The cloud `score-queue` (every 4h) and `tailor-queue` (3x/day)
routines normally do the scoring and tailoring; this command is for catching up by hand. The laptop never
writes `pipeline/jobs.sqlite`: it publishes sidecars and folders to main, and `fold.yml`
folds them in GitHub Actions within a few minutes.

0. `python pipeline/localgit.py begin`. If it prints `STOPPED:`, relay the reason and stop.
1. Score: `python pipeline/worklist.py score` lists the queue files to score (one per role,
   newest first). One `job-scorer` subagent (Sonnet) per listed file, in parallel batches of
   up to 8, each given only its queue JSON path. Save each reply verbatim to a file and run
   `python pipeline/ingest_scores.py record <id> <file> --scorer local`, which reads the
   GATES / STRETCH / NOTE / SCORE lines and writes the sidecar (a gate lanes it skip, a strong
   5-year role lanes it digest-stretch). Then `python pipeline/worklist.py
   siblings --scorer local` copies each score onto the role's duplicate postings.
2. Work list: `python pipeline/worklist.py tailor --cap N`, the same rules the cloud routine
   uses (lane, duplicates, worked roles, staleness, company cap, order: promoted, score,
   tier, recency; digest-stretch jobs fill only the slots left over). Ask Vaish for N if he
   is present; otherwise 4. Tailor `selected` in order
   and report `deferred` and `blocked`.
3. For each: the `resume-tailor` skill end to end (as `/tailor` steps 2-3), writing
   `tailored/<id>.json` with `"tailorer": "local"`.
4. Publish everything at once: `python pipeline/localgit.py publish -m "scan: <N> scored,
   <M> tailored" scores tailored applications`. If it prints `STOPPED:`, relay it.
5. Digest, for Vaish: counts by status; the ready-to-submit list with company, title, score,
   the three review scores and folder; digest jobs worth his look; anything that failed.
   Say that statuses update on the dashboard once the fold runs. Never submit anything.
