"""Table-driven tests for ats_score.py - the plain JD-keyword coverage scorer.

Any change to extraction, weighting or the arithmetic reruns this table first.
The whole contract is the last line, so the format cases are load-bearing: they
pin the exact `ATS: NN% (K of M weighted terms matched); missing: ...` shape
that every downstream parser reads, including the no-missing variant.

Fakes for JD and resume text, plus the committed template docx for the real
docx text layer and pipeline/keyword_corpus/frequencies.json for rarity.

Run: python pipeline/test_ats_score.py   (no Word, no LLM, no network)
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import analyze_corpus  # noqa: E402
import ats_score as A  # noqa: E402

SCRIPT = HERE / "ats_score.py"
TEMPLATE = A.ROOT / "profile" / "template" / "Vaishnav_Ajai_Resume_Template_Format.docx"

# The one shape every caller parses. Anchored, so a stray prefix or a trailing
# period fails here rather than in a dashboard three files away.
ATS_RX = re.compile(r"ATS: \d+% \(\d+ of \d+ weighted terms matched\)"
                    r"(?:; missing: \S.*[^.\s])?")

PASS = FAIL = 0
SKIP = 0


def skip(name: str, why: str) -> None:
    """Skip a leg that needs a private file this public copy does not carry."""
    global SKIP
    SKIP += 1
    print(f"skip  {name} ({why})")


def run_cli(*args) -> tuple[int, str]:
    """The CLI contracts (exit codes, last line) can only be pinned by running
    the CLI. Still no Word, no LLM, no network."""
    r = subprocess.run([sys.executable, str(SCRIPT)] + [str(a) for a in args],
                       capture_output=True, text=True)
    return r.returncode, r.stdout


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"FAIL  {name}" + (f" - {detail}" if detail else ""))


def terms_by_text(terms) -> dict:
    return {t.text: t for t in terms}


def T(text: str, weight: float) -> A.Term:
    return A.Term(A.canonical_term(text), A.variants_for(text), weight, "jd-mined")


# --- fixtures ---------------------------------------------------------------

JD_PM = """# Senior Product Manager, Payments Platform - Acme

**Location:** New York, NY

Acme is building the payments platform. You will own the roadmap and partner
with engineering on the API surface. Our platform is a payments platform used
by enterprise customers.

**Minimum Qualifications:**

- Experience with tokenization and digital wallets
- Comfort with SQL and analytics
- Strong stakeholder management across engineering teams
"""

JD_TPM = """# Technical Program Manager, Infrastructure

