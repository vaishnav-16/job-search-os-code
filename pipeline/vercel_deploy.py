"""Deploy the one-file dashboard to Vercel production through the REST API.

    python pipeline/vercel_deploy.py publish/index.html

W7 (2026-10-13). This replaced `npm i -g vercel@latest` + `vercel deploy` +
`vercel alias set` in .github/actions/publish-dashboard. The npm install alone
was 25s of every detect and fold run, and it is what pushed a ~50s fold over a
billed minute (GitHub bills whole minutes per job). The CLI also failed
silently twice: its output went into `url=$(...)` and the log kept nothing.
Here every non-2xx prints Vercel's own status and error body.

Steps (checked against Vercel's REST docs, 2026-10-13):
  1. POST /v2/files               the page bytes, keyed by their sha1
  2. POST /v13/deployments        target production, the file by sha
  3. GET  /v13/deployments/{id}   until readyState is READY (ERROR stops)
  4. POST /v2/deployments/{id}/aliases   PROD_ALIAS (400 until READY)

No git metadata is sent, so the 2026-08-18 TEAM_ACCESS_REQUIRED block (the CLI
attaching the commit author when run inside the work tree) cannot happen.
vercel_guard.py --preflight / --postflight still run around this step.

Reads VERCEL_TOKEN, VERCEL_ORG_ID, VERCEL_PROJECT_ID and PROD_ALIAS.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vercel_guard import _env, api  # noqa: E402

READY_TIMEOUT = 120
POLL_SECONDS = 2


def fail(what: str, status: int, body: dict) -> int:
    err = body.get("error") if isinstance(body.get("error"), dict) else body
    print(f"::error title=Vercel deploy failed::{what}: HTTP {status} {err}")
    return 1


def deploy(page: Path) -> int:
    data = page.read_bytes()
    sha = hashlib.sha1(data).hexdigest()
    pid, alias = _env("VERCEL_PROJECT_ID"), _env("PROD_ALIAS")

    st, body = api("/v2/files", "POST", data, headers={
        "Content-Type": "application/octet-stream",
        "x-vercel-digest": sha,
        "Content-Length": str(len(data))})
    if st not in (200, 201):
        return fail("upload", st, body)
    print(f"uploaded {page.name}: {len(data):,} bytes, sha1 {sha[:12]}")

    st, proj = api(f"/v9/projects/{pid}")
    if st != 200 or not proj.get("name"):
        return fail("read project", st, proj)

    st, dep = api("/v13/deployments?skipAutoDetectionConfirmation=1", "POST", {
        "name": proj["name"],
        "project": pid,
        "target": "production",
        "projectSettings": {"framework": None},
        "files": [{"file": "index.html", "sha": sha, "size": len(data)}],
    })
    if st not in (200, 201) or not dep.get("id"):
        return fail("create deployment", st, dep)
    dep_id, url = dep["id"], dep.get("url", "")
    print(f"deployment {dep_id} https://{url}")

    state, waited = dep.get("readyState", ""), 0
    while state != "READY":
        if state in ("ERROR", "CANCELED"):
            return fail(f"deployment {dep_id} ended {state}", st, dep)
        if waited >= READY_TIMEOUT:
            return fail(f"deployment {dep_id} not READY after {READY_TIMEOUT}s "
                        f"(last {state or 'unknown'})", st, dep)
        time.sleep(POLL_SECONDS)
        waited += POLL_SECONDS
        st, dep = api(f"/v13/deployments/{dep_id}")
        if st != 200:
            return fail(f"read deployment {dep_id}", st, dep)
        state = dep.get("readyState", "")
    print(f"READY after ~{waited}s")

    st, res = api(f"/v2/deployments/{dep_id}/aliases", "POST", {"alias": alias})
    if st != 200:
        return fail(f"alias {alias}", st, res)
    print(f"aliased: {alias}")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(f"### Dashboard published: https://{url}\n")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("page", type=Path, help="the built dashboard HTML")
    args = ap.parse_args()
    if not args.page.is_file() or not args.page.stat().st_size:
        print(f"::error::{args.page} is missing or empty")
        return 1
    return deploy(args.page)


if __name__ == "__main__":
    sys.exit(main())
