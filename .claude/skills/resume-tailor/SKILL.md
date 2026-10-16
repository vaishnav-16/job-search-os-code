---
name: resume-tailor
description: Tailors Vaish's one-page resume to one scored job: writes a brief of what the hiring manager needs, drafts the strongest true resume from the master PM resume and the knowledge base, maps it against the posting's own qualification list, gets it reviewed by an ATS screen, a recruiter, a hiring manager and a readability reader, revises by writing, and ships docx + PDF + screening draft. Use for every /tailor and for every job the judge-queue routine tailors.
---

# Resume tailor (v3, 2026-09-16; requirement screen 2026-10-14)

You produce one tailored, one-page resume for one job, and the review record that lets
Vaish decide fast. Vaish reads every resume before applying and is the final gate, so your
job is to write the strongest resume he can honestly present and defend in the interview
it wins. Do not ask him questions mid-run; make the call and move on.

## Framing: the strongest true reading

The goal is interviews. He did the work and answers interviews with situational stories,
not logs, so a page that hedges real work undersells him. So write the strongest true
framing of work he did, in the JD's own words, and mirror the JD's vocabulary where it is
the honest word:

- **Where the record shows he did the work, frame it at full strength.** Lead with the
  ownership verb the work supports at its strongest reading: ran, led, drove, owned,
  handled. Do not hedge with "supported", "helped", "contributed to" or "participated in"
  when he was the one doing it.
- **Use his estimates as numbers.** A `Resolved` line marked as his estimate is a usable
  figure, written "~N" or "about N". Never leave a number off because it is an estimate.
- **State a result only when the record supports it.** Name a role's routine duties
  plainly when the role makes them certain, without adding outcomes or numbers.
- **Hard lines that stay, whatever the JD asks:** no invented employer, title, date,
  degree or credential (no PMP, no Azure DevOps, nothing on the never-used tools list or
  the "No experience in" list); no number the record or his estimates do not hold. Nothing
  that never shipped is described as running, and a target is never written as a result.
- **A line a reviewer questions is not silently kept or silently softened:** if the record
  supports it, it stays and goes under `## Lines to defend` in `brief.md` for the human to
  verify or strike (at most five lines, placed just before `## Would raise the score if
  true`, which stays last), so Vaish has his interview prep in one place.

## What you read

- `queue/<id>.json`: `company`, `title`, `location`, `url`, `posted_at`, `jd_markdown`.
  About a third of LinkedIn-sourced files carry no JD text. If `jd_markdown` is under 200
  characters, fetch the posting (WebFetch on `url`; if that fails, search the company's
  careers site by title) and write `jd.md` from what you get, never from memory. Never
  write to `queue/`. If no real JD can be had, skip the job: no folder, no sidecar, one
  line in the report.
- `profile/master-resume/Vaishnav_Ajai_Master_PM_Resume.md`: the superset resume, written
  once for the ideal early-career big-tech PM. Start from it; it already carries the
  strongest framing of every role and project.
- `profile/knowledge/*.md`: the evidence behind the master resume. Read the files for the
  roles and projects you are writing about when you need detail, numbers, or an angle the
  master resume does not carry. `00-profile.md` has titles, dates and the contact line;
  `50-skills.md` the tools inventory; `60-attested-facts.md` things Vaish confirmed.
  Each file's `## Open facts` holds his newest answers as `Resolved <date>` lines (usable
  evidence, often numbers the master does not carry yet) and the known gaps, marked "do not
  ask again" or "never claim".
- `profile/voice.md`: how the writing should read.
- `profile/template/Vaishnav_Ajai_Resume_Template_Format.docx`: the only starting docx.

## What you write, in `applications/<company>-<role-slug>-<YYYY-MM-DD>/`

