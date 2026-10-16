"""Table-driven tests for requirements.py - the requirement screen.

Extraction cases are the JD shapes found across the 714 jd.md files under
applications/ (2026-10-14): markdown and bold headings, plain-text headings,
inline "preferred" / "is a plus", Capital One's OR lines and "+" sub-bullets,
HTML postings, Google's heading glued to the line before it, and the sections
that must stay out (responsibilities, benefits, physical requirements, a stray
heading after a filled block). Grading cases pin the HiredScore tiers, the
arithmetic, and the two guards against inflation: a quote that is not on the
page earns nothing, and Skills-line-only evidence earns at most partial.

Run: python pipeline/test_requirements.py   (no Word, no LLM, no network)
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import requirements as R  # noqa: E402

SCRIPT = HERE / "requirements.py"
TEMPLATE = R.ROOT / "profile" / "template" / "Vaishnav_Ajai_Resume_Template_Format.docx"

PASS = FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"FAIL  {name}" + (f" - {detail}" if detail else ""))


def texts(items, kind=None):
    return [i["text"] for i in items if kind is None or i["kind"] == kind]


# --- extraction -----------------------------------------------------------------

def test_extract():
    print("extract")
    md = """# Acme - Product Manager

## About the role
You will own the roadmap.

## Responsibilities
- Own the roadmap for payments
- Run discovery with customers

## Qualifications
- 3+ years of product management experience
- Experience with SQL and A/B testing
- Experience with payments is a plus

## Preferred Qualifications
- MBA
- Fintech background

## Benefits
- Health insurance and 401(k)
"""
    it = R.extract(md)
    check("bullets under Qualifications are basic",
          texts(it, "basic") == ["3+ years of product management experience",
                                 "Experience with SQL and A/B testing"], str(texts(it)))
    check("inline 'is a plus' and the Preferred heading are preferred",
          texts(it, "preferred") == ["Experience with payments is a plus", "MBA", "Fintech background"],
          str(texts(it, "preferred")))
    check("responsibilities and benefits stay out",
          not any("roadmap" in t or "Health" in t for t in texts(it)))
    check("ids run R1..Rn in JD order", [i["id"] for i in it] == ["R1", "R2", "R3", "R4", "R5"])
    check("years flag read from the item", it[0].get("years") == 3)

    # Context FDE (2026-10-13): a bare all-caps "WHAT WE REQUIRE" was not a
    # heading, so only the NICE TO HAVE block came through and the page was
    # graded on preferred items alone.
    caps = """WHAT YOU'LL DO

 - Deploy agents at customer sites

WHAT WE REQUIRE

 - 2+ years of relevant, post-college work experience in software engineering

 - Strong coding skills in Python or TypeScript

NICE TO HAVE

 - Experience with AI/ML systems, LLMs, or agent frameworks
"""
    it = R.extract(caps)
    check("'WHAT WE REQUIRE' is a basic block",
          texts(it, "basic") == ["2+ years of relevant, post-college work experience in software engineering",
                                 "Strong coding skills in Python or TypeScript"], str(texts(it)))
    check("...and NICE TO HAVE stays preferred",
          texts(it, "preferred") == ["Experience with AI/ML systems, LLMs, or agent frameworks"],
          str(texts(it, "preferred")))

    # A preference inside a required item narrows it; it does not make it
    # optional. Travel is a screening question, not a resume line.
    qual = """## Requirements
- 3+ years of product management experience, preferably in payments
- Bachelor's degree (preferably in Computer Science)
- Experience with Kafka is a plus
- Ability and interest to travel up to 25-50% as needed to customer sites
- Exposure to the travel & hospitality industry
"""
    it = R.extract(qual)
    check("'..., preferably in payments' stays basic",
          "3+ years of product management experience, preferably in payments" in texts(it, "basic"),
          str(texts(it)))
    check("'(preferably in ...)' stays basic",
          "Bachelor's degree (preferably in Computer Science)" in texts(it, "basic"), str(texts(it)))
    check("'is a plus' is still preferred",
          texts(it, "preferred") == ["Experience with Kafka is a plus"], str(texts(it, "preferred")))
    check("travel is a knockout, out of the grade",
          [i["text"] for i in it if i["kind"] == "knockout"]
          == ["Ability and interest to travel up to 25-50% as needed to customer sites"],
          str([(i["kind"], i["text"]) for i in it]))
    check("a travel-industry domain is still a requirement",
          "Exposure to the travel & hospitality industry" in texts(it, "basic"), str(texts(it)))

    plain = """Responsibilities
