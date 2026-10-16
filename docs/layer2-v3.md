# Layer 2 v3 — flow and output contract

One page for humans. The prompts are the source of truth: `.claude/routines/score-queue.md` and `tailor-queue.md`,
`.claude/skills/resume-tailor/SKILL.md`, `.claude/agents/*.md`.

## Flow

```
detect.yml (Actions, 2x/hour) ──> queue/<id>.json on main
                                        │
score-queue (cloud, Sonnet 5.5, every 4h)  ▼
  overlay every claude/judge* branch; job-scorer (Sonnet) x N, 10 at a time ──> scores/<id>.json
  siblings; gate re-check; push claude/judge-score-<run>
                                        │
tailor-queue (cloud, Opus 5.5 coordinator, 3x/day; W4 2026-10-11, replaced judge-queue)
  STEP 1  overlay every claude/judge* branch, oldest first (scores/, tailored/, applications/)
  STEP 2  work list: auto-tailor or promoted, not tailored, role not worked, not stale; score first, then age; cap 12
  STEP 3  per job, up to 3 at once: a fresh resume-tailorer subagent runs the resume-tailor skill
            brief.md ─> draft docx (docx_engine) ─> soffice PDF, one page ─> preflight ─> "REVIEW ROUND N"
            coordinator: ats-scanner (Sonnet) ‖ recruiter-screen (Opus) ‖ hiring-manager (Opus) ‖ readability-screen (Sonnet, W9 10-16)
            ─> SendMessage resumes the same tailorer: revise, review again (two rounds normal, three the cap)
            ─> review.md (SCORES line) ─> screening-draft.md ─> tailored/<id>.json ─> commit
  STEP 4  push the session's designated branch (claude/judge-tailor-<run>); no new job after 5h
                                        │
sync.py (laptop, 1x per window)         ▼
  fold scores/ + tailored/ + new folders into jobs.sqlite; Word page check = flag;
  freshness sweep; dashboard; push main; delete folded run branches, reset claude/judge to main
                                        │
dashboard (Vercel)                      ▼
  Ready to submit (sorted by min(recruiter, hm)) · Render problem · Decide (digest) · Applied ...
  buttons -> decisions/pending.json -> folded by the next sync
```

## Files per application folder `applications/<company>-<slug>-<date>/`

| File | Written by | Read by |
|---|---|---|
| `jd.md` | tailor (verbatim JD) | reviewers, requirements.py |
| `requirements.json` | `requirements.py extract` (the JD's qualifications, R1..Rn, basic/preferred); round 1 adds `search_terms` | tailor (requirement map), ats-scanner, grade |
| `brief.md` | tailor: `# Company - Title`, `**id:** <id>`, the analysis, requirement map, tailoring notes, revision notes | dashboard (id line), Vaish |
| `Vaishnav_Ajai_Resume_<Company>.docx/.pdf` | tailor via docx_engine + soffice | Vaish, reviewers |
| `review.md` | tailor, from the four reviewer files; `READABILITY: N` (since 10-16) then the last line `SCORES: ats NN / recruiter N / hm N` (`ats` = screen score) | dashboard, sync |
| `screen.json` | `requirements.py grade` (last round: grade A-D, score, basic/preferred met, title, findability) | dashboard |
| `screening-draft.md` | `screening_draft.py render` + tailor (§3, §4) | Vaish at submission |

## Sidecars

`scores/<id>.json`: `{"id","score" (0-10),"note" ("<lane>; rationale"),"scored_at","scorer","model"}`,
lanes `auto-tailor` / `digest` / `skip`.

`tailored/<id>.json`: `{"id","folder","status_intent" ("tailored"|"needs_review"),"scores":{"ats","recruiter","hm"},"rounds","pages_soffice","resume","tailored_at","tailorer","model"}`.
`needs_review` means only "could not render to one page". Legacy fields (`verdict`,
`converged`, `ats`) are ignored if present.

## Lanes and gates

- Hard filter (Layer 1): 7+ years required, Director/Staff/Principal, marketing/sales PM,
  non-US, explicit no-sponsorship, clearance/citizenship requirement.
- Scorer: 10 points over role type, years asked, domain/skills fit, company, keyword
  overlap. `auto-tailor` = total >= 7 and years asked <= 4 (or unstated / new-grad).
  `digest` = 5-6, or >= 7 with 5-6 years asked. Strategy and govcon employers cap at 6.
- Targets: ATS 80, recruiter 8, hiring manager 8. A round that meets every target ends the run; a third round needs a missed target and an actionable ask.

## Models

| Where | Model | Effort |
|---|---|---|
| tailor-queue coordinator | Opus 5.5 | high (routines UI) |
| resume-tailorer | Opus | high (agent frontmatter) |
| score-queue main thread | Sonnet 5.5 | medium (routines UI) |
| recruiter-screen, hiring-manager | Opus (alias) | medium (agent frontmatter) |
| job-scorer, ats-scanner, readability-screen | Sonnet 5 | medium (agent frontmatter) |
| master PM resume, knowledge-base review (interactive, one-off) | Fable 5.1 | high |
