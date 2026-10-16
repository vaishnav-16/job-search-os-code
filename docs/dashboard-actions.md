# Dashboard action buttons (setup + how it actually flows)

The published dashboard has buttons that record a decision - mark a job
applied/rejected/skipped/interview/offer, or promote a digest/stretch job into
the auto-tailor lane. The page is a single static file with no server, so a
click cannot write the DB directly; it writes a note to `decisions/pending.json`
in git instead, and `pipeline/decisions.py` (invoked from `sync.py` step 3.2)
is what turns that note into a real status or lane change. This doc is the
setup steps and the honest timing, in the style of `docs/dashboard-hosting.md`.

## What a click actually does, end to end

1. **Click.** The button queues `{id, action, at}` in this browser's
   `localStorage`, under the published page's own origin - it is not sent
   anywhere yet. The row shows a "pending: <action> (awaiting sync)" badge
   with a cancel link.
2. **One debounced commit.** A 10-second timer coalesces further clicks, then
   the page PUTs the merged queue to `decisions/pending.json` on `main`
   through the GitHub contents API, authenticated with the fine-grained PAT
   stored in this browser (see below).
3. **CI rebuild shows pending.** `.github/workflows/dashboard.yml` watches
   `decisions/**`, so that push triggers a rebuild. The republished page reads
   the same `pending.json` back and repaints the badge for anyone who opens it
   - this step does not apply anything, it only makes the pending state
   visible again after a reload.
4. **Sync fold applies it.** `pipeline/sync.py` step 3.2 runs
   `pipeline/decisions.py` against the local clone of `main`: a mark calls
   `db.set_status` (and clears `queue/` + the score sidecar exactly as
   `/mark` does); a promote calls `db.set_lane(id, "auto-tailor", marker=...)`
   with a `promoted via dashboard <date>` note, but only once the row has
   actually been scored - a promote clicked before scoring lands stays
   pending and is picked up by a later sync once the score arrives.
5. **Cloud reacts at its next run.** A promoted job is now indistinguishable
   from any other auto-tailor row; `judge-queue` picks it up (sorted ahead of
   the rest of that day's batch - see `.claude/routines/judge-queue.md`) at
   its next scheduled run.

**Honest timing:** a promotion reaches the cloud at the first `sync.py` run
after the click, and is tailored at the first `judge-queue` run after that
sync - not at the moment of the click. Nothing here is instant, and nothing
here should be treated as instant when deciding whether a promotion "took."

## PAT setup

The buttons need a GitHub fine-grained personal access token, created once and
pasted into the page:

1. github.com -> Settings -> Developer settings -> Fine-grained tokens ->
   Generate new token.
2. Repository access: **only** `<owner>/<repo>` - not "all repos."
3. Permissions: **Contents: Read and write**. Nothing else.
4. Expiry: 30-90 days.
5. Open the published page, click the gear icon, paste the token, save. It is
   stored in that browser's `localStorage` and is sent only to
   `api.github.com`.

**Renewal is the same flow: generate a new token and paste it in again.**
There is no rotation reminder built in yet; a token that has expired makes
clicks queue locally and never push, with no error surfaced beyond a stalled
"pending" badge.

## Blast radius, stated plainly

This token is not a toy scope. Be deliberate about where it lives:

- **Contents: write** means the token can alter `pipeline/*.py` - the same
  code CI executes with the Vercel deployment secrets in scope. A token that
  leaks is a path to those secrets, not just to this repo's files.
- **Contents: read** means the token can read private profile files, contact details and every tailored resume in `applications/` - all private-repo content the dashboard
  itself is careful never to expose.

Given that:

- **Never store the token anywhere but the published page's gear panel.** The
  local `file://` build deliberately ships with no buttons and no token
  prompt at all - Chrome shares `localStorage` across every `file://`
  document on the machine, so a token typed into a local copy would be
  readable by any other local page, which is a strictly worse blast radius
  than the published page's single origin.
- **Revoke on any suspicion**, even a vague one. Regenerating a token is free;
  a live leaked token with repo write is not.
- **Treat a leak as a data breach**, not an inconvenience - the private repo holds personal data.

## Escape hatches

- **Cancel** works on any button's "pending" badge for as long as it says
  pending - it queues a `cancel` entry that overrides the original before the
  fold ever sees it, or removes it from `pending.json` if the fold has not run
  yet. Once step 3.2 has folded an entry, cancel no longer applies; the change
  is already in the DB.
- **`/mark`** remains the escape hatch for everything the buttons do not
  cover, including any transition with no button on the page - most notably
  un-skipping a job, which has no dashboard control at all - and for anyone
  who wants the change to land immediately in the local DB rather than
  waiting on a push and a sync.