Gather requirements and coordinate stakeholders

Skills
Bachelor's degree in Business or a related field
2+ years of experience in digital banking products
Experience with SQL

Benefits
Competitive compensation
"""
    it = R.extract(plain)
    check("plain-text 'Skills' heading with one item per line",
          texts(it) == ["Bachelor's degree in Business or a related field",
                        "2+ years of experience in digital banking products", "Experience with SQL"],
          str(texts(it)))
    check("degree flag", it[0].get("degree") is True)
    check("a requirement line that starts with 'Experience' is not a heading",
          R.head_kind("Experience with SQL") is None)

    capone = """**Basic Qualifications:**

* Currently has, or is in the process of obtaining one of the following:

	+ A Bachelor's or Master's Degree in a quantitative field

 **OR**

* Bachelor's degree in any field and at least 3 years of Product Management experience

 **Preferred Qualifications:**

* Experience translating business strategy into consumer facing digital products

*At this time, Capital One will not sponsor a new applicant for employment authorization*
"""
    it = R.extract(capone)
    check("Capital One: sub-bullet and OR alternative make one basic item",
          len(texts(it, "basic")) == 1 and " OR " in texts(it, "basic")[0]
          and "quantitative field" in texts(it, "basic")[0], str(texts(it)))
    check("Capital One: closing italic sponsorship line is not an item",
          texts(it, "preferred") == ["Experience translating business strategy into consumer facing digital products"],
          str(texts(it, "preferred")))

    html = ("<h2><strong>Minimum qualifications</strong></h2><ul><li>2 years of experience in "
            "product management</li><li>Experience with data analysis</li></ul>"
            "<h2><strong>Preferred qualifications</strong></h2><ul><li>Experience with APIs</li>"
            "<li>Experience in healthcare</li></ul><h2>Benefits</h2><p>Great pay</p>")
    it = R.extract(html)
    check("HTML posting is flattened and read",
          texts(it, "basic") == ["2 years of experience in product management", "Experience with data analysis"]
          and texts(it, "preferred") == ["Experience with APIs", "Experience in healthcare"], str(texts(it)))

    google = ("Note: share your preferred working location: **New York, NY**.### **Minimum qualifications:**\n\n"
              "* Bachelor's degree or equivalent practical experience.\n"
              "* 5 years of experience in product management.\n\n---\n\n"
              "### **Preferred qualifications:**\n\n* Master's degree in a technical field.\n"
              "* Experience with machine learning products.\n")
    it = R.extract(google)
    check("Google: heading glued to the previous line, and a --- after the block",
          len(texts(it, "basic")) == 2 and len(texts(it, "preferred")) == 2, str(texts(it)))

    sub = """**Qualifications**

**Required**

* 2-4 years of experience in product ownership
* Exposure to Agile/Scrum methodologies

**Preferred**

* Experience with Power BI dashboards

**Interviewing with Acme**

* We run a take-home exercise
"""
    it = R.extract(sub)
    check("sub-headings under Qualifications are read; Required basic, Preferred preferred",
          texts(it, "basic") == ["2-4 years of experience in product ownership",
                                 "Exposure to Agile/Scrum methodologies"]
          and texts(it, "preferred") == ["Experience with Power BI dashboards"], str(texts(it)))

    phys = """## Requirements
- Experience leading cross-functional teams
- Strong communication skills

## Physical Requirements
- Must be able to sit for long periods
"""
    it = R.extract(phys)
    check("Physical Requirements stay out", texts(it) == ["Experience leading cross-functional teams",
                                                          "Strong communication skills"], str(texts(it)))

    check("no headings at all gives an empty list (the screener derives it)",
          R.extract("We are hiring a PM. You will build things and talk to users.") == [])

    msft = """**Required Qualifications:**

