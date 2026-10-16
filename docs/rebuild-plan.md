# Layer 2 rebuild — phased plan and progress tracker

Started 2026-09-16. The full design lives in a private session plan.
This file is the working tracker: one phase at a time, status updated as work lands.

Goal: strongly tailored resumes, reviewed by Vaish, applied manually.

Status key: `[ ]` not started · `[~]` in progress · `[x]` done · `[-]` dropped

---

## Phase 0 — Prep
Goal: safe to rebuild without the automation committing half-done work.
- [x] Check git state (clean at f4ef5779); local sync task DISABLED 2026-09-16
- [x] Note the Actions cron keeps running (it only touches `queue/`, `scores/`, `jobs.sqlite`)
- [x] Record Vaish's rulings from the planning session in this file (below)
Done when: sync task disabled, tree clean or understood.

## Phase 1 — Archive the old doctrine
Goal: nothing deleted from git; the tailor stops reading rules.
- [x] `git mv` CLAUDE.md, LEARNINGS.md, decisions/doctrine docs -> `docs/history/`
- [x] `git mv` master-doc, attestations, SAP/NetOS/OpenDoor/HeatWise companions, drafts,
      template backups -> `profile/archive/`
- [x] Archived (not deleted) standing-dispositions, recruiter-critic, both routine prompt files + live.txt,
      project-bundle/, bank-rendered master-resume artefacts
- [x] Minimal placeholder CLAUDE.md so the repo still has one (full rewrite in Phase 5)
Done when: tree builds, nothing references a moved file at import time.

## Phase 2 — Profile knowledge base (`profile/knowledge/`)
Goal: plain evidence per role/project; every fact kept, every rule dropped.
- [x] 00-profile, 10-fidelity, 20/21/22-citi, 30-penzigo
- [x] 40-networking-os, 41-opendoor, 42-sap, 43-heat-wise, 44-tuck, 45-hallucination, 46-gyfteasy
- [x] 50-skills, 60-attested-facts, record.json (structured dates/titles for the parse-back check) (from the 19 resolved attestations + decision docs)
- [x] Fable review pass: token scan clean, three missing Vaish decisions added to 60-attested-facts
Done when: Vaish signs off on the folder.

## Phase 3 — Master PM resume (Fable, one-time)
Goal: the ideal early-career big-tech PM resume Vaish can honestly present; the tailor's source.
- [x] Research: APM program requirements, FAANG PM resume guidance, AI PM resume guidance (web) + the 90-JD keyword corpus
- [x] Wrote `profile/master-resume/Vaishnav_Ajai_Master_PM_Resume.md` (superset: 5 positioning variants, 7+4+5+4+3 work bullets, 6 projects, skills, tailor notes)
- [x] Rendered docx/pdf for review (`pipeline/render_master_md.py`)
Done when: Vaish signs off.

## Phase 4 — Pipeline code
Goal: code matches the new contract; tests green.
- [x] `filter.py` YOE ceiling 7
- [x] `ats_score.py` -> plain coverage scorer (1515 -> 920 lines, 79 tests)
- [x] `preflight.py` mechanical checks only (80 tests pass); parse_back reads record.json
- [x] Deleted ats_gap, bank_audit, render_master, attestation_audit, backfill_ats, measure_loop (+tests)
- [x] `sync.py` one branch `claude/judge`, `scores` sidecar field, Word check = flag (79 tests; per-path checkout fallback when a run pushes scores/ only)
- [x] `dashboard.py` SCORES line, brief.md, word_pages badge, Render problem column, claude/judge overlay
- [x] `screening_draft.py` §6 markers only
- [x] `schedule.py` + docs/schedule.md: one routine + one sync per window
- [x] All `pipeline/test_*.py` pass (18 scripts, 2026-09-16)
Done when: every remaining test script prints "all tests pass".

## Phase 5 — Prompts and doctrine files
Goal: short, goal-stated prompts per current Opus 5 / Sonnet guidance.
- [x] Agents: job-scorer (sonnet), ats-scanner (sonnet), recruiter-screen (opus), hiring-manager (opus)
- [x] `resume-tailor/SKILL.md` v3 (draft; revisit after Phase 6)
- [x] `.claude/routines/judge-queue.md` (config + 6.8K-char prompt) + `judge-queue.notes.md` + test_routine_prompts rewritten
- [x] `CLAUDE.md` (v3), `LEARNINGS.md` (operational traps only)
- [x] `/scan`, `/tailor`, `/digest` commands; `docs/layer2-v3.md`; dashboard.yml overlays claude/judge
- [x] voice.md, preferences.md, keywords.md trimmed; lint hook exempts brief.md and review-*.md
Done when: a fresh session can run `/tailor <id>` from the docs alone.

## Phase 6 — Local end-to-end
- [x] `/tailor` on three real queued jobs (2026-09-16, Opus agents following SKILL.md):
      Company A APM University Graduate 2027 (`a4fb0a4b`, ATS 83 / recruiter 7 / HM 7, 1 round,
      29 min), Company B PM New Grad Accelerator (`b15ebab3`, ATS 64 / recruiter 7 / HM 7, 2
      rounds, 29 min), Company C PM II Payments (`c3c5092e`, ATS 89 / recruiter 4 / HM 6, 2 rounds,
      25 min; the JD body is marketplace work, not payments). All one page, all folders
      complete, sidecars written, DB rows `tailored`.
