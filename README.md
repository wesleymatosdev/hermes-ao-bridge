# ao-bridge — an Agent Orchestrator board in the Hermes dashboard

ao-bridge is a [Hermes](https://github.com/NousResearch/hermes-agent) dashboard
plugin that turns the local [Agent Orchestrator (AO)](https://github.com/OrchestratorInc/agent-orchestrator)
daemon into a live Kanban board: every AO project's sessions render as cards
across six columns (orchestrator sessions get a quiet strip of their own above
the board), you can spawn new workers, message running ones, and open a
session's transcript as a chat. The plugin runs nothing itself — it mirrors
the daemon, which does the actual agent work.

![AO Bridge board](docs/screenshots/ao-bridge-board.png)

## Requirements

- **Hermes** with the web dashboard and its plugin SDK (current releases ship both).
- **The AO desktop app** (or a standalone `ao` daemon) running on the same
  machine — the plugin talks only to its loopback REST API, `http://127.0.0.1:3001`
  by default. Launching the AO app starts the daemon.

## Install

```bash
hermes plugins install wesleymatosdev/hermes-ao-bridge   # or copy this directory into ~/.hermes/plugins/
hermes plugins enable ao-bridge                          # user plugins are opt-in by design
```

Then **restart the dashboard**: the plugin's backend imports once at dashboard
startup, so installs and updates only take effect after a restart (the UI
tells you when it's newer than the running backend — see *Version-skew banner*).
An **AO Bridge** tab appears in the dashboard.

## What you get

**The board.** Columns `Queued · Running · Review · Blocked · Done · Archive`,
mapped from live AO session state (exact rules below). Cards show project,
session name, harness icon, activity state, branch, age, and PR links, and working
cards show the worker's current plan step. Spawn workers with the **+ Spawn
worker** form (project, harness, name, initial prompt), steer and message
sessions without leaving the board, and archive sessions (with confirmation —
archiving terminates the session via the daemon).

| AO session state | Board column |
|---|---|
| `isTerminated` / status `terminated` | Archive |
| status `merged` | Done |
| status `review_pending` or `kanbanColumn: needs_review` | Review |
| status `ci_failed` or `provisionState: failed` | Blocked |
| `provisionState: provisioning` | Queued |
| `kanbanColumn: ready`, or displayStatus `Awaiting PR` | Done |
| `working` / `idle` / `exited` (session alive) | Running |

**AO parity: placement vs activity.** Idle sessions stay on the board — that
is AO's own model (a finished-but-unarchived worker keeps occupying its
column), and the bridge deliberately mirrors it: `column_for` decides only
WHERE a session sits, never whether it is busy. On top of that placement the
bridge renders an honest activity state per session: **Working** (live turn),
**Idle** (alive, no turn), **Awaiting PR** (AO's displayStatus), or
**Archived** (terminated / exited). The card chips, the orchestrator strip's
dots, and the column-header counters (e.g. "3 working · 2 idle") all speak
this enum, and the **Active only** toggle keeps only sessions with a live
turn — an idle "Awaiting PR" card in Done never counts as active.

**Filters.** Text search over project, session name and harness; dropdown
filters for status, project and harness; a recency selector (last hour / 24h /
7 days / all time, default 24h); and an **Active only** toggle (default on)
that keeps only sessions with a live turn (idle, awaiting-PR, exited and
archived sessions all hide with it). All filtering happens in
the browser over the already-fetched board, and the filtered view still
updates live. Recency, the active toggle, and the Archive column's
collapse state persist across reloads.

**Live updates.** A WebSocket stream pushes board changes the moment the
daemon state moves (the backend re-checks AO about twice a second and only
sends real diffs) — no manual refresh. If the stream can't run, the board
falls back to 5-second polling; a `live` / `polling` / `degraded` badge shows
which mode you're in.

**Card detail.** Click a card for the session's metadata, its live plan, and
the conversation transcript as an iMessage-style chat, color-coded by sender
class: **Worker** turns (the agent's output), **End user** messages (typed in
the AO app or sent from this board), and **Orchestrator / automation**
(everything injected over the legacy queue-only REST path). Each class has a
click-to-hide filter chip, and classification keys on the daemon's `origin`
field — the `userAuthored` flag senders pass is self-declared and is never
treated as proof of human authorship. The sheet opens at the latest message
and follows new ones while open (~1s); scrolling up unpins and shows a
**↓ Jump to latest** pill. The transcript tail is bounded to the last 200
messages.

![Card detail sheet](docs/screenshots/ao-bridge-detail.png)

**Thinking vs speech.** The worker's reasoning activities are not speech —
they render as a clearly secondary **Thinking** row, never shaped like a
Worker bubble or a tool run: a small brain glyph, an italic muted
"Thinking" label and a one-line preview of the thought (~80 characters,
whitespace collapsed), collapsed by default; click to expand the full text,
still muted and italic. While a thought is actually being streamed, its row
pulses — only the last reasoning entry can be live (mid-turn, every entry of
the running turn reads `running`, so the backend marks liveness, not the
raw status). Thinking is worker output: it hides and shows with the **Worker**
chip, and has no filter chip of its own.

**Tool runs and plans.** The daemon's command and MCP-tool activities
interleave with messages in order, kept visually quiet: one muted
line per execution with an exit glyph (✓ / ✗ / spinner), click to expand the
full command, input or output (outputs capped at 4,000 characters, last 400
activities). The worker's plan renders as a checklist in the sheet and as a
one-line "current step" on working cards.

**Steer and message workers from the board.** The detail sheet ends in a
message composer, and every live card has a **Message** affordance that opens
the sheet with the composer focused — one click to steer. Delivery uses the
daemon's steer-or-send operation, the same one the AO app types into: a
session whose turn is in flight gets your text steered into that turn; an
idle session gets it as a new turn. Sent messages land with `origin=human`,
so they render as **End user** and (with the sheet open) appear in the
transcript through the live follow right after the send. Feedback is honest
per outcome — *"Sent — steered into the working turn"* vs *"Sent — new turn
queued"* — and daemon refusals (empty text, a harness that can't take
mid-turn guidance, uncertain delivery) surface inline with the daemon's error
code. The legacy queue-only `/send` route stays on the backend for
compatibility.
**Hermes session-store enrichment (hermes workers).** ACP — the protocol AO
uses to watch harnesses — delivers a lossy view of a `hermes` worker: command
outputs arrive truncated and oversized tool results degrade to a
"persisted-output" notice (parity audit below: 116k vs 442k characters of
tool I/O on one real session). Because this plugin's backend runs on the same
machine as the Hermes gateway, for `hermes`-harness sessions whose Hermes
session row it can identify, the detail sheet gains a second section —
**Hermes session store — full tool I/O** — with the session's real stats
(model, input/output/reasoning tokens, duration, cost) and the full-fidelity
tool calls (exact tool name, arguments, full output) and reasoning exactly as
Hermes stored them. Enriched rows are visually tinted and read straight from
`state.db` over a read-only sqlite connection — the plugin never writes to it
and never imports Hermes internals. Sessions with no identifiable Hermes row
(a non-`hermes` harness, or a worker that never produced a session row) show
no enrichment section at all; a missing store changes nothing. The join is
not a guess: AO launches `hermes acp` with its cwd set to the session's
worktree (`…/worktrees/<projectId>/<sessionId>`; standalone:
`…/worktrees/standalone/sessions/<sessionId>`), and that cwd is stored on the
Hermes session row — an exact suffix match narrowed to rows started at/after
the AO session's creation, most recent wins (resumes mint a fresh row). On a
measured 60 recent sessions this joined 52; the 8 misses were sessions with
no Hermes row at all, not wrong joins. Assumes plugin and gateway share the
machine (the normal install). Non-standard installs can point the reader
explicitly with the `AO_BRIDGE_STATE_DB` environment variable.

**Orchestrators.** AO orchestrator sessions (they coordinate workers; they
don't carry worker state) never enter the Kanban columns or their counters.
They get one quiet strip above the board instead — a compact row each with a
live status dot, name, project, harness, display status and age; live
orchestrators sort first and terminated history sinks to the bottom
(scrollable, capped). Rows share every filter with the worker cards and open
the same detail sheet. The strip hides itself when no orchestrator matches
the filters.

**Open in AO — interim workaround, off by default.** The AO desktop app
doesn't route session deep links yet (its open-url handler only knows the
orchestrator auth callback and billing hosts — session deep links are coming
to AO, upstream work in progress). With
`plugins.entries.ao-bridge.settings.open_in_ao_links: true` in config.yaml,
cards, orchestrator rows and the detail sheet show an **Open in AO** button:
it opens/focuses the AO app and copies the session id to your clipboard —
paste it into AO's Search (⌘K) to jump to the session. That's the interim
flow until true deep links land; the `ao-app://sessions/…` builder already
sits behind a single scheme constant in both backend and frontend, so when
AO handles session paths the same button becomes real navigation.

**Daemon-outage resilience.** When the AO daemon goes down, the board keeps
the last-known state under an "AO daemon unreachable — retrying…" banner with
a `degraded` badge, every failure surfaces as a typed error (never a blank
board), and live mode resumes automatically — including a forced refresh the
moment the daemon is back, even if nothing changed.

**Version-skew banner.** The dashboard imports the plugin's backend once at
startup while the frontend is served fresh from disk, so an updated plugin can
end up driving a stale backend. The UI compares versions and shows a "Plugin
updated — reload dashboard to apply" banner with a **Reload** button instead of
misbehaving silently.

**First-run setup.** If the daemon has never answered, the board shows a setup
panel — what to install, how to point the plugin at a daemon elsewhere — with
a retry button, and checks continue in the background. Once connected, later
outages get the retry banner instead of regressing to setup.

![First-run setup state](docs/screenshots/ao-bridge-setup.png)

**Per-harness icons.** Cards and the spawn form show the same icon per harness
that the AO desktop app shows (38 harnesses mapped, with a fallback icon).

## Configuration

Settings live at `plugins.entries.ao-bridge.settings.*` in `~/.hermes/config.yaml`:

```yaml
plugins:
  entries:
    ao-bridge:
      settings:
        ao_base_url: http://127.0.0.1:3001
        relay_enabled: false
```

| Key | Default | What it does |
|---|---|---|
| `ao_base_url` | `http://127.0.0.1:3001` | Base URL of the AO daemon's REST API. Loopback only. |
| `relay_enabled` | `false` | Turn on the AO event relay (see below). |
| `relay_delivery` | `auto` | Delivery route: `auto` (api_server when configured, else peer) / `api_server` / `peer`. Relay only. |
| `relay_events` | `[]` (all) | Which event kinds to relay: `report_done`, `report_checkpoint`, `report_stuck`, `report_needs_input`, `session_idle`, `session_exited`, `session_terminated`. |
| `relay_api_url` | *(auto-detect)* | Gateway api_server base URL the relay delivers through (loopback). Empty auto-detects from your config.yaml api_server settings. Relay only. |
| `relay_api_key` | *(empty)* | Your gateway's `API_SERVER_KEY`, pasted here — authenticates the api_server delivery route ([Enable delivery](#enable-delivery)). Relay only. |
| `relay_peer` | *(empty)* | Optional enhancement: name of a Hermes peer whose `HERMES_PEER_<NAME>_KEY` authenticates relay delivery when no api_server key is set (falls back to `API_SERVER_KEY`). Relay only. |
| `relay_session` | *(empty)* | Override destination for events whose orchestrator mapping can't resolve (see the relay section). Normal destinations are resolved per event; `GET /api/plugins/ao-bridge/relay/sessions` lists recent sessions to copy from. Relay only. |

For development, `AO_BRIDGE_BASE_URL` overrides the daemon URL (the config
setting wins when both are set). The backend reads nothing beyond the table
above; the relay's api_server URL is auto-detected from config.yaml, and the
plugin never reads any credential from Hermes config or env on its own —
api_server delivery uses the key you paste into `relay_api_key`, the peer
route uses Hermes's secret store (same scoped mechanism `hermes peer` uses).

## AO event relay (opt-in)

With `relay_enabled: true`, the plugin's backend (not your browser) polls the
AO daemon and delivers worker milestones — report transitions (done /
checkpoint / stuck / needs-input) and working → idle / exited / terminated
changes — into the owning orchestrator's Hermes conversation, so they reach
you wherever that conversation lives, not only in the AO app.

**Where events go.** Destinations are resolved per event from AO's own
structure: the event's project's live orchestrator session (AO REST), then the
Hermes conversation that orchestrator runs (identified by its
`…/worktrees/<project>/orchestrator/` working-directory marker — not the
volatile display name). Workers on different projects route to their own
orchestrators. Events that can't be mapped — no live orchestrator, several
sharing a project, no matching conversation — are dropped rather than guessed
(and reported via the diagnostics endpoint); `relay_session` is an explicit
override for exactly those cases.

**How it delivers.** The batch is submitted as a **live turn** to the
destination session through the gateway's own api_server session-chat route —
the same path `hermes peer dm` drives. A delivered event **wakes the
destination session immediately and costs it a model turn**: the agent sees
the milestone right away and may act on it — that's the point. Milestones
landing within ~5 seconds share one turn (max 8 lines), so the cost is per
burst, not per event; the `relay_events` filter keeps quiet projects quiet.

**Delivery is a two-tier route** (`relay_delivery` picks: `auto` /
`api_server` / `peer`):

1. **`api_server` — the generic route (works for anyone).** Uses the stock
   [api_server platform](https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server)
   of your own gateway, authenticated with your `API_SERVER_KEY` pasted into
   `relay_api_key`. Any Hermes user can enable it — no peer setup, no
   1Password, nothing machine-specific.
2. **`peer` — optional enhancement.** Uses a registered bot peer's scoped
   key (`hermes peer add` → `HERMES_PEER_<NAME>_KEY`, resolved through the
   same secret-scope read `hermes peer` uses). The original credential path;
   kept for setups that already have a peer registered.

`auto` (the default) uses api_server when `relay_api_key` is set, else peer
when a peer key resolves. When neither is configured, nothing is delivered
**and the dashboard says so** — an amber "delivery not configured" banner
pointing at [Enable delivery](#enable-delivery) (the `GET
/api/plugins/ao-bridge/relay` diagnostics carry the same state and reason).
It is never a silent no-op. An explicitly picked route that is unavailable
stays unavailable — it never silently falls back to the other one.

#### Enable delivery

1. **Enable the api_server platform on your gateway.** In `~/.hermes/.env`:

   ```bash
   API_SERVER_ENABLED=true
   API_SERVER_KEY=change-me-long-random
   ```

   then start (or restart) the gateway — it listens on `http://127.0.0.1:8642`
   by default (`platforms.api_server.host` / `.port` in config.yaml override
   the bind; the plugin auto-detects that address for `relay_api_url`, so you
   normally never set the URL yourself).

2. **Paste that same key into the plugin config** (`config.yaml`):

   ```yaml
   plugins:
     entries:
       ao-bridge:
         settings:
           relay_enabled: true
           relay_api_key: change-me-long-random   # your API_SERVER_KEY
   ```

   The plugin dials your gateway's api_server on loopback with this key — the
   same secret your own machine already holds; it never leaves the machine,
   and the plugin never reads it from Hermes config or env on its own.

3. **Optional enhancement — a peer credential instead.** If you already run
   bot peers, set `relay_peer: <name>`; the relay then authenticates with
   that peer's scoped `HERMES_PEER_<NAME>_KEY`. Under `relay_delivery: auto`
   an api_server key, when present, still wins.

The relay deduplicates on stable report ids (AO's repeat-count churn never
re-relays the same state), records a baseline on its first poll so enabling it
doesn't flood any conversation with history, stays silent through daemon
downtime, and retries when the gateway is busy or unreachable (permanent
failures — route not configured, gone session — drop the batch and surface in
diagnostics and the dashboard banner). `GET /api/plugins/ao-bridge/relay`
reports enabled state, the delivery route (mode, active route, configured,
reason), pending destinations, delivered count and last error. It makes no
platform sends: the conversation's own bound surfaces render the exchange,
exactly as for any turn in it.

## Security & privacy

| Capability | What actually happens |
|---|---|
| **Spawns agent sessions** | `POST …/spawn` creates a real AO worker session (an AI coding agent) that executes tasks in that project's workspace. |
| **Archives agent sessions** | The card's Archive action calls the daemon's kill endpoint, terminating a running session. Unsaved work in its workspace is lost. |
| **Messages agents** | The Message action posts into a running session; the agent treats it as user input and acts on it. |
| **Reads AO state** | The board, detail sheet and relay read the daemon's project/session/report endpoints. Read-only. |
| **Relays AO events (opt-in)** | With `relay_enabled`, the backend submits worker milestones as live turns to the destination session through the gateway's api_server session-chat route (URL auto-detected from config.yaml, loopback by default). A delivered event wakes that session. Authenticated with your pasted `API_SERVER_KEY` (`relay_api_key`) or a peer's scoped `HERMES_PEER_<NAME>_KEY`. |

All network calls go to the local AO daemon on loopback — plus, when the
relay is enabled, the local gateway api_server it delivers through. The only
secret in plugin config is `relay_api_key` — your own gateway's
`API_SERVER_KEY`, authenticating loopback calls to your own machine; it never
leaves the machine, and the plugin never reads any key from Hermes config or
env on its own. The optional peer route's credential is read from Hermes's
own secret store through the same scoped mechanism `hermes peer` uses.
Plugin API routes sit behind the dashboard's auth, so scripts and
agents can't call them headlessly — call the daemon's own loopback REST API
directly instead.

## Known limitations

- **Intra-turn card motion is bounded by the daemon.** A session's row is
  static for the length of a turn, so a working card's plan step refreshes on
  a 15-second cache while the turn runs; the detail sheet's watch channel is
  the fine-grained (~1s) live surface for a single session.
- **No drag-and-drop between columns** — column membership is AO-owned state,
  and the plugin doesn't fight the daemon's lifecycle model.
- **Spawn requires a healthy workspace registration** — AO itself rejects
  spawns for projects whose directories are missing.
- **Enrichment can't recover outputs Hermes spilled to disk.** When a tool
  result exceeded Hermes's inline limit, Hermes itself stored only the
  "persisted-output" notice (the full text lives in a spillover cache file);
  the enriched section then shows the same notice AO saw — that is everything
  the store holds.

## Development

```
hermes-ao-bridge/
├── plugin.yaml               # package manifest + config_schema
├── __init__.py               # register(ctx) — no agent-runtime registrations
└── dashboard/
    ├── manifest.json         # tab (/ao-bridge, after skills) + api + assets
    ├── plugin_api.py         # FastAPI router, mounted at /api/plugins/ao-bridge/
    ├── assets/               # per-harness icons + map (served to the browser)
    └── dist/                 # plain-IIFE React frontend (HERMES_PLUGIN_SDK) + theme CSS
```

The backend is stdlib + FastAPI only, importable outside the dashboard server
(the `hermes_cli` config read is best-effort inside functions). It talks to
the AO daemon with `urllib`:

| Plugin route (`/api/plugins/ao-bridge/`) | AO daemon call |
|---|---|
| `GET /health`, `GET /board` | `GET /healthz`; `GET /api/v1/projects` + `GET /api/v1/sessions` (+ `GET /api/v1/sessions/{id}/conversation` for plan steps) |
| `GET /sessions/{id}/detail` | `GET /api/v1/sessions/{id}` + `…/conversation`; for `hermes` sessions plus a read-only sqlite read of the local Hermes `state.db` (enrichment) |
| `POST /spawn` | `POST /api/v1/sessions` (kind `worker`) |
| `POST /sessions/{id}/steer` | `POST /api/v1/sessions/{id}/conversation/steer-or-send` (steer-or-send; mints the required `clientMessageId`) |
| `POST /sessions/{id}/send` | `POST /api/v1/sessions/{id}/send` (legacy queue-only, kept for compatibility) |
| `POST /sessions/{id}/kill` | `POST /api/v1/sessions/{id}/kill` |
| `GET /relay`, `GET /relay/sessions` | `GET /api/v1/reports?projectId=…` (relay only) |
| `WS /events` | board + detail pushes; server-polls the routes above |

**Tests** run against an in-process fake AO daemon — no live daemon, no
network, stdlib `unittest` only:

```bash
~/.hermes/hermes-agent/.venv/bin/python -m unittest discover -s tests -t tests
```

(Any Python with `fastapi` works; the suite isolates `HERMES_HOME` to a
throwaway directory.) Coverage: the AO client, column mapping, the FastAPI
router (board assembly, spawn/send/steer/kill, steer error-shape mapping,
detail shape and
bounding, WebSocket stream, outage recovery, version-skew binding), plan
parsing, the relay (event extraction, batching, dedup, delivery), and the
Hermes enrichment (join resolution against a fixture `state.db`, merge +
bounding contracts, degradation without a store).

#### Parity audit — AO's ACP view vs the Hermes store (2026-11-09)

What AO captured vs what Hermes stored for the same worker session, measured
live (enrichment exists to close the tool-I/O half of this gap):

| Signal | AO (ACP conversation) | Hermes `state.db` |
|---|---|---|
| Tool I/O, hermes-agent-31 | 116,336 chars | 442,119 chars |
| Tool I/O, agent-orchestrator-172 | 36k (command) + persisted-output notices | 400,477 chars |
| Command output cap | ~630 chars max observed | full |
| Exact tool names | provider-facing summaries | `terminal`, `read_file`, `patch`, … |
| Reasoning | 162,649 chars | 163,914 chars (at parity — no enrichment needed) |
| Session stats (model, tokens, cost, duration) | absent | sessions row |
| Sessions with a Hermes row | 52 of 60 recent | the 8 misses never produced one |

**Serving locally:** point a throwaway Hermes home at this checkout to verify
frontend/backend changes against a live daemon — assets are served per-request
from disk (frontend edits are live on reload), while `plugin_api.py` imports
once at dashboard boot (restart the dashboard to pick up backend edits):

```bash
HERMES_HOME=/tmp/ao-bridge-dev-home hermes dashboard --port 9119 --no-open
# with /tmp/ao-bridge-dev-home/config.yaml:
#   plugins:
#     enabled: [ao-bridge]
# and this checkout at /tmp/ao-bridge-dev-home/plugins/ao-bridge
```
