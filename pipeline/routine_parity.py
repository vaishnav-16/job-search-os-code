"""Compare a cloud Routine's LIVE prompt against its versioned copy.

    python pipeline/routine_parity.py <name> --emit        # the exact body to send
    python pipeline/routine_parity.py <name> <live.txt>    # diff; exit 1 on drift
    python pipeline/routine_parity.py <name> --snapshot    # diff vs the committed snapshot

Why this exists. The live prompt is not in the repo and no test can reach it, so
until 2026-08-30 the only thing holding the two together was a session
remembering to send both halves. That discipline has failed once already: on
2026-08-24 both routine files had drifted into paraphrases of the live prompts —
`tailor-queue.md` had lost the whole environment-prep step — and nothing noticed
for weeks, while the live prompt was telling the routine "if it disagrees with
this prompt, follow that file".

The second failure mode is newer and is what prompted this file. A
`RemoteTrigger update` body has to be typed out in full, ~20k characters for
`tailor-queue`, because the tool takes an inline string and cannot read a path.
A transcription slip anywhere in that is invisible: the run still starts, and
everything below STEP 1 is masked by "follow that file". STEP 0 is not masked —
it runs before the routine reads anything — so a typo there fails a render.

Usage is deliberately two-step rather than one. This script cannot fetch: the
routine API is reachable only from a Claude session's `RemoteTrigger` tool, not
from a shell. So a session fetches the prompt, saves it, and points this at it.
`--emit` closes the loop from the other side: it prints the exact bytes to send,
so the NEXT update is a copy from a known-good source instead of a re-read of a
JSON blob.

Read-only, no network, writes nothing.
"""

from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import routine_prompt  # noqa: E402


def compare(name: str, live: str, label: str, context: int = 2) -> int:
    """Print a verdict and, on drift, a unified diff. Returns an exit code."""
    body = routine_prompt.prompt_body(name)
    print(f"{name}")
    print(f"  file  ({routine_prompt.routine_path(name).name}): {len(body):>6} chars, "
          f"{body.count(chr(10)) + 1} lines")
    print(f"  live  ({label}): {len(live):>6} chars, {live.count(chr(10)) + 1} lines")

    if body == live:
        print("  IDENTICAL")
        return 0

    # Strip-insensitive first: trailing whitespace is a copy artefact, not drift,
    # and reporting it as a mismatch would train the reader to ignore this tool.
    if body.strip() == live.strip():
        print("  IDENTICAL apart from leading/trailing whitespace")
        return 0

    print("  DRIFT")
    diff = difflib.unified_diff(
        body.split("\n"), live.split("\n"),
        fromfile=f"{name}.md (file)", tofile=f"{name} ({label})",
        lineterm="", n=context)
    shown = 0
    for line in diff:
        # Long prompt lines are one paragraph each; truncate so a one-word
        # difference does not print two 2000-character lines.
        print("  " + (line if len(line) <= 200 else line[:200] + " ..."))
        shown += 1
        if shown >= 80:
            print("  ... diff truncated at 80 lines")
            break
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("name", help=f"routine name, one of: {', '.join(routine_prompt.names())}")
    ap.add_argument("live", nargs="?",
                    help="file holding the live prompt (from RemoteTrigger get, "
                         "derived_state.prompt)")
    ap.add_argument("--emit", action="store_true",
                    help="print the exact prompt body to send, and exit")
    ap.add_argument("--snapshot", action="store_true",
                    help="compare against the committed .live.txt snapshot")
    args = ap.parse_args()

    if args.emit:
        sys.stdout.write(routine_prompt.prompt_body(args.name) + "\n")
        return 0

    if args.snapshot:
        path = routine_prompt.snapshot_path(args.name)
        if not path.is_file():
            print(f"ERROR  no snapshot at {path}.")
            print("       Fetch the live prompt with RemoteTrigger get and save it")
            print("       there. NEVER generate it from the markdown file: that")
            print("       would only prove the file equals itself.")
            return 1
        return compare(args.name, path.read_text(encoding="utf-8"), "snapshot")

    if not args.live:
        ap.error("give a live prompt file, or --snapshot, or --emit")
    return compare(args.name, Path(args.live).read_text(encoding="utf-8"), "live")


if __name__ == "__main__":
    sys.exit(main())
