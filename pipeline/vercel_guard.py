"""Refuse to publish the dashboard anywhere the public can read it.

The dashboard lists every company, role, and score in Vaish's search and links
to his resumes. It is only safe to publish because Vercel Authentication sits
in front of it. That protection is NOT uniform on the Hobby plan, and the gap
is silent:

    Verified 2026-08-18 by deploying a canary page to production and fetching
    every resulting URL with no credentials.
      your-dashboard-<team>.vercel.app  -> Vercel login  (gated)
      your-dashboard-<hash>-<team>...   -> Vercel login  (gated)
      your-dashboard.vercel.app   -> HTTP 200, the page itself

    The short alias is a *project domain*, and the API refuses
    ssoProtection={"deploymentType": "all"} on Hobby with 428 "not available on
    your plan for production deployments", falling back to
    "all_except_custom_domains" — under which a project domain is a custom
    domain and goes out unprotected.

The fix is a configuration, not code: the project carries ZERO project domains,
which leaves only the gated team-scoped alias. Deleting the short alias sticks
across production deploys (verified by redeploying and re-fetching). But
nothing in Vercel warns you if one is ever added back, and the failure is
invisible from inside the workflow — the deploy succeeds either way. So the
invariant gets a hard stop on both sides of the deploy:

    python pipeline/vercel_guard.py --preflight    # before publishing anything
    python pipeline/vercel_guard.py --postflight   # after, prove it is gated

Preflight is the one that matters: it fails BEFORE the data is uploaded.
Postflight is the empirical backstop, because a config that looks right and a
URL that is actually gated are different claims.

Reads VERCEL_TOKEN, VERCEL_ORG_ID, VERCEL_PROJECT_ID from the environment.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dashboard import PUBLIC_SENTINEL  # noqa: E402

API = "https://api.vercel.com"
TIMEOUT = 30

# ssoProtection.deploymentType values that put a login in front of production.
# "all_except_custom_domains" is what Hobby settles on and is sufficient ONLY
# because this project holds no project domains — which the domain check below
# is what actually enforces. "only_preview" is the dangerous near-miss: valid,
# truthy, and production-public.
SSO_COVERS_PRODUCTION = frozenset({
    "all",
    "all_except_custom_domains",
    "prod_deployment_urls_and_all_previews",
})


def _env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        sys.exit(f"vercel_guard: {name} is not set")
    return val


def _json(raw: bytes) -> dict:
    try:
        return json.loads(raw or b"{}")
    except ValueError:
        return {"raw": raw[:500].decode("utf-8", "replace")}


def api(path: str, method: str = "GET", body=None,
        headers: dict | None = None) -> tuple[int, dict]:
    """Authenticated Vercel API call, scoped to the configured team.

    `body` is a dict (sent as JSON) or bytes (sent as is, for /v2/files).
    """
    sep = "&" if "?" in path else "?"
    hdrs = {"Authorization": f"Bearer {_env('VERCEL_TOKEN')}", **(headers or {})}
    data = None
    if isinstance(body, (bytes, bytearray)):
        data = bytes(body)
    elif body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(
        f"{API}{path}{sep}teamId={_env('VERCEL_ORG_ID')}",
        data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, _json(r.read())
    except urllib.error.HTTPError as e:
        return e.code, _json(e.read())


def fetch_anonymous(url: str) -> tuple[int, str]:
    """GET with no credentials at all — the whole point of the postflight check.

    A stranger with the URL is exactly this request, so it must carry no token,
    no cookie, and no redirect back into an authenticated session.
    """
    req = urllib.request.Request(url, headers={"User-Agent": "vercel-guard"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, r.read(600_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read(600_000).decode("utf-8", "replace")
    except urllib.error.URLError as e:
        return 0, f"unreachable: {e}"


def preflight() -> int:
    pid = _env("VERCEL_PROJECT_ID")
    fails: list[str] = []

    st, proj = api(f"/v9/projects/{pid}")
    if st != 200:
        sys.exit(f"vercel_guard: cannot read project ({st}): {json.dumps(proj)[:200]}")

    sso = proj.get("ssoProtection")
    print(f"  ssoProtection: {json.dumps(sso)}")
    if not sso:
        fails.append("Vercel Authentication is OFF for this project")
    elif sso.get("deploymentType") not in SSO_COVERS_PRODUCTION:
        # An allow-list, not a not-null check. "only_preview" is a perfectly
        # valid setting that leaves production wide open, and accepting any
        # truthy value would reproduce exactly the bug this file exists to
        # catch: a config that reads protected while a URL is not. Anything
        # unrecognised fails too — a new Vercel value is not assumed safe.
        fails.append(
            f"ssoProtection deploymentType is {sso.get('deploymentType')!r}, "
            f"which does not cover production (expected one of "
            f"{', '.join(sorted(SSO_COVERS_PRODUCTION))})")

    st, doms = api(f"/v9/projects/{pid}/domains")
    names = [d.get("name") for d in doms.get("domains", [])] if st == 200 else None
    if names is None:
        fails.append(f"cannot read project domains ({st})")
    else:
        print(f"  project domains: {names or '(none - correct)'}")
        if names:
            fails.append(
                "project has domain(s) " + ", ".join(names) +
                " - on Hobby these are served WITHOUT Vercel Authentication")

    if fails:
        print("::error title=Dashboard would be public::" + "; ".join(fails))
        print("\n  Refusing to publish. The dashboard lists every role, score, and")
        print("  resume link in the search; publishing it unprotected is a leak.")
        print("  Fix: remove every project domain (Vercel > Project > Settings >")
        print("  Domains), leaving only the team-scoped *.vercel.app alias, which")
        print("  Vercel Authentication does cover. Re-run to confirm.")
        return 1
    print("  preflight OK: only the gated team-scoped alias can serve this project")
    return 0


def production_urls() -> list[str]:
    """Every URL a production deploy is reachable at, per Vercel itself.

    Includes the alias list explicitly: the project's targets.production.alias
    field has been observed to drop the team-scoped alias, and that alias is
    precisely the URL a stranger would be handed, so it must always be probed.
    """
    pid = _env("VERCEL_PROJECT_ID")
    urls: list[str] = [a for a, _ in alias_targets()]
    st, proj = api(f"/v9/projects/{pid}")
    if st == 200:
        prod = (proj.get("targets") or {}).get("production") or {}
        urls += list(prod.get("alias") or [])
        if prod.get("url"):
            urls.append(prod["url"])
    st, deps = api(f"/v6/deployments?projectId={pid}&limit=3&target=production")
    if st == 200:
        urls += [d["url"] for d in deps.get("deployments", []) if d.get("url")]
    seen, out = set(), []
    for u in urls:
        u = u if u.startswith("http") else f"https://{u}"
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def production_state() -> tuple[str, str]:
    """(readyState, deployment id) of whatever the project calls production."""
    st, proj = api(f"/v9/projects/{_env('VERCEL_PROJECT_ID')}")
    if st != 200:
        return "UNKNOWN", ""
    prod = (proj.get("targets") or {}).get("production") or {}
    return (prod.get("readyState") or "UNKNOWN"), (prod.get("id") or "")


def alias_targets() -> list[tuple[str, str]]:
    """[(alias, deployment id)] for every alias on this project."""
    st, res = api(f"/v4/aliases?projectId={_env('VERCEL_PROJECT_ID')}&limit=20")
    if st != 200:
        return []
    out = []
    for a in res.get("aliases", []):
        dep = (a.get("deployment") or {}).get("id") or a.get("deploymentId") or ""
        if a.get("alias"):
            out.append((a["alias"], dep))
    return out


def state_reason(dep: str) -> str:
    """Vercel's own explanation for a non-READY deployment.

    Worth the extra call: the first version of this guard guessed "probably a
    rate limit" and was wrong — the real answer was sitting in
    readyStateReason ("Git author ... must have access to the team"), which
    named both the cause and the fix.
    """
    if not dep:
        return ""
    st, d = api(f"/v13/deployments/{dep}")
    if st != 200:
        return ""
    reason = (d.get("readyStateReason") or "").strip()
    code = ((d.get("seatBlock") or {}).get("blockCode") or "").strip()
    return f"{code}: {reason}" if code else reason


def check_serving() -> list[str]:
    """Assert production is actually alive, not merely not-leaking.

    Added 2026-08-18 after the gap showed up in practice: Vercel put four
    deployments in readyState BLOCKED (a burst rate-limit), the production
    alias started answering 404, and the leak check still passed — because
    "the page was not served to a stranger" is equally true of a dead page.
    A published dashboard nobody can open is a silent failure of exactly the
    kind the Layer 1 clock exists to prevent, so it fails the run too.
    """
    fails: list[str] = []
    state, dep = production_state()
    print(f"  production deployment: {dep or '(none)'} readyState={state}")
    # The alias is the URL Vaish actually opens, and it is a separate fact from
    # "the newest deployment is READY": an alias can stay pinned to an older
    # build while every deploy succeeds, which is a page that silently stops
    # updating. Seen 2026-08-18 after a deployment was promoted by hand.
    for alias, target in alias_targets():
        ok = (target == dep) if dep else False
        print(f"  alias {alias} -> {target or '(unassigned)'}{'' if ok else '  STALE'}")
        if not ok:
            fails.append(f"alias {alias} serves {target or 'nothing'}, not the current"
                         f" production deployment {dep} - the page is stale")
    if state != "READY":
        why = state_reason(dep)
        if why:
            print(f"  Vercel says: {why}")
        fails.append(f"the newest production deployment is {state}, not READY,"
                     " so the page being served is NOT the one just built"
                     + (f" [{why}]" if why else ""))
    return fails


def postflight() -> int:
    urls = production_urls()
    if not urls:
        sys.exit("vercel_guard: no production URLs found to check")
    dead = check_serving()
    leaked: list[str] = []
    primary = urls[0] if urls else ""
    for url in urls:
        status, body = fetch_anonymous(url)
        # Absence of the sentinel is the pass condition, not presence of a login
        # page: a 404, a 401, and Vercel's SSO interstitial are all fine, and
        # guessing which one Vercel returns would make this brittle.
        exposed = PUBLIC_SENTINEL in body
        print(f"  [{'LEAK' if exposed else ' ok '}] {status} {url}")
        if exposed:
            leaked.append(url)
        # The primary alias is the URL Vaish actually opens. A 404 there means
        # the page is unreachable, which "no sentinel" would happily call a
        # pass - a dead page leaks nothing.
        if url == primary and status == 404:
            dead.append(f"{url} returns 404: nothing is aliased to a live build")
    if leaked:
        print("::error title=Dashboard is publicly readable::" + ", ".join(leaked))
        print("\n  These URLs served the dashboard to a request carrying no")
        print("  credentials. Treat as a live data exposure: delete the project")
        print("  domain behind them, then redeploy.")
        return 1
    if dead:
        print("::error title=Dashboard is published but not serving::" + "; ".join(dead))
        return 1
    print(f"  postflight OK: {len(urls)} production URL(s), none served the page"
          " anonymously, production is READY")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--preflight", action="store_true",
                    help="check the project config BEFORE uploading anything")
    ap.add_argument("--postflight", action="store_true",
                    help="prove the deployed URLs are not anonymously readable")
    args = ap.parse_args()
    if not (args.preflight or args.postflight):
        ap.error("pick --preflight or --postflight")
    rc = 0
    if args.preflight:
        print("[vercel_guard] preflight")
        rc |= preflight()
    if args.postflight:
        print("[vercel_guard] postflight")
        rc |= postflight()
    return rc


if __name__ == "__main__":
    sys.exit(main())
