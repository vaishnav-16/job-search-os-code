"""Tests for parse_back.py — script-style, same discipline as test_preflight.

Run: python pipeline/test_parse_back.py

Three legs, deliberately separated by what each one needs:

1. SYNTHETIC LINES — one case per diff class, no renderer, always runs. A suite
   that only runs where soffice is installed is a suite that stops running
   (LEARNINGS 2026-09-01: a suite you exclude from the routine run is not a
   suite).
2. expected() COMPLETENESS against the LIVE `profile/knowledge/record.json`.
   Every employer, sanctioned title variant and date the record holds must come
   through; a record entry that silently stopped parsing would drop an employer
   and make every resume ERROR.
3. ROUND-TRIP over the real template, gated on a renderer being present. The
   plan's wording was "untouched template -> 0 diffs"; the honest translation
   is 0 ERROR-grade diffs, because the template genuinely does carry a bare
   Penzigo year and an end-date-only Education line where the record holds
   the full months. Those two WARNs are PINNED so they cannot
   silently grow into a third.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import docx_engine  # noqa: E402
import parse_back as P  # noqa: E402

PASS = FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"FAIL  {name}" + (f" — {detail}" if detail else ""))


SKIP = 0


def skip(name: str, why: str) -> None:
    """Skip a leg that needs a private file this public copy does not carry."""
    global SKIP
    SKIP += 1
    print(f"skip  {name} ({why})")


def errs(findings):
    return [m for g, m in findings if g == "error"]


def warns(findings):
    return [m for g, m in findings if g == "warn"]


DASH = "–"
GLYPH = ""

# A minimal but REAL page shape: bands, employer lines, role lines, a bullet
# first line carrying the soffice glyph, and a bullet continuation line that
# opens on an acronym — the shape that would misparse as an employer line if
# split_employer had no remainder guard.
def page(fidelity_end=f"June 2026 {DASH} August 2026",
         citi_emp="Pune, India | July 2022 " + DASH + " June 2025",
         penzigo=f"May 2021 {DASH} July 2021",
         edu=f"September 2025 {DASH} December 2026",
         bank_title="Product Owner, Payments",
         extra=()):
    return [
        "Vaishnav Ajai",
        "WORK EXPERIENCE",
        "FIDELITY INVESTMENTS Merrimack, NH",
        f"Product Management Intern, API Platform {fidelity_end}",
        f"{GLYPH} Took a firmwide API migration to 100% completion on schedule",
        "API by the firm cutoff, with weekly office hours and a dashboard",
        f"CITIGROUP {citi_emp}" if citi_emp else "CITIGROUP Pune, India",
        f"{bank_title} October 2024 {DASH} June 2025",
        f"{GLYPH} Drove a phased product roadmap migrating legacy services",
        f"Product Analyst, Regulatory Reporting August 2023 {DASH} September 2024",
        f"{GLYPH} Led delivery of Japan and EU trade reporting programs",
        f"ML Engineer, Predictive Analytics July 2022 {DASH} July 2023",
        f"{GLYPH} Scoped the Trade Fail Prediction product with Operations",
        "PENZIGO Remote",
        f"Product Management Intern {penzigo}",
        f"{GLYPH} Integrated the Converge Payment Gateway API (Elavon)",
        *extra,
        "EDUCATION",
        "DARTMOUTH COLLEGE Hanover, NH",
        f"Master of Engineering Management | Product Management Track {edu}",
        "Courses: Product Design & Development, Platform Strategy & Management",
        "NATIONAL INSTITUTE OF TECHNOLOGY KARNATAKA Surathkal, India",
        f"Bachelor of Technology in Electronics and Communication Engineering July 2018 {DASH} May 2022",
        "SKILLS & INTERESTS",
        "Technical: Payments, REST API, SQL, Python, Java, Machine Learning",
    ]


def main() -> int:
    print("\nexpected() fails LOUDLY on a malformed record:")
    with tempfile.TemporaryDirectory() as td:
        broken = Path(td) / "record.json"
        broken.write_text('{"work": [{"roles": []}]}', encoding="utf-8")
        try:
            P.expected(broken)
            check("an entry without an employer raises AnchorError", False, "no raise")
        except P.AnchorError as exc:
            check("an entry without an employer raises AnchorError", True)
            check("the AnchorError names the file", "record.json" in str(exc), str(exc))
        missing = Path(td) / "nope.json"
        check("an ABSENT record degrades to empty, it does not raise",
              P.expected(missing).entries == [])
        note = P.check(HERE / "nothing.pdf", record=missing)
        check("check() on an absent record returns a warn, never an error",
              note and not errs(note) and "parse-back skipped" in warns(note)[0],
              str(note))

    # Sanctioned title variants, checked against the INVENTED example record so
    # this leg runs in every clone.
    print("\nsanctioned title variants (testdata/record.example.json):")
    example = HERE / "testdata" / "record.example.json"
    ex = P.sanctioned_titles(example)
    check("the invented record parses: three employers",
          [e.employer for e in P.expected(example).entries]
          == ["Example Bank", "Acme Corp", "Example University"],
          str([e.employer for e in P.expected(example).entries]))
    check("the functional title and the official variant are both sanctioned",
          {"Product Owner", "Product Owner, Payments", "Operations Associate II"}
          <= ex["Product Owner"], str(ex))
    check("a genericized TEAM half draws no finding",
          P._title_ok("Product Owner, Card Disputes", ex["Product Owner"]))
    check("a program title the record does not list is not sanctioned",
          not P._title_ok("Technical Program Manager, Payments", ex["Product Owner"]))
    check("an unsanctioned ROLE token is not sanctioned",
          not P._title_ok("Senior Director, Payments", ex["Product Owner"]))

    # Everything below diffs a rendered page against the canonical record.
    # profile/knowledge/record.json is not published, so these legs need the
    # record you supply. The negative-path checks above ran without one.
    if not P.RECORD.exists():
        skip("record-anchored diff legs",
             "profile/knowledge/record.json is not published; supply your own")
        print(f"\n{PASS} passed, {FAIL} failed, {SKIP} skipped")
        return 1 if FAIL else 0

    rec = P.expected()
    titles = P.sanctioned_titles()

    # ---------------------------------------------------------------- leg 2
    print("expected() against the live record.json:")
    names = [e.employer for e in rec.entries]
    check("all five record employers resolve",
          names == ["Fidelity Investments", "Citigroup", "Penzigo",
                    "Dartmouth College", "NIT Karnataka"], str(names))
    citi = [e for e in rec.entries if e.employer == "Citigroup"][0]
    check("Citi holds three roles", len(citi.roles) == 3, str(citi.roles))
    check("Citi role months come off the record",
          citi.roles[0].start == (2024, 10) and citi.roles[2].end == (2023, 7),
          str(citi.roles))
    fid = rec.entries[0]
    check("the Fidelity span parses",
          fid.roles[0].start == (2026, 6) and fid.roles[0].end == (2026, 8),
          f"{P.show(fid.roles[0].start)}..{P.show(fid.roles[0].end)}")
    dart = [e for e in rec.entries if e.employer == "Dartmouth College"][0]
    check("Dartmouth carries a start and an end",
          dart.roles[0].start == (2025, 9) and dart.roles[0].end == (2026, 12),
          str(dart.roles))
    check("every record title is one of its own sanctioned variants",
          all(t in v for t, v in titles.items()), str(titles))

    # ---------------------------------------------------------------- leg 1
    print("\nsynthetic pages — one case per diff class:")
    clean = P.diff(P.parse_lines(page()), rec, titles)
    check("a fully correct page produces NO findings at all",
          not clean, str(clean))

    parsed = P.parse_lines(page())
    check("the bullet continuation opening on 'API' is not read as an employer",
          [e.employer for e in parsed.entries] == [
              "FIDELITY INVESTMENTS", "CITIGROUP", "PENZIGO",
              "DARTMOUTH COLLEGE", "NATIONAL INSTITUTE OF TECHNOLOGY KARNATAKA"],
          str([e.employer for e in parsed.entries]))
    check("bullet bodies contribute no roles",
          [len(e.roles) for e in parsed.entries] == [1, 3, 1, 1, 1],
          str([len(e.roles) for e in parsed.entries]))
    # NEGATIVE CONTROL for the glyph exclusion. Every bullet in page() happens
    # not to end in a date, so the assertion above would pass even with the
    # glyph test deleted -- a defence you have only ever seen pass is untested
    # (LEARNINGS 2026-08-19, 2026-08-30). This bullet ends in a bare year, so
    # it WOULD parse as a role line if the glyph were not excluded.
    baited = page(extra=[])
    baited.insert(baited.index("PENZIGO Remote"),
                  GLYPH + " Closed the last regulatory gap before the cutoff in June 2025")
    check("a bullet whose first line ENDS in a date is still not a role",
          [len(e.roles) for e in P.parse_lines(baited).entries] == [1, 3, 1, 1, 1],
          str([len(e.roles) for e in P.parse_lines(baited).entries]))
    check("  ... and without the glyph that same line WOULD be taken as one",
          len(P.parse_lines([ln.lstrip(P.GLYPHS + " ") for ln in baited])
              .entries[1].roles) == 4,
          "the negative control does not bait; the assertion above proves nothing")

    f = P.diff(P.parse_lines(page(fidelity_end=f"June 2026 {DASH} Present")),
               rec, titles)
    check("'Present' on a role the record has ENDED is an ERROR",
          any("claims current employment" in m for m in errs(f)), str(f))

    f = P.diff(P.parse_lines(page(penzigo="2021")), rec, titles)
    check("a bare year against attested months is a WARN, never an ERROR",
          not errs(f) and any("bare year" in m for m in warns(f)), str(f))

    f = P.diff(P.parse_lines(page(edu="December 2026")), rec, titles)
    check("an end-date-only Education line is a WARN (pre-apply the range)",
          not errs(f) and any("start is missing" in m for m in warns(f)), str(f))

    f = P.diff(P.parse_lines(page(fidelity_end=f"June 2025 {DASH} August 2025")),
               rec, titles)
    check("a wrong YEAR is an ERROR", len(errs(f)) == 2, str(f))

    f = P.diff(P.parse_lines(page(fidelity_end=f"July 2026 {DASH} August 2026")),
               rec, titles)
    check("a wrong MONTH inside the right year is an ERROR",
          any("Jul 2026" in m for m in errs(f)), str(f))

    f = P.diff(P.parse_lines(page(citi_emp="")), rec, titles)
    check("a Citi employer line with NO aggregate range produces no finding "
          "(the range is optional)", not f, str(f))

    f = P.diff(P.parse_lines(page(citi_emp=f"Pune, India | July 2021 {DASH} June 2025")),
               rec, titles)
    check("a WRONG employer-line range, once present, is an ERROR",
          any("employer-line start" in m for m in errs(f)), str(f))

    f = P.diff(P.parse_lines(
        page(bank_title="Product Owner, Trade Operations")), rec, titles)
    check("a genericized TEAM half draws no finding", not f, str(f))
    f = P.diff(P.parse_lines(
        page(bank_title="Senior Director, Payments")), rec, titles)
    check("an unsanctioned ROLE token is a WARN",
          any("outside the sanctioned variants" in m for m in warns(f)), str(f))

    f = P.diff(P.parse_lines(page(extra=["ACME CORPORATION Boston, MA",
                                         f"Head of Product January 2020 {DASH} May 2021"])),
               rec, titles)
    check("an employer the record does not hold is an ERROR",
          any("absent from the record" in m for m in errs(f)), str(f))

    # Section bands are renamed and reordered per JD, and some pages put a
    # renamed Leadership band ABOVE Education.
    lines = page()
    i = lines.index("EDUCATION")
    reordered = (lines[:i] + ["AI PRODUCT & LEADERSHIP",
                              f"Networking OS | Founder | networking-os.app | August 2026 {DASH} Present",
                              f"{GLYPH} Shipped a multi-tenant AI product to a live beta"]
                 + lines[i:])
    f = P.diff(P.parse_lines(reordered), rec, titles)
    check("a renamed Leadership band above Education breaks nothing",
          not f, str(f))
    check("project lines are excluded, never graded as roles",
          all("Networking OS" not in (r.title or "")
              for e in P.parse_lines(reordered).entries for r in e.roles))

    f = P.diff(P.parse_lines(page(edu=f"September 2025 {DASH} December 2026 (Expected)")),
               rec, titles)
    check("a trailing '(Expected)' qualifier still parses the date",
          not f, str(f))

    dropped = [ln for ln in page()
               if ln != f"Product Management Intern May 2021 {DASH} July 2021"]
    f = P.diff(P.parse_lines(dropped), rec, titles)
    check("a role the page omits is reported, not silently passed",
          any("role line(s)" in m for m in warns(f)), str(f))
    # NEGATIVE CONTROL for strict_role_count (Phase 5b). The same page graded
    # both ways. A one-pager renders a SELECTION of roles, and preflight calls
    # diff() on every resume, every critic round -- so an ERROR default here
    # would block every tailor. The master resume renders every role the record
    # holds, so there the same shape is a renderer defect. Proving the flag on
    # a page known to trip it is the point: a guard only ever seen pass is a
    # guard nobody has tested (LEARNINGS 2026-09-04).
    strict = P.diff(P.parse_lines(dropped), rec, titles, strict_role_count=True)
    check("strict_role_count promotes that same WARN to an ERROR",
          any("role line(s)" in m for m in errs(strict)), str(strict))
    check("  ... and the default is unchanged, so preflight still only WARNs",
          not any("role line(s)" in m for m in errs(f)), str(errs(f)))

    check("a degree spelled 'B.Tech' in the record matches 'Bachelor of "
          "Technology in ...' on the page",
          P._title_ok("Bachelor of Technology in Electronics and Communication "
                      "Engineering", {"B.Tech, Electronics and Communication "
                                      "Engineering"}, "education"))
    check("a MASTER's claimed against a bachelor record does not match",
          not P._title_ok("Master of Technology in Electronics and Communication "
                          "Engineering", {"B.Tech, Electronics and Communication "
                                          "Engineering"}, "education"))

    # ---------------------------------------------------------------- leg 4
    print("\nround-trip over the real template:")
    template = HERE.parent / "profile" / "template" / \
        "Vaishnav_Ajai_Resume_Template_Format.docx"
    renderer_ok = False
    try:
        renderer_ok = bool(docx_engine.default_renderer()) and template.exists()
    except Exception:
        renderer_ok = False
    if not renderer_ok:
        print("  -- skipped: no renderer or no template on this machine")
    else:
        with tempfile.TemporaryDirectory() as td:
            work = Path(td) / template.name
            shutil.copyfile(template, work)
            pdf, _pages = docx_engine.render_pdf(work, Path(td) / "t.pdf")
            findings = P.diff(P.parse(pdf), rec, titles)
            check("the untouched template has ZERO error-grade diffs",
                  not errs(findings), str(errs(findings)))
            # PINNED AT ZERO. Until 2026-09-01 this pin sat at 3: the template
            # rendered Penzigo as a bare "2021" and Education as "November
            # 2026" alone, both against master-doc instructions to pre-apply
            # the precision (A-14, A-15). Vaish ruled them fixed the same day,
            # so the template now agrees with the record on every date and the
            # honest pin is none at all. This is the assertion that makes the
            # template's dates a TESTED property rather than a remembered one —
            # LEARNINGS 2026-08-17: the template is the defect-propagation
            # point, and a correction recorded only in master-doc fixes nothing.
            w = warns(findings)
            check("the template now agrees with the record on EVERY date",
                  not w, f"{len(w)}: {w}")

            doc = docx_engine.Doc.load(work)
            target = next(p for p in doc.paragraphs()
                          if "Predictive Analytics" in p.text)
            before = target.text
            doc.replace_run_text(target.index, "July 2022", "July 2019")
            mutated = Path(td) / "mutated.docx"
            doc.save(mutated)
            pdf2, _ = docx_engine.render_pdf(mutated, Path(td) / "m.pdf")
            after = P.diff(P.parse(pdf2), rec, titles)
            new_errs = [m for m in errs(after) if m not in errs(findings)]
            check("mutating ONE date yields exactly one new error",
                  len(new_errs) == 1, f"{new_errs} (was {before!r})")
            check("  ... and it names the employer whose date moved",
                  new_errs and "Citigroup" in new_errs[0], str(new_errs))

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
