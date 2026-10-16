---
description: Tailor one job end to end by id (incl. digest jobs Vaish promotes by hand)
argument-hint: <job-id>
---

Tailor one job: `$ARGUMENTS` is the job id (the `queue/<id>.json` filename stem and the DB
primary key). A digest job being promoted by hand is the normal case for this command; do
not second-guess the promotion.

The laptop never writes `pipeline/jobs.sqlite`. Like the cloud routine, this command writes
sidecars and a folder, publishes them to main, and the cloud fold (`fold.yml`) sets the
status within a few minutes.

0. `python pipeline/localgit.py begin`. If it prints `STOPPED:`, relay the reason and stop.
1. Load the job row (`db.connect_readonly()`) and `queue/<id>.json`. If it was never scored,
   run the `job-scorer` subagent (Sonnet) on the queue JSON path first, save its reply to a
   file and record it with `python pipeline/ingest_scores.py record <id> <file> --scorer local`.
2. Run the `resume-tailor` skill end to end: `brief.md`, the one-page docx and PDF, the
   four reviewers in parallel, revise, review again (two rounds normal, three the cap),
   `review.md` ending with the `SCORES:` line, `screening-draft.md` rendered and
   checked. Set `RESUME_RENDERER=soffice` in every shell that runs Python.
3. Write the sidecar `tailored/<id>.json` with `"tailorer": "local"` and `status_intent`
   `tailored` (or `needs_review` only if the resume could not be rendered to one page).
4. Publish: `python pipeline/localgit.py publish -m "tailor: <company> <id8>" tailored/<id>.json scores/<id>.json applications/<folder>`
   (drop `scores/<id>.json` if step 1 did not score). If it prints `STOPPED:`, relay it;
   the work is committed locally and the next command's `begin` retries the push.

If `subagent_type: ats-scanner` (or `recruiter-screen`, `hiring-manager`, `readability-screen`, `job-scorer`)
is "not found", the session was started above the repo root and `.claude/agents/` is not
registered: launch `general-purpose` with that agent file's body pasted verbatim as the
prompt, the same `model` pinned, and the exact files it may read named. Never paraphrase
the agent file; the output shape is what `review.md` assembly depends on.
5. Report: folder path, the three scores, rounds used, the projects and bullets you chose
   and anything you inferred beyond the written record. Do not submit; Vaish does that.
