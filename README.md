# ao-bridge — Hermes Dashboard Plugin

A Hermes web-dashboard plugin that makes the **Agent Orchestrator (AO) daemon**
Hermes' multi-project orchestration backend, surfaced as a Kanban board.

Every AO project's sessions render as cards across columns mapped from live AO
session state — spawn new workers, send messages to running sessions, watch the
board refresh itself.

## ⚠️ Capability disclosure (read this first)

| Capability | Detail | Risk |
|---|---|---|
| **Spawns agent sessions** | `POST /api/plugins/ao-bridge/spawn` creates a real AO worker session (an AI coding agent) in the selected project with the harness you pick. | **Heavy.** The spawned agent executes tasks in that project's workspace. |
| **Archives agent sessions** | The card **Archive** action calls the AO daemon's kill endpoint, terminating a running agent session (AO marks it Archived). | **Heavy.** Unsaved work in that session's workspace is lost. |
| **Sends messages to agents** | The **Message** action posts a user-authored message into a running AO session; the agent treats it as user input and acts on it. | Medium. Drives a live agent. |
| **Local-only network calls** | The backend talks ONLY to the local AO daemon REST API (`http://127.0.0.1:3001` by default, loopback). No secrets are read, no external requests are made. | Low. Local loopback only. |
| **Reads AO state** | `GET /board` reads the AO daemon's project and session lists and reduces them to board cards. | Low. Read-only. |
| **Relays AO events into a Hermes conversation** (v0.3.x, **off by default**) | With `relay_enabled: true`, a backend-owned poll loop delivers one-line AO worker milestones (report done/checkpoint/stuck/needs-input, working→idle/exited/terminated transitions) as **live turns** into the destination conversation through the gateway's own api_server session-chat route (`POST /api/sessions/{id}/chat` — the path `hermes peer dm` drives). The destination is resolved **per event** from AO's own orchestrator mapping (the project's live `orchestrator` session, whose Hermes conversation is identified by its `…/worktrees/<project>/orchestrator/` cwd marker); `relay_session` is an explicit override for unmappable cases only. | Medium, opt-in. **A delivered event wakes the destination session and costs it a model turn** — the agent sees the milestone immediately and may act on it; that is the point, and the `relay_events` filter plus the built-in batch/debounce (one turn per burst) exist to keep the cost per burst, not per event. Authenticated with a Hermes-held secret (a named peer's `HERMES_PEER_<NAME>_KEY`, or `API_SERVER_KEY`) — **the plugin config holds no secrets**. The turn runs through Hermes core's own admission path; the relay never talks to a messaging platform directly. |

Nothing here spawns processes itself, touches the filesystem outside the AO
daemon's own scope, or calls any network host other than the configured local
AO daemon base URL — plus, when the relay is enabled, the local gateway
api_server it delivers through (`relay_api_url`, loopback by default).

## Screenshots

The board against a live AO daemon (six projects, 300+ sessions):

![Kanban board](docs/screenshots/ao-bridge-board.png)

Spawn form — pick a registered project, harness, name, and initial prompt:

![Spawn form](docs/screenshots/ao-bridge-spawn-form.png)

Message sheet — send a user-authored message to a running session:

![Message sheet](docs/screenshots/ao-bridge-message-sheet.png)

## Prerequisites