* Bachelor's Degree AND 2+ years experience in product management
* OR equivalent experience.

* Ability to meet Microsoft, customer and/or government security screening requirements are required for this role.
* Experience with SQL
"""
    it = R.extract(msft)
    check("Microsoft: an 'OR ...' line joins the item above it",
          texts(it, "basic") == ["Bachelor's Degree AND 2+ years experience in product management"
                                 " OR equivalent experience.", "Experience with SQL"], str(texts(it)))
    check("a security-screening item is a knockout, not a basic requirement",
          texts(it, "knockout") == ["Ability to meet Microsoft, customer and/or government security"
                                    " screening requirements are required for this role."], str(it))

    inline = "Requirements: 3+ years of product management in B2B SaaS\nBenefits: health"
    it = R.extract(inline)
    check("'Requirements: ...' on one line is a heading plus its item",
          texts(it) == ["3+ years of product management in B2B SaaS"], str(texts(it)))


# --- grading --------------------------------------------------------------------

def g(kind, verdict):
    return {"kind": kind, "verdict": verdict}


def test_grade():
    print("grade")
    B, P = R.BASIC, R.PREFERRED
    check("A: all basic met and half the preferred",
          R.grade_letter([g(B, "met"), g(B, "met"), g(P, "met"), g(P, "not_met")]) == "A")
    check("B: all basic met, preferred under half",
          R.grade_letter([g(B, "met"), g(P, "not_met"), g(P, "not_met"), g(P, "met")]) == "B")
    check("partial is not met for the tier",
          R.grade_letter([g(B, "met"), g(B, "partial")]) == "C")
    check("C: at least half the basic", R.grade_letter([g(B, "met"), g(B, "not_met")]) == "C")
    check("D: under half the basic",
          R.grade_letter([g(B, "met"), g(B, "not_met"), g(B, "not_met")]) == "D")
    check("no basic items: graded on the preferred list",
          R.grade_letter([g(P, "met"), g(P, "met")]) == "A"
          and R.grade_letter([g(P, "not_met"), g(P, "not_met")]) == "D")
    check("A with no preferred items", R.grade_letter([g(B, "met")]) == "A")
    # basic 2x: (2*1 + 2*0.5 + 1*0) / (2+2+1) = 3/5
    check("score: basic weighs double, partial earns half",
          R.screen_score([g(B, "met"), g(B, "partial"), g(P, "not_met")]) == 60)
    check("score of an empty list is 0", R.screen_score([]) == 0)
    ko = {"kind": R.KNOCKOUT, "verdict": "n/a"}
    check("knockouts stay out of the tier and the score",
          R.grade_letter([g(B, "met"), ko]) == "A" and R.screen_score([g(B, "met"), ko]) == 100)


def test_quotes():
    print("quotes")
    hay = [R._flat("• Ran A/B and alpha tests on GyftEasy, a consumer mobile gifting and rewards app\n"
                   "(Flutter, Firebase), with designers\nJuly 2022 – June 2025 at Citi’s desk")]
    check("a quote across a PDF line break matches",
          R.quote_on_page("gifting and rewards app (Flutter, Firebase), with designers", hay))
    check("curly vs straight apostrophe, en dash vs hyphen",
          R.quote_on_page("July 2022 - June 2025 at Citi's desk", hay))
    check("bold markers in the quote are ignored", R.quote_on_page("Ran **A/B** and alpha tests", hay))
    check("a paraphrase does not match", not R.quote_on_page("Ran A/B tests on a gifting app", hay))
    check("a two-word quote is too short to count", not R.quote_on_page("alpha tests", hay))

    reqs = [{"id": "R1", "kind": "basic", "text": "A/B testing"},
            {"id": "R2", "kind": "basic", "text": "Monetization"},
            {"id": "R3", "kind": "basic", "text": "SQL"},
            {"id": "R4", "kind": "preferred", "text": "Payments"}]
    page = "Ran A/B and alpha tests on GyftEasy\nProduct: Monetization and pricing analysis, SQL"
    body = R._flat("Ran A/B and alpha tests on GyftEasy")
    screen = {"verdicts": [
        {"id": "R1", "verdict": "met", "evidence": ["Ran A/B and alpha tests"]},
        {"id": "R2", "verdict": "met", "evidence": ["Monetization and pricing analysis"]},
        {"id": "R3", "verdict": "met", "evidence": ["shipped SQL dashboards weekly"]}]}
    out = {o["id"]: o for o in R.apply_verdicts(reqs, screen, [R._flat(page)], body)}
    check("a verified quote keeps met", out["R1"]["verdict"] == "met")
    check("Skills-line-only evidence is graded partial", out["R2"]["verdict"] == "partial",
          out["R2"]["note"])
    check("a quote not on the page is graded not met", out["R3"]["verdict"] == "not_met"
          and "not found" in out["R3"]["note"])
    check("a requirement with no verdict is not met", out["R4"]["verdict"] == "not_met"
          and out["R4"]["note"] == "not graded by the scanner")
    cert = [{"id": "R1", "kind": "basic", "text": "Certified Scrum Product Owner"}]
    out = R.apply_verdicts(cert, {"verdicts": [{"id": "R1", "verdict": "met", "evidence": [
        "Agile (CSPO Certified), Release management"]}]},
        [R._flat("Product: Agile (CSPO Certified), Release management")], R._flat("Ran A/B tests"))
    check("a certification stated on the Skills line stays met", out[0]["verdict"] == "met",
          out[0]["note"])


def test_facts_and_title():
    print("facts and title")
    # Invented employers and titles, in the spirit of testdata/record.example.json.
    rec = {"work": [
        {"employer": "Acme Corp", "roles": [
            {"title": "Product Management Intern", "start": "June 2026", "end": "August 2026"}]},
        {"employer": "Example Bank", "roles": [
            {"title": "Product Owner", "start": "October 2024", "end": "June 2025"},
            {"title": "Data Engineer", "start": "July 2022", "end": "September 2024"}]},
        {"employer": "Globex Labs", "roles": [
            {"title": "Software Intern", "start": "May 2021", "end": "July 2021"}]}],
        "education": []}
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "record.json"
        p.write_text(json.dumps(rec), encoding="utf-8")
        f = R.candidate_facts("ACME CORP ... EXAMPLE BANK ...", p)
    check("full-time years from the dates (Jul 2022 to Jun 2025 = 36 months)",
          f["full_time_years"] == 3.0, str(f))
    check("product-titled years count only titles with Product", f["product_titled_years"] == 0.8, str(f))
    check("an employer cut from the page does not count (Globex Labs)",
          f["internship_years"] == 0.2 and not any("Globex" in r for r in f["roles"]), str(f))

    # A bank's posting, 10-14: "5+ years with a bachelor's OR 2+ with an advanced degree" graded D
    # because a master's finishing in December read as not held in October.
    from datetime import date as _d
    oct14 = _d(2026, 10, 14)
    check("a degree finishing within 6 months counts as held",
          R.degree_state("December 2026", oct14).startswith("expected December 2026, counts as held"),
          R.degree_state("December 2026", oct14))
    check("one finishing later is still in progress",
          R.degree_state("December 2027", oct14) == "in progress")
    check("a past end date is completed", R.degree_state("May 2022", oct14) == "completed")

    jd = "# Acme - Senior Product Manager, Payments\n"
    lines = ["Product Owner, Payments Oct 2024 – June 2025",
             "SAP Experience Garage | AI Product Manager"]
    t = R.title_check(jd, lines)
    check("role phrase drops the seniority word", t["role"] == "Product Manager", t["role"])
    check("a project header is not a job title", t["exact"] is False)
    lines.append("Product Management Intern June 2026 – August 2026")
    t = R.title_check(jd, lines)
    check("wildcard search finds 'Product Management Intern'", t["exact"] is False and t["stem"] is True)
    t = R.title_check(jd, ["Product Manager, Payments Jan 2023 – Present"])
    check("an exact title match", t["exact"] is True)


# --- CLI ------------------------------------------------------------------------

def run(*args) -> tuple[int, str, str]:
    r = subprocess.run([sys.executable, str(SCRIPT)] + [str(a) for a in args],
                       capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr


def test_cli():
    print("cli")
    if not TEMPLATE.exists():
        print("  skip  CLI round trip (needs the template docx at profile/template/, which is not published)")
        return
    with tempfile.TemporaryDirectory() as td:
        folder = Path(td) / "acme-pm"
        folder.mkdir()
        (folder / "jd.md").write_text("""# Acme - Product Manager