`jd.md` (the JD verbatim), `brief.md`, `Vaishnav_Ajai_Resume_<Company>.docx` and `.pdf`
(exactly one page), `requirements.json` (written by the extractor, never by you), `review.md`,
`screen.json` (the last ATS screen, which the dashboard reads), `screening-draft.md`, and the sidecar
`tailored/<id>.json` (every run, local or routine). Company in the docx filename is a short
clean form (`Stripe`, `CapitalOne`, `JPMorganChase`); the folder name is lowercase and
hyphenated like every existing folder.

## 1. Brief

`brief.md` starts with `# <Company> - <Title>` (plain hyphen; you write no em-dashes
anywhere, the lint exemption exists for pasted JD and reviewer text) and a line
`**id:** <40-hex id>` (the dashboard reads that line). Then, in about 40 lines: the problem this hiring manager is
hiring to solve and what the first six months look like; the requirement map (below); the
10-15 load-bearing terms (skills, domains, tools, level words) in the JD's own spelling,
starting from the plan's search strings; the role archetype (platform/API, AI product,
fintech/payments, TPM/program, data product, consumer, new-grad); and the ideal candidate in
three sentences. This is the target you tailor to. Write it before touching the docx.

**The requirement map.** The page is screened the way 2026 ATS screeners read it (Workday
HiredScore, Greenhouse, Ashby, LinkedIn): the posting's qualifications split into basic and
preferred, each judged met or not against a line on the page, then a tier (A all basic and
most preferred, B all basic, C most basic, D fewer). Get the fixed list before drafting:

```bash
python pipeline/requirements.py extract applications/<folder>
python pipeline/requirements.py plan applications/<folder>
```

`plan` prints each requirement (R1..Rn, basic or preferred, years and degree flags), the years
a parser will compute from his dates, the title a recruiter searches, and the search strings
in the requirements. Under `## Requirement map` in the brief, one line per id:
`R3 basic | <the bullet that will prove it: role, evidence file, the number> | <JD term to use>`
or `R5 basic | GAP: <the nearest true evidence, or none>`. Grep `profile/knowledge/` before
writing GAP. When `plan` finds no list (about one posting in eleven), map the JD's must-haves
in your own words; the round-1 screener derives the graded list. Never edit
`requirements.json`: the list is the employer's, and it stays fixed so every round compares.

When the title names a domain the JD body never mentions (a "Payments" req whose body is
all marketplace work), tailor the bullets to the body, let the first Fidelity or Citi bullet nod to
the title, and say in the brief which is which. The reviewers and the ATS scorer only see the
body.

## 2. Draft

Copy the template and edit it with `pipeline/docx_engine.py`; never build a docx from
scratch (styles, tab stops and numbering live in the template).

```python
import sys; sys.path.insert(0, "pipeline")
from docx_engine import Doc, render_pdf
doc = Doc.load("profile/template/Vaishnav_Ajai_Resume_Template_Format.docx")
for p in doc.paragraphs(): print(p.index, p.style, p.text[:80])   # see the slots
p = doc.find("Drove a firm-mandated")            # returns a Para; locate by text, never by a remembered index
doc.replace_paragraph_text(p.index, "…with **bold metric** spans…")
doc.replace_run_text(doc.find(official).index, official, functional)   # a title pair record.json sanctions
doc.delete_paragraph(doc.find_style("Positioning").index)   # every tailored page: no positioning line
doc.delete_employer("PENZIGO")                    # unless the JD rewards it (opt-in, below)
new = doc.clone_paragraph(src_idx, before_idx)   # the ONLY way to add a bullet
doc.delete_paragraph(idx)                         # refuses spacers
doc.resize_spacer(idx, 8)                         # half-points; 8 (4pt) is the floor
doc.save(out_docx); pdf, pages = render_pdf(out_docx)   # soffice writes the shipped PDF
assert pages == 1                                  # preflight also errors on pages != 1
```

