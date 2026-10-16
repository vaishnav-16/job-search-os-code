# The ATS screen (2026-10-14)

Replaces the `ats-scanner` must-have percentage and `ats_score.py`'s whole-JD keyword count as
the resume's ATS number. Code: `pipeline/requirements.py` (tests: `pipeline/test_requirements.py`).
Prompt: `.claude/agents/ats-scanner.md`. Tailor contract: `.claude/skills/resume-tailor/SKILL.md`.

## Why the old number was wrong

Across several hundred tailored resumes the old `ats` score had a median of 85, and 78% cleared the 80
target. On the same resumes, literal must-have coverage had a median of about 68% and the
mechanical counter 33%. The LLM picked about 18 terms and counted any synonym as a full hit.
On one resume, "ran it live" counted as live-service experience and an
Interests line counted as domain experience: 94%, while the recruiter and hiring manager
both gave it 4/10. The mechanical counter ran low for the opposite reason, because it scored
employer boilerplate (the company's own name, "benefits", "content").

## How screening works in 2026 (research, with sources)

- **Automatic rejection comes from knockout questions, not resume text.** Taleo ACE
  prescreening, Lever auto-archive, Ashby auto-reject and SmartRecruiters all document it
  (sponsorship, location, minimum years, degree). The "75% of resumes auto-rejected" figure
  traces to Preptel (about 2012), which never published a method. The Harvard Business
  School / Accenture "Hidden Workers" study (2021) found that more than 90% of employers
  use the ATS to filter or rank on recruiter-configured criteria.
- **AI screeners grade each requirement and output a coarse tier.** None of them counts
  keywords.
  - Workday HiredScore: A (all basic and most preferred), B (all basic), C (most basic),
    D (fewer); its Fit & Gap view has an LLM judge "each screenable qualification
    individually" with cited evidence.
    (doc.workday.com/hiredscore: reference--candidate-grades, concept--fit---gap)
  - Greenhouse Talent Matching: Strong / Good / Partial / Limited, using fine-tuned LLMs
    and embeddings for skills and titles. Documented points: "multiple terms map to the
    same calibrated skill", so keyword stuffing does not help, and it never auto-rejects.
    (support.greenhouse.io 41131886674075)
  - Ashby AI-assisted review: meets / does not meet each recruiter-written criterion, plus
    "Criteria Met %". (docs.ashbyhq.com/ai-assisted-application-review)
  - LinkedIn Hiring Assistant: Top fit / Maybe / Not a fit; missing any must-have gives
    Not a fit. (training.talent.linkedin.com/review-applicants-with-hiring-assistant)
  - Eightfold scores 0-5 on titles, experience, skills and trajectory. Morgan Stanley's
    NYC LL144 bias audit is titled "Eightfold Model For Scoring Applicants".
- **Recruiters still run literal searches.** Boolean keyword and title searches over parsed
  text: in Jobscan's recruiter survey, 76% search by skills from the JD and 55% by job
  title. Textkernel boosts the current title and recently used skills.
- **Commercial scanners** (Jobscan, Resume Worded) count keyword overlap over the whole JD:
  the same flaw as the old counter, and none is validated against real outcomes.
- **Market share:** Workday has more than 40% of the Fortune 500 (banks, EA). Greenhouse,
  Lever and Ashby dominate tech and startup postings. (jobscan.co Fortune 500 ATS report, 2026)
- **What cannot be known from outside:** whether an employer has turned on the paid AI
  add-on, the recruiter's calibration weights and search strings, and pool-relative tiers
  (iCIMS Role Fit). So the grade means "how a requirement-by-requirement screener would
  rate this page", not a prediction of the ATS outcome.

## The design

1. `requirements.py extract` (no LLM) pulls the qualification bullets from `jd.md` into
   `requirements.json`, tagging each basic or preferred (by heading, or inline: "preferred",
   "is a plus") and flagging years, degree and work authorization. It runs before drafting,
   and the list is fixed for the run, so the tailor cannot choose easier targets and every
   round grades the same items. Calibrated on several hundred `jd.md` files under `applications/`:
   about 91% give a list (median 9 items). For the other 9% (postings with no
   headings, or unusual layouts), the round-1 screener derives the list and the grader
   adopts it.
2. `requirements.py plan` gives the tailor the list before the first draft: the computed
   years, the title a recruiter searches, and the search-string candidates. The brief maps
   every requirement to the bullet that will prove it, or to GAP. This is where the resume
   gets better, not just the score.
3. The `ats-scanner` (Sonnet) reads `requirements.py packet`, judges each item met / partial
   / not met with a verbatim quote, and in round 1 picks 8-15 recruiter search strings
   (locked after that). It writes `screen-r<N>.json`.
4. `requirements.py grade` (no LLM):
   - checks every quote is on the page; a quote that is not there earns nothing;
   - grades a `met` that rests only on a Skills or Interests line as partial;
   - computes the HiredScore tier and a score (basic items weigh double, partial earns half);
   - checks literal findability of the search strings, and the title search against dated
     title lines only;
   - writes `review-ats-r<N>.md` (ending `ATS_SCORE: NN`) and `screen.json`.
5. The dashboard shows "Screen B · 7/9 basic" from `screen.json`. Folders reviewed before
   this change keep their "Keywords NN%" badge.

`review.md`'s `SCORES: ats NN / recruiter N / hm N` line keeps its shape, with `ats` now the
screen score, so `sync.py`, `fire_decay.py` and the sidecars need no change.

## Known limits

- Grading a requirement is still an LLM judgment. The quote check stops invented evidence,
  not a generous reading of real evidence.
- The `Product Manager` title search rarely matches exactly, because the functional titles
  are Product Owner / Product Analyst / Product Management Intern (Vaish's 10-08 ruling). A
  wildcard search finds the intern title. The screen reports this and never changes titles.
- Calibrate the tiers against interview outcomes: compare interview rate by grade before
  trusting any threshold.
