"""Render and check `applications/<folder>/screening-draft.md`.

    python pipeline/screening_draft.py render <folder> [<folder> ...]
    python pipeline/screening_draft.py check  <folder> [<folder> ...]
    python pipeline/screening_draft.py backfill [--dry-run] [--all]

The contract lives in `.claude/skills/resume-tailor/SKILL.md`. This module owns
the DETERMINISTIC half of it -- sections 1, 2, 5 and the machine part of 6 --
and leaves sections 3 and 4 as `>> DRAFT PENDING` for the tailor to write. The
split is the point: compliance text never passes through a model.

Why a script and not a prose rule. Eight drafts existed before this file, every
one of them opening section 1 with the word VERBATIM, and every one of them
rendering that section differently: `: yes`, `: **yes**`, and
`**"Are you legally authorized to work in the US?"** -> `yes``. Nothing ever
compared the file to `profile/preferences.md`, so the only thing the header
guaranteed was that it said VERBATIM. `check` is that comparison.

Four rules the implementation is shaped by, each with a cost behind it:

1. ONE extractor, read at CHECK TIME. `check` re-reads `preferences.md` and
   compares through the same `extract()` that `render` used. A comparison
   against a literal in this file could only ever catch an edit to the draft,
   never a change to preferences -- the defect the old `test_sync_consistency`
   sentinel had. Fields are located by HEADING REGEX, never by line number.

2. VERBATIM spans the ANSWER BULLETS, never the section around them. Copying
   the whole `## Work authorization` section is blocked by the ban-list hook
   (rc=2): its body carries the literal `[FILL]` and two em-dashes. The visa
   line is TRANSFORMED to the `>> OWNER TO COMPLETE AT SUBMISSION` marker, which
   is what preferences.md's own blockquote instructs -- the marker is
   deliberately not `[FILL]`, which the hook blocks in every generated file.

3. Section 6 lists only the `>> OWNER TO COMPLETE` markers (the attestation
   ledger is retired). Historically it also carried ledger ids, whose prose
   was written by the cloud Routine and legitimately contained ban-list
   words -- one shipped `score.md` is already unwritable through the Write tool
   because `journey` sits inside a quoted finding. Pulling that prose into a
   linted file would make a batch backfill die on an unpredictable subset.
   Same family as `dashboard.routine_text()`.

4. RENDER SELF-LINTS BEFORE IT WRITES. `.claude/hooks/lint_generated.py` is a
   PreToolUse hook: it fires on the Write and Edit TOOLS, so a Python script
   writing into `applications/` bypasses it completely. This module would
   otherwise be the one writer of `screening-draft.md` that no ban-list rule
   reaches. It imports the hook's own `RULES` and `DATE_RANGE` rather than
   copying them, assembles the whole file in memory, lints it, and writes only
   if clean -- so a rule added to the hook covers this writer for free.

The unit of "byte-for-byte" is stated once here and enforced everywhere: a
field's value is the FIRST PHYSICAL LINE of its bullet in `preferences.md`,
carried across with its leading `- ` and no re-indenting. Continuation lines are
commentary about the answer, not the answer -- `preferences.md`'s Location
bullet continues with scorer guidance ("never a reason to down-rank a US role"),
which is pipeline doctrine and has no business on a form.
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import sys
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import dashboard  # noqa: E402
import db  # noqa: E402

ROOT = HERE.parent
APPLICATIONS = ROOT / "applications"
PREFS = ROOT / "profile" / "preferences.md"
HOOK = ROOT / ".claude" / "hooks" / "lint_generated.py"

DRAFT_NAME = "screening-draft.md"
MARKER = ">> OWNER TO COMPLETE AT SUBMISSION"
VISA_MARKER = f"{MARKER}: visa status detail"
PENDING = ">> DRAFT PENDING"

# The statuses the phase's Done-check names. `tailored`/`needs_review` are the
# live cases; `applied` is in the set because a submitted role's draft is what
# a recruiter follow-up is answered from.
TARGET_STATUSES = ("tailored", "needs_review", "applied")

# Located by heading, never by line number: preferences.md grows, and a line
# number in a doc is a scheduled false alarm (three of them in LEARNINGS).
# Anchored on the stable prefix only -- every one of these headings carries a
# drifting parenthetical.
H_AVAILABILITY = re.compile(r"^##\s+Availability\b", re.M)
H_WORK_AUTH = re.compile(r"^##\s+Work\s+authorization\b", re.M)
H_COMPENSATION = re.compile(r"^##\s+Compensation\b", re.M)
NEXT_HEADING = re.compile(r"^##\s", re.M)

# Section headings inside a draft, keyed on the NUMBER rather than the heading
# text: the text drifted across all eight pre-existing drafts ("Why me" / "Why
# you" / "Why me, in two sentences"), the number never did.
DRAFT_SECTION = re.compile(r"^##\s+(\d+[a-z]?)\.\s", re.M)

H1 = re.compile(r"^#\s+(.+)$", re.M)

_DASHES = {"—": "-", "–": "-", "−": "-"}


class DraftError(Exception):
    """A field could not be located, or a draft could not be parsed.

    Raised rather than defaulted on purpose: a silently empty compliance
    section is the worst outcome this file has, because it looks answered.
    """


@dataclass(frozen=True)
class Prefs:
    """The screening answers, each the first physical line of its bullet."""

    authorized: str
    sponsorship: str
    visa: str
    start: str
    location: str
    compensation: str
    notes: tuple[str, ...] = ()

    def section_1(self) -> list[str]:
        return [self.authorized, self.sponsorship, self.visa]

    def section_2(self) -> list[str]:
        return [self.start, self.location]

    def section_5(self) -> list[str]:
        return [self.compensation]

    def all_values(self) -> list[str]:
        return self.section_1() + self.section_2() + self.section_5()


# --------------------------------------------------------------------------
# preferences.md -> Prefs
# --------------------------------------------------------------------------

def _section_body(text: str, heading: re.Pattern, label: str) -> str:
    """The body under `heading`, up to the next column-zero `## `.

    Bounded on the section boundary as well as on its own kind -- the failure
    that ate `profile/attestations.md` for nine syncs was a block regex that
    only knew how to stop at the next thing of its own type.
    """
    m = heading.search(text)
    if not m:
        raise DraftError(f"preferences.md has no `## {label}` heading")
    rest = text[m.end():]
    nxt = NEXT_HEADING.search(rest)
    return rest[:nxt.start()] if nxt else rest


def _bullet(body: str, prefix: str, where: str) -> str:
    """The first physical line of the bullet opening with `prefix`.

    Searched inside its own section body, so a bullet with the same label under
    a different heading can never satisfy it.
    """
    for line in body.splitlines():
        if line.startswith(f"- {prefix}"):
            return line.rstrip()
    raise DraftError(
        f"preferences.md `## {where}` has no bullet starting `- {prefix}`")


def extract(prefs_path: Path | None = None) -> Prefs:
    """Read the screening answers out of preferences.md, right now.

    Both `render` and `check` come through here, which is what makes `check`
    able to notice a change to preferences rather than only a change to a draft.
    """
    path = Path(prefs_path) if prefs_path else PREFS
    text = path.read_text(encoding="utf-8")

    auth = _section_body(text, H_WORK_AUTH, "Work authorization")
    avail = _section_body(text, H_AVAILABILITY, "Availability")
    comp = _section_body(text, H_COMPENSATION, "Compensation")

    if VISA_MARKER not in text:
        raise DraftError(
            "preferences.md no longer prescribes the marker "
            f"`{VISA_MARKER}`; the two files have drifted apart and the "
            "constant here must not be the one that wins")

    notes: list[str] = []
    visa_line = _bullet(auth, "Visa status detail", "Work authorization")
    if "[FILL]" in visa_line:
        # Transformed, never copied: the hook blocks the literal [FILL] in
        # every generated file under applications/, and cannot tell a
        # deliberate marker from a leaked placeholder.
        visa_line = visa_line.replace("[FILL]", VISA_MARKER)
    else:
        # Deliberately not fatal and deliberately not silent. The whole
        # non-blocking ruling rests on this line being unfilled; if it is
        # answered, the draft must NOT start copying a real visa status into a
        # linted, committed file on its own authority -- but nor should every
        # render fail. Emit the marker, say so, let Vaish rule.
        visa_line = re.sub(r":\s*\S.*$", f": {VISA_MARKER}", visa_line)
        notes.append("visa status detail is now filled in preferences.md; the "
                     "draft still emits the marker. Rule on this before the "
                     "next backfill.")

    return Prefs(
        authorized=_bullet(auth, '"Are you legally authorized', "Work authorization"),
        sponsorship=_bullet(auth, '"Will you now', "Work authorization"),
        visa=visa_line,
        start=_bullet(avail, "Full-time start", "Availability"),
        location=_bullet(avail, "Location:", "Availability"),
        compensation=_bullet(comp, "Base salary expectation", "Compensation"),
        notes=tuple(notes),
    )


# --------------------------------------------------------------------------
# self-lint (rule 4)
# --------------------------------------------------------------------------

_hook_mod = None


def _hook():
    """The real ban-list hook, imported -- never a copy of its regexes.

    Raises if the hook has moved. That is the right failure: silently skipping
    the lint would leave this writer uncovered exactly when someone has been
    reorganising the guardrails.
    """
    global _hook_mod
    if _hook_mod is None:
        spec = importlib.util.spec_from_file_location("lint_generated", HOOK)
        if spec is None or spec.loader is None:
            raise DraftError(f"cannot load the ban-list hook at {HOOK}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _hook_mod = mod
    return _hook_mod


def lint(text: str, company: str = "") -> list[str]:
    """Ban-list violations in text about to be written. Empty means clean.

    `company` is masked first: an employer whose name is a banned word (Empower,
    2026-09-20) made every draft for it unwritable, since section 3 must name it.
    """
    h = _hook()
    masked = h.DATE_RANGE.sub("", text)
    if company.strip():
        masked = re.sub(re.escape(company.strip()), "", masked)
    out = []
    for why, rx in h.RULES:
        hits = sorted({m.group(0) for m in rx.finditer(masked)})
        if hits:
            out.append(f"{why}: {', '.join(hits[:5])}")
    return out


# --------------------------------------------------------------------------
# folder facts
# --------------------------------------------------------------------------

def _ascii(s: str) -> str:
    """Dashes folded and non-ASCII dropped before any derived text is written.

    The title is derived from `score.md`'s H1, which carries employer copy this
    repo did not author. An em-dash there would fail the self-lint.
    """
    for bad, good in _DASHES.items():
        s = s.replace(bad, good)
    return s.encode("ascii", "ignore").decode("ascii").strip()


def folder_title(folder: Path) -> str:
    """`<Company>, <Title>` from brief.md's (else score.md's) H1, else the folder name."""
    for name in ("brief.md", "score.md"):
        marker = folder / name
        if marker.is_file():
            m = H1.search(marker.read_text(encoding="utf-8", errors="replace"))
            if m:
                title = _ascii(m.group(1))
                if title:
                    return title
    return folder.name


def cited_open_ids(folder: Path, open_ids: set[str] | None = None) -> list[str]:
    """Retired 2026-09-16: the attestation ledger no longer exists, so a draft
    never cites open ids. Kept as a no-op so callers stay simple."""
    return []


# --------------------------------------------------------------------------
# render
# --------------------------------------------------------------------------

SECTION_1_PREAMBLE = [
    "Copied character for character by `pipeline/screening_draft.py`, and "
    "re-checked by its `check`",
    "subcommand. Never paraphrase these, never improve the wording, never infer "
    "one that is not",
    "written there. The resume itself carries no work-authorization or location "
    "line (ruled",
    "2026-08-24: the form asks these, the resume never carries them), so this "
    "file is the only",
    "place these answers live.",
]


GEN_OPEN = "<!-- screening-draft: generated, do not edit by hand -->"
GEN_CLOSE = "<!-- /screening-draft -->"


def _norm(line: str) -> str:
    """Loose key for de-duplicating a human line against a generated one."""
    s = re.sub(r"[*`]", "", line).strip().lstrip("-0123456789. ").lower()
    return re.sub(r"\s+", " ", s).rstrip(".:;")


def section_6_notes(folder: Path, generated: list[str]) -> list[str]:
    """Human lines in an existing section 6, minus what the block now owns.

    Section 6 is a submission checklist, so eating a line here is the one loss
    this file cannot afford: a tailor legitimately adds a SECOND
    `>> OWNER TO COMPLETE` marker (one live draft asks whether Vaish will commit
    to Pacific-time overlap) and per-role notes on why an exit was a FAIL. A
    regenerate that dropped those would silently delete open questions from the
    checklist whose entire purpose is that nothing is missed.

    Lines between the generated delimiters are ours and are replaced. Anything
    else is carried, except a legacy line that merely restates a generated one.
    """
    draft = folder / DRAFT_NAME
    if not draft.is_file():
        return []
    try:
        body = draft_sections(draft.read_text(encoding="utf-8")).get("6", [])
    except (DraftError, OSError):
        return []
    kept, skipping = [], False
    for line in body:
        if line.strip() == GEN_OPEN:
            skipping = True
            continue
        if line.strip() == GEN_CLOSE:
            skipping = False
            continue
        if not skipping:
            kept.append(line.rstrip())
    owned = {_norm(g) for g in generated}
    out, drop_continuation = [], False
    for line in kept:
        if line.strip() and not line.startswith((" ", "\t")):
            drop_continuation = _norm(line) in owned
        if drop_continuation and (not line.strip() or line.startswith((" ", "\t"))
                                  or _norm(line) in owned):
            continue
        out.append(line)
    while out and not out[0].strip():
        out.pop(0)
    while out and not out[-1].strip():
        out.pop()
    return out


def written_prose(folder: Path) -> dict[str, list[str]]:
    """Sections 3 and 4 of an existing draft, when the tailor has written them.

    Load-bearing, not a convenience. The skill tells the tailor to write 3 and 4
    over the `>> DRAFT PENDING` markers and THEN run `check`; if `check` fires,
    the documented fix is to re-render. Without this, that instruction destroys
    the prose the tailor has just written, and the better the draft the more it
    costs. A section still carrying the marker counts as unwritten and is left
    to the template.
    """
    draft = folder / DRAFT_NAME
    if not draft.is_file():
        return {}
    try:
        sections = draft_sections(draft.read_text(encoding="utf-8"))
    except (DraftError, OSError):
        return {}
    out = {}
    for key in ("3", "4"):
        body = [ln.rstrip() for ln in sections.get(key, [])]
        while body and not body[0].strip():
            body.pop(0)
        while body and not body[-1].strip():
            body.pop()
        if body and not any(ln.startswith(PENDING) for ln in body):
            out[key] = body
    return out


def build(folder: Path, prefs_path: Path | None = None,
          open_ids: set[str] | None = None,
          keep: dict[str, list[str]] | None = None,
          keep_notes: list[str] | None = None) -> tuple[str, list[str]]:
    """The full draft text plus any notes. Deterministic; no model involved."""
    p = extract(prefs_path)
    ids = cited_open_ids(folder, open_ids)
    keep = written_prose(folder) if keep is None else keep

    # The banner is the first thing read, so it has to describe THIS file and
    # not the template it came from. Fixed text kept asserting "sections 3 and
    # 4 are not written yet" after the tailor had written them, which makes the
    # one line that says what is outstanding the one line that is wrong -- in a
    # file whose whole purpose is that nothing is missed at submission.
    unwritten = [n for n in ("3", "4") if not keep.get(n)]
    if not unwritten:
        state = "Sections 3 and 4 are written."
    elif len(unwritten) == 1:
        state = f"Section {unwritten[0]} is not written yet."
    else:
        state = "Sections 3 and 4 are not written yet."

    lines: list[str] = [
        f"# Screening draft: {folder_title(folder)}",
        "",
        "**Not submittable as-is.** One line still needs Vaish at submission "
        "time; section 6 is",
        f"the checklist. {state}",
        "",
        "## 1. Work authorization (VERBATIM from profile/preferences.md)",
        "",
    ]
    lines += SECTION_1_PREAMBLE
    lines += ["", *p.section_1(), ""]
    lines += ["## 2. Availability and location", "", *p.section_2(), ""]
    lines += ["## 3. Why this company", ""]
    lines += keep.get("3") or [
        f"{PENDING}: one researched, currently-true observation about a NAMED "
        "product or platform;",
        "one line connecting it to something Vaish has actually done; no flattery, no "
        "mission-statement quoting."]
    lines += ["", "## 4. Why you (two-sentence pitch)", ""]
    lines += keep.get("4") or [
        f"{PENDING}: the same claims as the resume, in prose. No claim here "
        "that is not already", "on the resume."]
    lines.append("")
    lines += ["## 5. Compensation", "", *p.section_5(), ""]
    generated = [
        f"- {VISA_MARKER} (non-blocking by decision, 2026-08-17: needed only at "
        "the moment of",
        "  submission, which is Vaish's step anyway)",
    ]
    generated.append("- Demographic, disability, and veteran-status questions "
                     "are deliberately not drafted")
    generated.append("  here, not even as a placeholder. Those are Vaish's alone.")

    lines += ["## 6. Open before submitting", "", GEN_OPEN, *generated,
              GEN_CLOSE]
    notes = section_6_notes(folder, generated) if keep_notes is None else keep_notes
    if notes:
        lines += ["", *notes]
    lines.append("")

    return "\n".join(lines), list(p.notes)


def render(folder: Path, prefs_path: Path | None = None, apply: bool = True,
           open_ids: set[str] | None = None) -> tuple[str, list[str]]:
    """Build, self-lint, then write. Returns (text, notes).

    Nothing reaches disk until the whole file has passed the ban-list hook's
    own rules, so a violation can never leave a partial write behind.
    """
    folder = Path(folder)
    if not folder.is_dir():
        raise DraftError(f"not a folder: {folder}")
    text, notes = build(folder, prefs_path, open_ids)
    violations = lint(text, company=re.split(r",| - ", folder_title(folder))[0])
    if violations:
        raise DraftError("ban-list violation in the rendered draft, nothing "
                         "written: " + "; ".join(violations))
    if apply:
        (folder / DRAFT_NAME).write_text(text, encoding="utf-8")
    return text, notes


# --------------------------------------------------------------------------
# check
# --------------------------------------------------------------------------

def draft_sections(text: str) -> dict[str, list[str]]:
    """Section key -> its body lines, for a draft."""
    marks = list(DRAFT_SECTION.finditer(text))
    if not marks:
        raise DraftError("no numbered sections found")
    out: dict[str, list[str]] = {}
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        key = m.group(1)
        if key in out:
            raise DraftError(f"duplicate section {key}")
        # From the end of the heading LINE, not the end of the number: the
        # heading's own text is not body, and treating it as body makes it
        # look like content when a section is carried across or reported.
        nl = text.find("\n", m.end())
        start = end if nl == -1 or nl >= end else nl + 1
        out[key] = text[start:end].splitlines()
    return out


def check(folder: Path, prefs_path: Path | None = None) -> list[str]:
    """Mismatches between a draft's compliance sections and preferences.md.

    Empty list means clean. Each expected value must appear as a WHOLE LINE,
    exactly -- no added indent, no bold, no re-punctuation. That strictness is
    the feature: every one of the eight pre-existing drafts differed from
    preferences by exactly such a decoration while claiming to be verbatim.
    """
    folder = Path(folder)
    draft = folder / DRAFT_NAME
    if not draft.is_file():
        return [f"{folder.name}: no {DRAFT_NAME}"]
    try:
        p = extract(prefs_path)
        text = draft.read_text(encoding="utf-8")
        sections = draft_sections(text)
    except (DraftError, OSError) as exc:
        return [f"{folder.name}: {exc}"]

    problems = []
    for num, expected in (("1", p.section_1()), ("2", p.section_2()),
                          ("5", p.section_5())):
        body = sections.get(num)
        if body is None:
            problems.append(f"{folder.name}: section {num} is missing")
            continue
        stripped = [line.rstrip() for line in body]
        for want in expected:
            if want not in stripped:
                problems.append(
                    f"{folder.name}: section {num} does not carry, verbatim: {want}")
    if MARKER in text and MARKER not in "\n".join(sections.get("6", [])):
        problems.append(f"{folder.name}: a {MARKER} marker is not listed in "
                        "section 6")
    return problems


# --------------------------------------------------------------------------
# sweep
# --------------------------------------------------------------------------

def _status_by_id() -> dict[str, str]:
    conn = db.connect_readonly()
    try:
        return {r["id"]: r["status"]
                for r in conn.execute("SELECT id, status FROM jobs")}
    finally:
        conn.close()


def backfill_targets() -> list[Path]:
    """Folders the sweep owns: the Done-check set, plus every existing draft.

    The second half matters -- it is how a pre-existing draft gets regenerated
    in the one folder whose `score.md` carries a Greenhouse id rather than a
    job hash, which no id map can parse because there is no id there to find.
    It rescues only a folder that ALREADY has a draft, though, so it is not a
    substitute for the map: an `applied` folder went two days with no draft at
    all on exactly that assumption (2026-09-01). Globbed one level deep on
    purpose: a nested application folder exists under `applications/datadog-...`
    and is a separate defect, not this sweep's work.
    """
    status = _status_by_id()
    out = {folder.resolve()
           for jid, folder in dashboard.application_folders().items()
           if status.get(jid) in TARGET_STATUSES}
    out |= {p.parent.resolve() for p in APPLICATIONS.glob(f"*/{DRAFT_NAME}")}
    return sorted(out, key=lambda p: p.name)


def all_folders() -> list[Path]:
    folders = {p.parent.resolve() for name in ("brief.md", "score.md")
               for p in APPLICATIONS.glob(f"*/{name}")}
    return sorted(folders, key=lambda p: p.name)


def nested_drafts() -> list[Path]:
    """Drafts below the one-level sweep. Reported so the gap is visible."""
    return sorted(p for p in APPLICATIONS.glob(f"*/*/{DRAFT_NAME}"))


def dropped_lines(folder: Path, p: Prefs) -> list[str]:
    """Content in an existing draft that a regenerate will not carry over.

    Vaish ruled full-regenerate on 2026-08-30, so this does not change what is
    written -- it makes the cost visible at the moment it is paid. Several of
    these are role-specific FACTS rather than prose (a posted salary band, a
    "this employer will not sponsor" note), which is exactly the kind of thing
    that should be read before it goes, not after.
    """
    draft = folder / DRAFT_NAME
    if not draft.is_file():
        return []
    try:
        sections = draft_sections(draft.read_text(encoding="utf-8"))
    except (DraftError, OSError) as exc:
        return [f"(unparsed: {exc})"]
    canonical = {v.strip() for v in p.all_values()}
    kept = set(written_prose(folder))
    out = []
    for key in sorted(sections, key=lambda k: (len(k), k)):
        if key in kept or key == "6":
            continue  # carried across verbatim, so nothing is lost here
        body = [ln.strip() for ln in sections[key] if ln.strip()]
        if key in ("1", "2", "5"):
            # A regenerate keeps only the canonical bullets, so any other
            # bullet or note in a compliance section is content that goes.
            extra = [ln for ln in body if ln not in canonical
                     and not ln.startswith(">")]
            extra = [ln for ln in extra if ln not in
                     {x.strip() for x in SECTION_1_PREAMBLE}]
        elif key in ("3", "4"):
            extra = [ln for ln in body if not ln.startswith(PENDING)]
        else:
            extra = body
        for ln in extra[:4]:
            out.append(f"    s{key}: {ln[:104]}")
        if len(extra) > 4:
            out.append(f"    s{key}: ... and {len(extra) - 4} more line(s)")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("render", help="write the draft for each folder")
    r.add_argument("folders", nargs="+")

    c = sub.add_parser("check", help="compare a draft against preferences.md")
    c.add_argument("folders", nargs="+")

    b = sub.add_parser("backfill", help="sweep the target set")
    b.add_argument("--dry-run", action="store_true",
                   help="write nothing; print the plan and what would be lost")
    b.add_argument("--all", action="store_true",
                   help="every folder with a score.md, not just the target set")

    args = ap.parse_args()

    try:
        if args.cmd == "render":
            rc = 0
            for f in args.folders:
                try:
                    _text, notes = render(Path(f))
                    rel = (Path(f) / DRAFT_NAME).resolve()
                    print(f"wrote {rel.relative_to(ROOT).as_posix()}")
                    for n in notes:
                        print(f"  WARN  {n}")
                except (DraftError, OSError) as exc:
                    print(f"ERROR  {f}: {exc}")
                    rc = 1
            return rc

        if args.cmd == "check":
            problems = []
            for f in args.folders:
                problems.extend(check(Path(f)))
            for line in problems:
                print(f"ERROR  {line}")
            bad = {p.split(":")[0] for p in problems}
            print(f"{len(args.folders) - len(bad)} of {len(args.folders)} "
                  "folder(s) carry a draft that matches preferences.md")
            return 1 if problems else 0

        folders = all_folders() if args.all else backfill_targets()
        prefs = extract()
        open_ids = set()
        print(f"{len(folders)} folder(s) in scope"
              f"{' (--all)' if args.all else ''}")
        for n in prefs.notes:
            print(f"WARN  {n}")
        rc = 0
        for f in folders:
            existing = (f / DRAFT_NAME).is_file()
            verb = "REGENERATE" if existing else "create    "
            if args.dry_run:
                # A dry run is a claim about the whole command, not about its
                # writes: no branch below this one touches the disk.
                print(f"  {verb}  {f.name}")
                for line in dropped_lines(f, prefs):
                    print(f"  {line}")
                continue
            try:
                render(f, open_ids=open_ids)
                print(f"  {verb}  {f.name}")
            except (DraftError, OSError) as exc:
                print(f"  ERROR       {f.name}: {exc}")
                rc = 1
        for p in nested_drafts():
            print(f"note: {p.relative_to(ROOT).as_posix()} is below the "
                  "one-level sweep and was not touched")
        if args.dry_run:
            print("dry run: nothing was written")
        return rc
    except DraftError as exc:
        print(f"ERROR  {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