Engine facts that cost time when unknown: `find()` matches rendered text, so a needle
with `**` markers never matches, and a needle goes stale the moment your script rewrites
that text. The empty `AchievementBullet` paragraphs after the last employer and between the
two project blocks are spacers; `delete_paragraph` refuses them, so removing a bullet means
removing a real one. `delete_employer` takes Penzigo's whole block with the spacer above it
and leaves the end-of-section spacers alone; it refuses Fidelity and Citigroup. A cloned project sub-header loses the template's hyperlink; write its URL as `[label](url)`.
`Para.is_spacer` is a property, not a method. After `clone_paragraph` the cloned text matches
two paragraphs, so hold the index you got back instead of calling `find()` again.
Author every string in a UTF-8 script; the console mangles `·`, `–` and `é`.

Template slots, top to bottom: name and contact line (never edited; the LinkedIn display
text is deliberate); the `Positioning` paragraph (always deleted, below); WORK EXPERIENCE
with Fidelity, Citigroup with three dated roles, and Penzigo (opt-in, below); EDUCATION
with the Dartmouth line, the Courses line and the Research Assistant bullet, then NIT
Karnataka; PROJECTS with two sub-headers and one bullet each; SKILLS & INTERESTS with a
Technical line, a Product line and the Interests line (keep it; it is real; it may be
trimmed to one line, never cut).

**Every page carries the CSPO, linked to its badge, in SKILLS & INTERESTS (Vaish,
2026-10-15).** Never under EDUCATION: it holds degrees only, and a certification under
Dartmouth reads as Dartmouth's. Default, on the Product line (the template already links it,
so a rewrite of that line must keep the link): `Agile ([CSPO®](https://app.badgecert.com/public/badges/kjhswadu) Certified)`.
When the JD asks for a certification, give it its own bold "Certification:" line above
Interests, cloned from the Product line:
`Certification: [Certified Scrum Product Owner (CSPO®)](https://app.badgecert.com/public/badges/kjhswadu), Scrum Alliance`.
Preflight ERRORs on a certification in Education and on a page without the linked CSPO.

**Nothing earns space by default (Vaish, 2026-10-13).** The template's bullet counts are
slots, not a plan. Decide every count from the brief, add with `clone_paragraph`, remove
with `delete_paragraph`, and spend the whole page:
- **Roles.** Fidelity and all three Citi roles stay on every page (dropping one leaves a hole
  in the dates). Each gets as many bullets as it has evidence this JD cares about, from one
  up. The role that answers the JD best gets the most.
- **Citi carries the weight by default.** Citi is three years and Fidelity one summer, so
  Fidelity gets 1-2 bullets and never more than the strongest Citi role. It gets 3 only when
  the JD is squarely its work (API platform, governance, developer-facing). Preflight warns
  when Fidelity outweighs every Citi role.
- **Penzigo is opt-in.** Run `doc.delete_employer("PENZIGO")` on every page unless the JD
  rewards a 2021 consumer-app internship (consumer mobile, payments or checkout, e-commerce,
  gifting). When it stays it gets one bullet, and the tailoring notes say why.
- **Projects and the Research Assistant bullet** follow the same rule: projects and their
  bullets by relevance (below), and the Research Assistant bullet only when it serves the JD.

Titles come from the variants `record.json` sanctions. The functional variants (which
match his LinkedIn) are the default: on every page except a forward deployed engineer page,
swap each template title run to its functional variant with `replace_run_text`. On an FDE
page keep the official variants. Never invent a title.

**One page shape, every resume (Vaish, 2026-10-08; preflight ERRORs on each):**
- No positioning line. First edit: `doc.delete_paragraph(doc.find_style("Positioning").index)`;
  spend the one or two lines it held on the most JD-relevant bullet.
- The template's section order, always: WORK EXPERIENCE, EDUCATION, PROJECTS, SKILLS &
  INTERESTS. Never `move_paragraphs` a section, whatever the JD's family or level.
- Labels ("Technical:", "Product:", "Interests:", "Courses") and project names stay bold.
  `replace_paragraph_text` keeps the bold lead-in on its own when your text starts with a
  label or `Name | role`; never write the whole sub-header in `**`.
- Functional titles (the `record.json` variants above), except the official variants on an FDE page.
- No retired claim: preflight reads `retired_claims` in `record.json` and prints the
  sanctioned wording for any retired claim.

