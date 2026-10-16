"""PreToolUse lint hook: hard-stop banned content in generated application files.

Fires on Write/Edit. Blocks (exit 2) when the content being written to a
generated file under applications/ matches the ban rules from CLAUDE.md +
profile/voice.md.

Two exemptions, same rationale: the file is ABOUT the resume, not outbound
resume copy.
- jd.md — employer source material; real JDs legitimately contain banned words.
- review.md, review-*.md, brief.md — reviewers and the tailor's brief must be able to QUOTE the violation they
  flag ('bullet 3 uses "leveraged"'), and the critic writes prose, not resume
  copy. Linting it blocks the very finding that protects the resume. The
  resume, score.md, and screening-draft.md stay fully linted.

Deviations from the raw CLAUDE.md hook spec, both deliberate (see LEARNINGS
2026-08-14):
- PreToolUse, not PostToolUse: only PreToolUse can actually block the write;
  "hooks are hard stops" is the constitution's intent.
- En-dashes inside date ranges ("2024–2025", "June 2026 – Present") are allowed:
  Vaish mandated en-dash date ranges on the resume, so the hook masks that
  exact shape before linting. Em-dashes and all other en-dashes still block.
- Hedge verbs carry a (?<!-) guard so compound adjectives ("AI-assisted")
  pass while the hedge verb still fails.

Reviewed 2026-10-15 (Vaish asked for a keep/cut pass):
- Hedge verbs are RESUME_RULES: preflight applies them to resume bullets, where
  "worked on X" undersells. In first-person prose ("a problem I worked on at
  Fidelity") they are how a person talks, so the hook and the draft renderer
  no longer block them.
- "journey" is gone: "customer journey" and "user journey" are real PM terms
  50 JDs use, and the singular-only regex let "journeys" through anyway.
- "I'd" is added: it is on Vaish's own voice list and was never enforced.
- "leverage" as a verb (leveraged, leveraging, leverages). The bare word stays
  legal: "the Product Owner's leverage" and a leverage ratio are nouns.
- "synergy" is gone: "synergy realization" is a real M&A and finance term.

voice.md is the source of truth for the ban list; when it grows, grow this.
"""

import json
import re
import sys

MONTHS = ("January|February|March|April|May|June|July|August|September|"
          "October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|"
          "Oct|Nov|Dec|Present")
# "2024-2025" / "June 2026 - August 2026" / "2026 - Present" with en-dash
DATE_RANGE = re.compile(
    rf"(?:(?:{MONTHS})\s+)?(?:19|20)\d{{2}}\s*–\s*(?:(?:{MONTHS})(?:\s+(?:19|20)\d{{2}})?|(?:19|20)\d{{2}})")

# Files under applications/ that are about the resume rather than resume copy.
# Exempt: the JD (employer text), every review file (reviewers must be able to
# quote what they flag), and brief.md (the tailor's analysis of the JD, which
# quotes its terms). Resume text, screening-draft.md and legacy score.md stay linted.
EXEMPT = {"jd.md", "review.md", "brief.md"}

RULES = [
    # ------------------------------------------------------------------
    # Claim guards. This is the most useful part of the file and the part you
    # replace with your own. Each rule pins one fact to one wording, so a number
    # or a verb that drifted once can never drift again: the guard outlives the
    # memory of the mistake. The three below are EXAMPLES using an invented
    # project, kept to show the shape.
    #
    # Three kinds are worth encoding:
    #   1. a metric that must carry its unit
    #   2. a target that must never be written as an achievement
    #   3. a claim you retracted, which must not come back
    # ------------------------------------------------------------------
    ("cost-savings figure must carry its unit: write $45K+, not $45+",
     re.compile(r"\$45\+(?!K)")),
    ("the pilot TARGETED 15% deflection: never write 'achieved'",
     re.compile(r"(?i)achieved\s+~?15\s*%")),
    # A retraction needs a rule, not a resolution, because the memory of why a
    # phrase was wrong fades faster than the temptation to reuse it. Suppose a
    # fix only cut outages in part. "Eliminated outages" is out, while the
    # narrower "fewer outages" stays legal. That is why this rule requires the
    # verb immediately before "outages" rather than matching the noun alone.
    ("retracted claim: never write 'eliminated outages'",
     re.compile(r"(?i)eliminat(?:ed|es|ing)\s+(?:all\s+)?outages")),
    ("[FILL]/[VERIFY] markers are off-limits in output", re.compile(r"\[(FILL|VERIFY)")),
    ("em-dash (use a comma, a colon, or two sentences)", re.compile("—")),
    ("en-dash outside a date range", re.compile("–")),
    ("voice.md banned vocabulary", re.compile(
        r"(?i)(?<!-)\b(spearheaded|leverag(?:ed|es|ing)|passionate|delve"
        r"|utilized?|robust|seamless(?:ly)?|holistic|cutting-edge|tapestry"
        r"|empower(?:ed|ing)?|foster(?:ed|ing)?)\b")),
    ("'I'd' is on the voice list: write 'I would', or say it another way",
     re.compile(r"\bI['\u2019]d\b")),
    # Two constructions banned for shape rather than vocabulary, so the
    # ban-list regex above could never reach them. Both are resume cliches:
    # the X-Y-Z scaffold reads as a template, and "not just X, but Y" is filler
    # that costs a line and says nothing.
    # The \b is load-bearing: without it this matches inside "was measured by",
    # and a score.md ledger line like "the bottom gap was measured by preflight"
    # is hard-blocked with a message quoting a phrase the author never typed.
    ("X-Y-Z scaffold phrase - the checklist replaced the template, so "
     "'as measured by' must never ship on a resume", re.compile(r"(?i)\bas measured by")),
    ("'not just X, but Y' construction is on the voice.md ban list", re.compile(
        r"(?i)\bnot just\s+\w+,?\s+but\b")),
]


# Resume copy only (preflight reads these with RULES). Prose files are first
# person, where "the problem I worked on" is plain speech, not a hedge.
RESUME_RULES = [
    ("hedge verb - claim the verb the master doc supports", re.compile(
        r"(?i)(?<!-)\b(helped|assisted|contributed to|was involved in|worked on)\b")),
]


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    tool_input = payload.get("tool_input") or {}
    path = (tool_input.get("file_path") or "").replace("\\", "/")
    if "/applications/" not in path and not path.startswith("applications/"):
        return 0
    name = path.rsplit("/", 1)[-1].lower()
    if name in EXEMPT or name.startswith("review-") or not name.endswith((".md", ".txt", ".html")):
        return 0

    content = "\n".join(str(tool_input.get(k) or "")
                        for k in ("content", "new_string"))
    if not content.strip():
        return 0

    masked = DATE_RANGE.sub("", content)
    violations = []
    for why, rx in RULES:
        hits = sorted({m.group(0) for m in rx.finditer(masked)})
        if hits:
            violations.append(f"  - {why}: {', '.join(hits[:5])}")
    if violations:
        sys.stderr.write(
            f"BLOCKED by ban-list hook ({name} is a generated file):\n"
            + "\n".join(violations)
            + "\nFix the content and retry. jd.md and review.md are exempt;"
              " these files are not.\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
