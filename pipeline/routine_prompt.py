"""Read a cloud Routine's prompt out of its versioned copy in `.claude/routines/`.

Shared by `test_routine_prompts.py` and `routine_parity.py` on purpose: those two
answer different questions about the same string ("does it still carry its
load-bearing clauses" and "does it equal what the cloud is actually running"),
and if each carved the block out for itself they could disagree about where the
prompt starts and ends while both reported green.

Two extractions, deliberately not one:

`prompt_section` is heading-to-EOF and is what the clause checks have always
used. It is kept EXACTLY as it was so 33 passing assertions do not change
meaning; it is a superset of the prompt (it includes the `## Prompt` heading and
the fence markers), which is harmless for substring tests.

`prompt_body` is the exact string to send to `RemoteTrigger` — the contents of
the outer ```text fence and nothing else. It needs a fence DEPTH counter because
the prompt embeds ```bash and ```json blocks of its own: `tailor-queue` holds 6
nested fences inside its outer one and `score-queue` holds 4. The obvious
implementation — scan forward to the first closing ``` — returns **2802 of
20632 characters** on `tailor-queue.md`, silently, and a caller who trusted it
would send a prompt truncated in the middle of STEP 0's environment prep. That
defect is the reason this module exists rather than a snippet at each call site.

Read-only. No network: the live prompt is not in the repo, and nothing here can
reach it. `routine_parity.py` is what compares the two, given a fetched copy.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ROUTINES = ROOT / ".claude" / "routines"

HEADING = "## Prompt"
OUTER_FENCE = "```text"


def routine_path(name: str) -> Path:
    return ROUTINES / f"{name}.md"


def _lines_from_heading(name: str) -> list[str]:
    """Every line from the `## Prompt` heading onward.

    Anchored on a line that IS the heading. Matching the substring anywhere
    truncated both files during the 2026-08-24 reconciliation, because the
    warning prose above the heading mentions `## Prompt` by name.
    """
    lines = routine_path(name).read_text(encoding="utf-8").split("\n")
    idx = [i for i, ln in enumerate(lines) if ln.strip() == HEADING]
    if len(idx) != 1:
        raise AssertionError(
            f"{name}: expected one '{HEADING}' heading, got {len(idx)}")
    return lines[idx[0]:]


def prompt_section(name: str) -> str:
    """The text under the `## Prompt` heading line, fenced block included."""
    return "\n".join(_lines_from_heading(name))


def prompt_body(name: str) -> str:
    """The exact prompt string the live Routine should be running.

    The contents of the outer ```text fence, fence-depth aware so the prompt's
    own ```bash and ```json blocks pass through as content rather than ending
    the block. Raises rather than returning a truncated prompt: a short read
    here becomes a truncated cloud prompt, and the failure would be silent.
    """
    rest = _lines_from_heading(name)
    opens = [i for i, ln in enumerate(rest) if ln.startswith(OUTER_FENCE)]
    if not opens:
        raise AssertionError(
            f"{name}: no '{OUTER_FENCE}' fence under {HEADING}; the prompt block "
            "has moved or changed shape")
    start = opens[0]

    depth = 1
    for i in range(start + 1, len(rest)):
        line = rest[i]
        if not line.startswith("```"):
            continue
        # A bare ``` closes; ```<lang> opens. Nothing in either routine file
        # writes a language tag on a closing fence, and if that ever changes
        # the depth goes wrong LOUDLY (unbalanced) rather than quietly short.
        depth += -1 if line.strip() == "```" else 1
        if depth == 0:
            return "\n".join(rest[start + 1:i]).strip()

    raise AssertionError(
        f"{name}: the '{OUTER_FENCE}' block opened at line {start} is never "
        f"closed (depth {depth} at EOF); refusing to return a truncated prompt")


def names() -> list[str]:
    """Every routine that has a versioned copy."""
    # The notes file (<name>.notes.md) is a routine's memory, not a prompt.
    return sorted(p.stem for p in ROUTINES.glob("*.md") if not p.stem.endswith(".notes"))


def snapshot_path(name: str) -> Path:
    """The last VERIFIED live prompt, committed beside its markdown copy.

    Written only from a `RemoteTrigger get`, never from the markdown file: a
    snapshot generated from the file would assert that the file equals itself.
    """
    return ROUTINES / f"{name}.live.txt"