Write for the brief's ideal candidate:
- **Level: tailor to the JD, not down to it.** The ideal candidate clears the bar with room
  to spare. When a JD's minimum qualifications describe a fresh graduate and the record is
  stronger, present the stronger candidate: three years of shipped product work and a
  founder's build record are why he beats the other applicants, not something to hide
  behind a TA line or an internship. Never inflate to senior; never delete or bury real
  experience to look junior. Mirror the JD's vocabulary where it is the honest word, and
  keep the page reading like one specific person with a point of view, not a keyword answer.
- **Bullets** (about 40 words; preflight warns past 42; a bold span of five or more
  words must contain a digit, so write numerals inside bold; bold what a skimming reader
  must catch, with no count cap: each bullet's headline number, plus a named scope or
  technology term when it is the point, and the same kind of fact bolded the same way
  across the page, so 10.6% is not bold in one bullet and $200K+ bold in the next; never a
  verb, a clause or filler, per voice.md): rewrite from the master
  resume and the knowledge base in the JD's own vocabulary, strongest true numbers first,
  one concrete artifact per bullet, the consequence stated. Every bullet must read in one
  pass to a recruiter with no domain background and still show the craft to the hiring
  manager: what he did, how, what changed. Frame at the strongest true reading (§ Framing above).
  Where the JD wants something the record supports only partly, write the nearest real
  work in the JD's own words. Name a role's routine duties plainly when the role makes them
  certain (a Product Owner ran sprint planning and backlog refinement, a reporting analyst
  wrote acceptance criteria), without adding outcomes or numbers. Mirror the JD's vocabulary
  where it is the honest word, and claim a domain only where the record gives you something
  real to stand on. Any line a reviewer questions goes under `## Lines to defend` for Vaish
  to verify or strike before he applies.
- **Titles are the functional ones on every resume except a forward deployed engineer
  page**: Fidelity Product Management Intern, Citi Product Owner / Product Analyst / ML
  Engineer, Penzigo Product Management Intern (Vaish, 2026-10-08: they match his LinkedIn,
  which recruiters cross-read). A TPM or program JD moves the bullet choice and bullet order
  to delivery, never the titles; never call him a program manager or TPM anywhere on the page.
- **FDE pages keep the official titles** (Vaish, 2026-10-13): a job whose title says forward
  deployed (or FDE) and is not a PM, program or strategist role uses the official variants
  `record.json` sanctions in place of the functional ones. For an engineering reader the PM
  titles say "not an engineer". Never invent a title. Preflight reads the job title from brief.md's heading
  and ERRORs on a functional title there. Forward Deployed PM roles keep the functional titles.
- **Adjacent families** (forward deployed engineer, developer relations, product analyst):
  the bullet choice and bullet order move to the family (building with
  customers on their systems, teaching developers an API, analysis that drove a product
  call); titles and dates stay exactly as `record.json` sanctions, and FDE pages lead with
  what he built (the ML engineer year, the 0-to-1 products) rather than claim engineering
  roles he did not hold.
- **Stretch roles** (the work list marks them `"stretch": true`: 5 years asked): lead with
  scope and ownership evidence (the 450+ API intake governance, the consent-order
  remediation releases, the Kafka migration), never with tenure. Never stretch dates, add years, or imply
  a longer career than the record shows; say in brief.md that the years gap is the risk.
- **Projects**: pick the two that map hardest onto the JD (Networking OS, OpenDoor Justice,
  SAP AI evaluation, Heat Wise, hallucination detection, Dashi, the NITK computer vision and NLP
  projects in `39-nitk-ml-projects.md`), judged fresh for each JD: no
  project is the default, Networking OS included. A project with more to say for this JD may
  take a second bullet, and an AI-heavy JD may earn a third project, never a moved section.
  Project sub-headers carry no dates; put a date inside the first bullet if it matters.
- **Skills lines**: the JD's tools and methods the record supports, in the JD's spelling,
  most relevant first.