1. **Agent Orchestrator (AO) desktop app / daemon** — the plugin is a view over
   the local AO daemon; it runs nothing itself. Install the [AO desktop app](https://github.com/OrchestratorInc/agent-orchestrator/releases)
   and open it: launching the app starts the daemon on loopback.
2. **Daemon base URL** — the plugin talks to `http://127.0.0.1:3001` by
   default. If your daemon answers elsewhere, set `ao_base_url`:
   `plugins.entries.ao-bridge.settings.ao_base_url` in `~/.hermes/config.yaml`
   (or the `AO_BRIDGE_BASE_URL` env var for development). Loopback addresses
   only; the plugin makes no external calls.
3. **Plugin install** — from the Hermes CLI: `hermes plugins install
   <source>` (or drop the repo into `~/.hermes/plugins/ao-bridge`), then
   enable it in `plugins.enabled` and restart the dashboard so the backend
   imports. Open the **AO Bridge** tab in the web dashboard.

Until a daemon first answers, the board shows a setup panel instead of cards;
once connected, a daemon going down degrades to the last-known board with a
retry banner (see *Daemon-outage resilience* below).

## Requires

- A Hermes version whose dashboard exposes `window.__HERMES_PLUGIN_SDK__`
  (current releases do).

## What you get

- **Kanban board**: columns `Queued · Running · Review · Blocked · Done · Archive`,
  mapped from live AO session state (`provisionState`, `status`, `kanbanColumn`,
  `isTerminated`). Cards show project, session name, harness, display status,
  branch, age, and PR links.
- **Live updates**: a WebSocket stream (`/events`) pushes board changes the
  moment the daemon state moves (server polls AO at ~1s, hash-gated diffs) —
  no manual refresh. Falls back to 5s polling automatically; a `live`/`polling`
  badge shows which mode you're in.
- **Daemon-outage resilience (v0.2.8)**: when the AO daemon bounces, every
  route answers a typed error payload (`kind`/`retryable`/`message`) instead of
  prose, the WS stream tells connected clients (`daemon_error` frame) and
  forces a board frame the moment the daemon is back, the UI keeps the
  last-known board under an "AO daemon unreachable — retrying…" banner with a
  `degraded` badge, and an HTTP poll runs alongside the reconnecting WS until
  live frames resume. A first-load failure renders an in-theme error state, never
  a blank board.
- **Version-skew detection (v0.2.8)**: the Hermes dashboard imports
  `plugin_api.py` once at startup and never re-imports after a plugin update,
  while the frontend assets are served from disk per request. The backend
  reports its import-time `pluginVersion` on every board/health payload; the UI
  compares it with the served `manifest.json` and shows a "Plugin updated —
  reload dashboard to apply" banner on mismatch (a payload with no version is
  a pre-0.2.8 backend — also stale). Until the dashboard restarts, the banner
  persists by design.
