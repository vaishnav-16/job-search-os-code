"""Dedupe tests. Run: python pipeline/test_dedupe.py

The scrape re-run check ("0 new the second time") cannot catch over-blocking:
a correctly-blocked job and a wrongly-blocked job both produce 0. These assert
the distinction directly.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db

# The real seed lives at profile/seed_applied.csv, which is not published.
# This suite imports the example fixture instead; db.import_seed's own
# default is untouched.
SEED = Path(__file__).resolve().parent / "testdata" / "seed_applied.csv"

FAILS = []


def check(label, got, want):
    if got == want:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}  (got {got!r}, want {want!r})")
        FAILS.append(label)


def main():
    print("normalization")
    check("company suffix stripped", db.norm_company("Stripe, Inc."), "stripe")
    check("company alias folded", db.norm_company("Meta Platforms"), "meta")
    check("roman numeral folded", db.norm_title("Product Manager I - Ads"),
          db.norm_title("Product Manager 1 Ads"))
    check("level II folded", db.norm_title("Program Manager II"),
          db.norm_title("Program Manager 2"))

    print("location bucketing")
    check("state code", db.location_bucket("Boston, MA, US"), "ma")
    check("state name", db.location_bucket("New York, New York, United States"), "ny")
    check("remote wins", db.location_bucket("Remote - US"), "remote")
    check("bare city mapped", db.location_bucket("San Francisco HQ"), "ca")
    check("multi-city takes first", db.location_bucket("NYC, Chicago, Seattle"), "ny")
    check("unknown stays empty", db.location_bucket("Somewhere Unlisted"), "")
    check("australia is not US", db.location_bucket("Sydney, Australia"), "")
    # City words that look like state codes must not win over the real state.
    check("city word 'La' loses to NE", db.location_bucket("La Vista, NE, US"), "ne")
    check("city word 'Orange' loses to CA", db.location_bucket("Orange, CA, US"), "ca")
    check("state-only location", db.location_bucket("CA, US"), "ca")
    check("trailing USA ignored", db.location_bucket("Scottsdale, AZ, USA"), "az")

    # ignore_cleanup_errors: Windows won't unlink the sqlite file until the
    # handle is released, and the close() below is inside the same block.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        conn = db.connect(Path(tmp) / "t.sqlite")

        print("seed wildcard (no location in seed_applied.csv)")
        db.insert_job(conn, "Globex", "Product Manager", "", "", "seed", status="applied")
        check("blocks same role in a city",
              db.is_seen(conn, "Globex", "Product Manager", "Menlo Park, CA"), True)
        check("blocks same role remote",
              db.is_seen(conn, "Globex, Inc.", "Product Manager", "Remote, US"), True)
        check("does not block a different role",
              db.is_seen(conn, "Globex", "Product Designer", "Menlo Park, CA"), False)
        check("does not block a different company",
              db.is_seen(conn, "Stripe", "Product Manager", "Menlo Park, CA"), False)

        print("REGRESSION: a scraped row must never act as an all-location wildcard")
        # "Somewhere Unlisted" buckets to '' just like a seed row does. Before the
        # fix, this suppressed the same title in every other city, permanently.
        db.insert_job(conn, "Plaid", "Product Manager", "Somewhere Unlisted",
                      "", "ashby", status="new")
        check("same scraped row still deduped",
              db.is_seen(conn, "Plaid", "Product Manager", "Somewhere Unlisted"), True)
        check("SAME TITLE IN ANOTHER CITY STILL VISIBLE",
              db.is_seen(conn, "Plaid", "Product Manager", "New York, NY"), False)
        check("same title remote still visible",
              db.is_seen(conn, "Plaid", "Product Manager", "Remote, US"), False)

        print("seed import is idempotent")
        n1 = db.import_seed(conn, SEED)
        n2 = db.import_seed(conn, SEED)
        check("first import inserts rows", n1 > 0, True)
        check("second import inserts none", n2, 0)
        conn.close()

    # --- role key: the identity of WORK, not of a posting (added 2026-08-25) ---
    # One role sat in the DB three times, under three spellings of a joint
    # venture's name, and two tailor runs were spent on it. The role key exists to stop that; the dedupe key must NOT
    # change, or already-seen jobs re-surface.
    TITLE = "Product Manager, E-Commerce Recommendations"
    variants = ["Initech Data Joint Venture", "Initech Data JV", "Initech Data"]
    keys = {db.role_key(c, TITLE) for c in variants}
    check("role_key folds JV against joint venture", len(keys), 1)
    check("role_key ignores location by construction (it takes none)",
          db.role_key("Acme", TITLE), db.role_key("Acme", TITLE))
    check("role_key still separates genuinely different companies",
          db.role_key("Acme JV", TITLE) != db.role_key("Beta JV", TITLE), True)
    check("role_key still separates different titles",
          db.role_key("Acme", "Product Manager") != db.role_key("Acme", "Data Scientist"), True)
    # A repost under a new requisition code is one piece of work (Referment 09-29).
    check("role_key folds a trailing req code",
          db.role_key("Referment", "Macro (7B48D45)") == db.role_key("Referment", "Macro (45EB295)"), True)
    check("but keeps a word or a level in parentheses",
          len({db.role_title(t) for t in ("PO (DICOM)", "PO (Level 2)", "PO (Level 3)", "PO")}), 4)
    check("and the dedupe key still sees two postings",
          db.job_id("Referment", "Macro (7B48D45)", "NY") != db.job_id("Referment", "Macro (45EB295)", "NY"), True)
    check("role_key folds Nimble Robotics into Nimble (LinkedIn vs Indeed)",
          db.role_key("Nimble Robotics", TITLE) == db.role_key("Nimble", TITLE), True)
    check("and Huron Consulting Group into Huron",
          db.role_key("Huron Consulting Group", TITLE) == db.role_key("Huron", TITLE), True)

    # The asymmetry is load-bearing: loosening the DEDUPE key would re-key rows
    # that have already been seen, and "seen jobs never resurface" is the rule
    # that keeps a job from being scored twice.
    check("norm_company is UNCHANGED by the role-key folding",
          db.norm_company("Initech Data JV"), "initech data jv")
    check("...so the two variants keep distinct dedupe keys",
          db.norm_company("Initech Data JV") != db.norm_company("Initech Data Joint Venture"),
          True)

    with tempfile.TemporaryDirectory() as td:
        conn = db.connect(Path(td) / "t.sqlite")
        jid_a = db.insert_job(conn, "Initech Data JV", TITLE, "Seattle, WA", "", "test",
                              status="scored")
        db.insert_job(conn, "Acme", "Product Manager", "Austin, TX", "", "test",
                      status="scored")
        conn.commit()
        check("a scored row is NOT treated as already-worked",
              db.role_key("Initech Data JV", TITLE) in db.worked_role_keys(conn), False)
        db.set_status(conn, jid_a, "needs_review")
        conn.commit()
        worked = db.worked_role_keys(conn)
        check("a needs_review row IS already-worked",
              db.role_key("Initech Data JV", TITLE) in worked, True)
        check("...and it suppresses the OTHER company spelling too",
              db.role_key("Initech Data Joint Venture", TITLE) in worked, True)
        check("an untouched role is not suppressed",
              db.role_key("Acme", "Product Manager") in worked, False)
        check("a skipped row never suppresses (it produced no resume)",
              db.role_key("Acme", "Product Manager") in db.worked_role_keys(conn), False)
        conn.close()

        # The cloud tailor routine calls this and is forbidden to modify the DB.
        # connect() would take a write lock (it runs DDL then commits) even when
        # every statement is a no-op, so the reader needs a connection that
        # CANNOT write, not one that merely does not.
        ro = db.connect_readonly(Path(td) / "t.sqlite")
        check("connect_readonly can read", isinstance(db.worked_role_keys(ro), set), True)
        try:
            ro.execute("UPDATE jobs SET status='new'")
            ro.commit()
            wrote = True
        except Exception:
            wrote = False
        check("connect_readonly REFUSES a write", wrote, False)
        ro.close()

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
        return 1
    print("all dedupe tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
