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
  // Browser assets are served from the plugin's dashboard/ dir by the host
  // (dashboard-plugins static route). The SDK exposes no asset-base helper,
  // so the fixed plugin id doubles as the URL base.
  const ASSET_BASE = "/dashboard-plugins/ao-bridge";

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

  // Raw AO statuses that mean "no agent is alive behind this session" — hidden
  // by the Active-only toggle (archive-column cards are hidden with it too).
  const INACTIVE_STATUSES = { terminated: true, exited: true };

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

  function parseApiErrorMessage(err) {
    const raw = (err && err.message) ? String(err.message) : String(err || "");
    const m = raw.match(/^(\d{3}):\s*(.*)$/s);
    const body = m ? m[2] : raw;
    try {
      const parsed = JSON.parse(body);
      if (parsed && parsed.detail) return String(parsed.detail);
    } catch (_e) { /* not JSON */ }
    return body || raw;
  }

  function authedFetchJson(url, options) {
    return (SDK.authedFetch || fetch)(url, options).then(function (resp) {
      return resp.text().then(function (txt) {
        if (!resp.ok) {
          let detail = txt;
          try { detail = JSON.parse(txt).detail || txt; } catch (_e) { /* raw */ }
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

  function StatusChip(props) {
    const label = props.displayStatus || props.status;
    if (!label) return null;
    return h("span", { className: "ao-chip ao-chip-status" }, label);
  }

  function HarnessChip(props) {
    if (!props.harness) return null;
    const src = harnessIconSrc(props.iconMap, props.harness);
    return h("span", { className: "ao-chip ao-chip-harness", title: props.harness },
      src ? h("img", { className: "ao-chip-icon", src: src, alt: "", loading: "lazy" }) : null,
      props.harness);
  }

  function PrLinks(props) {
    const prs = props.prs || [];
    if (!prs.length) return null;
    return h("div", { className: "ao-card-prs" },
      prs.slice(0, 3).map(function (pr, i) {
        return h("a", {
          key: i, className: "ao-chip ao-chip-pr", href: pr.url,
          target: "_blank", rel: "noreferrer",
          onClick: function (e) { e.stopPropagation(); },
        }, "PR" + (pr.number ? " #" + pr.number : ""));
      })
    );
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
        h("span", { className: "ao-card-age", title: card.createdAt }, timeAgo(card.createdAt))
      ),
      h("div", { className: "ao-card-name", title: card.displayName }, card.displayName),
      card.plan && card.status === "working" ? h(PlanLine, { plan: card.plan }) : null,
      h("div", { className: "ao-card-meta" },
        h(HarnessChip, { harness: card.harness, iconMap: props.iconMap }),
        h(StatusChip, { displayStatus: card.displayStatus, status: card.status }),
        card.branch ? h("span", { className: "ao-chip ao-chip-branch", title: card.branch }, card.branch) : null
      ),
      h(PrLinks, { prs: card.prs }),
      err ? h("div", { className: "ao-card-error" }, err) : null,
      h("div", { className: "ao-card-actions" },
        card.column !== "archive" && card.column !== "done"
          ? h("button", {
              className: "ao-btn ao-btn-ghost", disabled: !!busy,
              onClick: function (e) { props.onSend(card, e); },
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
    return h("div", { className: "ao-column ao-column-" + (COLUMN_CLASS[props.id] || "") + (props.collapsed ? " ao-collapsed" : "") },
      h("div", { className: "ao-column-head", onClick: props.onToggle },
        h("span", { className: "ao-column-label" }, props.label),
        h("span", { className: "ao-column-count" }, cards.length)
      ),
      props.collapsed
        ? null
        : h("div", { className: "ao-column-body" },
            cards.length === 0
              ? h("div", { className: "ao-column-empty" }, "No sessions")
              : cards.map(function (c) {
                  return h(Card, { key: c.id, card: c, iconMap: props.iconMap, onChanged: props.onChanged, onSend: props.onSend, onOpen: props.onOpen });
                })
          )
    );
  }

  // --- Spawn form (modal sheet) -------------------------------------------

  function SpawnForm(props) {
    const [projectId, setProjectId] = useState(props.projects[0] ? props.projects[0].id : "");
    const [harness, setHarness] = useState("hermes");
    const [name, setName] = useState("");
    const [prompt, setPrompt] = useState("");
    const [busy, setBusy] = useState(false);
    const [err, setErr] = useState(null);

    function submit(e) {
      e.preventDefault();
      if (busy) return;
      setBusy(true); setErr(null);
      authedFetchJson(API + "/spawn", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ projectId: projectId, harness: harness, displayName: name, prompt: prompt }),
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
        err ? h("div", { className: "ao-sheet-error" }, err) : null,
        h("div", { className: "ao-sheet-actions" },
          h("button", { type: "button", className: "ao-btn ao-btn-ghost", onClick: props.onClose }, "Cancel"),
          h("button", { type: "submit", className: "ao-btn ao-btn-primary", disabled: busy },
            busy ? "Spawning…" : "Spawn")
        )
      )
    );
  }

  // --- Send-message sheet --------------------------------------------------

  function SendSheet(props) {
    const [message, setMessage] = useState("");
    const [busy, setBusy] = useState(false);
    const [err, setErr] = useState(null);

    function submit(e) {
      e.preventDefault();
      if (busy || !message.trim()) return;
      setBusy(true); setErr(null);
      authedFetchJson(API + "/sessions/" + encodeURIComponent(props.session.id) + "/send", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: message, userAuthored: true }),
      })
        .then(function () { props.onClose(); props.onChanged(); })
        .catch(function (e2) { setErr(parseApiErrorMessage(e2)); })
        .finally(function () { setBusy(false); });
    }

    return h("div", { className: "ao-overlay ao-overlay-bottom", onClick: function (e) { if (e.target === e.currentTarget) props.onClose(); } },
      h("form", { className: "ao-sheet ao-sheet-send", onSubmit: submit },
        h("div", { className: "ao-sheet-title" }, "Message \u201C" + props.session.displayName + "\u201D"),
        h("textarea", { className: "ao-input ao-textarea", rows: 4, autoFocus: true, value: message,
                        placeholder: "Send a message to this session…",
                        onChange: function (e) { setMessage(e.target.value); } }),
        err ? h("div", { className: "ao-sheet-error" }, err) : null,
        h("div", { className: "ao-sheet-actions" },
          h("button", { type: "button", className: "ao-btn ao-btn-ghost", onClick: props.onClose }, "Cancel"),
          h("button", { type: "submit", className: "ao-btn ao-btn-primary", disabled: busy || !message.trim() },
            busy ? "Sending…" : "Send")
        )
      )
    );
  }

  // --- Card detail: message classes -----------------------------------------
  // Three classes over the daemon's wire truth (probed live, 2026-11-09):
  // origin is the reliable provenance signal — "human" only for messages
  // typed in the AO app, "automation" for every REST /send call regardless
  // of the userAuthored flag, "provider" for assistant turns. authoredByUser
  // is self-declared by the sender and proves nothing (an orchestrator can
  // pass userAuthored: true), so it is never used to claim "End user":
  //   worker       — assistant turns (the harness's own output)
  //   user         — origin=human (End user)
  //   orchestrator — everything else non-assistant (Orchestrator / automation)
  const MSG_CLASSES = {
    worker: { label: "Worker", title: "The harness's own output (assistant turns)" },
    user: { label: "End user", title: "Typed by the session's user in the AO app (origin=human)" },
    orchestrator: { label: "Orchestrator / automation", title: "Injected via the REST /send path (origin=automation), or otherwise not a human or worker turn" },
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

  // --- Detail sheet (card click; read-only) --------------------------------

  function DetailSheet(props) {
    const [detail, setDetail] = useState(null);
    const [err, setErr] = useState(null);
    const [hiddenClasses, setHiddenClasses] = useState({});
    const session = props.session;

    function toggleClass(id) {
      setHiddenClasses(function (prev) {
        const next = {};
        Object.keys(prev).forEach(function (k) { next[k] = prev[k]; });
        if (next[id]) delete next[id]; else next[id] = true;
        return next;
      });
    }

    // Refetch when the session changes OR when its plan changes on a board
    // push (plan.writtenAt) — the sheet tracks the live plan like the board does.
    const planWrittenAt = (session.plan && session.plan.writtenAt) || "";
    useEffect(function () {
      let alive = true;
      setDetail(null); setErr(null);
      authedFetchJson(API + "/sessions/" + encodeURIComponent(session.id) + "/detail")
        .then(function (d) { if (alive) setDetail(d); })
        .catch(function (e) { if (alive) setErr(parseApiErrorMessage(e)); });
      return function () { alive = false; };
    }, [session.id, planWrittenAt]);

    const s = detail && detail.session;
    return h("div", { className: "ao-overlay", onClick: function (e) { if (e.target === e.currentTarget) props.onClose(); } },
      h("div", { className: "ao-sheet ao-sheet-detail" },
        h("div", { className: "ao-sheet-title" }, session.displayName),
        !detail && !err ? h("div", { className: "ao-loading" }, "Loading session detail…") : null,
        err ? h("div", { className: "ao-sheet-error" }, err) : null,
        s ? h("div", { className: "ao-detail-meta" },
          h("div", { className: "ao-detail-row" }, h("span", { className: "ao-detail-key" }, "Status"), h("span", null, s.displayStatus || s.status || "—")),
          h("div", { className: "ao-detail-row" }, h("span", { className: "ao-detail-key" }, "Project"), h("span", null, s.projectId || "standalone")),
          h("div", { className: "ao-detail-row" }, h("span", { className: "ao-detail-key" }, "Harness"), h("span", null, s.harness || "—")),
          h("div", { className: "ao-detail-row" }, h("span", { className: "ao-detail-key" }, "Branch"), h("span", { className: "ao-chip-branch" }, s.branch || "—")),
          h("div", { className: "ao-detail-row" }, h("span", { className: "ao-detail-key" }, "Created"), h("span", null, timeAgo(s.createdAt) + (s.createdAt ? " (" + s.createdAt + ")" : ""))),
          h("div", { className: "ao-detail-row" }, h("span", { className: "ao-detail-key" }, "AO link"), h("a", { className: "ao-chip-pr", href: s.aoLink }, s.aoLink))
        ) : null,
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
        s ? h("div", { className: "ao-detail-transcript" },
          h("div", { className: "ao-detail-key" },
            "Transcript" + (detail.transcriptTruncated ? " (last " + detail.transcript.length + " of " + detail.transcriptTotal + " messages)" : "")),
          h("div", { className: "ao-class-filters" },
            Object.keys(MSG_CLASSES).map(function (id) {
              const off = !!hiddenClasses[id];
              return h("button", {
                key: id,
                className: "ao-chip ao-class-chip ao-class-" + id + (off ? " is-off" : ""),
                title: MSG_CLASSES[id].title + (off ? " — hidden, click to show" : " — shown, click to hide"),
                onClick: function () { toggleClass(id); },
              }, (off ? "\u25CB " : "\u25CF ") + MSG_CLASSES[id].label);
            })),
          h("div", { className: "ao-chat" },
            (detail.transcript || []).filter(function (m) { return !hiddenClasses[messageClass(m)] && bubbleText(m); }).length === 0
              ? h("div", { className: "ao-column-empty" }, "No messages in the shown classes")
              : (detail.transcript || [])
                  .filter(function (m) { return !hiddenClasses[messageClass(m)] && bubbleText(m); })
                  .map(function (m, i) {
                    const cls = messageClass(m);
                    return h("div", { key: i, className: "ao-chat-row ao-chat-" + cls },
                      h("div", { className: "ao-chat-col" },
                        h("span", { className: "ao-chat-sender" }, MSG_CLASSES[cls].label),
                        h("div", { className: "ao-bubble" }, bubbleText(m))));
                  }))
        ) : null,
        h("div", { className: "ao-sheet-actions" },
          h("button", { type: "button", className: "ao-btn ao-btn-ghost", onClick: props.onClose }, "Close")
        )
      )
    );
  }

  // --- Board page ----------------------------------------------------------

  function AoBridgePage() {
    const [data, setData] = useState(null);
    const [error, setError] = useState(null);
    const [loaded, setLoaded] = useState(false);
    const [live, setLive] = useState(false);
    const [refreshedAt, setRefreshedAt] = useState(null);
    const [spawnOpen, setSpawnOpen] = useState(false);
    const [sendTarget, setSendTarget] = useState(null);
    const [detailTargetId, setDetailTargetId] = useState(null);
    // Derive the detail card from the live board so the sheet re-renders (and
    // refetches, via the plan-writtenAt effect below) on WS pushes.
    const detailCard = detailTargetId && data
      ? (data.cards || []).find(function (c) { return c.id === detailTargetId; })
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
    const [collapsed, setCollapsed] = useState(function () {
      try { return window.localStorage.getItem("ao-bridge.collapsed") === "archive"; }
      catch (_e) { return false; }
    });
    const timerRef = useRef(null);
    const wsRef = useRef(null);
    const iconMap = useHarnessIcons();

    const refresh = useCallback(function () {
      return authedFetchJson(API + "/board")
        .then(function (body) {
          setData(body); setError(null);
          setLoaded(true); setRefreshedAt(new Date());
        })
        .catch(function (e) { setError(parseApiErrorMessage(e)); })
        .finally(function () { setLoaded(true); });
    }, []);

    // Live updates: WebSocket /events (server polls the daemon, pushes on
    // change); falls back to 5s polling when the SDK has no WS helper or the
    // stream is refused.
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

      function onBoard(body) {
        if (closed) return;
        setData(body); setError(null);
        setLoaded(true); setLive(true); setRefreshedAt(new Date());
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
            } catch (_e) { /* ignore */ }
          };
          ws.onclose = function (e) {
            if (closed) return;
            if (e && e.code === 1008) {
              setError("Live stream unauthorized — falling back to polling");
              startPoll();
              return;
            }
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

    function toggleArchive() {
      setCollapsed(function (prev) {
        const next = !prev;
        try { window.localStorage.setItem("ao-bridge.collapsed", next ? "archive" : ""); } catch (_e) {}
        return next;
      });
    }

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

    const statusOptions = useMemo(function () {
      const seen = {};
      (data && data.cards || []).forEach(function (c) {
        const v = (c.displayStatus || c.status || "").toLowerCase();
        if (v) seen[v] = true;
      });
      return Object.keys(seen).sort();
    }, [data]);

    const harnessOptions = useMemo(function () {
      const seen = {};
      (data && data.cards || []).forEach(function (c) {
        const v = (c.harness || "").trim();
        if (v) seen[v] = true;
      });
      return Object.keys(seen).sort();
    }, [data]);

    const columns = useMemo(function () {
      if (!data) return [];
      const q = query.trim().toLowerCase();
      const filter = statusFilter !== "all"
        ? function (c) { return (c.displayStatus || c.status || "").toLowerCase() === statusFilter; }
        : null;
      const bucket = RECENCY_OPTIONS.find(function (r) { return r.id === recency; });
      const cutoffMs = bucket && bucket.ms ? Date.now() - bucket.ms : 0;
      const buckets = {};
      (data.cards || []).forEach(function (c) {
        if (q
            && (c.project || "").toLowerCase().indexOf(q) === -1
            && (c.displayName || "").toLowerCase().indexOf(q) === -1
            && (c.harness || "").toLowerCase().indexOf(q) === -1) return;
        if (filter && !filter(c)) return;
        if (projectFilter !== "all" && (c.projectId || "") !== projectFilter) return;
        if (harnessFilter !== "all" && (c.harness || "").trim() !== harnessFilter) return;
        if (activeOnly && (c.column === "archive" || INACTIVE_STATUSES[(c.status || "").toLowerCase()])) return;
        if (cutoffMs) {
          const ts = new Date(c.updatedAt || c.createdAt || "").getTime();
          if (!isFinite(ts) || ts < cutoffMs) return;
        }
        (buckets[c.column] = buckets[c.column] || []).push(c);
      });
      return (data.columns || []).map(function (col) {
        const b = buckets[col.id] || [];
        b.sort(function (a, b2) { return (b2.createdAt || "").localeCompare(a.createdAt || ""); });
        return { id: col.id, label: col.label, cards: b };
      });
    }, [data, query, statusFilter, projectFilter, harnessFilter, recency, activeOnly]);

    return h("div", { className: "ao-bridge" },
      h("div", { className: "ao-header" },
        h("div", { className: "ao-header-title" },
          h("span", { className: "ao-header-name" }, "AO Bridge"),
          data && data.baseUrl
            ? h("span", { className: "ao-header-daemon", title: data.baseUrl }, "daemon " + data.baseUrl.replace(/^https?:\/\//, ""))
            : null,
          h("span", { className: "ao-header-live" + (live ? " is-live" : "") },
            live ? "live" : "polling"),
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
          error ? h("span", { className: "ao-banner ao-banner-error" }, error) : null,
          h("button", { className: "ao-btn ao-btn-ghost", onClick: function () { refresh(); } }, "Refresh"),
          h("button", { className: "ao-btn ao-btn-primary", onClick: function () { setSpawnOpen(true); } }, "+ Spawn worker")
        )
      ),
      !loaded
        ? h("div", { className: "ao-loading" }, "Loading AO board…")
        : h("div", { className: "ao-board" },
            columns.map(function (col) {
              return h(Column, {
                key: col.id, id: col.id, label: col.label, cards: col.cards,
                iconMap: iconMap,
                collapsed: col.id === "archive" && collapsed,
                onToggle: col.id === "archive" ? toggleArchive : undefined,
                onChanged: refresh, onSend: setSendTarget,
                onOpen: function (card) { setDetailTargetId(card.id); },
              });
            })
          ),
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
      sendTarget
        ? h(SendSheet, {
            session: sendTarget,
            onClose: function () { setSendTarget(null); },
            onChanged: function () { setSendTarget(null); refresh(); },
          })
        : null,
      detailCard
        ? h(DetailSheet, {
            session: detailCard,
            onClose: function () { setDetailTargetId(null); },
          })
        : null
    );
  }

  if (window.__HERMES_PLUGINS__ && typeof window.__HERMES_PLUGINS__.register === "function") {
    window.__HERMES_PLUGINS__.register("ao-bridge", AoBridgePage);
  }
})();
