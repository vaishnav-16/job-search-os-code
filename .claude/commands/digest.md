---
description: Pipeline status counts + ready-to-submit list (read-only)
---

Read-only status digest. No scoring, no tailoring, no status changes.

0. `python pipeline/localgit.py begin` so the counts are current (detection and the cloud
   fold commit to main all day). If it prints `STOPPED:`, say so and report from the local copy.
1. Counts by status from `pipeline/jobs.sqlite`.
2. **Freshness.** `python pipeline/freshness.py` (report only): lead with the age histogram
   and name the <24h bucket; those are the apply-today roles.
3. **Ready to submit** (`tailored`): folder, company, title, fit score, the three review
   scores from `review.md`'s `SCORES:` line, resume PDF path. Order as the dashboard does:
   min(recruiter, hm) descending, then freshness tier, then fit score.
4. **Render problems** (`needs_review`): folder and what failed (page count, Word check note
   in `notes`), and the fix: `/tailor <id>` to redo, or `/mark <id> skipped`.
5. **Cloud work awaiting fold:** `python pipeline/ingest_scores.py` (report only) for
   score sidecars; count `tailored/*.json` with no matching DB status. Non-zero right after a
   routine run is normal: `fold.yml` absorbs them within minutes. Non-zero for hours means
   check `gh run list --workflow fold.yml`.
6. **Tailor backlog:** jobs in the `auto-tailor` lane with no `tailored/` sidecar, no
   application folder and not provably stale; count plus the top 5 by score with age tier.
   ```bash
   python -c "import sys,json,glob,os; sys.path.insert(0,'pipeline'); import db; q=[p for p in glob.glob('queue/*.json') if os.path.exists('scores/'+os.path.basename(p)) and json.load(open('scores/'+os.path.basename(p),encoding='utf-8'))['note'].startswith('auto-tailor') and not os.path.exists('tailored/'+os.path.basename(p)) and not db.is_provably_stale(json.load(open(p,encoding='utf-8')).get('posted_at','')); print('backlog', len(q))"
   ```
7. **Outcomes:** `python pipeline/db.py --outcomes`. Report the "no outcome yet" list
   (applied, silent 14+ days: follow up or `/mark <id> rejected`) and the funnel by
   channel; say plainly when a rate is suppressed for lack of data.
8. **Digest jobs** awaiting Vaish's call: score, company, title, rationale, and the
   `/tailor <id>` command to promote.
9. Last Layer 1 run timestamp; flag if older than 6 hours.
10. The published dashboard is rebuilt by `dashboard.yml`; no local rebuild needed.