You will drive dependencies and milestones across engineering.
Dependencies are the job; milestones are how we report.
"""

RESUME_DOCX = """Vaishnav Ajai
Citi - Product Owner
Drove the payments platform roadmap with engineering partners on the API layer.
Built stakeholder management rhythm across the platform org; payments roadmap.
Technical: Python, SQL, Machine Learning, Kafka, Power BI
Interests: chess, distance running
"""

SKILLS_TEXT = ("Technical: Python, SQL, Machine Learning, Kafka, Power BI\n"
               "Interests: chess, distance running")


def main() -> int:
    freq = A.load_corpus_frequencies()

    print("-- 1. boundary matching --")
    rx_rag = analyze_corpus.compile_term("rag")
    check("'rag' does NOT hit inside 'storage'",
          not rx_rag.search(analyze_corpus.normalize("object storage tiers")))
    check("'rag' hits standalone",
          bool(rx_rag.search(analyze_corpus.normalize("we use RAG"))))
    check("'spec' does NOT hit inside 'specific'",
          not analyze_corpus.compile_term("spec").search(
              analyze_corpus.normalize("specific requirements")))

    print("-- 2. variant normalization --")
    gtm_forms = ["go to market", "go-to-market", "GTM"]
    canons = {A.canonical_term(f) for f in gtm_forms}
    check("go to market / go-to-market / GTM resolve to one canonical term",
          len(canons) == 1, str(canons))
    v = A.variants_for(A.canonical_term("GTM"))
    check("...and that term carries all three surface forms",
          all(any(A.fold(x) == A.fold(f) for x in v) for f in gtm_forms), str(v))
    ab = A.variants_for(A.canonical_term("a/b testing"))
    check("'a/b testing' and 'ab testing' are one term",
          A.canonical_term("a/b testing") == A.canonical_term("ab testing")
          and any(A.fold(x) == "ab testing" for x in ab), str(ab))
    t = A.Term("go-to-market", A.variants_for("go-to-market"), 1.0, "phrase-list")
    check("the merged term matches every surface form in text",
          all(t.hits(analyze_corpus.normalize(f"we own {f} here"))
              for f in gtm_forms))
    check("separator-squashed forms stay >=4 chars and carry a letter "
          "(so '0-1' never spawns '01')",
          "01" not in A.variants_for("0-1"), str(A.variants_for("0-1")))

    print("-- 3. extraction and weighting --")
    terms = terms_by_text(A.extract_jd_terms(JD_PM, "PM", freq))
    check("a title-line term outweighs an equally common body-only term",
          terms["payments"].weight > terms["roadmap"].weight,
          f"payments={terms['payments']} roadmap={terms['roadmap']}")
    check("a title-line term records the x2 boost",
          terms["payments"].boosted and terms["payments"].weight
          >= 2 * terms["payments"].occurrences_capped * terms["payments"].rarity
          * 0.999, str(terms.get("payments")))
    check("a requirements-block term is boosted too",
          terms["tokenization"].boosted, str(terms.get("tokenization")))
    check("a body-only term is not boosted",
          not terms["partner"].boosted if "partner" in terms else True,
          str(terms.get("partner")))
    check("the employer's own name is never a coverage target",
          "acme" not in terms, str(sorted(terms)[:20]))
    check("occurrences are capped at 3",
          all(t.occurrences_capped <= 3 for t in terms.values()))

    stop_jd = ("# Product Manager - Acme\n\nThese systems matter. Those systems "
               "matter too. These are the platforms; those are the pipelines. "
               "Being on call is part of being here.\n")
    st = {t.text for t in A.extract_jd_terms(stop_jd, "PM", freq)}
    check("'these'/'those'/'being' are stopped, not mined as JD keywords",
          not ({"these", "those", "being"} & st), str(sorted(st)))

    # The capitalized-name layer bypassed the stop lists once, so ALL-CAPS
    # section headings entered the sheet as if they were acronyms.
    heading_jd = ("**ABOUT US**\nWe are hiring. About us: we build things.\n"
                  "**JOB RESPONSIBILITIES**\nThe job is a real job.\n"
                  "You will use SQL daily; SQL is required, and more SQL.\n")
    hits = A._capitalized_names(heading_jd, analyze_corpus.normalize(heading_jd))
    check("ALL-CAPS section headings no longer mine as acronyms",
          "about" not in hits and "job" not in hits, str(sorted(hits)))
    check("...and a real acronym in the same text still does",
          "sql" in hits, str(sorted(hits)))

    print("-- 4. bucket detection --")
    check("'Technical Program Manager' title -> TPM", A.bucket_for(JD_TPM) == "TPM")
    check("'Senior Product Manager' title -> PM", A.bucket_for(JD_PM) == "PM")
    check("'Product Management Intern' title -> PM",
          A.bucket_for("# Product Management Intern - Datadog\n\nbody") == "PM")
    check("'Program Manager' (non-technical) title -> TPM",
          A.bucket_for("# Program Manager, Member Experience\n\nbody") == "TPM")
    tpm_terms = terms_by_text(A.extract_jd_terms(JD_TPM, "TPM", freq))
    check("TPM JD mines its own vocabulary", "dependencies" in tpm_terms,
          str(sorted(tpm_terms)[:20]))

    print("-- 5. corpus rarity --")
    if A.FREQUENCIES.exists():
        check("frequencies.json is committed, so every clone has it",
              A.FREQUENCIES.exists(), str(A.FREQUENCIES))
        check("...and it is the source that answers the rarity question",
              A.FREQ_SOURCE == "frequencies.json", f"source={A.FREQ_SOURCE}")
        check("a >=75%-frequency corpus term is damped x0.5",
              A.rarity_multiplier(["engineering"], freq, "PM") == 0.5,
              str(A.corpus_frequency(["engineering"], freq, "PM")))
        check("a 25-74% term is neutral x1.0",
              A.rarity_multiplier(["metrics"], freq, "PM") == 1.0,
              str(A.corpus_frequency(["metrics"], freq, "PM")))
        check("a term absent from the corpus is x1.5",
              A.rarity_multiplier(["sprocket-widgetry"], freq, "PM") == 1.5)
        check("a mined term at >=75% corpus frequency damps to x0.5",
              A.mined_rarity(["build"], freq, "PM") == 0.5,
              str(A.corpus_frequency(["build"], freq, "PM")))
        check("a corpus-SILENT mined term stays x1.0, never the x1.5 a phrase-list "
              "term would get (that would price boilerplate above real vocabulary)",
              A.mined_rarity(["sprocket-widgetry"], freq, "PM") == 1.0
              and A.rarity_multiplier(["sprocket-widgetry"], freq, "PM") == 1.5)
        mined = [t for t in A.extract_jd_terms(JD_PM, "PM", freq)
                 if t.source == "jd-mined"]
        check("jd-mined terms never enter above x1.0 rarity",
              all(t.rarity <= 1.0 for t in mined),
              str([(t.text, t.rarity) for t in mined][:5]))
        # Direct calls leave the one line that matters untested: the call site in
        # extract_jd_terms. Reverting it to a bare 1.0 passes every direct case
        # while silently raising every score. "build" is 93% doc-frequency in the PM
        # corpus and is not in PHRASES, so it must arrive jd-mined at rarity 0.5 and
        # weight min(occ,3) x 1 (unboosted) x 0.5 = 1.5, not 3.0.
        damp_jd = ("# Senior Product Manager, Payments Platform - Acme\n\n"
                   "We build payment products. The team will build more products, "
                   "and build the products customers ask for; our products are how "
                   "we build trust.\n")
        b = terms_by_text(A.extract_jd_terms(damp_jd, "PM", freq)).get("build")
        check("the damper is WIRED: a >=75% mined term arrives damped from the real "
              "extraction, at half weight",
              b is not None and b.source == "jd-mined" and b.rarity == 0.5
              and abs(b.weight - 1.5) < 1e-9, str(b))
    else:
        skip("corpus rarity",
             "pipeline/keyword_corpus/frequencies.json is generated data and is not published; build it with collect_corpus.py + analyze_corpus.py")

    print("-- 6. unusable JD vocabulary never becomes a coverage target --")
    dropped: list = []
    slop = A.extract_jd_terms(
        "# Product Manager\n\nWe want robust systems. Robust systems are the "
        "seamless path. The seamless path matters. You will drive the roadmap; "
        "drive the roadmap daily.", "PM", freq, dropped)
    names = {t.text for t in slop}
    check("a JD word the resume may never write is dropped from the term sheet",
          "robust" not in names and "seamless path" not in names, str(sorted(names)))
    check("...while the usable vocabulary in the same JD survives",
          "roadmap" in names and "drive" in names, str(sorted(names)))
    # "robust" alone never reaches the drop: the bigram-subsumption rule folded
    # it into "robust systems", which is the term that gets dropped.
    check("the drop is collected", {"robust systems", "seamless path"} <= set(dropped),
          str(dropped))
    note = A.dropped_note(dropped)
    check("...and formats into one note naming every term",
          note.startswith(f"{len(dropped)} terms dropped as unusable resume "
                          "vocabulary")
          and "robust systems" in note and "seamless path" in note, note)
    check("no drop means no note", A.dropped_note([]) == "", A.dropped_note([]))
    quiet: list = []
    A.extract_jd_terms("# Product Manager\n\nOwn the roadmap with engineering.",
                       "PM", freq, quiet)
    check("a clean JD reports no drop", quiet == [], str(quiet))

    print("-- 7. resume text reading --")
    if TEMPLATE.exists():
        rt = A.resume_texts(TEMPLATE)
        check("the committed template docx yields a text layer",
              len(rt.docx.strip()) > 100 and not rt.notes,
              f"{len(rt.docx)} chars notes={rt.notes}")
        check("...with no PDF leg when none is given", rt.pdf == "")
    else:
        skip("docx text-layer case",
             "needs the template docx at profile/template/, which is not published")
    junk = Path(tempfile.mkdtemp(prefix="ats_docx_")) / "junk.docx"
    junk.write_bytes(b"not a zip, not a docx")
    bad = A.resume_texts(junk)
    check("an unreadable docx degrades to a note, never an exception",
          bad.docx == "" and any("docx unreadable" in n for n in bad.notes),
          str(bad.notes))
    shutil.rmtree(junk.parent, ignore_errors=True)
    split = A.ResumeText(docx=RESUME_DOCX, pdf=RESUME_DOCX, skills=SKILLS_TEXT)
    check("body is the docx minus its Skills label lines",
          "roadmap" in split.body and "Kafka" not in split.body, split.body)

    print("-- 8. percentage arithmetic --")
    rt = A.ResumeText(docx=RESUME_DOCX, pdf=RESUME_DOCX, skills=SKILLS_TEXT)
    rep = A.score([T("roadmap", 3.0), T("sprocket-widgetry", 1.0)], rt)
    check("pct is matched weight over total weight, not a term count",
          abs(rep.pct - 75.0) < 1e-9 and rep.matched_n == 1 and rep.total_n == 2,
          f"pct={rep.pct} {rep.matched_n}/{rep.total_n}")
    check("...and the weights are tallied too",
          abs(rep.matched_weight - 3.0) < 1e-9
          and abs(rep.total_weight - 4.0) < 1e-9,
          f"{rep.matched_weight}/{rep.total_weight}")
    check("missing is ordered by weight, heaviest first",
          A.score([T("sprocket-widgetry", 1.0), T("frobnicator", 5.0)], rt).missing
          == ["frobnicator", "sprocket-widgetry"])
    check("every term lands in exactly one status",
          len(rep.rows) == 2
          and {r.status for r in rep.rows} == {A.MATCHED, A.MISSING})
    empty = A.score([], A.ResumeText())
    check("no terms at all is 0%, not a ZeroDivisionError",
          empty.pct == 0.0 and empty.total_n == 0, str(empty))

    print("-- 9. where a term matched --")
    both = A.score([T("cross-functional", 2.0)],
                   A.ResumeText(docx="cross-functional delivery",
                                pdf="cross-functional delivery"))
    check("a term in both layers reads 'both'", both.rows[0].where == "both",
          both.rows[0].where)
    docx_only = A.score([T("cross-functional", 2.0)],
                        A.ResumeText(docx="cross-functional delivery",
                                     pdf="unrelated text"))
    check("a term the PDF text layer lost reads 'docx', and still counts",
          docx_only.rows[0].where == "docx"
          and docx_only.rows[0].status == A.MATCHED, str(docx_only.rows))
    pdf_only = A.score([T("cross-functional", 2.0)],
                       A.ResumeText(docx="unrelated",
                                    pdf="cross-functional delivery"))
    check("a term only in the PDF text layer reads 'pdf'",
          pdf_only.rows[0].where == "pdf"
          and pdf_only.rows[0].status == A.MATCHED, str(pdf_only.rows))
    nopdf = A.score([T("cross-functional", 2.0)],
                    A.ResumeText(docx="cross-functional delivery", pdf=""))
    check("with no PDF at all the column is still 'docx'",
          nopdf.rows[0].where == "docx", str(nopdf.rows))
    gone = A.score([T("cross-functional", 2.0)],
                   A.ResumeText(docx="unrelated", pdf="unrelated"))
    check("a missing term reads '-'",
          gone.rows[0].where == "-" and gone.rows[0].status == A.MISSING,
          str(gone.rows))
    fused = A.score([T("cross-functional", 2.0)],
                    A.ResumeText(docx="crossfunctional delivery",
                                 pdf="crossfunctional delivery"))
    check("matching stays tolerant: a fused form still counts as coverage",
          fused.matched_n == 1, str(fused.rows))

    print("-- 10. the ATS line, exactly --")
    line = A.ats_line(rep)
    check("the line matches the contract shape", bool(ATS_RX.fullmatch(line)), line)
    check("...and is exactly right for a known case",
          line == "ATS: 75% (1 of 2 weighted terms matched); "
                  "missing: sprocket-widgetry", line)
    clean = A.ats_line(A.score([T("roadmap", 3.0)], rt))
    check("with nothing missing the tail is omitted entirely",
          clean == "ATS: 100% (1 of 1 weighted terms matched)"
          and "; missing" not in clean, clean)
    check("...and the empty report still parses",
          A.ats_line(empty) == "ATS: 0% (0 of 0 weighted terms matched)",
          A.ats_line(empty))
    many = A.score([T(f"widget-{i:02d}", float(20 - i)) for i in range(15)], rt)
    tail = A.ats_line(many).split("; missing: ")[1]
    check("the missing list is capped at the top ten by weight",
          len(tail.split(", ")) == 10 and tail.startswith("widget-00, widget-01")
          and "widget-10" not in tail, tail)
    check("...while the Report keeps the full list",
          len(many.missing) == 15, str(len(many.missing)))
    check("pct rounds, it does not truncate",
          A.ats_line(A.score([T("roadmap", 2.0), T("frobnicator", 1.0)], rt))
          .startswith("ATS: 67% "),
          A.ats_line(A.score([T("roadmap", 2.0), T("frobnicator", 1.0)], rt)))

    print("-- 11. skills-only detection --")
    kafka = A.score([T("kafka", 4.0)], rt)
    check("a term hit only in a 'Technical:' line is flagged",
          kafka.skills_only == ["kafka"], str(kafka.skills_only))
    check("...and still counts as matched at full weight",
          abs(kafka.matched_weight - 4.0) < 1e-9, str(kafka.matched_weight))
    check("a term hit in a body bullet is not flagged",
          A.score([T("roadmap", 4.0)], rt).skills_only == [])

    print("-- 12. CLI contracts and sheet reuse --")
    tmp = Path(tempfile.mkdtemp(prefix="ats_cli_"))
    try:
        jd_file = tmp / "jd.md"
        jd_file.write_text(JD_PM, encoding="utf-8")

        rc, so = run_cli(jd_file)
        check("no docx at all: exit 0 and the TERMS last line stays",
              rc == 0 and so.splitlines()[-1].startswith("TERMS: "),
              f"rc={rc} last={so.splitlines()[-1] if so else ''!r}")
        check("...and that line parses",
              bool(re.fullmatch(r"TERMS: \d+ terms / bucket (PM|TPM) / weight \d+",
                                so.splitlines()[-1])), so.splitlines()[-1])

        rc, so = run_cli(jd_file, tmp / "gone.docx")
        check("a NAMED but missing docx is unusable input: exit 2, no ATS line",
              rc == 2 and "ATS:" not in so and "cannot read resume" in so,
              f"rc={rc} {so[-200:]!r}")

        junk2 = tmp / "junk.docx"
        junk2.write_bytes(b"not a zip, not a docx")
        rc, so = run_cli(jd_file, junk2)
        check("a named but UNREADABLE docx is unusable input too: exit 2",
              rc == 2 and "ATS:" not in so and "cannot read resume" in so,
              f"rc={rc} {so[-200:]!r}")

        if TEMPLATE.exists():
            lone = tmp / "resume_no_pdf.docx"
            shutil.copy(TEMPLATE, lone)
            rc, so = run_cli(jd_file, lone)
            lines = [ln for ln in so.splitlines() if ln.strip()]
            check("a real docx with no PDF still scores, at exit 0",
                  rc == 0 and bool(ATS_RX.fullmatch(lines[-1])),
                  f"rc={rc} last={lines[-1] if lines else ''!r}")
            check("...the ATS line is printed at column zero",
                  lines[-1] == lines[-1].lstrip(), repr(lines[-1]))
            check("...the missing PDF is REPORTED, not silent",
                  any("no PDF at" in ln for ln in lines),
                  "\n".join(ln for ln in lines if ln.startswith("note"))
                  or "NO NOTES AT ALL")
            check("...and every scored row is docx-only (where column is 'docx' "
                  "or '-')",
                  all(re.search(r"\s(?:docx|-)$", ln) for ln in lines
                      if re.match(r"^\S.*\s+\d+\.\d\s+(matched|missing)\s+", ln)),
                  "\n".join(lines[6:10]))
        else:
            skip("CLI scoring case",
                 "needs the template docx at profile/template/, which is not published")

        sheet = tmp / "sheet.json"
        rc, so = run_cli(jd_file, "--terms", "--json", sheet)
        written = json.loads(sheet.read_text(encoding="utf-8"))
        check("--terms --json writes the bucket and the term list",
              rc == 0 and written["bucket"] == "PM" and len(written["terms"]) > 5,
              str(sorted(written))[:200])
        check("...and the sheet carries no class fields any more",
              not any(k in written for k in ("ceiling", "classes"))
              and not any(k in written["terms"][0]
                          for k in ("klass", "class", "action")),
              str(sorted(written["terms"][0])))

        sheet.write_text(json.dumps({"bucket": "Product"}), encoding="utf-8")
        rc, so = run_cli(jd_file, "--terms", "--json", sheet)
        check("a garbage bucket in the sheet is refused, not obeyed",
              rc == 0 and "bucket  PM (from JD title line)" in so,
              f"rc={rc} {so[:400]!r}")
        check("...and it says so in a note",
              "unrecognized bucket" in so and "'Product'" in so, so[:600])

        sheet.write_text(json.dumps({"bucket": "TPM"}), encoding="utf-8")
        rc, so = run_cli(jd_file, "--terms", "--json", sheet)
        check("a VALID bucket in the sheet is still reused",
              rc == 0 and "bucket  TPM (from term sheet)" in so, so[:300])
        rc, so = run_cli(jd_file, "--terms", "--bucket", "PM", "--json", sheet)
        check("...but an explicit --bucket wins over the sheet",
              rc == 0 and "bucket  PM (from --bucket)" in so, so[:300])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("-- 13. output hygiene (score.md is a linted file) --")
    # dropped_note is deliberately excluded: its whole job is to NAME the
    # ban-list words it removed, so it is the one line allowed to carry them.
    joined = "\n".join([A.ats_line(rep), A.terms_line(slop, "PM")]
                       + [r.term for r in rep.rows]
                       + [r.status for r in rep.rows]
                       + A._table(rep.rows))
    check("no em/en-dash anywhere in the scorer's own output vocabulary",
          not re.search(r"[–—]", joined), repr(joined))
    check("no voice.md banned word in the scorer's own output vocabulary",
          not re.search(r"(?i)\b(spearheaded|leveraged|synerg|passionate|delve"
                        r"|utilized?|robust|seamless|holistic|cutting-edge"
                        r"|tapestry|empower|foster|journey)\b", joined),
          repr(joined))
    check("_ascii folds a non-ASCII character rather than raising",
          A._ascii("a — b ≤ c") == "a , b <= c",
          A._ascii("a — b ≤ c"))

    print(f"\n{PASS} passed, {FAIL} failed, {SKIP} skipped")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
