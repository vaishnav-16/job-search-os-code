"""Tests for the screening-draft renderer and its compliance check.
Run: python pipeline/test_screening_draft.py

What is being guarded. Section 1 of a screening draft is compliance text that
Vaish pastes into an employer's form. Eight drafts existed before this module,
all eight declared themselves VERBATIM, and all eight rendered that section
differently -- so the word VERBATIM was the only thing that stayed constant.
The failure is silent by construction: a wrong-but-plausible answer looks
exactly like a right one, and nothing downstream reads the file except a human
in a hurry.

Three tests here carry most of the weight, and each pins a way this could pass
for the wrong reason:

  - `check` READS preferences at check time. A comparison against a literal in
    the module would catch an edit to a draft and never a change to
    preferences. Proved by pointing the module at a second preferences file and
    watching an UNCHANGED draft start failing.
  - the ban-list hook harness is WIRED. `lint_generated.main` returns 0
    immediately unless `file_path` contains `/applications/`, so a probe using
    a plain temp path is green for every input, forever. The negative control
    runs first.
  - copying the whole `## Work authorization` SECTION is BLOCKED. That is the
    empirical fact the extractor's design rests on, so it is asserted against
    the live preferences.md rather than a fixture -- if someone "simplifies"
    the extractor to take the section, this goes red.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import screening_draft as sd  # noqa: E402

ROOT = HERE.parent
HOOK = ROOT / ".claude" / "hooks" / "lint_generated.py"

FAILS = []


def check(label, cond, detail=""):
    if cond:
        print(f"  ok  {label}")
    else:
        print(f"  FAIL  {label}  {detail}")
        FAILS.append(label)


SKIPS = []


def skip(name, why):
    """Skip a leg that needs a private file this public copy does not carry.

    Prints for a direct `python pipeline/test_screening_draft.py` run, and
    raises a real pytest skip when the suite is collected by pytest.
    """
    print(f"  skip  {name} ({why})")
    SKIPS.append(name)
    pytest = sys.modules.get("pytest")
    if pytest is not None:
        pytest.skip(f"{name}: {why}")


# The real answers live at profile/preferences.md, which is not published.
# This suite reads the example fixture instead; screening_draft.PREFS itself
# is untouched.
PREFS_FIXTURE = HERE / "testdata" / "preferences.md"


# A miniature preferences.md. Carries a decoy `- Location:` bullet under an
# unrelated heading, to prove the extractor scopes bullets to their section.
PREFS_MIN = """# preferences.md

## Role targeting (feeds the scorer)
- Location: DECOY, must never be picked up.

## Availability
- Full-time start: 26-11-2026
- Location: **open to relocation anywhere in the US; no no-go cities** (attested 2026-08-17).
  Location is therefore never a reason to down-rank or skip a US role.

## Work authorization (VERBATIM answers - fill precisely)
- "Are you legally authorized to work in the US?": yes
- "Will you now or in the future require sponsorship?": no
- Visa status detail (for forms that ask): [FILL]
  > marks the line `>> OWNER TO COMPLETE AT SUBMISSION: visa status detail`.

## Compensation (for forms that force a number)
- Base salary expectation: market-rate
"""

SCORE_MIN = """# Acme, Product Manager, Payments Platform

**id:** `0123456789abcdef0123456789abcdef01234567`