- **Requirement coverage**: every basic requirement the map can prove gets a bullet that
  SHOWS it (what he did, the artifact, the outcome) in the requirement's own key term where
  that is the honest word; then the preferred ones, as space allows. A screener credits a
  Skills-line entry as partial at most, so a must-have that lives only on the Skills line is
  not covered. A GAP stays a gap: write the nearest true work plainly and never invent the
  missing part.
- Everything reads like a sharp person describing their work aloud (voice.md, test 5).
- **Write for the JD's reader (W9, Vaish 2026-10-16).** The person who reads this page knows
  the JD's field, not banking, Citi's systems or his old tools. Pages written for the JD's
  reader read better. Keep a domain term when
  the JD uses it, or when the JD shows the reader works in that domain (CSDR for a bank or a
  trading desk is a signal). Otherwise say what the thing does, keeping every fact and number:
  - "Led two regulator-dated reporting programs" -> "Led two reporting programs in Japan and
    the EU with fixed regulator deadlines"
  - "Traced a surge of regulator rejections to one bad upstream field" -> "Traced a surge of
    reports the regulators rejected to one bad data field upstream"
  - "Cut NAV breaks by a third" -> "Cut the daily fund-pricing mismatches the team had to
    fix by a third"
  Preflight lists each such term with a true plain gloss (`pipeline/reader_terms.py`); the
  Skills lines may keep a tool name for search.

Plan the page before the first render so it fits in one or two renders, not eight: the
template holds about 58 rendered body lines; a 40-word bullet takes 2 lines, a 25-word one
takes 1 to 2, each section header and each spacer costs 1, the contact block 3,
Education 3 to 4 plus its bullets, Skills 3. Count before you write. Render and check `pages == 1` yourself. Over one page: reclaim widows (a bullet whose last
line holds one or two words), then trim connective filler, then cut the Courses line, then
cut the weakest bullet for this JD; never fonts or margins. Short: lengthen the most
JD-relevant bullet toward the 40-word budget first, then clone in the next most relevant
bullet. Preflight's "page runs SHORT" is a WARN under 2.0 unused body lines and an ERROR
from 2.0; treat the WARN as advisory and do not chase it into the word cap. Then run preflight and fix what it reports; its text-layer warning lists docx
words a parser would never see, and a dropped letter there is a real defect, and its
"reader term" warnings are the words this JD's reader may not know (§ Write for the JD's
reader):

```bash
RESUME_RENDERER=soffice python pipeline/preflight.py <docx> <pdf> --jd <folder>/jd.md
```

## 3. Review

Launch these four subagents in parallel, fresh context, each given only what it names;
they write their own files:

| Subagent | Give it | Writes |
|---|---|---|
| `ats-scanner` (Sonnet) | `jd.md`, the docx, the PDF, `<folder>/review-ats-r<N>.md` | `screen-r<N>.json`, then (via `requirements.py grade`) `## ATS screen` with the grade, ends `ATS_SCORE: NN`, and `screen.json` |
| `recruiter-screen` (Opus) | `jd.md`, the PDF, `<folder>/review-recruiter-r<N>.md` | `## Recruiter screen`, ends `RECRUITER_SCORE: N` |
| `hiring-manager` (Opus) | `jd.md`, the PDF, `<folder>/review-hm-r<N>.md` | `## Hiring manager read`, ends `HM_SCORE: N` |
| `readability-screen` (Sonnet) | `jd.md`, the PDF, `<folder>/review-readability-r<N>.md` | `## Readability read`, ends `READABILITY_SCORE: N` |

