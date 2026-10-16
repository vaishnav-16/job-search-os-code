# Preferences (EXAMPLE FIXTURE)

This is the synthetic stand-in for `profile/preferences.md`, which is not published.
It carries the headings and bullet labels `pipeline/screening_draft.py` reads, with
invented answers, so the screening-draft tests can run against a real file. Supply
your own at `profile/preferences.md` in the same shape.

## Role targeting (feeds the scorer)
- Titles: Product Manager, Associate Product Manager, Technical Program Manager
- Seniority: 0-4 years of experience asked

## Availability
- Full-time start: available from January 2027
- Location: **open to relocation anywhere in the US; no no-go cities**

## Work authorization (VERBATIM answers, fill precisely, this is compliance-sensitive)
- "Are you legally authorized to work in the US?": yes
- "Will you now or in the future require sponsorship?": no
- Visa status detail: [FILL]
<!-- The [FILL] marker above is DELIBERATE test input, not an unfinished field.
     screening_draft.py transforms it into a 'complete at submission' marker rather
     than copying a real visa status into a committed file, and the ban-list hook
     rejects the literal marker in generated output. test_screening_draft.py asserts
     both behaviours against this line, so removing it breaks the suite. -->

## Compensation (for forms that force a number)
- Base salary expectation: <your number, or a range>
