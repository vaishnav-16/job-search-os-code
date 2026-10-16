"""Standalone ban-list gate: lint generated application files on demand.

The PreToolUse hook (.claude/hooks/lint_generated.py) is a hard stop only
inside a Claude Code session — it reads a tool payload from stdin and blocks
the write. Anywhere else (a claude.ai project chat, a manual edit, CI) the
same rules have no enforcement point at all, which is how a banned string
reaches a shipped resume.

This wraps the hook's RULES + DATE_RANGE + EXEMPT and points them at files on
disk instead. It imports them rather than restating them: voice.md is the
source of truth for the ban list, the hook is its enforcement point, and a
second copy of the regexes would drift from both.

    python pipeline/lint_files.py applications/<folder>/*.md
    python pipeline/lint_files.py --all applications/<folder>

Scope: this lints GENERATED files under applications/ - resume copy, score.md,
screening-draft.md. Pointed anywhere else it will happily report rules that do
not apply there (repo docs and JDs legitimately carry em-dashes and ban-list
words), so a violation outside applications/ is a misaimed run, not a defect.

Exit 0 = clean, 2 = violations (same code the hook returns, so a caller can
treat them identically). Non-linted files (jd.md, review.md, anything not
.md/.txt/.html) are reported as skipped, never as passing — "clean" and "not
checked" are different answers and collapsing them is how a gate lies.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOOK_PATH = HERE.parent / ".claude" / "hooks" / "lint_generated.py"


def _hook():
    spec = importlib.util.spec_from_file_location("lint_generated", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def lint_text(text: str, rules, date_range) -> list[str]:
    masked = date_range.sub("", text)
    out = []
    for why, rx in rules:
        hits = sorted({m.group(0) for m in rx.finditer(masked)})
        if hits:
            out.append(f"  - {why}: {', '.join(hits[:5])}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Lint generated files against the ban list.")
    ap.add_argument("paths", nargs="+", help="files, or folders with --all")
    ap.add_argument("--all", action="store_true",
                    help="treat each path as a folder and lint every lintable file in it")
    args = ap.parse_args()

    if not HOOK_PATH.exists():
        print(f"lint_files: hook not found at {HOOK_PATH} - the ban list has no source", file=sys.stderr)
        return 2
    hook = _hook()

    targets: list[Path] = []
    for p in args.paths:
        path = Path(p)
        if args.all or path.is_dir():
            targets.extend(sorted(f for f in path.rglob("*") if f.is_file()))
        else:
            targets.append(path)

    failed, checked, skipped = 0, 0, []
    for f in targets:
        if not f.exists():
            print(f"lint_files: missing {f}", file=sys.stderr)
            failed += 1
            continue
        name = f.name.lower()
        if name in hook.EXEMPT or not name.endswith((".md", ".txt", ".html")):
            skipped.append(f)
            continue
        checked += 1
        violations = lint_text(f.read_text(encoding="utf-8", errors="replace"),
                               hook.RULES, hook.DATE_RANGE)
        if violations:
            failed += 1
            print(f"BLOCKED {f}:")
            print("\n".join(violations))

    for f in skipped:
        print(f"skipped (not linted) {f}")
    print(f"lint_files: {checked} checked, {len(skipped)} skipped, {failed} with violations")
    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
