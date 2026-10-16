"""Hard-filter tests. Run: python pipeline/test_filter.py

Every rule is asserted in BOTH directions. The near-miss cases matter more than
the hits: a wrongly rejected job is invisible forever. Phrasings marked [real]
are copied from JDs actually sitting in the queue.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import filter as hf

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}  (got {got!r}, want {want!r})")
        FAILS.append(label)


def rejects(label, title, jd="", loc="", rule=None):
    check(label, hf.hard_filter(title, jd, loc), rule)


def survives(label, title, jd="", loc=""):
    check(label, hf.hard_filter(title, jd, loc), None)


def main():
    print("seniority titles (reject Principal/Staff/Director, per Vaish)")
    rejects("Principal PM", "Principal Product Manager", rule="seniority_title")
    rejects("Sr. Principal PM", "Sr. Principal Product Manager", rule="seniority_title")
    rejects("Staff PM", "Staff, Data Product Manager", rule="seniority_title")
    rejects("Director", "Director, Product Management - Salesforce", rule="seniority_title")
    rejects("VP", "Vice President, Product Strategy", rule="seniority_title")
    rejects("Head of", "Head of Product Marketing", rule="seniority_title")

    print("...but 'Lead' and 'Senior' go to the scorer, not the bin")
    survives("Lead Digital PM (senior side)", "Lead Digital Product Manager")
    survives("Lead Product Ops Associate (junior side)", "Lead Product Operations Associate")
    survives("Senior PM", "Senior Product Manager, Credit")
    survives("PM II", "Product Manager II - SQL & Sheets Growth")
    survives("APM", "Associate Product Manager")
    # preferences.md caps strategy roles at 6 rather than rejecting them, so a
    # bare \bstaff\b / \bchief\b regex must not swallow Chief of Staff.
    survives("Chief of Staff exempt", "Chief of Staff, Product")

    print("sponsorship (real phrasings)")
    rejects("[real] Jobgether 10-07", "Product Manager",
            "Visa sponsorship is generally not available for this role.", rule="no_sponsorship")
    rejects("[real] Mobility Global 10-07", "Product Manager",
            "Candidates will not require employer sponsorship now or in the future.",
            rule="no_sponsorship")
    rejects("[real] Capital One 10-08", "Product Manager",
            "Capital One will not sponsor a new applicant for employment authorization "
            "for this position.", rule="no_sponsorship")
    survives("sponsorship offered", "Product Manager", "Visa sponsorship is available.")
    survives("executive sponsor", "Product Manager",
             "You will not require executive sponsorship to launch small experiments.")
    rejects("[real] JPMorgan", "Product Owner",
            "Additional information. Visa sponsorship is not available for this position.",
            rule="no_sponsorship")
    rejects("[real] Rippling", "Technical Product Manager",
            "We cannot provide new visa sponsorship for this role but are open to "
            "facilitating H-1B visa transfers.", rule="no_sponsorship")
    rejects("[real] USAA", "Product Manager",
            "USAA does not provide visa sponsorship for this role.", rule="no_sponsorship")
    rejects("[real] Wells Fargo", "Product Manager",
            "This position is not eligible for Visa sponsorship. Hybrid work schedule.",
            rule="no_sponsorship")
    rejects("[real] Osaic", "Product Manager",
            "Candidates must have valid work authorization that does not now and/or will "
            "not in the future require sponsorship of a visa for employment authorization.",
            rule="no_sponsorship")
    rejects("unable to sponsor", "Product Manager",
            "We are unable to sponsor visas at this time.", rule="no_sponsorship")

    print("...but sponsor-the-word must not trigger it")
    # Real Cigna line. A bare /sponsor/ regex rejects this job for no reason.
    survives("[real] executive sponsors", "Product Manager",
             "Communicate progress, risks, dependencies, and tradeoffs clearly to "
             "executive sponsors and steering forums, enabling informed decisions.")
    survives("project sponsor", "Product Manager",
             "Partner with the project sponsor to align on scope.")
    survives("company DOES sponsor", "Product Manager",
             "We provide visa sponsorship for qualified candidates.")
    survives("willing to sponsor", "Product Manager",
             "Sponsorship is available for this role.")

    print("years of experience (ceiling 7, high-confidence only)")
    rejects("minimum 8 years", "Product Manager",
            "Minimum requirements: 8 years of product management experience required.",
            rule="yoe_6plus")
    rejects("[real] Twilio 12+", "Product Manager",
            "*Required: 12+ years of product management experience building large scale "
            "platforms. 10+ years people management experience.", rule="yoe_6plus")
    rejects("more than 10 years", "Product Manager",
            "More than 10 years Relevant Product Management experience in agile software.",
            rule="yoe_6plus")
    rejects("at least 9 years", "Product Manager",
            "At least 9 years of experience in a product role.", rule="yoe_6plus")
    rejects("7+ years required (ceiling 6)", "Product Manager",
            "Requirements: 7+ years of product management experience.", rule="yoe_6plus")

    print("...but ambiguous or reachable asks must pass")
    survives("5-8 years range (Vaish's example)", "Product Manager",
             "We are looking for 5-8 years of product management experience.")
    rejects("6+ years is at the ceiling (2026-10-09)", "Product Manager",
            "6+ years of product management experience required.", rule="yoe_6plus")
    survives("5+ years is the stretch line", "Product Manager",
             "5+ years of product management experience required.")

    print("Senior titles: rejected only when the JD states no years")
    rejects("Senior with a JD and no years", "Senior Product Manager",
            "You will own the roadmap for our payments platform.",
            rule="senior_no_years")
    rejects("Sr. with no years", "Sr. Product Manager, Growth",
            "Own experiments end to end.", rule="senior_no_years")
    rejects("Product Manager III with no years", "Product Manager III",
            "Lead discovery for the mobile app.", rule="senior_no_years")
    survives("Senior with an in-band years ask", "Senior Product Analyst",
             "Requirements: 3+ years of product analytics experience.")
    survives("Senior with no JD yet (title-only pre-check)", "Senior Product Manager")
    survives("Lead stays with the scorer", "Lead Product Manager",
             "Own the roadmap for our payments platform.")
    survives("'Seniority' is not 'Senior'", "Product Manager",
             "Seniority will match experience.")
    survives("8+ preferred is not required", "Product Manager",
             "8+ years of experience preferred but not required for the right candidate.")
    survives("[real] fintech 1-3 years", "Product Manager",
             "What You'll Need: 1-3 years of product experience, or 3 - 5 years of total "
             "experience in a role that built real judgment.")
    survives("[real] Stripe 3+ and 7+", "Program Manager",
             "Minimum requirements: 3+ years of demonstrated experience leading complex "
             "cross-functional work. Preferred: 7+ years of experience in program management.")
    # 10-06: requirement bullets are additive. The minimum across them read
    # Google's 10-year ask as 5 and let 25 such JDs reach the scorer.
    rejects("[real] Google 10 + 5 additive bullets", "Group Product Manager",
            "Minimum qualifications:\n* Bachelor's degree or equivalent practical experience.\n"
            "* 10 years of experience in product management or a related technical role.\n"
            "* 5 years of experience taking technical products from conception to launch.",
            rule="yoe_6plus")
    rejects("[real] Amazon 5/7/3 basic quals", "Technical Program Manager",
            "**Basic Qualifications**\n\n* 5+ years of technical product or program management "
            "experience\n* 7+ years of working directly with engineering teams experience\n"
            "* 3+ years of software development experience", rule="yoe_6plus")
    rejects("[real] Google 8 then a Preferred heading right after", "Technical Program Manager",
            "Minimum qualifications:\n* 8 years of experience in program management.\n\n"
            "### **Preferred qualifications:**\n\n* 8 years of experience managing projects.",
            rule="yoe_6plus")
    rejects("[real] hard bullet next to 'advanced degree is preferred'", "Product Manager",
            "* Bachelor's in engineering; an advanced degree is preferred.\n\n"
            "* Typically 7 years of relevant experience in a business unit.", rule="yoe_6plus")
    rejects("[real] 8+ years across product operations", "Product Operations Manager",
            "What you bring\n\n* 8+ years across product operations, program management, "
            "and/or design leadership.", rule="yoe_6plus")
    survives("[real] Gallo: a 'How You'll Stand Out' list is soft", "Technical Product Manager",
             "**Requirements:**\n* 3 years of product owner experience\n\n"
             "**How You'll Stand Out**\n\n* Bachelor's degree plus 7 years of IT experience "
             "to include software development.")
    survives("a soft heading covers every bullet under it", "Product Manager",
             "Required: 3+ years of PM experience.\n\nPreferred Qualifications\n"
             "* MBA\n* SQL\n* Startup background\n* Experience with B2B SaaS\n"
             "* 8+ years of experience in fintech")
    survives("[real] company data tenure is not an ask", "Product Manager",
             "Backed by 18 years of proprietary credit data and deep risk expertise, we "
             "lend.\n\n**Required Experience**\n* 3-5 years of experience in product management.")
    rejects("we-phrased hard ask", "Product Manager",
            "We are looking for a PM with 8+ years of product management experience.",
            rule="yoe_6plus")
    rejects("our-phrased hard ask", "Product Manager",
            "Our ideal candidate has 10 years of experience in product.", rule="yoe_6plus")
    survives("a team's combined tenure is not an ask", "Product Manager",
             "Join a team with 20+ years of experience in payments.\n"
             "Requirements: 2+ years of product management experience.")
    survives("[real] T-Mobile dual path", "Product Manager",
             "Experience in product management within Agile environments, including 7-9 "
             "years (Bachelor's degree) or 5+ years (advanced degree).")
    # "At least 18 years of age" is a real T-Mobile line, not a YOE requirement.
    survives("[real] 18 years of age", "Product Manager",
             "At least 18 years of age. Legally authorized to work in the United States.")
    survives("no years stated", "Product Manager", "Own the roadmap for our payments platform.")
    # Company-age boilerplate, not a hiring bar. Both were live false positives
    # that would have silently killed two real jobs.
    survives("[real] ETS company history", "Lead Product Operations Associate",
             "Enabling lifelong learners worldwide to be future-ready. For more than 75 "
             "years, we've been advancing the science of measurement.")
    survives("[real] UST company history", "Product Manager",
             "UST is a global digital transformation solutions provider. For more than 20 "
             "years, UST has worked side by side with the world's best companies.")
    survives("celebrating N years", "Product Manager",
             "Celebrating 30 years of building tools developers love.")

    print("marketing / sales titles")
    rejects("product marketing", "Product Marketing Manager", rule="marketing_sales")
    rejects("marketing specialist", "Marketing Specialist", rule="marketing_sales")
    rejects("sales manager", "Regional Sales Manager, Federal DoD", rule="marketing_sales")
    survives("growth PM is a real PM lane", "Product Manager, Growth")
    survives("GTM program manager", "Program Manager, GTM Strategic Programs")

    print("adjacent families (2026-10-08) pass the title rules")
    survives("developer advocate", "Developer Advocate")
    survives("developer relations", "Developer Relations Engineer, AI")
    survives("forward deployed", "Forward Deployed Engineer")
    survives("product analyst", "Product Analyst II")
    survives("technical product owner", "Technical Product Owner")
    rejects("sales engineer stays out", "Sales Engineer", rule="marketing_sales")
    rejects("solutions architect stays out", "Solutions Architect", rule="seniority_title")

    print("location re-check")
    rejects("Canada only", "Product Manager", loc="Remote - Canada", rule="non_us")
    # Real Twilio posting that leaked through the first country shortlist and was
    # only caught because a scorer agent flagged it. The list is exhaustive now.
    rejects("[real] Remote - Colombia", "Program Manager",
            loc="Remote - Colombia", rule="non_us")
    rejects("Remote - India", "Product Manager", loc="Remote - India", rule="non_us")
    rejects("Remote - Singapore", "Product Manager", loc="Remote - Singapore", rule="non_us")
    rejects("LATAM region", "Product Manager", loc="Remote - LATAM", rule="non_us")
    survives("US remote", "Product Manager", loc="Remote - US")
    survives("multi-region incl. US", "Product Manager",
             loc="US-Remote; US-Chicago; CA-Toronto")
    survives("unknown location", "Product Manager", loc="")

    print("clearance / US-citizenship requirement (Vaish: skip govcon/defence)")
    # The exact line from both Guidehouse JDs that reached the auto-tailor lane.
    rejects("Guidehouse Public Trust line", "Product Owner",
            "Must be able to obtain a Public Trust Clearance (US Citizen).",
            rule="clearance_required")
    rejects("clearance required", "Product Manager",
            "An active Secret clearance is required for this position.",
            rule="clearance_required")
    rejects("TS/SCI", "Technical Program Manager",
            "Candidates must hold a TS/SCI clearance with polygraph.",
            rule="clearance_required")
    rejects("ability to obtain", "Product Manager",
            "Clearance Required: Ability to Obtain Public Trust",
            rule="clearance_required")
    rejects("explicit citizenship requirement", "Product Manager",
            "Applicants must be a US citizen due to federal contract terms.",
            rule="clearance_required")
    rejects("US citizenship is required", "Product Manager",
            "U.S. citizenship is required for this role.",
            rule="clearance_required")

    print("...but clearance-adjacent language that is NOT a requirement must pass")
    survives("EEO boilerplate naming citizenship as a protected class",
             "Product Manager",
             "We are an equal opportunity employer and consider all applicants "
             "without regard to race, religion, or citizenship status.")
    survives("trade clearing (Vaish's own Citi domain)", "Product Manager",
             "Own the post-trade clearing and settlement workflow for OTC "
             "derivatives, partnering with the clearing house.")
    survives("legal/compliance clearance, not a security clearance",
             "Product Manager",
             "Work with Legal to obtain marketing clearance for each release.")
    survives("security in the product sense", "Product Manager",
             "Partner with the security team on our API security roadmap.")
    # Verbatim shape of the Blue Origin ITAR paragraph, which the first version
    # of this rule wrongly rejected.
    survives("ITAR boilerplate that INCLUDES green-card holders", "Product Manager",
             "Export Control Regulations: Applicants for employment must be a "
             "U.S. citizen or national, U.S. permanent resident (i.e. current "
             "Green Card holder), or lawfully admitted into the U.S. as a refugee.")
    # "SCI" is Save the Children International's own acronym, used throughout
    # its JDs. Bare \bsci\b rejected a real NGO posting on 2026-08-17.
    survives("SCI as an ordinary org acronym, not TS/SCI", "Program Manager",
             "The project is implemented by a Save the Children (SCI)-led "
             "consortium. The role holder follows SCI Health and Safety policies.")
    rejects("...but TS/SCI still rejects", "Program Manager",
            "Requires an active TS/SCI.", rule="clearance_required")
    # ...but the softening applies only to the citizenship branch.
    rejects("named clearance still rejects even beside ITAR language",
            "Product Manager",
            "Applicants must be a U.S. citizen or permanent resident. An active "
            "Top Secret clearance is required.",
            rule="clearance_required")

    print("rule precedence: title checked before JD")
    rejects("principal + no-sponsorship reports title first",
            "Principal Product Manager", "Visa sponsorship is not available.",
            rule="seniority_title")

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
        return 1
    print("all hard-filter tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