- [x] Fixed what the runs exposed: preflight now errors on page count != 1; SKILL.md carries
      the engine facts, the 40/42-word budget, the digit-in-bold rule, the new-grad positioning
      rule, PROJECTS-above-WORK for builder reqs, the title/body-mismatch rule, the reviewer
      wait-for-results rule, the round-2 ATS must-have pinning, the sidecar-on-every-run rule,
      hyphen headings, live-source check for screening §3; tailor.md names the fallback when
      `.claude/agents` types are not registered (session started above the repo root);
      master resume: two scope figures disambiguated so a bullet names the one it means,
      and one metric pinned to a single wording; attested facts: clause-bank rules removed.
- [x] Master resume plain-language pass (one achievement per bullet; language rule added to
      "Notes for the tailor")
- [x] Re-run with the v3.1 skill (revise then always re-review, targets 80/8/8, up to three
      rounds, evidence map): Company A ATS 87 / recruiter 7 / HM 8 (34 min), Company B 93 / 7 / 8
      (32 min), Company C 79 / 4 / 6 (145 min; structural: 4+ yrs and consumer scale). Recruiter
      7s are anchored on two of Vaish's own rulings (no availability line, LinkedIn display
      text) and one missing fact (a beta user count).
- [x] Vaish ruled: LinkedIn display text stays; no availability line (reviewers told not to
      deduct). Facts added: TA for Game Theory and Data Analytics; ~10 Networking OS testers and
      their feedback; Pricing Strategy & Analytics course and pricing/monetization analysis.
- [x] Third run with the new facts: Company A ATS 94 / recruiter 8 / HM 8 (2 rounds, ~30 min),
      Company B 93 / 8 / 8 (2 rounds, ~45 min). Both at target. Company C left at 79 / 4 / 6 (weak fit).
Done when: Vaish would send the three resumes (he may still edit them).

## Phase 7 — Cloud switch-over
- [ ] Create `judge-queue` routine (cap 2), verify prompt with routine_parity
- [ ] Manual trigger; check branch diff = scores/ + applications/ + tailored/ only
- [ ] `sync.py` folds it; dashboard shows scores
- [ ] Cap 8, cron on; **Vaish deletes score-queue + tailor-queue routines**; re-register sync task
Done when: three unattended runs land on the dashboard.

## Phase 8 — Week one measurement
- [ ] jobs/day tailored, minutes/job, score distribution, Max 5x quota headroom
- [ ] Tune cap / effort from data
Done when: throughput is measured, not assumed.

---

## Rulings from the planning session (2026-09-16)
1. The model writes bullets: strongest true framing of what the JD and hiring manager want,
   inferred from JD + profile. No inference markers; Vaish reviews every resume and is the gate.
2. Reviewers are advisory; the tailor fixes by writing, aiming for an 85-90% match.
3. Auto-tailor: graduate / university / 0-4 years asked. 5+ is Vaish's call from the digest.
4. Claude Max 5x.
5. Laptop may stay in the loop; watch GitHub Actions minutes (design keeps them at zero extra).
6. Model-authored "hard truths" / do-not-claim lists are dropped as rules; kept only as facts.
7. Domain stretch: the model judges case by case.
8. No Fable per job. Fable writes one master PM resume; Opus tailors from it per JD.

## Progress log
- 2026-09-16: planning done, tracker created. Phase 0 done. Phase 1 done (local commit).
- 2026-09-16: Phases 2-5 built and committed locally (knowledge base, master PM resume, pipeline simplification, v3 prompts). All 18 test scripts pass. Phase 6 started; every subagent launched under /low-priority stalled before starting (0 bytes), so no v3 resume exists yet. Master resume got a plain-language rewrite. Stale master-doc/attestation references cleaned from live messages. Paused until the usage window resets. Vaish still to read `profile/knowledge/` and the master resume.
- 2026-09-16 (evening): window reset; three real tailors ran in parallel (~25-29 min each, 3 reviewer spawns per round); Phase 6 fixes landed.
- 2026-09-18: pre-switch review written to `docs/rebuild-review-2026-09-18.md`: coherence audit (routine TypeError, WebFetch grant, partial-folder and promote rules, bounded reviewer wait, rounds aligned everywhere), Layer 1 checked (healthy; 15 boards added; LinkedIn descriptions fetched at detection), taste rules (positioning optional, tailor to the JD not down to it). Weekly quota at 85% with the reset in ~10h; Phase 7 next.
- 2026-09-16 (night): skill v3.1 (always re-review, 80/8/8 targets, 3-round cap, evidence map); re-runs 87/7/8, 93/7/8, 79/4/6; preflight page-count and text-layer checks; master resume corpus vocabulary pass (PM terms present 45 -> 61 of 76). Third run with Vaish's new facts reached 94/8/8 and 93/8/8. Ready for Phase 7.