- R1-1 -> attestation pending, rides A-99. And A-01, long resolved.
"""


def sandbox(prefs: str = PREFS_MIN, score: str | None = SCORE_MIN):
    """A temp folder plus a temp preferences path, as (folder, prefs_path)."""
    tmp = Path(tempfile.mkdtemp(prefix="screening-draft-test-"))
    folder = tmp / "acme-product-manager-2026-08-30"
    folder.mkdir()
    if score is not None:
        (folder / "score.md").write_text(score, encoding="utf-8")
    prefs_path = tmp / "preferences.md"
    prefs_path.write_text(prefs, encoding="utf-8")
    return folder, prefs_path


def hook(content: str, name: str = "screening-draft.md") -> int:
    """Run the REAL PreToolUse hook over a candidate file body."""
    payload = {"tool_input": {
        "file_path": f"applications/probe-folder-2026-08-30/{name}",
        "content": content}}
    r = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload),
                       capture_output=True, text=True, errors="replace",
                       cwd=str(ROOT))
    return r.returncode


# --------------------------------------------------------------------------

def test_extract():
    _folder, prefs = sandbox()
    p = sd.extract(prefs)
    check("the authorization answer is the whole bullet, verbatim",
          p.authorized == '- "Are you legally authorized to work in the US?": yes',
          repr(p.authorized))
    check("the sponsorship answer is the whole bullet, verbatim",
          p.sponsorship == '- "Will you now or in the future require sponsorship?": no',
          repr(p.sponsorship))
    check("the visa value is never copied",
          "[FILL]" not in p.visa and sd.VISA_MARKER in p.visa, repr(p.visa))
    check("the start date is found", "26-11-2026" in p.start, repr(p.start))
    check("only the FIRST line of the Location bullet is taken",
          "down-rank" not in p.location and "no no-go cities" in p.location,
          repr(p.location))
    check("the compensation answer is found",
          p.compensation == "- Base salary expectation: market-rate",
          repr(p.compensation))
    check("a bullet is scoped to its own section (the decoy is not picked up)",
          "DECOY" not in p.location, repr(p.location))


def test_extract_fails_loudly():
    for drop, label in (
            ("## Work authorization", "a missing heading raises"),
            ('- "Will you now', "a missing bullet raises")):
        broken = "\n".join(ln for ln in PREFS_MIN.splitlines()
                           if not ln.startswith(drop))
        _folder, prefs = sandbox(prefs=broken)
        try:
            sd.extract(prefs)
            check(label, False, "no exception")
        except sd.DraftError as exc:
            check(label, True)
            check(f"{label} -- and names what is missing",
                  "Work authorization" in str(exc) or "Will you now" in str(exc),
                  str(exc))

    # A reworded parenthetical must still parse: heading TEXT drifts, and a
    # pattern anchored on the whole line would rot the way a line number does.
    reworded = PREFS_MIN.replace("## Work authorization (VERBATIM answers - fill precisely)",
                                 "## Work authorization (compliance)")
    _folder, prefs = sandbox(prefs=reworded)
    try:
        sd.extract(prefs)
        check("a reworded heading parenthetical still parses", True)
    except sd.DraftError as exc:
        check("a reworded heading parenthetical still parses", False, str(exc))

    # The marker constant here must not be able to outlive preferences.md.
    no_marker = PREFS_MIN.replace(
        "`>> OWNER TO COMPLETE AT SUBMISSION: visa status detail`", "`nothing`")
    _folder, prefs = sandbox(prefs=no_marker)
    try:
        sd.extract(prefs)
        check("preferences dropping the marker is a loud failure", False,
              "no exception")
    except sd.DraftError:
        check("preferences dropping the marker is a loud failure", True)


def test_visa_filled_is_warned_not_copied():
    """The one case where failing closed and failing open are both wrong."""
    filled = PREFS_MIN.replace(
        "- Visa status detail (for forms that ask): [FILL]",
        "- Visa status detail (for forms that ask): EXAMPLE STATUS, valid to 2030-01-01")
    _folder, prefs = sandbox(prefs=filled)
    p = sd.extract(prefs)
    check("a filled visa line is never copied into the draft",
          "EXAMPLE STATUS" not in p.visa and sd.VISA_MARKER in p.visa, repr(p.visa))
    check("a filled visa line is reported rather than swallowed",
          any("filled" in n for n in p.notes), repr(p.notes))


def test_render():
    folder, prefs = sandbox()
    text, _notes = sd.render(folder, prefs)
    for want in sd.extract(prefs).all_values():
        check(f"the draft carries verbatim: {want[:46]}...",
              want in text.splitlines())
    check("sections 3 and 4 are left pending",
          text.count(sd.PENDING) == 2, str(text.count(sd.PENDING)))
    check("no [FILL] anywhere in the draft", "[FILL]" not in text)
    check("the draft says it is not submittable as-is",
          "Not submittable as-is" in text)

    first = (folder / sd.DRAFT_NAME).read_bytes()
    sd.render(folder, prefs)
    second = (folder / sd.DRAFT_NAME).read_bytes()
    check("render is idempotent (bytes identical on a second run)",
          first == second)
    sd.render(folder, prefs)
    check("render is idempotent on a THIRD run (section 6 does not grow)",
          (folder / sd.DRAFT_NAME).read_bytes() == first)


def test_render_preserves_written_prose():
    """The skill says: write 3 and 4, then run check; if check fires, re-render.

    Without preservation that instruction destroys the prose the tailor just
    wrote, and the better the draft the more it costs.
    """
    folder, prefs = sandbox()
    sd.render(folder, prefs)
    draft = folder / sd.DRAFT_NAME
    text = draft.read_text(encoding="utf-8")

    prose3 = "Acme's settlement product publishes its own break rates."
    prose4 = "Three years on regulated trade workflows at Citi."
    # Replace each PENDING block (marker line plus its continuation) wholesale,
    # the way a tailor writing over the markers would.
    sections = sd.draft_sections(text)
    text2 = text
    for key, prose in (("3", prose3), ("4", prose4)):
        block = "\n".join(ln for ln in sections[key] if ln.strip())
        text2 = text2.replace(block, prose)
    draft.write_text(text2, encoding="utf-8")

    check("a hand-written section 3 is detected as written",
          sd.written_prose(folder).get("3") == [prose3],
          repr(sd.written_prose(folder).get("3")))
    sd.render(folder, prefs)
    after = draft.read_text(encoding="utf-8")
    check("re-render keeps a written section 3", prose3 in after)
    check("re-render keeps a written section 4", prose4 in after)
    check("re-render still leaves no pending marker behind",
          sd.PENDING not in after)
    check("re-render still fixes the compliance sections",
          sd.check(folder, prefs) == [], repr(sd.check(folder, prefs)))

    before = draft.read_bytes()
    sd.render(folder, prefs)
    check("render with preserved prose is still idempotent",
          draft.read_bytes() == before)

    # The banner is the first line read and the only one that says what is
    # outstanding. Fixed text made it the only WRONG line once the tailor had
    # written 3 and 4, in a file that exists so nothing is missed.
    check("the banner stops claiming 3 and 4 are unwritten once they are",
          "Sections 3 and 4 are written." in after
          and "not written yet" not in after.split("## 1.")[0])

    # A section still carrying the marker is NOT written, and must not be
    # preserved as if it were -- otherwise the template can never come back.
    folder2, prefs2 = sandbox()
    sd.render(folder2, prefs2)
    check("a pending section is not mistaken for written prose",
          sd.written_prose(folder2) == {}, repr(sd.written_prose(folder2)))

    # Section 6 is a submission checklist, so eating a line here deletes an
    # open question from the one place that exists to stop things being missed.
    note = f"- {sd.MARKER}: whether you will commit to Pacific Time overlap."
    text3 = draft.read_text(encoding="utf-8").replace(
        sd.GEN_CLOSE, f"{sd.GEN_CLOSE}\n\n{note}")
    draft.write_text(text3, encoding="utf-8")
    sd.render(folder, prefs)
    kept = draft.read_text(encoding="utf-8")
    check("a second OWNER TO COMPLETE marker in section 6 survives a re-render",
          note in kept)
    check("the generated block is still replaced, not duplicated",
          kept.count(sd.GEN_OPEN) == 1, str(kept.count(sd.GEN_OPEN)))
    before6 = draft.read_bytes()
    sd.render(folder, prefs)
    check("section 6 notes do not accumulate on a re-render",
          draft.read_bytes() == before6)

    # A legacy draft has no delimiters, and its section 6 restates what the
    # generated block now owns. Those restatements must not come back as notes.
    legacy = kept.replace(sd.GEN_OPEN, "").replace(sd.GEN_CLOSE, "")
    draft.write_text(legacy, encoding="utf-8")
    sd.render(folder, prefs)
    final = draft.read_text(encoding="utf-8")
    check("a legacy section 6 does not duplicate the generated lines",
          final.count("Demographic, disability") == 1,
          str(final.count("Demographic, disability")))
    check("...but its genuinely extra marker still survives", note in final)


def test_check_fires():
    folder, prefs = sandbox()
    sd.render(folder, prefs)
    check("a freshly rendered draft is clean", sd.check(folder, prefs) == [])

    draft = folder / sd.DRAFT_NAME
    original = draft.read_text(encoding="utf-8")

    for mutation, label in (
            ('- "Are you legally authorized to work in the US?": yes',
             '- "Are you legally authorized to work in the US?": **yes**'),
            ('- Base salary expectation: market-rate',
             '- Base salary expectation: negotiable')):
        draft.write_text(original.replace(mutation, label), encoding="utf-8")
        problems = sd.check(folder, prefs)
        check(f"check fires on a mutated answer ({label[:40]}...)",
              len(problems) == 1, repr(problems))

    # A deleted bullet is a different failure from a decorated one.
    draft.write_text(
        "\n".join(ln for ln in original.splitlines()
                  if not ln.startswith('- "Will you now')), encoding="utf-8")
    check("check fires on a deleted compliance bullet",
          any("Will you now" in p for p in sd.check(folder, prefs)))

    draft.write_text(original, encoding="utf-8")
    check("check is clean again once the draft is restored",
          sd.check(folder, prefs) == [])

    # The contract is a non-zero EXIT, so assert it through the CLI.
    draft.write_text(original.replace(": yes", ": **yes**"), encoding="utf-8")
    saved = sd.PREFS
    try:
        sd.PREFS = prefs
        rc = subprocess.run(
            [sys.executable, str(HERE / "screening_draft.py"), "check",
             str(folder)], capture_output=True, text=True, cwd=str(ROOT),
            env={**__import__("os").environ}).returncode
    finally:
        sd.PREFS = saved
    check("the check subcommand exits non-zero on a mismatch", rc == 1, str(rc))


def test_check_reads_preferences_at_check_time():
    """The anti-literal test: prove the comparison follows the FILE."""
    folder, prefs = sandbox()
    sd.render(folder, prefs)
    check("clean against the preferences it was rendered from",
          sd.check(folder, prefs) == [])

    moved = prefs.parent / "preferences-moved.md"
    moved.write_text(PREFS_MIN.replace("26-11-2026", "01-01-2027"),
                     encoding="utf-8")
    problems = sd.check(folder, moved)
    check("the UNCHANGED draft fails once preferences changes",
          any("Full-time start" in p for p in problems), repr(problems))


def test_the_real_ban_list_hook():
    # Negative control FIRST. Without it every assertion below is green for
    # every input, because the hook no-ops on a path outside applications/.
    check("the hook harness is wired (a banned word is blocked)",
          hook("we leveraged the platform") == 2)
    check("the hook harness passes clean text", hook("a plain sentence") == 0)
    # Reviewed 2026-10-15: first-person prose may say "worked on"; "I'd" is banned.
    check("a first-person 'I worked on' passes in prose",
          hook("That is the problem I worked on at Fidelity.") == 0)
    check("'I'd' is blocked in prose", hook("I'd love to talk.") == 2)
    check("'customer journey' passes", hook("Customer journey mapping") == 0)
    check("'leveraging' the verb is blocked", hook("We are leveraging data.") == 2)
    check("'leverage' the noun passes", hook("the Product Owner's leverage is limited") == 0)

    folder, prefs = sandbox()
    text, _ = sd.render(folder, prefs, apply=False)
    check("a rendered draft passes the real ban-list hook", hook(text) == 0)

    # The empirical fact the extractor is built on, asserted against the LIVE
    # preferences.md so a future simplification of extract() goes red here.
    live = PREFS_FIXTURE.read_text(encoding="utf-8")
    start = live.index("## Work authorization")
    end = live.index("## Compensation")
    check("copying the whole Work authorization SECTION is blocked",
          hook(live[start:end]) == 2)

    # render self-lints: the hook is PreToolUse, so a script write bypasses it.
    saved = sd.SECTION_1_PREAMBLE[:]
    draft = folder / sd.DRAFT_NAME
    sd.render(folder, prefs)          # a real file to prove is left untouched
    before = draft.read_bytes()
    try:
        sd.SECTION_1_PREAMBLE[0] = "We utilized a robust approach."
        try:
            sd.render(folder, prefs)
            check("render refuses to write a ban-list violation", False,
                  "no exception")
        except sd.DraftError as exc:
            check("render refuses to write a ban-list violation",
                  "ban-list" in str(exc), str(exc))
        check("...and the file on disk is untouched", draft.read_bytes() == before)
    finally:
        sd.SECTION_1_PREAMBLE[:] = saved


def test_the_live_corpus():
    if not sd.APPLICATIONS.exists():
        return skip("live corpus sweep",
                    "applications/ holds submitted resumes and is not published")
    p = sd.extract()
    check("the live preferences.md extracts cleanly",
          all(v.startswith("- ") for v in p.all_values()))
    check("the live extract raises no unresolved note", p.notes == (),
          repr(p.notes))

    targets = sd.backfill_targets()
    check("the sweep finds the target set", len(targets) > 0, str(len(targets)))
    existing = {q.parent.resolve()
                for q in sd.APPLICATIONS.glob(f"*/{sd.DRAFT_NAME}")}
    check("every folder that already has a draft is in the sweep",
          existing <= set(targets),
          str(sorted(f.name for f in existing - set(targets))))

    # Every target renders without raising, in memory. This is the check that
    # a real backfill will not die halfway.
    open_ids = set()
    bad = []
    for f in targets:
        try:
            sd.render(f, apply=False, open_ids=open_ids)
        except sd.DraftError as exc:
            bad.append(f"{f.name}: {exc}")
    check("every target folder renders clean, in memory", not bad, repr(bad[:3]))

    # LEARNINGS 2026-08-18 made durable: a dry run is a claim about the whole
    # command. Hash the live drafts, run the real dry run, re-hash.
    def digests():
        return {q: hashlib.sha256(q.read_bytes()).hexdigest()
                for q in sorted(sd.APPLICATIONS.glob(f"**/{sd.DRAFT_NAME}"))}

    before = digests()
    r = subprocess.run([sys.executable, str(HERE / "screening_draft.py"),
                        "backfill", "--dry-run"], capture_output=True,
                       text=True, errors="replace", cwd=str(ROOT))
    check("the dry-run sweep exits 0", r.returncode == 0, r.stderr[-400:])
    check("the dry-run sweep says it wrote nothing",
          "dry run: nothing was written" in r.stdout)
    check("the dry-run sweep mutated no live draft", digests() == before)
    # Conditional on purpose. The corpus had exactly one nested draft until
    # 2026-09-01, and it held the only copy of ~215 words of real prose -- the
    # sweep reported it in every dry run for two days and nobody read the note.
    # Asserting the note unconditionally would now require a nested folder to
    # exist forever, which is asserting the defect. Assert the reporting
    # instead: named when there is one, silent when there is not.
    named = "below the one-level sweep" in r.stdout
    check("the sweep names a nested draft when one exists, and only then",
          named == bool(sd.nested_drafts()),
          f"named={named} nested={[str(p) for p in sd.nested_drafts()]}")


def test_anomalous_folders():
    if not sd.APPLICATIONS.exists():
        return skip("anomalous folder sweep",
                    "applications/ holds submitted resumes and is not published")
    """Four dirs hold only a jd.md; one score.md id is unparseable."""
    targets = {f.name for f in sd.backfill_targets()}
    jd_only = {p.parent.name for p in sd.APPLICATIONS.glob("*/jd.md")
               if not (p.parent / "score.md").is_file()
               and not (p.parent / "brief.md").is_file()}
    check("a jd.md-only folder is never swept into",
          not (jd_only & targets), str(sorted(jd_only & targets)))

    # The Greenhouse-id folder is invisible to application_folders() and always
    # will be -- its score.md carries a posting number, not a job hash, so there
    # is no id to parse however lenient the pattern gets. It is in the sweep
    # only because it already has a draft, which is the whole reason
    # backfill_targets() unions the glob in. It is now the ONLY folder relying
    # on that clause: the unfenced ids the clause used to cover are mapped
    # (dashboard.ID_LINE), because the clause cannot create a first draft.
    datadog = sd.APPLICATIONS / "datadog-product-management-intern-2026-08-20"
    if (datadog / sd.DRAFT_NAME).is_file():
        check("a folder with an unparseable score.md id is still swept",
              datadog.name in targets)


def main() -> int:
    print("extractor")
    test_extract()
    print("extractor failure modes")
    test_extract_fails_loudly()
    test_visa_filled_is_warned_not_copied()
    print("prose preservation")
    test_render_preserves_written_prose()
    print("render")
    test_render()
    print("section 6")
    print("check")
    test_check_fires()
    test_check_reads_preferences_at_check_time()
    print("the real ban-list hook")
    test_the_real_ban_list_hook()
    print("live corpus")
    test_the_live_corpus()
    test_anomalous_folders()
    print()
    if FAILS:
        print(f"{len(FAILS)} failed: {', '.join(FAILS)}")
        return 1
    print("all screening-draft tests pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
