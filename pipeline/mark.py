"""Record a status change Vaish made by hand: /mark <id> <status>.

    python pipeline/mark.py <id or 8+ char prefix> <applied|skipped|rejected|interview|offer>

Writes the same entry a dashboard button writes (decisions/pending.json) and
pushes it. Since W7 (2026-10-13) the push starts no workflow: the next fold
applies it (a routine push, or the one detect.yml dispatches when decisions
are waiting), within about 2h. worklist.py keeps a pending skip untailored. The laptop never writes jobs.sqlite (see
localgit.py), so there is no binary to race.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import db  # noqa: E402
import decisions  # noqa: E402
import localgit  # noqa: E402


def resolve(conn, ref: str) -> tuple[str, str, str, str]:
    rows = conn.execute("SELECT id, company, title, status FROM jobs WHERE id LIKE ?",
                        (ref.lower() + "%",)).fetchall()
    if len(ref) < 8 or len(rows) != 1:
        raise SystemExit(f"'{ref}' matches {len(rows)} job(s); give the full id or an "
                         "unambiguous prefix of 8+ characters")
    return rows[0]


def record(jid: str, action: str, at: str) -> None:
    """Append (replacing any earlier entry for this id, as the browser does)."""
    entries = []
    if decisions.PENDING_PATH.exists():
        parsed = json.loads(decisions.PENDING_PATH.read_text(encoding="utf-8") or "[]")
        entries = [e for e in parsed if not (isinstance(e, dict) and e.get("id") == jid)]
    entries.append({"id": jid, "action": action, "at": at, "via": "mark"})
    decisions.write_pending(entries)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("id")
    ap.add_argument("status", choices=decisions.MARK_ACTIONS)
    ap.add_argument("--no-push", action="store_true", help="record locally only (tests)")
    args = ap.parse_args(argv)

    try:
        if not args.no_push:
            localgit.begin()
        conn = db.connect_readonly()
        jid, company, title, current = resolve(conn, args.id)
        conn.close()
        at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        record(jid, args.status, at)
        if not args.no_push:
            localgit.publish([localgit.PENDING], f"mark: {company} {jid[:8]} {args.status}",
                             redo=lambda: record(jid, args.status, at))
    except localgit.LocalGitError as e:
        print(f"STOPPED: {e}", file=sys.stderr)
        return 1
    print(f"{company} | {title}: {current} -> {args.status} "
          f"({'recorded locally' if args.no_push else 'pushed; the next cloud fold applies it, within about 2h'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
