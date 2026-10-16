"""Table-driven tests for preflight.py — same discipline as the hook self-test:
any preflight rule change reruns this table first. A checker that flags
legitimate content is worse than no checker (it trains ignore-the-output).

Run: python pipeline/test_preflight.py  (no Word, no PDF, no LLM — fakes only)
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import preflight  # noqa: E402

PASS = FAIL = 0


SKIP = 0


def skip(name: str, why: str) -> None:
    """Skip a leg that needs a private file this public copy does not carry."""
    global SKIP
    SKIP += 1
    print(f"skip  {name} ({why})")


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"FAIL  {name}" + (f" — {detail}" if detail else ""))


class FakePara:
    def __init__(self, index, text, bolds=(), bullet=True):
        self.index = index
        self._text = text
        self.is_bullet = bullet
        runs = "".join(
            f'<w:r><w:rPr><w:b/><w:bCs/></w:rPr><w:t>{b}</w:t></w:r>' for b in bolds)
        self.xml = f"<w:p><w:pPr/>{runs}<w:r><w:t>{text}</w:t></w:r></w:p>"

    @property
    def text(self):
        return self._text


class FakeDoc:
    def __init__(self, paras):
        self._paras = paras

    def paragraphs(self):
        return self._paras


def run_bold(paras):
    errors, warns = [], []
    preflight.check_bold(FakeDoc(paras), errors, warns)
    return errors, warns


def run_rhythm(paras, tag=None):
    """check_rhythm emits five independent WARN classes over the same bullets,
    so every fixture would trip several at once. Each message opens with a
    stable tag; filtering on it is what makes a per-check assertion honest
    (an unfiltered `len(warns) == 1` would pass or fail for the wrong reason).
    """
    errors, warns = [], []
    preflight.check_rhythm(FakeDoc(paras), errors, warns)
    if tag is not None:
        warns = [x for x in warns if x.startswith(tag)]
    return errors, warns


# Six bullets that must classify as `other`, used as ratio-test filler. They
# are asserted individually below: if one silently drifted into a real class,
# the ratio fixtures would pass or fail for a reason that has nothing to do
# with the threshold under test. No terminal periods anywhere — resume bullets
# carry none, and a check that anchors on `.` would be testing fiction.
OTHER_BULLETS = [
    "Migrated 140 consumer applications with zero production incidents",
    "Led the API platform roadmap across four partner teams",
    "Defined the intake contract for 450 internal service owners",
    "Shipped a self-serve onboarding flow for enterprise customers",
    "Built the tier model with the data science team",
    "Cut approval latency to a one-week service level agreement",
]

# Five bullets whose FINAL clause starts with a gerund — the `comma-gerund`
# class. One `, <verb>ing` each, so they never trip the clause-stacking check.
COMMA_GERUND_BULLETS = [
    "Drove the migration program, cutting manual oversight 90%",
    "Scoped the anomaly tool, reducing review time 40%",
    "Codified the release checklist, tightening handoffs across teams",
    "Standardized the intake form, removing 12 manual steps",
    "Designed the routing rules, closing the escalation gap",
]


class PosPara:
    """A paragraph carrying the Positioning style — what check_positioning finds."""

    def __init__(self, text, center=True):
        self.index = 0
        self._text = text
        self.is_bullet = False
        jc = '<w:jc w:val="center"/>' if center else ""
        self.xml = (f'<w:p><w:pPr><w:pStyle w:val="Positioning"/>{jc}</w:pPr>'
                    f'<w:r><w:t>{text}</w:t></w:r></w:p>')

    @property
    def text(self):
        return self._text


def run_pos(paras, text=""):
    errors, warns = [], []
    preflight.check_positioning(FakeDoc(paras), text, errors, warns)
    return errors, warns



def paras(texts, bullet=True):
    return [FakePara(i, t, bullet=bullet) for i, t in enumerate(texts)]


def main() -> int:
    print("-- bold-span rules --")
    e, w = run_bold([FakePara(0, "Doubled peak-hour throughput on the roadmap",
                              bolds=["Doubled peak-hour throughput"])])
    check("verb-led bold span is ERROR (a round-2 regression)",
          len(e) == 1 and "starts with a verb" in e[0], str(e))

    e, w = run_bold([FakePara(0, "Improved data quality across programs",
                              bolds=["first bank to full spec completion status"])])
    check("5+-word no-digit span is ERROR", len(e) == 1 and "clause-length" in e[0], str(e))

    e, w = run_bold([FakePara(0, "Scoped a Helpdesk triage bot pilot targeting 15%",
                              bolds=["Helpdesk triage bot pilot", "~15% deflection"])])
    check("4-word named-product span is WARN, not ERROR",
          not e and len(w) == 1 and "4-word" in w[0], f"e={e} w={w}")

    e, w = run_bold([FakePara(0, "Cut cycle time meaningfully this quarter",
                              bolds=["3-4 weeks", "1-week SLA", "15 hrs/week",
                                     "450+ APIs", "10K+ estate"])])
    check("no count cap: five clean spans pass (Vaish, 2026-10-08)", not e and not w,
          f"e={e} w={w}")

    e, w = run_bold([FakePara(0, "Technical: Python, SQL, Machine Learning",
                              bolds=["Technical:"])])
    check("label-line bold is exempt", not e and not w, f"e={e} w={w}")

    e, w = run_bold([FakePara(0, "Migrated 140 consumer applications with zero incidents",
                              bolds=["140 consumer applications", "zero production incidents"])])
    check("clean bullet passes", not e and not w, f"e={e} w={w}")

    print("-- hyphen ATS check --")
    errs, wrns = [], []
    preflight.check_hyphen_ats("uses embedding-based semantic dedup",
                               "uses embeddingbased semantic dedup", errs, wrns)
    check("fused hyphen is ERROR (a round-3 finding)",
          len(errs) == 1 and "embedding-based" in errs[0], str(errs))

    errs, wrns = [], []
    preflight.check_hyphen_ats("uses embedding-based semantic dedup",
                               "uses embedding-based\nsemantic dedup", errs, wrns)
    check("intact hyphen on its own line passes", not errs and not wrns,
          f"e={errs} w={wrns}")

    errs, wrns = [], []
    preflight.check_hyphen_ats("uses embedding-based semantic dedup",
                               "uses embedding-\nbased semantic dedup", errs, wrns,
                               jd_text="working knowledge of embeddings and evals")
    check("split JD-keyword term is ERROR when --jd given",
          len(errs) == 1 and "line break" in errs[0], f"e={errs} w={wrns}")

    errs, wrns = [], []
    preflight.check_hyphen_ats("cross-functional signoffs on 2024-2025 work",
                               "cross-\nfunctional signoffs on 2024-2025 work",
                               errs, wrns)
    check("split non-JD term is WARN; numeric-hyphen tokens skipped",
          not errs and len(wrns) == 1 and "cross-functional" in wrns[0],
          f"e={errs} w={wrns}")

    errs, wrns = [], []
    preflight.check_hyphen_ats("iterated to an adoption-ready threshold",
                               "iterated to an ad option-\nready threshold",
                               errs, wrns, jd_text="driving real adoption and impact")
    check("word mangled across a line break is ERROR when JD stem matches "
          "(a round-6 artifact)",
          len(errs) == 1 and "line break" in errs[0], f"e={errs} w={wrns}")

    # Same-line injected spaces were waived as "pypdf noise" until 2026-08-19.
    # They are a RENDERER property, not an extractor one: Word sprays them
    # through justified text and soffice does not, for the same docx. Now that
    # soffice writes every shipped PDF, a spray is a real defect and grades
    # like a line break — ERROR when a JD keyword is at stake.
    errs, wrns = [], []
    preflight.check_hyphen_ats("cut intake-to-approval to a 1-week SLA",
                               "cut intake -to-approval to a 1 -week SLA",
                               errs, wrns, jd_text="intake everything")
    check("same-line injected spaces are ERROR when the JD stem matches",
          len(errs) == 1 and "injected spaces" in errs[0], f"e={errs} w={wrns}")
    check("...and the non-JD token on the same line is only a WARN",
          len(wrns) == 1 and "1-week" in wrns[0], f"e={errs} w={wrns}")

    errs, wrns = [], []
    preflight.check_hyphen_ats("shipped real-time cross-functional review",
                               "shipped real -time cross -functional review",
                               errs, wrns)
    check("injected spaces with no --jd are WARNs, not silence",
          not errs and len(wrns) == 2, f"e={errs} w={wrns}")

    errs, wrns = [], []
    preflight.check_hyphen_ats("shipped real-time review",
                               "shipped real-time review", errs, wrns, jd_text="real time")
    check("a verbatim extraction still passes clean",
          not errs and not wrns, f"e={errs} w={wrns}")

    errs, wrns = [], []
    preflight.check_hyphen_ats("uses embedding-based semantic dedup",
                               "entirely unrelated text with no trace", errs, wrns)
    check("term absent in any form is WARN (inspect manually)",
          not errs and len(wrns) == 1 and "any form" in wrns[0], f"e={errs} w={wrns}")

    print("-- shell damage from an unquoted heredoc (09-29, 10-02) --")
    for bad, why in (("Cut /bin/bash ongoing inference cost", "$0"),
                     ("Saved 00K+ a year in fees", "$200K"),
                     ("Unlocked .5M in pipeline", "$1.5M"),
                     ("Partnered with Fidelity\"s custody team", "quote")):
        errs = []
        preflight.check_shell_damage(bad, errs)
        check(f"flags {why} damage", len(errs) == 1, str(errs))
    errs = []
    preflight.check_shell_damage(
        "Saved $200K+ a year, $1.5M pipeline, $0 incremental cost, 2,000 users, "
        "Fidelity's team, 100% uptime, 0.5 FTE, v2.0, Q4 2025, 10x faster", errs)
    check("intact figures, apostrophes and decimals pass", not errs, str(errs))

    print("-- text guardrails (hook rules imported, single source of truth) --")
    errs = []
    preflight.check_text_guardrails("Delivered $45+ platform savings — big win", errs)
    check("hook rules fire ($45+ and em-dash)",
          any("$45" in x for x in errs) and any("em-dash" in x for x in errs), str(errs))

    errs = []
    preflight.check_text_guardrails(
        "June 2026 – August 2026 targeting ~15% deflection, $45K+ YoY", errs)
    check("date range en-dash + targeting + $45K+ all pass", not errs, str(errs))

    # The two RULES added 2026-08-22 live in the hook, not here. These two cases
    # prove the addition reaches the docx text through _load_hook_rules — if the
    # hook is the single source of truth, preflight inherits growth for free.
    errs = []
    preflight.check_text_guardrails(
        "Cut escalations 40% as measured by the weekly queue", errs)
    check("inherited rule: 'as measured by' scaffold is ERROR",
          len(errs) == 1 and "as measured by" in errs[0], str(errs))

    errs = []
    preflight.check_text_guardrails(
        "Shipped a pipeline that is not just fast, but reliable", errs)
    check("inherited rule: 'not just X, but Y' is ERROR",
          len(errs) == 1 and "not just fast, but" in errs[0], str(errs))

    errs = []
    preflight.check_text_guardrails(
        "Measured the queue and shipped the fix, not just the ticket", errs)
    check("'not just' without a following 'but' passes", not errs, str(errs))

    # Review round 1: the rule shipped without a leading \b, so it matched
    # inside "was measured by" and hard-blocked a score.md ledger line quoting
    # a phrase the author never typed. The \b is the whole fix.
    errs = []
    preflight.check_text_guardrails(
        "The bottom gap was measured by preflight at round 2", errs)
    check("'was measured by' is NOT the scaffold phrase", not errs, str(errs))

    errs = []
    preflight.check_text_guardrails(
        "Cut escalations 40%, as measured by the weekly queue", errs)
    check("...but 'as measured by' still blocks after the \\b fix",
          len(errs) == 1 and "as measured by" in errs[0], str(errs))

    print("-- date consistency --")
    warns = []
    preflight.check_dates(FakeDoc([
        FakePara(0, "Program Intern June 2026 – August 2026", bullet=False),
        FakePara(1, "Product Owner 2024–2025", bullet=False)]), warns)
    check("mixed date formats is WARN", len(warns) == 1 and "mixed" in warns[0], str(warns))

    warns = []
    preflight.check_dates(FakeDoc([
        FakePara(0, "Product Owner 2024–2025", bullet=False),
        FakePara(1, "Analyst 2023–2024", bullet=False)]), warns)
    check("uniform date formats pass", not warns, str(warns))

    print("-- rhythm: final-clause classification --")
    for text, want in [
        ("Drove the migration program, cutting manual oversight 90%", "comma-gerund"),
        ("Cut review cycles by embedding it in governance", "by-gerund"),
        ("Codified the checklist, which the platform team now owns", "which-that"),
        ("Ran the pilot across three regions, 40% faster than the baseline",
         "bare-metric"),
        ("Migrated 140 consumer applications with zero production incidents",
         "other"),
    ]:
        got = preflight.clause_class(preflight.final_clause(text))
        check(f"{text[:34]!r} classifies {want}", got == want, f"got {got}")

    for t in OTHER_BULLETS:
        got = preflight.clause_class(preflight.final_clause(t))
        check(f"ratio filler {t[:28]!r} is `other`", got == "other", f"got {got}")

    print("-- rhythm: uniform trailing-clause ratio --")
    _, w = run_rhythm(paras(COMMA_GERUND_BULLETS + OTHER_BULLETS[:5]),
                      tag="uniform bullet shape")
    check("5 of 10 in one class (0.5 > 0.4) WARNs",
          len(w) == 1 and "comma-gerund" in w[0], str(w))

    _, w = run_rhythm(paras(COMMA_GERUND_BULLETS[:4] + OTHER_BULLETS),
                      tag="uniform bullet shape")
    check("4 of 10 (0.4, not > 0.4) does not WARN", not w, str(w))

    _, w = run_rhythm(paras(COMMA_GERUND_BULLETS), tag="uniform bullet shape")
    check("under 6 bullets never fires, even at ratio 1.0", not w, str(w))

    _, w = run_rhythm(paras(COMMA_GERUND_BULLETS + OTHER_BULLETS[:1]),
                      tag="uniform bullet shape")
    check("exactly 6 bullets at 5/6 does fire (the min-bullets boundary)",
          len(w) == 1, str(w))

    print("-- rhythm: label lines are excluded from every check --")
    label = ("Technical: Python, building dashboards, shipping models, "
             "communication management alignment initiatives")
    _, w = run_rhythm(paras([label] * 3))
    check("label lines trip no rhythm check at all", not w, str(w))

    print("-- rhythm: repeated opening verb --")
    _, w = run_rhythm(paras([
        "Led the API platform roadmap across four partner teams",
        "Led the migration of 140 consumer applications",
        "Led the intake redesign for 450 service owners"]), tag="opening verb")
    check("three bullets opening 'Led' WARN",
          len(w) == 1 and "'led'" in w[0], str(w))

    _, w = run_rhythm(paras([
        "Led the API platform roadmap across four partner teams",
        "Led the migration of 140 consumer applications"]), tag="opening verb")
    check("two bullets opening 'Led' do not WARN", not w, str(w))

    print("-- rhythm: bullet length --")
    # Boundary re-pinned at the calibrated cap (32 -> 42, 2026-08-22). The cap is
    # fitted to "renders to a third justified line" in the shipped PDFs, not
    # chosen for roundness, so these two cases are what stop it drifting back.
    _, w = run_rhythm(paras(["Led " + " ".join(["scope"] * 42)]), tag="bullet length")
    check("43-word bullet WARNs", len(w) == 1 and "43 words" in w[0], str(w))

    _, w = run_rhythm(paras(["Led " + " ".join(["scope"] * 41)]), tag="bullet length")
    check("42-word bullet does not WARN", not w, str(w))

    # The old cap. A 33-word bullet is the shipped median-ish length and must not
    # warn any more; this is the regression guard for the retune itself.
    _, w = run_rhythm(paras(["Led " + " ".join(["scope"] * 32)]), tag="bullet length")
    check("a 33-word bullet (near the shipped median) no longer WARNs",
          not w, str(w))

    print("-- rhythm: gerund clause-stacking --")
    _, w = run_rhythm(paras([
        "Drove the rollout, turning manual triage into a queue, "
        "cutting review time 40%"]), tag="gerund clause-stacking")
    check("two ', <verb>ing' clauses in one bullet WARN", len(w) == 1, str(w))

    _, w = run_rhythm(paras([
        "Drove the rollout, turning manual triage into a reviewed queue"]),
        tag="gerund clause-stacking")
    check("one ', <verb>ing' clause does not WARN", not w, str(w))

    # Review round 1: capitalized org-list nouns were 42% of this check's WARNs
    # across the shipped corpus. Team names are not clause chains. The `[a-z]`
    # lead separates them, and only works because the pattern is not (?i).
    #
    # This fixture is the REAL corpus shape, taken from the Citi bullet that
    # produced the false WARN: ONE genuine lowercase gerund plus ONE capitalized
    # team name in a list, which together cleared the threshold of 2. A fixture
    # with only capitalized nouns proves nothing — it never reached 2 matches
    # under the old regex either, so it passed before and after the fix.
    _, w = run_rhythm(paras([
        "Led cross-functional signoffs across Market Ops, DevOps, Engineering, "
        "and Compliance, achieving full regulatory coverage"]),
        tag="gerund clause-stacking")
    check("one real gerund beside an org list does not WARN "
          "(the 42% false-positive class)", not w, str(w))

    _, w = run_rhythm(paras([
        "Drove the rollout with Product, Engineering, and Compliance, "
        "cutting review time 40%, boosting throughput 2x"]),
        tag="gerund clause-stacking")
    check("a real chain still WARNs even beside an org list", len(w) == 1, str(w))

    print("-- rhythm: the deleted abstract-noun check stays deleted --")
    # The provisional stacked-abstract-noun check and its four cases were cut
    # 2026-08-22 (never fired at 3 on the shipped corpus; would have produced 3
    # false positives at 2). This case is what stops it coming back by accident:
    # its former three-noun WARN fixture must now be silent.
    _, w = run_rhythm(paras([
        "Drove communication management alignment initiatives across teams"]))
    check("three consecutive abstract nouns no longer WARN", not w, str(w))
    check("abstract_noun_run and its constant are gone from the module",
          not hasattr(preflight, "abstract_noun_run")
          and not hasattr(preflight, "ABSTRACT_SUFFIX")
          and not hasattr(preflight, "RHYTHM_MAX_ABSTRACT_RUN"))

    print("-- rhythm: WARN-only in this task --")
    # The length fixture is 43 words, past the calibrated cap, so this bundle
    # still trips every surviving check. The assertion is worthless if nothing
    # fires, so `w` is asserted non-empty alongside `e` being empty.
    e, w = run_rhythm(paras(COMMA_GERUND_BULLETS + OTHER_BULLETS[:5] + [
        "Led " + " ".join(["scope"] * 42),
        "Led communication management alignment initiatives, turning triage "
        "into a queue, cutting review time 40%",
        "Led the intake redesign for 450 service owners"]))
    check("no rhythm finding is ever graded ERROR", not e, str(e))
    check("...and that is not because nothing fired", len(w) >= 3, str(w))

    print("-- tailored-page conventions (applications/ only, 2026-10-08) --")

    class TPara(FakePara):
        def __init__(self, index, text, bolds=(), bullet=False, style=""):
            super().__init__(index, text, bolds, bullet)
            if style:
                self.xml = self.xml.replace("<w:pPr/>",
                                            f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>')

    def page(**over):
        rows = [TPara(0, "WORK EXPERIENCE"),
                TPara(1, "Product Management Intern, API Platform"),
                TPara(2, "Moved 140 applications off a legacy API", bullet=True),
                TPara(3, "EDUCATION"),
                TPara(4, ": Product Design", bolds=["Courses"]),
                TPara(5, "PROJECTS"),
                TPara(6, " | Founder", bolds=["Networking OS"]),
                TPara(7, "Shipped a multi-tenant product", bullet=True),
                TPara(8, "SKILLS & INTERESTS"),
                TPara(9, " SQL, Python", bolds=["Technical:"], bullet=True)]
        for i, para in over.items():
            rows[int(i[1:])] = para
        # FakePara text is the plain run only; give label rows their full text
        for r in rows:
            r._text = "".join(__import__("re").findall(r"<w:t>(.*?)</w:t>", r.xml))
        return rows

    def run_tailored(rows, claims=()):
        errors = []
        preflight.check_tailored_page(FakeDoc(rows), errors, list(claims))
        return errors

    check("a clean tailored page passes", run_tailored(page()) == [], str(run_tailored(page())))
    e = run_tailored([TPara(99, "Product Manager with 3+ years", style="Positioning")] + page())
    check("a positioning line is ERROR", any("positioning line" in x for x in e), str(e))
    rows = page()
    rows[0], rows[3] = TPara(0, "EDUCATION"), TPara(3, "WORK EXPERIENCE")
    e = run_tailored(rows)
    check("a moved section is ERROR", any("section order" in x for x in e), str(e))
    e = run_tailored(page(r9=TPara(9, "Technical: SQL, Python", bullet=True)))
    check("a plain skills label is ERROR", any("label not bold" in x for x in e), str(e))
    e = run_tailored(page(r4=TPara(4, "Certification: Certified Scrum Product Owner (CSPO), Scrum Alliance")))
    check("a CSPO line under EDUCATION is ERROR", any("under EDUCATION" in x for x in e), str(e))
    e = run_tailored(page(r9=TPara(9, " SQL, Agile (CSPO Certified)", bolds=["Technical:"], bullet=True)))
    check("the CSPO on a skills line passes", e == [], str(e))
    errs = []
    preflight.check_text_guardrails("Helped migrate 140 applications", errs)
    check("a hedge verb on a resume is still ERROR", any("hedge verb" in x for x in errs), str(errs))
    U = preflight.CSPO_BADGE_URL
    linked = ('<w:p><w:r><w:t>Agile (</w:t></w:r><w:hyperlink r:id="rId9" w:history="1">'
              '<w:r><w:t>CSPO®</w:t></w:r></w:hyperlink><w:r><w:t> Certified)</w:t></w:r></w:p>')
    def run_link(xmls, targets):
        errors = []
        preflight.check_cspo_link(xmls, targets, errors)
        return errors
    check("a CSPO linked to its badge passes", run_link([linked], {"rId9": U}) == [])
    check("an unlinked CSPO is ERROR",
          any("not linked" in x for x in run_link(["<w:p><w:r><w:t>Agile (CSPO Certified)</w:t></w:r></w:p>"], {})))
    check("a CSPO linked elsewhere is ERROR",
          any("not linked" in x for x in run_link([linked], {"rId9": "https://example.com"})))
    check("a page with no CSPO at all is ERROR", any("not linked" in x for x in run_link([], {U: U})))
    e = run_tailored(page(r6=TPara(6, "Networking OS | Founder")))
    check("a plain project name is ERROR", any("project name not bold" in x for x in e), str(e))
    e = run_tailored(page(r1=TPara(1, "Operations Associate II	Oct 2024")))
    check("an official title on a PM page is ERROR", any("retired title" in x for x in e), str(e))
    def run_fde(rows):
        errors = []
        preflight.check_tailored_page(FakeDoc(rows), errors, [], fde=True)
        return errors

    # Titles below come from the invented pipeline/testdata/record.example.json.
    e = run_fde(page(r1=TPara(1, "Operations Associate II	Oct 2024")))
    check("an official title on an FDE page passes", e == [], str(e))
    e = run_fde(page(r1=TPara(1, "Operations Associate I, Payments	Aug 2023")))
    check("an official title with a team on an FDE page passes", e == [], str(e))
    e = run_fde(page(r1=TPara(1, "Product Owner, Payments	Oct 2024")))
    check("a functional title on an FDE page is ERROR",
          any("functional title on an FDE page" in x for x in e), str(e))
    e = run_fde(page(r1=TPara(1, "Product Analyst	Aug 2023 - Sep 2024")))
    check("a functional analyst title on an FDE page is ERROR",
          any("functional title on an FDE page" in x for x in e), str(e))
    e = run_fde(page(r1=TPara(1, "Product Management Intern, API Platform	June 2026")))
    check("an intern title that is already official passes on an FDE page", e == [], str(e))
    from docx_engine import Doc
    tpl_path = HERE.parent / "profile" / "template" / "Vaishnav_Ajai_Resume_Template_Format.docx"
    if not tpl_path.exists():
        print("  skip  role-weight checks (need the template docx at profile/template/, "
              "which is not published)")
    else:
        tpl = Doc.load(tpl_path)
        counts = [n for _, _, n in preflight.role_bullets(tpl)]
        check("role_bullets reads the template's slots", counts == [3, 2, 2, 2, 1], str(counts))
        w = []
        preflight.check_weight(tpl, w)
        check("the template's 3 Fidelity bullets over 2-per-Citi-role WARN", len(w) == 1, str(w))
        tpl.delete_paragraph(tpl.find("Drove a firm-mandated").index)
        tpl.delete_employer("PENZIGO")
        w = []
        preflight.check_weight(tpl, w)
        check("Fidelity at 2 bullets, Penzigo gone: no WARN", w == [], str(w))
    e = run_fde(page(r1=TPara(1, "Technical Program Manager	May 2021")))
    check("a TPM title stays retired on an FDE page", any("retired title" in x for x in e), str(e))
    check("FDE engineer titles are FDE", all(preflight.is_fde_title(t) for t in (
        "Forward Deployed Engineer, AWS Forward Deployed Engineering",
        "Senior Analyst-AI Forward Deployed Engineer", "Forward-Deployed Engineer (FDE)")))
    check("FD PM and strategist titles are not", not any(preflight.is_fde_title(t) for t in (
        "Associate Forward Deployed Product Manager", "Forward Deployed AI Strategist",
        "Product Manager, FDE Platform")))
    e = run_tailored(page(r2=TPara(2, "Worked with a program manager on 140 applications",
                                   bullet=True)))
    check("...but a bullet naming a program manager is not", e == [], str(e))
    # An invented retired claim (a renamed metric), as in testdata/record.example.json.
    claim = {"pattern": "first-contact resolution", "use": "self-serve resolution rate"}
    e = run_tailored(page(r2=TPara(2, "raised first-contact resolution in the pilot",
                                   bullet=True)), [claim])
    check("a retired claim is ERROR and names the replacement",
          any("retired claim" in x and "self-serve resolution rate" in x for x in e), str(e))
    example = HERE / "testdata" / "record.example.json"
    check("retired_claims() reads pattern/use pairs from a record",
          preflight.retired_claims(example) == [claim], str(preflight.retired_claims(example)))
    check("retired_claims() is empty when the record is absent",
          preflight.retired_claims(HERE / "no-such-record.json") == [])
    check("only pages under applications/ get these checks",
          preflight.is_tailored_page(Path("applications/x/r.docx"))
          and not preflight.is_tailored_page(Path("profile/network/general/r.docx")))

    print("-- positioning paragraph (mechanical shape only) --")
    e, w = run_pos([])
    check("a missing Positioning paragraph WARNs and never ERRORs",
          not e and len(w) == 1 and "no paragraph carries the Positioning style" in w[0],
          f"{e} / {w}")

    e, w = run_pos([PosPara("Product Owner with 3+ years in regulated systems"),
                    PosPara("Product Manager with 3+ years in regulated systems")])
    check("two Positioning paragraphs is ERROR (the page has one slot)",
          len(e) == 1 and "exactly one may" in e[0], f"{e} / {w}")

    e, w = run_pos([PosPara("")])
    check("an empty Positioning paragraph is ERROR", len(e) == 1 and "EMPTY" in e[0], str(e))

    e, w = run_pos([PosPara("word " * (preflight.POS_MAX_WORDS + 1))])
    check("a line over POS_MAX_WORDS WARNs and never ERRORs",
          not e and len(w) == 1 and f"{preflight.POS_MAX_WORDS + 1} words" in w[0],
          f"{e} / {w}")

    e, w = run_pos([PosPara("word " * preflight.POS_MAX_WORDS)])
    check("...and a line exactly at the cap says nothing", not e and not w, f"{e} / {w}")

    print("-- font families: a substituted font sets line heights --")
    # The 2026-09-04 defect at rest. numbering.xml named the bullet glyph's font
    # as Symbol; Symbol MT is 1.2251 em against Times' 1.1499, and a line's
    # height is the max over its runs, so every bullet's FIRST line set 0.75pt
    # taller on a seat that HAD Symbol MT (Word, Windows LO) than on one that
    # did not (the Linux cloud). Over ~15 bullets that is a body line, which is
    # why 40 resumes counted one page in the cloud and two in Word.
    check("a sanctioned family is accepted under its PostScript name too",
          preflight._sanctioned("TimesNewRomanPS-BoldItalicMT")
          and preflight._sanctioned("Times New Roman")
          and preflight._sanctioned("LiberationSerif-Bold"))
    check("and the two substitution faces are still rejected",
          not preflight._sanctioned("SymbolMT")
          and not preflight._sanctioned("OpenSymbol")
          and not preflight._sanctioned("Wingdings"))

    class _P:
        def __init__(self, parts): self._parts = parts
    clean = _P({"word/numbering.xml":
                b'<w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman"/>'})
    e, w = [], []
    preflight.check_font_families(clean, None, e, w)
    check("a single-family docx passes", e == [] and w == [], f"{e} {w}")

    dirty = _P({"word/numbering.xml":
                b'<w:rFonts w:ascii="Symbol" w:hAnsi="Symbol" w:hint="default"/>'})
    e, w = [], []
    preflight.check_font_families(dirty, None, e, w)
    check("a docx naming Symbol errors", any("Symbol" in x for x in e), str(e))

    # The regex reads <w:rFonts> ONLY. The same attribute names sit on <w:lang>,
    # where w:eastAsia is a LOCALE -- read loose, the check reported 'en-US' as a
    # missing font, which is a false alarm that would have taught the next
    # session to ignore it.
    lang = _P({"word/document.xml":
               b'<w:lang w:val="en-US" w:eastAsia="en-US" w:bidi="ar-SA"/>'
               b'<w:rFonts w:ascii="Times New Roman"/>'})
    e, w = [], []
    preflight.check_font_families(lang, None, e, w)
    check("a <w:lang> locale is not read as a font family", e == [], str(e))

    print("-- page fill: measured against the docx's own bottom margin --")
    # Pure arithmetic, so the bands are testable without rendering anything.
    # 12.0pt lines, a 6.0pt bottom margin (the template as of 2026-09-03).
    ys = [100.0, 200.0, 300.0]
    check("no surplus when the lowest baseline sits on the margin",
          abs(preflight.page_fill_surplus([6.0, 50.0], 12.0, 6.0)) < 1e-9)
    check("surplus is (lowest baseline - bottom margin) / line height",
          abs(preflight.page_fill_surplus(ys, 12.0, 6.0) - (100.0 - 6.0) / 12.0) < 1e-9,
          str(preflight.page_fill_surplus(ys, 12.0, 6.0)))
    bands = [(preflight.page_fill_surplus([6.0 + 0.9 * 12.0], 12.0, 6.0), "ok"),
             (preflight.page_fill_surplus([6.0 + 1.5 * 12.0], 12.0, 6.0), "warn"),
             (preflight.page_fill_surplus([6.0 + 2.5 * 12.0], 12.0, 6.0), "error")]
    check("the three bands land where the doctrine puts them (ok <1, warn >=1, error >=2)",
          bands[0][0] < 1.0 and 1.0 <= bands[1][0] < 2.0 and bands[2][0] >= 2.0,
          str([b[0] for b in bands]))
    # A margin cut moves the reference, which is the whole point of the change:
    # the same render measures MORE free room once the page ends lower.
    check("cutting the bottom margin surfaces the room it freed",
          preflight.page_fill_surplus(ys, 12.0, 6.0)
          > preflight.page_fill_surplus(ys, 12.0, 12.2))

    e, w = [], []
    preflight.check_page_fill(Path("nonexistent.pdf"), None, e, w)
    check("a docx with no <w:pgMar> warns rather than measuring against a default",
          not e and len(w) == 1 and "no <w:pgMar>" in w[0], f"{e} / {w}")

    from docx_engine import Doc  # noqa: E402
    template = HERE.parent / "profile" / "template" / "Vaishnav_Ajai_Resume_Template_Format.docx"
    if template.exists():
        margins = Doc.load(template).page_margins()
        check("page_margins reads all four sides in points",
              set(margins) == {"top", "right", "bottom", "left"}, str(margins))
        # 244 twips = 12.2pt (the historical template), 120 twips = 6.0pt (the
        # 2026-09-03 cut). Both are accepted because the template edit lands in
        # a parallel change; test_docx_fidelity.py is where 6.0 is REQUIRED.
        check("the template's bottom margin is the historical 12.2pt or the cut 6.0pt",
              margins.get("bottom") in (12.2, 6.0), str(margins))
        check("the top margin is unchanged at 11.9pt (238 twips), the real margin the "
              "old top-gap reference was 1.3 lines adrift from",
              margins.get("top") == 11.9, str(margins))
    else:
        skip("page-margin reader",
             "needs the template docx at profile/template/, which is not published")

    print("-- plain English: voice.md diagnostic 5 (Say it aloud) --")
    w = []
    preflight.check_plain_english(
        "Cut intake-to-approval time and shortened time-to-process for Operations", w)
    check("a shorthand compound WARNs, once per distinct term",
          len(w) == 2 and any("intake-to-approval" in x for x in w)
          and any("time-to-process" in x for x in w), str(w))
    w = []
    preflight.check_plain_english(
        "Cut intake-to-approval here and intake-to-approval again, plus "
        "Intake-to-approval capitalized", w)
    check("...and a repeated term is one finding, not three", len(w) == 1, str(w))
    w = []
    preflight.check_plain_english(
        "Owned the end-to-end rollout End-to-End, built AI products 0-to-1 on a "
        "3-to-4-week cycle", w)
    check("end-to-end is allowlisted and numeric spans (0-to-1, 3-to-4-week) are not shorthand",
          not w, str(w))
    check("check_plain_english takes only a warns list — it can never block a tailor",
          "errors" not in preflight.check_plain_english.__code__.co_varnames)

    # --- check_parse_back: the ADAPTER, not the rules (test_parse_back owns
    # those). What matters here is which failures block the critic loop and
    # which merely annotate it. A record that cannot be READ is loud, because
    # an unreadable record means the check is silently not running; any other
    # exception degrades to a warn, because a parse-back defect must never take
    # the tailor loop down with it -- the same contract ats_score.resume_texts
    # holds for a locked or corrupt PDF.
    print("")
    print("check_parse_back adapter:")
    import parse_back

    class _Boom:
        def __init__(self, exc):
            self.exc = exc

        def __call__(self, *a, **k):
            raise self.exc

    real = parse_back.check
    try:
        parse_back.check = _Boom(parse_back.AnchorError("Citigroup heading moved"))
        e, w = [], []
        preflight.check_parse_back(Path("x.pdf"), e, w)
        check("an unreadable record is an ERROR, not a silent skip",
              len(e) == 1 and "could not read the record" in e[0], str(e))

        parse_back.check = _Boom(ValueError("pdf is a potato"))
        e, w = [], []
        preflight.check_parse_back(Path("x.pdf"), e, w)
        check("any other failure degrades to a warn, never blocking the loop",
              not e and len(w) == 1 and "skipped" in w[0], str(e) + str(w))

        parse_back.check = lambda *a, **k: [("error", "bad date"), ("warn", "coarse")]
        e, w = [], []
        preflight.check_parse_back(Path("x.pdf"), e, w)
        check("grades are carried through and labelled",
              len(e) == 1 and e[0].startswith("parse-back") and "bad date" in e[0]
              and len(w) == 1 and "coarse" in w[0], str(e) + str(w))
        # The import itself. The monkeypatches above all run AFTER parse_back
        # is already in sys.modules, so none of them exercises the import line
        # -- and that line is the one whose failure would escape main() and
        # crash preflight before every critic round. sys.modules[name] = None
        # makes `import name` raise ImportError, which is the real shape.
        import sys as _sys
        saved = _sys.modules.pop("parse_back")
        _sys.modules["parse_back"] = None
        try:
            e, w = [], []
            preflight.check_parse_back(Path("x.pdf"), e, w)
            check("an unimportable parse_back warns, it does not crash preflight",
                  not e and len(w) == 1 and "skipped" in w[0], str(e) + str(w))
        except Exception as exc:                            # noqa: BLE001
            check("an unimportable parse_back warns, it does not crash preflight",
                  False, f"raised {exc!r}")
        finally:
            _sys.modules["parse_back"] = saved
    finally:
        parse_back.check = real

    # -- W9 reader terms (2026-10-16) -----------------------------------------
    # The Meta page that drew a screen carried 1 bank term its JD never used;
    # pipeline pages carried ~10. Warn on those, stay silent when the JD uses the
    # term or shares its domain, and keep the Skills lines apart from prose.
    print("-- reader terms --")
    page = ("WORK EXPERIENCE\nCut CSDR penalties and moved WebLogic services to Kafka.\n"
            "SKILLS & INTERESTS\nTechnical: Python, Power Automate\n")

    def reader(jd):
        w = []
        preflight.check_reader_terms(page, jd, w)
        return w

    w = reader("We build consumer apps for creators. You will write PRDs.")
    check("a bank term the JD never uses warns, with a gloss",
          any("'CSDR'" in x and "settle late" in x for x in w), str(w))
    check("each prose term warns once (CSDR, WebLogic, Kafka)",
          sum(x.startswith("reader term ") for x in w) == 3, str(w))
    check("a Skills-line term is reported apart, as findability",
          any(x.startswith("reader terms on the Skills lines") and "Power Automate" in x
              for x in w), str(w))
    w = reader("Own our Kafka streaming platform at a global bank.")
    check("silent when the JD uses the term or shares its domain",
          not any("'CSDR'" in x or "'Kafka'" in x for x in w), str(w))
    check("a term outside the JD's domains still warns",
          any("'WebLogic'" in x for x in w), str(w))
    check("no JD, no warning", reader("") == [], str(reader("")))
    check("REMIT the acronym is not 'remit' the word",
          not preflight.check_reader_terms("REMIT reporting", "within your remit", w := [])
          and any("'REMIT'" in x for x in w), str(w))

    # -- CLI end to end on a real rendered pair --------------------------------
    # The 2026-09-16 page-count check shipped with a NameError that no fake-based
    # leg could see; every local tailor died at the first line of main(). This
    # leg runs the real entry point on the newest application folder that holds a
    # docx + pdf pair and only asks that it finishes with its summary line.
    print("-- CLI on a real docx + pdf --")
    import subprocess
    apps = HERE.parent / "applications"
    pairs = []
    if apps.exists():
        for folder in apps.iterdir():
            for docx in folder.glob("*.docx"):
                pdf = docx.with_suffix(".pdf")
                if pdf.exists():
                    pairs.append((pdf.stat().st_mtime, docx, pdf))
    if pairs:
        _, docx, pdf = max(pairs)
        jd = docx.parent / "jd.md"
        jd_args = ["--jd", str(jd)] if jd.exists() else []
        proc = subprocess.run([sys.executable, str(HERE / "preflight.py"), str(docx), str(pdf),
                               *jd_args],
                              capture_output=True, text=True, encoding="utf-8", errors="replace")
        out = (proc.stdout or "") + (proc.stderr or "")
        check("preflight CLI reaches its summary line (no traceback)",
              "preflight:" in out and "Traceback" not in out, out[-400:])
    else:
        print("  --  no rendered application pair on disk; CLI leg skipped")

    print(f"\n{PASS} passed, {FAIL} failed, {SKIP} skipped")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
