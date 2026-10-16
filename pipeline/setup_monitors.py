"""Create Firecrawl monitors for the custom-board companies in monitors.yaml.

Run once, after the Firecrawl API key is configured:
    firecrawl config            (or export FIRECRAWL_API_KEY=fc-...)
    python pipeline/setup_monitors.py

Idempotent-ish: prints the existing monitor list first so you can see duplicates
before creating more. Notifies by email only; nothing is written to queue/.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

# Firecrawl's student tier allows 3 requests/minute. Pace creates rather than
# letting them fail, and skip names that already exist so re-runs are safe.
RATE_LIMIT_SLEEP = 25

CONFIG = Path(__file__).resolve().parent / "monitors.yaml"
ENV_FILE = Path(__file__).resolve().parent / ".env"


def load_env_key() -> str:
    """Read FIRECRAWL_API_KEY from pipeline/.env, falling back to the process env.

    Deliberately a 6-line KEY=VALUE parser rather than a python-dotenv dependency.
    The key is never printed. pipeline/.env must stay gitignored.
    """
    if os.environ.get("FIRECRAWL_API_KEY"):
        return os.environ["FIRECRAWL_API_KEY"]
    if not ENV_FILE.exists():
        return ""
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k.strip() == "FIRECRAWL_API_KEY":
            return v.strip().strip("\"'")
    return ""


def firecrawl_bin() -> str | None:
    """npm installs both `firecrawl` (sh shim) and `firecrawl.cmd` on Windows;
    subprocess can only exec the .cmd, so prefer it."""
    return shutil.which("firecrawl.cmd") or shutil.which("firecrawl")


def run(args: list[str], key: str = "") -> tuple[int, str]:
    env = dict(os.environ)
    if key:
        env["FIRECRAWL_API_KEY"] = key
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=180, env=env)
    except OSError as e:
        return 1, f"{type(e).__name__}: {e}"
    out = (proc.stdout + proc.stderr).strip()
    if key:  # never let the key reach stdout via an error echo
        out = out.replace(key, "***")
    return proc.returncode, out


def main() -> int:
    fc = firecrawl_bin()
    if not fc:
        print("firecrawl CLI not found on PATH. Install it, then re-run.")
        return 1

    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    email, interval = cfg["notify_email"], cfg.get("interval", "3h")
    key = load_env_key()
    print(f"api key: {'loaded from pipeline/.env' if key else 'NOT FOUND'}")

    code, out = run([fc, "monitor", "list"], key)
    if code != 0:
        print("Firecrawl is not authenticated yet:\n  " + out.splitlines()[0])
        print('\nSet the key in pipeline/.env as FIRECRAWL_API_KEY=... or run '
              '"firecrawl config", then re-run this script.')
        return 1
    try:
        existing = {m.get("name", "") for m in json.loads(out).get("data", [])}
    except (json.JSONDecodeError, AttributeError):
        existing = set()
    print(f"existing monitors: {len(existing)}" +
          (" -> " + ", ".join(sorted(existing)) if existing else "") + "\n")

    todo = [m for m in cfg["monitors"] if f"{m['company']} PM roles" not in existing]
    for m in existing.intersection({f"{x['company']} PM roles" for x in cfg["monitors"]}):
        print(f"  {m.split(' PM roles')[0]:<12} already exists, skipping")

    for i, m in enumerate(todo):
        if i:
            time.sleep(RATE_LIMIT_SLEEP)  # stay under 3 req/min
        company, url = m["company"], m["url"]
        # CLI contract (verified 2026-08-11): --page for a single URL, --schedule
        # for natural-language cadence, --email for recipients. There is no
        # --interval / --notify-email.
        cmd = [fc, "monitor", "create",
               "--name", f"{company} PM roles",
               "--page", url,
               "--schedule", interval,
               "--timezone", "America/New_York",
               "--email", email]
        if cfg.get("goal"):
            cmd += ["--goal", cfg["goal"].strip()]
        code, out = run(cmd, key)
        # The rate limiter reports exactly how long to wait; honour it once
        # instead of failing and making the user re-run.
        if code != 0 and "rate limit" in out.lower():
            wait = re.search(r"retry after (\d+)", out, re.I)
            delay = int(wait.group(1)) + 3 if wait else 60
            print(f"  {company:<12} rate-limited, waiting {delay}s")
            time.sleep(delay)
            code, out = run(cmd, key)
        status = "created" if code == 0 else f"FAILED ({out.splitlines()[0] if out else code})"
        print(f"  {company:<12} {status}")

    print("\nDone. Notifications go to " + email + ". These monitors only tell you a "
          "career page changed; Layer 2 still confirms and dedupes through the queue.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