## Qualifications
- 2+ years of product management experience
- Experience writing user stories and acceptance criteria
- Experience with machine learning products is a plus
""", encoding="utf-8")
        shutil.copy(TEMPLATE, folder / "Vaishnav_Ajai_Resume_Acme.docx")
        rc, out, _ = run("extract", folder)
        doc = json.loads((folder / "requirements.json").read_text(encoding="utf-8"))
        check("extract writes requirements.json", rc == 0 and len(doc["requirements"]) == 3, out)

        rc, out, err = run("grade", folder, "--round", "1")
        check("grade without the verdict file exits 2", rc == 2, err)

        (folder / "screen-r1.json").write_text(json.dumps({
            "search_terms": ["user stories", "acceptance criteria", "Kubernetes"],
            "verdicts": [
                {"id": "R1", "verdict": "partial", "evidence": [
                    "Wrote release acceptance criteria for Citi"], "note": "PO titles"},
                {"id": "R2", "verdict": "met", "evidence": [
                    "Wrote release acceptance criteria for Citi"]},
                {"id": "R3", "verdict": "met", "evidence": [
                    "building an ML anomaly detection tool"]}],
            "parse": []}), encoding="utf-8")
        rc, out, err = run("grade", folder, "--round", "1")
        review = (folder / "review-ats-r1.md").read_text(encoding="utf-8")
        screen = json.loads((folder / "screen.json").read_text(encoding="utf-8"))
        check("grade writes the section ending ATS_SCORE", rc == 0
              and review.rstrip().splitlines()[-1] == f"ATS_SCORE: {screen['score']}", err + out)
        check("grade: one basic partial is C, score (2*.5+2*1+1)/5 = 80",
              screen["grade"] == "C" and screen["score"] == 80, str(screen))
        check("findability uses the locked search strings",
              screen["findability"]["total"] == 3 and "Kubernetes" in screen["findability"]["missing"],
              str(screen["findability"]))
        doc = json.loads((folder / "requirements.json").read_text(encoding="utf-8"))
        check("round 1 locks the search strings into requirements.json",
              doc.get("search_terms") == ["user stories", "acceptance criteria", "Kubernetes"])
        check("the section carries no em-dash", "—" not in review)

        # Fallback: nothing extracted, the screener supplies the list.
        (folder / "jd.md").write_text("# Acme - PM\nWe want a builder who ships.\n", encoding="utf-8")
        run("extract", folder)
        doc = json.loads((folder / "requirements.json").read_text(encoding="utf-8"))
        check("too few items writes an empty list with source none",
              doc["requirements"] == [] and doc["source"] == "none")
        (folder / "screen-r1.json").write_text(json.dumps({
            "requirements": [{"id": "A", "kind": "basic", "text": "Ships product"},
                             {"id": "B", "kind": "preferred", "text": "ML exposure"}],
            "verdicts": [{"id": "A", "verdict": "met", "evidence": [
                "Productized 4+ internal tools"]},
                {"id": "B", "verdict": "not_met", "evidence": []}]}), encoding="utf-8")
        rc, out, err = run("grade", folder, "--round", "1")
        doc = json.loads((folder / "requirements.json").read_text(encoding="utf-8"))
        screen = json.loads((folder / "screen.json").read_text(encoding="utf-8"))
        check("fallback: the screener's list is adopted and renumbered",
              rc == 0 and doc["source"] == "screener" and [r["id"] for r in doc["requirements"]] == ["R1", "R2"],
              err + out)
        check("fallback: verdict ids follow the renumbering", screen["grade"] == "B", str(screen))


if __name__ == "__main__":
    test_extract()
    test_grade()
    test_quotes()
    test_facts_and_title()
    test_cli()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
