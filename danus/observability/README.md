# Dashboard: live activity and research records

The default page is now a **read-only live activity view**. It uses the records
Danus already writes; it does not change workers, the orchestrator, assignments,
models, verification, round scheduling, or stopping behavior.

## What the operator sees

Worker cards show the recorded model/effort, state, PID, round, last exit code,
and separate timestamps for the status file and latest round log. Select a
worker to inspect its current `TASK.md` and compare a selected round's output
with the previous recorded round. The latest 12 rounds appear in the selector.
The page follows the latest round by default; selecting an older round pins the
view. Auto-refresh updates the **view only**, every three seconds while visible.

The orchestrator transcript and the `master_guidance`, `elaboration`, and
`verification` files have their own write timestamps. Missing or unreadable
sources are explicitly unavailable, never silently treated as healthy.

**Interpretation:** recorded process state is not a health probe; file activity
is not mathematical progress; absence of a new fact is not evidence of a loop.
The dashboard does not diagnose looping, generate model summaries, or stop
anything. The operator decides whether intervention is needed. `last_fact_id`
from a scraped round log is deliberately not presented as a new accepted fact.
The browser fetch timestamp is labelled separately from source timestamps.

The existing fact graph, overview, and global-memory views remain available
through **Fact graph & global memory**, at `/static/results.html`. Their original
APIs and client assets are retained. The old overview's timestamp is its fetch
time, not a live activity timestamp.

## Run and connect

Use the existing dashboard command on the machine holding the project records:

```sh
python -m danus.observability --project /absolute/path/to/project --port 8099
```

The dashboard still defaults to `127.0.0.1:8099`. Access it from the Mac through
the existing VM/SSH forwarding. Updating this code requires only a dashboard
service reload; no research process needs to restart. Deployment and service
reloads are operator actions, not actions performed by the dashboard.

The independent orchestrator cannot be discovered reliably from worker logs.
To include it, explicitly set this environment variable **for the dashboard
process**, then reload only that service:

```sh
export DANUS_DASHBOARD_ORCHESTRATOR_LOG=/absolute/path/to/the/project-session.jsonl
```

This must be the exact transcript for the intended project/deployment. The view
labels it as operator-configured; it does not independently verify the session's
project association or process health. It never scans the whole Codex home,
imports session history into an app, or resumes a session. Without this setting,
it displays **not connected**. Only output already written to the selected log
can be shown; this is not token-level streaming or hidden internal reasoning.

## Read-only source contract

`activity.py` reads the existing `workers/<name>/.status.json`, `.role`, `TASK.md`,
and `logs/round_<number>.log`, plus metadata for the three shared records above.
These filename contracts mirror `danus/execution/layout.py` without importing
execution code. Symlink escapes and browser-supplied arbitrary paths are rejected.
The optional orchestrator transcript is an explicit server-side allowlist entry.

`GET /api/activity` returns source metadata and small assignment/status previews.
It scans round filenames numerically and stats only the recent window; it never
reads historical round bodies. `GET /api/activity/log?worker=<name>&round=<n>`
returns at most a 64 KiB tail. `source=orchestrator` selects the configured
transcript instead. Truncation is labelled. The client fetches a log again only
when its selection/version changes or the operator explicitly refreshes.
There is no secondary database, event journal, paid summarizer, or runtime writer.

The two new APIs are GET-only and return `Cache-Control: no-store`. The new live
page has no CDN dependencies and renders all runtime text with `textContent`,
not HTML. Research logs may contain private or sensitive information: keep the
service on loopback/SSH, do not publish it, and do not commit real transcripts or
local configuration. The retained results page still uses its existing CDN assets.

## Files and checks

`__init__.py` registers the activity router on the existing FastAPI app.
`activity.py` contains bounded read-only adapters. `static/index.html` is the
self-contained live page; `static/results.html` preserves the original view.
`tests/test_activity.py` uses synthetic data to exercise source freshness,
malformed records, bounded tails, rotation, numeric ordering, missing transcripts,
path containment, GET-only APIs, unchanged source contents/mtimes, and old routes.

```sh
python -m pytest danus/observability/
```

No test needs a model call or a running research worker. A browser smoke check
should also cover appended output, round comparison, source switching,
disconnection, and a narrow viewport. Do not test against private live transcripts
when a synthetic fixture is sufficient.