- **Search & filter**: instant client-side match on project, session name, and
  harness, plus a status dropdown built from live `displayStatus` values. On top
  of the text search the header has a **project** dropdown (from the registered
  AO projects), a **harness** dropdown (from the harnesses present on cards), a
  **recency** selector (Last hour / Last 24h / Last 7 days / All time, keyed on
  each session's last-activity timestamp, defaulting to Last 24h), and an
  **Active only** toggle (on by default) that hides terminated, exited and
  archived sessions. Recency and the active toggle persist across reloads. All
  filtering happens in the browser over the already-fetched board — changing a
  filter makes no daemon call, and the filtered view still updates in place
  from live `/events` pushes.
- **Per-harness icons**: cards and the spawn form show the same icon per
  harness that the AO desktop app shows (33 inline SVGs from AO's renderer
  bundle, plus bundled PNG/SVG assets for hermes, memcode, unreal-agent, agy
  and codewhale), mapped via `dashboard/assets/harness-icons.json` with a
  fallback icon. Native `<select>` options can't render images, so the spawn
  form shows the selected harness's icon live beside the selector.
- **Card detail sheet**: click a card for status, project, harness, branch,
  age, the transcript tail (bounded to 200 messages), and the canonical
  `ao://sessions/…` link — read-only. The transcript reads as an
  iMessage-style chat: rounded bubbles aligned by sender class —
  **Worker** turns on the left (green), **End user** messages on the right
  (purple/cyan gradient), **Orchestrator / automation** on the left with a
  dashed amber bubble — each with a small class label and its own
  click-to-hide filter chip. Provenance (v0.2.4): classification keys on the
  daemon's `origin` field, the only reliable signal — `human` is set only
  for messages typed in the AO app, `automation` for every REST `/send`
  call. `authoredByUser` is **self-declared** by whoever calls the send
  endpoint (any orchestrator can pass `userAuthored: true`), so it never
  grants the "End user" class; Worker = assistant turns, End user =
  `origin: "human"`, everything else = Orchestrator / automation.
  Live follow (v0.3.1): the sheet opens at the **latest** message (not the
  first) and follows new ones while open — it sends `{"type": "watch",
  "sessionId"}` over the plugin's `/events` WS and re-renders on
  `{"type": "detail"}` pushes (transcript, tools, plan and turn state,
  digest-gated, ≤ ~1s). Scrolling up unpins the follow and shows a sticky
  **↓ Jump to latest** pill; scrolling back to the bottom (or clicking the
  pill) re-pins. Without WS support the sheet falls back to a 2s poll of the
  detail route.
- **Tool executions in the transcript** (v0.3.0): the daemon's conversation
  activities (`command`, `mcp_tool`, `reasoning`) interleave with messages by
  their shared `sequence` number, kept visually quiet — one muted line per
  execution (glyph, truncated summary, exit status: green check / red ✗ /
  spinner while in flight), click to expand the full command, input, or
  output. Reasoning ships collapsed by default. The daemon reports an
  in-flight command as `status: "pending"`; reasoning stays `"running"` even
  after the turn moves on, so it never shows a spinner. Bounded like the
  transcript (last 400 activities, outputs capped at 4000 chars). `plan`
  activities render in the Plan panel, not the transcript.
- **Open in Agent Orchestrator** (v0.3.0): every card and the detail-sheet
  header carry the canonical `ao://sessions/{projectId}/{sessionId}` deep
  link (each path segment percent-encoded), handing off to the AO desktop
  app. Note: the installed AO app must register the `ao` URL scheme —
  nightlies up to 0.14.11 register only `ao-app`.
- **Spawn form**: pick a registered AO project, harness, session name, and
  initial prompt → creates the worker via the AO daemon.
- **Message sheet**: send a user-authored message to any live session.
- **Archive action** with confirmation on any non-archived session.

## Install

```bash
hermes plugins install <path-or-repo>   # or copy this dir into ~/.hermes/plugins/
hermes plugins enable ao-bridge         # user plugins are opt-in by design
```

Then open the dashboard — a new **AO Bridge** tab appears after Skills.

Requires a running AO daemon (the Agent Orchestrator desktop app or
`ao` daemon) on the loopback. Default base URL: `http://127.0.0.1:3001`.

### Configuration

`plugin.yaml` declares one setting (`config_schema`), stored at
`plugins.entries.ao-bridge.settings.ao_base_url` in config.yaml:

```yaml
plugins:
  entries:
    ao-bridge:
      settings:
        ao_base_url: http://127.0.0.1:3001
```

For development you can also set `AO_BRIDGE_BASE_URL` (the config.yaml setting
wins when both are present). The backend reads no other configuration and
no secrets.

### AO event relay (opt-in, v0.3.x)

AO worker milestones — report transitions (`done` / `checkpoint` / `stuck` /
`needs-input`) and session status transitions (`working` → `idle` / `exited` /
`terminated`) — can be relayed into the owning orchestrator's Hermes
conversation, so they reach you wherever that conversation lives instead of
only on the orchestrator's AO-embedded surface.

```yaml
plugins:
  entries:
    ao-bridge:
      settings:
        relay_enabled: true
        # everything below is optional:
        relay_events:                    # omit for all kinds
          - report_done
          - report_stuck
          - report_needs_input
        relay_peer: relaybot             # scoped credential (see below)
        relay_api_url: http://127.0.0.1:8642
        relay_session: local-2a0aee93…   # OVERRIDE for unmappable cases only
```

- **Destination is resolved per event, not configured.** Each event carries
  its AO project; the relay maps it to that project's live `orchestrator`
  session (AO REST), then to the orchestrator's Hermes conversation
  (identified by the `…/worktrees/<project>/orchestrator/` cwd marker on the
  session — the marker, not the volatile display name). Different workers can
  belong to different orchestrators and route accordingly. **`relay_session`
  is an override fallback** for unmappable cases only (no live orchestrator,
  several sharing a project, no matching conversation) — it never shadows a
  resolvable mapping.
- **What arrives.** Compact one-line events, batched: several milestones
  landing within ~5 s share one message (cap 8 lines), e.g.
  `✅ AO my-worker — report done: PR open, CI green` /
  `💤 AO my-worker is idle (was working)`.
- **How it delivers — a live wake, not a transcript row.** The batch is
  submitted as a turn into the destination session through the gateway's own
  api_server session-chat route (`POST /api/sessions/{id}/chat` — the same
  sanctioned immediate-submission path `hermes peer dm` drives). The session
  wakes **immediately** and runs a turn on the milestone; the exchange
  renders on the surfaces that watch that conversation. **Trade-off, by
  design:** a delivered event costs the destination session a model turn
  (even when idle) — that is the price of immediate delivery, and the
  v0.3.2 alternative (a store row that only landed at the session's next
  turn) is exactly the "messages arrive only after you message the session"
  bug class this replaces. Keep it calm with `relay_events` + the built-in
  batching; there is no separate min-interval knob — the debounce IS the
  interval.
- **Credentials stay in Hermes.** The relay authenticates with a Hermes-held
  secret: a named peer's scoped key (`relay_peer` →
  `HERMES_PEER_<NAME>_KEY`, resolved through the same secret-scope read
  `hermes peer` uses; `hermes peer add` registers one), falling back to the
  gateway's `API_SERVER_KEY`. The plugin config never holds a secret.