These are the only subagents you use per job. Do not spawn others to check your own work.
Launch all four in one message. If the tool returns before they finish, wait with ONE
background shell call, not a polling loop of tool turns (a polling run once burned 300
tool calls on waiting):
```bash
N=1; F=<folder>; t=0
until grep -q '^ATS_SCORE:' $F/review-ats-r$N.md 2>/dev/null && grep -q '^RECRUITER_SCORE:' $F/review-recruiter-r$N.md 2>/dev/null && grep -q '^HM_SCORE:' $F/review-hm-r$N.md 2>/dev/null && grep -q '^READABILITY_SCORE:' $F/review-readability-r$N.md 2>/dev/null; do sleep 10; t=$((t+10)); [ $t -ge 1200 ] && { echo TIMEOUT; break; }; done
```
(N is the round; the score line is the last thing each reviewer writes; 20 minutes is the
ceiling). A reviewer that never writes its score line is relaunched once; if it fails
again, proceed with the scores you have and say so in review.md. If a subagent type is "not
found" (a local session started above the repo root), launch `general-purpose` with the
agent file's body pasted verbatim, the same model pinned, and the files it may read named.

## 4. Revise, then review again

The target is the candidate this hiring manager pictured, not a pass mark. Read the four
reviews as a to-do list. A readability rewrite keeps every fact and number, and never swaps
out a term the JD itself uses. The reviewer's "Plain:" version is a suggestion written
without the record: check its wording against the record and the master's settled rulings
before using it (it can offer a phrase the record has retired, and preflight's
retired-claim check names the sanctioned wording). For every ask, in this order:

1. Find the evidence. Grep `profile/knowledge/` for it before deciding anything; the master
   resume's "Where the evidence for common asks lives" note tells you where to look first.
   Most asks that look unanswerable are answerable: a "technical presentation" is the SVP and
   MD pitch at Citi, the client board at Heat Wise, the weekly sponsor meeting at SAP, the
   executive decks at Fidelity; "market and user requirements" is the OpenDoor landscape
   study, the Heat Wise client work, the 140 Fidelity teams treated as customers.
2. Write it in. New bullet, restructured bullet, a phrase inside an existing bullet, a
   Skills entry. Use the JD's literal term where it is the honest word
   (`cross-functional`, `user research`, `technical presentations`). The ATS screen's
   "Gaps to close" come first, basic before preferred: a `partial` because the only match is
   a Skills line, or because the bullet names a neighbouring skill, is closed by a bullet that
   shows the work in the requirement's words; a `not met` the record answers is closed by
   writing that evidence in; one it cannot answer goes under "Would raise the score if true"
   (below), never onto the page. Its title-search and findability lines are asks too: a
   search string the record supports belongs in a bullet or the Skills line. Near-certain
   inferences from his roles are allowed (§2). A weaker point may be cut to make room.
3. Refuse only what is false, has no evidence, or conflicts with a §2 rule or one of
   Vaish's standing rulings (in the master resume's notes). A line a reviewer questions
   is not silently kept or silently softened: if the record supports it, it stays and goes
   under `## Lines to defend` for the human to verify or strike (§ Framing). Say which file or rule, one
   line per refusal, under `## Revision notes` in `brief.md`. That section is one line
   per fix applied or refused and nothing else; never restate the review. Every ask from
   every reviewer gets its line, so none is dropped silently. An ask for a summary or
   positioning line, or for a moved section, is refused under the page-shape rule (§2).

Then re-render, re-run preflight, and run the four reviewers again on the revised PDF.
Always. The `SCORES:` line must describe the page that ships, never an earlier render.
`requirements.json` and its search strings are fixed after round 1, so every round's screen
grades the same list; give the `ats-scanner` nothing extra.
A re-render that changes no words (a layout or text-layer defect fix) does not need a new
round; the `SCORES:` line still describes it.

Targets: ATS screen grade B or better (every basic requirement met), recruiter 8, hiring
manager 8, readability 7 (Vaish, 2026-10-16). Two rounds is the normal run. A round
that meets every target ends the run: apply the craft fixes it raised that change no claim
(bolding, a clumsy clause, a split number range), note them under the `SCORES:` line as
`Applied after round N without re-review: ...`, and ship under that round's scores. Run a
third round only when round 2 misses a target and at least one reviewer ask is still open
that you can act on (evidence the record holds, or an overclaim to remove). When every
open ask is one the record cannot answer, stop and ship: a round that can only confirm a
structural gap is wasted. Three rounds is the maximum. There is no FAIL state.

