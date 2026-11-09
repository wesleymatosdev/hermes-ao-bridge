/**
 * AO Bridge — Hermes Dashboard Plugin (frontend)
 *
 * Kanban board over the local AO daemon: every AO project's sessions as cards,
 * spawn form, send-message sheet, archive action. Polls the plugin backend at
 * /api/plugins/ao-bridge/ for live refresh.
 *
 * Plain IIFE, no build step. Uses window.__HERMES_PLUGIN_SDK__ for React and
 * the authed fetch. Bail out silently when the SDK global is absent.
 */
(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK) return;

  const { React } = SDK;
  const h = React.createElement;
  const { useState, useEffect, useCallback, useMemo, useRef } = SDK.hooks || React;

  const API = "/api/plugins/ao-bridge";
  const POLL_MS = 5000;
  const DEFAULT_BASE_URL = "http://127.0.0.1:3001";
  // Deep-link scheme of the installed AO desktop app (Info.plist
  // CFBundleURLTypes, 0.14.11-nightly): it registers ONLY "ao-app" — plain
  // "ao://" never reaches the app. Kept as a single constant: if a future AO
  // build registers another scheme, this is the one-line change (the backend
  // has the same constant; the skew banner catches a mismatch).
  const AO_DEEP_LINK_SCHEME = "ao-app";
  // Verified from the installed AO desktop app's own update feed
  // (OrchestratorInc/agent-orchestrator) — not a guessed link.
  const AO_APP_RELEASES_URL = "https://github.com/OrchestratorInc/agent-orchestrator/releases";
  // Where the "Enable delivery" relay setup steps live — the anchor the
  // delivery-not-configured banner points at.
  const README_DELIVERY_URL = "https://github.com/wesleymatosdev/hermes-ao-bridge#enable-delivery";
  // One message for every "daemon is down, holding the last-known board"
  // surface: header banner, WS daemon_error frames, the first-load error state.
  const DAEMON_DOWN_MSG = "AO daemon unreachable — retrying…";
  // Browser assets are served from the plugin's dashboard/ dir by the host
  // (dashboard-plugins static route). The SDK exposes no asset-base helper,
  // so the fixed plugin id doubles as the URL base.
  const ASSET_BASE = "/dashboard-plugins/ao-bridge";

  // === theme layer: begin (pure on purpose — tests/test_themes.py runs this
  // === exact block under node with a stubbed localStorage) ===
  const THEME_STORAGE_KEY = "ao-bridge.theme";
  const DEFAULT_THEME = "nebula";
  const THEMES = [
    { id: "nebula", label: "Nebula" },
    { id: "editorial", label: "Editorial" },
    { id: "aurora", label: "Aurora" },
    { id: "glass", label: "Glass" },
    { id: "sky", label: "Sky" },
  ];
  // Ids persisted by older releases: mapped to their current id, or dropped
  // to the default when the theme was removed. "air" → "sky" (v0.8.4
  // rename); "waves" / "waves-discovery" were REMOVED in v0.8.4 and fall
  // back to nebula via the unknown-id path below.
  const LEGACY_THEME_IDS = { air: "sky" };
  // Anything unknown in storage (first run, a removed id, hand-edited
  // localStorage) falls back to the default instead of breaking the board.
  function normalizeTheme(id) {
    const mapped = LEGACY_THEME_IDS[id] || id;
    return THEMES.some(function (t) { return t.id === mapped; }) ? mapped : DEFAULT_THEME;
  }
  function readStoredTheme() {
    try { return normalizeTheme(window.localStorage.getItem(THEME_STORAGE_KEY)); }
    catch (_e) { return DEFAULT_THEME; }
  }
  function writeStoredTheme(id) {
    try { window.localStorage.setItem(THEME_STORAGE_KEY, normalizeTheme(id)); } catch (_e) {}
  }
  // === theme layer: end ===

  // Per-harness icon map (assets/harness-icons.json, copied from the AO
  // desktop app). Fetched once, cached at module scope.
  let iconMapPromise = null;
  function loadIconMap() {
    if (!iconMapPromise) {
      iconMapPromise = fetch(ASSET_BASE + "/assets/harness-icons.json")
        .then(function (r) { return r.ok ? r.json() : {}; })
        .catch(function () { return {}; });
    }
    return iconMapPromise;
  }

  function useHarnessIcons() {
    const [map, setMap] = useState(null);
    useEffect(function () {
      let alive = true;
      loadIconMap().then(function (m) { if (alive) setMap(m || {}); });
      return function () { alive = false; };
    }, []);
    return map;
  }

  // Vendored xterm.js (@xterm/xterm 5.5.0 + @xterm/addon-fit 0.10.0, MIT —
  // dist/vendor/ with the licenses kept alongside). Chosen over riotermjs
  // (Rio's wasm engine, 0.1.x): the plugin has no build step, so it needs
  // plain-script UMD builds and a proven fit/resize API — rioterm ships an
  // ESM+wasm graph with no stable fit story yet. xterm.js is also exactly
  // what the AO desktop drives against this same mux, so protocol behavior
  // (full repaint per attach, server-pushed authoritative resizes) matches.
  // Loaded lazily on first Terminal-tab activation; nothing hits a CDN.
  let xtermLibPromise = null;
  function loadTag(tag, attrs) {
    return new Promise(function (resolve, reject) {
      const el = document.createElement(tag);
      Object.keys(attrs).forEach(function (k) { el.setAttribute(k, attrs[k]); });
      el.onload = function () { resolve(); };
      el.onerror = function () { el.remove(); reject(new Error("failed to load " + (attrs.src || attrs.href))); };
      document.head.appendChild(el);
    });
  }
  function loadXterm() {
    if (!xtermLibPromise) {
      xtermLibPromise = Promise.all([
        loadTag("link", { rel: "stylesheet", href: ASSET_BASE + "/dist/vendor/xterm.css" }),
        loadTag("script", { src: ASSET_BASE + "/dist/vendor/xterm.js" }),
        loadTag("script", { src: ASSET_BASE + "/dist/vendor/addon-fit.js" }),
      ]).then(function () {
        if (!window.Terminal || !window.FitAddon || !window.FitAddon.FitAddon) {
          throw new Error("xterm.js did not register its globals");
        }
        return { Terminal: window.Terminal, FitAddon: window.FitAddon.FitAddon };
      });
      xtermLibPromise.catch(function () { xtermLibPromise = null; });
    }
    return xtermLibPromise;
  }

  // Disk-served manifest version: what a fresh page load WOULD run. Compared
  // against the backend's import-time pluginVersion (board payloads, error
  // payloads, WS frames): the dashboard imports plugin_api.py ONCE at
  // startup and never re-imports after a plugin update, while this file and
  // the manifest are served from disk per request — so a mismatch means this
  // UI is newer than the process serving it. Surface a reload banner instead
  // of misbehaving silently.
  let manifestVersionPromise = null;
  function loadManifestVersion() {
    if (!manifestVersionPromise) {
      manifestVersionPromise = fetch(ASSET_BASE + "/manifest.json")
        .then(function (r) { return r.ok ? r.json() : {}; })
        .catch(function () { return {}; });
    }
    return manifestVersionPromise;
  }

  function useManifestVersion() {
    const [v, setV] = useState(null);
    useEffect(function () {
      let alive = true;
      loadManifestVersion().then(function (m) { if (alive) setV((m && m.version) || ""); });
      return function () { alive = false; };
    }, []);
    return v;
  }

  // Relay diagnostics (v0.3.4+ backend): polled cheaply so the "delivery not
  // configured" banner appears/disappears without a page reload. Best-effort —
  // a failed diagnostics fetch just means no banner, and a pre-0.3.4 backend
  // (no `delivery` in the payload) must never banner either.
  function useRelayStatus() {
    const [relay, setRelay] = useState(null);
    useEffect(function () {
      let alive = true;
      function load() {
        authedFetchJson(API + "/relay").then(function (body) {
          if (alive) setRelay(body);
        }).catch(function (_e) { /* diagnostics are optional surface */ });
      }
      load();
      const t = window.setInterval(load, 30000);
      return function () { alive = false; window.clearInterval(t); };
    }, []);
    return relay;
  }

  function harnessIconSrc(map, harness) {
    if (!map || !harness) return null;
    const v = map[harness] || map._fallback;
    return v ? ASSET_BASE + "/" + v : null;
  }

  // Recency buckets for the board filter; ms=0 means "no time bound".
  const RECENCY_OPTIONS = [
    { id: "1h", label: "Last hour", ms: 3600e3 },
    { id: "24h", label: "Last 24h", ms: 86400e3 },
    { id: "7d", label: "Last 7 days", ms: 7 * 86400e3 },
    { id: "all", label: "All time", ms: 0 },
  ];

  // Honest activity state (v0.5.1): "active" means a live turn, nothing else.
  // The backend derives card.state; deriveState is the fallback for a stale
  // backend — plugin_api.py imports once at server startup while this file is
  // served fresh from disk, so the pair can skew across a plugin update.
  const STATE_LABELS = {
    working: "Working", idle: "Idle", awaiting_pr: "Awaiting PR",
    exited: "Exited", archived: "Archived",
  };

  function deriveState(card) {
    // Mirrors backend state_for: "exited" is its own honest state — an
    // exited session is resumable, not archived (v0.8.2; "Archived" here
    // made live workers read as dead). A daemon-archive-column row is
    // archived too — the desktop predicate's kanbanColumn clause.
    const st = (card.status || "").toLowerCase();
    if (st === "terminated" || card.column === "archive") return "archived";
    if (st === "exited") return "exited";
    if (st === "working") return "working";
    if ((card.displayStatus || "").toLowerCase().indexOf("awaiting pr") !== -1) return "awaiting_pr";
    return "idle";
  }

  function stateOf(card) {
    return STATE_LABELS[card.state] ? card.state : deriveState(card);
  }

  // Column/strip counter split, e.g. "2 working · 3 idle" — omitted when every
  // card is working (the count pill alone then means exactly that).
  function splitLabel(cards) {
    if (!cards.length) return "";
    const counts = {};
    cards.forEach(function (c) {
      const s = stateOf(c);
      counts[s] = (counts[s] || 0) + 1;
    });
    const distinct = Object.keys(counts);
    if (distinct.length === 1 && distinct[0] === "working") return "";
    const order = ["working", "awaiting_pr", "idle", "exited", "archived"];
    return order
      .filter(function (s) { return counts[s]; })
      .map(function (s) { return counts[s] + " " + STATE_LABELS[s].toLowerCase(); })
      .join(" · ");
  }

  // Always use the local implementation: the host SDK's timeAgo expects a
  // different time format and rendered "NaNd ago" on live cards.
  function timeAgo(iso) {
    if (!iso) return "";
    const secs = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
    if (!isFinite(secs)) return "";
    if (secs < 60) return "just now";
    if (secs < 3600) return Math.floor(secs / 60) + "m ago";
    if (secs < 86400) return Math.floor(secs / 3600) + "h ago";
    return Math.floor(secs / 86400) + "d ago";
  }

  // Parse a plugin-API failure into { kind, message, baseUrl } — typed
  // payloads ({ kind, retryable, message, baseUrl }) from the v0.2.8+ backend,
  // degraded to the raw text for older backends.
  function parseApiError(err) {
    const raw = (err && err.message) ? String(err.message) : String(err || "");
    const m = raw.match(/^(\d{3}):\s*(.*)$/s);
    const body = m ? m[2] : raw;
    try {
      const parsed = JSON.parse(body);
      if (parsed && parsed.detail) {
        if (typeof parsed.detail === "object") {
          return {
            kind: parsed.detail.kind || "error",
            message: parsed.detail.kind === "daemon_unreachable"
              ? DAEMON_DOWN_MSG
              : String(parsed.detail.message || parsed.detail.kind || "AO daemon error"),
            baseUrl: parsed.detail.baseUrl || "",
          };
        }
        return { kind: "error", message: String(parsed.detail) };
      }
    } catch (_e) { /* not JSON */ }
    return { kind: "error", message: body || raw };
  }

  function parseApiErrorMessage(err) {
    return parseApiError(err).message;
  }

  // Canonical AO desktop-app deep link; each path segment percent-encoded so
  // ids can't reshape the link. The scheme is handled by the AO app — a page
  // can't script the launch, the browser/OS hands it off.
  function aoDeepLink(projectId, sessionId) {
    const sid = encodeURIComponent(sessionId || "");
    return projectId
      ? AO_DEEP_LINK_SCHEME + "://sessions/" + encodeURIComponent(projectId) + "/" + sid
      : AO_DEEP_LINK_SCHEME + "://sessions/" + sid;
  }

  // Interim open-in-AO flow (backend flag open_in_ao_links, default OFF):
  // AO's open-url handler drops session deep links today — it only routes the
  // orchestrator auth callback and billing hosts — so the link opens/focuses
  // the app WITHOUT navigating to the session. The click therefore copies the
  // session name/id to the clipboard first: paste into AO's Search (⌘K) to
  // jump. When upstream AO ships session deep links, the same button becomes
  // true navigation — the URL builder above already emits the handled shape.
  const OPEN_IN_AO_HINT = "AO opens — paste the session id into Search (⌘K) to jump to it";

  function openInAO(e, projectId, sessionId, displayName) {
    e.preventDefault();
    e.stopPropagation();
    const text = displayName && displayName !== sessionId
      ? displayName + " (" + sessionId + ")"
      : sessionId;
    if (navigator.clipboard && navigator.clipboard.writeText) {
      // Copy failure (denied permission, unfocused tab) must not block the
      // launch — the app still opens; the tooltip is the fallback instruction.
      navigator.clipboard.writeText(text).catch(function () {});
    }
    window.location.href = aoDeepLink(projectId, sessionId);
  }

  function OpenInAO(props) {
    return h("a", {
      className: props.className || "ao-btn ao-btn-ghost ao-card-open",
      href: aoDeepLink(props.projectId, props.sessionId),
      title: OPEN_IN_AO_HINT,
      onClick: function (e) {
        openInAO(e, props.projectId, props.sessionId, props.displayName);
      },
    }, props.label || "Open in AO", h(ExtIcon));
  }

  function ExtIcon() {
    return h("svg", {
      className: "ao-ext-icon", width: 12, height: 12, viewBox: "0 0 24 24",
      fill: "none", stroke: "currentColor", strokeWidth: 2,
      strokeLinecap: "round", strokeLinejoin: "round", "aria-hidden": true,
    },
      h("path", { d: "M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" }),
      h("polyline", { points: "15 3 21 3 21 9" }),
      h("line", { x1: 10, y1: 14, x2: 21, y2: 3 }));
  }

  function authedFetchJson(url, options) {
    return (SDK.authedFetch || fetch)(url, options).then(function (resp) {
      return resp.text().then(function (txt) {
        if (!resp.ok) {
          let detail = txt;
          // Re-envelope typed detail objects so parseApiErrorMessage can
          // read them (a bare object stringifies as "[object Object]").
          try {
            const j = JSON.parse(txt);
            if (j && j.detail !== undefined && j.detail !== null) detail = JSON.stringify({ detail: j.detail });
          } catch (_e) { /* raw */ }
          throw new Error(resp.status + ": " + detail);
        }
        try { return JSON.parse(txt); } catch (_e) { return {}; }
      });
    });
  }

  // --- Column presentation -------------------------------------------------

  const COLUMN_CLASS = {
    queued: "queued", running: "running", review: "review",
    blocked: "blocked", done: "done", archive: "archive",
  };

  // Honest per-session state chip — AO's displayStatus rides along as the
  // tooltip nuance ("Awaiting PR", "Needs review", …).
  function StatusChip(props) {
    const state = stateOf(props.card);
    const label = STATE_LABELS[state] || props.card.displayStatus || props.card.status;
    if (!label) return null;
    return h("span", {
      className: "ao-chip ao-chip-status ao-chip-state-" + state,
      title: props.card.displayStatus || props.card.status || label,
    }, label);
  }

  function HarnessChip(props) {
    if (!props.harness) return null;
    const src = harnessIconSrc(props.iconMap, props.harness);
    return h("span", { className: "ao-chip ao-chip-harness", title: props.harness },
      src ? h("img", { className: "ao-chip-icon", src: src, alt: "", loading: "lazy" }) : null,
      props.harness);
  }

  // PR chips with the lifecycle state the daemon reports (open/draft/merged/
  // closed + unresolved-comment flag), matching the AO card's state-grouped
  // PR presentation as far as the session row's facts reach.
  const PR_STATE_CLASS = {
    open: "is-open", draft: "is-draft", merged: "is-merged", closed: "is-closed",
  };

  function PrLinks(props) {
    const prs = props.prs || [];
    if (!prs.length) return null;
    return h("div", { className: "ao-card-prs" },
      prs.slice(0, 3).map(function (pr, i) {
        const state = (pr.state || "").toLowerCase();
        return h("a", {
          key: i,
          className: "ao-chip ao-chip-pr" + (PR_STATE_CLASS[state] ? " " + PR_STATE_CLASS[state] : ""),
          href: pr.url,
          target: "_blank", rel: "noreferrer",
          title: prStateTitle(pr),
          onClick: function (e) { e.stopPropagation(); },
        },
          "#" + (pr.number != null ? pr.number : "?"),
          state ? h("span", { className: "ao-pr-state" }, state) : null,
          pr.reviewComments ? h("span", { className: "ao-pr-comments", title: "unresolved review comments" }, "💬") : null);
      })
    );
  }

  function prStateTitle(pr) {
    const parts = [];
    if (pr.state) parts.push(pr.state);
    if (pr.ci && pr.ci !== "unknown") parts.push("CI " + pr.ci);
    if (pr.review) parts.push(String(pr.review).replace(/_/g, " "));
    if (pr.mergeability && pr.mergeability !== "unknown") parts.push(pr.mergeability);
    return "PR" + (pr.number != null ? " #" + pr.number : "") +
      (parts.length ? " — " + parts.join(" · ") : "");
  }

  function branchStateTitle(bs) {
    if (!bs) return "";
    const parts = [];
    if (bs.commits != null) parts.push(bs.commits + (bs.commits === 1 ? " commit" : " commits"));
    if (bs.unpushed != null) parts.push(bs.unpushed + " unpushed");
    return parts.join(" · ");
  }

  // Card footer metrics, AO formats (server-precomputed): live process usage
  // "240 MB" / "1.4 GB · 82%" while active, and the token/cost line from the
  // daemon's usage endpoint — the figures the AO app shows for the same card.
  function UsageChip(props) {
    const usage = props.card.usage;
    if (!usage || !usage.label) return null;
    return h("span", {
      className: "ao-chip ao-chip-usage",
      title: "Estimated usage" + (usage.incomplete ? " (incomplete — usage still settling)" : ""),
    }, usage.label);
  }

  function ResourceChip(props) {
    const res = props.card.resource;
    if (!res || !res.label) return null;
    return h("span", { className: "ao-chip ao-chip-resource", title: "Live process usage" }, res.label);
  }

  // Live plan (wire truth: last plan activity in the conversation snapshot is
  // the current plan). The card shows the step being worked on, one line.
  function planCurrentStep(steps) {
    for (const st of steps) if (st.status === "in_progress") return st;
    for (const st of steps) if (st.status === "pending") return st;
    return null;
  }

  function PlanLine(props) {
    const steps = props.plan.steps || [];
    if (!steps.length) return null;
    const cur = planCurrentStep(steps);
    const done = steps.filter(function (s) { return s.status === "completed"; }).length;
    return h("div", {
      className: "ao-card-plan",
      title: steps.map(function (s) { return s.text; }).join("\n"),
    },
      h("span", { className: "ao-card-plan-step" }, cur ? cur.text : ""),
      h("span", { className: "ao-card-plan-count" }, done + "/" + steps.length + " steps"));
  }

  // Lane order (AO board semantics): pinned, then attention — displayStatus
  // Blocked / CI failing / Changes requested is AO's needs-attention set —
  // then most-recent activity. Mirrors the backend's sort_column_cards so the
  // order also holds against an older backend that ships unsorted cards.
  const ATTENTION_DISPLAY_STATUSES = {
    "Blocked": true, "CI failing": true, "Changes requested": true,
  };

  function cardLaneOrder(a, b) {
    const pa = a.isPinned ? 0 : 1, pb = b.isPinned ? 0 : 1;
    if (pa !== pb) return pa - pb;
    const aa = ATTENTION_DISPLAY_STATUSES[a.displayStatus] ? 0 : 1;
    const ab = ATTENTION_DISPLAY_STATUSES[b.displayStatus] ? 0 : 1;
    if (aa !== ab) return aa - ab;
    return (b.updatedAt || "").localeCompare(a.updatedAt || "");
  }

  function Card(props) {
    const card = props.card;
    const [busy, setBusy] = useState(null);
    const [err, setErr] = useState(null);

    function action(kind, e) {
      e.stopPropagation();
      setBusy(kind); setErr(null);
      const url = kind === "kill"
        ? API + "/sessions/" + encodeURIComponent(card.id) + "/kill"
        : null;
      authedFetchJson(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" })
        .then(function () { props.onChanged(); })
        .catch(function (e2) { setErr(parseApiErrorMessage(e2)); })
        .finally(function () { setBusy(null); });
    }

    return h("div", {
      className: "ao-card ao-card-" + (COLUMN_CLASS[card.column] || ""),
      onClick: function () { props.onOpen(card); },
      title: "Click for session detail",
    },
      h("div", { className: "ao-card-top" },
        h("span", { className: "ao-card-project", title: card.projectId || "standalone" }, card.project),
        card.isPinned ? h("span", { className: "ao-card-pin", title: "Pinned" }, "📌") : null,
        // The card's clock is last activity (AO semantics), not creation age.
        h("span", {
          className: "ao-card-age",
          title: card.updatedAt ? "Updated " + card.updatedAt : "",
        }, timeAgo(card.updatedAt))
      ),
      h("div", { className: "ao-card-name", title: card.displayName }, card.displayName),
      card.plan && card.status === "working" ? h(PlanLine, { plan: card.plan }) : null,
      h("div", { className: "ao-card-meta" },
        h(HarnessChip, { harness: card.harness, iconMap: props.iconMap }),
        h(StatusChip, { card: card }),
        card.branch ? h("span", { className: "ao-chip ao-chip-branch", title: card.branch }, card.branch) : null
      ),
      h(PrLinks, { prs: card.prs }),
      (card.resource && card.resource.label) || (card.usage && card.usage.label)
        ? h("div", { className: "ao-card-metrics" },
            h(ResourceChip, { card: card }),
            h(UsageChip, { card: card }))
        : null,
      err ? h("div", { className: "ao-card-error" }, err) : null,
      h("div", { className: "ao-card-actions" },
        props.openLinks
          ? h(OpenInAO, { projectId: card.projectId, sessionId: card.id,
                          displayName: card.displayName })
          : null,
        card.column !== "archive" && card.column !== "done"
          ? h("button", {
              className: "ao-btn ao-btn-ghost", disabled: !!busy,
              title: "Open the session and steer it — one click to message",
              onClick: function (e) {
                e.stopPropagation();
                props.onMessage(card);
              },
            }, "Message")
          : null,
        card.column !== "archive"
          ? h("button", {
              className: "ao-btn ao-btn-danger", disabled: !!busy,
              title: "Archive this AO session",
              onClick: function (e) {
                e.stopPropagation();
                if (window.confirm("Archive AO session \"" + card.displayName + "\"? This terminates the agent session.")) action("kill", e);
              },
            }, busy === "kill" ? "Archiving…" : "Archive")
          : null
      )
    );
  }

  function Column(props) {
    const cards = props.cards;
    const split = splitLabel(cards);
    return h("div", { className: "ao-column ao-column-" + (COLUMN_CLASS[props.id] || "") },
      h("div", { className: "ao-column-head" },
        h("span", { className: "ao-column-label" }, props.label),
        h("span", { className: "ao-column-count" }, cards.length),
        split ? h("span", { className: "ao-column-split" }, split) : null
      ),
      h("div", { className: "ao-column-body" },
        cards.length === 0
          ? h("div", { className: "ao-column-empty" }, "No sessions")
          : cards.map(function (c) {
              return h(Card, { key: c.id, card: c, iconMap: props.iconMap, openLinks: props.openLinks, onChanged: props.onChanged, onMessage: props.onMessage, onOpen: props.onOpen });
            }))
    );
  }

  // --- Orchestrators strip -------------------------------------------------
  // Orchestrator sessions (kind=orchestrator) coordinate workers; they don't
  // carry worker state, so instead of polluting the Kanban columns they get
  // one quiet strip above the board: a compact row per orchestrator (live
  // dot, name, project, harness, display status, age, open-in-AO). Rows share
  // the worker cards' filters and open the same detail sheet. Hidden entirely
  // when nothing matches — it never announces an empty section.

  // The strip's status dot follows the honest state enum (not AO's green
  // alive-dot): working pulses, awaiting PR glows amber, idle sits muted,
  // archived dims out — same classes the worker cards' chips use.
  function OrchDot(props) {
    const kind = stateOf(props.card);
    const label = STATE_LABELS[kind] || props.card.displayStatus || props.card.status;
    return h("span", {
      className: "ao-orch-dot ao-orch-dot-" + kind,
      title: label,
      "aria-label": "status: " + label,
    });
  }

  function OrchestratorStrip(props) {
    const orchs = props.orchestrators;
    const split = splitLabel(orchs);
    return h("div", { className: "ao-orch-strip" },
      h("div", { className: "ao-orch-head" },
        h("span", { className: "ao-orch-title" }, "Orchestrators"),
        h("span", { className: "ao-orch-count" }, orchs.length),
        split ? h("span", { className: "ao-orch-split" }, split) : null),
      h("div", { className: "ao-orch-rows" },
        orchs.map(function (c) {
          return h("div", {
            key: c.id,
            className: "ao-orch-row",
            onClick: function () { props.onOpen(c); },
            title: "Click for session detail",
          },
            h(OrchDot, { card: c }),
            h("span", { className: "ao-orch-name", title: c.displayName }, c.displayName),
            h(HarnessChip, { harness: c.harness, iconMap: props.iconMap }),
            h("span", { className: "ao-orch-project", title: c.projectId || "standalone" }, c.project),
            h("span", { className: "ao-orch-status" },
              c.displayStatus || c.status || ""),
            h("span", { className: "ao-orch-age", title: c.updatedAt }, timeAgo(c.updatedAt)),
            props.openLinks
              ? h(OpenInAO, { projectId: c.projectId, sessionId: c.id,
                              displayName: c.displayName,
                              className: "ao-chip ao-chip-pr ao-orch-open" })
              : null);
        })
      )
    );
  }

  // --- Archive strip ---------------------------------------------------------
  // Terminated sessions (backend "archived" population, split out of the
  // columns like orchestrators) get the same quiet-strip treatment: a compact
  // row per session — dim dot, name, project, harness, age — plus the one
  // action a dead session can take: Restore, which rides the daemon's
  // POST /sessions/{id}/restore (verified in the AO backend Controllers; a
  // non-terminated session is refused with a typed conflict). A restored
  // session relaunches and re-enters the board on the next snapshot. The
  // strip hides entirely when nothing matches — it never announces an empty
  // section — and stays visible under "Active only" (that toggle aims at the
  // live board; hiding the archive here would defeat the strip's purpose).

  function ArchiveStrip(props) {
    const rows = props.archived;
    const [busyId, setBusyId] = useState(null);
    const [errById, setErrById] = useState({});
    // Collapsed by default (v0.8.3): AO desktop's board keeps its archive as
    // a collapsed drawer; ours rendered every row up top and the board read
    // as "everything is archived". The count stays visible on the header.
    const [expanded, setExpanded] = useState(function () {
      try { return window.localStorage.getItem("ao-bridge.archExpanded") === "1"; }
      catch (_e) { return false; }
    });
    function toggleExpanded() {
      setExpanded(function (prev) {
        const next = !prev;
        try { window.localStorage.setItem("ao-bridge.archExpanded", next ? "1" : "0"); } catch (_e) {}
        return next;
      });
    }
    function restore(row) {
      if (!window.confirm("Restore \"" + row.displayName + "\"? The session relaunches and re-enters the board.")) return;
      setBusyId(row.id); setErrById({});
      authedFetchJson(API + "/sessions/" + encodeURIComponent(row.id) + "/restore",
                      { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" })
        .then(function () { props.onChanged(); })
        .catch(function (e) {
          setErrById(function (prev) {
            const next = {};
            Object.keys(prev).forEach(function (k) { next[k] = prev[k]; });
            next[row.id] = parseApiErrorMessage(e);
            return next;
          });
        })
        .finally(function () { setBusyId(null); });
    }
    return h("div", { className: "ao-orch-strip ao-arch-strip" + (expanded ? " is-expanded" : "") },
      h("button", {
        type: "button",
        className: "ao-arch-toggle",
        title: expanded ? "Collapse the archive" : "Show the archive (" + rows.length + " sessions)",
        "aria-expanded": expanded,
        onClick: toggleExpanded,
      },
        h("span", { className: "ao-arch-chevron" + (expanded ? " is-open" : "") }, "▸"),
        h("span", { className: "ao-orch-title ao-arch-title" }, "Archive"),
        h("span", { className: "ao-orch-count" }, rows.length)),
      expanded ? h("div", { className: "ao-orch-rows" },
        rows.map(function (c) {
          const err = errById[c.id];
          return h("div", {
            key: c.id,
            className: "ao-orch-row",
            onClick: function () { props.onOpen(c); },
            title: "Click for session detail",
          },
            h("span", { className: "ao-orch-dot ao-orch-dot-" + stateOf(c),
                        title: (STATE_LABELS[stateOf(c)] || c.displayStatus || c.status || "archived"),
                        "aria-label": "status: archived" }),
            h("span", { className: "ao-orch-name", title: c.displayName }, c.displayName),
            h(HarnessChip, { harness: c.harness, iconMap: props.iconMap }),
            h("span", { className: "ao-orch-project", title: c.projectId || "standalone" }, c.project),
            err
              ? h("span", { className: "ao-orch-status ao-arch-error", title: err }, err)
              : h("span", { className: "ao-orch-status" },
                  (c.displayStatus || c.status || "archived")),
            h("span", { className: "ao-orch-age", title: c.updatedAt }, timeAgo(c.updatedAt)),
            h("button", {
              className: "ao-btn ao-btn-ghost ao-arch-restore",
              disabled: busyId === c.id,
              title: "Relaunch this archived session — it re-enters the board",
              onClick: function (e) { e.stopPropagation(); restore(c); },
            }, busyId === c.id ? "Restoring…" : "Restore"),
            props.openLinks
              ? h(OpenInAO, { projectId: c.projectId, sessionId: c.id,
                              displayName: c.displayName,
                              className: "ao-chip ao-chip-pr ao-orch-open" })
              : null);
        })
      ) : null
    );
  }

  // --- Spawn form (modal sheet) -------------------------------------------

  function SpawnForm(props) {
    const [projectId, setProjectId] = useState(props.projects[0] ? props.projects[0].id : "");
    const [harness, setHarness] = useState("hermes");
    const [name, setName] = useState("");
    const [prompt, setPrompt] = useState("");
    const [mode, setMode] = useState("chat");
    const [branch, setBranch] = useState("");
    const [model, setModel] = useState("");
    const [effort, setEffort] = useState("");
    const [approvalMode, setApprovalMode] = useState("");
    const [busy, setBusy] = useState(false);
    const [err, setErr] = useState(null);

    function submit(e) {
      e.preventDefault();
      if (busy) return;
      setBusy(true); setErr(null);
      authedFetchJson(API + "/spawn", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          projectId: projectId, harness: harness, displayName: name, prompt: prompt,
          mode: mode, branch: branch, model: model, effort: effort, approvalMode: approvalMode,
        }),
      })
        .then(function (resp) {
          props.onClose();
          props.onChanged(resp.session);
        })
        .catch(function (e2) { setErr(parseApiErrorMessage(e2)); })
        .finally(function () { setBusy(false); });
    }

    return h("div", { className: "ao-overlay", onClick: function (e) { if (e.target === e.currentTarget) props.onClose(); } },
      h("form", { className: "ao-sheet ao-sheet-form", onSubmit: submit },
        h("div", { className: "ao-sheet-title" }, "Spawn AO worker"),
        h("label", { className: "ao-field" },
          h("span", { className: "ao-field-label" }, "Project"),
          h("select", { className: "ao-input", value: projectId,
                        onChange: function (e) { setProjectId(e.target.value); } },
            (props.projects || []).map(function (p) {
              return h("option", { key: p.id, value: p.id },
                p.name + (p.folderMissing ? " (missing dir)" : ""));
            })
          )
        ),
        h("label", { className: "ao-field" },
          h("span", { className: "ao-field-label" }, "Harness"),
          h("div", { className: "ao-harness-select" },
            (function () {
              const src = harnessIconSrc(props.iconMap, harness);
              return src ? h("img", { className: "ao-harness-icon", src: src, alt: harness, title: harness }) : null;
            })(),
            h("select", { className: "ao-input", value: harness,
                          onChange: function (e) { setHarness(e.target.value); } },
              (props.harnesses || ["hermes"]).map(function (x) {
                return h("option", { key: x, value: x }, x);
              })
            )
          )
        ),
        h("label", { className: "ao-field" },
          h("span", { className: "ao-field-label" }, "Name"),
          h("input", { className: "ao-input", value: name, placeholder: "session display name (optional if a project is picked)",
                       onChange: function (e) { setName(e.target.value); } })
        ),
        h("label", { className: "ao-field" },
          h("span", { className: "ao-field-label" }, "Prompt"),
          h("textarea", { className: "ao-input ao-textarea", rows: 6, value: prompt,
                          placeholder: "Initial task for the agent (optional)",
                          onChange: function (e) { setPrompt(e.target.value); } })
        ),
        h("div", { className: "ao-field-grid" },
          h("label", { className: "ao-field" },
            h("span", { className: "ao-field-label" }, "Interface"),
            h("select", { className: "ao-input", value: mode, title: "chat or TUI",
                          onChange: function (e) { setMode(e.target.value); } },
              ["chat", "tui"].map(function (m) {
                return h("option", { key: m, value: m }, m);
              }))
          ),
          h("label", { className: "ao-field" },
            h("span", { className: "ao-field-label" }, "Branch"),
            h("input", { className: "ao-input", value: branch,
                         placeholder: "default",
                         title: "Branch to start the worktree from (optional)",
                         onChange: function (e) { setBranch(e.target.value); } })
          ),
          h("label", { className: "ao-field" },
            h("span", { className: "ao-field-label" }, "Model"),
            h("input", { className: "ao-input", value: model,
                         placeholder: "harness default",
                         title: "Model id the harness should run (optional)",
                         onChange: function (e) { setModel(e.target.value); } })
          ),
          h("label", { className: "ao-field" },
            h("span", { className: "ao-field-label" }, "Effort"),
            h("input", { className: "ao-input", value: effort,
                         placeholder: "harness default",
                         title: "Reasoning effort the model accepts, e.g. low/medium/high (optional)",
                         onChange: function (e) { setEffort(e.target.value); } })
          ),
          h("label", { className: "ao-field" },
            h("span", { className: "ao-field-label" }, "Approvals"),
            h("select", { className: "ao-input", value: approvalMode,
                          title: "How the agent handles permission prompts",
                          onChange: function (e) { setApprovalMode(e.target.value); } },
              [["", "harness default"],
               ["default", "default"],
               ["accept-edits", "accept-edits"],
               ["auto", "auto"],
               ["bypass-permissions", "bypass-permissions"]].map(function (m) {
                return h("option", { key: m[0], value: m[0] }, m[1]);
              }))
          )
        ),
        err ? h("div", { className: "ao-sheet-error" }, err) : null,
        h("div", { className: "ao-sheet-actions" },
          h("button", { type: "button", className: "ao-btn ao-btn-ghost", onClick: props.onClose }, "Cancel"),
          h("button", { type: "submit", className: "ao-btn ao-btn-primary", disabled: busy },
            busy ? "Spawning…" : "Spawn")
        )
      )
    );
  }

  // --- Detail-sheet composer -------------------------------------------------
  // Messages from the board ride the daemon's steer-or-send route (wire truth
  // probed live, 2026-11-09): a working session is steered mid-turn, an idle
  // one gets a new turn — AO-native semantics from one route. The daemon
  // records origin=human, so board messages render as "End user" and arrive
  // in the transcript through the sheet's live WS follow right after the send.
  //
  // Images (v0.6.0): the daemon's steer-or-send accepts native image blocks
  // ({"mimeType", "data"}, standard base64 — verified in the AO backend
  // Controllers), so pasted/dropped/picked images ride along as attachments.
  // They preview above the input until send. Text stays REQUIRED: the daemon
  // refuses a bare-attachment steer (CHAT_STEER_TEXT_REQUIRED), so the local
  // limits mirror that honestly (8 images, 10 MB each, 25 MB total, raster
  // image/* only — SVG is blocked daemon-side as active content).

  const COMPOSER_MAX_IMAGES = 8;
  const COMPOSER_IMAGE_MAX_BYTES = 10 * 1024 * 1024;
  const COMPOSER_IMAGES_MAX_TOTAL_BYTES = 25 * 1024 * 1024;

  function isImageFile(f) {
    return (f && (f.type || "").indexOf("image/") === 0 && f.type !== "image/svg+xml");
  }

  function readImageAsBase64(file) {
    return new Promise(function (resolve, reject) {
      const r = new FileReader();
      r.onload = function () {
        const dataUrl = String(r.result || "");
        const comma = dataUrl.indexOf(",");
        resolve(comma === -1 ? "" : dataUrl.slice(comma + 1));
      };
      r.onerror = function () { reject(new Error("Could not read " + (file.name || "image"))); };
      r.readAsDataURL(file);
    });
  }

  function Composer(props) {
    const [text, setText] = useState("");
    const [images, setImages] = useState([]);
    const [busy, setBusy] = useState(false);
    const [err, setErr] = useState(null);
    const [sentNote, setSentNote] = useState("");
    const taRef = useRef(null);
    const fileRef = useRef(null);
    const imagesRef = useRef([]);

    function setImagesAndRef(next) {
      imagesRef.current = next;
      setImages(next);
    }

    function dropImages(list) {
      list.forEach(function (im) { if (im.url) URL.revokeObjectURL(im.url); });
    }

    // One-click steer: the card "Message" affordance opens the sheet with the
    // composer ready to type.
    useEffect(function () {
      if (props.focus && taRef.current) taRef.current.focus();
    }, [props.focus]);

    // The confirmation is feedback, not state — it fades so the next send
    // starts from a clean slate.
    useEffect(function () {
      if (!sentNote) return;
      const t = window.setTimeout(function () { setSentNote(""); }, 6000);
      return function () { window.clearTimeout(t); };
    }, [sentNote]);

    // Object URLs outlive the send — release them when the sheet closes.
    useEffect(function () {
      return function () { dropImages(imagesRef.current); };
    }, []);

    function addFiles(fileList) {
      const files = Array.from(fileList || []).filter(isImageFile);
      if (!files.length) return;
      setErr(null);
      const room = COMPOSER_MAX_IMAGES - imagesRef.current.length;
      if (files.length > room) {
        setErr("At most " + COMPOSER_MAX_IMAGES + " images per message");
      }
      const take = files.slice(0, Math.max(0, room));
      const tooBig = take.find(function (f) { return f.size > COMPOSER_IMAGE_MAX_BYTES; });
      if (tooBig) { setErr("Image \"" + (tooBig.name || "image") + "\" is too large (max 10 MB)"); return; }
      const existingBytes = imagesRef.current.reduce(function (sum, im) { return sum + im.bytes; }, 0);
      const addBytes = take.reduce(function (sum, f) { return sum + f.size; }, 0);
      if (existingBytes + addBytes > COMPOSER_IMAGES_MAX_TOTAL_BYTES) {
        setErr("Images are too large in total (max 25 MB)");
        return;
      }
      take.forEach(function (f) {
        readImageAsBase64(f).then(function (data) {
          if (!data) { setErr("Could not read " + (f.name || "image")); return; }
          setImagesAndRef(imagesRef.current.concat([{
            name: f.name || "image", mimeType: f.type, data: data,
            bytes: f.size, url: URL.createObjectURL(f),
          }]));
        }).catch(function (e) { setErr(e.message); });
      });
    }

    function removeImage(idx) {
      const next = imagesRef.current.slice();
      dropImages(next.splice(idx, 1));
      setImagesAndRef(next);
    }

    function clearImages() {
      dropImages(imagesRef.current);
      setImagesAndRef([]);
    }

    function submit() {
      if (busy) return;
      if (!text.trim()) { setErr("Message must not be empty"); return; }
      setBusy(true); setErr(null); setSentNote("");
      authedFetchJson(API + "/sessions/" + encodeURIComponent(props.sessionId) + "/steer", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message: text,
          attachments: imagesRef.current.map(function (im) {
            return { mimeType: im.mimeType, data: im.data };
          }),
        }),
      })
        .then(function (resp) {
          setText("");
          clearImages();
          setSentNote(resp && resp.outcome === "steered"
            ? "Sent — steered into the working turn"
            : "Sent — new turn queued");
        })
        .catch(function (e2) { setErr(parseApiErrorMessage(e2)); })
        .finally(function () { setBusy(false); });
    }

    return h("div", {
      className: "ao-composer",
      // Only image drops are intercepted; a text drop must keep reaching the
      // textarea's own handler.
      onDragOver: function (e) {
        const dt = e.dataTransfer;
        if (dt && Array.from(dt.items || []).some(function (t) {
          return t.kind === "file" && t.type.indexOf("image/") === 0 && t.type !== "image/svg+xml";
        })) e.preventDefault();
      },
      onDrop: function (e) {
        const dt = e.dataTransfer;
        if (!dt) return;
        const hasImages = Array.from(dt.files || []).some(isImageFile);
        if (!hasImages) return;
        e.preventDefault();
        addFiles(dt.files);
      },
    },
      h("textarea", {
        ref: taRef, className: "ao-input ao-textarea ao-composer-input", rows: 2,
        value: text,
        placeholder: props.working
          ? "Steer this session — delivered while it works…"
          : "Message this session…  (paste or drop an image to attach it)",
        onChange: function (e) { setText(e.target.value); },
        onPaste: function (e) {
          const files = Array.from((e.clipboardData && e.clipboardData.items) || [])
            .filter(function (item) { return item.kind === "file"; })
            .map(function (item) { return item.getAsFile(); })
            .filter(isImageFile);
          if (files.length) { e.preventDefault(); addFiles(files); }
        },
        onKeyDown: function (e) {
          if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); submit(); }
        },
      }),
      images.length
        ? h("div", { className: "ao-composer-images" },
            images.map(function (im, i) {
              return h("span", { className: "ao-composer-image", key: i, title: im.name },
                h("img", { src: im.url, alt: im.name }),
                h("button", {
                  className: "ao-composer-image-x", title: "Remove image",
                  "aria-label": "Remove image " + (i + 1),
                  onClick: function () { removeImage(i); },
                }, "×"));
            }))
        : null,
      err ? h("div", { className: "ao-sheet-error" }, err) : null,
      sentNote ? h("div", { className: "ao-composer-sent" }, sentNote) : null,
      h("div", { className: "ao-composer-actions" },
        h("span", { className: "ao-composer-hint" }, "⌘⏎ to send"),
        h("input", {
          ref: fileRef, type: "file", accept: "image/*", multiple: true,
          style: { display: "none" },
          onChange: function (e) { addFiles(e.target.files); e.target.value = ""; },
        }),
        h("button", {
          className: "ao-btn ao-btn-ghost ao-composer-add",
          title: "Attach images (paste and drag-drop work too)",
          disabled: busy || images.length >= COMPOSER_MAX_IMAGES,
          onClick: function () { if (fileRef.current) fileRef.current.click(); },
        }, "🖼"),
        h("button", { className: "ao-btn ao-btn-primary", disabled: busy || !text.trim(),
                      onClick: submit }, busy ? "Sending…" : "Send")));
  }

  // --- Card detail: message classes -----------------------------------------
  // Three classes over the daemon's wire truth (probed live, 2026-11-09):
  // origin is the reliable provenance signal — "human" for messages typed in
  // the AO app AND for the board's composer (the steer-or-send route records
  // origin=human), "automation" for the legacy REST /send call regardless of
  // the userAuthored flag, "provider" for assistant turns. authoredByUser
  // is self-declared by the sender and proves nothing (an orchestrator can
  // pass userAuthored: true), so it is never used to claim "End user":
  //   worker       — assistant turns (the harness's own output)
  //   user         — origin=human (End user)
  //   orchestrator — everything else non-assistant (Orchestrator / automation)
  const MSG_CLASSES = {
    worker: { label: "Worker", title: "The harness's own output (assistant turns)" },
    user: { label: "End user", title: "Typed by the user — in the AO app or on this board (origin=human)" },
    orchestrator: { label: "Orchestrator / automation", title: "Injected via the legacy queue-only REST /send path (origin=automation), or otherwise not a human or worker turn" },
  };

  function messageClass(m) {
    if ((m.role || "").toLowerCase() === "assistant") return "worker";
    if ((m.origin || "").toLowerCase() === "human") return "user";
    return "orchestrator";
  }

  // Harness transcripts carry leading/trailing blank-line artifacts ("\n\n"
  // before an assistant turn); white-space: pre-wrap would render them as empty
  // bands inside the bubble, so trim the ends for display only — the /detail
  // endpoint stays raw.
  function bubbleText(m) {
    return String(m.text == null ? "" : m.text).replace(/^\s+|\s+$/g, "");
  }

  // --- Tool executions in the transcript ------------------------------------
  // Conversation activities (command | mcp_tool) interleave with messages by
  // their shared ``sequence`` number. Kept visually quiet: one muted line per
  // execution, expandable. ``plan`` activities render in the Plan panel
  // instead; reasoning gets its own ThinkingRow below; system/file_change are
  // not part of the transcript.
  function toolStatusGlyph(a) {
    // Wire truth (probed on the live daemon, 2026-11-09): an in-flight command
    // or tool reports status "pending" (never observed as "running").
    if (a.status === "running" || a.status === "pending")
      return h("span", { className: "ao-tool-spin", title: "running", "aria-label": "running" });
    if (a.status === "failed"
        || (a.activityKind === "command" && a.exitCode != null && a.exitCode !== 0))
      return h("span", {
        className: "ao-tool-status is-fail",
        title: "failed" + (a.exitCode != null ? " (exit " + a.exitCode + ")" : ""),
      }, "✗");
    return h("span", { className: "ao-tool-status is-ok", title: "completed" }, "✓");
  }

  function ToolRow(props) {
    const a = props.activity;
    const [open, setOpen] = useState(false);
    const isCmd = a.activityKind === "command";
    const label = isCmd
      ? (a.summary || a.command || "command")
      : (a.toolName || a.summary || "tool");
    return h("div", { className: "ao-tool-row ao-tool-" + a.activityKind + (open ? " is-open" : "") },
      h("div", {
        className: "ao-tool-head",
        onClick: function () { setOpen(function (p) { return !p; }); },
        title: isCmd ? (a.command || label) : label,
      },
        h("span", { className: "ao-tool-glyph" }, isCmd ? "$" : "⚙"),
        h("span", { className: "ao-tool-label" }, label),
        h("span", { className: "ao-tool-tail" },
          toolStatusGlyph(a),
          h("span", { className: "ao-tool-caret" }, open ? "▾" : "▸"))),
      open ? h("div", { className: "ao-tool-body" },
        isCmd && a.command ? h("pre", { className: "ao-tool-pre" }, a.command) : null,
        isCmd && a.exitCode != null
          ? h("div", { className: "ao-tool-exit" + (a.exitCode ? " is-fail" : "") },
              "exit " + a.exitCode)
          : null,
        !isCmd && a.input ? h("pre", { className: "ao-tool-pre" }, a.input) : null,
        a.output ? h("pre", { className: "ao-tool-pre" },
          a.output + (a.outputTruncated ? "\n…" : "")) : null
      ) : null
    );
  }

  // --- Thinking rows ---------------------------------------------------------
  // Reasoning activities (activityKind "reasoning") are the worker's private
  // thinking, not its speech: they render as a clearly SECONDARY muted row —
  // brain glyph + italic "Thinking" + a one-line preview — collapsed by
  // default, expandable to the full (still muted, italic) text. Never styled
  // like a Worker bubble or a tool row. ``live`` is set by the backend on the
  // single reasoning entry actually being streamed (the last one: mid-turn,
  // EVERY entry of the current turn reads status "running"), and only that
  // row pulses. Thinking is worker output, so it hides with the Worker chip
  // (timelineItems below) rather than having a chip of its own.
  function ThinkingRow(props) {
    const a = props.activity;
    const [open, setOpen] = useState(false);
    const preview = a.preview || "…";
    return h("div", {
      className: "ao-thinking-row" + (a.live ? " is-live" : "") + (open ? " is-open" : ""),
    },
      h("div", {
        className: "ao-thinking-head",
        onClick: function () { setOpen(function (p) { return !p; }); },
        title: "Worker thinking — click to " + (open ? "collapse" : "expand"),
      },
        h("span", { className: "ao-thinking-glyph", "aria-hidden": true }, "🧠"),
        h("span", { className: "ao-thinking-label" }, "Thinking"),
        a.live
          ? h("span", { className: "ao-thinking-pulse", title: "thinking…",
                       "aria-label": "thinking" })
          : null,
        open
          ? null
          : h("span", { className: "ao-thinking-preview", title: preview }, preview),
        h("span", { className: "ao-tool-caret" }, open ? "▾" : "▸")),
      open ? h("div", { className: "ao-thinking-body" },
        a.text ? h("div", { className: "ao-thinking-text" }, a.text) : null,
        a.textTruncated ? h("div", { className: "ao-tool-exit" }, "…truncated") : null
      ) : null
    );
  }

  // Merge transcript messages and tool activities into one sequence-ordered
  // timeline. Message-class filters apply to messages and to the activity
  // kinds that follow them: thinking is worker output (Worker chip), steered
  // guidance is End-user authored (End-user chip) even though it arrives as
  // an activity.
  function timelineItems(transcript, activities, hiddenClasses) {
    const msgs = (transcript || [])
      .filter(function (m) { return !hiddenClasses[messageClass(m)] && bubbleText(m); })
      .map(function (m) { return { kind: "message", seq: m.sequence, item: m }; });
    const acts = (activities || [])
      .filter(function (a) {
        if (a.activityKind === "plan") return false;
        if (a.activityKind === "reasoning" && hiddenClasses.worker) return false;
        if (a.activityKind === "steer") return !hiddenClasses.user;
        return true;
      })
      .map(function (a) { return { kind: "activity", seq: a.sequence, item: a }; });
    return msgs.concat(acts).sort(function (x, y) {
      const a = typeof x.seq === "number" ? x.seq : Infinity;
      const b = typeof y.seq === "number" ? y.seq : Infinity;
      return a - b;
    });
  }

  // --- Hermes session-store enrichment --------------------------------------
  // For hermes-harness sessions whose Hermes session row is found on this
  // machine (worktree-cwd join), the detail payload carries "hermesEnriched":
  // the session's stats plus full-fidelity tool calls (name + args + output)
  // and reasoning that ACP truncates (parity audit, 2026-11-09: 116k vs 442k
  // chars of tool I/O on one session). Rendered as its own section UNDER the
  // AO transcript — the AO view stays the protagonist; enriched rows reuse
  // the same tool-run/thinking components, tinted to mark the provenance.
  function enrichedStatsLine(st) {
    const parts = [];
    if (st.model) parts.push(st.model);
    const toks = [];
    if (st.inputTokens != null) toks.push("in " + Number(st.inputTokens).toLocaleString());
    if (st.outputTokens != null) toks.push("out " + Number(st.outputTokens).toLocaleString());
    if (st.reasoningTokens) toks.push("think " + Number(st.reasoningTokens).toLocaleString());
    if (toks.length) parts.push(toks.join(" / ") + " tok");
    if (st.durationSeconds != null) parts.push(Math.max(1, Math.round(st.durationSeconds / 60)) + " min");
    if (st.estimatedCostUsd != null && st.estimatedCostUsd > 0) parts.push("$" + st.estimatedCostUsd.toFixed(4));
    return parts.join(" · ");
  }

  function EnrichedSection(props) {
    const enr = props.enriched;
    if (!enr || !enr.found) return null;
    const acts = enr.activities || [];
    return h("div", { className: "ao-detail-transcript ao-enriched" },
      h("div", { className: "ao-detail-key" },
        "Hermes session store — full tool I/O" +
        (enr.activitiesTruncated
          ? " (last " + acts.length + " of " + enr.activitiesTotal + ")"
          : "")),
      h("div", { className: "ao-enriched-stats" }, enrichedStatsLine(enr.session || {})),
      acts.length === 0
        ? h("div", { className: "ao-column-empty" }, "No tool activity recorded")
        : acts.map(function (a, i) {
            // Same row components as the AO transcript: reasoning renders as a
            // ThinkingRow (never live — enrichment is recorded history), tool
            // calls as quiet mcp_tool rows with the full stored I/O.
            if (a.kind === "reasoning")
              return h(ThinkingRow, { key: "e" + i, activity: {
                activityKind: "reasoning", status: "completed",
                text: a.text, preview: a.preview,
                textTruncated: a.textTruncated } });
            return h(ToolRow, { key: "e" + i, activity: {
              activityKind: "mcp_tool", status: "completed",
              toolName: a.toolName, input: a.input, output: a.output,
              outputTruncated: a.outputTruncated } });
          }));
  }

  // --- Detail sheet (card click; read-only) --------------------------------
  // --- Terminal view: mux framing + attachment ------------------------------
  //
  // Wire protocol = the AO daemon's terminal multiplexer (GET /mux; AO source
  // backend/internal/terminal/protocol.go, mirrored from the desktop app's
  // lib/terminal-mux.ts): one JSON socket, frames channel-tagged ("ch").
  // "terminal" frames carry PTY bytes base64-encoded (arbitrary bytes, not
  // always UTF-8), keyed by the session's runtime handle. open{id,cols,rows}
  // → opened{id} + full repaint as data{id,data}; data{id,data} = keystrokes
  // in; resize{id,cols,rows} negotiates the shared grid — the daemon pushes
  // its authoritative grid back as a resize frame, which wins; close{id}
  // detaches; ch "system" ping/pong liveness. Input is lease-gated
  // server-side (exclusive session ops answer error frames): we surface that
  // as a view-only banner rather than trying to lock input here — the mux
  // deliberately does NOT arbitrate typing between viewers.

  function bytesToBase64(bytes) {
    let binary = "";
    const chunk = 0x8000;
    for (let i = 0; i < bytes.length; i += chunk) {
      binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
    }
    return btoa(binary);
  }

  function base64ToBytes(b64) {
    const binary = atob(b64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
    return bytes;
  }

  function muxOpenFrame(id, cols, rows) {
    return JSON.stringify({ ch: "terminal", type: "open", id: id, cols: cols, rows: rows });
  }
  function muxDataFrame(id, str) {
    return JSON.stringify({ ch: "terminal", type: "data", id: id, data: bytesToBase64(new TextEncoder().encode(str)) });
  }
  function muxResizeFrame(id, cols, rows) {
    return JSON.stringify({ ch: "terminal", type: "resize", id: id, cols: cols, rows: rows });
  }
  function muxCloseFrame(id) {
    return JSON.stringify({ ch: "terminal", type: "close", id: id });
  }
  function muxPingFrame() {
    return JSON.stringify({ ch: "system", type: "ping" });
  }

  const TERM_PHASE_LABEL = {
    loading: "Loading terminal…",
    connecting: "Attaching…",
    attached: "Live",
    reattaching: "Reconnecting…",
    exited: "Process exited",
    error: "Terminal error",
    failed: "Terminal unavailable",
  };

  // One live attachment: a proxied mux WebSocket bound to an xterm instance.
  // Reconnects with capped backoff while the sheet is open (daemon restarts);
  // a pane error or process exit stops retrying (the PTY is gone) and the
  // user decides via Reconnect. Returns { dispose, reconnect, onVisible }.
  function attachTerminal(opts) {
    const sessionId = opts.sessionId;
    const handle = opts.handle;
    const term = opts.term;
    const fit = opts.fit;
    const setPhase = opts.setPhase;
    const setPhaseMsg = opts.setPhaseMsg;
    const setInputBlocked = opts.setInputBlocked;
    let phase = "connecting";
    let attempts = 0;
    let disposed = false;
    let ws = null;
    let pingTimer = null;
    let retryTimer = null;
    let lastGrid = null;

    function setOwnPhase(next) {
      if (disposed) return;
      phase = next;
      setPhase(next);
    }

    function send(frame) {
      if (ws && ws.readyState === 1) ws.send(frame);
    }

    function fitAndPublish() {
      try { fit.fit(); } catch (_e) { return; } // 0×0 while the tab is hidden
      const g = { cols: term.cols, rows: term.rows };
      if (g.cols > 0 && g.rows > 0 &&
          (!lastGrid || lastGrid.cols !== g.cols || lastGrid.rows !== g.rows)) {
        lastGrid = g;
        send(muxResizeFrame(handle, g.cols, g.rows));
      }
    }

    function clearPing() {
      if (pingTimer) { window.clearInterval(pingTimer); pingTimer = null; }
    }

    function dropSocket() {
      clearPing();
      if (ws) { try { ws.close(); } catch (_e) { /* already closing */ } ws = null; }
    }

    function scheduleReconnect() {
      if (disposed || retryTimer) return;
      if (phase === "exited" || phase === "error") return; // PTY gone — user decides
      setOwnPhase("reattaching");
      const delay = Math.min(500 * Math.pow(2, attempts), 8000);
      attempts += 1;
      retryTimer = window.setTimeout(function () {
        retryTimer = null;
        doConnect();
      }, delay);
    }

    function handleServerFrame(f) {
      if (!f || f.ch !== "terminal") return;
      if (f.type === "data" && f.data) {
        term.write(base64ToBytes(f.data));
        return;
      }
      if (f.type === "opened") {
        attempts = 0;
        setInputBlocked(false);
        setOwnPhase("attached");
        setPhaseMsg("");
        return;
      }
      if (f.type === "resize" && f.cols && f.rows) {
        // Authoritative grid push: the daemon arbitrates the shared PTY size
        // across viewers (largest primary wins) — follow it, don't re-fight.
        if (term.cols !== f.cols || term.rows !== f.rows) term.resize(f.cols, f.rows);
        lastGrid = { cols: f.cols, rows: f.rows };
        return;
      }
      if (f.type === "exited") {
        setOwnPhase("exited");
        term.writeln("\r\n\x1b[2m[process exited]\x1b[0m");
        dropSocket();
        return;
      }
      if (f.type === "error") {
        const msg = f.error || "unknown terminal error";
        if (msg.indexOf("input is disabled") !== -1) {
          // The daemon's input lease: an exclusive operation (agent switch,
          // resume, kill) owns the pane. View-only until it releases.
          setInputBlocked(true);
          return;
        }
        setOwnPhase("error");
        setPhaseMsg(msg);
        term.writeln("\r\n\x1b[2m[terminal error] " + msg + "\x1b[0m");
        dropSocket();
      }
    }

    function doConnect() {
      if (disposed) return;
      SDK.buildWsUrl(API + "/sessions/" + encodeURIComponent(sessionId) + "/terminal", {})
        .then(function (url) {
          if (disposed) return;
          try { ws = new WebSocket(url); } catch (_e) { scheduleReconnect(); return; }
          ws.onopen = function () {
            if (disposed) return;
            if (attempts > 0) term.reset(); // reattach = fresh full repaint; drop the stale buffer first
            try { fit.fit(); } catch (_e) { /* hidden */ }
            const g = { cols: term.cols, rows: term.rows };
            lastGrid = g.cols > 0 && g.rows > 0 ? g : null;
            send(muxOpenFrame(handle, Math.max(0, g.cols), Math.max(0, g.rows)));
            clearPing();
            pingTimer = window.setInterval(function () { send(muxPingFrame()); }, 20000);
          };
          ws.onmessage = function (ev) {
            if (typeof ev.data !== "string") return;
            let f = null;
            try { f = JSON.parse(ev.data); } catch (_e) { return; }
            handleServerFrame(f);
          };
          ws.onclose = function () {
            ws = null;
            clearPing();
            scheduleReconnect();
          };
          ws.onerror = function () { dropSocket(); };
        })
        .catch(function () { scheduleReconnect(); });
    }

    const dataSub = term.onData(function (data) {
      if (phase !== "attached") return; // view-only until the pane is open
      send(muxDataFrame(handle, data));
    });

    let resizeObserver = null;
    if (opts.hostEl && typeof ResizeObserver !== "undefined") {
      resizeObserver = new ResizeObserver(function () { fitAndPublish(); });
      resizeObserver.observe(opts.hostEl);
    }

    setOwnPhase("connecting");
    doConnect();

    return {
      dispose: function () {
        disposed = true;
        if (retryTimer) { window.clearTimeout(retryTimer); retryTimer = null; }
        try { send(muxCloseFrame(handle)); } catch (_e) { /* socket already gone */ }
        dropSocket();
        if (resizeObserver) resizeObserver.disconnect();
        dataSub.dispose();
      },
      reconnect: function () {
        if (disposed) return;
        attempts = 0;
        setPhaseMsg("");
        setInputBlocked(false);
        setOwnPhase("connecting");
        term.reset();
        if (retryTimer) { window.clearTimeout(retryTimer); retryTimer = null; }
        doConnect();
      },
      // Tab flipped back to visible: 0×0 while hidden must not linger.
      onVisible: function () { fitAndPublish(); },
    };
  }

  function TerminalTab(props) {
    const hostRef = useRef(null);
    const attachRef = useRef(null);
    const termRef = useRef(null);
    const [phase, setPhase] = useState("loading");
    const [phaseMsg, setPhaseMsg] = useState("");
    const [inputBlocked, setInputBlocked] = useState(false);

    useEffect(function () {
      let dead = false;
      let localAttach = null;
      loadXterm().then(function (lib) {
        if (dead || !hostRef.current) return;
        const term = new lib.Terminal({
          scrollback: 5000,
          fontSize: 12,
          cursorBlink: true,
          fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace',
          theme: { background: "#101426", foreground: "#dfe3ff", cursor: "#dfe3ff",
                   selectionBackground: "rgba(124, 108, 255, 0.35)" },
        });
        const fit = new lib.FitAddon();
        term.loadAddon(fit);
        term.open(hostRef.current);
        termRef.current = term;
        localAttach = attachTerminal({
          sessionId: props.sessionId,
          handle: props.handle,
          term: term,
          fit: fit,
          hostEl: hostRef.current,
          setPhase: setPhase,
          setPhaseMsg: setPhaseMsg,
          setInputBlocked: setInputBlocked,
        });
        attachRef.current = localAttach;
      }).catch(function (e) {
        if (dead) return;
        setPhase("failed");
        setPhaseMsg(String((e && e.message) || e));
      });
      return function () {
        dead = true;
        if (localAttach) localAttach.dispose();
        attachRef.current = null;
        termRef.current = null;
      };
    }, [props.sessionId]);

    useEffect(function () {
      if (props.active && attachRef.current) attachRef.current.onVisible();
    }, [props.active]);

    const showRetry = phase === "exited" || phase === "error" || phase === "failed";
    return h("div", { className: "ao-terminal" + (props.active ? "" : " is-hidden") },
      h("div", { className: "ao-term-status" },
        h("span", { className: "ao-term-phase ao-term-" + phase },
          TERM_PHASE_LABEL[phase] || phase,
          phaseMsg && phase !== "failed" ? " — " + phaseMsg : ""),
        inputBlocked
          ? h("span", { className: "ao-term-blocked",
              title: "The AO daemon answers keystrokes with an error while an exclusive session operation (agent switch, resume, kill) holds the input lease." },
            "View-only — an exclusive session operation holds the input lease")
          : null,
        showRetry
          ? h("button", { className: "ao-btn ao-btn-ghost ao-term-retry",
              onClick: function () { if (attachRef.current) attachRef.current.reconnect(); } }, "Reconnect")
          : null),
      h("div", { className: "ao-term-host", ref: hostRef }),
      phase === "failed" ? h("div", { className: "ao-sheet-error" }, phaseMsg) : null
    );
  }

  // Opens at the LATEST message and follows new ones while open: the sheet
  // watches the session over the plugin's /events WS ("watch" -> "detail"
  // pushes; the watch payload includes the live plan, so the plan updates
  // in place too without the writtenAt refetch) and pins the transcript to
  // the bottom. Scrolling up unpins and shows a sticky "Jump to latest"
  // pill; scrolling back to the bottom (or clicking the pill) re-pins.
  // Without WS support it falls back to a 2s poll of the detail route.

  function DetailSheet(props) {
    const [detail, setDetail] = useState(null);
    const [err, setErr] = useState(null);
    const [hiddenClasses, setHiddenClasses] = useState({});
    const [pinned, setPinned] = useState(true);
    const [wsFailed, setWsFailed] = useState(false);
    const [live, setLive] = useState(false);
    const [busy, setBusy] = useState(null);
    // Sheet tabs: transcript (default) | terminal. terminalOpened is sticky
    // for the session so flipping tabs doesn't re-attach the PTY every time.
    const [sheetTab, setSheetTab] = useState("transcript");
    const [terminalOpened, setTerminalOpened] = useState(false);
    const chatRef = useRef(null);
    const pinnedRef = useRef(true);
    const session = props.session;

    function reload() {
      authedFetchJson(API + "/sessions/" + encodeURIComponent(session.id) + "/detail")
        .then(function (d) { setDetail(d); setErr(null); })
        .catch(function (e) { setErr(parseApiErrorMessage(e)); });
    }

    function pin(next) {
      setBusy("pin");
      authedFetchJson(API + "/sessions/" + encodeURIComponent(session.id) + "/pin", {
        method: next ? "POST" : "DELETE",
        headers: { "Content-Type": "application/json" }, body: "{}",
      }).then(reload).catch(function (e) { setErr(parseApiErrorMessage(e)); })
        .finally(function () { setBusy(null); });
    }

    function rename() {
      const next = window.prompt("Rename session", session.displayName);
      if (next == null) return;
      const name = next.trim();
      if (!name || name === session.displayName) return;
      setBusy("rename");
      authedFetchJson(API + "/sessions/" + encodeURIComponent(session.id), {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ displayName: name }),
      }).then(function () {
        session.displayName = name; // the board card catches up on its next sync
        reload();
      }).catch(function (e) { setErr(parseApiErrorMessage(e)); })
        .finally(function () { setBusy(null); });
    }

    function setFollow(v) {
      pinnedRef.current = v;
      setPinned(v);
    }

    function scrollToBottom() {
      const el = chatRef.current;
      if (el) el.scrollTop = el.scrollHeight;
    }

    function onChatScroll() {
      const el = chatRef.current;
      if (!el) return;
      const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 24;
      if (atBottom !== pinnedRef.current) setFollow(atBottom);
    }

    function toggleClass(id) {
      setHiddenClasses(function (prev) {
        const next = {};
        Object.keys(prev).forEach(function (k) { next[k] = prev[k]; });
        if (next[id]) delete next[id]; else next[id] = true;
        return next;
      });
    }

    useEffect(function () {
      let alive = true;
      setDetail(null); setErr(null);
      setFollow(true); setLive(false); setWsFailed(false);
      setSheetTab("transcript"); setTerminalOpened(false);
      authedFetchJson(API + "/sessions/" + encodeURIComponent(session.id) + "/detail")
        .then(function (d) { if (alive) setDetail(d); })
        .catch(function (e) { if (alive) setErr(parseApiErrorMessage(e)); });
      return function () { alive = false; };
    }, [session.id]);

    // Live follow: subscribe to this session's detail over the plugin WS.
    useEffect(function () {
      if (!SDK.buildWsUrl) return;
      let closed = false;
      let ws = null;
      SDK.buildWsUrl(API + "/events", {}).then(function (url) {
        if (closed) return;
        try { ws = new WebSocket(url); } catch (_e) { setWsFailed(true); return; }
        ws.onopen = function () {
          if (closed) return;
          setLive(true);
          ws.send(JSON.stringify({ type: "watch", sessionId: session.id }));
        };
        ws.onmessage = function (ev) {
          try {
            const msg = JSON.parse(ev.data);
            if (msg && msg.type === "detail" && msg.sessionId === session.id && msg.detail) {
              setDetail(msg.detail); setErr(null);
            }
          } catch (_e2) { /* ignore malformed frames */ }
        };
        ws.onclose = function () {
          if (closed) return;
          setLive(false);
          setWsFailed(true); // stream lost mid-watch: fall back to polling
        };
      }).catch(function () { if (!closed) setWsFailed(true); });
      return function () {
        closed = true;
        try { if (ws) ws.close(); } catch (_e) {}
      };
    }, [session.id]);

    // Poll fallback (no WS helper in the SDK, or the stream was refused/lost).
    useEffect(function () {
      if (SDK.buildWsUrl && !wsFailed) return;
      const t = window.setInterval(function () {
        authedFetchJson(API + "/sessions/" + encodeURIComponent(session.id) + "/detail")
          .then(function (d) { setDetail(d); setErr(null); })
          .catch(function () { /* keep last detail; next tick retries */ });
      }, 2000);
      return function () { window.clearInterval(t); };
    }, [session.id, wsFailed]);

    // Keep the transcript pinned to the latest message while following.
    useEffect(function () {
      if (detail && pinnedRef.current) scrollToBottom();
    }, [detail]);

    const s = detail && detail.session;
    const projectId = (s && s.projectId) || session.projectId || "";
    // Terminal tab: backend-gated (terminal_view) and only for sessions the
    // daemon gave a PTY handle (headless harnesses have none).
    const hasTerminal = Boolean(s && s.terminalView && s.terminalHandleId);
    const chatItems = detail
      ? timelineItems(detail.transcript, detail.activities, hiddenClasses)
      : [];
    return h("div", { className: "ao-overlay", onClick: function (e) { if (e.target === e.currentTarget) props.onClose(); } },
      h("div", { className: "ao-sheet ao-sheet-detail" },
        h("div", { className: "ao-sheet-head" },
          h("div", { className: "ao-sheet-title" }, session.displayName),
          props.openLinks
            ? h(OpenInAO, { projectId: projectId, sessionId: session.id,
                            displayName: session.displayName,
                            className: "ao-btn ao-btn-ghost ao-open-ao",
                            label: "Open in Agent Orchestrator" })
            : null),
        !detail && !err ? h("div", { className: "ao-loading" }, "Loading session detail…") : null,
        err ? h("div", { className: "ao-sheet-error" }, err) : null,
        hasTerminal
          ? h("div", { className: "ao-sheet-tabs" },
              h("button", { className: "ao-tab" + (sheetTab === "transcript" ? " is-active" : ""),
                onClick: function () { setSheetTab("transcript"); } }, "Transcript"),
              h("button", { className: "ao-tab" + (sheetTab === "terminal" ? " is-active" : ""),
                onClick: function () { setSheetTab("terminal"); setTerminalOpened(true); } }, "Terminal"))
          : null,
        s ? h("div", { className: "ao-detail-meta" },
          h("div", { className: "ao-detail-row" }, h("span", { className: "ao-detail-key" }, "Status"), h("span", null, s.displayStatus || s.status || "—")),
          h("div", { className: "ao-detail-row" }, h("span", { className: "ao-detail-key" }, "Project"), h("span", null, s.projectId || "standalone")),
          h("div", { className: "ao-detail-row" }, h("span", { className: "ao-detail-key" }, "Harness"), h("span", null, s.harness || "—")),
          s.model ? h("div", { className: "ao-detail-row" }, h("span", { className: "ao-detail-key" }, "Model"), h("span", null, s.model)) : null,
          s.mode ? h("div", { className: "ao-detail-row" }, h("span", { className: "ao-detail-key" }, "Mode"), h("span", null, s.mode)) : null,
          h("div", { className: "ao-detail-row" },
            h("span", { className: "ao-detail-key" }, "Branch"),
            h("span", { className: "ao-chip-branch", title: branchStateTitle(s.branchState) }, s.branch || "—")),
          detail && detail.usage && detail.usage.label
            ? h("div", { className: "ao-detail-row" },
                h("span", { className: "ao-detail-key" }, "Usage"),
                h("span", { title: detail.usage.incomplete ? "usage still settling" : "estimated from provider pricing" },
                  detail.usage.label))
            : null,
          h("div", { className: "ao-detail-row" }, h("span", { className: "ao-detail-key" }, "Created"), h("span", null, timeAgo(s.createdAt) + (s.createdAt ? " (" + s.createdAt + ")" : ""))),
          s.updatedAt ? h("div", { className: "ao-detail-row" }, h("span", { className: "ao-detail-key" }, "Updated"), h("span", null, timeAgo(s.updatedAt) + " (" + s.updatedAt + ")")) : null
        ) : null,
        detail && detail.prs && detail.prs.length
          ? h("div", { className: "ao-detail-prs" },
              h("div", { className: "ao-detail-key" }, "Pull requests"),
              detail.prs.map(function (pr, i) {
                return h("div", { key: i, className: "ao-detail-pr" },
                  h("a", { href: pr.url, target: "_blank", rel: "noreferrer",
                           className: "ao-chip ao-chip-pr" + (PR_STATE_CLASS[(pr.state || "").toLowerCase()] ? " " + PR_STATE_CLASS[(pr.state || "").toLowerCase()] : "") },
                    "#" + (pr.number != null ? pr.number : "?")),
                  h("span", { className: "ao-detail-pr-facts" }, prStateTitle(pr)));
              }))
          : null,
        detail && detail.plan && detail.plan.steps && detail.plan.steps.length
          ? h("div", { className: "ao-detail-plan" },
              h("div", { className: "ao-detail-key" }, "Plan" +
                (detail.plan.writtenAt ? " (updated " + timeAgo(detail.plan.writtenAt) + ")" : "")),
              h("div", { className: "ao-plan-steps" },
                detail.plan.steps.map(function (st, i) {
                  const status = st.status || "pending";
                  return h("div", { key: i, className: "ao-plan-step ao-plan-" + status.replace("_", "-") },
                    h("span", { className: "ao-plan-glyph" },
                      status === "completed" ? "✓" : status === "cancelled" ? "×" : ""),
                    h("span", { className: "ao-plan-text" }, st.text));
                })))
          : null,
        // Kept mounted while the Terminal tab is up (display:none) so the
        // transcript scroll position and a half-typed composer draft survive
        // a tab flip.
        s ? h("div", { className: "ao-detail-transcript" + (sheetTab === "terminal" ? " is-hidden" : "") },
          h("div", { className: "ao-detail-key" },
            "Transcript" +
            (detail.transcriptTruncated ? " (last " + detail.transcript.length + " of " + detail.transcriptTotal + " messages)" : "") +
            (live ? " — live" : wsFailed ? " — polling" : "")),
          detail.conversationNote ? null : h("div", { className: "ao-class-filters" },
            Object.keys(MSG_CLASSES).map(function (id) {
              const off = !!hiddenClasses[id];
              return h("button", {
                key: id,
                className: "ao-chip ao-class-chip ao-class-" + id + (off ? " is-off" : ""),
                title: MSG_CLASSES[id].title + (off ? " — hidden, click to show" : " — shown, click to hide"),
                onClick: function () { toggleClass(id); },
              }, (off ? "\u25CB " : "\u25CF ") + MSG_CLASSES[id].label);
            })),
          h("div", { className: "ao-chat", ref: chatRef, onScroll: onChatScroll },
            detail.conversationNote
              ? h("div", { className: "ao-column-empty" }, detail.conversationNote)
              : chatItems.length === 0
              ? h("div", { className: "ao-column-empty" }, "Nothing to show in the selected classes")
              : chatItems.map(function (it, i) {
                  if (it.kind === "activity") {
                    // Steered guidance: daemon-recorded with origin=human, so it
                    // renders as an End-user bubble, not a tool row.
                    if (it.item.activityKind === "steer")
                      return h("div", { key: "s" + (it.item.id || i), className: "ao-chat-row ao-chat-user" },
                        h("div", { className: "ao-chat-col" },
                          h("span", { className: "ao-chat-sender" }, MSG_CLASSES.user.label),
                          h("div", { className: "ao-bubble" }, bubbleText({ text: it.item.text }))));
                    return it.item.activityKind === "reasoning"
                      ? h(ThinkingRow, { key: "a" + (it.item.id || i), activity: it.item })
                      : h(ToolRow, { key: "a" + (it.item.id || i), activity: it.item });
                  }
                  const m = it.item;
                  const cls = messageClass(m);
                  return h("div", { key: "m" + (m.sequence != null ? m.sequence : i), className: "ao-chat-row ao-chat-" + cls },
                    h("div", { className: "ao-chat-col" },
                      h("span", { className: "ao-chat-sender" }, MSG_CLASSES[cls].label),
                      h("div", { className: "ao-bubble" }, bubbleText(m))));
                }),
            !pinned && chatItems.length
              ? h("button", {
                  className: "ao-jump-latest",
                  title: "Scroll back to the latest message",
                  onClick: function () { setFollow(true); scrollToBottom(); },
                }, "↓ Jump to latest")
              : null
          ),
          // The composer rides the transcript: steer-or-send works for a
          // working session (mid-turn steer) and an idle one (new turn).
          // Archived sessions are terminated — there is nothing to message.
          session.column !== "archive"
            ? h(Composer, { sessionId: session.id,
                            focus: props.focusComposer,
                            working: ((detail && detail.session.status) || session.status || "") === "working" })
            : null
        ) : null,
        hasTerminal && terminalOpened
          ? h(TerminalTab, { sessionId: session.id, handle: s.terminalHandleId,
                             active: sheetTab === "terminal" })
          : null,
        detail ? h(EnrichedSection, { enriched: detail.hermesEnriched }) : null,
        h("div", { className: "ao-sheet-actions" },
          h("button", {
            type: "button", className: "ao-btn ao-btn-ghost", disabled: busy === "pin",
            title: s && s.isPinned ? "Unpin — pinned sessions sort first on the board" : "Pin — sort this session first on the board",
            onClick: function () { pin(!s || !s.isPinned); },
          }, busy === "pin" ? "…" : (s && s.isPinned ? "Unpin" : "Pin")),
          h("button", {
            type: "button", className: "ao-btn ao-btn-ghost", disabled: busy === "rename",
            title: "Rename this session (the daemon limits names to 100 characters)",
            onClick: rename,
          }, busy === "rename" ? "…" : "Rename"),
          h("button", { type: "button", className: "ao-btn ao-btn-ghost", onClick: props.onClose }, "Close")
        )
      )
    );
  }

  // --- Board page ----------------------------------------------------------

  // First-run state: the daemon has NEVER answered (no cached board state at
  // all). A retry banner would be dishonest here — the user has nothing to
  // lose; they need setup guidance instead. "Was connected, now down" keeps
  // the retry banner over the last-known board.
  function SetupState(props) {
    return h("div", { className: "ao-setup" },
      h("div", { className: "ao-setup-card" },
        h("div", { className: "ao-setup-title" }, "Agent Orchestrator not detected"),
        h("p", { className: "ao-setup-text" },
          "This board mirrors a local Agent Orchestrator (AO) daemon. Nothing answered at ",
          h("code", { className: "ao-setup-url" }, props.baseUrl || DEFAULT_BASE_URL), " yet."),
        h("ul", { className: "ao-setup-steps" },
          h("li", null, "Install and open the ",
            h("a", { className: "ao-chip-pr", href: AO_APP_RELEASES_URL,
                     target: "_blank", rel: "noreferrer" }, "AO desktop app"),
            " — it runs the daemon this plugin mirrors."),
          h("li", null, "Already running the daemon elsewhere? Point the plugin at it: set ",
            h("code", { className: "ao-setup-url" }, "plugins.entries.ao-bridge.settings.ao_base_url"),
            " in your Hermes config.yaml (default " + DEFAULT_BASE_URL + ").")),
        h("div", { className: "ao-setup-actions" },
          h("button", { className: "ao-btn ao-btn-primary",
                        onClick: props.onRetry }, "Retry now")),
        h("div", { className: "ao-setup-hint" },
          "Checks continue in the background — the board appears here on its own once a daemon answers.")
      ));
  }

  function AoBridgePage() {
    const [data, setData] = useState(null);
    const [error, setError] = useState(null);
    const [errorKind, setErrorKind] = useState(null);
    const [daemonUrl, setDaemonUrl] = useState(null);
    const [loaded, setLoaded] = useState(false);
    // "Never connected" vs "was connected, now down": persisted, so a reload
    // during an outage keeps the retry banner instead of regressing to setup.
    const [everConnected, setEverConnected] = useState(function () {
      try { return window.localStorage.getItem("ao-bridge.everConnected") === "1"; }
      catch (_e) { return false; }
    });
    function markConnected() {
      setEverConnected(function (prev) {
        if (prev) return prev;
        try { window.localStorage.setItem("ao-bridge.everConnected", "1"); } catch (_e) {}
        return true;
      });
    }
    const [live, setLive] = useState(false);
    const [refreshedAt, setRefreshedAt] = useState(null);
    const [spawnOpen, setSpawnOpen] = useState(false);
    // The card detail sheet hosts the composer; `focusComposer` marks the
    // "Message" quick-action path — the sheet opens with the composer focused
    // (one click to steer a working session).
    const [focusComposer, setFocusComposer] = useState(false);
    const [detailTargetId, setDetailTargetId] = useState(null);
    // Derive the detail card from the live board so the sheet re-renders (and
    // refetches, via the plan-writtenAt effect below) on WS pushes. A click
    // from any population (worker card, orchestrator row, archive row) lands
    // here.
    const allSessions = useMemo(function () {
      return data ? (data.cards || []).concat(data.orchestrators || [], data.archived || []) : [];
    }, [data]);
    const detailCard = detailTargetId
      ? allSessions.find(function (c) { return c.id === detailTargetId; }) || null
      : null;
    const [query, setQuery] = useState("");
    const [statusFilter, setStatusFilter] = useState("all");
    const [projectFilter, setProjectFilter] = useState("all");
    const [harnessFilter, setHarnessFilter] = useState("all");
    const [recency, setRecency] = useState(function () {
      try { return window.localStorage.getItem("ao-bridge.recency") || "24h"; }
      catch (_e) { return "24h"; }
    });
    const [activeOnly, setActiveOnly] = useState(function () {
      try { return window.localStorage.getItem("ao-bridge.activeOnly") !== "0"; }
      catch (_e) { return true; }
    });
    // Board theme: read once, switch live (the class on the root div re-points
    // the CSS custom properties — no reload), persisted like the filters.
    const [theme, setTheme] = useState(readStoredTheme);
    function setThemePersisted(id) {
      const next = normalizeTheme(id);
      setTheme(next);
      writeStoredTheme(next);
    }
    const timerRef = useRef(null);
    const wsRef = useRef(null);
    const iconMap = useHarnessIcons();
    const manifestVersion = useManifestVersion();
    // Version skew (plugin updated while the dashboard kept running): the
    // backend answers with its import-time version, the manifest here is the
    // disk one. A backend OLDER than the version field answers with no
    // version at all — also stale (pre-0.2.8 module behind this UI).
    const staleBackend = Boolean(manifestVersion && data
      && manifestVersion !== data.pluginVersion);
    const relay = useRelayStatus();
    // Honest, not silent: the relay is on but no route resolves (no pasted
    // api_server key, no peer) — events would be dropped with nowhere to go.
    const relayUnconfigured = Boolean(relay && relay.enabled
      && relay.delivery && !relay.delivery.configured);
    // First run: the daemon has never answered on this browser (no cached
    // board state). Only a genuine "daemon unreachable" counts — any other
    // failure keeps the ordinary error surface.
    const firstRunSetup = Boolean(errorKind === "daemon_unreachable" && !everConnected && !data);

    // Re-render periodically so card ages (timeAgo) tick even between WS
    // pushes — during a working turn the daemon's session row is static, so
    // without this the "5m ago" labels would freeze mid-turn.
    const [, setAgeTick] = useState(0);
    useEffect(function () {
      const t = window.setInterval(function () { setAgeTick(function (v) { return v + 1; }); }, 30000);
      return function () { window.clearInterval(t); };
    }, []);

    const refresh = useCallback(function () {
      return authedFetchJson(API + "/board")
        .then(function (body) {
          setData(body); setError(null);
          setLoaded(true); setRefreshedAt(new Date());
          markConnected();
        })
        .catch(function (e) {
          const parsed = parseApiError(e);
          setError(parsed.kind === "daemon_unreachable" ? DAEMON_DOWN_MSG : parsed.message);
          setErrorKind(parsed.kind);
          setDaemonUrl(parsed.baseUrl || null);
        })
        .finally(function () { setLoaded(true); });
    }, []);

    // Live updates: WebSocket /events (server polls the daemon, pushes on
    // change); falls back to 5s polling when the SDK has no WS helper, the
    // stream is refused, or the daemon is down (the WS error frame starts the
    // poll; the next board frame stops it again).
    useEffect(function () {
      let closed = false;
      let backoff = 1000;
      let pollTimer = null;
      let reconnectTimer = null;

      function startPoll() {
        if (pollTimer || closed) return;
        setLive(false);
        refresh();
        pollTimer = window.setInterval(refresh, POLL_MS);
      }

      function stopPoll() {
        if (pollTimer) window.clearInterval(pollTimer);
        pollTimer = null;
      }

      function onBoard(body) {
        if (closed) return;
        setData(body); setError(null); setErrorKind(null);
        setLoaded(true); setLive(true); setRefreshedAt(new Date());
        markConnected();
        stopPoll(); // live again — the fallback poll did its job
      }

      function onDaemonDown(frame) {
        if (closed) return;
        setError(DAEMON_DOWN_MSG);
        setErrorKind((frame && frame.error && frame.error.kind) || "daemon_unreachable");
        setDaemonUrl((frame && frame.error && frame.error.baseUrl) || null);
        setLive(false);
        startPoll(); // keep retrying over HTTP while the WS is quiet
      }

      function openWs() {
        if (closed) return;
        if (!SDK.buildWsUrl) { startPoll(); return; }
        SDK.buildWsUrl(API + "/events", {}).then(function (url) {
          if (closed) return;
          let ws;
          try { ws = new WebSocket(url); } catch (_e) { startPoll(); return; }
          ws.onopen = function () { backoff = 1000; };
          ws.onmessage = function (ev) {
            try {
              const msg = JSON.parse(ev.data);
              if (msg && msg.type === "board" && msg.board) onBoard(msg.board);
              else if (msg && msg.type === "daemon_error") onDaemonDown(msg);
            } catch (_e) { /* ignore */ }
          };
          ws.onclose = function (e) {
            if (closed) return;
            if (e && e.code === 1008) {
              setError("Live stream unauthorized — falling back to polling");
              startPoll();
              return;
            }
            // Socket lost (server restart, network blip): reconnect with
            // backoff AND poll over HTTP meanwhile — a board that is neither
            // live nor polling is how it freezes forever.
            startPoll();
            reconnectTimer = window.setTimeout(openWs, Math.min(backoff, 30000));
            backoff = Math.min(backoff * 2, 30000);
          };
        }).catch(function () { startPoll(); });
      }

      refresh();
      openWs();
      return function () {
        closed = true;
        if (pollTimer) window.clearInterval(pollTimer);
        if (reconnectTimer) window.clearTimeout(reconnectTimer);
        try { if (wsRef.current) wsRef.current.close(); } catch (_e) {}
      };
    }, [refresh]);

    function setRecencyPersisted(id) {
      setRecency(id);
      try { window.localStorage.setItem("ao-bridge.recency", id); } catch (_e) {}
    }

    function toggleActiveOnly() {
      setActiveOnly(function (prev) {
        const next = !prev;
        try { window.localStorage.setItem("ao-bridge.activeOnly", next ? "1" : "0"); } catch (_e) {}
        return next;
      });
    }

    // Status/harness options cover BOTH populations — the filters drive the
    // worker columns and the orchestrators strip alike.
    const statusOptions = useMemo(function () {
      const seen = {};
      allSessions.forEach(function (c) {
        const v = (c.displayStatus || c.status || "").toLowerCase();
        if (v) seen[v] = true;
      });
      return Object.keys(seen).sort();
    }, [allSessions]);

    const harnessOptions = useMemo(function () {
      const seen = {};
      allSessions.forEach(function (c) {
        const v = (c.harness || "").trim();
        if (v) seen[v] = true;
      });
      return Object.keys(seen).sort();
    }, [allSessions]);

    // One filter pass shared by every population, so search and each
    // dropdown/toggle behave identically for workers, orchestrators and the
    // archive. The archive strip opts out of the two live-board gates
    // (Active-only and the recency window) — it exists to surface history.
    const filterSession = useCallback(function (c, opts) {
      const applyActiveOnly = !opts || opts.activeOnly !== false;
      const applyRecency = !opts || opts.recency !== false;
      const q = query.trim().toLowerCase();
      if (q
          && (c.project || "").toLowerCase().indexOf(q) === -1
          && (c.displayName || "").toLowerCase().indexOf(q) === -1
          && (c.harness || "").toLowerCase().indexOf(q) === -1) return false;
      if (statusFilter !== "all"
          && (c.displayStatus || c.status || "").toLowerCase() !== statusFilter) return false;
      if (projectFilter !== "all" && (c.projectId || "") !== projectFilter) return false;
      if (harnessFilter !== "all" && (c.harness || "").trim() !== harnessFilter) return false;
      // "Active only" means a LIVE TURN (v0.5.1 semantics) — idle sessions
      // hide with it; the archive strip opts out via applyActiveOnly=false.
      // v0.8.2: actually consult the TOGGLE. The v0.5.1 rewrite dropped the
      // read (applyActiveOnly derived from opts alone, and matchesFilters
      // passes no opts), so "Show all" was a no-op and every non-working
      // card was permanently hidden — the live board read as "everything is
      // archived" while only the strip and live turns rendered.
      if (activeOnly && applyActiveOnly && stateOf(c) !== "working") return false;
      if (applyRecency) {
        const bucket = RECENCY_OPTIONS.find(function (r) { return r.id === recency; });
        const cutoffMs = bucket && bucket.ms ? Date.now() - bucket.ms : 0;
        if (cutoffMs) {
          const ts = new Date(c.updatedAt || c.createdAt || "").getTime();
          if (!isFinite(ts) || ts < cutoffMs) return false;
        }
      }
      return true;
    }, [query, statusFilter, projectFilter, harnessFilter, recency, activeOnly]);

    const matchesFilters = useCallback(function (c) { return filterSession(c); }, [filterSession]);
    const matchesArchiveFilters = useCallback(function (c) {
      return filterSession(c, { activeOnly: false, recency: false });
    }, [filterSession]);

    const columns = useMemo(function () {
      if (!data) return [];
      const buckets = {};
      (data.cards || []).forEach(function (c) {
        // Belt-and-braces: orchestrators must never re-enter the worker
        // columns, even against an older backend that still ships them in
        // `cards` (the current backend splits them out server-side).
        if ((c.kind || "").toLowerCase() === "orchestrator") return;
        if (!matchesFilters(c)) return;
        (buckets[c.column] = buckets[c.column] || []).push(c);
      });
      return (data.columns || []).map(function (col) {
        const b = buckets[col.id] || [];
        b.sort(cardLaneOrder);
        return { id: col.id, label: col.label, cards: b };
      });
    }, [data, matchesFilters]);

    // Orchestrator strip: opts out of Active-only and the recency window
    // (v0.8.3) — the coordinators are few and are the board's map of who is
    // running what; gating them by live turn made the strip vanish entirely
    // the moment no orchestrator happened to be mid-turn. Search and the
    // dropdown filters still apply. Terminated orchestrators never render
    // here: the backend routes them to the archive strip (belt-and-braces
    // below for an older backend that still ships them in orchestrators).
    const matchesOrchFilters = useCallback(function (c) {
      return filterSession(c, { activeOnly: false, recency: false });
    }, [filterSession]);

    const orchestrators = useMemo(function () {
      if (!data) return [];
      const list = (data.orchestrators || (data.cards || []).filter(function (c) {
        return (c.kind || "").toLowerCase() === "orchestrator";
      })).filter(function (c) { return stateOf(c) !== "archived"; })
        .filter(matchesOrchFilters);
      // Live orchestrators first, then by recency — archived history sinks
      // to the bottom instead of burying the ones actually coordinating.
      list.sort(function (a, b) {
        const la = stateOf(a), lb = stateOf(b);
        if (la !== lb) {
          const order = { working: 0, awaiting_pr: 1, idle: 2, exited: 3, archived: 4 };
          return order[la] - order[lb];
        }
        return (b.updatedAt || "").localeCompare(a.updatedAt || "");
      });
      return list;
    }, [data, matchesOrchFilters]);

    // Archived sessions (predicate over ALL sessions — workers AND terminated
    // orchestrators, matching AO desktop's archive grouping; older backends
    // keep terminated orchestrators in `orchestrators`, folded in here).
    const archived = useMemo(function () {
      if (!data) return [];
      const legacy = (data.orchestrators || []).filter(function (c) {
        return stateOf(c) === "archived";
      });
      const list = (data.archived || []).concat(legacy).filter(matchesArchiveFilters);
      list.sort(function (a, b) {
        return (b.updatedAt || b.createdAt || "").localeCompare(a.updatedAt || a.createdAt || "");
      });
      return list;
    }, [data, matchesArchiveFilters]);

    return h("div", { className: "ao-bridge ao-theme-" + normalizeTheme(theme) },
      h("div", { className: "ao-header" },
        h("div", { className: "ao-header-title" },
          h("span", { className: "ao-header-name" }, "AO Bridge"),
          data && data.baseUrl
            ? h("span", { className: "ao-header-daemon", title: data.baseUrl }, "daemon " + data.baseUrl.replace(/^https?:\/\//, ""))
            : null,
          h("span", { className: "ao-header-live" + (live ? " is-live" : (error && everConnected ? " is-degraded" : "")) },
            live ? "live" : (error && everConnected ? "degraded" : (firstRunSetup ? "setup" : "polling"))),
          refreshedAt ? h("span", { className: "ao-header-updated" }, "updated " + refreshedAt.toLocaleTimeString()) : null
        ),
        h("div", { className: "ao-header-actions" },
          h("input", {
            className: "ao-input ao-search", placeholder: "Search project, session, harness…",
            value: query, onChange: function (e) { setQuery(e.target.value); },
          }),
          h("select", {
            className: "ao-input ao-filter-project", title: "Filter by project",
            value: projectFilter,
            onChange: function (e) { setProjectFilter(e.target.value); },
          },
            h("option", { value: "all" }, "All projects"),
            ((data && data.projects) || []).map(function (p) {
              return h("option", { key: p.id, value: p.id }, p.name);
            })
          ),
          h("select", {
            className: "ao-input ao-filter-harness", title: "Filter by harness",
            value: harnessFilter,
            onChange: function (e) { setHarnessFilter(e.target.value); },
          },
            h("option", { value: "all" }, "All harnesses"),
            harnessOptions.map(function (s) {
              return h("option", { key: s, value: s }, s);
            })
          ),
          h("select", {
            className: "ao-input ao-filter-recency", title: "Filter by recent activity",
            value: recency,
            onChange: function (e) { setRecencyPersisted(e.target.value); },
          },
            RECENCY_OPTIONS.map(function (r) {
              return h("option", { key: r.id, value: r.id }, r.label);
            })
          ),
          h("button", {
            className: "ao-btn ao-btn-toggle" + (activeOnly ? " is-on" : ""),
            title: "Hide terminated, exited and archived sessions",
            onClick: toggleActiveOnly,
          }, activeOnly ? "● Active only" : "○ Show all"),
          h("select", {
            className: "ao-input ao-status-filter", value: statusFilter,
            onChange: function (e) { setStatusFilter(e.target.value); },
          },
            h("option", { value: "all" }, "All statuses"),
            statusOptions.map(function (s) {
              return h("option", { key: s, value: s }, s);
            })
          ),
          error && !firstRunSetup ? h("span", { className: "ao-banner ao-banner-error" }, error) : null,
          h("div", { className: "ao-theme-picker", role: "group", "aria-label": "Board theme" },
            THEMES.map(function (t) {
              return h("button", {
                key: t.id,
                className: "ao-theme-dot ao-theme-dot-" + t.id + (theme === t.id ? " is-on" : ""),
                title: "Theme: " + t.label,
                "aria-label": "Theme: " + t.label,
                "aria-pressed": theme === t.id,
                onClick: function () { setThemePersisted(t.id); },
              });
            })),
          h("button", { className: "ao-btn ao-btn-ghost", onClick: function () { refresh(); } }, "Refresh"),
          h("button", { className: "ao-btn ao-btn-primary", onClick: function () { setSpawnOpen(true); } }, "+ Spawn worker")
        )
      ),
      staleBackend
        ? h("div", { className: "ao-banner-row" },
            h("span", { className: "ao-banner ao-banner-reload" },
              "Plugin updated — reload dashboard to apply (backend "
              + (data.pluginVersion ? "v" + data.pluginVersion : "predates version reporting")
              + ", on disk v" + manifestVersion + ")"),
            h("button", {
              className: "ao-btn ao-btn-ghost",
              title: "Reload the dashboard page to pick up the updated plugin",
              onClick: function () { window.location.reload(); },
            }, "Reload"))
        : null,
      relayUnconfigured
        ? h("div", { className: "ao-banner-row" },
            h("span", { className: "ao-banner ao-banner-reload" },
              "AO event relay is on, but delivery is not configured — "
              + ((relay.delivery && relay.delivery.reason) || "no route is set up.")),
            h("a", {
              className: "ao-btn ao-btn-ghost",
              href: README_DELIVERY_URL, target: "_blank", rel: "noreferrer",
              title: "Setup steps: enable api_server, paste its key into the plugin config",
            }, "Enable delivery"))
        : null,
      !loaded
        ? h("div", { className: "ao-loading" }, "Loading AO board…")
        : !data
          ? firstRunSetup
            ? h(SetupState, { baseUrl: daemonUrl, onRetry: function () { refresh(); } })
            : h("div", { className: "ao-board-error" },
                h("div", { className: "ao-banner ao-banner-error" }, error || DAEMON_DOWN_MSG),
                h("div", { className: "ao-board-error-hint" },
                  "The AO daemon has not answered yet. Retries continue automatically — the board appears here the moment the daemon does."))
          : [
              // Orchestrators and the archive live in quiet strips above the
              // columns; each strip hides itself when nothing matches.
              orchestrators.length
                ? h(OrchestratorStrip, {
                    orchestrators: orchestrators, iconMap: iconMap,
                    openLinks: !!(data && data.openInAoLinks),
                    onOpen: function (card) { setDetailTargetId(card.id); },
                  })
                : null,
              archived.length
                ? h(ArchiveStrip, {
                    archived: archived, iconMap: iconMap,
                    openLinks: !!(data && data.openInAoLinks),
                    onOpen: function (card) { setDetailTargetId(card.id); },
                    onChanged: refresh,
                  })
                : null,
              h("div", { className: "ao-board" },
                columns.map(function (col) {
                  return h(Column, {
                    key: col.id, id: col.id, label: col.label, cards: col.cards,
                    iconMap: iconMap,
                    openLinks: !!(data && data.openInAoLinks),
                    onChanged: refresh,
                    onMessage: function (card) {
                      setDetailTargetId(card.id);
                      setFocusComposer(true);
                    },
                    onOpen: function (card) { setDetailTargetId(card.id); setFocusComposer(false); },
                  });
                })),
            ],
      spawnOpen
        ? h(SpawnForm, {
            projects: (data && data.projects) || [], harnesses: (data && data.harnesses) || ["hermes"],
            iconMap: iconMap,
            onClose: function () { setSpawnOpen(false); },
            onChanged: function (session) {
              setSpawnOpen(false);
              refresh();
              if (session && session.id) {
                // Surface the fresh spawn by flashing its column: refresh picks it up.
              }
            },
          })
        : null,
      detailCard
        ? h(DetailSheet, {
            session: detailCard,
            focusComposer: focusComposer,
            openLinks: !!(data && data.openInAoLinks),
            onClose: function () { setDetailTargetId(null); setFocusComposer(false); },
          })
        : null
    );
  }

  if (window.__HERMES_PLUGINS__ && typeof window.__HERMES_PLUGINS__.register === "function") {
    window.__HERMES_PLUGINS__.register("ao-bridge", AoBridgePage);
  }
})();