- **Reliability.** The poll loop is backend-owned (independent of dashboard
  browser clients), survives daemon downtime silently, and deduplicates on
  stable report ids so AO's `repeatCount` churn never re-relays the same
  state. A busy/unreachable gateway (429, 5xx, connection refused) keeps the
  batch and retries next tick; permanent failures (no credential, session
  gone) drop it and surface on the diagnostics endpoint.
- **Diagnostics.** `GET /api/plugins/ao-bridge/relay` reports enabled/
  override/api URL/credential kind/delivery count/pending destinations/last
  error. `GET /api/plugins/ao-bridge/relay/sessions` lists recent sessions
  for picking the override.

No platform configuration: the relay makes no platform sends itself — any
platform delivery is the destination session's own Hermes transport, exactly
as for your own messages in that conversation.

## How it differs

Almost nothing ships an **agent runtime as the orchestrator brain over many
external harnesses** — the one exception stops at chat tools (below). The
landscape splits into five groups, and ao-bridge sits in the gap between them:

| Category | Projects | Why it's not this |
|---|---|---|
| Hermes-native orchestration | Hermes built-in kanban & delegation, olympus, hermes-war-room | Orchestrate **Hermes profiles only** — no claude-code/codex/agy workers underneath |
| Multi-harness orchestrators with Hermes as a worker | mco, awslabs cli-agent-orchestrator, Agentainer, bernstein | Hermes is **one CLI they drive** — Hermes never drives the fleet |
| Standalone fleet apps, human-conducted | Paseo, agetor, Hydra, multiagents | The **human is the conductor**; no agent runtime sits in the loop |
| Agent-driven via CLI skill | **Orca** (stablyai/orca) | See below — closest, but the agent only reaches it by shelling out |
| Agent tools for AO, chat-only | **hermes-ao** (illegalcall/hermes-plugin-agent-orchestrator, by the AO vendor) | Real: 17 agent tools (spawn/send/kill/issues/PRs) so Hermes can conduct AO from chat. But it has **no UI** — the human's only window is chat text — and it shells the `ao` CLI per call (`AO_CWD`, subprocess timeouts) rather than the daemon REST. Last updated April 2026. |

**On hermes-ao:** it proves the demand for Hermes-conducts-AO and deserves credit —
it's the AO vendor's own tool plugin. ao-bridge is the layer it doesn't have:
the **visual board**. Where hermes-ao turns the conversation into the only
interface (text in, text out), ao-bridge gives the human a live window — every
AO project's sessions on one Kanban board with real-time state, PR links, and
one-click spawn/send/archive — served from the dashboard over the daemon's REST
API. The two are complementary (chat tools for the agent, board for the
human), and ao-bridge deliberately talks to the daemon's loopback REST instead
of shelling the CLI, so it works wherever the dashboard works.

**On Orca specifically, honestly:** Orca (YC-backed, open source, macOS/Windows/
Linux + mobile + SSH/cloud) matches or beats AO on harness breadth (30+,
including Hermes itself) and clearly wins on platform reach, with real
structured orchestration (runs, tasks, dispatches, supervised workers). But the
only way an agent drives Orca today is by shelling out through `orca-cli` — the
prompt-or-skill category, not a native integration — and the Hermes↔Orca
connection is one-way telemetry: an orca-status hook plugin reports Hermes
events *to* Orca, and nothing lets Hermes *drive* an Orca fleet. We don't claim
ao-bridge beats Orca generally; it claims a different niche.

**The ao-bridge niche:** Hermes stays the agent you already talk to — chat,
Discord, Telegram, cron — while the Agent Orchestrator supplies the machinery
underneath: git worktree isolation per session, session persistence, a review
system, and a 37-harness catalog. It is a real Hermes plugin (dashboard tab +
backend API router, with the shipped hermes-ao-connector closing the loop so
spawned workers talk back through the gateway), not a skill wrapping someone
else's CLI. The dashboard is the human's window; the agent is the conductor.
The target end-state: one orchestrator, driven from messaging, running
multi-harness workers across multiple projects.

## Architecture