`brief.md` ends with `## Would raise the score if true` (at most three lines): facts the
reviewers asked for that nobody has asked Vaish yet. Grep `profile/knowledge/` before writing
each line. Leave it out when the record holds the fact (put it on the page instead), or
already answers it as unknown, not done or not true: a known gap in a file's Open facts or
"Facts to keep straight", a never-used tool in `50-skills.md`, or the "No experience in" list
in `60-attested-facts.md`. A domain the JD needs and his record lacks is not a question;
leave it out too. Write "none open" when nothing is left. Vaish reads it and adds what is true
to `profile/knowledge/`. It never blocks anything.

## 5. Finish

1. Assemble `review.md`: a heading `# Review - <Company> - <Title>`, then the latest
   `review-ats-r<N>.md`, `review-recruiter-r<N>.md`, `review-hm-r<N>.md`,
   `review-readability-r<N>.md` contents in that order (earlier
   rounds above them under `## Round 1`), then `READABILITY: N` (from the last
   `READABILITY_SCORE` line) on its own line, and as the LAST line, at column zero:
   `SCORES: ats NN / recruiter N / hm N` (`ats` is the screen score from the last
   `ATS_SCORE` line). Delete every `review-*-r*.md` and `screen-r*.json` file; keep
   `requirements.json` and `screen.json`.
2. Check `screen.json`'s `round` is the last review round, so the dashboard's grade
   describes the page that ships.
3. Screening draft: `python pipeline/screening_draft.py render <folder>` fills the
   compliance sections from `profile/preferences.md` verbatim (never hand-edit sections 1,
   2, 5); write sections 3 (why this company: one specific, current, named-product
   observation, checked against a live source with WebFetch or WebSearch rather than
   written from memory, and one link to his work; no flattery) and 4 (a two-sentence pitch in the
   resume's own claims) over the `>> DRAFT PENDING` markers; then
   `python pipeline/screening_draft.py check <folder>` must exit 0 (it checks the verbatim
   sections; you confirm yourself that no `>> DRAFT PENDING` marker is left).
4. `brief.md` gets a short `## Tailoring notes` (at most eight lines): which projects and
   bullets you chose and why, the bullet split per role (and why Penzigo stayed, if it did),
   the final scores and screen grade, and always this one line, which the
   dashboard shows Vaish on the card as what he must verify:
   `- Inferred beyond the written record: <claim> | <claim>` (each claim a short phrase
   naming the bullet it sits on), or `- Inferred beyond the written record: none`.
   Never leave the line out. The brief has no sections beyond the ones this file names; it is a
   working note, not a report, and every line of it is re-read on every later turn.
5. Status: `tailored`. Only a resume that could not be rendered to one page is
   `needs_review`. Write the sidecar `tailored/<id>.json` on every run (`tailorer` is
   `routine` or `local`):

```json
{"id": "<40-hex>", "folder": "applications/<folder>", "status_intent": "tailored",
 "scores": {"ats": 82, "recruiter": 8, "hm": 7}, "rounds": 1, "pages_soffice": 1,
 "resume": "Vaishnav_Ajai_Resume_<Company>.docx", "tailored_at": "<UTC ISO8601>",
 "tailorer": "routine", "model": "<model you run as>"}
```

Locally (`/tailor`) the sidecar is the whole hand-off too: never write the DB. The command
publishes the sidecar and folder to main, and the cloud fold sets the status.

## Boundaries

Never close, kill or signal another program. If a file you must write is locked (Vaish has
the PDF open in Acrobat), write to `<name>.new.pdf` or `.docx`, say so in your report, and
leave the swap to him. Never submit an application, contact anyone, or mark a job applied. Never edit the master
resume, the knowledge base, or the template during a tailor. Write only inside the
application folder (and `tailored/` in the routine). The lint hook blocks em-dashes, the
ban-list words and `[FILL]` in generated files; write clean the first time.
