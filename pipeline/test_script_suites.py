"""Bridge the script-style suites into pytest.

Most suites in this directory are hand-rolled scripts: a `main() -> int` that
prints `ok`/`FAIL` lines and returns non-zero on failure. They are meant to be
run as `python pipeline/test_age.py`. Five of them ALSO expose `test_*`
functions, so pytest collects those directly. The other twelve expose no
`test_*` function at all, which means a bare `python -m pytest pipeline` used to
collect nothing from them and still report green. This file closes that gap.

Each suite runs in its own subprocess, which is how they are designed to run:
they monkeypatch module globals (`sync.ROOT`, `ingest_scores.SCORES_DIR`, and so
on) and share `PASS`/`FAIL` counters at module scope, so importing twelve of
them into one interpreter would have them stepping on each other.

A suite that cannot run at all in this public copy (it needs a private file that
is not published) prints a single `skip  ...` line and exits 0. That is reported
here as a real pytest skip rather than a pass, so the summary line never claims
coverage the run did not have.
"""

import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent

# The twelve suites that expose only `main()`. The five left out
# (test_dashboard, test_decisions, test_outcomes, test_screening_draft,
# test_sync_consistency) expose `test_*` functions that pytest already
# collects; running their `main()` here as well would execute every check a
# second time against the same module-level counters, which is exactly what
# test_decisions.py's `test_zz_no_failures` sentinel exists to catch.
SCRIPT_SUITES = [
    "test_age",
    "test_ats_score",
    "test_dedupe",
    "test_docx_fidelity",
    "test_filter",
    "test_ingest",
    "test_parse_back",
    "test_preflight",
    "test_routine_prompts",
    "test_schedule",
    "test_scrape_sources",
    "test_sync",
]


@pytest.mark.parametrize("suite", SCRIPT_SUITES)
def test_script_suite(suite):
    r = subprocess.run([sys.executable, str(HERE / f"{suite}.py")],
                       capture_output=True, text=True, timeout=900)
    out = (r.stdout or "") + (r.stderr or "")
    body = out.strip()
    # Whole-suite skip: the script's only output is its one-line reason.
    if r.returncode == 0 and body.startswith("skip  ") and "\n" not in body:
        pytest.skip(body[len("skip  "):])
    assert r.returncode == 0, f"{suite} failed:\n{out[-4000:]}"