```
hermes-ao-bridge/
├── plugin.yaml               # native package manifest + config_schema
├── __init__.py               # register(ctx) — no agent-runtime registrations
└── dashboard/
    ├── manifest.json         # tab (/ao-bridge, after:skills) + api + assets
    ├── plugin_api.py         # FastAPI APIRouter, mounted at /api/plugins/ao-bridge/
    ├── assets/               # per-harness icons + map (served to the browser)
    └── dist/
        ├── index.js          # plain-IIFE React frontend (HERMES_PLUGIN_SDK)
        └── style.css         # nebula/galaxy dark theme, scoped under .ao-bridge
```

The backend is stdlib + FastAPI only. It never imports Hermes internals at
module scope (the `hermes_cli` config read is best-effort inside a function) so
the dashboard server, the plugin validator, and bare-FastAPI test harnesses can
all load it. It talks to the AO daemon with `urllib` over these endpoints
(wire contract verified against the live daemon):

| Plugin route (mounted at `/api/plugins/ao-bridge/`) | AO daemon call |
|---|---|
| `GET /health` | `GET /healthz` |
| `GET /board` | `GET /api/v1/projects` + `GET /api/v1/sessions` |
| `POST /spawn` | `POST /api/v1/sessions` (kind `worker`) |
| `POST /sessions/{id}/send` | `POST /api/v1/sessions/{id}/send` |
| `POST /sessions/{id}/kill` | `POST /api/v1/sessions/{id}/kill` |

The frontend registers through `window.__HERMES_PLUGINS__.register`, fetches via
`SDK.authedFetch` (loopback token header and gated-cookie modes both handled by
the SDK), and bails out silently when the SDK global is absent.

## Column mapping

| AO session state | Board column |
|---|---|
| `isTerminated` / status `terminated` | Archive |
| status `merged` | Done |
| status `review_pending` or `kanbanColumn: needs_review` | Review |
| status `ci_failed` or `provisionState: failed` | Blocked |
| `provisionState: provisioning` | Queued |
| `kanbanColumn: ready` (Awaiting PR) | Done |
| `working` / `idle` / `exited` (session alive) | Running |

## Tests

stdlib `unittest` only (the gateway runtime ships no pytest), against an
in-process fake AO daemon — no live daemon, no network:

```bash
HERMES_SRC=~/.hermes/hermes-agent \
  ~/.hermes/hermes-agent/.venv/bin/python \
  -m unittest discover -s hermes-ao-bridge/tests -t hermes-ao-bridge/tests
```

Coverage: AO client (health, projects, sessions, single-session unwrap, spawn
payload shape, send, kill, conversation, error mapping), column mapping (every
rule + precedence), and the FastAPI router (board assembly, spawn validation,
send/kill forwarding, 502/404/409 mapping, detail route shape + transcript
truncation + 404, tool-activity reduction + bounding + `ao://` deep-link
encoding, WebSocket stream push-on-change / daemon-blip survival / auth
rejection) via `fastapi.testclient`. v0.2.8 adds `tests/test_resilience.py`:
pluginVersion exposure + import-time binding (the skew mechanism), typed error
payloads for every route with the upstream down (including validation
precedence), full-route recovery once the upstream returns, and the WS
outage contract (error frame during the outage, board frame on recovery even
with an unchanged card set). The frontend has no JS test harness; its behavior
is verified manually against a live board (see the resilience notes above).

## Known limitations

- **Intra-turn card motion is bounded by the daemon.** The daemon's session
  list is static for the whole length of a turn — status, activity and
  `updatedAt` only move at turn/state transitions, and its `/api/v1/events`
  stream carries no conversation/plan events. So a working card's status chip,
  counts and plan step update within ~1s of any daemon-side change (the WS
  poll loop pushes on every digest change), but the plan step itself only
  refreshes on the plan cache's 15s TTL window while the turn runs. There is
  no polling of every session to work around this; the detail sheet's watch
  channel is the fine-grained live surface (≤ ~1s) for a single session.
- Headless callers can't reach the plugin routes: the dashboard's auth
  middleware (basic hash / OAuth) protects plugin endpoints by design and
  cannot be bypassed by a third-party plugin. Scripts and agents should call
  the AO daemon's loopback REST API directly instead.
- Spawn requires the target project's workspace registration to be healthy;
  AO itself rejects spawns for projects with missing directories.
- No drag-and-drop between columns — column membership is AO-owned state, and
  the plugin deliberately doesn't fight the daemon's lifecycle model.
